"""Broad feature audit for a running LocalCode instance.

Covers endpoints and workflows not exercised by e2e_agent.py: settings,
memories, workspace browsing, session CRUD/export, audio/video/scanned-PDF
attachments, provider probing, model search/repo browsing, a real tiny model
download from a URL, model deletion, runtime metadata, MCP lifecycle and
presets, system info/export, and error paths.

Run with the app already running and the mock provider available:
    .venv\\Scripts\\python.exe examples\\feature_audit.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import requests

BASE = os.environ.get("LOCALCODE_BASE", "http://127.0.0.1:8765").rstrip("/")
MOCK = os.environ.get("LOCALCODE_MOCK", "http://127.0.0.1:8899/v1")
TINY_GGUF = ("https://huggingface.co/ggml-org/models/resolve/main/"
             "tinyllamas/stories15M-q4_0.gguf")
FAILS: list[str] = []
PASSES = 0
CLEANUP: list[tuple[str, callable]] = []


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


def delete(path: str):
    return requests.delete(BASE + path, timeout=90)


def make_blank_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def upload(name: str, data: bytes, mime: str) -> dict:
    r = requests.post(BASE + "/api/attachments",
                      files={"file": (name, io.BytesIO(data), mime)}, timeout=90)
    r.raise_for_status()
    return r.json()["attachment"]


def main() -> int:
    print("== setup ==")
    health = get("/api/health").json()
    check("app reachable", health.get("ok") is True, str(health))
    r = patch("/api/settings", {
        "provider": "openai", "external_base_url": MOCK,
        "external_model": "mock-coder", "approval_mode": "auto",
        "auto_compact": True, "show_costs": True,
    })
    check("settings patch accepted", r.status_code == 200, r.text[:200])
    settings = r.json().get("settings") or {}
    check("provider persisted", settings.get("provider") == "openai", str(settings.get("provider")))

    print("\n== settings and validation ==")
    r = patch("/api/settings", {"approval_mode": "bogus"})
    check("invalid approval mode rejected", r.status_code == 400, r.text[:120])
    r = patch("/api/settings", {"context_size": -1})
    check("invalid context size rejected", r.status_code == 400, r.text[:120])
    r = get("/api/settings")
    body = r.json()
    check("settings payload has system/runtime/mcp", all(
        k in body for k in ("settings", "system", "runtime", "mcp")), str(body.keys()))

    print("\n== system and capabilities ==")
    info = get("/api/system/info").json()
    check("system info has machine fields", all(
        k in info for k in ("ram", "cpu", "workspace", "data_dir", "disk")), str(info.keys()))
    check("system info has MCP/node status", "node" in info and "mcp" in info)
    caps = get("/api/capabilities").json().get("capabilities") or {}
    check("capabilities report text and tools", bool(caps.get("text")) and bool(caps.get("tools")),
          str(caps))
    check("capabilities honestly mark audio/video", "audio" in caps and "video" in caps, str(caps))
    tools_list = get("/api/tools").json().get("tools") or []
    check("built-in tools endpoint lists tools", len(tools_list) >= 10,
          str(len(tools_list)))
    check("built-in tools have descriptions", all(
        t.get("name") and t.get("description") for t in tools_list), str(tools_list[0]))
    models = get("/api/models").json()
    installed = models.get("installed") or []
    check("installed model list endpoint works", isinstance(installed, list), str(installed))
    if installed:
        one = get("/api/capabilities?model_id=" + requests.utils.quote(installed[0]["id"], safe="")).json()
        check("per-model capabilities", bool(one.get("capabilities")), str(one)[:200])
    usage = get("/api/usage").json()
    check("usage summary present", "usage" in usage and "total_tokens" in usage["usage"], str(usage)[:200])

    print("\n== memories ==")
    marker = f"audit-memory-{time.time_ns()}"
    r = post("/api/memories", {"content": marker, "scope": "global"})
    mem = r.json().get("memory") or {}
    check("global memory created", r.status_code == 201 and mem.get("content") == marker, r.text[:200])
    if mem.get("id"):
        CLEANUP.append(("memory", lambda mid=mem["id"]: delete(f"/api/memories/{mid}")))
    r = post("/api/memories", {"content": marker + "-workspace", "scope": "workspace"})
    mem2 = r.json().get("memory") or {}
    check("workspace memory created", r.status_code == 201, r.text[:200])
    if mem2.get("id"):
        CLEANUP.append(("memory", lambda mid=mem2["id"]: delete(f"/api/memories/{mid}")))
    r = post("/api/memories", {"content": ""})
    check("empty memory rejected", r.status_code == 400, r.text[:120])
    listed = get("/api/memories?q=" + requests.utils.quote(marker, safe="")).json().get("memories") or []
    check("memory search finds both", len(listed) >= 2, str(len(listed)))
    for _, fn in [x for x in CLEANUP if x[0] == "memory"]:
        fn()
    listed = get("/api/memories?q=" + requests.utils.quote(marker, safe="")).json().get("memories") or []
    check("memories deleted", len(listed) == 0, str(listed))
    CLEANUP[:] = [x for x in CLEANUP if x[0] != "memory"]

    print("\n== workspace browsing ==")
    workspace = Path(info.get("workspace") or "")
    test_file = workspace / "audit-workspace.txt"
    test_file.write_text("LocalCode workspace audit\n", encoding="utf-8")
    tree = get("/api/workspace/tree?path=.").json()
    names = [e.get("name") for e in tree.get("entries") or []]
    check("workspace tree lists files", "audit-workspace.txt" in names, str(names)[:300])
    preview = get("/api/workspace/file?path=audit-workspace.txt").json()
    check("workspace file preview", "LocalCode workspace audit" in str(preview), str(preview)[:200])
    bad = get("/api/workspace/file?path=../data/localcode.db")
    check("path traversal blocked", bad.status_code >= 400, f"{bad.status_code} {bad.text[:120]}")
    test_file.unlink(missing_ok=True)

    print("\n== session CRUD and export ==")
    created = post("/api/sessions", {"title": "Audit session"}).json()["session"]
    sid = created["id"]
    CLEANUP.append(("session", lambda s=sid: delete(f"/api/sessions/{s}")))
    r = patch(f"/api/sessions/{sid}", {"title": "Audit renamed", "pinned": True})
    check("session rename/pin", r.status_code == 200 and r.json()["session"]["title"] == "Audit renamed",
          r.text[:200])
    found = get("/api/sessions?q=Audit%20renamed").json().get("sessions") or []
    check("session search", any(s["id"] == sid for s in found), str(found)[:200])
    md = get(f"/api/sessions/{sid}/export")
    check("markdown export", md.status_code == 200 and "# Audit renamed" in md.text, md.text[:160])
    js = get(f"/api/sessions/{sid}/export?format=json")
    check("json export", js.status_code == 200 and js.json().get("session", {}).get("id") == sid)
    missing = get("/api/sessions/ses_missing")
    check("missing session is 404", missing.status_code == 404, str(missing.status_code))
    delete(f"/api/sessions/{sid}")
    CLEANUP[:] = [x for x in CLEANUP if x[0] != "session"]
    check("session deleted", get(f"/api/sessions/{sid}").status_code == 404)

    print("\n== attachments: text, audio, video, scanned PDF ==")
    text_att = upload("audit.txt", b"attachment audit\n", "text/plain")
    check("text attachment", text_att.get("kind") == "text" and text_att.get("text_chars", 0) > 0,
          str(text_att))
    wav = (b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little") +
           (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little") +
           (8000).to_bytes(4, "little") + (1).to_bytes(2, "little") + (8).to_bytes(2, "little") +
           b"data" + (0).to_bytes(4, "little"))
    audio_att = upload("audit.wav", wav, "audio/wav")
    check("audio attachment metadata", audio_att.get("kind") == "audio", str(audio_att))
    mp4 = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
           b"\x00\x00\x00\x08free")
    video_att = upload("audit.mp4", mp4, "video/mp4")
    check("video attachment metadata", video_att.get("kind") == "video", str(video_att))
    pdf_att = upload("audit-blank.pdf", make_blank_pdf(), "application/pdf")
    check("blank PDF stored", pdf_att.get("kind") == "pdf", str(pdf_att))
    check("blank PDF detected as scanned", pdf_att.get("scanned") is True, str(pdf_att))
    for att in (text_att, audio_att, video_att, pdf_att):
        raw = get(att["url"])
        check(f"{att['kind']} raw download", raw.status_code == 200 and len(raw.content) > 0)
        r = delete(f"/api/attachments/{att['id']}")
        check(f"{att['kind']} attachment delete", r.status_code == 200)
        check(f"{att['kind']} attachment gone", get(f"/api/attachments/{att['id']}").status_code == 404)

    print("\n== provider ==")
    r = get("/api/provider/models")
    check("provider models endpoint", r.status_code == 200 and isinstance(r.json().get("models"), list),
          r.text[:200])
    r = post("/api/provider/probe", {})
    check("provider probe responds", r.status_code == 200, r.text[:200])

    print("\n== model catalog, search, repo browser ==")
    catalog = get("/api/models").json().get("catalog") or []
    check("catalog populated", len(catalog) >= 30, str(len(catalog)))
    check("catalog has fit badges", all("fit" in m for m in catalog), str(catalog[:1]))
    repo = get("/api/models/repo?repo=Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF")
    check("repo browser returns quants", repo.status_code == 200 and len(repo.json().get("quants") or []) > 0,
          repo.text[:200])
    vl = get("/api/models/repo?repo=unsloth/Qwen2.5-VL-7B-Instruct-GGUF")
    check("vision repo lists projectors", vl.status_code == 200 and len(vl.json().get("mmproj") or []) > 0,
          vl.text[:200])
    search = get("/api/models/search?q=qwen&limit=5")
    check("HF model search", search.status_code == 200 and len(search.json().get("results") or []) > 0,
          search.text[:200])
    check("downloads list", isinstance(get("/api/downloads").json().get("downloads"), list))
    bad = post("/api/models/download-url", {"url": "http://example.com/not-a-gguf"})
    check("non-GGUF URL rejected", bad.status_code == 400, bad.text[:160])

    print("\n== real tiny model download and delete ==")
    name = f"audit-tiny-{int(time.time())}"
    r = post("/api/models/download-url", {"url": TINY_GGUF, "name": name})
    job = r.json().get("job") or {}
    check("tiny model download started", r.status_code == 202 and job.get("id"), r.text[:200])
    dup = post("/api/models/download-url", {"url": TINY_GGUF, "name": name})
    check("duplicate download rejected", dup.status_code == 400, dup.text[:200])
    job_id = job.get("id")
    model_id = job.get("model_id")
    if job_id:
        CLEANUP.append(("job", lambda j=job_id: post("/api/models/remove-job", {"job_id": j})))
    deadline = time.time() + 180
    status = {}
    while job_id and time.time() < deadline:
        status = next((d for d in get("/api/downloads").json().get("downloads") or []
                       if d.get("id") == job_id), {})
        if status.get("status") in {"complete", "error", "cancelled"}:
            break
        time.sleep(1)
    check("tiny model downloaded", status.get("status") == "complete", str(status)[:300])
    if status.get("status") == "complete" and model_id:
        installed_now = get("/api/models").json().get("installed") or []
        check("downloaded model installed", any(m["id"] == model_id for m in installed_now),
              str([m["id"] for m in installed_now]))
        r = post("/api/models/delete", {"id": model_id})
        check("downloaded model deleted", r.status_code == 200 and r.json().get("ok") is True,
              r.text[:200])
        installed_now = get("/api/models").json().get("installed") or []
        check("deleted model gone", all(m["id"] != model_id for m in installed_now))
    if job_id:
        post("/api/models/remove-job", {"job_id": job_id})
        CLEANUP[:] = [x for x in CLEANUP if x[0] != "job"]
    check("cancel unknown job is false", post("/api/models/cancel", {"job_id": "nope"}).json().get("ok") is False)
    check("remove unknown job is false", post("/api/models/remove-job", {"job_id": "nope"}).json().get("ok") is False)

    print("\n== runtime metadata ==")
    rt = get("/api/runtime").json()
    check("runtime status endpoint", "installed" in rt and "ready" in rt, str(rt)[:200])
    logs = get("/api/runtime/logs?lines=20")
    check("runtime logs endpoint",
          logs.status_code == 200 and isinstance(logs.json().get("logs"), (str, list)),
          logs.text[:160])

    print("\n== MCP lifecycle and presets ==")
    mcp = get("/api/mcp").json()
    check("MCP status lists servers", isinstance(mcp.get("servers"), list), str(mcp)[:200])
    node = get("/api/mcp/node").json()
    check("portable Node status", "installed" in node, str(node)[:200])
    tools = get("/api/mcp/tools")
    tool_names = [t.get("name") for t in tools.json().get("tools") or []]
    check("MCP tools discovered", bool(tool_names) and any(
        n in {"echo", "get-sum", "get-tiny-image"} for n in tool_names), str(tool_names)[:300])
    if any(s.get("name") == "everything" for s in mcp.get("servers") or []):
        check("MCP logs endpoint", get("/api/mcp/servers/everything/logs").status_code == 200)
        check("MCP stop", post("/api/mcp/servers/everything/stop").status_code == 200)
        check("MCP start", post("/api/mcp/servers/everything/start").status_code == 200)
        check("MCP restart", post("/api/mcp/servers/everything/restart").status_code == 200)
        deadline = time.time() + 45
        ready = False
        while time.time() < deadline:
            servers = get("/api/mcp").json().get("servers") or []
            one = next((s for s in servers if s.get("name") == "everything"), {})
            ready = one.get("status") == "ready"
            if ready:
                break
            time.sleep(1)
        check("MCP ready after restart", ready)

    for preset, unique in (("everything", "audit-everything"), ("roblox-studio", "audit-roblox")):
        r = post("/api/mcp/presets", {"preset": preset, "name": unique, "start": False})
        check(f"preset {preset} added", r.status_code == 201, r.text[:200])
        server = r.json().get("server") or {}
        if preset == "roblox-studio":
            check("Roblox preset uses npx", server.get("command") == "npx", str(server)[:240])
            check("Roblox preset package correct",
                  "@chrrxs/robloxstudio-mcp@latest" in (server.get("args") or []), str(server.get("args")))
        r = delete(f"/api/mcp/servers/{unique}")
        check(f"preset {preset} removed", r.status_code == 200, r.text[:160])
    check("unknown MCP logs handled", get("/api/mcp/servers/nope/logs").status_code >= 400)

    print("\n== system export and pages ==")
    exp = get("/api/system/export")
    check("full export zip", exp.status_code == 200 and exp.content[:2] == b"PK", exp.text[:120])
    try:
        with zipfile.ZipFile(io.BytesIO(exp.content)) as zf:
            names = zf.namelist()
            exported_settings = json.loads(zf.read("settings.json"))
        check("export contains sessions and settings",
              all(n in names for n in ("sessions.json", "settings.json", "memories.json")),
              str(names)[:300])
        check("export masks secrets", exported_settings.get("external_api_key") in {"", "***"},
              str(exported_settings.get("external_api_key"))[:80])
    except Exception as exc:
        check("export zip readable", False, str(exc))
    index = get("/")
    check("UI served", index.status_code == 200 and "LocalCode" in index.text)
    favicon = get("/favicon.svg")
    check("favicon served", favicon.status_code == 200 and len(favicon.content) > 100)

    print("\n== chat error paths ==")
    check("chat status endpoint", get("/api/chat/status").status_code == 200)
    check("stop with no run", post("/api/chat/stop", {"session_id": "ses_nope"}).status_code == 200)
    queued = post("/api/chat/queue", {
        "session_id": "ses_nope", "message": "hi", "mode": "queue",
    })
    check("queue with no run reports inactive",
          queued.status_code == 200 and queued.json().get("active") is False, queued.text[:160])
    check("unknown approval", post("/api/approval", {"id": "nope", "approved": True}).status_code == 200)
    check("unknown question", post("/api/ask", {"id": "nope", "answer": "x"}).status_code == 200)

    print("\n== reasoning stream and persistence ==")
    # Context 0 = automatic: exercises the prompt builder path that reads the
    # model's capability context (regression guard for a variable-order bug).
    patch("/api/settings", {"context_size": 0})
    sid = post("/api/sessions", {}).json()["session"]["id"]
    events: list[dict] = []
    answer = ""
    resp = requests.post(BASE + "/api/chat", json={
        "session_id": sid, "message": "show your reasoning please",
    }, stream=True, timeout=(15, 600))
    resp.raise_for_status()
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            events.append(event)
            if event.get("type") == "delta":
                answer += event.get("text") or ""
            elif event.get("type") == "approval":
                post("/api/approval", {"id": event["id"], "approved": True})
            elif event.get("type") == "ask_user":
                post("/api/ask", {"id": event["id"], "answer": "42"})
    finally:
        resp.close()
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    check("reasoning events streamed", any(e.get("type") == "reasoning" for e in events),
          str([e.get("type") for e in events])[:300])
    check("reasoning run completed", bool(done) and done.get("status") == "complete",
          str(done)[:240])
    check("reasoning answer returned", "42" in answer, answer[:160])
    state = get(f"/api/sessions/{sid}").json()
    assistant = [m for m in state.get("messages") or [] if m.get("role") == "assistant"]
    check("reasoning persisted in chat", any(m.get("reasoning") for m in assistant),
          str([m.get("reasoning") for m in assistant])[:240])
    delete(f"/api/sessions/{sid}")

    print("\n== cleanup ==")
    for kind, fn in reversed(CLEANUP):
        try:
            fn()
        except Exception as exc:
            print(f"  warn cleanup {kind}: {exc}", flush=True)

    print("\n== start from scratch ==")
    r = post("/api/system/reset", {})
    check("reset endpoint accepted", r.status_code == 200 and r.json().get("ok") is True,
          r.text[:240])
    settings = get("/api/settings").json().get("settings") or {}
    check("onboarded cleared after reset", settings.get("onboarded") is False,
          str(settings.get("onboarded")))
    mcp_now = get("/api/mcp").json().get("servers") or []
    check("all MCP tools removed by reset", len(mcp_now) == 0, str(mcp_now))
    sessions_now = get("/api/sessions").json().get("sessions") or []
    check("all chats wiped by reset", len(sessions_now) == 0, str(len(sessions_now)))
    memories_now = get("/api/memories").json().get("memories") or []
    check("all memories wiped by reset", len(memories_now) == 0, str(len(memories_now)))

    print(f"\n{'=' * 64}\n{PASSES} checks passed, {len(FAILS)} failed", flush=True)
    for fail in FAILS:
        print("  FAIL:", fail, flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
