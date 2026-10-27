"""Write requirements.txt and setup/torch-*.txt with exact versions and SHA-256 hashes.

For maintainers, after changing packages in the working .venv:
    run.cmd setup\\lock_requirements.py

Versions come from the running venv (pip freeze without the build-only tools). Hashes come
from PyPI's JSON API and the PyTorch wheel index; only Windows x64 / pure-Python wheels and
sdists are listed, which is what pip on a 64-bit Windows Python 3.12 can pick.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_ONLY = {"pip", "altgraph", "pefile", "pyinstaller", "pyinstaller-hooks-contrib", "pywin32-ctypes"}
TORCH = {"torch", "torchvision"}
# PyTorch is pinned here, not taken from the venv: Windows Smart App Control blocks the unsigned DLLs
# of builds it does not trust (Oct 2026: 2.11.0+cpu blocked with WinError 4551, 2.9.1+cpu loads; the
# torch_cuda.dll of every CUDA build tried, 2.7.1 .. 2.11.0, is blocked, so bootstrap.ps1 installs the
# cpu build while Smart App Control is on). Test a new version with Smart App Control on before raising this.
TORCH_PIN = {"torch": "2.9.1", "torchvision": "0.24.1"}
# Installed in the dev venv but not used by YardGuard: lap is only for Ultralytics' own trackers
# (YardGuard tracks objects itself), and Smart App Control blocks its _lapjv .pyd.
NOT_NEEDED = {"lap"}
TORCH_INDEX = {"cu128": "https://download.pytorch.org/whl/cu128", "cpu": "https://download.pytorch.org/whl/cpu"}
PY_TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "YardGuard-lock/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def usable(filename: str) -> bool:
    if filename.endswith((".tar.gz", ".zip")):
        return True
    if not filename.endswith(".whl"):
        return False
    plat = filename[:-4].split("-")[-1]
    return plat in ("any", "win_amd64")


def pypi_hashes(name: str, version: str) -> list[str]:
    data = json.loads(get(f"https://pypi.org/pypi/{name}/{version}/json"))
    hashes = sorted({f["digests"]["sha256"] for f in data["urls"] if usable(f["filename"])})
    if not hashes:
        raise SystemExit(f"no usable files for {name}=={version} on PyPI")
    return hashes


def torch_hashes(name: str, version: str, flavour: str) -> list[str]:
    page = get(f"{TORCH_INDEX[flavour]}/{name}/").decode("utf-8", "replace")
    want = f"{name}-{version}%2B{flavour}-{PY_TAG}-{PY_TAG}-win_amd64.whl"
    alt = want.replace("%2B", "+")
    hashes = sorted({m.group(2) for m in re.finditer(r'href="([^"]+)#sha256=([0-9a-f]{64})"', page)
                     if m.group(1).split("/")[-1] in (want, alt)})
    if not hashes:
        raise SystemExit(f"{want} not found on {TORCH_INDEX[flavour]}")
    return hashes


def block(spec: str, hashes: list[str]) -> str:
    return spec + "".join(f" \\\n    --hash=sha256:{h}" for h in hashes) + "\n"


def main() -> None:
    dists = {norm(d.metadata["Name"]): (d.metadata["Name"], d.version) for d in md.distributions()}
    common = []
    for key in sorted(dists):
        name, version = dists[key]
        if key in BUILD_ONLY or key in TORCH or key in NOT_NEEDED:
            continue
        print(f"  {name}=={version}")
        common.append(block(f"{name}=={version}", pypi_hashes(name, version)))
    head = ("# YardGuard: exact versions with hashes for Windows x64, Python 3.12.\n"
            "# Made by setup/lock_requirements.py. PyTorch is in setup/torch-cu128.txt or torch-cpu.txt,\n"
            "# picked by setup/bootstrap.ps1 (NVIDIA GPU or not).\n")
    (ROOT / "requirements.txt").write_text(head + "".join(common), encoding="utf-8")
    for flavour in TORCH_INDEX:
        lines = [f"# PyTorch for {'NVIDIA GPUs (CUDA 12.8)' if flavour == 'cu128' else 'the processor only'}."
                 " Made by setup/lock_requirements.py.\n",
                 f"--extra-index-url {TORCH_INDEX[flavour]}\n"]
        for key in ("torch", "torchvision"):
            version = TORCH_PIN[key]
            print(f"  {key}=={version}+{flavour}")
            lines.append(block(f"{key}=={version}+{flavour}", torch_hashes(key, version, flavour)))
        (ROOT / "setup" / f"torch-{flavour}.txt").write_text("".join(lines), encoding="utf-8")
    print("Wrote requirements.txt, setup/torch-cu128.txt, setup/torch-cpu.txt")


if __name__ == "__main__":
    main()
