"""MCP (Model Context Protocol) client manager with stdio transport.

Supports local MCP servers launched with a command (npx, node, python, ...).
Includes a portable Node.js downloader so npx-based servers such as the Roblox
Studio bridge work even when Node is not installed system-wide.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config

MCP_PATH = config.DATA_DIR / "mcp.json"
NODE_DIR = config.RUNTIME_DIR / "node"
NODE_STATE: dict = {"status": "idle", "percent": 0, "message": "", "version": None, "error": None}
NODE_LOCK = threading.RLock()
PROTOCOL_VERSION = "2025-06-18"
MAX_LOG_LINES = 300


class McpError(Exception):
    pass


def _sanitize(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value or "").strip("_")
    return value or "tool"


def _tool_name(server: str, tool: str) -> str:
    return f"mcp__{_sanitize(server)}__{_sanitize(tool)}"


# --------------------------------------------------------------------- presets
PRESETS: list[dict] = [
    {
        "id": "roblox-studio",
        "name": "Roblox Studio",
        "description": "Build, inspect and edit Roblox places from LocalCode. Requires Roblox Studio + the MCP plugin.",
        "command": "npx",
        "args": ["-y", "@chrrxs/robloxstudio-mcp@latest"],
        "env": {},
        "requires_node": True,
        "featured": True,
        "notes": (
            "Install the Studio plugin once with:\n"
            "npx -y @chrrxs/robloxstudio-mcp@latest --install-plugin\n\n"
            "Then open Roblox Studio, open a place, and start this server. "
            "The plugin connects to the local MCP bridge automatically."
        ),
    },
    {
        "id": "filesystem",
        "name": "Filesystem",
        "description": "Read and write files through the official MCP filesystem server.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem@latest", "{workspace}"],
        "env": {},
        "requires_node": True,
        "notes": "The workspace folder is passed automatically.",
    },
    {
        "id": "everything",
        "name": "Everything (test server)",
        "description": "Official MCP demo server with echo, math, long tasks and sample tools.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything@latest"],
        "env": {},
        "requires_node": True,
        "notes": "Great for checking that MCP is wired up correctly.",
    },
    {
        "id": "memory",
        "name": "Memory",
        "description": "Persistent knowledge-graph memory for the agent.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory@latest"],
        "env": {"MEMORY_FILE_PATH": "{data}/mcp-memory.json"},
        "requires_node": True,
    },
    {
        "id": "sequential-thinking",
        "name": "Sequential Thinking",
        "description": "Structured step-by-step reasoning scratchpad.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking@latest"],
        "env": {},
        "requires_node": True,
    },
    {
        "id": "puppeteer",
        "name": "Puppeteer",
        "description": "Control a headless Chrome browser for screenshots and web automation.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer@latest"],
        "env": {},
        "requires_node": True,
    },
    {
        "id": "sqlite",
        "name": "SQLite",
        "description": "Query and modify a SQLite database.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite@latest", "{data}/mcp.db"],
        "env": {},
        "requires_node": True,
    },
    {
        "id": "github",
        "name": "GitHub",
        "description": "Browse repositories, issues and pull requests. Needs a personal access token.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github@latest"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        "requires_node": True,
        "notes": "Paste a GitHub personal access token into the environment value.",
    },
]


def preset_by_id(preset_id: str) -> dict | None:
    return next((p for p in PRESETS if p["id"] == preset_id), None)


# ---------------------------------------------------------------- node runtime
def _node_exe() -> Path | None:
    p = NODE_DIR / "node.exe"
    if p.exists():
        return p
    found = shutil.which("node")
    return Path(found) if found else None


def _npx_cli() -> Path | None:
    p = NODE_DIR / "node_modules" / "npm" / "bin" / "npx-cli.js"
    if p.exists():
        return p
    found = shutil.which("npx")
    if found:
        base = Path(found).resolve().parent
        for candidate in (
            base / "node_modules" / "npm" / "bin" / "npx-cli.js",
            base.parent / "node_modules" / "npm" / "bin" / "npx-cli.js",
        ):
            if candidate.exists():
                return candidate
    return None


def node_status() -> dict:
    node = _node_exe()
    npx = _npx_cli()
    version = None
    if node:
        try:
            res = subprocess.run(
                [str(node), "--version"], capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0:
                version = (res.stdout or "").strip()
        except Exception:
            pass
    with NODE_LOCK:
        state = dict(NODE_STATE)
    state.update({
        "installed": bool(node and npx),
        "node_path": str(node) if node else None,
        "npx_path": str(npx) if npx else None,
        "version": version or state.get("version"),
        "portable": (NODE_DIR / "node.exe").exists(),
    })
    return state


def start_node_install() -> dict:
    with NODE_LOCK:
        if NODE_STATE.get("status") == "working":
            return dict(NODE_STATE)
        NODE_STATE.update({
            "status": "working", "percent": 0, "message": "Finding the latest Node.js LTS release…",
            "error": None,
        })
    threading.Thread(target=_node_install_worker, daemon=True, name="node-install").start()
    return node_status()


def _node_install_worker() -> None:
    try:
        NODE_DIR.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get("https://nodejs.org/dist/index.json", timeout=30,
                         headers={"User-Agent": "LocalCode/0.1"})
        r.raise_for_status()
        releases = r.json()
        release = next(
            (x for x in releases if x.get("lts") and any(
                (f.get("name", "") if isinstance(f, dict) else str(f)).endswith("win-x64-zip")
                for f in x.get("files", [])
            )),
            None,
        )
        if not release:
            raise McpError("Could not find a Node.js Windows x64 LTS build.")
        version = release["version"]
        url = f"https://nodejs.org/dist/{version}/node-{version}-win-x64.zip"
        zip_path = config.RUNTIME_DIR / f"node-{version}-win-x64.zip"
        with NODE_LOCK:
            NODE_STATE.update(percent=8, message=f"Downloading Node.js {version}…", version=version)
        with requests.get(url, stream=True, timeout=(20, 300),
                          headers={"User-Agent": "LocalCode/0.1"}) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = time.time()
            with open(zip_path, "wb") as fh:
                for chunk in resp.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 0.3:
                        pct = 8 + int(done / total * 72) if total else 40
                        with NODE_LOCK:
                            NODE_STATE.update(
                                percent=min(80, pct),
                                message=f"Downloading Node.js {version}… {config.human_bytes(done)}",
                            )
                        last = time.time()
        with NODE_LOCK:
            NODE_STATE.update(percent=84, message="Extracting Node.js…")
        tmp = config.RUNTIME_DIR / "node-extract"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        source = roots[0] if roots else tmp
        if NODE_DIR.exists():
            shutil.rmtree(NODE_DIR, ignore_errors=True)
        shutil.move(str(source), str(NODE_DIR))
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            zip_path.unlink()
        except OSError:
            pass
        node = NODE_DIR / "node.exe"
        if not node.exists():
            raise McpError("Node.js extraction did not produce node.exe")
        with NODE_LOCK:
            NODE_STATE.update(
                status="complete", percent=100, version=version,
                message=f"Node.js {version} installed",
            )
    except Exception as exc:
        with NODE_LOCK:
            NODE_STATE.update(status="error", error=str(exc), message=f"Node.js install failed: {exc}")


def _resolve_command(command: str, args: list[str]) -> list[str]:
    command = str(command or "").strip()
    if not command:
        raise McpError("MCP server command is empty.")
    base = Path(command).name.lower()
    if base in {"npx", "npx.cmd", "npx.exe"}:
        node = _node_exe()
        npx = _npx_cli()
        if not node or not npx:
            raise McpError(
                "Node.js is not installed. Open MCP → Node.js and press Install, "
                "or install Node.js from nodejs.org."
            )
        return [str(node), str(npx), *args]
    if base in {"node", "node.exe"}:
        node = _node_exe()
        if not node:
            raise McpError("Node.js is not installed. Install it in MCP → Node.js.")
        return [str(node), *args]
    if base in {"python", "python.exe", "py"}:
        import sys
        return [sys.executable, *args]
    return [command, *args]


# -------------------------------------------------------------------- client
class McpServer:
    def __init__(self, name: str, cfg: dict) -> None:
        self.name = name
        self.cfg = dict(cfg)
        self.proc: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending: dict[int, dict] = {}
        self.next_id = 1
        self.reader: threading.Thread | None = None
        self.status = "stopped"
        self.error: str | None = None
        self.tools: list[dict] = []
        self.logs: list[str] = []
        self.started_at: float | None = None
        self._stopping = False
        self._tool_map: dict[str, dict] = {}

    # ------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 120) -> dict:
        with self.start_lock:
            with self.lock:
                if self.proc and self.proc.poll() is None and self.status == "ready":
                    return self.public()
            self.stop(quiet=True)
            with self.lock:
                self._stopping = False
                self.error = None
                self.status = "starting"
            command = str(self.cfg.get("command") or "")
            raw_args = self.cfg.get("args") or []
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = [raw_args]
            args = [str(a) for a in raw_args]
            env = os.environ.copy()
            # npx and package bin shims call `node` by name. The portable Node
            # build is not on PATH, so put it first for the whole process tree.
            node_exe = _node_exe()
            if node_exe:
                env["PATH"] = str(Path(node_exe).parent) + os.pathsep + env.get("PATH", "")
            for key, value in (self.cfg.get("env") or {}).items():
                if value is not None:
                    env[str(key)] = str(value)
            env.setdefault("PYTHONUNBUFFERED", "1")
            env.setdefault("NO_COLOR", "1")
            cwd = str(self.cfg.get("cwd") or config.workspace_path())
            try:
                argv = _resolve_command(command, args)
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
            except Exception as exc:
                with self.lock:
                    self.status = "error"
                    self.error = str(exc)
                self._log(f"failed to start: {exc}")
                return self.public()
            with self.lock:
                self.proc = proc
                self.started_at = time.time()
                self.status = "initializing"
            self.reader = threading.Thread(target=self._read_loop, daemon=True,
                                           name=f"mcp-{self.name}")
            self.reader.start()
            try:
                result = self._request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "roots": {"listChanged": True},
                        "sampling": {},
                    },
                    "clientInfo": {"name": "LocalCode", "version": config.APP_VERSION},
                }, timeout=timeout)
                self._notify("notifications/initialized", {})
                self.server_info = result.get("serverInfo") or {}
                self._refresh_tools(timeout=timeout)
                with self.lock:
                    self.status = "ready"
                    self.error = None
                self._log("connected")
            except Exception as exc:
                with self.lock:
                    self.status = "error"
                    self.error = str(exc)
                self._log(f"initialization failed: {exc}")
                self._terminate()
            return self.public()

    def stop(self, quiet: bool = False) -> dict:
        with self.lock:
            self._stopping = True
        self._terminate()
        with self.lock:
            self.status = "stopped"
            self.tools = []
            self._tool_map = {}
            if not quiet:
                self.error = None
        return self.public()

    def _terminate(self) -> None:
        with self.lock:
            proc = self.proc
            self.proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for item in list(self.pending.values()):
            item["error"] = "MCP server stopped"
            item["event"].set()
        self.pending.clear()

    def _read_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        while True:
            try:
                line = proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                self._log(line[:1000])
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self._log(line[:1000])
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lock:
                    pending = self.pending.pop(int(msg["id"]), None)
                if pending:
                    pending["result"] = msg.get("result")
                    pending["error"] = msg.get("error")
                    pending["event"].set()
                continue
            method = msg.get("method") or ""
            if method == "notifications/tools/list_changed":
                threading.Thread(target=self._safe_refresh, daemon=True).start()
            elif method == "notifications/message":
                params = msg.get("params") or {}
                self._log(str(params.get("data") or params)[:1000])
            elif method:
                self._log(f"notification: {method}")
        code = proc.poll() if proc else None
        with self.lock:
            if not self._stopping:
                self.status = "error"
                self.error = f"MCP server exited (code {code})"
                self._log(self.error)
        for item in list(self.pending.values()):
            item["error"] = "MCP server exited"
            item["event"].set()
        self.pending.clear()

    def _log(self, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        self.logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        if len(self.logs) > MAX_LOG_LINES:
            self.logs = self.logs[-MAX_LOG_LINES:]

    # ---------------------------------------------------------------- protocol
    def _send(self, obj: dict) -> None:
        proc = self.proc
        if not proc or not proc.stdin:
            raise McpError("MCP server is not running")
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        with self.write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except Exception as exc:
                raise McpError(f"Could not write to MCP server: {exc}") from exc

    def _request(self, method: str, params: dict | None, timeout: float = 120):
        with self.lock:
            req_id = self.next_id
            self.next_id += 1
            item = {"event": threading.Event(), "result": None, "error": None}
            self.pending[req_id] = item
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                        "params": params or {}})
        except Exception:
            with self.lock:
                self.pending.pop(req_id, None)
            raise
        if not item["event"].wait(timeout):
            with self.lock:
                self.pending.pop(req_id, None)
            raise McpError(f"MCP request timed out: {method}")
        if item["error"]:
            err = item["error"]
            if isinstance(err, dict):
                raise McpError(str(err.get("message") or err))
            raise McpError(str(err))
        return item["result"] or {}

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _refresh_tools(self, timeout: float = 60) -> None:
        tools: list[dict] = []
        cursor = None
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=timeout)
            for tool in result.get("tools") or []:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                tools.append({
                    "name": tool["name"],
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("inputSchema") or {},
                    "server": self.name,
                })
            cursor = result.get("nextCursor")
            if not cursor:
                break
        with self.lock:
            self.tools = tools
            self._tool_map = {_tool_name(self.name, t["name"]): t for t in tools}

    def _safe_refresh(self) -> None:
        try:
            self._refresh_tools(timeout=30)
            self._log("tool list refreshed")
        except Exception as exc:
            self._log(f"tool refresh failed: {exc}")

    def call_tool(self, tool_name: str, args: dict | None, timeout: float = 600) -> tuple[str, dict]:
        if self.status != "ready":
            self.start()
        with self.lock:
            tool = self._tool_map.get(tool_name)
        if not tool:
            raise McpError(f"Unknown MCP tool: {tool_name}")
        result = self._request("tools/call", {
            "name": tool["name"],
            "arguments": args or {},
        }, timeout=timeout)
        texts: list[str] = []
        images = 0
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            elif item.get("type") == "image":
                images += 1
                texts.append(f"[image returned by MCP tool, {item.get('mimeType') or 'image'}]")
            elif item.get("type") == "resource":
                res = item.get("resource") or {}
                texts.append(str(res.get("text") or res.get("uri") or "[resource]"))
            else:
                texts.append(json.dumps(item, ensure_ascii=False)[:4000])
        text = "\n".join(t for t in texts if t).strip() or "(no text content)"
        if result.get("isError"):
            raise McpError(text)
        structured = result.get("structuredContent")
        return text, {
            "server": self.name,
            "tool": tool["name"],
            "images": images,
            "structured": structured,
        }

    # ----------------------------------------------------------------- public
    def public(self, include_tools: bool = True) -> dict:
        with self.lock:
            out = {
                "name": self.name,
                "command": self.cfg.get("command"),
                "args": self.cfg.get("args") or [],
                "env": self.cfg.get("env") or {},
                "cwd": self.cfg.get("cwd"),
                "enabled": bool(self.cfg.get("enabled", True)),
                "preset": self.cfg.get("preset"),
                "status": self.status,
                "error": self.error,
                "started_at": self.started_at,
                "server_info": getattr(self, "server_info", {}),
                "tool_count": len(self.tools),
                "logs": self.logs[-80:],
            }
            if include_tools:
                out["tools"] = [
                    {
                        "name": _tool_name(self.name, t["name"]),
                        "original": t["name"],
                        "description": t["description"],
                        "input_schema": t["input_schema"],
                        "risk": mcp_risk(t["name"], t["description"]),
                    }
                    for t in self.tools
                ]
            return out


def mcp_risk(name: str, description: str = "") -> str:
    low = f"{name} {description}".lower()
    if re.search(r"execute|exec|run|command|shell|launch|click|type|press|delete|remove|destroy|write|edit|update|create|set_|insert|publish|upload", low):
        return "shell" if re.search(r"execute|exec|run|command|shell|launch|click|type|press", low) else "write"
    if re.search(r"read|list|get|search|find|fetch|query|describe|inspect|status|health|info|tree|snapshot", low):
        return "safe"
    return "write"


# ------------------------------------------------------------------- manager
class McpManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.servers: dict[str, McpServer] = {}
        self.config: dict = {"servers": {}}
        self.load()

    def load(self) -> None:
        try:
            if MCP_PATH.exists():
                data = json.loads(MCP_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("servers"), dict):
                    self.config = data
        except Exception:
            self.config = {"servers": {}}
        for name, cfg in (self.config.get("servers") or {}).items():
            if name not in self.servers:
                self.servers[name] = McpServer(name, cfg)

    def save(self) -> None:
        MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MCP_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, MCP_PATH)

    def public(self) -> dict:
        with self.lock:
            servers = [s.public() for s in self.servers.values()]
        servers.sort(key=lambda s: (not s.get("enabled"), s.get("name", "").lower()))
        return {
            "servers": servers,
            "presets": PRESETS,
            "node": node_status(),
            "connected": sum(1 for s in servers if s["status"] == "ready"),
            "tools": sum(s["tool_count"] for s in servers),
        }

    def add(self, name: str, cfg: dict) -> dict:
        name = _sanitize(name)
        if not name:
            raise McpError("Server name is required")
        with self.lock:
            if name in self.servers:
                self.servers[name].stop(quiet=True)
            clean = {
                "command": str(cfg.get("command") or "").strip(),
                "args": cfg.get("args") or [],
                "env": cfg.get("env") or {},
                "cwd": cfg.get("cwd") or None,
                "enabled": bool(cfg.get("enabled", True)),
                "preset": cfg.get("preset"),
            }
            if not clean["command"]:
                raise McpError("Server command is required")
            self.config.setdefault("servers", {})[name] = clean
            self.servers[name] = McpServer(name, clean)
            self.save()
        return self.servers[name].public()

    def add_preset(self, preset_id: str, name: str | None = None,
                   overrides: dict | None = None) -> dict:
        preset = preset_by_id(preset_id)
        if not preset:
            raise McpError("Unknown preset")
        workspace = str(config.workspace_path())
        data_dir = str(config.DATA_DIR)
        args = [
            str(a).replace("{workspace}", workspace).replace("{data}", data_dir)
            for a in preset.get("args") or []
        ]
        env = {
            str(k): str(v).replace("{workspace}", workspace).replace("{data}", data_dir)
            for k, v in (preset.get("env") or {}).items()
        }
        if overrides:
            args = overrides.get("args", args)
            env = overrides.get("env", env)
        cfg = {
            "command": preset["command"],
            "args": args,
            "env": env,
            "enabled": True,
            "preset": preset_id,
        }
        return self.add(name or preset["id"], cfg)

    def remove(self, name: str) -> bool:
        with self.lock:
            server = self.servers.pop(name, None)
            if server:
                server.stop(quiet=True)
            existed = (self.config.get("servers") or {}).pop(name, None) is not None
            self.save()
        return bool(server or existed)

    def start(self, name: str) -> dict:
        server = self._get(name)
        return server.start()

    def stop(self, name: str) -> dict:
        return self._get(name).stop()

    def restart(self, name: str) -> dict:
        server = self._get(name)
        server.stop(quiet=True)
        return server.start()

    def _get(self, name: str) -> McpServer:
        with self.lock:
            server = self.servers.get(name)
        if not server:
            raise McpError("MCP server not found")
        return server

    def logs(self, name: str) -> list[str]:
        return self._get(name).logs

    def all_tools(self, ensure: bool = True, timeout: float = 120) -> list[dict]:
        out: list[dict] = []
        with self.lock:
            servers = list(self.servers.values())
        for server in servers:
            if not server.cfg.get("enabled", True):
                continue
            if ensure and server.status != "ready":
                try:
                    server.start(timeout=timeout)
                except Exception:
                    continue
            for tool in server.tools:
                out.append({
                    **tool,
                    "qualified": _tool_name(server.name, tool["name"]),
                    "risk": mcp_risk(tool["name"], tool.get("description", "")),
                })
        return out

    def tool_prompt(self, tools: list[dict] | None = None, max_chars: int = 0) -> str:
        tools = tools if tools is not None else self.all_tools(ensure=False)
        if not tools:
            return ""
        header = [
            "MCP TOOLS (external servers)",
            "These tools are provided by connected MCP servers. Call them with the same <tool> protocol.",
        ]
        lines: list[str] = []
        used = 0
        listed = 0
        budget = max_chars if max_chars > 0 else 10_000_000
        for t in tools:
            schema = t.get("input_schema") or {}
            props = schema.get("properties") or {}
            args = ", ".join(
                f"{k}: {v.get('description') or v.get('type') or 'value'}"
                for k, v in list(props.items())[:12]
            )
            line = (
                f"- {t['qualified']} — {(t.get('description') or 'MCP tool')[:220]} "
                f"(server: {t.get('server')}). Arguments: {args or 'see schema'}"
            )
            if listed and used + len(line) > budget:
                lines.append(f"... [{len(tools) - listed} more MCP tools hidden to save context]")
                break
            lines.append(line)
            used += len(line)
            listed += 1
        if not listed and tools:
            t = tools[0]
            lines.append(f"- {t['qualified']} — {(t.get('description') or 'MCP tool')[:150]} ...")
        return "\n".join(header + lines)

    def call(self, qualified: str, args: dict | None) -> tuple[str, dict]:
        with self.lock:
            servers = list(self.servers.values())
        for server in servers:
            with server.lock:
                found = qualified in server._tool_map
            if found:
                return server.call_tool(qualified, args)
        raise McpError(f"Unknown MCP tool: {qualified}. The server may be disconnected.")

    def start_enabled_async(self) -> None:
        def worker():
            with self.lock:
                servers = [s for s in self.servers.values() if s.cfg.get("enabled", True)]
            for server in servers:
                try:
                    server.start(timeout=120)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True, name="mcp-autostart").start()


mcp = McpManager()


# Module-level convenience wrappers used by the agent loop.
def all_tools(ensure: bool = True, timeout: float = 120) -> list[dict]:
    return mcp.all_tools(ensure=ensure, timeout=timeout)


def tool_prompt(tools: list[dict] | None = None, max_chars: int = 0) -> str:
    return mcp.tool_prompt(tools, max_chars=max_chars)


def call(qualified: str, args: dict | None = None) -> tuple[str, dict]:
    return mcp.call(qualified, args)
