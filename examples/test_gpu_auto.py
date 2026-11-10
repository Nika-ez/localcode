"""Regression checks for automatic GPU offload and GGUF memory estimates.

Run from the project root:
    .venv\\Scripts\\python.exe examples\\test_gpu_auto.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import config, gguf  # noqa: E402
from server.runtime import RuntimeManager  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name + (f" - {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def find_model() -> Path | None:
    for base in (ROOT / "models", ROOT / "dist" / "models"):
        if not base.exists():
            continue
        for path in base.rglob("*.gguf"):
            if "mmproj" not in path.name.lower():
                return path
    return None


def main() -> int:
    print("GGUF profile")
    model = find_model()
    if model:
        profile = gguf.model_profile(model)
        check("reads architecture", bool(profile.get("architecture")), str(profile))
        check("reads layer count", int(profile.get("layers") or 0) > 0, str(profile))
        check("reads context length", int(profile.get("context") or 0) > 0, str(profile))
        check("estimates KV bytes", gguf.kv_bytes_per_token(profile) > 0, str(profile))
    else:
        print("  (no GGUF model installed; skipping profile checks)")

    print("Launch command")
    mgr = RuntimeManager()
    mgr.exe = ROOT / "dist" / "runtime" / "llama" / "llama-server.exe"
    mgr.port = 12345
    mgr.model_path = model or (ROOT / "models" / "missing.gguf")
    mgr.version_info = lambda: {"flavor": "cuda"}  # type: ignore[assignment]
    original_load = config.load_config
    try:
        for layers, expected in ((-1, "auto"), (0, "0"), (12, "12")):
            config.load_config = lambda l=layers: {  # type: ignore[assignment]
                "gpu_layers": l, "context_size": 0, "threads": 0,
            }
            cmd = mgr._cmd()
            check(
                f"gpu_layers={layers} passes -ngl {expected}",
                "-ngl" in cmd and cmd[cmd.index("-ngl") + 1] == expected,
                " ".join(cmd),
            )
        config.load_config = lambda: {"gpu_layers": -1, "context_size": 0, "threads": 0}  # type: ignore[assignment]
        cmd = mgr._cmd()
        check("auto enables --fit", "--fit" in cmd and cmd[cmd.index("--fit") + 1] == "on")
        config.load_config = lambda: {"gpu_layers": 0, "context_size": 0, "threads": 0}  # type: ignore[assignment]
        cmd = mgr._cmd()
        check("CPU-only does not enable --fit", "--fit" not in cmd)
    finally:
        config.load_config = original_load  # type: ignore[assignment]

    print("Config migration")
    original_path = config.CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        try:
            config.CONFIG_PATH = path  # type: ignore[assignment]
            config.clear_cache()
            path.write_text(json.dumps({"gpu_layers": 0}), encoding="utf-8")
            migrated = config.load_config()
            check("old default 0 migrates to Auto", migrated.get("gpu_layers") == -1,
                  str(migrated.get("gpu_layers")))
            check("migration marker is set", migrated.get("gpu_auto_migrated") is True)
            config.clear_cache()
            path.write_text(json.dumps({"gpu_layers": 0, "gpu_auto_migrated": True}),
                            encoding="utf-8")
            explicit = config.load_config()
            check("explicit CPU-only is respected", explicit.get("gpu_layers") == 0,
                  str(explicit.get("gpu_layers")))
        finally:
            config.CONFIG_PATH = original_path  # type: ignore[assignment]
            config.clear_cache()

    print("Startup diagnostics")
    mgr.version_info = lambda: {"flavor": "cpu"}  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "llama.log"
        log.write_text("load_model: loading model 'big.gguf'\n", encoding="utf-8")
        mgr._log_path = log  # type: ignore[assignment]
        message = mgr._startup_error()
        check("silent exit mentions RAM/VRAM", "RAM + VRAM" in message, message)
        log.write_text("ggml_cuda: CUDA error: out of memory\n", encoding="utf-8")
        message = mgr._startup_error()
        check("CUDA OOM gives actionable advice", "Q4_K_M" in message, message)

    print()
    print(f"{PASSES} passed, {len(FAILS)} failed")
    for fail in FAILS:
        print("  -", fail)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
