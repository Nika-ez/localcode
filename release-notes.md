# LocalCode v1.0.1

A fully offline AI coding agent that runs entirely on your machine. No accounts, no API keys, no cloud.

## What's new in 1.0.1

### GPU acceleration actually turns on now
- **GPU layers defaults to Auto.** LocalCode launches llama.cpp with `-ngl auto` plus `--fit`, so it offloads as many layers as actually fit in VRAM and keeps the rest on the CPU — CPU + GPU together, with a safety margin.
- **Existing installs migrate automatically.** If you had the old default of `0` (CPU only), the next launch switches you to Auto. You can still set `0` for CPU-only or an exact layer count.
- **Fixed the CUDA runtime panel** showing `GPU layers —`. It now shows `Auto (fit to VRAM)`, an exact count, or `CPU only` for both CUDA and Vulkan.
- **Installing a GPU engine sets Auto automatically** instead of leaving the GPU idle.
- **CUDA 13.5 / Blackwell detection** remains in place for RTX 50-series cards.

### Honest model-fit information
- Model lists show **two separate badges**: `fits RAM / tight RAM / too large for RAM` and `fits GPU / GPU partial`.
- The Hugging Face quant picker now shows the GPU badge too, and warns before you download a quant that cannot fit in this PC's RAM.
- The load-time safety net now accounts for **RAM + usable VRAM**, and refuses only models that clearly cannot fit. A too-large load gets a plain-language explanation instead of a cryptic engine exit.
- The engine log now starts with a hardware line (`RAM | GPU | flavor | gpu_layers | context | threads`) so problems are diagnosable at a glance.

### Better errors
- Out-of-memory, Vulkan allocation, CUDA OOM, unsupported architecture, missing projector and silent engine exits now produce specific, actionable messages.
- If the engine dies while reading the model, LocalCode says the model is probably larger than available RAM + VRAM and suggests a smaller quantization.

## Quick start

1. Download `LocalCode.exe` below (single file).
2. Double-click it. The app opens in your browser.
3. On first run, install the local engine (one click) and download a model.
4. Start chatting — the agent reads your files, edits them, and runs commands.

Works on Windows 10/11. Your chats, settings and models stay in a `data/` folder next to the exe.

## Highlights

- **Real local agent** with tools: read/write/edit files, grep, glob, run commands, todo lists, persistent memory, web fetch, MCP servers.
- **Unlimited chats** — automatic context sizing, rolling compaction and persistent summaries mean no context wall.
- **Queue messages** while it works, or press Ctrl+Enter to interrupt and send now.
- **Model downloads** with a live bottom-right popup, RAM/VRAM fit badges, and one-click engine install (CPU, Vulkan, or CUDA).
- **CPU + GPU together** via automatic partial offload, with RTX 50-series (Blackwell) CUDA 13.5 detection.
- **Token usage + hosted cost estimates** on every reply.
- **Slash commands** (`/read`, `/run`, `/model`, …), a Ctrl+K command palette, and a Tools panel.
- **MCP support**, including a one-click Roblox Studio preset.

## Honest limits

- Audio/video files are stored but not transcribed.
- Roblox Studio integration needs Studio installed and open.
- A 27B model at Q8 is roughly 27 GB. On an 8 GB GPU it only runs with partial offload and needs a lot of system RAM; Q4_K_M is the practical choice.

## Verify the download

SHA-256 is published on the release page.

## Run from source

```bat
start.bat
```

Requires Python 3.10+ (tested on 3.14) and Windows. See `README.md` for details and the test suite in `examples/`.
