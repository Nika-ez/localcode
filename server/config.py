"""LocalCode configuration, paths and machine info."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

APP_VERSION = "1.0.0"
APP_NAME = "LocalCode"

# In a PyInstaller one-file build, bundled assets live in _MEIPASS while user
# data, models and the llama.cpp runtime live next to the executable.
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".localcode-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


# Portable installs keep everything next to LocalCode.exe. If that folder is
# read-only (Program Files, a locked USB stick, etc.), fall back to LocalAppData.
APP_DIR = EXE_DIR
if FROZEN and not _writable_dir(APP_DIR):
    _fallback = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "LocalCode"
    if _writable_dir(_fallback):
        APP_DIR = _fallback
DATA_DIR = APP_DIR / "data"
MODELS_DIR = APP_DIR / "models"
RUNTIME_DIR = APP_DIR / "runtime"
STATIC_DIR = (BUNDLE_DIR / "static") if FROZEN else (APP_DIR / "static")
DB_PATH = DATA_DIR / "localcode.db"
CONFIG_PATH = DATA_DIR / "config.json"
LOGS_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
HF_CACHE_DIR = CACHE_DIR / "hf"
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"

for _p in (DATA_DIR, MODELS_DIR, RUNTIME_DIR, LOGS_DIR, CACHE_DIR, HF_CACHE_DIR):
    _p.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_cache: dict | None = None


def _default_workspace() -> str:
    # Keep the whole install self-contained inside the project folder.
    return str(APP_DIR / "workspace")


DEFAULTS: dict = {
    "workspace": _default_workspace(),
    "provider": "managed",  # managed | ollama | openai
    "context_size": 0,  # 0 = automatic, never a hard conversation limit
    "threads": 0,
    "gpu_layers": 0,
    "approval_mode": "ask",  # ask | auto
    "max_steps": 0,  # 0 = unlimited agent steps
    "temperature": 0.2,
    "max_tokens": 4096,
    "hf_token": "",
    "runtime_flavor": "auto",  # auto | cpu | vulkan | cuda
    "auto_load": False,
    "last_model_id": None,
    "external_base_url": "http://127.0.0.1:11434/v1",
    "external_api_key": "",
    "external_model": "",
    "onboarded": False,
    "theme": "dark",
    "port": 8765,
    "show_reasoning": True,
    "compact_threshold": 0.72,
    "auto_compact": True,
    "mcp_auto_start": True,
    "show_costs": True,
    "keep_alive": True,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        cfg = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass
        _cache = cfg
        return cfg


def save_config(updates: dict | None = None) -> dict:
    global _cache
    with _lock:
        cfg = dict(load_config())
        if updates:
            for key, value in updates.items():
                if key in DEFAULTS:
                    cfg[key] = value
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        _cache = cfg
        return cfg


def get(key: str, default=None):
    return load_config().get(key, default)


def clear_cache() -> None:
    """Forget the cached config so the next read loads from disk."""
    global _cache
    with _lock:
        _cache = None


def workspace_path() -> Path:
    cfg = load_config()
    raw = cfg.get("workspace") or _default_workspace()
    p = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def set_workspace(path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    p.mkdir(parents=True, exist_ok=True)
    if not p.is_dir():
        raise ValueError("Workspace path is not a directory")
    save_config({"workspace": str(p)})
    return p


def safe_slug(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-._")
    return (value[:max_len] or "model").lower()


def human_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def disk_info() -> dict:
    try:
        usage = shutil.disk_usage(APP_DIR)
        return {"total": usage.total, "free": usage.free, "used": usage.used}
    except Exception:
        return {"total": 0, "free": 0, "used": 0}


def dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _total_ram() -> int:
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys)
        except Exception:
            return 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
    except Exception:
        return 0


def free_ram() -> int:
    """Available physical memory in bytes (0 if unknown)."""
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullAvailPhys)
        except Exception:
            return 0
    try:
        import resource  # POSIX
        return resource.getpagesize() * os.sysconf("SC_AVPHYS_PAGES")
    except Exception:
        return 0


def _detect_gpus() -> list[dict]:
    if os.name != "nt":
        return []
    # Prefer nvidia-smi for accurate VRAM on NVIDIA cards (WMI AdapterRAM is often
    # wrong or capped). Falls back to WMI for AMD/Intel or when nvidia-smi is absent.
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode == 0 and res.stdout.strip():
            gpus = []
            for line in res.stdout.strip().splitlines():
                name, _, vram_mib = line.rpartition(",")
                vram_mib = vram_mib.strip().split()[0]  # strip "MiB"
                try:
                    vram = int(vram_mib) * 1024 * 1024
                except ValueError:
                    vram = 0
                gpus.append({"name": name.strip(), "vram": vram})
            if gpus:
                return gpus
    except Exception:
        pass
    try:
        cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if res.returncode != 0 or not res.stdout.strip():
            return []
        data = json.loads(res.stdout)
        if isinstance(data, dict):
            data = [data]
        gpus = []
        for g in data:
            name = str(g.get("Name") or "GPU")
            vram = int(g.get("AdapterRAM") or 0)
            gpus.append({"name": name, "vram": vram})
        return gpus
    except Exception:
        return []


_sys_cache: dict | None = None


def system_info() -> dict:
    global _sys_cache
    if _sys_cache is None:
        gpus = _detect_gpus()
        _sys_cache = {
            "app": APP_NAME,
            "version": APP_VERSION,
            "platform": platform.platform(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "cpu": platform.processor() or platform.machine(),
            "cpu_count": os.cpu_count() or 1,
            "ram": _total_ram(),
            "gpus": gpus,
            "python": platform.python_version(),
            "app_dir": str(APP_DIR),
            "exe_dir": str(EXE_DIR),
            "data_dir": str(DATA_DIR),
            "models_dir": str(MODELS_DIR),
            "runtime_dir": str(RUNTIME_DIR),
            "workspace": str(workspace_path()),
            "disk": disk_info(),
            "model_disk": dir_size(MODELS_DIR),
        }
    return _sys_cache


def refresh_system_info() -> dict:
    global _sys_cache
    _sys_cache = None
    return system_info()
