"""End-to-end HTTP smoke test for LocalCode.

Run the app first, then:
    .venv\\Scripts\\python.exe examples\\smoke_api.py
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8765"
FAILS: list[str] = []
PASSES = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name + (f" — {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def get(path: str, **kw):
    return requests.get(BASE + path, timeout=30, **kw)


def post(path: str, payload=None, **kw):
    return requests.post(BASE + path, json=payload, timeout=60, **kw)


def patch(path: str, payload=None):
    return requests.patch(BASE + path, json=payload, timeout=30)


def delete(path: str):
    return requests.delete(BASE + path, timeout=30)


def json_of(resp):
    try:
        return resp.json()
    except Exception:
        return None


def main() -> int:
    print(f"LocalCode smoke test against {BASE}\n")

    # ---------------------------------------------------------------- static
    r = get("/")
    check("GET / serves the app", r.status_code == 200 and "LocalCode" in r.text,
          f"status={r.status_code}")
    r = get("/favicon.svg")
    check("GET /favicon.svg", r.status_code == 200 and "<svg" in r.text[:200].lower())
    for asset in ("/static/css/app.css", "/static/js/api.js", "/static/js/app.js",
                  "/static/vendor/marked.min.js", "/static/vendor/purify.min.js",
                  "/static/vendor/highlight.min.js"):
        r = get(asset)
        check(f"GET {asset}", r.status_code == 200 and len(r.content) > 500,
              f"status={r.status_code} bytes={len(r.content)}")

    # ---------------------------------------------------------------- health
    r = get("/api/health")
    health = json_of(r) or {}
    check("GET /api/health", r.status_code == 200 and health.get("app") == "LocalCode",
          json.dumps(health)[:300])
    check("health reports version", bool(health.get("version")),
          json.dumps(health)[:300])

    # -------------------------------------------------------------- settings
    r = get("/api/settings")
    settings = (json_of(r) or {}).get("settings") or {}
    check("GET /api/settings", r.status_code == 200 and isinstance(settings, dict),
          f"status={r.status_code}")
    for key in ("workspace", "context_size", "max_steps", "approval_mode", "provider"):
        check(f"settings has {key}", key in settings, str(list(settings))[:200])
    check("context_size defaults to auto (0)", int(settings.get("context_size") or 0) == 0,
          str(settings.get("context_size")))
    check("max_steps defaults to unlimited (0)", int(settings.get("max_steps") or 0) == 0,
          str(settings.get("max_steps")))

    r = get("/api/system/info")
    info = json_of(r) or {}
    check("GET /api/system/info", r.status_code == 200 and info.get("ram"),
          f"status={r.status_code}")
    for key in ("os", "cpu", "cpu_count", "models_dir", "data_dir", "workspace"):
        check(f"system info has {key}", key in info, str(list(info))[:200])

    # ---------------------------------------------------------------- models
    r = get("/api/models")
    models = json_of(r) or {}
    check("GET /api/models", r.status_code == 200 and "catalog" in models,
          f"status={r.status_code}")
    check("model catalog is populated", len(models.get("catalog") or []) >= 8,
          str(len(models.get("catalog") or [])))
    check("installed list present", isinstance(models.get("installed"), list))
    check("runtime status present", isinstance(models.get("runtime"), dict))
    check("capabilities present", isinstance(models.get("capabilities"), dict))
    check("categories include uncensored", "uncensored" in (models.get("categories") or {}))
    check("runtime reports installed", bool((models.get("runtime") or {}).get("installed")),
          json.dumps(models.get("runtime"))[:300])

    r = get("/api/downloads")
    check("GET /api/downloads", r.status_code == 200 and "downloads" in (json_of(r) or {}))

    r = get("/api/models/search?q=qwen&limit=3")
    check("GET /api/models/search", r.status_code in (200, 502, 503),
          f"status={r.status_code}")
    if r.status_code == 200:
        check("HF search returns a list", isinstance((json_of(r) or {}).get("results"), list))

    r = post("/api/models/load", {"id": "does-not-exist"})
    check("load unknown model is rejected", r.status_code >= 400 and r.status_code < 500,
          f"status={r.status_code}")

    r = post("/api/models/download-url", {"url": "not-a-url"})
    check("invalid download URL is rejected", r.status_code == 400,
          f"status={r.status_code}")

    # --------------------------------------------------------------- runtime
    r = get("/api/runtime")
    runtime = json_of(r) or {}
    check("GET /api/runtime", r.status_code == 200 and "installed" in runtime,
          f"status={r.status_code}")
    check("runtime is installed", bool(runtime.get("installed")))
    r = get("/api/runtime/logs?lines=20")
    check("GET /api/runtime/logs", r.status_code == 200 and "logs" in (json_of(r) or {}))

    # ------------------------------------------------------------------- MCP
    r = get("/api/mcp")
    mcp = json_of(r) or {}
    check("GET /api/mcp", r.status_code == 200 and "servers" in mcp,
          f"status={r.status_code}")
    check("MCP presets present", len(mcp.get("presets") or []) >= 6,
          str(len(mcp.get("presets") or [])))
    check("Roblox preset present",
          any(p.get("id") == "roblox-studio" for p in (mcp.get("presets") or [])))
    node = mcp.get("node") or {}
    check("portable Node detected", bool(node.get("installed")),
          json.dumps(node)[:300])
    r = get("/api/mcp/node")
    check("GET /api/mcp/node", r.status_code == 200 and (json_of(r) or {}).get("installed"))
    r = get("/api/mcp/tools")
    check("GET /api/mcp/tools", r.status_code == 200 and "tools" in (json_of(r) or {}),
          f"status={r.status_code}")

    # -------------------------------------------------------------- usage/etc
    r = get("/api/usage")
    usage = json_of(r) or {}
    check("GET /api/usage", r.status_code == 200 and "usage" in usage,
          f"status={r.status_code}")
    check("usage has hosted pricing note", bool((usage.get("pricing") or {}).get("note")))
    r = get("/api/capabilities")
    check("GET /api/capabilities", r.status_code == 200 and "capabilities" in (json_of(r) or {}))

    # --------------------------------------------------------------- sessions
    r = post("/api/sessions", {"title": "Smoke test chat"})
    session = (json_of(r) or {}).get("session") or {}
    sid = session.get("id")
    check("POST /api/sessions", r.status_code in (200, 201) and bool(sid),
          f"status={r.status_code}")
    r = get(f"/api/sessions/{sid}")
    payload = json_of(r) or {}
    check("GET /api/sessions/<id>", r.status_code == 200 and payload.get("session", {}).get("id") == sid)
    for key in ("messages", "run", "approvals", "questions", "usage", "capabilities", "price"):
        check(f"session payload has {key}", key in payload, str(list(payload))[:200])
    check("new session is not running", (payload.get("run") or {}).get("active") is False)

    r = patch(f"/api/sessions/{sid}", {"title": "Renamed smoke chat", "pinned": True})
    check("PATCH /api/sessions/<id>", r.status_code == 200 and
          (json_of(r) or {}).get("session", {}).get("title") == "Renamed smoke chat",
          f"status={r.status_code}")

    r = get("/api/sessions?q=Renamed")
    check("GET /api/sessions search", r.status_code == 200 and
          any(s.get("id") == sid for s in (json_of(r) or {}).get("sessions", [])))

    # ------------------------------------------------------------- attachments
    files = {"file": ("smoke.txt", io.BytesIO(b"hello from the smoke test\n"), "text/plain")}
    r = requests.post(BASE + "/api/attachments", files=files, timeout=30)
    att = (json_of(r) or {}).get("attachment") or {}
    att_id = att.get("id")
    check("POST /api/attachments", r.status_code in (200, 201) and bool(att_id),
          f"status={r.status_code} {json.dumps(att)[:300]}")
    if att_id:
        check("attachment records text kind", att.get("kind") == "text", json.dumps(att)[:300])
        r = get(f"/api/attachments/{att_id}/raw")
        check("GET attachment raw", r.status_code == 200 and b"smoke test" in r.content,
              f"status={r.status_code}")
        r = get(f"/api/attachments/{att_id}")
        check("GET attachment metadata", r.status_code == 200 and
              (json_of(r) or {}).get("attachment", {}).get("id") == att_id)

    # --------------------------------------------------------------- memories
    r = post("/api/memories", {"content": "Smoke test memory: use tabs.", "scope": "workspace"})
    memory = (json_of(r) or {}).get("memory") or {}
    mem_id = memory.get("id")
    check("POST /api/memories", r.status_code in (200, 201) and bool(mem_id),
          f"status={r.status_code}")
    r = get("/api/memories")
    check("GET /api/memories", r.status_code == 200 and
          any(m.get("id") == mem_id for m in (json_of(r) or {}).get("memories", [])))
    if mem_id:
        r = delete(f"/api/memories/{mem_id}")
        check("DELETE /api/memories/<id>", r.status_code == 200)

    # -------------------------------------------------------------- workspace
    r = get("/api/workspace/tree?path=.")
    tree = json_of(r) or {}
    check("GET /api/workspace/tree", r.status_code == 200 and "entries" in tree,
          f"status={r.status_code}")
    probe = Path(__file__).resolve().parent.parent / "workspace" / "_smoke_probe.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("LocalCode smoke workspace file\n", encoding="utf-8")
    try:
        r = get("/api/workspace/file?path=_smoke_probe.txt")
        check("GET /api/workspace/file", r.status_code == 200 and
              "LocalCode smoke workspace file" in ((json_of(r) or {}).get("content") or ""),
              f"status={r.status_code}")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    r = get("/api/workspace/file?path=../outside.txt")
    check("workspace traversal is blocked", r.status_code in (400, 403, 404),
          f"status={r.status_code}")

    # ------------------------------------------------------------------- chat
    r = get(f"/api/chat/status?session_id={sid}")
    check("GET /api/chat/status", r.status_code == 200 and
          (json_of(r) or {}).get("active") is False)
    r = post("/api/chat/queue", {"session_id": sid, "message": "hello"})
    check("queue on idle chat reports inactive", r.status_code == 200 and
          (json_of(r) or {}).get("active") is False, f"status={r.status_code}")
    r = post("/api/chat/stop", {"session_id": sid})
    check("stop idle chat", r.status_code == 200 and
          (json_of(r) or {}).get("stopped") is False)
    r = post("/api/approval", {"id": "apr_missing", "approved": True})
    check("unknown approval is a no-op", r.status_code == 200 and
          (json_of(r) or {}).get("ok") is False)
    r = post("/api/ask", {"id": "ask_missing", "answer": "x"})
    check("unknown question is a no-op", r.status_code == 200 and
          (json_of(r) or {}).get("ok") is False)

    # ---------------------------------------------------------------- compact
    r = post(f"/api/sessions/{sid}/compact")
    check("POST compact on empty chat", r.status_code == 200, f"status={r.status_code}")

    # ----------------------------------------------------------------- export
    for fmt in ("md", "json"):
        r = get(f"/api/sessions/{sid}/export?format={fmt}")
        check(f"export chat as {fmt}", r.status_code == 200 and len(r.content) > 0,
              f"status={r.status_code}")
        if fmt == "json":
            check("JSON export parses", isinstance(json_of(r), dict))
        else:
            check("Markdown export contains title", b"Renamed smoke chat" in r.content)

    r = get("/api/system/export")
    check("GET /api/system/export", r.status_code == 200 and len(r.content) > 0,
          f"status={r.status_code}")
    if r.status_code == 200:
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                names = zf.namelist()
            check("system export is a valid zip", any(n.endswith(".json") for n in names),
                  str(names)[:200])
        except Exception as exc:
            check("system export is a valid zip", False, str(exc))

    # ----------------------------------------------------------------- errors
    r = get("/api/sessions/ses_missing")
    check("unknown session returns 404", r.status_code == 404, f"status={r.status_code}")
    r = post("/api/system/open-folder", {"kind": "not-a-folder"})
    check("unknown folder kind is rejected", r.status_code == 400, f"status={r.status_code}")
    r = get("/api/models/repo?repo=")
    check("empty repo is rejected", r.status_code == 400, f"status={r.status_code}")

    # ---------------------------------------------------------------- cleanup
    if att_id:
        r = delete(f"/api/attachments/{att_id}")
        check("DELETE attachment", r.status_code == 200)
    r = delete(f"/api/sessions/{sid}")
    check("DELETE /api/sessions/<id>", r.status_code == 200)
    r = get(f"/api/sessions/{sid}")
    check("deleted session is gone", r.status_code == 404)

    print()
    print(f"{PASSES} passed, {len(FAILS)} failed")
    if FAILS:
        print("\nFailures:")
        for item in FAILS:
            print("  -", item)
        return 1
    print("All API smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
