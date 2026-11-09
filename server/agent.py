"""The agent loop: tools, approvals, steering, memory and unlimited sessions."""
from __future__ import annotations

import ast
import json
import platform
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import attachments, capabilities, config, db, llm, mcp, pricing, tools

MAX_TOOL_CONTEXT = 14_000
MAX_ASSISTANT_CONTEXT = 24_000
HARD_STEP_LIMIT = 10_000  # practical infinity; 0 in settings means unlimited
_extra_tool_names: set[str] = set()


# ------------------------------------------------------------------ run state
class RunControl:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.stop_event = threading.Event()
        self.steer_event = threading.Event()
        self.lock = threading.Lock()
        self.queue: list[dict] = []
        self.active = True
        self.phase = "starting"
        self.step = 0
        self.created_at = time.time()
        self.last_activity = time.time()
        self.auto_compactions = 0

    def touch(self, phase: str | None = None, step: int | None = None) -> None:
        self.last_activity = time.time()
        if phase is not None:
            self.phase = phase
        if step is not None:
            self.step = step

    def enqueue(self, content: str, interrupt: bool = False,
                attachments_list: list | None = None) -> dict:
        item = {
            "id": "q_" + uuid.uuid4().hex[:12],
            "content": content,
            "attachments": attachments_list or [],
            "created_at": config.now(),
            "mode": "steer" if interrupt else "queue",
        }
        with self.lock:
            self.queue.append(item)
            if interrupt:
                self.steer_event.set()
        self.touch("steering" if interrupt else self.phase)
        return item

    def take(self) -> tuple[list[dict], bool]:
        with self.lock:
            items = self.queue
            self.queue = []
            steered = self.steer_event.is_set()
            self.steer_event.clear()
            return items, steered

    def has_queued(self) -> bool:
        with self.lock:
            return bool(self.queue)

    def pending(self) -> list[dict]:
        with self.lock:
            return list(self.queue)

    def remove(self, queue_id: str) -> dict | None:
        with self.lock:
            for i, item in enumerate(self.queue):
                if item["id"] == queue_id:
                    return self.queue.pop(i)
        return None

    def cancel_queue(self) -> list[dict]:
        with self.lock:
            items = self.queue
            self.queue = []
            return items


_runs: dict[str, RunControl] = {}
_runs_lock = threading.RLock()


def register_run(session_id: str) -> RunControl | None:
    with _runs_lock:
        existing = _runs.get(session_id)
        if existing and existing.active:
            return None
        control = RunControl(session_id)
        _runs[session_id] = control
        return control


def get_run(session_id: str) -> RunControl | None:
    with _runs_lock:
        control = _runs.get(session_id)
        return control if control and control.active else None


def unregister_run(session_id: str, control: RunControl) -> None:
    with _runs_lock:
        if _runs.get(session_id) is control:
            _runs.pop(session_id, None)
    control.active = False
    control.touch("done")


def active_run_count() -> int:
    with _runs_lock:
        return len(_runs)


def stop_all_runs() -> int:
    with _runs_lock:
        controls = list(_runs.values())
    for control in controls:
        control.stop_event.set()
    return len(controls)


def queue_message(session_id: str, content: str, interrupt: bool = False,
                  attachments_list: list | None = None) -> dict | None:
    control = get_run(session_id)
    if not control:
        return None
    return control.enqueue(content, interrupt, attachments_list)


def remove_queued(session_id: str, queue_id: str) -> dict | None:
    control = get_run(session_id)
    if not control:
        return None
    return control.remove(queue_id)


def stop_run(session_id: str) -> bool:
    control = get_run(session_id)
    if not control:
        return False
    control.stop_event.set()
    control.touch("stopping")
    _deny_control(control, "Stopped by user")
    return True


def run_status(session_id: str) -> dict:
    control = get_run(session_id)
    if not control:
        return {"active": False}
    return {
        "active": True,
        "phase": control.phase,
        "step": control.step,
        "queued": control.pending(),
        "stop": control.stop_event.is_set(),
        "elapsed": int(time.time() - control.created_at),
        "last_activity": control.last_activity,
        "auto_compactions": control.auto_compactions,
        "approvals": list_approvals(session_id),
        "questions": list_questions(session_id),
    }


# ------------------------------------------------------------------ approvals
_approvals: dict[str, dict] = {}
_approvals_lock = threading.RLock()


def create_approval(control: RunControl, tool_name: str, args: dict,
                    risk: str, preview: str) -> dict:
    aid = "apr_" + uuid.uuid4().hex[:12]
    rec = {
        "id": aid,
        "session_id": control.session_id,
        "control": control,
        "tool": tool_name,
        "args": args,
        "risk": risk,
        "preview": preview,
        "event": threading.Event(),
        "decision": None,
        "reason": "",
        "created_at": time.time(),
    }
    with _approvals_lock:
        _approvals[aid] = rec
    return rec


def resolve_approval(approval_id: str, approved: bool, reason: str = "") -> bool:
    with _approvals_lock:
        rec = _approvals.get(approval_id)
    if not rec:
        return False
    rec["decision"] = bool(approved)
    rec["reason"] = reason or ("Approved" if approved else "Denied by user")
    rec["event"].set()
    return True


def _deny_control(control: RunControl, reason: str) -> None:
    with _approvals_lock:
        recs = [r for r in _approvals.values() if r["control"] is control]
    for rec in recs:
        resolve_approval(rec["id"], False, reason)
    for rec in list_questions(control.session_id):
        resolve_question(rec["id"], "", reason=reason)


def deny_control(control: RunControl, reason: str) -> None:
    _deny_control(control, reason)


def list_approvals(session_id: str | None = None) -> list[dict]:
    with _approvals_lock:
        recs = list(_approvals.values())
    out = []
    for rec in recs:
        if session_id and rec["session_id"] != session_id:
            continue
        out.append({
            "id": rec["id"],
            "session_id": rec["session_id"],
            "tool": rec["tool"],
            "args": rec["args"],
            "risk": rec["risk"],
            "preview": rec["preview"],
        })
    return out


def wait_approval(rec: dict, control: RunControl, timeout: float = 1800) -> tuple[bool, str]:
    deadline = time.time() + timeout
    while True:
        if rec["event"].wait(0.35):
            break
        if control.stop_event.is_set():
            resolve_approval(rec["id"], False, "Stopped by user")
            break
        if control.steer_event.is_set():
            resolve_approval(rec["id"], False, "Interrupted by user")
            break
        if time.time() > deadline:
            resolve_approval(rec["id"], False, "Approval timed out")
            break
    with _approvals_lock:
        _approvals.pop(rec["id"], None)
    return bool(rec["decision"]), rec["reason"]


# ------------------------------------------------------------------ questions
_questions: dict[str, dict] = {}
_questions_lock = threading.RLock()


def create_question(control: RunControl, question: str, options: list | None = None,
                    default: str = "") -> dict:
    qid = "ask_" + uuid.uuid4().hex[:12]
    rec = {
        "id": qid,
        "session_id": control.session_id,
        "control": control,
        "question": question,
        "options": [str(o) for o in (options or []) if str(o).strip()][:8],
        "default": str(default or ""),
        "event": threading.Event(),
        "answer": None,
        "reason": "",
        "created_at": time.time(),
    }
    with _questions_lock:
        _questions[qid] = rec
    return rec


def resolve_question(question_id: str, answer: str, reason: str = "") -> bool:
    with _questions_lock:
        rec = _questions.get(question_id)
    if not rec:
        return False
    rec["answer"] = str(answer or "").strip()
    rec["reason"] = reason or "Answered by user"
    rec["event"].set()
    return True


def list_questions(session_id: str | None = None) -> list[dict]:
    with _questions_lock:
        recs = list(_questions.values())
    out = []
    for rec in recs:
        if session_id and rec["session_id"] != session_id:
            continue
        out.append({
            "id": rec["id"],
            "session_id": rec["session_id"],
            "question": rec["question"],
            "options": rec["options"],
            "default": rec["default"],
        })
    return out


def wait_question(rec: dict, control: RunControl, timeout: float = 1800) -> tuple[str, str]:
    deadline = time.time() + timeout
    while True:
        if rec["event"].wait(0.35):
            break
        if control.stop_event.is_set():
            resolve_question(rec["id"], "", "Stopped by user")
            break
        if control.steer_event.is_set():
            resolve_question(rec["id"], "", "Interrupted by user")
            break
        if time.time() > deadline:
            resolve_question(rec["id"], "", "Question timed out")
            break
    with _questions_lock:
        _questions.pop(rec["id"], None)
    return (rec["answer"] or "", rec["reason"])


# ------------------------------------------------------------- tool parsing
TOOL_OPEN = "<tool>"
TOOL_CLOSE = "</tool>"
_TOOL_TAG_RE = re.compile(r"<tool(?:\s+([^>]*))?>", re.IGNORECASE)
_NAME_ATTR_RE = re.compile(r'name\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _valid_tool_name(name: str) -> bool:
    return (
        name in tools.TOOL_NAMES
        or name in _extra_tool_names
        or name.startswith("mcp__")
    )


def _parse_tool_payload(payload: str, forced_name: str | None = None) -> dict | None:
    text = payload.strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json|tool)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    if forced_name and forced_name not in tools.TOOL_NAMES and not _valid_tool_name(forced_name):
        forced_name = None
    if forced_name:
        try:
            args = json.loads(text) if text else {}
        except Exception:
            try:
                args = ast.literal_eval(text) if text else {}
            except Exception:
                args = {}
        return {"name": forced_name, "arguments": args if isinstance(args, dict) else {}}
    # Allow <tool name="read_file">{...}</tool>
    attr = re.match(r'^name\s*=\s*["\']([^"\']+)["\']\s*([\s\S]*)$', text, re.IGNORECASE)
    if attr and _valid_tool_name(attr.group(1)):
        name = attr.group(1)
        rest = attr.group(2).strip()
        try:
            args = json.loads(rest) if rest else {}
        except Exception:
            try:
                args = ast.literal_eval(rest) if rest else {}
            except Exception:
                args = {}
        return {"name": name, "arguments": args if isinstance(args, dict) else {}}
    # Allow read_file({"path": "..."})
    fn = re.match(r"^([A-Za-z_][\w]*)\s*\(([\s\S]*)\)\s*$", text)
    if fn and _valid_tool_name(fn.group(1)):
        raw_args = fn.group(2).strip()
        try:
            args = json.loads(raw_args) if raw_args else {}
        except Exception:
            try:
                args = ast.literal_eval(raw_args) if raw_args else {}
            except Exception:
                args = {}
        return {"name": fn.group(1), "arguments": args if isinstance(args, dict) else {}}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    body = text[start:end + 1]
    try:
        obj = json.loads(body)
    except Exception:
        try:
            obj = ast.literal_eval(body)
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            try:
                args = ast.literal_eval(args)
            except Exception:
                args = {}
    if not isinstance(name, str) or not _valid_tool_name(name):
        return None
    return {"name": name, "arguments": args if isinstance(args, dict) else {}}


class ToolStreamParser:
    """Streams visible text while buffering <tool>…</tool> blocks."""

    def __init__(self) -> None:
        self.buf = ""
        self.in_tool = False
        self.pending_name: str | None = None
        self.calls: list[dict] = []
        self.raw: list[str] = []

    def feed(self, chunk: str) -> list[str]:
        self.raw.append(chunk)
        self.buf += chunk
        out: list[str] = []
        while True:
            if not self.in_tool:
                match = _TOOL_TAG_RE.search(self.buf)
                if not match:
                    # Hold back enough characters for a possible split tag.
                    hold = 12
                    if len(self.buf) > hold:
                        out.append(self.buf[:-hold])
                        self.buf = self.buf[-hold:]
                    break
                if match.start() > 0:
                    out.append(self.buf[:match.start()])
                attr = match.group(1) or ""
                name_match = _NAME_ATTR_RE.search(attr)
                self.pending_name = name_match.group(1) if name_match else None
                self.buf = self.buf[match.end():]
                self.in_tool = True
            else:
                idx = self.buf.find(TOOL_CLOSE)
                if idx == -1:
                    break
                payload = self.buf[:idx]
                self.buf = self.buf[idx + len(TOOL_CLOSE):]
                self.in_tool = False
                call = _parse_tool_payload(payload, self.pending_name)
                self.pending_name = None
                if call:
                    self.calls.append(call)
                else:
                    out.append(TOOL_OPEN + payload + TOOL_CLOSE)
        return out

    def finish(self) -> list[str]:
        out: list[str] = []
        if self.in_tool:
            call = _parse_tool_payload(self.buf, self.pending_name)
            if call:
                self.calls.append(call)
            elif self.buf:
                out.append(TOOL_OPEN + self.buf + TOOL_CLOSE)
            self.buf = ""
            self.in_tool = False
            self.pending_name = None
        elif self.buf:
            out.append(self.buf)
            self.buf = ""
        return out


def _fenced_tool_calls(raw: str) -> list[dict]:
    calls = []
    for m in re.finditer(r"```(?:tool|json)?\s*([\s\S]*?)```", raw, re.IGNORECASE):
        call = _parse_tool_payload(m.group(1))
        if call:
            calls.append(call)
    return calls


# ---------------------------------------------------------------- context
def _read_agents_md(workspace: str) -> str:
    for name in ("AGENTS.md", "LOCALCODE.md", ".localcode/AGENTS.md"):
        p = Path(workspace) / name
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                return text[:8000]
        except Exception:
            continue
    return ""


def _looks_like_chat(text: str) -> bool:
    """True for greetings, small talk and simple questions that need no tools.

    Small local models get distracted by a long tool protocol and start calling
    tools instead of answering. For plain conversation we send a compact prompt
    without any tool instructions, which makes tiny models answer directly and
    much faster.
    """
    low = (text or "").strip().lower()
    if not low:
        return False
    if len(low) > 400:
        return False
    # Continuations and confirmations belong to the current task, so keep tools.
    if low in {"continue", "go on", "keep going", "carry on", "proceed", "next",
               "yes", "yeah", "yep", "ok", "okay", "sure", "do it", "finish",
               "finish it", "keep working"}:
        return False
    if low.startswith(("continue", "go on", "keep going", "carry on", "proceed",
                       "do it", "finish it", "keep working")):
        return False
    # Creative writing is plain conversation, not a file operation.
    if re.match(r"^(write|tell|give me|make up|compose)\b.*\b(poem|story|essay|song|"
                r"joke|haiku|letter|email|paragraph|tale|rap)\b", low):
        return True
    task_words = (
        "file", "folder", "directory", "path", "workspace", "code", "script",
        "function", "bug", "error", "fix", "refactor", "test", "build", "run",
        "execute", "command", "terminal", "shell", "install", "package",
        "dependency", "repo", "git", "create", "write", "edit", "delete", "rename",
        "open", "read", "list", "search", "grep", "find", "project", "website",
        "debug", "compile", "deploy", "server", "model", "download", "attach",
        "image", "screenshot", "pdf", "roblox", "mcp", "api", "app", "tool",
    )
    if re.search(r"\b(" + "|".join(task_words) + r")\b", low):
        return False
    if any(ext in low for ext in (".py", ".js", ".ts", ".html", ".css", ".json",
                                  ".md", ".txt", "src/", "http://", "https://")):
        return False
    return True


def _system_prompt(session: dict, *, aggressive: bool = False,
                   mcp_tools: list[dict] | None = None,
                   chat_only: bool = False) -> str:
    workspace = session.get("workspace") or str(config.workspace_path())
    cfg = config.load_config()
    cap = capabilities.current()
    memories = db.list_memories(workspace)
    ctx_tokens = int(cfg.get("context_size") or cap.get("context") or 8192)
    # Keep the fixed sections small enough that they always fit the window,
    # even on an 8 GB machine with an 8k context.
    mem_budget_chars = max(800, int(ctx_tokens * 0.432))
    mem_parts: list[str] = []
    mem_used = 0
    for m in memories[:80]:
        piece = f"- {m['content'][:400]}"
        if mem_parts and mem_used + len(piece) > mem_budget_chars:
            mem_parts.append(f"- … ({len(memories) - len(mem_parts)} more memories)")
            break
        mem_parts.append(piece)
        mem_used += len(piece)
    mem_lines = "\n".join(mem_parts) or "(none)"
    agents_md = _read_agents_md(workspace)
    summary = (session.get("summary") or "").strip()
    if aggressive and len(summary) > 3000:
        summary = summary[:3000] + "\n... [summary shortened for space]"
    shell = "PowerShell (Windows)" if platform.system() == "Windows" else "bash"
    mcp_budget_chars = max(1200, int(ctx_tokens * 0.648))
    mcp_prompt = mcp.tool_prompt(mcp_tools or [], max_chars=mcp_budget_chars)
    parts = [
        "You are LocalCode, an expert AI coding agent running entirely on the user's computer.",
        "Your name is LocalCode. Always refer to yourself as LocalCode and nothing else.",
        "You have no other name, persona or backstory. Never invent a different name for "
        "yourself, never claim to be another model or person, and never sign or label your "
        "replies with any other name.",
        f"Date: {datetime.now().strftime('%A, %d %B %Y')}",
        f"Operating system: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Workspace: {workspace}",
        f"Shell: {shell}. Use commands appropriate for this platform.",
        f"Loaded model: {cap.get('model')}",
        f"Model input: {', '.join(cap.get('input') or ['text'])}",
        f"Vision ready: {'yes' if cap.get('image') else 'no'}",
        "",
    ]
    if chat_only:
        parts += [
            "This is a normal conversation. You have no tools available right now.",
            "Answer the user directly in plain text. Do not output tool blocks, JSON, or file paths.",
            "Be concise, friendly and accurate.",
        ]
    else:
        parts += [
            "You can inspect and modify files and run commands through tools. Work autonomously:",
            "inspect before editing, make focused changes, verify your work, and report clearly.",
            "Only use tools when the task actually needs files, commands or external data.",
            "If the user asks a simple question, greets you, or asks you to repeat/return specific",
            "text, answer directly in plain text and do not call any tool.",
            "",
            "TOOL PROTOCOL (critical)",
            "To call a tool, output a block exactly like this:",
            "<tool>",
            '{"name": "read_file", "arguments": {"path": "src/main.py"}}',
            "</tool>",
            "Rules:",
            "- Put exactly one JSON object per block, using the tool names below.",
            "- You may write a short sentence before a tool block. Never wrap the block in markdown fences.",
            "- After a tool block, stop and wait for the result. Never invent tool output.",
            "- You may call several tools in sequence; each result is added automatically.",
            "- When you are done, answer normally with no tool block.",
            "- Paths must be relative to the workspace. Never use absolute paths outside it.",
            "- Read a file before editing it. Prefer edit_file over rewriting a whole file.",
            "- Run tests or a build after changes when practical. Do not claim success without checking.",
            "- If a tool errors, read the error and adapt. Do not repeat an identical failing call.",
            "- If a tool succeeds, do not call it again with the same arguments. Finish with a normal answer.",
            "- Never run destructive system commands (format, diskpart, rm -rf outside the workspace).",
            "- Use remember for durable facts and recall to search memory.",
            "",
            "TOOLS",
            tools.tools_prompt(),
        ]
        if mcp_prompt:
            parts += ["", mcp_prompt]
    parts += [
        "",
        "WORKSPACE MEMORY",
        mem_lines,
    ]
    if agents_md:
        parts += ["", "PROJECT INSTRUCTIONS (AGENTS.md)", agents_md]
    if summary:
        parts += [
            "",
            "CONVERSATION MEMORY (summary of earlier messages; the full transcript is still stored)",
            summary,
        ]
    parts += [
        "",
        "UNLIMITED CONVERSATION",
        "There is no fixed conversation length. Older turns are compacted into the memory above.",
        "Never stop just because the conversation is long; continue from the memory and recent messages.",
        f"Context window: about {int(cfg.get('context_size') or cap.get('context') or 8192)} tokens.",
    ]
    if not chat_only:
        parts += [
            "Keep working across as many steps as the task needs. Use todo_write for long tasks.",
            "Keep tool outputs focused.",
            "The user can send extra messages while you work. Treat the newest user message as the highest priority",
            "and adapt your plan instead of ignoring it.",
            "Keep final answers concise: what you changed, where, and how to verify it.",
        ]
    return "\n".join(parts)


def _merge_content(existing, extra_text: str, extra_parts: list[dict] | None = None):
    if not extra_text and not extra_parts:
        return existing
    if isinstance(existing, str):
        if not extra_parts:
            return (existing + "\n\n" + extra_text).strip()
        parts = []
        if existing.strip():
            parts.append({"type": "text", "text": existing.strip()})
        if extra_text.strip():
            parts.append({"type": "text", "text": extra_text.strip()})
        return parts + (extra_parts or [])
    if isinstance(existing, list):
        out = list(existing)
        if extra_text.strip():
            out.append({"type": "text", "text": extra_text.strip()})
        out.extend(extra_parts or [])
        return out
    return existing


def _attachment_parts(message: dict, vision_ready: bool) -> tuple[str, list[dict]]:
    text_blocks: list[str] = []
    image_parts: list[dict] = []
    for meta in message.get("attachments") or []:
        if not isinstance(meta, dict) or not meta.get("id"):
            continue
        full = attachments.get(str(meta["id"]))
        if not full:
            continue
        kind = full.get("kind")
        if kind in {"text", "pdf"}:
            body = attachments.text_for_model(full)
            if body:
                label = "PDF" if kind == "pdf" else "file"
                text_blocks.append(f"[Attached {label}: {full.get('name')}]\n{body}")
            elif full.get("error"):
                text_blocks.append(f"[Attachment {full.get('name')} could not be read: {full['error']}]")
        elif kind == "image":
            if vision_ready:
                url = attachments.image_data_url(full)
                if url:
                    image_parts.append({"type": "image_url", "image_url": {"url": url}})
                    text_blocks.append(f"[Attached image: {full.get('name')}]")
            else:
                text_blocks.append(
                    f"[Image attached: {full.get('name')}. The loaded model cannot see images; "
                    "the image was not sent. Load a vision model with an mmproj projector to use it.]"
                )
        else:
            text_blocks.append(
                f"[{kind or 'file'} attached: {full.get('name')}. This build does not send "
                f"{kind or 'this'} content to the model.]"
            )
    return "\n\n".join(text_blocks), image_parts


def _message_text_tokens(message: dict) -> int:
    role = message.get("role")
    if role == "assistant":
        body = message.get("raw") or message.get("content") or ""
    elif role == "tool":
        body = message.get("content") or ""
    else:
        body = message.get("content") or ""
    return llm.estimate_tokens(body) + 6


def _context_messages(session_id: str, *, aggressive: bool = False,
                      mcp_tools: list[dict] | None = None,
                      chat_only: bool = False) -> tuple[list[dict], dict]:
    session = db.get_session(session_id)
    if not session:
        raise ValueError("Session not found")
    messages = db.get_messages(session_id)
    summary_upto = session.get("summary_upto")
    start_idx = 0
    if summary_upto:
        for i, m in enumerate(messages):
            if m["id"] == summary_upto:
                start_idx = i + 1
                break
    selected = messages[start_idx:]
    if aggressive:
        selected = selected[-6:]
    cap = capabilities.current()
    vision_ready = bool(cap.get("image"))
    out: list[dict] = [{
        "role": "system",
        "content": _system_prompt(session, aggressive=aggressive, mcp_tools=mcp_tools,
                                  chat_only=chat_only),
    }]

    def append(role: str, content, extra_text: str = "", extra_parts=None) -> None:
        content = _merge_content(content, extra_text, extra_parts)
        if isinstance(content, str):
            if not content.strip():
                return
        if out[-1]["role"] == role == "user":
            out[-1]["content"] = _merge_content(out[-1]["content"], "", [])
            if isinstance(out[-1]["content"], str) and isinstance(content, str):
                out[-1]["content"] = (out[-1]["content"] + "\n\n" + content).strip()
            else:
                out[-1]["content"] = _merge_content(out[-1]["content"], content if isinstance(content, str) else "", content if isinstance(content, list) else None)
        else:
            out.append({"role": role, "content": content})

    for m in selected:
        role = m["role"]
        if role == "user":
            extra_text, image_parts = _attachment_parts(m, vision_ready)
            append("user", m["content"], extra_text, image_parts)
        elif role == "assistant":
            raw = (m.get("raw") or m.get("content") or "").strip()
            if raw:
                limit = 6000 if aggressive else MAX_ASSISTANT_CONTEXT
                append("assistant", raw[:limit])
        elif role == "tool":
            body = (m.get("content") or "").strip()
            limit = 3000 if aggressive else MAX_TOOL_CONTEXT
            if len(body) > limit:
                body = body[:limit] + "\n... [tool result truncated]"
            append("user", f"[Tool result: {m.get('tool_name') or 'tool'}]\n{body}")
    tokens = 0
    images = 0
    for m in out:
        content = m["content"]
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    tokens += llm.estimate_tokens(part.get("text") or "")
                elif part.get("type") == "image_url":
                    images += 1
                    tokens += 900
        else:
            tokens += llm.estimate_tokens(content)
        tokens += 4
    ctx = int(config.get("context_size") or 0) or int(cap.get("context") or 8192)
    return out, {
        "tokens": tokens,
        "messages": len(selected),
        "summary": bool(session.get("summary")),
        "context_size": ctx,
        "images": images,
        "vision": vision_ready,
    }


def _transcript_for_summary(messages: list[dict], max_chars: int = 20_000) -> str:
    lines = []
    for m in messages:
        role = m["role"]
        if role == "user":
            body = m["content"]
            if m.get("attachments"):
                names = ", ".join(str(a.get("name")) for a in m["attachments"] if isinstance(a, dict))
                if names:
                    body += f" [attachments: {names}]"
        elif role == "assistant":
            body = m.get("content") or m.get("raw") or ""
        else:
            body = f"tool {m.get('tool_name')}: {m.get('content') or ''}"
        body = re.sub(r"\s+", " ", body).strip()
        if len(body) > 1800:
            body = body[:1800] + "…"
        if body:
            lines.append(f"{role.upper()}: {body}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:6000] + "\n... [middle omitted] ...\n" + text[-12000:]
    return text


def _deterministic_summary(old: list[dict], previous: str = "") -> str:
    users = [m["content"].strip() for m in old if m["role"] == "user" and m["content"].strip()]
    tools_used = []
    files = []
    for m in old:
        if m["role"] == "tool":
            tools_used.append(f"{m.get('tool_name')} ({m.get('status')})")
            try:
                args = json.loads(m.get("tool_args") or "{}")
            except Exception:
                args = {}
            p = args.get("path") if isinstance(args, dict) else None
            if p and str(p) not in files:
                files.append(str(p))
    lines = []
    if previous:
        lines += ["Earlier memory:", previous[:3000], ""]
    lines.append(f"Compacted {len(old)} messages. The full transcript is still stored in the chat database.")
    if users:
        lines.append("User goals and requests:")
        for u in users[-8:]:
            lines.append(f"- {u[:500]}")
    if files:
        lines.append("Files touched: " + ", ".join(files[-20:]))
    if tools_used:
        lines.append("Tools used: " + ", ".join(tools_used[-20:]))
    return "\n".join(lines)[:7000]


def _summarize_one(text: str, previous: str, emit=None) -> str:
    prompt = (
        "You maintain long-term memory for an AI coding assistant. Merge the previous summary "
        "and the new transcript into one compact continuation brief. Keep concrete facts: "
        "the user's goals, decisions, files created or modified (with paths), commands/tests and "
        "their results, unresolved tasks, and important constraints. Remove chatter and repetition. "
        "Write at most 600 words, plain text, no preamble.\n\n"
        f"PREVIOUS SUMMARY:\n{previous or '(none)'}\n\nNEW TRANSCRIPT:\n{text}"
    )
    try:
        return llm.complete_text([
            {"role": "system", "content": "You compress conversation history for a coding agent. Be accurate and concise."},
            {"role": "user", "content": prompt},
        ], max_tokens=900).strip()
    except llm.ContextOverflow:
        try:
            return llm.complete_text([
                {"role": "system", "content": "Compress this conversation into a short memory. Keep files, decisions, tasks and errors."},
                {"role": "user", "content": text[:8000]},
            ], max_tokens=600).strip()
        except Exception:
            return ""
    except Exception:
        return ""


def _summarize(old: list[dict], previous: str, emit=None) -> str:
    transcript = _transcript_for_summary(old)
    if len(transcript) <= 16_000:
        summary = _summarize_one(transcript, previous, emit)
        return summary or _deterministic_summary(old, previous)
    chunks = []
    size = 12_000
    for i in range(0, len(transcript), size):
        chunks.append(transcript[i:i + size])
    partials = []
    for i, chunk in enumerate(chunks[:12], 1):
        if emit:
            emit({"type": "compacting", "phase": "chunk", "chunk": i, "chunks": len(chunks)})
        part = _summarize_one(chunk, "", emit)
        if part:
            partials.append(part)
    if not partials:
        return _deterministic_summary(old, previous)
    merged = _summarize_one("\n\n".join(partials)[:16_000], previous, emit)
    return merged or _deterministic_summary(old, previous)


def compact_session(session_id: str, emit=None, force: bool = False,
                    target_tokens: int | None = None) -> dict:
    session = db.get_session(session_id)
    if not session:
        raise ValueError("Session not found")
    messages = db.get_messages(session_id)
    summary_upto = session.get("summary_upto")
    start_idx = 0
    if summary_upto:
        for i, m in enumerate(messages):
            if m["id"] == summary_upto:
                start_idx = i + 1
                break
    candidates = messages[start_idx:]
    ctx = int(config.get("context_size") or 0) or int(
        capabilities.current().get("context") or 8192
    )
    keep_budget = target_tokens or max(
        1200, int(ctx * (0.24 if force else 0.38))
    )
    keep_count = 0
    used = 0
    for m in reversed(candidates):
        cost = _message_text_tokens(m)
        if keep_count >= 2 and used + cost > keep_budget:
            break
        used += cost
        keep_count += 1
    keep_count = min(keep_count, max(0, len(candidates) - 2))
    if keep_count <= 0 and not force:
        keep_count = min(2, len(candidates))
    # Never compact the current turn. Everything from the latest user message
    # onward must stay in context, including tool results produced for it.
    last_user_idx = None
    for i, m in enumerate(candidates):
        if m["role"] == "user":
            last_user_idx = i
    if last_user_idx is not None:
        keep_count = max(keep_count, len(candidates) - last_user_idx)
    old = candidates[:-keep_count] if keep_count else candidates
    if len(old) < 2 and not force:
        return {"compacted": False, "reason": "Not enough messages to compact yet"}
    if not old:
        return {"compacted": False, "reason": "Nothing old enough to compact"}
    previous = (session.get("summary") or "").strip()
    if emit:
        emit({"type": "compacting", "messages": len(old), "force": force})
    summary = _summarize(old, previous, emit)
    if not summary:
        summary = _deterministic_summary(old, previous)
    summary = summary[:9000]
    db.touch_session(session_id, summary=summary, summary_upto=old[-1]["id"])
    result = {
        "compacted": True,
        "messages": len(old),
        "summary": summary,
        "upto": old[-1]["id"],
        "kept": keep_count,
        "context_size": ctx,
    }
    if emit:
        emit({"type": "compacted", **result})
    return result


# ------------------------------------------------------------------- agent
def _title_from(text: str) -> str:
    text = re.sub(r"[#*_`>\[\]]", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:60] or "New chat").rstrip(" .,:;") or "New chat"


def _tool_risk_for(name: str, args: dict | None = None,
                   mcp_tools: list[dict] | None = None) -> str:
    if name.startswith("mcp__"):
        info = _mcp_tool_info(name, mcp_tools or [])
        if info and info.get("risk"):
            return info["risk"]
        return "write"
    return tools.tool_risk(name)


def _mcp_tool_info(name: str, mcp_tools: list[dict]) -> dict | None:
    return next((t for t in mcp_tools if t.get("qualified") == name), None)


def _provider_label() -> str:
    try:
        return llm.provider_info()["label"]
    except Exception:
        return "local"


def run(session_id: str, control: RunControl, emit) -> None:
    session = db.get_session(session_id)
    if not session:
        emit({"type": "error", "message": "Session not found"})
        return
    cfg = config.load_config()
    configured_steps = int(cfg.get("max_steps") or 0)
    max_steps = HARD_STEP_LIMIT if configured_steps <= 0 else min(max(1, configured_steps), HARD_STEP_LIMIT)
    final_status = "complete"
    started = time.time()
    run_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    model_label = _provider_label()
    emit({"type": "run_start", "session_id": session_id, "model": model_label,
          "unlimited_steps": configured_steps <= 0})
    control.touch("connecting")

    # Connect MCP servers once, then expose their tools to the model.
    mcp_tools: list[dict] = []
    try:
        if config.get("mcp_auto_start", True):
            emit({"type": "mcp", "status": "connecting"})
            mcp_tools = mcp.all_tools(ensure=True, timeout=60)
        else:
            mcp_tools = mcp.all_tools(ensure=False)
        _extra_tool_names.clear()
        _extra_tool_names.update(t.get("qualified") for t in mcp_tools if t.get("qualified"))
        if mcp_tools:
            emit({"type": "mcp", "status": "ready", "tools": len(mcp_tools)})
    except Exception as exc:
        emit({"type": "mcp", "status": "error", "error": str(exc)})
    if not mcp_tools:
        _extra_tool_names.clear()

    try:
        repeat_counts: dict[str, int] = {}
        tool_only_streak = 0
        for step in range(1, max_steps + 1):
            control.touch("working", step)
            if control.stop_event.is_set():
                final_status = "stopped"
                break

            # Pick up MCP tools that connected after the run started.
            try:
                refreshed = mcp.all_tools(ensure=False)
                if len(refreshed) != len(mcp_tools):
                    mcp_tools = refreshed
                    _extra_tool_names.clear()
                    _extra_tool_names.update(
                        t.get("qualified") for t in mcp_tools if t.get("qualified")
                    )
            except Exception:
                pass

            # Deliver any messages the user queued while the agent was working.
            delivered, _steered_flag = control.take()
            for item in delivered:
                msg = db.add_message(
                    session_id, "user", item["content"],
                    attachments=item.get("attachments") or [],
                    status="complete",
                )
                emit({
                    "type": "user_message", "message": msg,
                    "queue_id": item["id"], "mode": item.get("mode", "queue"),
                })
            if control.stop_event.is_set():
                final_status = "stopped"
                break

            # Build context; compact proactively so the window is never exceeded.
            recent = db.get_messages(session_id)
            last_user_text = next(
                (m.get("content") for m in reversed(recent) if m.get("role") == "user"), ""
            )
            chat_only = (
                isinstance(last_user_text, str)
                and _looks_like_chat(last_user_text)
                and not any(m.get("role") == "tool" for m in recent[-4:])
            )
            context_retries = 0
            while True:
                try:
                    messages, meta = _context_messages(
                        session_id, mcp_tools=mcp_tools, chat_only=chat_only
                    )
                    context_size = int(meta["context_size"] or 8192)
                    threshold = float(cfg.get("compact_threshold") or 0.72)
                    if (cfg.get("auto_compact", True) and meta["messages"] > 6
                            and meta["tokens"] > context_size * threshold):
                        compact_session(session_id, emit=emit)
                        control.auto_compactions += 1
                        messages, meta = _context_messages(
                            session_id, mcp_tools=mcp_tools, chat_only=chat_only
                        )
                    if meta["tokens"] > context_size * 0.97:
                        compact_session(session_id, emit=emit, force=True,
                                        target_tokens=max(800, int(context_size * 0.18)))
                        messages, meta = _context_messages(
                            session_id, aggressive=True, mcp_tools=mcp_tools,
                            chat_only=chat_only
                        )
                    break
                except llm.ContextOverflow:
                    context_retries += 1
                    if context_retries > 3:
                        raise
                    compact_session(session_id, emit=emit, force=True,
                                    target_tokens=800)
                    messages, meta = _context_messages(
                        session_id, aggressive=True, mcp_tools=mcp_tools,
                        chat_only=chat_only
                    )
                    break
                except Exception as exc:
                    emit({"type": "error", "message": f"Could not build the conversation context: {exc}"})
                    final_status = "error"
                    break
            if final_status == "error":
                break

            # Safety net for small models that keep calling tools forever without
            # ever answering. Normal coding work still gets plenty of steps.
            if tool_only_streak >= 12:
                messages = messages + [{
                    "role": "user",
                    "content": (
                        "You have made many tool calls without answering the user. "
                        "Stop calling tools now and give your final answer in plain text."
                    ),
                }]
                tool_only_streak = 0

            emit({
                "type": "context",
                "tokens": meta["tokens"],
                "context_size": context_size,
                "percent": round(meta["tokens"] / context_size * 100, 1),
                "compacted": meta["summary"],
                "unlimited": True,
            })
            emit({"type": "step", "step": step, "max_steps": 0 if configured_steps <= 0 else max_steps})

            assistant = None
            parser = None
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage = None
            native_calls: list[dict] = []
            stream_error = None
            context_overflow = False
            finish_reason = None
            stream_retries = 0

            while True:
                assistant = db.add_message(
                    session_id, "assistant", "", status="streaming", model=model_label
                )
                emit({"type": "assistant_start", "message_id": assistant["id"], "step": step})
                parser = ToolStreamParser()
                text_parts = []
                reasoning_parts = []
                usage = None
                native_calls = []
                stream_error = None
                context_overflow = False
                finish_reason = None
                try:
                    for event in llm.stream_chat(
                        messages,
                        stop_event=control.stop_event,
                        steer_event=control.steer_event,
                    ):
                        kind = event.get("type")
                        if kind == "text":
                            for out in parser.feed(event["text"]):
                                if out:
                                    text_parts.append(out)
                                    emit({"type": "delta", "text": out})
                        elif kind == "reasoning":
                            reasoning_parts.append(event["text"])
                            emit({"type": "reasoning", "text": event["text"]})
                        elif kind == "usage":
                            usage = event["usage"]
                        elif kind == "finish":
                            finish_reason = event.get("reason")
                        elif kind == "native_tools":
                            for tc in event["tool_calls"]:
                                fn = tc.get("function") or {}
                                try:
                                    args = json.loads(fn.get("arguments") or "{}")
                                except Exception:
                                    args = {}
                                if _valid_tool_name(fn.get("name") or ""):
                                    native_calls.append({"name": fn["name"], "arguments": args})
                        elif kind == "interrupted":
                            break
                except llm.ContextOverflow as exc:
                    context_overflow = True
                    stream_error = str(exc)
                except llm.LLMError as exc:
                    stream_error = str(exc)
                except Exception as exc:
                    stream_error = f"Unexpected model error: {exc}"

                for out in parser.finish():
                    if out:
                        text_parts.append(out)
                        emit({"type": "delta", "text": out})

                if context_overflow:
                    db.delete_message(assistant["id"])
                    compact_session(session_id, emit=emit, force=True,
                                    target_tokens=max(800, int(context_size * 0.16)))
                    control.auto_compactions += 1
                    messages, meta = _context_messages(
                        session_id, aggressive=True, mcp_tools=mcp_tools
                    )
                    context_retries += 1
                    if context_retries > 4:
                        break
                    emit({"type": "context", "tokens": meta["tokens"],
                          "context_size": context_size,
                          "percent": round(meta["tokens"] / context_size * 100, 1),
                          "compacted": True, "unlimited": True})
                    continue
                if stream_error and stream_retries < 2 and any(
                    token in stream_error.lower() for token in
                    ("timeout", "timed out", "connection", "temporarily", "still loading")
                ) and not control.stop_event.is_set():
                    db.delete_message(assistant["id"])
                    stream_retries += 1
                    time.sleep(1.5 * stream_retries)
                    continue
                break

            stopped = control.stop_event.is_set()
            steered = control.steer_event.is_set()
            calls = parser.calls + native_calls if parser else []
            visible = "".join(text_parts)
            raw = "".join(parser.raw) if parser else visible
            if stopped or steered:
                calls = []
                raw = visible
            status = "error" if stream_error and not context_overflow else (
                "stopped" if stopped else ("steered" if steered else "complete")
            )
            content = visible
            if not content.strip() and reasoning_parts and not calls:
                content = "".join(reasoning_parts)
            tool_only_streak = tool_only_streak + 1 if (calls and not content.strip()) else 0
            if stream_error and not context_overflow:
                content = f"⚠️ {stream_error}"
                reasoning_parts = []

            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            cost_usd = 0.0
            if usage:
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            if not prompt_tokens:
                prompt_tokens = int(meta.get("tokens") or 0)
            if not completion_tokens:
                completion_tokens = llm.estimate_tokens(content) + llm.estimate_tokens("".join(reasoning_parts))
            if not total_tokens:
                total_tokens = prompt_tokens + completion_tokens
            price = pricing.estimate(prompt_tokens, completion_tokens, model_label)
            cost_usd = float(price.get("cost_usd") or 0)

            if assistant:
                db.update_message(
                    assistant["id"],
                    content=content,
                    raw=raw,
                    reasoning="".join(reasoning_parts),
                    tool_calls=calls,
                    status=status,
                    tokens=total_tokens,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                )
                db.add_usage(session_id, prompt_tokens, completion_tokens, cost_usd)
                assistant.update({
                    "content": content, "raw": raw, "reasoning": "".join(reasoning_parts),
                    "tool_calls": calls, "status": status, "tokens": total_tokens,
                    "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                    "cost_usd": cost_usd,
                })
                run_usage["prompt_tokens"] += prompt_tokens
                run_usage["completion_tokens"] += completion_tokens
                run_usage["total_tokens"] += total_tokens
                run_usage["cost_usd"] += cost_usd
                session_now = db.get_session(session_id) or {}
                emit({
                    "type": "assistant_end",
                    "message": assistant,
                    "status": status,
                    "usage": usage,
                    "tokens": {
                        "prompt": prompt_tokens,
                        "completion": completion_tokens,
                        "total": total_tokens,
                        "cost_usd": cost_usd,
                        "price": price,
                        "session": {
                            "prompt_tokens": session_now.get("total_prompt_tokens"),
                            "completion_tokens": session_now.get("total_completion_tokens"),
                            "total_tokens": session_now.get("total_tokens"),
                            "cost_usd": session_now.get("total_cost_usd"),
                        },
                    },
                })

            if context_overflow:
                if context_retries > 4:
                    emit({"type": "error", "message": "The model kept rejecting the context even after aggressive compaction. Try a larger context size in Settings."})
                    final_status = "error"
                    break
                continue
            if stream_error:
                emit({"type": "error", "message": stream_error})
                final_status = "error"
                break
            if stopped:
                final_status = "stopped"
                break
            if steered:
                control.steer_event.clear()
                continue
            if calls:
                interrupt_after_tools = False
                loop_break = False
                for call in calls:
                    if control.stop_event.is_set():
                        final_status = "stopped"
                        interrupt_after_tools = True
                        break
                    name = call.get("name")
                    args = call.get("arguments") or {}
                    call_id = "call_" + uuid.uuid4().hex[:10]
                    if not _valid_tool_name(name):
                        output = f"Tool error: unknown tool {name!r}. Available: {', '.join(tools.TOOL_NAMES)}"
                        tool_status = "error"
                        meta = {}
                        emit({"type": "tool_call", "id": call_id, "name": name,
                              "args": args, "risk": "write"})
                    else:
                        risk = _tool_risk_for(name, args, mcp_tools)
                        emit({"type": "tool_call", "id": call_id, "name": name,
                              "args": args, "risk": risk})
                        approved = True
                        deny_reason = ""
                        if name == "ask_user":
                            question = str(args.get("question") or "").strip()
                            if not question:
                                output = "Tool error: ask_user requires a question."
                                tool_status = "error"
                                meta = {}
                            else:
                                rec = create_question(
                                    control, question,
                                    args.get("options") if isinstance(args.get("options"), list) else [],
                                    str(args.get("default") or ""),
                                )
                                emit({
                                    "type": "ask_user", "id": rec["id"],
                                    "question": question, "options": rec["options"],
                                    "default": rec["default"],
                                })
                                answer, reason = wait_question(rec, control)
                                if answer:
                                    output = answer
                                    tool_status = "ok"
                                else:
                                    output = f"No answer from user ({reason}). Continue with a sensible default or ask later."
                                    tool_status = "denied"
                                meta = {"question": question, "options": rec["options"]}
                        else:
                            repeat_key = name + "\n" + json.dumps(args, sort_keys=True, ensure_ascii=False)
                            repeat_counts[repeat_key] = repeat_counts.get(repeat_key, 0) + 1
                            repeat_count = repeat_counts[repeat_key]
                            if repeat_count > 3:
                                output = (
                                    "Tool error: this exact call has already been attempted three times. "
                                    "Change your approach, inspect the result, or ask the user."
                                )
                                tool_status = "error"
                                meta = {"repeated": True}
                                if repeat_count >= 4:
                                    meta["loop"] = True
                            else:
                                if cfg.get("approval_mode") != "auto" and risk != "safe":
                                    try:
                                        if name.startswith("mcp__"):
                                            info = _mcp_tool_info(name, mcp_tools)
                                            preview = f"MCP tool {name}\n" + json.dumps(args, indent=2, ensure_ascii=False)[:2000]
                                            if info and info.get("description"):
                                                preview = info["description"] + "\n\n" + preview
                                        else:
                                            preview = tools.preview_tool(name, args)
                                    except Exception:
                                        preview = ""
                                    rec = create_approval(control, name, args, risk, preview)
                                    emit({
                                        "type": "approval", "id": rec["id"], "tool": name,
                                        "args": args, "risk": risk, "preview": preview,
                                    })
                                    approved, deny_reason = wait_approval(rec, control)
                                if not approved:
                                    output = f"Tool not run: {deny_reason or 'denied by user'}."
                                    tool_status = "denied"
                                    meta = {}
                                else:
                                    emit({"type": "tool_running", "id": call_id, "name": name})
                                    tool_started = time.time()
                                    try:
                                        if name.startswith("mcp__"):
                                            output, meta = mcp.call(name, args)
                                        else:
                                            output, meta = tools.execute_tool(name, args)
                                        tool_status = "ok"
                                    except tools.ToolError as exc:
                                        output = f"Tool error: {exc}"
                                        tool_status = "error"
                                        meta = {}
                                    except mcp.McpError as exc:
                                        output = f"MCP tool error: {exc}"
                                        tool_status = "error"
                                        meta = {}
                                    except Exception as exc:
                                        output = f"Tool failed: {exc}"
                                        tool_status = "error"
                                        meta = {}
                                    meta = dict(meta or {})
                                    meta["duration_ms"] = int((time.time() - tool_started) * 1000)
                    tool_msg = db.add_message(
                        session_id, "tool", output, tool_name=name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        meta=meta, status=tool_status,
                        tokens=llm.estimate_tokens(output),
                    )
                    emit({
                        "type": "tool_result", "id": call_id, "message": tool_msg,
                        "name": name, "status": tool_status, "output": output,
                        "meta": meta,
                    })
                    if meta.get("loop"):
                        emit({
                            "type": "notice",
                            "message": (
                                "Stopped a repeated tool-call loop: the same call was attempted 4 times. "
                                "Send a new message to continue with a different approach."
                            ),
                        })
                        final_status = "loop"
                        loop_break = True
                        break
                    if control.stop_event.is_set():
                        final_status = "stopped"
                        interrupt_after_tools = True
                        break
                    if control.steer_event.is_set():
                        control.steer_event.clear()
                        break
                if loop_break:
                    break
                if interrupt_after_tools:
                    break
                continue

            # No tool calls. If the user queued something during the final stream, keep going.
            if control.has_queued():
                continue
            final_status = "complete"
            break
        else:
            final_status = "step_limit"
            emit({
                "type": "notice",
                "message": f"Paused after {max_steps:,} steps as a safety measure. Send another message to continue.",
            })
    except Exception as exc:
        final_status = "error"
        emit({"type": "error", "message": f"Agent error: {exc}"})
    finally:
        leftover = control.cancel_queue()
        if leftover:
            emit({"type": "queue_cancelled", "messages": leftover})
        try:
            latest = db.get_session(session_id)
            if latest and (latest["title"] in ("New chat", "Untitled") or not latest["title"]):
                first_user = next(
                    (m["content"] for m in db.get_messages(session_id) if m["role"] == "user"), ""
                )
                if first_user:
                    db.touch_session(session_id, title=_title_from(first_user))
            db.touch_session(session_id, model=model_label)
            latest = db.get_session(session_id)
            session_usage = {
                "prompt_tokens": latest.get("total_prompt_tokens") if latest else 0,
                "completion_tokens": latest.get("total_completion_tokens") if latest else 0,
                "total_tokens": latest.get("total_tokens") if latest else 0,
                "cost_usd": latest.get("total_cost_usd") if latest else 0,
            }
        except Exception:
            session_usage = run_usage
        emit({
            "type": "done",
            "session_id": session_id,
            "status": final_status,
            "duration_ms": int((time.time() - started) * 1000),
            "usage": run_usage,
            "session_usage": session_usage,
            "price": pricing.price_for(model_label),
            "unlimited": True,
        })
