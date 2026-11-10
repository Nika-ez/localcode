"""Flask HTTP API for LocalCode."""
from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context

from . import agent, attachments, capabilities, config, db, llm, mcp, models, pricing, tools
from .mcp import mcp as mcp_manager
from .runtime import RuntimeErrorLC, runtime

app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = attachments.MAX_BYTES + (2 * 1024 * 1024)
STARTED_AT = time.time()


# ------------------------------------------------------------------ helpers
def err(message: str, status: int = 400, **extra):
    payload = {"error": str(message)}
    payload.update(extra)
    return jsonify(payload), status


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}
    return data


def _public_settings() -> dict:
    cfg = dict(config.load_config())
    cfg["workspace"] = str(config.workspace_path())
    cfg["app_version"] = config.APP_VERSION
    cfg["frozen"] = config.FROZEN
    cfg["app_dir"] = str(config.APP_DIR)
    return cfg


def _attachment_list(raw) -> list[dict]:
    """Accept ids or public attachment objects and return public metadata."""
    out = []
    for item in raw or []:
        att_id = item.get("id") if isinstance(item, dict) else item
        if not att_id:
            continue
        meta = attachments.get_public(str(att_id))
        if meta:
            out.append(meta)
    return out


def _session_payload(session_id: str) -> dict:
    session = db.get_session(session_id)
    if not session:
        raise ValueError("Session not found")
    messages = db.get_messages(session_id, include_raw=False)
    cap = capabilities.current()
    return {
        "session": session,
        "messages": messages,
        "run": agent.run_status(session_id),
        "approvals": agent.list_approvals(session_id),
        "questions": agent.list_questions(session_id),
        "usage": db.usage_summary(session_id),
        "capabilities": cap,
        "price": pricing.price_for(cap.get("model")),
        "mcp": {
            "servers": len([s for s in mcp_manager.servers.values() if s.cfg.get("enabled", True)]),
            "connected": len([s for s in mcp_manager.servers.values() if s.status == "ready"]),
            "tools": sum(len(s.tools) for s in mcp_manager.servers.values()),
        },
    }


def _ndjson(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _stream_run(session_id: str, control: agent.RunControl) -> Response:
    """Stream a run. If the browser disconnects, the agent keeps working in the
    background; the UI can reconnect by polling the session."""
    q: queue.Queue = queue.Queue(maxsize=4000)
    finished = threading.Event()
    client_connected = threading.Event()
    client_connected.set()

    def emit(event: dict) -> None:
        if not client_connected.is_set():
            return
        try:
            q.put(event, timeout=2)
        except queue.Full:
            # Never let a slow/disconnected UI stall the agent.
            pass

    def worker() -> None:
        try:
            agent.run(session_id, control, emit)
        except Exception as exc:
            emit({"type": "error", "message": f"Agent crashed: {exc}"})
            emit({"type": "done", "session_id": session_id, "status": "error"})
        finally:
            agent.unregister_run(session_id, control)
            finished.set()
            try:
                q.put_nowait(None)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True, name=f"agent-{session_id[-6:]}").start()

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=0.5)
                except queue.Empty:
                    if finished.is_set():
                        break
                    continue
                if event is None:
                    break
                yield _ndjson(event)
        except GeneratorExit:
            # The run is deliberately not stopped here. It continues and writes
            # everything to the database; the UI can reattach via /api/sessions.
            client_connected.clear()
            raise

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# -------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(config.STATIC_DIR, "index.html")


@app.get("/favicon.svg")
def favicon():
    return send_from_directory(config.STATIC_DIR, "favicon.svg")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "pid": os.getpid(),
        "uptime": int(time.time() - STARTED_AT),
        "time": config.now(),
        "frozen": config.FROZEN,
    })


# ----------------------------------------------------------------- sessions
@app.get("/api/sessions")
def sessions_list():
    q = request.args.get("q", "")
    return jsonify({"sessions": db.list_sessions(q)})


@app.post("/api/sessions")
def sessions_create():
    data = _json_body()
    title = str(data.get("title") or "New chat").strip()[:120] or "New chat"
    workspace = data.get("workspace")
    if workspace:
        try:
            workspace = str(config.set_workspace(str(workspace)))
        except Exception as exc:
            return err(exc)
    session = db.create_session(title=title, workspace=workspace or str(config.workspace_path()))
    return jsonify({"session": session}), 201


@app.get("/api/sessions/<session_id>")
def sessions_get(session_id: str):
    try:
        return jsonify(_session_payload(session_id))
    except ValueError as exc:
        return err(exc, 404)


@app.patch("/api/sessions/<session_id>")
def sessions_patch(session_id: str):
    data = _json_body()
    session = db.get_session(session_id)
    if not session:
        return err("Session not found", 404)
    fields = {}
    if "title" in data:
        fields["title"] = str(data.get("title") or "").strip()[:120] or "New chat"
    if "pinned" in data:
        fields["pinned"] = 1 if data.get("pinned") else 0
    if "archived" in data:
        fields["archived"] = 1 if data.get("archived") else 0
    if "model" in data:
        fields["model"] = str(data.get("model") or "")
    if "workspace" in data and data.get("workspace"):
        try:
            fields["workspace"] = str(config.set_workspace(str(data["workspace"])))
        except Exception as exc:
            return err(exc)
    if fields:
        db.touch_session(session_id, **fields)
    return jsonify({"session": db.get_session(session_id)})


@app.delete("/api/sessions/<session_id>")
def sessions_delete(session_id: str):
    if agent.get_run(session_id):
        return err("Stop the agent before deleting this chat.", 409)
    db.delete_session(session_id)
    return jsonify({"ok": True})


@app.post("/api/sessions/<session_id>/compact")
def sessions_compact(session_id: str):
    if agent.get_run(session_id):
        return err("Wait for the current run to finish before compacting.", 409)
    try:
        result = agent.compact_session(session_id, force=True)
        return jsonify(result)
    except Exception as exc:
        return err(exc)


@app.get("/api/sessions/<session_id>/export")
def sessions_export(session_id: str):
    session = db.get_session(session_id)
    if not session:
        return err("Session not found", 404)
    fmt = request.args.get("format", "md")
    messages = db.get_messages(session_id)
    usage = db.usage_summary(session_id)
    if fmt == "json":
        payload = json.dumps({"session": session, "messages": messages, "usage": usage},
                             indent=2, ensure_ascii=False)
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.json"'},
        )
    lines = [f"# {session['title']}", "", f"- Workspace: `{session.get('workspace')}`",
             f"- Created: {session.get('created_at')}",
             f"- Tokens: {usage['total_tokens']:,} ({usage['prompt_tokens']:,} in / {usage['completion_tokens']:,} out)",
             f"- Typical hosted cost: {pricing.format_usd(usage['cost_usd'])}", ""]
    if session.get("summary"):
        lines += ["## Session memory", "", session["summary"], ""]
    for m in messages:
        if m["role"] == "user":
            lines += ["## You", "", m["content"], ""]
            for a in m.get("attachments") or []:
                lines += [f"> Attachment: {a.get('name')} ({a.get('kind')})", ""]
        elif m["role"] == "assistant":
            if m.get("reasoning"):
                lines += ["<details><summary>Thinking</summary>", "", m["reasoning"], "", "</details>", ""]
            if m.get("content"):
                lines += ["## LocalCode", "", m["content"], ""]
        elif m["role"] == "tool":
            lines += [f"### Tool: {m.get('tool_name') or 'tool'} ({m.get('status')})", "",
                      "```", (m.get("content") or "")[:20000], "```", ""]
    text = "\n".join(lines)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", session["title"]).strip("-") or "chat"
    return Response(
        text,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe}.md"'},
    )


# --------------------------------------------------------------------- chat
@app.post("/api/chat")
def chat():
    data = _json_body()
    session_id = data.get("session_id")
    message = str(data.get("message") or "").strip()
    edit_id = data.get("edit_message_id")
    regenerate = bool(data.get("regenerate"))
    attachments_list = _attachment_list(data.get("attachments") or [])
    if not session_id:
        if not message and not attachments_list:
            return err("Message is required")
        session = db.create_session(workspace=str(config.workspace_path()))
        session_id = session["id"]
    session = db.get_session(session_id)
    if not session:
        return err("Session not found", 404)
    if agent.get_run(session_id):
        return err("The agent is already working in this chat. Queue a message instead.", 409, code="busy")

    if edit_id:
        target = db.get_message(str(edit_id))
        if not target or target["session_id"] != session_id or target["role"] != "user":
            return err("Can only edit a user message in this chat")
        if not message and not attachments_list:
            return err("Message is required")
        db.update_message(target["id"], content=message, attachments=attachments_list, status="complete")
        db.delete_messages_from(session_id, target["id"], include=False)
    elif regenerate:
        last = db.last_user_message(session_id)
        if not last:
            return err("There is no message to regenerate")
        db.delete_messages_from(session_id, last["id"], include=False)
    else:
        if not message and not attachments_list:
            return err("Message is required")
        db.add_message(session_id, "user", message, attachments=attachments_list)
        current = db.get_session(session_id)
        if current and current["title"] in ("New chat", "Untitled"):
            title = re.sub(r"\s+", " ", message)[:60].strip() or (
                attachments_list[0].get("name", "New chat") if attachments_list else "New chat"
            )
            db.touch_session(session_id, title=title)

    control = agent.register_run(session_id)
    if not control:
        return err("The agent is already working in this chat.", 409, code="busy")
    return _stream_run(session_id, control)


@app.post("/api/chat/queue")
def chat_queue():
    data = _json_body()
    session_id = str(data.get("session_id") or "")
    message = str(data.get("message") or "").strip()
    interrupt = bool(data.get("interrupt"))
    attachments_list = _attachment_list(data.get("attachments") or [])
    if not session_id or (not message and not attachments_list):
        return err("session_id and message are required")
    control = agent.get_run(session_id)
    if not control:
        return jsonify({"active": False})
    item = agent.queue_message(session_id, message, interrupt=interrupt,
                               attachments_list=attachments_list)
    if not item:
        return jsonify({"active": False})
    return jsonify({"active": True, "item": item, "interrupt": interrupt})


@app.post("/api/chat/unqueue")
def chat_unqueue():
    data = _json_body()
    item = agent.remove_queued(str(data.get("session_id") or ""), str(data.get("queue_id") or ""))
    return jsonify({"removed": item})


@app.post("/api/chat/stop")
def chat_stop():
    data = _json_body()
    stopped = agent.stop_run(str(data.get("session_id") or ""))
    return jsonify({"stopped": stopped})


@app.get("/api/chat/status")
def chat_status():
    return jsonify(agent.run_status(str(request.args.get("session_id") or "")))


@app.post("/api/approval")
def approval_resolve():
    data = _json_body()
    approval_id = str(data.get("id") or "")
    approved = bool(data.get("approved"))
    reason = str(data.get("reason") or ("Approved" if approved else "Denied by user"))
    ok = agent.resolve_approval(approval_id, approved, reason)
    return jsonify({"ok": ok})


@app.post("/api/ask")
def ask_answer():
    data = _json_body()
    question_id = str(data.get("id") or "")
    answer = str(data.get("answer") or "")
    reason = str(data.get("reason") or "Answered by user")
    ok = agent.resolve_question(question_id, answer, reason)
    return jsonify({"ok": ok})


# --------------------------------------------------------------- attachments
@app.post("/api/attachments")
def attachment_upload():
    file = request.files.get("file")
    if not file:
        return err("No file uploaded")
    try:
        meta = attachments.save(file)
    except ValueError as exc:
        return err(exc)
    except Exception as exc:
        return err(f"Could not process attachment: {exc}")
    return jsonify({"attachment": meta}), 201


@app.get("/api/attachments/<att_id>")
def attachment_get(att_id: str):
    meta = attachments.get_public(att_id)
    if not meta:
        return err("Attachment not found", 404)
    return jsonify({"attachment": meta})


@app.get("/api/attachments/<att_id>/raw")
def attachment_raw(att_id: str):
    meta = attachments.get(att_id)
    if not meta:
        return err("Attachment not found", 404)
    p = attachments.data_path(att_id)
    if not p:
        return err("Attachment file is missing", 404)
    return send_file(
        p,
        mimetype=meta.get("mime") or "application/octet-stream",
        as_attachment=False,
        download_name=meta.get("name") or p.name,
        conditional=True,
    )


@app.delete("/api/attachments/<att_id>")
def attachment_delete(att_id: str):
    return jsonify({"ok": attachments.delete(att_id)})


# ------------------------------------------------------------------- models
@app.get("/api/models")
def models_list():
    runtime_status = runtime.status()
    installed = models.scan_installed()
    for m in installed:
        m["loaded"] = m["id"] == runtime_status.get("model_id")
        m["capabilities"] = capabilities.for_installed(m)
    catalog = models.catalog_for_ui(installed, runtime_status)
    for entry in catalog:
        entry["capabilities"] = capabilities.detect(
            entry.get("name"), context=entry.get("context"),
            files=[f for i in entry.get("installed", []) for f in (i.get("files") or [])],
            loaded=any(i.get("loaded") for i in entry.get("installed", [])),
        )
        for inst in entry.get("installed", []):
            inst["capabilities"] = capabilities.for_installed(inst)
    try:
        provider = llm.provider_info()
    except Exception as exc:
        provider = {"error": str(exc), "kind": config.get("provider")}
    return jsonify({
        "installed": installed,
        "catalog": catalog,
        "categories": models.CATEGORY_LABELS,
        "downloads": models.downloads.list(),
        "runtime": runtime_status,
        "disk": models.disk_summary(),
        "provider": provider,
        "capabilities": capabilities.current(),
        "system": {
            "ram": config.system_info().get("ram"),
            "gpus": config.system_info().get("gpus"),
        },
    })


@app.get("/api/models/repo")
def models_repo():
    repo = request.args.get("repo", "")
    try:
        data = models.repo_quants(repo)
    except Exception as exc:
        return err(exc)
    entry = next((c for c in models.CATALOG if c["repo"] == data["repo"]), None)
    wanted = (entry or {}).get("quant", "")
    info = config.system_info()
    ram = info.get("ram") or 0
    gpus = info.get("gpus") or []
    vram = max(((g.get("vram") or 0) for g in gpus), default=0)
    for q in data["quants"]:
        q["recommended"] = bool(wanted and q["quant"] == wanted)
        if not q.get("recommended") and wanted:
            q["recommended"] = q["quant"].startswith(wanted)
        q["size_gb"] = round(q["size"] / models.GB, 2)
        if ram:
            # Windows and the app need room too; don't call a model a "fit"
            # just because its file is smaller than the physical RAM.
            usable = ram * 0.75
            ratio = q["size"] / usable
            q["fit"] = "great" if ratio <= 0.7 else ("tight" if ratio <= 1.0 else "no")
        else:
            q["fit"] = "unknown"
        if vram <= 0:
            q["gpu_fit"] = "none"
        elif q["size"] <= vram * 0.85:
            q["gpu_fit"] = "full"
        else:
            q["gpu_fit"] = "partial"
        q["vram_gb"] = round(vram / 1e9, 1)
    for mm in data.get("mmproj", []):
        mm["size_gb"] = round(mm["size"] / models.GB, 2)
    return jsonify({
        "repo": data["repo"], "quants": data["quants"],
        "mmproj": data.get("mmproj", []), "entry": entry,
        "capabilities": capabilities.detect(
            (entry or {}).get("name") or data["repo"],
            context=(entry or {}).get("context"),
            files=[m["path"] for m in data.get("mmproj", [])],
        ),
    })


@app.post("/api/models/download")
def models_download():
    data = _json_body()
    repo = str(data.get("repo") or "")
    files = data.get("files") or []
    if not isinstance(files, list) or not files:
        return err("No files selected")
    mmproj = data.get("mmproj") or []
    if mmproj and not isinstance(mmproj, list):
        return err("Invalid vision projector selection")
    meta = {
        "name": data.get("name"),
        "quant": data.get("quant"),
        "category": data.get("category"),
        "params": data.get("params"),
        "context": data.get("context"),
    }
    try:
        job = models.downloads.start(repo, files, meta, mmproj=mmproj)
    except Exception as exc:
        return err(exc)
    if data.get("autoload"):
        threading.Thread(
            target=_autoload_when_done, args=(job["id"],), daemon=True, name="autoload"
        ).start()
    return jsonify({"job": job}), 202


@app.post("/api/models/download-url")
def models_download_url():
    data = _json_body()
    try:
        job = models.downloads.start_url(
            str(data.get("url") or ""), str(data.get("name") or ""), str(data.get("category") or "local")
        )
    except Exception as exc:
        return err(exc)
    return jsonify({"job": job}), 202


def _autoload_when_done(job_id: str) -> None:
    for _ in range(7200):
        job = models.downloads.get(job_id)
        if not job:
            return
        if job["status"] == "complete":
            item = models.get_installed(job["model_id"])
            if item:
                try:
                    runtime.start_async(item["main_file"], item["id"], item["name"],
                                        model_context=item.get("context"))
                    config.save_config({"last_model_id": item["id"]})
                except Exception:
                    pass
            return
        if job["status"] in {"error", "cancelled"}:
            return
        time.sleep(1)


@app.get("/api/downloads")
def downloads_list():
    return jsonify({"downloads": models.downloads.list()})


@app.post("/api/models/cancel")
def models_cancel():
    data = _json_body()
    return jsonify({"ok": models.downloads.cancel(str(data.get("job_id") or ""))})


@app.post("/api/models/remove-job")
def models_remove_job():
    data = _json_body()
    try:
        return jsonify({"ok": models.downloads.remove(str(data.get("job_id") or ""))})
    except Exception as exc:
        return err(exc)


@app.get("/api/models/search")
def models_search():
    try:
        results = models.search_hf(request.args.get("q", ""), int(request.args.get("limit", 24)))
        return jsonify({"results": results})
    except Exception as exc:
        return err(exc)


@app.post("/api/models/load")
def models_load():
    data = _json_body()
    item = models.get_installed(str(data.get("id") or ""))
    if not item:
        return err("Model not found. Download it first.", 404)
    try:
        status = runtime.start_async(item["main_file"], item["id"], item["name"],
                                     model_context=item.get("context"))
        config.save_config({"last_model_id": item["id"]})
        return jsonify({"runtime": status})
    except RuntimeErrorLC as exc:
        return err(exc)
    except Exception as exc:
        return err(exc)


@app.post("/api/models/unload")
def models_unload():
    runtime.stop()
    return jsonify({"runtime": runtime.status()})


@app.post("/api/models/delete")
def models_delete():
    data = _json_body()
    model_id = str(data.get("id") or "")
    if runtime.model_id == model_id:
        runtime.stop()
    try:
        models.delete_installed(model_id)
        if config.get("last_model_id") == model_id:
            config.save_config({"last_model_id": None})
        return jsonify({"ok": True})
    except Exception as exc:
        return err(exc)


# ------------------------------------------------------------------ runtime
@app.get("/api/runtime")
def runtime_status():
    include_logs = request.args.get("logs") in ("1", "true", "yes")
    return jsonify(runtime.status(include_logs=include_logs))


@app.post("/api/runtime/install")
def runtime_install():
    data = _json_body()
    flavor = str(data.get("flavor") or config.get("runtime_flavor") or "auto")
    try:
        state = runtime.start_install(flavor)
        config.save_config({"runtime_flavor": flavor})
        return jsonify({"install": state}), 202
    except RuntimeErrorLC as exc:
        return err(exc)
    except Exception as exc:
        return err(exc)


@app.post("/api/runtime/stop")
def runtime_stop():
    runtime.stop()
    return jsonify({"runtime": runtime.status()})


@app.get("/api/runtime/logs")
def runtime_logs():
    lines = max(20, min(int(request.args.get("lines", 200)), 2000))
    return jsonify({"logs": runtime.logs(lines)})


@app.get("/api/provider/models")
def provider_models():
    try:
        return jsonify({"models": llm.list_models()})
    except Exception as exc:
        return err(exc)


@app.post("/api/provider/probe")
def provider_probe():
    return jsonify(llm.probe())


# ------------------------------------------------------------------- MCP
@app.get("/api/mcp")
def mcp_status():
    return jsonify(mcp_manager.public())


@app.get("/api/mcp/tools")
def mcp_tools():
    try:
        return jsonify({"tools": mcp_manager.all_tools(ensure=True, timeout=60)})
    except Exception as exc:
        return err(exc)


@app.post("/api/mcp/servers")
def mcp_add_server():
    data = _json_body()
    name = str(data.get("name") or "")
    cfg = {
        "command": data.get("command"),
        "args": data.get("args") or [],
        "env": data.get("env") or {},
        "cwd": data.get("cwd"),
        "enabled": bool(data.get("enabled", True)),
        "preset": data.get("preset"),
    }
    try:
        server = mcp_manager.add(name, cfg)
        if data.get("start", True):
            server = mcp_manager.start(name)
        return jsonify({"server": server}), 201
    except Exception as exc:
        return err(exc)


@app.post("/api/mcp/presets")
def mcp_add_preset():
    data = _json_body()
    try:
        server = mcp_manager.add_preset(
            str(data.get("preset") or ""),
            str(data.get("name") or "") or None,
            {"args": data.get("args")} if data.get("args") else None,
        )
        if data.get("start", True):
            server = mcp_manager.start(server["name"])
        return jsonify({"server": server}), 201
    except Exception as exc:
        return err(exc)


@app.delete("/api/mcp/servers/<name>")
def mcp_remove_server(name: str):
    try:
        return jsonify({"ok": mcp_manager.remove(name)})
    except Exception as exc:
        return err(exc)


@app.post("/api/mcp/servers/<name>/start")
def mcp_start_server(name: str):
    try:
        return jsonify({"server": mcp_manager.start(name)})
    except Exception as exc:
        return err(exc)


@app.post("/api/mcp/servers/<name>/stop")
def mcp_stop_server(name: str):
    try:
        return jsonify({"server": mcp_manager.stop(name)})
    except Exception as exc:
        return err(exc)


@app.post("/api/mcp/servers/<name>/restart")
def mcp_restart_server(name: str):
    try:
        return jsonify({"server": mcp_manager.restart(name)})
    except Exception as exc:
        return err(exc)


@app.get("/api/mcp/servers/<name>/logs")
def mcp_server_logs(name: str):
    try:
        return jsonify({"logs": mcp_manager.logs(name)})
    except Exception as exc:
        return err(exc)


@app.get("/api/mcp/node")
def mcp_node():
    return jsonify(mcp.node_status())


@app.post("/api/mcp/node/install")
def mcp_node_install():
    try:
        return jsonify(mcp.start_node_install()), 202
    except Exception as exc:
        return err(exc)


# ----------------------------------------------------------------- settings
@app.get("/api/settings")
def settings_get():
    return jsonify({
        "settings": _public_settings(),
        "system": config.refresh_system_info(),
        "runtime": runtime.status(),
        "provider": _safe_provider(),
        "mcp": {"node": mcp.node_status(), "servers": len(mcp_manager.servers)},
    })


@app.patch("/api/settings")
def settings_patch():
    data = _json_body()
    updates = {}
    if "workspace" in data:
        try:
            updates["workspace"] = str(config.set_workspace(str(data["workspace"])))
            config.refresh_system_info()
        except Exception as exc:
            return err(exc)
    if "provider" in data:
        provider = str(data["provider"])
        if provider not in {"managed", "ollama", "openai"}:
            return err("Unknown provider")
        updates["provider"] = provider
    for key, lo, hi, cast in (
        ("context_size", 0, 131072, int),
        ("threads", 0, 64, int),
        ("gpu_layers", -1, 999, int),
        ("max_steps", 0, 10000, int),
        ("temperature", 0.0, 2.0, float),
        ("max_tokens", 128, 32768, int),
        ("compact_threshold", 0.3, 0.95, float),
    ):
        if key in data:
            try:
                value = cast(data[key])
            except Exception:
                return err(f"Invalid value for {key}")
            if value < lo or value > hi:
                return err(f"{key} must be between {lo} and {hi}")
            updates[key] = value
    if "approval_mode" in data:
        if data["approval_mode"] not in {"ask", "auto"}:
            return err("approval_mode must be 'ask' or 'auto'")
        updates["approval_mode"] = data["approval_mode"]
    if "runtime_flavor" in data:
        if data["runtime_flavor"] not in {"auto", "cpu", "vulkan", "cuda"}:
            return err("Unknown runtime flavor")
        updates["runtime_flavor"] = data["runtime_flavor"]
    for key in ("hf_token", "external_base_url", "external_api_key", "external_model"):
        if key in data:
            updates[key] = str(data[key] or "").strip()
    for key in ("auto_load", "show_reasoning", "auto_compact", "mcp_auto_start",
                "show_costs", "keep_alive"):
        if key in data:
            updates[key] = bool(data[key])
    if "theme" in data:
        updates["theme"] = str(data["theme"])
    if "onboarded" in data:
        updates["onboarded"] = bool(data["onboarded"])
    cfg = config.save_config(updates)
    needs_reload = runtime.ready and any(k in updates for k in ("context_size", "threads", "gpu_layers"))
    return jsonify({"settings": _public_settings(), "needs_reload": needs_reload, "raw": cfg})


def _safe_provider() -> dict:
    try:
        return llm.provider_info()
    except Exception as exc:
        return {"error": str(exc), "kind": config.get("provider")}


# ----------------------------------------------------------------- usage
@app.get("/api/usage")
def usage_get():
    session_id = request.args.get("session_id") or None
    return jsonify({
        "usage": db.usage_summary(session_id),
        "pricing": {
            "model": _safe_provider().get("label"),
            "price": pricing.price_for(_safe_provider().get("label")),
            "note": "Local inference costs $0. The hosted estimate maps this model to a comparable paid API.",
        },
    })


@app.get("/api/capabilities")
def capabilities_get():
    model_id = request.args.get("model_id")
    if model_id:
        item = models.get_installed(model_id)
        if not item:
            return err("Model not found", 404)
        return jsonify({"capabilities": capabilities.for_installed(item)})
    return jsonify({"capabilities": capabilities.current()})


@app.get("/api/tools")
def tools_list():
    """Built-in tools, for the UI tools panel and / slash commands."""
    return jsonify({
        "tools": [
            {"name": name, "description": spec.get("description", ""),
             "risk": spec.get("risk", "safe"),
             "args": spec.get("args", {})}
            for name, spec in tools.TOOL_DEFS.items()
        ],
    })


# ----------------------------------------------------------------- memories
@app.get("/api/memories")
def memories_list():
    workspace = request.args.get("workspace") or str(config.workspace_path())
    q = request.args.get("q", "")
    return jsonify({"memories": db.list_memories(workspace, query=q), "workspace": workspace})


@app.post("/api/memories")
def memories_add():
    data = _json_body()
    content = str(data.get("content") or "").strip()
    if not content:
        return err("Memory content is required")
    if len(content) > 4000:
        return err("Memory is too long (max 4000 characters)")
    scope = str(data.get("scope") or "global")
    if scope == "workspace":
        scope = str(config.workspace_path())
    elif scope != "global":
        scope = "global"
    return jsonify({"memory": db.add_memory(content, scope)}), 201


@app.delete("/api/memories/<memory_id>")
def memories_delete(memory_id: str):
    db.delete_memory(memory_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- workspace
@app.get("/api/workspace/tree")
def workspace_tree():
    try:
        return jsonify(tools.tree_level(request.args.get("path", ".")))
    except Exception as exc:
        return err(exc)


@app.get("/api/workspace/file")
def workspace_file():
    try:
        return jsonify(tools.read_preview(request.args.get("path", "")))
    except Exception as exc:
        return err(exc)


# ------------------------------------------------------------------- system
@app.get("/api/system/info")
def system_info():
    info = config.refresh_system_info()
    info["model_disk"] = config.dir_size(config.MODELS_DIR)
    info["disk"] = config.disk_info()
    info["node"] = mcp.node_status()
    info["mcp"] = {
        "servers": len(mcp_manager.servers),
        "connected": len([s for s in mcp_manager.servers.values() if s.status == "ready"]),
    }
    info["runtime"] = runtime.status()
    return jsonify(info)


@app.post("/api/system/quit")
def system_quit():
    """Stop the local engine and exit the process (used by the app window)."""
    def worker():
        time.sleep(0.4)
        try:
            runtime.stop()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=worker, daemon=True, name="quit").start()
    return jsonify({"ok": True})


@app.post("/api/system/reset")
def system_reset():
    """Start from scratch: wipe chats, memories, tools (MCP), settings and
    attachments. Downloaded models and the engine binaries are kept unless
    asked for. Re-running onboarding afterwards makes this a fresh install."""
    import shutil

    data = _json_body()
    wipe_models = bool(data.get("models"))
    wipe_runtime = bool(data.get("runtime_files"))
    if agent.active_run_count():
        return err("An agent is still working. Stop it first, then reset.", 409)
    runtime.stop()
    for name in list(mcp_manager.servers.keys()):
        try:
            mcp_manager.remove(name)
        except Exception:
            pass
    shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    if wipe_models:
        shutil.rmtree(config.MODELS_DIR, ignore_errors=True)
    if wipe_runtime:
        shutil.rmtree(config.RUNTIME_DIR, ignore_errors=True)
    config.clear_cache()
    for path in (config.DATA_DIR, config.MODELS_DIR, config.RUNTIME_DIR,
                 config.LOGS_DIR, config.CACHE_DIR, config.HF_CACHE_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    try:
        attachments.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db.init_db()
    except Exception as exc:
        return err(f"Reset could not finish initializing: {exc}")
    config.save_config({"onboarded": False})
    return jsonify({
        "ok": True,
        "wiped": {
            "settings": True, "chats": True, "memories": True,
            "attachments": True, "mcp": True,
            "models": wipe_models, "runtime_files": wipe_runtime,
        },
    })


@app.post("/api/system/pick-folder")
def pick_folder():
    if os.name != "nt":
        return err("Folder picker is currently available on Windows only. Type the path instead.")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = 'Choose a workspace folder for LocalCode'; "
        "$d.ShowNewFolderButton = $true; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Output $d.SelectedPath }"
    )
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        path = (res.stdout or "").strip().splitlines()
        if not path:
            return jsonify({"cancelled": True})
        return jsonify({"path": path[-1].strip()})
    except Exception as exc:
        return err(f"Could not open the folder picker: {exc}")


@app.post("/api/system/open-folder")
def open_folder():
    data = _json_body()
    kind = str(data.get("kind") or "workspace")
    mapping = {
        "workspace": config.workspace_path(),
        "models": config.MODELS_DIR,
        "data": config.DATA_DIR,
        "logs": config.LOGS_DIR,
        "runtime": config.RUNTIME_DIR,
        "uploads": attachments.UPLOAD_DIR,
    }
    path = mapping.get(kind)
    if not path:
        return err("Unknown folder")
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys_platform() == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return jsonify({"ok": True, "path": str(path)})
    except Exception as exc:
        return err(f"Could not open folder: {exc}")


def sys_platform() -> str:
    import sys
    return sys.platform


@app.get("/api/system/export")
def export_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        sessions = db.list_sessions(limit=10000)
        all_data = []
        for s in sessions:
            all_data.append({
                "session": db.get_session(s["id"]),
                "messages": db.get_messages(s["id"]),
                "usage": db.usage_summary(s["id"]),
            })
        zf.writestr("sessions.json", json.dumps(all_data, indent=2, ensure_ascii=False))
        zf.writestr("memories.json", json.dumps(db.list_memories(), indent=2, ensure_ascii=False))
        zf.writestr("usage.json", json.dumps(db.usage_summary(), indent=2, ensure_ascii=False))
        cfg = dict(config.load_config())
        cfg["hf_token"] = "***" if cfg.get("hf_token") else ""
        cfg["external_api_key"] = "***" if cfg.get("external_api_key") else ""
        zf.writestr("settings.json", json.dumps(cfg, indent=2))
        try:
            zf.writestr("mcp.json", json.dumps(mcp_manager.config, indent=2, ensure_ascii=False))
        except Exception:
            pass
        zf.writestr("README.txt", "LocalCode export. sessions.json contains every chat and tool result.\n")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"localcode-export-{time.strftime('%Y%m%d-%H%M')}.zip")


@app.errorhandler(Exception)
def handle_error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return err(exc.description, exc.code or 500)
    app.logger.exception("Unhandled API error")
    return err(str(exc), 500)


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    attachments.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    mcp_manager.load()
    if config.get("mcp_auto_start", True):
        mcp_manager.start_enabled_async()
