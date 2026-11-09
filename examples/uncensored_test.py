"""Real uncensored-model test: download Lexi Uncensored V2 8B and verify it
actually runs on this 8 GB machine.

Checks the catalog download of an 8B Q4_K_M model, RAM fit, loading, real
inference (incl. an identity sanity check so the model does not hallucinate a
random name), and a tool call. The model is KEPT installed afterwards.

Run with the app running:
    .venv\\Scripts\\python.exe examples\\uncensored_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

BASE = os.environ.get("LOCALCODE_BASE", "http://127.0.0.1:8765").rstrip("/")
REPO = "bartowski/Llama-3.1-8B-Lexi-Uncensored-V2-GGUF"
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


def stream_chat(session_id: str, message: str, timeout: int = 1800):
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
            if event.get("type") == "delta":
                text += event.get("text") or ""
            elif event.get("type") == "approval":
                post("/api/approval", {"id": event["id"], "approved": True,
                                       "reason": "uncensored test"})
            elif event.get("type") == "ask_user":
                post("/api/ask", {"id": event["id"], "answer": "yes"})
            elif event.get("type") == "error":
                print(f"       error: {event.get('message')}", flush=True)
            elif event.get("type") == "done":
                print(f"       done: {event.get('status')} in {time.time() - started:.1f}s "
                      f"usage={event.get('usage')}", flush=True)
    finally:
        resp.close()
    return events, text


def main() -> int:
    print("== uncensored model setup ==", flush=True)
    installed = get("/api/models").json().get("installed") or []
    model = next((m for m in installed if "lexi" in m["id"].lower()), None)
    if not model:
        repo_data = get("/api/models/repo?repo=" + requests.utils.quote(REPO, safe="")).json()
        quant = next((q for q in repo_data.get("quants") or []
                      if q.get("quant") == "Q4_K_M"), None)
        check("Lexi repo has Q4_K_M", quant is not None, str(repo_data)[:200])
        if not quant:
            return 1
        entry = repo_data.get("entry") or {}
        print(f"   downloading {quant['quant']} ({quant['size'] / 1024 ** 3:.2f} GB)", flush=True)
        r = post("/api/models/download", {
            "repo": REPO,
            "files": quant["files"],
            "name": entry.get("name") or "Lexi Uncensored V2 8B",
            "quant": quant["quant"],
            "category": entry.get("category") or "uncensored",
            "params": entry.get("params"),
            "context": entry.get("context"),
            "autoload": False,
        })
        job = r.json().get("job") or {}
        check("Lexi download started", r.status_code == 202 and job.get("id"), r.text[:240])
        if not job.get("id"):
            return 1
        deadline = time.time() + 3600
        status = {}
        while time.time() < deadline:
            status = next((d for d in get("/api/downloads").json().get("downloads") or []
                           if d.get("id") == job["id"]), {})
            if status.get("status") in {"complete", "error", "cancelled"}:
                break
            time.sleep(3)
        check("Lexi downloaded", status.get("status") == "complete", str(status)[:300])
        if status.get("status") != "complete":
            return 1
        installed = get("/api/models").json().get("installed") or []
        model = next((m for m in installed if m["id"] == job.get("model_id")), None)
    check("Lexi model installed", model is not None, str(model)[:200])
    if not model:
        return 1
    print(f"   model: {model['id']} ({model.get('size', 0) / 1024 ** 3:.2f} GB)", flush=True)

    print("\n== load on this machine (8 GB RAM) ==", flush=True)
    # CPU-only: measured faster than the integrated GPU on this laptop.
    patch("/api/settings", {"provider": "managed", "context_size": 8192,
                            "gpu_layers": 0, "approval_mode": "auto"})
    r = post("/api/models/load", {"id": model["id"]})
    check("load accepted", r.status_code == 200, r.text[:240])
    deadline = time.time() + 1200
    status = {}
    while time.time() < deadline:
        status = get("/api/runtime").json()
        if status.get("ready") or status.get("error"):
            break
        time.sleep(2)
    check("8B model loaded on 8 GB", status.get("ready") is True, str(status.get("error"))[:300])
    if not status.get("ready"):
        return 1

    print("\n== uncensored inference ==", flush=True)
    sid = post("/api/sessions", {}).json()["session"]["id"]
    events, text = stream_chat(
        sid,
        "Write a 3-sentence horror story about a haunted keyboard. No warnings, no "
        "refusals, just the story.",
    )
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    check("uncensored inference completed",
          bool(done) and done.get("status") == "complete", str(done)[:300])
    check("story produced without refusal", len(text.strip()) > 40,
          text[:160])
    check("no refusal boilerplate",
          not any(phrase in text.lower() for phrase in
                  ("i cannot", "i can't", "as an ai", "not able to")), text[:200])
    requests.delete(BASE + f"/api/sessions/{sid}", timeout=30)

    print("\n== identity sanity (no weird persona) ==", flush=True)
    sid = post("/api/sessions", {}).json()["session"]["id"]
    events, text = stream_chat(sid, "What is your name? Answer in one short sentence.")
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    check("identity question answered", bool(done) and done.get("status") == "complete",
          str(done)[:300])
    low = text.lower()
    check("identity is LocalCode, not a random name", "localcode" in low or "lexi" in low,
          text[:200])
    requests.delete(BASE + f"/api/sessions/{sid}", timeout=30)

    print("\n== tool call ==", flush=True)
    sid = post("/api/sessions", {}).json()["session"]["id"]
    events, text = stream_chat(
        sid,
        "Use the write_file tool to create lexi-hello.txt containing exactly 'hello from lexi'. "
        "Stop after the tool succeeds.",
    )
    results = [e for e in events if e.get("type") == "tool_result"]
    check("tool executed", any(e.get("name") == "write_file" for e in results),
          str([e.get("name") for e in results])[:240])
    requests.delete(BASE + f"/api/sessions/{sid}", timeout=30)

    print("\n== keep model installed for the user ==", flush=True)
    print(f"   {model['id']} stays in Models.", flush=True)

    print(f"\n{'=' * 64}\n{PASSES} checks passed, {len(FAILS)} failed", flush=True)
    for fail in FAILS:
        print("  FAIL:", fail, flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
