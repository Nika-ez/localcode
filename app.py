"""LocalCode entry point: serve the local UI and launch an app window."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests

from server import config, db, models
from server.api import app, init
from server.runtime import runtime

LOG = logging.getLogger("localcode")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _localcode_running(port: int) -> bool:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1.2)
        return r.status_code == 200 and (r.json() or {}).get("app") == config.APP_NAME
    except Exception:
        return False


def _fatal(message: str) -> None:
    """Show a startup error even in the windowed (no console) build."""
    LOG.exception("Fatal: %s", message)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"LocalCode could not start.\n\n{message}\n\n"
            f"Log file:\n{config.LOGS_DIR / 'localcode.log'}",
            "LocalCode",
            0x10,
        )
    except Exception:
        pass


def _find_browser() -> str | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    return None


def open_ui(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    browser = _find_browser()
    if browser:
        try:
            config.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([
                browser,
                f"--app={url}",
                "--window-size=1440,920",
                f"--user-data-dir={config.BROWSER_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter",
                "--disable-background-networking",
            ])
            return
        except Exception:
            LOG.exception("Could not launch the app window; falling back to the default browser")
    webbrowser.open(url)


def _autoload_last_model() -> None:
    if not config.get("auto_load"):
        return
    model_id = config.get("last_model_id")
    if not model_id:
        return
    time.sleep(2)
    try:
        item = models.get_installed(model_id)
        if item and runtime.installed() and not runtime.ready:
            LOG.info("Auto-loading last model: %s", item["name"])
            runtime.start_async(item["main_file"], item["id"], item["name"],
                                model_context=item.get("context"))
    except Exception:
        LOG.exception("Auto-load failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalCode — offline AI coding agent")
    parser.add_argument("--port", type=int, default=int(config.get("port") or 8765))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Use the Flask development server")
    args = parser.parse_args()

    # Windows consoles often default to a legacy code page. Never let a log line
    # or the banner crash the app. A windowed PyInstaller build has no streams.
    console_stream = sys.stdout or sys.stderr
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(config.LOGS_DIR / "localcode.log", encoding="utf-8"),
    ]
    if console_stream is not None:
        handlers.insert(0, logging.StreamHandler(console_stream))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("waitress").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    init()
    config.workspace_path()
    LOG.info("%s v%s starting", config.APP_NAME, config.APP_VERSION)

    port = args.port
    if _localcode_running(port):
        LOG.info("LocalCode is already running at http://127.0.0.1:%d", port)
        if not args.no_browser:
            open_ui(port)
        return 0
    if not _port_free(port):
        for candidate in range(port + 1, port + 40):
            if _port_free(candidate):
                port = candidate
                break
        else:
            LOG.error("No free port found near %d", args.port)
            return 1
    if port != int(config.get("port") or 8765):
        config.save_config({"port": port})

    url = f"http://127.0.0.1:{port}"
    if console_stream is not None:
        print()
        print("  +---------------------------------------------+")
        print("  |  LocalCode - your offline AI coding agent   |")
        print("  +---------------------------------------------+")
        print(f"  UI:      {url}")
        print(f"  Data:    {config.DATA_DIR}")
        print(f"  Models:  {config.MODELS_DIR}")
        print("  Keep this window open. Press Ctrl+C to quit.")
        print()
    LOG.info("UI ready at %s", url)

    if not args.no_browser:
        threading.Timer(1.1, lambda: open_ui(port)).start()
    threading.Thread(target=_autoload_last_model, daemon=True, name="autoload-last").start()

    try:
        if args.dev:
            app.run(host=args.host, port=port, threaded=True, debug=False, use_reloader=False)
        else:
            from waitress import serve
            serve(
                app,
                host=args.host,
                port=port,
                threads=32,
                channel_timeout=86400,
                cleanup_interval=30,
                ident="LocalCode",
            )
    except KeyboardInterrupt:
        LOG.info("Shutting down")
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # A windowed build has no console to print a traceback to.
        if getattr(sys, "frozen", False):
            try:
                logging.basicConfig(
                    level=logging.INFO,
                    handlers=[logging.FileHandler(
                        config.LOGS_DIR / "localcode.log", encoding="utf-8")],
                )
            except Exception:
                pass
            _fatal(str(exc))
            raise SystemExit(1)
        raise
