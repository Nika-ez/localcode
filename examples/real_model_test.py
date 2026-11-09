"""Real local-model test: load the installed GGUF and exercise inference + tools.

Run with the app already running:
    .venv\\Scripts\\python.exe examples\\real_model_test.py

Uses whatever model is installed; does not download anything. Slow on CPU.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("LOCALCODE_BASE", "http://127.0.0.1:8765").rstrip("/")
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
    return requests.get(BASE + path, timeout=60, **kw)


def post(path: str, payload=None, **kw):
    return requests.post(BASE + path, json=payload, timeout=60, **kw)


def patch(path: str, payload=None):
    return requests.patch(BASE + path, json=payload, timeout=60)


def stream_chat(session_id: str, message: str, *, approve: bool = True,
                timeout: int = 1800):
    events: list[dict] = []
    text = ""
    started = time.time()
    resp = requests.post(
        BASE + "/api/chat",
        json={"session_id": session_id, "message": message},
        stream=True, timeout=(15, timeout),
    )
    resp.raise_for_status()
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            events.append(event)
            kind = event.get("type")
            if kind == "delta":
                text += event.get("text") or ""
            elif kind == "approval":
                print(f"       approval: {event.get('tool')} ({event.get('risk')})", flush=True)
                post("/api/approval", {"id": event["id"], "approved": approve,
                                       "reason": "real-model test"})
            elif kind == "ask_user":
                print(f"       question: {event.get('question')}", flush=True)
                post("/api/ask", {"id": event["id"], "answer": "Use the simplest option."})
            elif kind == "tool_call":
                print(f"       tool_call: {event.get('name')} {event.get('args')}", flush=True)
            elif kind == "tool_result":
                print(f"       tool_result: {event.get('name')} {event.get('status')} "
                      f"{(event.get('output') or '')[:160]}", flush=True)
            elif kind == "error":
                print(f"       ERROR: {event.get('message')}", flush=True)
            elif kind == "done":
                print(f"       done: {event.get('status')} in {time.time() - started:.1f}s "
                      f"usage={event.get('usage')}", flush=True)
    finally:
        resp.close()
    return events, text


def done_of(events):
    for event in reversed(events):
        if event.get("type") == "done":
            return event
    return None


def main() -> int:
    print("== real model setup ==", flush=True)
    models = get("/api/models").json()
    installed = models.get("installed") or []
    if not installed:
        print("No installed model. Download one first.")
        return 2
    model = installed[0]
    print(f"   model: {model['name']} ({model['id']})", flush=True)
    r = patch("/api/settings", {
        "provider": "managed", "context_size": 0, "max_steps": 0,
        "approval_mode": "ask", "auto_compact": True,
    })
    check("switched to local provider", r.status_code == 200, r.text[:200])
    r = post("/api/models/load", {"id": model["id"]})
    print("   load:", r.status_code, flush=True)
    deadline = time.time() + 600
    status = {}
    while time.time() < deadline:
        status = get("/api/runtime").json()
        if status.get("ready") or status.get("error"):
            break
        time.sleep(2)
    check("model loaded", status.get("ready") is True, str(status.get("error")))
    if not status.get("ready"):
        return 1
    print(f"   context: {status.get('context_size')} tokens", flush=True)

    print("\n== real basic inference ==", flush=True)
    sid = post("/api/sessions", {}).json()["session"]["id"]
    events, text = stream_chat(
        sid,
        "Write a short Python function called multiply that returns the product of two numbers. "
        "Reply with code only, no tools.",
    )
    done = done_of(events)
    check("inference completed", bool(done) and done.get("status") == "complete", str(done))
    check("non-empty answer", len(text.strip()) > 10, text[:120])
    check("answer mentions def multiply", "multiply" in text.lower(), text[:200])
    check("tokens recorded", (done or {}).get("usage", {}).get("total_tokens", 0) > 0,
          str((done or {}).get("usage")))

    print("\n== real tool use ==", flush=True)
    sid = post("/api/sessions", {}).json()["session"]["id"]
    workspace = Path(get("/api/settings").json()["system"]["workspace"])
    target = workspace / "real-model-hello.py"
    if target.exists():
        target.unlink()
    events, text = stream_chat(
        sid,
        "Use the write_file tool to create a file named real-model-hello.py containing exactly "
        "print('Hello from LocalCode'). After the tool succeeds, stop and tell me it is done. "
        "Do not call the same tool again.",
    )
    done = done_of(events)
    results = [e for e in events if e.get("type") == "tool_result"]
    check("tool run finished cleanly", bool(done) and done.get("status") in {"complete", "loop"},
          str(done))
    check("write_file was attempted", any(e.get("name") == "write_file" for e in results),
          str([e.get("name") for e in results]))
    check("file created by the model", target.exists(), str(target))
    if target.exists():
        content = target.read_text(encoding="utf-8", errors="replace")
        check("file content is correct", "Hello from LocalCode" in content, content[:200])
    check("loop guard bounded the run", len(results) <= 4, f"{len(results)} tool attempts")

    print(f"\n{'=' * 60}\n{PASSES} passed, {len(FAILS)} failed", flush=True)
    for fail in FAILS:
        print("  FAIL:", fail, flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
