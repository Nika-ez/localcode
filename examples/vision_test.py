"""Real vision test: download Gemma 3 4B + mmproj, load it, and read an image.

This exercises the complete image path with a real multimodal GGUF:
catalog download (main model + projector), llama.cpp --mmproj startup,
attachment storage, OpenAI-style image_url parts, and model inference.

Run with the dev app running:
    .venv\\Scripts\\python.exe examples\\vision_test.py

Set LOCALCODE_KEEP_MODEL=1 to keep the ~3.1 GB model after the test.
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

import requests

BASE = os.environ.get("LOCALCODE_BASE", "http://127.0.0.1:8765").rstrip("/")
REPO = "unsloth/gemma-3-4b-it-GGUF"
FAILS: list[str] = []
PASSES = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok   {name}", flush=True)
    else:
        FAILS.append(name + (f" - {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""), flush=True)


def get(path: str, **kw):
    return requests.get(BASE + path, timeout=90, **kw)


def post(path: str, payload=None, **kw):
    return requests.post(BASE + path, json=payload, timeout=90, **kw)


def patch(path: str, payload=None):
    return requests.patch(BASE + path, json=payload, timeout=90)


def png_circle(size: int = 384, color=(220, 30, 30)) -> bytes:
    """Minimal PNG: white background with a filled circle."""
    cx = cy = size / 2
    radius = size * 0.34
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
            row += bytes(color if inside else (255, 255, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) +
            chunk(b"IEND", b""))


def stream_chat(session_id: str, message: str, attachments: list[str],
                timeout: int = 1800):
    events: list[dict] = []
    text = ""
    resp = requests.post(
        BASE + "/api/chat",
        json={"session_id": session_id, "message": message, "attachments": attachments},
        stream=True, timeout=(15, timeout),
    )
    resp.raise_for_status()
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            events.append(event)
            if event.get("type") == "delta":
                text += event.get("text") or ""
            elif event.get("type") == "approval":
                post("/api/approval", {"id": event["id"], "approved": True,
                                       "reason": "vision test"})
            elif event.get("type") == "ask_user":
                post("/api/ask", {"id": event["id"], "answer": "Red."})
            elif event.get("type") == "error":
                print(f"       error: {event.get('message')}", flush=True)
            elif event.get("type") == "done":
                print(f"       done: {event.get('status')} usage={event.get('usage')}",
                      flush=True)
    finally:
        resp.close()
    return events, text


def main() -> int:
    print("== vision model setup ==", flush=True)
    installed = get("/api/models").json().get("installed") or []
    model = next((m for m in installed if "gemma-3-4b" in m["id"] and m.get("vision_ready")), None)
    downloaded = False
    if not model:
        repo_data = get("/api/models/repo?repo=" + requests.utils.quote(REPO, safe="")).json()
        quant = next((q for q in repo_data.get("quants") or []
                      if q.get("quant") == "Q4_K_M"), None)
        projectors = repo_data.get("mmproj") or []
        check("vision repo has Q4_K_M", quant is not None, str(repo_data)[:200])
        check("vision repo has a projector", bool(projectors), str(repo_data)[:200])
        if not quant or not projectors:
            return 1
        projector = min(projectors, key=lambda p: int(p.get("size") or 0))
        entry = repo_data.get("entry") or {}
        print(f"   downloading {quant['quant']} + {projector['path']} "
              f"({(quant['size'] + projector['size']) / 1024 ** 3:.2f} GB)", flush=True)
        r = post("/api/models/download", {
            "repo": REPO,
            "files": quant["files"],
            "mmproj": [projector],
            "name": entry.get("name") or "Gemma 3 4B IT (vision)",
            "quant": quant["quant"],
            "category": entry.get("category") or "starter",
            "params": entry.get("params"),
            "context": entry.get("context"),
            "autoload": False,
        })
        job = r.json().get("job") or {}
        check("vision download started", r.status_code == 202 and job.get("id"), r.text[:240])
        if not job.get("id"):
            return 1
        downloaded = True
        deadline = time.time() + 2400
        status = {}
        while time.time() < deadline:
            status = next((d for d in get("/api/downloads").json().get("downloads") or []
                           if d.get("id") == job["id"]), {})
            if status.get("status") in {"complete", "error", "cancelled"}:
                break
            time.sleep(3)
        check("vision download completed", status.get("status") == "complete", str(status)[:300])
        if status.get("status") != "complete":
            return 1
        installed = get("/api/models").json().get("installed") or []
        model = next((m for m in installed if m["id"] == job.get("model_id")), None)
    check("vision model installed", model is not None and model.get("vision_ready") is True,
          str(model)[:300])
    if not model:
        return 1
    print(f"   model: {model['id']} ({model.get('size', 0) / 1024 ** 3:.2f} GB)", flush=True)

    print("\n== load with projector ==", flush=True)
    patch("/api/settings", {"provider": "managed", "context_size": 0,
                            "approval_mode": "auto"})
    r = post("/api/models/load", {"id": model["id"]})
    check("vision model load accepted", r.status_code == 200, r.text[:240])
    deadline = time.time() + 900
    status = {}
    while time.time() < deadline:
        status = get("/api/runtime").json()
        if status.get("ready") or status.get("error"):
            break
        time.sleep(2)
    check("vision model loaded", status.get("ready") is True, str(status.get("error"))[:300])
    check("mmproj projector active", bool(status.get("mmproj_path")),
          str(status.get("mmproj_path")))
    # Auto context sizing must leave room for the image prompt.
    check("auto context sized for vision", int(status.get("context_size") or 0) >= 12288,
          str(status.get("context_size")))
    if not status.get("ready"):
        return 1

    print("\n== capabilities ==", flush=True)
    caps = get("/api/capabilities?model_id=" +
               requests.utils.quote(model["id"], safe="")).json().get("capabilities") or {}
    check("capabilities advertise images", caps.get("image") is True, str(caps)[:240])
    check("capabilities badge Images is on",
          any(b.get("key") == "image" and b.get("on") for b in caps.get("badges") or []),
          str(caps.get("badges"))[:240])

    print("\n== image understanding ==", flush=True)
    image = png_circle()
    r = requests.post(BASE + "/api/attachments",
                      files={"file": ("red-circle.png", io.BytesIO(image), "image/png")},
                      timeout=90)
    att = r.json().get("attachment") or {}
    check("image attachment stored", r.status_code == 201 and att.get("kind") == "image",
          r.text[:200])
    sid = post("/api/sessions", {}).json()["session"]["id"]
    events, text = stream_chat(
        sid,
        "Look at the attached image. What color is the shape? Answer with one word.",
        [att["id"]] if att.get("id") else [],
    )
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    check("vision inference completed",
          bool(done) and done.get("status") == "complete", str(done)[:300])
    check("vision answer names the color", "red" in text.lower(), text[:200])

    print("\n== cleanup ==", flush=True)
    if att.get("id"):
        requests.delete(BASE + f"/api/attachments/{att['id']}", timeout=30)
    requests.delete(BASE + f"/api/sessions/{sid}", timeout=30)
    if downloaded and os.environ.get("LOCALCODE_KEEP_MODEL") != "1" and not FAILS:
        r = post("/api/models/delete", {"id": model["id"]})
        check("vision model deleted after test", r.status_code == 200, r.text[:160])
    else:
        print("   model kept", flush=True)

    print(f"\n{'=' * 64}\n{PASSES} checks passed, {len(FAILS)} failed", flush=True)
    for fail in FAILS:
        print("  FAIL:", fail, flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
