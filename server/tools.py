"""Workspace tools available to the local coding agent."""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config, db

MAX_READ_BYTES = 1_000_000
MAX_WRITE_BYTES = 5_000_000
MAX_OUTPUT_CHARS = 30_000
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".next", ".nuxt", "dist", "build", "target", ".idea", ".vscode", "models",
    "runtime", "data", ".localcode", ".cache", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "site-packages",
}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}


class ToolError(Exception):
    pass


TOOL_DEFS: dict[str, dict] = {
    "list_files": {
        "description": "List files and folders in the workspace as an indented tree.",
        "args": {"path": "optional relative folder (default '.')", "depth": "optional depth 1-4 (default 2)"},
        "risk": "safe",
    },
    "read_file": {
        "description": "Read a UTF-8 text file with line numbers.",
        "args": {"path": "relative file path", "start_line": "optional 1-based first line", "end_line": "optional last line"},
        "risk": "safe",
    },
    "grep": {
        "description": "Search file contents using a regular expression (or literal text).",
        "args": {"pattern": "regex or text", "path": "optional folder", "glob": "optional file glob like *.py", "case_sensitive": "optional bool", "max_results": "optional 1-200"},
        "risk": "safe",
    },
    "write_file": {
        "description": "Create or overwrite a file with complete content.",
        "args": {"path": "relative file path", "content": "full file content"},
        "risk": "write",
    },
    "edit_file": {
        "description": "Replace an exact string in an existing file. Prefer this over rewriting files.",
        "args": {"path": "relative file path", "old_string": "exact text to find", "new_string": "replacement text", "replace_all": "optional bool"},
        "risk": "write",
    },
    "make_dir": {
        "description": "Create a directory (including parents).",
        "args": {"path": "relative directory path"},
        "risk": "write",
    },
    "delete_path": {
        "description": "Delete a file or directory inside the workspace.",
        "args": {"path": "relative path", "recursive": "optional bool for directories"},
        "risk": "delete",
    },
    "run_command": {
        "description": "Run a shell command in the workspace (PowerShell on Windows, bash elsewhere).",
        "args": {"command": "command to run", "timeout": "optional seconds 1-300 (default 60)"},
        "risk": "shell",
    },
    "todo_write": {
        "description": "Record/update a short task plan. Use for multi-step work.",
        "args": {"todos": "array of {content, status: pending|in_progress|completed}"},
        "risk": "safe",
    },
    "glob": {
        "description": "Find files by glob pattern, e.g. '**/*.py' or 'src/**/*.ts'.",
        "args": {"pattern": "glob pattern", "path": "optional folder", "max_results": "optional 1-500"},
        "risk": "safe",
    },
    "remember": {
        "description": "Save a durable fact, preference or decision to long-term memory.",
        "args": {"content": "what to remember", "scope": "global or workspace (default workspace)"},
        "risk": "safe",
    },
    "recall": {
        "description": "Search long-term memory for facts, preferences or decisions.",
        "args": {"query": "optional search text"},
        "risk": "safe",
    },
    "ask_user": {
        "description": "Ask the user a question and wait for their answer. Use only when genuinely blocked.",
        "args": {"question": "the question", "options": "optional array of suggested answers", "default": "optional default answer"},
        "risk": "safe",
    },
    "fetch_url": {
        "description": "Fetch a public http(s) URL and return readable text. Useful for docs.",
        "args": {"url": "http(s) URL", "max_chars": "optional 1000-100000"},
        "risk": "network",
    },
}

TOOL_NAMES = list(TOOL_DEFS.keys())


def tool_risk(name: str) -> str:
    return TOOL_DEFS.get(name, {}).get("risk", "write")


def tools_prompt() -> str:
    lines = []
    for name, spec in TOOL_DEFS.items():
        arg_bits = "; ".join(f"{k}: {v}" for k, v in spec["args"].items())
        lines.append(f"- {name} — {spec['description']} Arguments: {arg_bits}")
    return "\n".join(lines)


def safe_resolve(rel: str, must_exist: bool = False) -> Path:
    base = config.workspace_path().resolve()
    if rel is None or str(rel).strip() == "":
        rel = "."
    raw = str(rel).strip().strip('"').strip("'")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = base / p
    p = p.resolve()
    if p != base and base not in p.parents:
        raise ToolError("Path is outside the workspace. Use a path relative to the workspace root.")
    if must_exist and not p.exists():
        raise ToolError(f"Path does not exist: {rel}")
    return p


def _rel(p: Path) -> str:
    base = config.workspace_path().resolve()
    try:
        return p.relative_to(base).as_posix() or "."
    except ValueError:
        return str(p)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    return f"{head}\n\n... [output truncated, {len(text) - limit} chars omitted] ...\n\n{tail}"


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _read_text(p: Path) -> str:
    data = p.read_bytes()
    if _is_binary(data):
        raise ToolError(f"File appears to be binary: {_rel(p)}")
    return data.decode("utf-8", errors="replace")


def make_diff(path: str, old: str, new: str, context: int = 3) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=context,
        )
    )
    if len(diff) > 500:
        diff = diff[:500] + ["... diff truncated ..."]
    return "\n".join(diff)


def _walk_files(root: Path, pattern: str = "*"):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and not d.startswith(".localcode")
        )
        for name in sorted(filenames):
            if name in IGNORED_FILES:
                continue
            if pattern and pattern != "*" and not fnmatch.fnmatch(name, pattern):
                continue
            p = Path(dirpath) / name
            try:
                if p.stat().st_size > MAX_READ_BYTES:
                    continue
            except OSError:
                continue
            yield p


def _list_files(args: dict) -> tuple[str, dict]:
    rel = str(args.get("path") or ".")
    depth = int(args.get("depth") or 2)
    depth = max(1, min(depth, 4))
    root = safe_resolve(rel, must_exist=True)
    if root.is_file():
        return f"{_rel(root)} ({root.stat().st_size} bytes)", {}
    lines: list[str] = []
    count = 0
    truncated = False
    base_depth = len(root.parts)

    def walk(d: Path, level: int):
        nonlocal count, truncated
        if truncated:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name in IGNORED_FILES:
                continue
            if entry.is_dir() and entry.name in IGNORED_DIRS:
                continue
            indent = "  " * (level - 1)
            try:
                if entry.is_dir():
                    lines.append(f"{indent}{entry.name}/")
                    count += 1
                    if level < depth:
                        walk(entry, level + 1)
                else:
                    size = entry.stat().st_size
                    lines.append(f"{indent}{entry.name}  ({config.human_bytes(size)})")
                    count += 1
            except OSError:
                continue
            if count >= 600:
                truncated = True
                return

    walk(root, 1)
    header = f"{_rel(root)}/" if root != config.workspace_path().resolve() else "./"
    body = "\n".join(lines) or "(empty)"
    if truncated:
        body += "\n... [listing truncated] ..."
    return f"{header}\n{body}", {"entries": count}


def _read_file(args: dict) -> tuple[str, dict]:
    p = safe_resolve(str(args.get("path") or ""), must_exist=True)
    if p.is_dir():
        raise ToolError("Path is a directory; use list_files.")
    if p.stat().st_size > MAX_READ_BYTES:
        raise ToolError(f"File is too large ({config.human_bytes(p.stat().st_size)}); read a smaller file or use grep.")
    text = _read_text(p)
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(args.get("start_line") or 1))
    end = min(total, int(args.get("end_line") or total))
    if start > total:
        return f"{_rel(p)} has {total} lines (empty range).", {"lines": total}
    selected = lines[start - 1:end]
    width = len(str(end))
    numbered = "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(selected, start=start))
    note = ""
    if start > 1 or end < total:
        note = f"\n\n[showing lines {start}-{end} of {total}]"
    return _truncate(f"{_rel(p)} ({total} lines)\n{numbered}{note}"), {"lines": total, "start": start, "end": end}


def _grep(args: dict) -> tuple[str, dict]:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        raise ToolError("pattern is required")
    path = str(args.get("path") or ".")
    glob = str(args.get("glob") or "*")
    case_sensitive = bool(args.get("case_sensitive", False))
    max_results = max(1, min(int(args.get("max_results") or 80), 200))
    root = safe_resolve(path, must_exist=True)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
        literal = False
    except re.error:
        rx = re.compile(re.escape(pattern), flags)
        literal = True
    files = [root] if root.is_file() else list(_walk_files(root, glob))
    results: list[str] = []
    files_hit = 0
    for fp in files:
        try:
            data = fp.read_bytes()
        except OSError:
            continue
        if _is_binary(data):
            continue
        text = data.decode("utf-8", errors="replace")
        hit_this = False
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                if not hit_this:
                    files_hit += 1
                    hit_this = True
                snippet = line.strip()
                if len(snippet) > 300:
                    snippet = snippet[:300] + "…"
                results.append(f"{_rel(fp)}:{lineno}: {snippet}")
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    if not results:
        return f"No matches for {pattern!r} in {_rel(root)}.", {"matches": 0, "files": 0}
    note = ""
    if len(results) >= max_results:
        note = f"\n\n[stopped at {max_results} matches]"
    mode = " (literal, invalid regex)" if literal else ""
    return _truncate(f"Matches for {pattern!r}{mode}:\n" + "\n".join(results) + note), {
        "matches": len(results), "files": files_hit
    }


def _write_file(args: dict) -> tuple[str, dict]:
    rel = str(args.get("path") or "")
    content = args.get("content")
    if not rel:
        raise ToolError("path is required")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError("content is too large (max 5 MB)")
    p = safe_resolve(rel)
    if p == config.workspace_path().resolve() or p.is_dir():
        raise ToolError("Cannot overwrite a directory.")
    old = ""
    existed = p.exists()
    if existed:
        try:
            old = _read_text(p)
        except ToolError:
            old = ""
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    diff = make_diff(_rel(p), old, content)
    action = "Updated" if existed else "Created"
    return f"{action} {_rel(p)} ({len(content.splitlines())} lines, {config.human_bytes(len(content.encode('utf-8')))}).", {
        "diff": diff, "created": not existed, "path": _rel(p)
    }


def _edit_file(args: dict) -> tuple[str, dict]:
    rel = str(args.get("path") or "")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))
    if not rel:
        raise ToolError("path is required")
    if not isinstance(old_string, str) or not old_string:
        raise ToolError("old_string must be a non-empty string")
    if not isinstance(new_string, str):
        raise ToolError("new_string must be a string")
    p = safe_resolve(rel, must_exist=True)
    if p.is_dir():
        raise ToolError("Path is a directory.")
    text = _read_text(p)
    count = text.count(old_string)
    if count == 0:
        hint = ""
        normalized = text.replace("\r\n", "\n")
        if old_string.replace("\r\n", "\n") in normalized:
            hint = " (the file uses CRLF line endings; match them or use write_file)"
        raise ToolError(f"old_string was not found in {_rel(p)}.{hint}")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times in {_rel(p)}. Add more context or set replace_all=true."
        )
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated)
    diff = make_diff(_rel(p), text, updated)
    return f"Edited {_rel(p)} ({count if replace_all else 1} replacement(s)).", {
        "diff": diff, "path": _rel(p), "replacements": count if replace_all else 1
    }


def _make_dir(args: dict) -> tuple[str, dict]:
    p = safe_resolve(str(args.get("path") or ""))
    if p.exists() and not p.is_dir():
        raise ToolError("A file already exists at that path.")
    p.mkdir(parents=True, exist_ok=True)
    return f"Directory ready: {_rel(p)}", {"path": _rel(p)}


def _delete_path(args: dict) -> tuple[str, dict]:
    p = safe_resolve(str(args.get("path") or ""), must_exist=True)
    base = config.workspace_path().resolve()
    if p == base:
        raise ToolError("Refusing to delete the workspace root.")
    recursive = bool(args.get("recursive", False))
    if p.is_dir():
        if any(p.iterdir()) and not recursive:
            raise ToolError("Directory is not empty. Set recursive=true to delete it.")
        shutil.rmtree(p) if recursive else p.rmdir()
    else:
        p.unlink()
    return f"Deleted {_rel(p)}.", {"path": _rel(p)}


def _run_command(args: dict) -> tuple[str, dict]:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError("command is required")
    low = command.lower()
    # Defense in depth: even in auto-approve mode, never let the agent run
    # commands that destroy the system or wipe drives.
    destructive = (
        "diskpart", "cipher /w", "rm -rf /", "rm -rf /*", "rm -fr /",
        "dd if=", "shutdown /s", "shutdown /r", "logoff", "mkfs.",
        "del /s /q c:\\", "del /f /s /q c:\\", "rd /s /q c:\\",
        "rmdir /s /q c:\\", "del /s /q %systemdrive%", "format c:",
    )
    for bad in destructive:
        if bad in low:
            raise ToolError(f"Refusing to run a destructive command ({bad.strip()}).")
    if re.search(r"format\s+[a-z]:", low):
        raise ToolError("Refusing to run a destructive command (format <drive>).")
    timeout = max(1, min(int(args.get("timeout") or 60), 300))
    cwd = str(config.workspace_path())
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    if os.name == "nt":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    else:
        argv = ["bash", "-lc", command]
    start = time.time()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            proc.kill()
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:
            output = ""
    duration = int((time.time() - start) * 1000)
    output = output or ""
    if timed_out:
        output += f"\n\n[command timed out after {timeout}s]"
    body = _truncate(output.strip() or "(no output)")
    code = proc.returncode if proc.returncode is not None else -1
    header = f"exit_code={code}, duration={duration}ms\n"
    return header + body, {"exit_code": code, "duration_ms": duration, "timed_out": timed_out}


def _todo_write(args: dict) -> tuple[str, dict]:
    todos = args.get("todos")
    if not isinstance(todos, list):
        raise ToolError("todos must be an array")
    icons = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = []
    clean = []
    for item in todos[:20]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending").lower()
        if status not in icons:
            status = "pending"
        if content:
            lines.append(f"{icons[status]} {content}")
            clean.append({"content": content, "status": status})
    return "Plan updated:\n" + ("\n".join(lines) or "(empty)"), {"todos": clean}


def _glob(args: dict) -> tuple[str, dict]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        raise ToolError("pattern is required")
    root = safe_resolve(str(args.get("path") or "."), must_exist=True)
    max_results = max(1, min(int(args.get("max_results") or 200), 500))
    base = config.workspace_path().resolve()
    matches: list[str] = []
    if root.is_file():
        candidates = [root]
    else:
        candidates = list(_walk_files(root, "*"))
    for p in candidates:
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            rel = p.as_posix()
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
            matches.append(rel)
            if len(matches) >= max_results:
                break
    if not matches:
        return f"No files match {pattern!r}.", {"matches": 0}
    note = f"\n... [stopped at {max_results} matches]" if len(matches) >= max_results else ""
    return _truncate("\n".join(matches) + note), {"matches": len(matches)}


def _remember(args: dict) -> tuple[str, dict]:
    content = str(args.get("content") or "").strip()
    if not content:
        raise ToolError("content is required")
    if len(content) > 4000:
        raise ToolError("Memory is too long (max 4000 characters)")
    scope = str(args.get("scope") or "workspace").lower()
    if scope not in {"global", "workspace"}:
        scope = "workspace"
    actual = "global" if scope == "global" else str(config.workspace_path())
    mem = db.add_memory(content, actual)
    return f"Remembered ({scope}): {content[:500]}", {"memory": mem}


def _recall(args: dict) -> tuple[str, dict]:
    query = str(args.get("query") or "").strip()
    memories = db.list_memories(str(config.workspace_path()), query=query)
    if not memories:
        return "No matching memories.", {"memories": 0}
    lines = [f"- [{m['scope']}] {m['content']}" for m in memories[:80]]
    return _truncate("Long-term memories:\n" + "\n".join(lines)), {"memories": len(memories)}


def _ask_user(args: dict) -> tuple[str, dict]:
    # The agent loop intercepts this tool and waits for a real UI answer.
    raise ToolError("ask_user must be handled by the agent loop")


def _fetch_url(args: dict) -> tuple[str, dict]:
    url = str(args.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError("url must be a public http(s) URL")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local"):
        raise ToolError("Refusing to fetch local/private addresses")
    max_chars = max(1000, min(int(args.get("max_chars") or 60_000), 100_000))
    try:
        r = requests.get(
            url, timeout=(10, 30), headers={
                "User-Agent": "LocalCode/0.1 (+offline coding agent)",
                "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
            },
        )
        r.raise_for_status()
    except Exception as exc:
        raise ToolError(f"Fetch failed: {exc}")
    ctype = (r.headers.get("content-type") or "").lower()
    text = r.text
    if "html" in ctype:
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = text.strip()[:max_chars]
    return _truncate(f"Fetched {url} ({ctype or 'unknown'}, {len(r.content)} bytes)\n\n{text}"), {
        "url": url, "status": r.status_code, "content_type": ctype, "chars": len(text)
    }


_DISPATCH = {
    "list_files": _list_files,
    "read_file": _read_file,
    "grep": _grep,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "make_dir": _make_dir,
    "delete_path": _delete_path,
    "run_command": _run_command,
    "todo_write": _todo_write,
    "glob": _glob,
    "remember": _remember,
    "recall": _recall,
    "ask_user": _ask_user,
    "fetch_url": _fetch_url,
}


def execute_tool(name: str, args: dict | None) -> tuple[str, dict]:
    if name not in _DISPATCH:
        raise ToolError(f"Unknown tool: {name}. Available: {', '.join(TOOL_NAMES)}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("Tool arguments must be a JSON object.")
    return _DISPATCH[name](args)


def preview_tool(name: str, args: dict | None) -> str:
    """A human-readable preview used in the approval dialog."""
    args = args or {}
    if not isinstance(args, dict):
        return ""
    try:
        if name == "run_command":
            return f"$ {args.get('command', '')}"
        if name in {"write_file", "edit_file"}:
            p = safe_resolve(str(args.get("path") or ""))
            rel = _rel(p)
            old = ""
            if p.exists() and p.is_file():
                try:
                    old = _read_text(p)
                except ToolError:
                    old = ""
            if name == "write_file":
                new = str(args.get("content") or "")
                return make_diff(rel, old, new) or f"(no change to {rel})"
            old_s = str(args.get("old_string") or "")
            new_s = str(args.get("new_string") or "")
            count = old.count(old_s)
            if count and (count == 1 or args.get("replace_all")):
                new = old.replace(old_s, new_s) if args.get("replace_all") else old.replace(old_s, new_s, 1)
                return make_diff(rel, old, new)
            return f"Edit {rel}: replace {count} occurrence(s) of:\n{old_s[:600]}"
        if name == "delete_path":
            p = safe_resolve(str(args.get("path") or ""), must_exist=True)
            if p.is_dir():
                n = sum(1 for _ in p.rglob("*")) if args.get("recursive") else 0
                return f"Delete directory {_rel(p)}" + (f" and {n} item(s) inside" if n else "")
            return f"Delete file {_rel(p)}"
        if name == "make_dir":
            return f"Create directory {_rel(safe_resolve(str(args.get('path') or '')))}"
        if name == "fetch_url":
            return f"Fetch {args.get('url', '')}"
        if name == "remember":
            return f"Remember: {args.get('content', '')[:600]}"
        if name == "ask_user":
            return f"Ask the user: {args.get('question', '')}"
        if name == "glob":
            return f"Find files matching {args.get('pattern', '')}"
    except Exception as exc:
        return f"(preview unavailable: {exc})"
    return json.dumps(args, indent=2, ensure_ascii=False)[:2000]


def tree_level(rel: str = ".") -> dict:
    """One directory level for the workspace file browser."""
    root = safe_resolve(rel, must_exist=True)
    if not root.is_dir():
        raise ToolError("Not a directory")
    entries = []
    try:
        children = sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    except OSError as exc:
        raise ToolError(str(exc))
    for child in children:
        if child.name in IGNORED_FILES:
            continue
        if child.is_dir() and child.name in IGNORED_DIRS:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": _rel(child),
            "type": "dir" if child.is_dir() else "file",
            "size": stat.st_size,
        })
        if len(entries) >= 500:
            break
    return {"path": _rel(root), "entries": entries}


def read_preview(rel: str, max_chars: int = 120_000) -> dict:
    p = safe_resolve(rel, must_exist=True)
    if p.is_dir():
        raise ToolError("Path is a directory")
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError("File is too large to preview")
    text = _read_text(p)
    return {"path": _rel(p), "content": text[:max_chars], "size": size, "truncated": len(text) > max_chars}
