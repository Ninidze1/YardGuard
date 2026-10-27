"""Compare decoding settings on one frame (run with the brain venv python)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "yardguard"))
from local_data import BRAIN_MODEL, SNAPS, need_brain, need_snapshots  # noqa: E402

need_brain()
need_snapshots("check_ch5_20261023_010046.jpg")
import brain_worker as bw  # noqa: E402

math_only = "--math" in sys.argv
w = bw.Worker(str(BRAIN_MODEL), mem_efficient=not math_only)
from PIL import Image  # noqa: E402

path = str(SNAPS / "check_ch5_20261023_010046.jpg")
im = w._prepare(Image.open(path).convert("RGB"), 896)
variants = [
    dict(repetition_penalty=1.0, temperature=0, top_p=0.9, generation_mode="fast"),
    dict(repetition_penalty=1.1, temperature=0, top_p=0.9, generation_mode="fast"),
    dict(repetition_penalty=1.0, temperature=0, top_p=0.9, generation_mode="hybrid"),
    dict(repetition_penalty=1.1, temperature=0, top_p=0.9, generation_mode="hybrid"),
    dict(repetition_penalty=1.1, temperature=0.7, top_p=0.9, generation_mode="hybrid"),
]
torch = w.torch
for v in variants * 2:
    query = "person</c>car"
    question = f"Locate all the instances that matches the following description: {query}."
    messages = [{"role": "user", "content": [{"type": "image", "image": im}, {"type": "text", "text": question}]}]
    text = w.processor.py_apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    images, videos = w.processor.process_vision_info(messages)
    inputs = w.processor(text=[text], images=images, videos=videos, return_tensors="pt").to("cuda")
    t = time.perf_counter()
    out = w.model.generate(pixel_values=inputs["pixel_values"].to(torch.bfloat16), input_ids=inputs["input_ids"],
                           attention_mask=inputs["attention_mask"], image_grid_hws=inputs.get("image_grid_hws"),
                           tokenizer=w.tokenizer, max_new_tokens=600, use_cache=True, 
                           do_sample=v["temperature"] > 0, **v)
    torch.cuda.synchronize()
    print(f"\n== math={math_only} {v} {time.perf_counter() - t:.2f}s", file=bw.PROTO)
    print(repr(out[:700]), file=bw.PROTO, flush=True)
