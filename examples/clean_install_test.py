"""Clean-install test for the frozen LocalCode.exe.

Copies the executable to an empty folder, starts it on a fresh port, verifies
first-run files, installs the llama.cpp engine from the real release, optionally
hardlinks an existing GGUF model, loads it and runs real inference, then quits.

Run after building:
    .venv\\Scripts\\python.exe examples\\clean_install_test.py

Environment:
    LOCALCODE_EXE      path to the executable (default dist\\LocalCode.exe)
    LOCALCODE_MODEL    model folder to link (default: first model under models\\).
                       Set LOCALCODE_MODEL=none to skip the model phase.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
EXE = Path(os.environ.get("LOCALCODE_EXE") or ROOT / "dist" / "LocalCode.exe").resolve()
PORT = int(os.environ.get("LOCALCODE_PORT") or 8770)
BASE = f"http://127.0.0.1:{PORT}"
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


def wait_health(timeout: float = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(BASE + "/api/health", timeout=2)
            if r.status_code == 200 and r.json().get("app") == "LocalCode":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def link_tree(src: Path, dst: Path) -> None:
    """Hardlink files (fast, same volume) with a copy fallback."""
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            source = Path(root) / name
            target = target_dir / name
            if target.exists():
                continue
            try:
                os.link(source, target)
            except Exception:
                shutil.copy2(source, target)


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
                                       "reason": "clean-install test"})
            elif event.get("type") == "ask_user":
                post("/api/ask", {"id": event["id"], "answer": "Use the simplest option."})
            elif event.get("type") == "error":
                print(f"       error: {event.get('message')}", flush=True)
            elif event.get("type") == "done":
                print(f"       done: {event.get('status')} in {time.time() - started:.1f}s",
                      flush=True)
    finally:
        resp.close()
    return events, text


def main() -> int:
    print(f"== clean install: {EXE} ==", flush=True)
    check("executable exists", EXE.exists(), str(EXE))
    if not EXE.exists():
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(os.environ.get("TEMP") or r"C:\Windows\Temp") / f"localcode-clean-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    exe = root / "LocalCode.exe"
    shutil.copy2(EXE, exe)
    print(f"   install dir: {root}", flush=True)

    model_source = None
    env_model = os.environ.get("LOCALCODE_MODEL")
    if env_model and env_model.lower() != "none":
        model_source = Path(env_model)
    elif env_model is None:
        candidates = [p for p in (ROOT / "models").glob("*/*/model.localcode.json")]
        if candidates:
            model_source = candidates[0].parent
    if model_source and not model_source.exists():
        print(f"   model source missing: {model_source}", flush=True)
        model_source = None
    proc = subprocess.Popen(
        [str(exe), "--port", str(PORT), "--no-browser"],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        print("\n== first launch ==", flush=True)
        up = wait_health()
        check("frozen exe starts and answers health", up)
        if not up:
            return 1
        check("first-run data folder created", (root / "data" / "localcode.db").exists(),
              str(root / "data"))
        cfg = json.loads((root / "data" / "config.json").read_text(encoding="utf-8"))
        check("fresh config saved", cfg.get("port") == PORT, str(cfg.get("port")))
        check("fresh config is not onboarded", cfg.get("onboarded") is False,
              str(cfg.get("onboarded")))
        index = get("/")
        check("UI served from bundled static files",
              index.status_code == 200 and "LocalCode" in index.text)
        info = get("/api/system/info").json()
        check("app_dir is next to the exe", Path(info.get("app_dir", "")).resolve() == root,
              str(info.get("app_dir")))
        check("no models on a clean install", get("/api/models").json().get("installed") == [],
              str(get("/api/models").json().get("installed")))
        if model_source:
            print(f"   linking model: {model_source}", flush=True)
            link_tree(model_source,
                      root / "models" / model_source.parent.name / model_source.name)
        rt = get("/api/runtime").json()
        check("engine reports not installed", rt.get("installed") is False, str(rt.get("installed")))

        print("\n== real llama.cpp runtime download ==", flush=True)
        r = post("/api/runtime/install", {"flavor": "cpu"})
        check("runtime install accepted", r.status_code == 202, r.text[:200])
        deadline = time.time() + 900
        state = {}
        while time.time() < deadline:
            state = get("/api/runtime").json().get("install") or {}
            if state.get("status") in {"complete", "error"}:
                break
            time.sleep(2)
        check("runtime installed from GitHub", state.get("status") == "complete",
              str(state)[:300])
        rt = get("/api/runtime").json()
        check("llama-server.exe present", rt.get("installed") is True and
              (root / "runtime" / "llama" / "llama-server.exe").exists(), str(rt)[:240])
        check("runtime version recorded", bool(rt.get("version")), str(rt.get("version")))
        check("runtime install left no archives",
              not any((root / "runtime" / "downloads").glob("*.zip")),
              str(list((root / "runtime" / "downloads").glob("*"))))

        if not model_source:
            print("\n== model phase skipped (no local GGUF) ==", flush=True)
        else:
            print("\n== real model load and inference ==", flush=True)
            installed = get("/api/models").json().get("installed") or []
            check("linked model discovered", len(installed) == 1,
                  str([m.get("id") for m in installed]))
            if installed:
                model = installed[0]
                r = post("/api/models/load", {"id": model["id"]})
                check("model load accepted", r.status_code == 200, r.text[:200])
                deadline = time.time() + 900
                status = {}
                while time.time() < deadline:
                    status = get("/api/runtime").json()
                    if status.get("ready") or status.get("error"):
                        break
                    time.sleep(2)
                check("model loaded", status.get("ready") is True, str(status.get("error"))[:300])
                if status.get("ready"):
                    sid = post("/api/sessions", {}).json()["session"]["id"]
                    events, text = stream_chat(
                        sid,
                        "Write a short Python function called multiply that returns the "
                        "product of two numbers. Reply with code only, no tools.",
                    )
                    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
                    check("real inference completed",
                          bool(done) and done.get("status") == "complete", str(done)[:300])
                    check("real answer is non-empty", len(text.strip()) > 10, text[:160])
                    check("real tokens recorded",
                          (done or {}).get("usage", {}).get("total_tokens", 0) > 0,
                          str((done or {}).get("usage")))
                    target = root / "workspace" / "clean-install-hello.py"
                    sid = post("/api/sessions", {}).json()["session"]["id"]
                    events, _ = stream_chat(
                        sid,
                        "Use the write_file tool to create a file named clean-install-hello.py "
                        "containing exactly print('Hello from clean install'). After it succeeds, "
                        "stop and say it is done. Do not call the same tool again.",
                    )
                    results = [e for e in events if e.get("type") == "tool_result"]
                    check("real tool call executed",
                          any(e.get("name") == "write_file" for e in results),
                          str([e.get("name") for e in results]))
                    check("tool wrote the file", target.exists(), str(target))
                    if target.exists():
                        check("tool file content correct",
                              "Hello from clean install" in target.read_text(
                                  encoding="utf-8", errors="replace"))

        print("\n== quit ==", flush=True)
        r = post("/api/system/quit", {})
        check("quit accepted", r.status_code == 200, r.text[:160])
        deadline = time.time() + 30
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.5)
        check("process exited", proc.poll() is not None, f"exit={proc.poll()}")
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        time.sleep(0.5)
        for _ in range(10):
            try:
                shutil.rmtree(root)
                break
            except Exception:
                time.sleep(0.5)

    print(f"\n{'=' * 64}\n{PASSES} checks passed, {len(FAILS)} failed", flush=True)
    for fail in FAILS:
        print("  FAIL:", fail, flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
