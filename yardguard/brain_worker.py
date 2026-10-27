"""LocateAnything worker: runs in its own venv (.venv-brain) and its own process.

The main app talks to it with JSON lines over stdin/stdout (see yardguard/brain.py):

    -> {"id": "1", "op": "ask", "image": "<jpeg path>", "phrases": ["person", ...],
        "mode": "fast", "max_side": 896, "seed": 0}
    <- {"id": "1", "ok": true, "boxes": [{"label", "x1", "y1", "x2", "y2", "score"}...],  # 0..1
        "raw": "...", "latency_s": 0.4, "vram_mb": 7900, "size": [w, h]}
    <- {"event": "ready", "load_s": 21.3, "vram_mb": 7600}   # once, after load + warm-up

Only protocol lines go to stdout; everything else (model prints, warnings) goes to
stderr, which the client writes to a log file.

This file must not import the yardguard package: it runs with the brain venv's python,
which does not have the app's dependencies. All paths come in as arguments.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import json
import os
import re
import sys
import time
import types
import warnings

PROTO = sys.stdout
sys.stdout = sys.stderr  # stray prints from the model code must not break the protocol

# Pure-python protobuf: Smart App Control may block the native extension.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_OFFLINE", "1")  # model is local; never phone home

# The answer is "<ref>label</ref>" followed by that label's boxes "<box><x1><y1><x2><y2></box>"
# (or "<box>None</box>" when nothing matches). Coordinates are normalised to 0..1000.
BOX_RE = re.compile(r"<box>\s*<(\d+)>\s*<(\d+)>\s*<(\d+)>\s*<(\d+)>\s*</box>")
REF_RE = re.compile(r"<ref>(.*?)</ref>", re.S)
JUNK_REF_RE = re.compile(r"(?:<\d+>\s*)+|\d*")  # a "label" that is only coordinate tokens
FULL_FRAME_AREA = 0.9  # boxes covering >= 90 % of the picture are dropped
PATCH = 28  # ViT patch 14 x merge 2: image sides must be multiples of this


def send(obj: dict) -> None:
    PROTO.write(json.dumps(obj, ensure_ascii=False) + "\n")
    PROTO.flush()


def log(msg: str) -> None:
    print(f"[brain {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _stub_unused_modules() -> None:
    """The model's processor imports decord/lmdb/cv2 at module level for video and dataset
    loading, which we never use. Register empty modules instead of editing the downloaded
    model files. They are deliberately not installed: the decord wheel breaks torch's DLL
    initialisation on Windows (WinError 1114), lmdb 3 needs cffi, and Smart App Control
    blocked cv2's DLL. Images are handled with Pillow."""
    for name in ("decord", "lmdb", "cv2"):
        mod = types.ModuleType(name)
        mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
        sys.modules[name] = mod


def _patch_sdpa() -> None:
    """The model runs attention inside sdp_kernel(flash, math, mem_efficient=False). With an
    explicit mask flash cannot run, so every call used the slow, memory-hungry math kernel.
    Allow the memory-efficient kernel too (same results, faster, less VRAM)."""
    import torch
    original = torch.backends.cuda.sdp_kernel

    def sdp_kernel(enable_flash=True, enable_math=True, enable_mem_efficient=True, enable_cudnn=True):
        return original(enable_flash=enable_flash, enable_math=enable_math,
                        enable_mem_efficient=True, enable_cudnn=enable_cudnn)

    torch.backends.cuda.sdp_kernel = sdp_kernel


class DecodeGuard:
    """Stops runaway decoding. With greedy decoding the model often follows its real boxes
    with a 'staircase' of invalid or near-identical boxes until max_new_tokens: seconds of
    GPU time and garbage boxes. generate() calls handle_pattern() for every parallel (MTP)
    block, so we wrap it: on the first invalid or duplicate box, or after too many boxes for
    one phrase, the block becomes <|im_end|>. Phrases never reached are asked again."""

    def __init__(self, module, max_per_label: int = 30) -> None:
        self.original = module.handle_pattern
        self.max_per_label = max_per_label
        self.enabled = True
        self.reset()
        module.handle_pattern = self._handle

    def reset(self) -> None:
        self.boxes: list[tuple[int, int, int, int]] = []
        self.stopped = False

    @staticmethod
    def _iou(a, b) -> float:
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / union if union > 0 else 1.0

    def _bad(self, box) -> bool:
        return (box[2] - box[0] < 3 or box[3] - box[1] < 3
                or len(self.boxes) >= self.max_per_label
                or any(self._iou(box, b) > 0.8 for b in self.boxes))

    def _handle(self, x0, token_ids, generation_mode="hybrid"):
        out = self.original(x0, token_ids, generation_mode)
        if not self.enabled:
            return out
        tokens = list(out.get("tokens", []))
        if out.get("type") == "ref_object":
            self.boxes = []  # the next phrase starts
            return out
        if out.get("type") != "coord_box" or len(tokens) < 5:
            return out
        cs, ce = token_ids["coord_start_token_id"], token_ids["coord_end_token_id"]
        coords = tokens[1:5]
        box = tuple(c - cs for c in coords) if all(cs <= c <= ce for c in coords) else None
        if box is None or self._bad(box):
            self.stopped = True
            return {"type": "im_end", "tokens": [token_ids["im_end_token_id"]],
                    "need_switch_to_ar": False, "is_terminal": True}
        self.boxes.append(box)
        return out


class Worker:
    def __init__(self, model_dir: str, guard: bool = True) -> None:
        _stub_unused_modules()
        warnings.filterwarnings("ignore")
        import torch
        from transformers import AutoConfig, AutoModel, AutoProcessor, AutoTokenizer
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        _patch_sdpa()
        self.torch = torch
        torch.backends.cuda.matmul.allow_tf32 = True
        cfg = AutoConfig.from_pretrained(model_dir, trust_remote_code=True)
        # No MagiAttention / flash-attn on Windows: PyTorch SDPA for text and vision.
        cfg._attn_implementation = "sdpa"
        cfg.text_config._attn_implementation = "sdpa"
        cfg.vision_config._attn_implementation = "sdpa"
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
        self.processor = AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)
        self.model = AutoModel.from_pretrained(
            model_dir, config=cfg, torch_dtype=torch.bfloat16, trust_remote_code=True,
            low_cpu_mem_usage=True, device_map="cuda",
        ).eval()
        self.guard = DecodeGuard(sys.modules[type(self.model).__module__])
        self.guard.enabled = guard

    def vram_mb(self) -> int:
        return int(self.torch.cuda.memory_reserved() / 2**20)

    @staticmethod
    def prepare(image, max_side: int):
        """Downscale so the long side is <= max_side, snapped to the 28 px patch grid
        (the processor would otherwise resample a second time)."""
        from PIL import Image
        w, h = image.size
        scale = min(1.0, max_side / max(w, h))
        nw = max(PATCH, round(w * scale / PATCH) * PATCH)
        nh = max(PATCH, round(h * scale / PATCH) * PATCH)
        return image if (nw, nh) == (w, h) else image.resize((nw, nh), Image.Resampling.BICUBIC)

    def generate(self, image, phrases: list[str], mode: str = "fast", max_new_tokens: int = 768,
                 repetition_penalty: float = 1.1, seed: int = 0) -> str:
        torch = self.torch
        query = "</c>".join(p.strip().rstrip(".") for p in phrases)
        question = f"Locate all the instances that matches the following description: {query}."
        messages = [{"role": "user", "content": [
            {"type": "image", "image": image}, {"type": "text", "text": question}]}]
        text = self.processor.py_apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        images, videos = self.processor.process_vision_info(messages)
        inputs = self.processor(text=[text], images=images, videos=videos, return_tensors="pt").to("cuda")
        self.guard.reset()
        # Fixed seed: sampling, but the same picture and seed always get the same answer. Callers vote
        # over several seeds when they need independent samples of one picture.
        torch.manual_seed(int(seed))
        with torch.inference_mode():
            out = self.model.generate(
                pixel_values=inputs["pixel_values"].to(torch.bfloat16),
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                image_grid_hws=inputs.get("image_grid_hws"),
                tokenizer=self.tokenizer,
                max_new_tokens=max_new_tokens,
                use_cache=True,
                generation_mode=mode,
                # The model card's settings. Greedy decoding (temperature 0) runs away into
                # "staircase" boxes until max_new_tokens; sampling with top_p ends cleanly
                # at <|im_end|>, because the decoder rejects coordinates outside the nucleus.
                do_sample=True,
                temperature=0.7,
                top_p=0.9,
                repetition_penalty=repetition_penalty,
            )
        return out[0] if isinstance(out, tuple) else out

    def ask(self, req: dict) -> dict:
        from PIL import Image
        t0 = time.perf_counter()
        with Image.open(req["image"]) as im:
            image = self.prepare(im.convert("RGB"), int(req.get("max_side", 896)))
        phrases = [str(p).strip() for p in req.get("phrases", []) if str(p).strip()]
        mode = req.get("mode", "fast")
        max_new = int(req.get("max_new_tokens", 768))
        seed = int(req.get("seed", 0))
        boxes, raws, todo = [], [], phrases
        # One generate call answers all phrases; if the guard cut it short, ask again
        # for the phrases the answer never reached (at most twice).
        for _ in range(3):
            if not todo:
                break
            raw = self.generate(image, todo, mode, max_new, seed=seed)
            raws.append(raw)
            boxes += parse_answer(raw, todo)
            answered = {m for m in (_match_label(r, todo) for r in REF_RE.findall(raw)) if m}
            if not answered and len(todo) == 1 and BOX_RE.search(raw):
                answered = {todo[0]}
            todo = [p for p in todo if p not in answered]
        self.torch.cuda.synchronize()
        return {"ok": True, "boxes": boxes, "raw": "\n".join(raws),
                "latency_s": round(time.perf_counter() - t0, 4), "vram_mb": self.vram_mb(),
                "size": list(image.size), "calls": len(raws)}

    def warm_up(self) -> None:
        from PIL import Image
        img = Image.new("RGB", (896, 504), (60, 60, 60))
        for mode in ("fast", "hybrid"):
            self.generate(img, ["person", "car"], mode, 64)


ARTICLES = {"a", "an", "the"}


def _norm(text: str) -> str:
    """Lower case, no punctuation, no articles: 'The person.' -> 'person'."""
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    return " ".join(w for w in words if w not in ARTICLES)


def _match_label(text: str, phrases: list[str]) -> str | None:
    """The asked phrase the model's label names, or None. Strict on purpose: the model sometimes
    writes a shorter label ("person" when asked "person lying under a car"), and a loose match
    would turn its plain person boxes into the behaviour that was asked about."""
    t = _norm(text)
    for p in phrases:
        if t and _norm(p) == t:
            return p
    return None


def parse_answer(raw: str, phrases: list[str]) -> list[dict]:
    """Labelled boxes with 0..1 coordinates. Boxes under a label that names none of the asked
    phrases are dropped; boxes before any <ref> count only when a single phrase was asked."""
    events = [(m.start(), "ref", m.group(1)) for m in REF_RE.finditer(raw)]
    events += [(m.start(), "box", m.groups()) for m in BOX_RE.finditer(raw)]
    events.sort(key=lambda e: e[0])
    label = phrases[0] if len(phrases) == 1 else None
    boxes = []
    for _, kind, val in events:
        if kind == "ref":
            if JUNK_REF_RE.fullmatch(val.strip()):
                continue  # "<ref><615></ref>": sampling noise, keep the previous label
            label = _match_label(val, phrases)
            continue
        if label is None:
            continue
        x1, y1, x2, y2 = (min(1000, max(0, int(v))) / 1000 for v in val)
        if x2 <= x1 or y2 <= y1:
            continue  # inverted / empty box, e.g. <598><106><611><94>
        if (x2 - x1) * (y2 - y1) >= FULL_FRAME_AREA:
            continue  # "the whole picture" (<0><0><999><998>) is never a real detection
        boxes.append({"label": label, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "score": None})
    return boxes


def serve(worker: Worker) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, op = req.get("id"), req.get("op")
        if op == "quit":
            break
        if op == "ping":
            send({"id": rid, "ok": True, "vram_mb": worker.vram_mb()})
            continue
        try:
            resp = worker.ask(req)
        except Exception as e:  # report and keep serving
            log(f"request failed: {e!r}")
            if "out of memory" in str(e).lower():
                worker.torch.cuda.empty_cache()
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        resp["id"] = rid
        send(resp)


def _watch_parent(pid: int) -> None:
    """Exit when the app process ends, even mid-load (stdin EOF only helps while serving)."""
    import ctypes
    import threading
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return

    def wait() -> None:
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()
    if args.parent_pid and os.name == "nt":
        _watch_parent(args.parent_pid)
    t0 = time.perf_counter()
    send({"event": "loading"})
    try:
        worker = Worker(args.model)
        log(f"model loaded in {time.perf_counter() - t0:.1f} s")
        if not args.no_warmup:
            worker.warm_up()
    except Exception as e:
        import traceback
        traceback.print_exc()
        send({"event": "error", "error": f"{type(e).__name__}: {e}"})
        return 1
    send({"event": "ready", "load_s": round(time.perf_counter() - t0, 2), "vram_mb": worker.vram_mb()})
    with contextlib.suppress(KeyboardInterrupt):
        serve(worker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
