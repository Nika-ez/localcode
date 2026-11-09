"""End-to-end agent tests for LocalCode using the deterministic mock model.

Start the app and the mock server, then run:
    .venv\\Scripts\\python.exe examples\\e2e_agent.py

Covers: streaming, prompt-based tools, approvals (approve + deny), ask_user,
shell tools, native tool calls, MCP tools, queue steering, interrupt steering,
stop, reconnect/background continuation, auto-compaction and usage/cost.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("LOCALCODE_BASE", "http://127.0.0.1:8765").rstrip("/")
MOCK = os.environ.get("LOCALCODE_MOCK", "http://127.0.0.1:8899/v1").rstrip("/")
FAILS: list[str] = []
PASSES = 0
WORKSPACE = Path("workspace")


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name + (f" - {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def post(path: str, payload=None, **kw):
    return requests.post(BASE + path, json=payload, timeout=60, **kw)


def get(path: str, **kw):
    return requests.get(BASE + path, timeout=30, **kw)


def patch(path: str, payload=None):
    return requests.patch(BASE + path, json=payload, timeout=30)


def new_session() -> str:
    resp = post("/api/sessions", {})
    resp.raise_for_status()
    return resp.json()["session"]["id"]


def session_state(session_id: str) -> dict:
    resp = get(f"/api/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json()


def stream_chat(session_id: str, message: str, *, approve: bool | None = None,
                answer: str | None = None, close_after_deltas: int = 0,
                during=None, timeout: int = 180):
    """Stream one chat run, resolving approvals/questions as they arrive."""
    events: list[dict] = []
    delta_count = 0
    resp = requests.post(
        BASE + "/api/chat",
        json={"session_id": session_id, "message": message},
        stream=True,
        timeout=(10, timeout),
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
                delta_count += 1
            if kind == "approval":
                print(f"       approval requested: {event.get('tool')} risk={event.get('risk')}")
                if approve is not None:
                    post("/api/approval", {
                        "id": event["id"], "approved": approve,
                        "reason": "e2e approve" if approve else "e2e deny",
                    })
            elif kind == "ask_user":
                print(f"       question: {event.get('question')}")
                if answer is not None:
                    post("/api/ask", {"id": event["id"], "answer": answer})
            elif kind == "error":
                print(f"       ERROR event: {event.get('message')}")
            if close_after_deltas and delta_count >= close_after_deltas:
                resp.close()
                break
            if during:
                during(kind, delta_count, events)
    finally:
        resp.close()
    return events


def event_types(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def tool_results(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "tool_result"]


def final_done(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("type") == "done":
            return event
    return None


def text_of(events: list[dict]) -> str:
    return "".join(e.get("text") or "" for e in events if e.get("type") == "delta")


def main() -> int:
    global WORKSPACE
    print("== setup ==")
    try:
        models = requests.get(MOCK + "/models", timeout=5).json()
        print("   mock model:", models["data"][0]["id"])
    except Exception as exc:
        print("Mock model server is not running on", MOCK, "-", exc)
        return 2
    health = get("/api/health").json()
    check("app reachable", health.get("ok") is True, str(health))
    settings_payload = get("/api/settings").json()
    WORKSPACE = Path(settings_payload["system"]["workspace"])
    print("   workspace:", WORKSPACE)

    resp = patch("/api/settings", {
        "provider": "openai",
        "external_base_url": MOCK,
        "external_model": "mock-coder",
        "approval_mode": "ask",
        "auto_compact": True,
        "context_size": 8192,
        "compact_threshold": 0.72,
        "max_steps": 0,
    })
    check("settings switched to mock provider", resp.status_code == 200, resp.text[:200])
    provider = get("/api/settings").json().get("provider", {})
    check("provider reports mock model", provider.get("model") == "mock-coder", str(provider))

    # ---------------------------------------------------------------- basics
    print("\n== basic streaming chat ==")
    sid = new_session()
    events = stream_chat(sid, "hello there")
    check("stream produced deltas", "delta" in event_types(events), str(event_types(events)))
    check("basic reply text", "deterministic mock model" in text_of(events), text_of(events)[:120])
    done = final_done(events)
    check("basic run completed", bool(done) and done.get("status") == "complete", str(done))
    state = session_state(sid)
    check("assistant message stored", any(
        m["role"] == "assistant" and m["status"] == "complete" for m in state["messages"]))
    check("usage tokens recorded", state["usage"]["total_tokens"] > 0, str(state["usage"]))
    check("hosted cost estimate recorded",
          state["usage"]["cost_usd"] > 0 or state["usage"]["session_cost_usd"] > 0,
          str(state["usage"]))
    check("session title set from message", state["session"]["title"] == "hello there",
          state["session"]["title"])

    # ------------------------------------------------------------ write tool
    print("\n== write tool with approval ==")
    sid = new_session()
    events = stream_chat(sid, "write a file for me", approve=True)
    approvals = [e for e in events if e.get("type") == "approval"]
    check("approval requested for write_file", len(approvals) == 1
          and approvals[0].get("tool") == "write_file", str(approvals))
    results = tool_results(events)
    check("write_file executed ok", len(results) == 1 and results[0].get("status") == "ok",
          str(results)[:300])
    target = WORKSPACE / "mock-output.txt"
    check("file exists in workspace", target.exists(), str(target))
    check("file has mock content", target.exists()
          and "hello from the mock model" in target.read_text(encoding="utf-8", errors="replace"))
    check("final answer after tool", "wrote the file" in text_of(events), text_of(events)[:120])
    check("write run complete", final_done(events) and final_done(events)["status"] == "complete")

    # ----------------------------------------------------------- deny tool
    print("\n== tool denial ==")
    target.write_text("keep me\n", encoding="utf-8")
    sid = new_session()
    events = stream_chat(sid, "write a file for me", approve=False)
    results = tool_results(events)
    check("denied tool reported", len(results) == 1 and results[0].get("status") == "denied",
          str(results)[:300])
    check("file untouched after denial", target.read_text(encoding="utf-8") == "keep me\n")
    check("denial run complete", final_done(events) and final_done(events)["status"] == "complete")

    # -------------------------------------------------------------- ask_user
    print("\n== ask_user ==")
    sid = new_session()
    events = stream_chat(sid, "ask me which framework to use", answer="Plain HTML/CSS")
    asks = [e for e in events if e.get("type") == "ask_user"]
    check("ask_user event emitted", len(asks) == 1, str(asks))
    results = tool_results(events)
    check("answer stored as tool result", len(results) == 1
          and results[0].get("status") == "ok"
          and "Plain HTML/CSS" in str(results[0].get("output")), str(results)[:300])
    check("thanks after answer", "used your answer" in text_of(events), text_of(events)[:120])

    # ------------------------------------------------------------ shell tool
    print("\n== shell command tool ==")
    sid = new_session()
    events = stream_chat(sid, "run a shell command", approve=True)
    results = tool_results(events)
    check("run_command ok", len(results) == 1 and results[0].get("status") == "ok",
          str(results)[:300])
    check("shell output captured", "mock-shell-ok" in str(results[0].get("output")),
          str(results)[:300])

    # ---------------------------------------------------------- native tools
    print("\n== native tool_calls protocol ==")
    sid = new_session()
    events = stream_chat(sid, "use a native tool call", approve=True)
    calls = [e for e in events if e.get("type") == "tool_call"]
    check("native tool_call parsed", len(calls) == 1 and calls[0].get("name") == "list_files",
          str(calls))
    results = tool_results(events)
    check("native tool executed", len(results) == 1 and results[0].get("status") == "ok",
          str(results)[:300])
    check("no literal tool markup leaked", "<tool>" not in text_of(events), text_of(events)[:120])

    # ------------------------------------------------------------------ MCP
    print("\n== MCP tool through the agent loop ==")
    mcp_state = get("/api/mcp").json()
    if not any(s.get("name") == "everything" for s in mcp_state.get("servers", [])):
        added = post("/api/mcp/presets", {"preset": "everything", "start": True})
        print("   added everything preset:", added.status_code)
    # npx presets need Node. Install the portable Node if this is a fresh
    # install (this also exercises the real Node downloader).
    node = get("/api/mcp/node").json()
    if not node.get("installed"):
        started = post("/api/mcp/node/install", {})
        print("   portable Node install:", started.status_code)
        deadline = time.time() + 600
        while time.time() < deadline:
            node = get("/api/mcp/node").json()
            if node.get("installed") or node.get("status") == "error":
                break
            time.sleep(3)
        if node.get("installed"):
            # Restart the preset now that Node exists.
            post("/api/mcp/servers/everything/restart", {})
    deadline = time.time() + 120
    while time.time() < deadline:
        mcp_state = get("/api/mcp").json()
        one = next((s for s in mcp_state.get("servers", [])
                    if s.get("name") == "everything"), {})
        if one.get("status") == "ready":
            break
        time.sleep(1)
    ready = [s for s in mcp_state.get("servers", []) if s.get("status") == "ready"]
    if ready:
        sid = new_session()
        events = stream_chat(sid, "use the mcp echo tool", approve=True)
        results = tool_results(events)
        check("MCP tool executed", len(results) == 1
              and results[0].get("name") == "mcp__everything__echo"
              and results[0].get("status") == "ok", str(results)[:400])
        check("MCP echo returned payload", "hello from LocalCode" in str(
            results[0].get("output") if results else ""), str(results)[:400])
        check("MCP final answer", "MCP echo tool answered" in text_of(events),
              text_of(events)[:120])
    else:
        check("MCP server ready", False, "no ready MCP servers")

    # ------------------------------------------------------- queue steering
    print("\n== queue a message during a run ==")
    sid = new_session()
    queued: list[dict] = []

    def queue_mid_run(kind, deltas, evs):
        if kind == "delta" and deltas == 3 and not queued:
            r = post("/api/chat/queue", {
                "session_id": sid, "message": "hello queued message", "interrupt": False,
            })
            queued.append(r.json())
            print("       queued:", r.json())

    events = stream_chat(sid, "slow long answer please", during=queue_mid_run)
    check("queue accepted while active", queued and queued[0].get("active") is True, str(queued))
    user_msgs = [e for e in events if e.get("type") == "user_message"]
    check("queued message delivered", len(user_msgs) == 1
          and user_msgs[0]["message"]["content"] == "hello queued message", str(user_msgs)[:300])
    check("two assistant turns for queue", len([e for e in events if e.get("type") == "assistant_start"]) >= 2,
          str(event_types(events)))
    check("queue run complete", final_done(events) and final_done(events)["status"] == "complete")
    state = session_state(sid)
    check("queued user message persisted", any(
        m["role"] == "user" and m["content"] == "hello queued message" for m in state["messages"]))

    # --------------------------------------------------- interrupt steering
    print("\n== interrupt & send now ==")
    sid = new_session()
    steered: list[dict] = []

    def steer_mid_run(kind, deltas, evs):
        if kind == "delta" and deltas == 3 and not steered:
            r = post("/api/chat/queue", {
                "session_id": sid, "message": "hello after interrupt", "interrupt": True,
            })
            steered.append(r.json())
            print("       steered:", r.json())

    events = stream_chat(sid, "slow long answer please", during=steer_mid_run)
    check("interrupt accepted", steered and steered[0].get("active") is True, str(steered))
    user_msgs = [e for e in events if e.get("type") == "user_message"]
    check("steer message delivered", len(user_msgs) == 1
          and user_msgs[0].get("mode") == "steer", str(user_msgs)[:300])
    check("steered reply answered new message", "deterministic mock model" in text_of(events),
          text_of(events)[:160])
    check("steer run complete", final_done(events) and final_done(events)["status"] == "complete")

    # ------------------------------------------------------------------ stop
    print("\n== stop a running answer ==")
    sid = new_session()
    stopped: list[dict] = []

    def stop_mid_run(kind, deltas, evs):
        if kind == "delta" and deltas == 3 and not stopped:
            stopped.append(post("/api/chat/stop", {"session_id": sid}).json())
            print("       stop:", stopped)

    events = stream_chat(sid, "slow long answer please", during=stop_mid_run)
    check("stop accepted", stopped and stopped[0].get("stopped") is True, str(stopped))
    check("run reports stopped", final_done(events) and final_done(events)["status"] == "stopped",
          str(final_done(events)))
    state = session_state(sid)
    assistant = [m for m in state["messages"] if m["role"] == "assistant"]
    check("assistant message marked stopped", bool(assistant) and assistant[-1]["status"] == "stopped",
          str([(m["role"], m["status"]) for m in state["messages"]]))

    # ------------------------------------------------- background / reconnect
    print("\n== reconnect: run continues after client disconnects ==")
    sid = new_session()
    events = stream_chat(sid, "slow long answer please", close_after_deltas=2)
    check("client disconnected early", final_done(events) is None, str(final_done(events)))
    status = get("/api/chat/status", params={"session_id": sid}).json()
    check("run still active after disconnect", status.get("active") is True, str(status))
    deadline = time.time() + 60
    while time.time() < deadline:
        status = get("/api/chat/status", params={"session_id": sid}).json()
        if not status.get("active"):
            break
        time.sleep(0.5)
    check("background run finished", status.get("active") is False, str(status))
    state = session_state(sid)
    assistant = [m for m in state["messages"] if m["role"] == "assistant"]
    check("full answer saved in background", bool(assistant)
          and assistant[-1]["status"] == "complete"
          and "done." in assistant[-1]["content"], str(assistant[-1:])[:300])

    # ------------------------------------------------- repeated tool loop
    print("\n== repeated tool call loop is stopped ==")
    sid = new_session()
    events = stream_chat(sid, "loop test", approve=True)
    results = tool_results(events)
    done = final_done(events)
    check("loop run stopped", bool(done) and done.get("status") == "loop", str(done))
    check("loop stopped after 4 attempts", len(results) == 4, f"{len(results)} attempts")
    check("loop notice shown", any(
        e.get("type") == "notice" and "repeated tool-call loop" in (e.get("message") or "")
        for e in events), str([e for e in events if e.get("type") == "notice"]))
    check("loop did not execute beyond guard", sum(
        1 for e in results if e.get("status") == "ok") == 3,
        str([e.get("status") for e in results]))

    # --------------------------------------------------------- compaction
    print("\n== auto-compaction keeps the chat alive ==")
    patch("/api/settings", {"context_size": 4096, "compact_threshold": 0.3})
    sid = new_session()
    filler = "Context filler sentence about building a Python app. " * 60
    saw_compaction = False
    for i in range(5):
        events = stream_chat(sid, f"hello turn {i}. {filler}")
        if any(e.get("type") in {"compacting", "compacted"} for e in events):
            saw_compaction = True
        check(f"turn {i} complete", final_done(events) and final_done(events)["status"] == "complete",
              str(final_done(events)))
    state = session_state(sid)
    check("auto-compaction happened", saw_compaction or bool(state["session"].get("summary")),
          str(state["session"].get("summary"))[:120])
    check("summary persisted", bool(state["session"].get("summary")),
          str(state["session"].get("summary"))[:120])
    check("summary marker persisted", bool(state["session"].get("summary_upto")))
    check("full transcript retained", len(state["messages"]) >= 10,
          f"{len(state['messages'])} messages")
    check("chat still answers after compaction",
          "deterministic mock model" in state["messages"][-1].get("content", ""),
          state["messages"][-1].get("content", "")[:120])

    # manual compaction endpoint. Use a context large enough that only the
    # manual call compacts (the automatic overflow guard must stay idle),
    # so the assertion is deterministic.
    patch("/api/settings", {"context_size": 16384, "auto_compact": False})
    events = stream_chat(sid, "hello turn 6, short")
    check("extra turn complete", final_done(events) and final_done(events)["status"] == "complete",
          str(final_done(events)))
    events = stream_chat(sid, "hello turn 7, short")
    check("second extra turn complete", final_done(events) and final_done(events)["status"] == "complete",
          str(final_done(events)))

    r = post(f"/api/sessions/{sid}/compact", {})
    result = r.json()
    check("manual compact endpoint", r.status_code == 200 and result.get("compacted") is True,
          r.text[:300])
    # The current turn must survive compaction (regression guard for the bug where
    # the newest user message was compacted away before the model saw it).
    check("current turn preserved after manual compact",
          int(result.get("kept") or 0) >= 2, str(result)[:300])
    state = session_state(sid)
    check("newest user message still in transcript",
          len(state["messages"]) >= 2 and state["messages"][-2].get("role") == "user"
          and "hello turn 7" in state["messages"][-2].get("content", ""),
          str(state["messages"][-2] if len(state["messages"]) >= 2 else None)[:200])
    # The newest message must still reach the model afterwards.
    events = stream_chat(sid, "final hello after compaction")
    check("current turn reaches the model after compaction",
          "deterministic mock model" in text_of(events), text_of(events)[:120])
    export = get(f"/api/sessions/{sid}/export")
    check("export after compaction", export.status_code == 200
          and "Summary" in export.text, export.text[:200])
    patch("/api/settings", {"context_size": 8192, "compact_threshold": 0.72,
                            "auto_compact": True})

    # ---------------------------------------------------------- error paths
    print("\n== error paths ==")
    r = post("/api/chat", {"session_id": "ses_missing", "message": "hi"})
    check("missing session rejected", r.status_code == 404, r.text[:200])
    r = post("/api/approval", {"id": "apr_missing", "approved": True})
    check("unknown approval handled", r.status_code == 200 and r.json().get("ok") is False)
    r = post("/api/ask", {"id": "q_missing", "answer": "x"})
    check("unknown question handled", r.status_code == 200 and r.json().get("ok") is False)

    print(f"\n{'=' * 60}\n{PASSES} passed, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL:", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
