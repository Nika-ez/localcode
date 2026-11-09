"""Managed llama.cpp runtime: download, install and serve GGUF models."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import requests

from . import config

GITHUB_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
UA = {"User-Agent": "LocalCode/0.1 (+https://localcode.app)"}


class RuntimeErrorLC(Exception):
    pass


def _tail(path: Path, lines: int = 80) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 128_000))
            data = fh.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception:
        return ""


class RuntimeManager:
    def __init__(self) -> None:
        self.dir = config.RUNTIME_DIR / "llama"
        self.exe = self.dir / "llama-server.exe"
        self.downloads = config.RUNTIME_DIR / "downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.model_path: Path | None = None
        self.model_id: str | None = None
        self.model_name: str | None = None
        self.model_context: int | None = None
        self.context_size: int | None = None
        self.gpu_layers: int = 0
        self.mmproj_path: Path | None = None
        self.loading = False
        self.ready = False
        self.error: str | None = None
        self.started_at: float | None = None
        self._log_path = config.LOGS_DIR / "llama-server.log"
        self._log_handle = None
        self._monitor: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._install_thread: threading.Thread | None = None
        self.install_state: dict = {"status": "idle"}
        self._last_health = 0.0
        atexit.register(self.stop)

    # ------------------------------------------------------------------ state
    def version_info(self) -> dict:
        try:
            return json.loads((self.dir / "version.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def installed(self) -> bool:
        return self.exe.exists()

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.port else ""

    def status(self, include_logs: bool = False) -> dict:
        with self.lock:
            info = self.version_info()
            st = {
                "installed": self.installed(),
                "exe": str(self.exe),
                "version": info.get("tag") or info.get("version"),
                "flavor": info.get("flavor"),
                "running": self.running(),
                "ready": self.ready,
                "loading": self.loading,
                "error": self.error,
                "port": self.port,
                "base_url": self.base_url(),
                "model_id": self.model_id,
                "model_name": self.model_name,
                "model_path": str(self.model_path) if self.model_path else None,
                "model_context": self.model_context,
                "context_size": self.context_size,
                "gpu_layers": self.gpu_layers,
                "mmproj_path": str(self.mmproj_path) if self.mmproj_path else None,
                "vision": bool(self.mmproj_path),
                "started_at": self.started_at,
                "install": dict(self.install_state),
            }
        if include_logs:
            st["logs"] = _tail(self._log_path, 120)
        return st

    # ---------------------------------------------------------------- install
    def start_install(self, flavor: str = "auto") -> dict:
        with self.lock:
            if self._install_thread and self._install_thread.is_alive():
                raise RuntimeErrorLC("Runtime install is already running")
            if flavor == "auto":
                flavor = self._auto_flavor()
            if flavor not in {"cpu", "vulkan", "cuda"}:
                raise RuntimeErrorLC("Unknown runtime flavor")
            self.install_state = {
                "status": "working", "phase": "starting", "percent": 0,
                "message": "Preparing llama.cpp runtime…", "flavor": flavor,
                "bytes": 0, "total": 0, "error": None,
            }
            self._install_thread = threading.Thread(
                target=self._install_worker, args=(flavor,), daemon=True, name="runtime-install"
            )
            self._install_thread.start()
            return dict(self.install_state)

    def _auto_flavor(self) -> str:
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0 and res.stdout.strip():
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def _compute_cap(self) -> float | None:
        """NVIDIA compute capability of the first GPU (e.g. 8.6, 12.0), or None."""
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = (res.stdout or "").strip()
            if not out:
                return None
            first = out.splitlines()[0].strip().split(",")[0].strip()
            return float(first)
        except Exception:
            return None

    def _is_blackwell(self) -> bool:
        """True for RTX 50-series (Blackwell, sm_120)."""
        cap = self._compute_cap()
        if cap is not None:
            return cap >= 12.0
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            name = (res.stdout or "").strip().lower()
            return any(m in name for m in ("rtx 50", "rtx 5060", "rtx 5070",
                                           "rtx 5080", "rtx 5090", "blackwell"))
        except Exception:
            return False

    def _release_assets(self) -> tuple[str, dict[str, str], int]:
        cache_path = config.RUNTIME_DIR / "release.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if time.time() - cached.get("ts", 0) < 3600 and cached.get("assets"):
                    return cached["tag"], cached["assets"], cached["sizes"]
            except Exception:
                pass
        r = requests.get(GITHUB_RELEASES, params={"per_page": 8}, headers=UA, timeout=30)
        r.raise_for_status()
        releases = r.json()
        for rel in releases:
            tag = rel.get("tag_name", "")
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            sizes = {a["name"]: a.get("size", 0) for a in rel.get("assets", [])}
            if any(n.startswith(f"llama-{tag}-bin-win-cpu-x64") for n in assets):
                cache_path.write_text(
                    json.dumps({"ts": time.time(), "tag": tag, "assets": assets, "sizes": sizes}),
                    encoding="utf-8",
                )
                return tag, assets, sizes
        raise RuntimeErrorLC("Could not find a llama.cpp Windows build. Check your internet connection.")

    def _download_file(self, url: str, dest: Path, total_hint: int, progress_cb) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        existing = part.stat().st_size if part.exists() else 0
        headers = dict(UA)
        if existing:
            headers["Range"] = f"bytes={existing}-"
        with requests.get(url, headers=headers, stream=True, timeout=(15, 120)) as r:
            if existing and r.status_code == 200:
                existing = 0
                part.unlink(missing_ok=True)
            elif existing and r.status_code == 416:
                part.replace(dest)
                return
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0) + existing
            if total_hint and not total:
                total = total_hint
            mode = "ab" if existing and r.status_code == 206 else "wb"
            with open(part, mode) as fh:
                downloaded = existing
                last = time.time()
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last > 0.2:
                        progress_cb(downloaded, total)
                        last = now
            progress_cb(part.stat().st_size, total or part.stat().st_size)
        part.replace(dest)

    def _install_worker(self, flavor: str) -> None:
        try:
            self.install_state.update(phase="checking", message="Finding latest llama.cpp build…", percent=1)
            tag, assets, sizes = self._release_assets()
            if flavor == "cpu":
                names = [f"llama-{tag}-bin-win-cpu-x64.zip"]
            elif flavor == "vulkan":
                names = [f"llama-{tag}-bin-win-vulkan-x64.zip"]
            else:
                # RTX 50-series (Blackwell, sm_120) is not compiled into the CUDA 12.4
                # build; it needs the newer CUDA 13.5 build to actually use the GPU.
                blackwell = self._is_blackwell()
                cuda_ver = "13.5" if blackwell else "12.4"
                names = [
                    f"llama-{tag}-bin-win-cuda-{cuda_ver}-x64.zip",
                    f"cudart-llama-bin-win-cuda-{cuda_ver}-x64.zip",
                ]
                if names[0] not in assets and cuda_ver == "13.5":
                    # Older releases may not ship a 13.5 build; fall back to 12.4.
                    cuda_ver = "12.4"
                    names = [
                        f"llama-{tag}-bin-win-cuda-12.4-x64.zip",
                        "cudart-llama-bin-win-cuda-12.4-x64.zip",
                    ]
                self.install_state["message"] = (
                    f"Using CUDA {cuda_ver} build"
                    + (" (Blackwell sm_120 detected)" if cuda_ver == "13.5" else "")
                )
            for name in names:
                if name not in assets:
                    raise RuntimeErrorLC(f"Build asset not found: {name}")
            total = sum(int(sizes.get(n, 0)) for n in names) or 1
            done_before = 0
            downloaded_paths: list[Path] = []
            for name in names:
                dest = self.downloads / name
                if dest.exists() and dest.stat().st_size >= int(sizes.get(name, 0) or 0) > 0:
                    done_before += dest.stat().st_size
                    downloaded_paths.append(dest)
                    continue

                def cb(done: int, tot: int, _before=done_before, _name=name):
                    overall = _before + done
                    pct = min(92, max(2, int(overall / total * 92)))
                    self.install_state.update(
                        phase="download", percent=pct, bytes=overall, total=total,
                        message=f"Downloading {_name}…",
                    )

                self._download_file(assets[name], dest, int(sizes.get(name, 0) or 0), cb)
                done_before += dest.stat().st_size
                downloaded_paths.append(dest)

            self.install_state.update(phase="extract", percent=94, message="Extracting llama.cpp…")
            self._stop_server_for_install()
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = config.RUNTIME_DIR / "extract-tmp"
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            # Clear the previous install so a different flavor's backend DLLs
            # (e.g. ggml-vulkan.dll after switching to CPU) can't linger and get
            # picked up by the wrong binary.
            for old in list(self.dir.iterdir()):
                if old.is_file():
                    try:
                        old.unlink()
                    except OSError:
                        pass
            for archive in downloaded_paths:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(tmp)
            moved = 0
            for root, _dirs, files in os.walk(tmp):
                for name in files:
                    src = Path(root) / name
                    dst = self.dir / name
                    if src.resolve() == dst.resolve():
                        continue
                    if dst.exists():
                        try:
                            dst.unlink()
                        except OSError:
                            pass
                    shutil.move(str(src), str(dst))
                    moved += 1
            shutil.rmtree(tmp, ignore_errors=True)
            if not self.exe.exists():
                raise RuntimeErrorLC("llama-server.exe was not found after extraction")
            (self.dir / "version.json").write_text(
                json.dumps({
                    "tag": tag, "flavor": flavor, "installed_at": config.now(),
                    "files": moved,
                }, indent=2),
                encoding="utf-8",
            )
            for archive in downloaded_paths:
                try:
                    archive.unlink()
                except OSError:
                    pass
            self.install_state = {
                "status": "complete", "phase": "done", "percent": 100,
                "message": f"llama.cpp {tag} installed", "flavor": flavor,
                "bytes": total, "total": total, "error": None,
            }
        except Exception as exc:
            self.install_state = {
                "status": "error", "phase": "error", "percent": 0,
                "message": "Runtime install failed", "flavor": flavor,
                "error": str(exc), "bytes": 0, "total": 0,
            }

    def _stop_server_for_install(self) -> None:
        if self.running():
            self.stop()

    # ------------------------------------------------------------------ serve
    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _auto_context(self) -> int:
        """Pick a safe context size for the machine, not a hard conversation limit."""
        try:
            ram_gb = (config.system_info().get("ram") or 0) / (1024 ** 3)
        except Exception:
            ram_gb = 8
        model_gb = 0.0
        try:
            if self.model_path and self.model_path.exists():
                model_gb = self.model_path.stat().st_size / (1024 ** 3)
        except Exception:
            pass
        # Leave headroom for the OS, the model and the KV cache.
        if ram_gb <= 0:
            ctx = 8192
        elif ram_gb <= 9:
            ctx = 8192 if model_gb > 2.5 else 16384
        elif ram_gb <= 16:
            ctx = 16384 if model_gb > 6 else 32768
        elif ram_gb <= 32:
            ctx = 32768
        else:
            ctx = 65536
        if self.model_context:
            ctx = min(ctx, int(self.model_context))
        return max(2048, ctx)

    def _cmd(self) -> list[str]:
        cfg = config.load_config()
        threads = int(cfg.get("threads") or 0)
        if threads <= 0:
            # Use every logical core (up to 8): faster prompt processing and
            # generation on small machines, including 2-core/4-thread laptops.
            threads = max(1, min(8, os.cpu_count() or 4))
        flavor = self.version_info().get("flavor")
        gpu_layers = int(cfg.get("gpu_layers") or 0)
        if flavor == "cpu":
            # A pure-CPU build has no GPU backend; never pass -ngl.
            gpu_layers = 0
        self.gpu_layers = gpu_layers
        configured = int(cfg.get("context_size") or 0)
        ctx = configured if configured > 0 else self._auto_context()
        ctx = max(2048, min(ctx, 131072))
        if self.model_context:
            ctx = min(ctx, int(self.model_context))
        self.context_size = ctx
        cmd = [
            str(self.exe),
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--alias", "local",
            "-c", str(ctx),
            "-t", str(threads),
            "-ngl", str(gpu_layers),
            "--parallel", "1",
            "--no-webui",
            "--jinja",
        ]
        # Vision projector, when the model folder includes one.
        if self.model_path:
            parent = self.model_path.parent
            candidates = sorted(parent.glob("mmproj*.gguf"))
            if candidates:
                self.mmproj_path = candidates[0]
                cmd += ["--mmproj", str(self.mmproj_path)]
        return cmd

    def start_async(self, model_path: str | Path, model_id: str, model_name: str,
                    model_context: int | None = None) -> dict:
        with self.lock:
            if not self.installed():
                raise RuntimeErrorLC("The local engine is not installed yet. Install it in Models → Engine.")
            p = Path(model_path)
            if not p.exists():
                raise RuntimeErrorLC(f"Model file not found: {p}")
            # Safety net only: refuse models that can never work on this machine.
            # (The model is memory-mapped, so slightly-big models still run via swap;
            # this just blocks absurd loads before they thrash the OS.)
            total = int(config.system_info().get("ram") or 0)
            model_bytes = p.stat().st_size
            if total and model_bytes > total * 0.9:
                raise RuntimeErrorLC(
                    f"This model is {model_bytes / 1e9:.1f} GB but the machine only has "
                    f"{total / 1e9:.1f} GB RAM. Choose a smaller model or quantization."
                )
            if self.running() and self.model_id == model_id and self.ready:
                return self.status()
            self.stop()
            self.model_path = p
            self.model_id = model_id
            self.model_name = model_name
            self.model_context = int(model_context) if model_context else None
            self.mmproj_path = None
            self.error = None
            self.ready = False
            self.loading = True
            self.started_at = time.time()
            self.port = self._free_port()
            self._monitor_stop.clear()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._log_handle = open(self._log_path, "a", encoding="utf-8", buffering=1)
                self._log_handle.write(f"\n\n===== {config.now()} starting {model_name} =====\n")
                self._log_handle.flush()
            except Exception:
                self._log_handle = None
            cmd = self._cmd()
            if self._log_handle:
                self._log_handle.write(" ".join(cmd) + "\n")
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.dir),
                    stdout=self._log_handle or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
            except Exception as exc:
                self.loading = False
                self.error = f"Failed to start llama-server: {exc}"
                raise RuntimeErrorLC(self.error)
            threading.Thread(target=self._wait_ready, daemon=True, name="llama-ready").start()
            self._monitor = threading.Thread(target=self._monitor_worker, daemon=True, name="llama-monitor")
            self._monitor.start()
            return self.status()

    def _wait_ready(self) -> None:
        deadline = time.time() + 1800
        health = self.base_url() + "/health"
        while time.time() < deadline:
            if self.proc is None:
                return
            if self.proc.poll() is not None:
                self.loading = False
                self.ready = False
                self.error = self._startup_error()
                return
            try:
                r = requests.get(health, timeout=3)
                if r.status_code == 200:
                    self.loading = False
                    self.ready = True
                    self.error = None
                    return
            except Exception:
                pass
            time.sleep(1.0)
        self.loading = False
        self.error = "Timed out waiting for the model to load (30 minutes)."
        self.stop()

    def _startup_error(self) -> str:
        tail = _tail(self._log_path, 40)
        low = tail.lower()
        if "vulkan" in low and ("failed to allocate" in low or "out of device memory" in low
                                or "failed to allocate pinned memory" in low):
            return ("The Vulkan build could not allocate GPU memory on this machine. "
                    "Open Models → Engine, install the CPU build, and set GPU layers to 0.")
        if "out of memory" in low or "failed to allocate" in low:
            if "cuda" in low or "cublas" in low:
                return ("CUDA ran out of GPU memory. The model is too large for your GPU's VRAM. "
                        "Lower GPU layers in Settings, or use a smaller model/quantization.")
            return "Not enough memory to load this model. Try a smaller quantization or a smaller model."
        if "no kernel image is available" in low or "sm_120" in low or "compute capability" in low:
            return ("This engine build does not support your NVIDIA GPU architecture. "
                    "Open Models → Engine and reinstall the CUDA engine.")
        if "unknown argument" in low or "invalid argument" in low:
            return "The local engine rejected a launch option. Try reinstalling it."
        if "error loading model" in low or "failed to load model" in low:
            return "The model file could not be loaded. It may be incomplete or unsupported."
        return "The local engine exited while loading the model.\n" + tail[-1200:]

    def _monitor_worker(self) -> None:
        while not self._monitor_stop.wait(3.0):
            with self.lock:
                if self.proc is None:
                    return
                code = self.proc.poll()
                if code is not None:
                    if self.ready or self.loading:
                        self.loading = False
                        self.ready = False
                        self.error = self._startup_error()
                    return

    def stop(self) -> None:
        self._monitor_stop.set()
        with self.lock:
            proc = self.proc
            self.proc = None
            self.ready = False
            self.loading = False
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=8)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self._log_handle:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
                self._log_handle = None
            self.port = None
            self.model_path = None
            self.model_id = None
            self.model_name = None
            self.model_context = None
            self.context_size = None
            self.gpu_layers = 0
            self.mmproj_path = None
            self.started_at = None

    def logs(self, lines: int = 200) -> str:
        return _tail(self._log_path, lines)

    def wait_ready(self, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ready:
                return True
            if self.error:
                return False
            time.sleep(0.5)
        return self.ready


runtime = RuntimeManager()
