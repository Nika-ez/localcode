"""Validate every catalog entry against Hugging Face for real.

For each model in server.models.CATALOG this checks:
  * the repository exists and is publicly reachable
  * the requested quantization is found by LocalCode's parser
  * every GGUF part resolves to a downloadable URL (1 KB range request)
  * the vision projector exists and is downloadable when advertised
  * the advertised size is roughly correct

Run: .venv\\Scripts\\python.exe examples\\validate_catalog.py
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import models  # noqa: E402

TIMEOUT = 45
RANGE = {"Range": "bytes=0-1023", **models.HF_UA}
PASSES = 0
FAILS: list[str] = []


def ok(name: str) -> None:
    global PASSES
    PASSES += 1
    print(f"  ok   {name}", flush=True)


def fail(name: str, detail: str = "") -> None:
    FAILS.append(name + (f" - {detail}" if detail else ""))
    print(f"  FAIL {name}" + (f" - {detail}" if detail else ""), flush=True)


def reachable(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(url, headers=RANGE, allow_redirects=True, timeout=TIMEOUT, stream=True)
        try:
            if r.status_code not in (200, 206):
                return False, f"HTTP {r.status_code}"
            chunk = next(r.iter_content(256), b"")
            if not chunk:
                return False, "empty response"
            return True, f"HTTP {r.status_code}, {len(chunk)} bytes"
        finally:
            r.close()
    except Exception as exc:
        return False, str(exc)[:160]


def check_entry(entry: dict) -> None:
    mid = entry["id"]
    repo = entry["repo"]
    print(f"\n== {mid} ({repo}) ==", flush=True)

    try:
        r = requests.get(f"{models.HF_API}/models/{repo}", timeout=TIMEOUT)
    except Exception as exc:
        fail(f"{mid}: repository lookup", str(exc)[:160])
        return
    if r.status_code != 200:
        fail(f"{mid}: repository exists", f"HTTP {r.status_code}")
        return
    info = r.json() or {}
    gated = info.get("gated")
    if gated:
        fail(f"{mid}: repository is public", f"gated={gated}")
    else:
        ok(f"{mid}: repository is public")

    try:
        data = models.repo_quants(repo, force=True)
    except Exception as exc:
        fail(f"{mid}: file tree / quant parser", str(exc)[:160])
        return
    quant = models.pick_quant(data.get("quants") or [], entry.get("quant", ""))
    if not quant:
        fail(f"{mid}: quant {entry.get('quant')} found", "no quants parsed")
        return
    if entry.get("quant") and quant.get("quant") != entry["quant"].upper():
        fail(f"{mid}: exact quant match", f"wanted {entry['quant']}, got {quant.get('quant')}")
    else:
        ok(f"{mid}: quant {quant.get('quant')} found ({len(quant.get('files') or [])} part(s))")

    total = 0
    for file in quant.get("files") or []:
        path = file["path"]
        total += int(file.get("size") or 0)
        url = f"https://huggingface.co/{repo}/resolve/main/{path}"
        good, detail = reachable(url)
        if good:
            ok(f"{mid}: download {path.rsplit('/', 1)[-1]}")
        else:
            fail(f"{mid}: download {path}", detail)

    projector = None
    if entry.get("mmproj"):
        projectors = data.get("mmproj") or []
        if not projectors:
            fail(f"{mid}: vision projector listed")
        else:
            # Prefer the smallest projector, which is what users need.
            projector = min(projectors, key=lambda f: int(f.get("size") or 0))
            total += int(projector.get("size") or 0)
            url = f"https://huggingface.co/{repo}/resolve/main/{projector['path']}"
            good, detail = reachable(url)
            if good:
                ok(f"{mid}: projector {projector['path']} ({projector['size'] / models.GB:.2f} GB)")
            else:
                fail(f"{mid}: projector {projector['path']}", detail)

    size_gb = total / models.GB
    advertised = float(entry.get("size_gb") or 0)
    if advertised and abs(size_gb - advertised) / advertised > 0.35:
        fail(f"{mid}: advertised size", f"catalog {advertised:.2f} GB, actual {size_gb:.2f} GB")
    else:
        ok(f"{mid}: size ~{size_gb:.2f} GB")


def main() -> int:
    print(f"Validating {len(models.CATALOG)} catalog entries against Hugging Face\n", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(check_entry, models.CATALOG))

    print(f"\n{'=' * 64}\n{PASSES} checks passed, {len(FAILS)} failed", flush=True)
    for item in FAILS:
        print("  FAIL:", item, flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
