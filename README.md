# LocalCode

**Your code. Your machine. Your model.**

LocalCode is a private, offline AI coding agent for Windows. It runs a real local
language model through [llama.cpp](https://github.com/ggerganov/llama.cpp), edits
files in your workspace, runs commands with your approval, talks to MCP servers
(including Roblox Studio), and keeps every chat on your disk. Nothing is sent to
a cloud unless you explicitly configure a hosted OpenAI-compatible endpoint.

The whole app ships as a single `LocalCode.exe`. Models, the llama.cpp engine,
and portable Node.js are downloaded on first use and then work with no internet.

---

## Quick start (release build)

1. Put `LocalCode.exe` in a folder you can write to (for example
   `C:\Users\<you>\LocalCode`). If the folder is read-only, LocalCode falls back
   to `%LOCALAPPDATA%\LocalCode`.
2. Double-click `LocalCode.exe`. A dedicated app window opens.
3. Open **Models** in the sidebar, pick a model that fits your RAM, and download it.
   The first launch also offers to install the llama.cpp engine automatically.
4. Press **Load** on the installed model, wait for the green "ready" state, and start chatting.

No console window appears. To close LocalCode, use **Settings → Quit LocalCode**
(or close the app window and use Task Manager if needed).

### Hardware guidance

| RAM | Suggested model size |
| --- | --- |
| 8 GB | 1.5B–3B (Q4) |
| 16 GB | 7B–8B (Q4) |
| 32 GB | 14B (Q4) or 8B with a large context |
| 64 GB+ | 27B–32B (Q4) |

CPU inference works without a GPU. A discrete GPU is used automatically when the
bundled llama.cpp build supports it, but CPU-only machines are fully supported.
The model list marks each entry as **great / tight / no** based on detected RAM.

---

## Features

### Real local agent
- Streaming chat with tool calling (prompt-based `<tool>` protocol plus native
  `tool_calls`), reasoning display, and multi-step runs.
- Built-in tools: `list_files`, `read_file`, `grep`, `glob`, `write_file`,
  `edit_file`, `make_dir`, `delete_path`, `run_command`, `todo_write`,
  `remember`, `recall`, `ask_user`, `fetch_url`.
- Approval modes: **ask** before writes/shell, **auto** for trusted work, or
  deny individual calls in the UI. Destructive actions are clearly marked.
- The agent keeps working in the background if you close the tab; reopening the
  chat reconnects to the live run.
- **Queue messages** while the agent works, or press **Ctrl+Enter** to interrupt
  and send immediately.
- Hard loop breaker: an identical tool call is executed at most three times; the
  fourth attempt stops the run with status `loop` so weak models cannot spin forever.

### Long chats that never hit a context wall
- Automatic context sizing based on your RAM and the model.
- Rolling auto-compaction: older turns are summarised into persistent memory,
  while the full transcript stays saved.
- Persistent session summaries and a workspace memory store (`remember` /
  `recall`).
- Automatic retry with aggressive compaction if the model reports a context overflow.
- Manual **Compact now** from the chat menu.

### Models
- Curated catalog of coding and uncensored coding GGUF models, including
  Qwen2.5/Qwen3 Coder, DeepSeek Coder, Codestral, Gemma, Llama, and abliterated
  community variants.
- Hugging Face repository browser and quant discovery for arbitrary GGUF repos.
- Resumable downloads, progress, cancel/remove, disk usage, and RAM-fit badges.
- Every download shows a live popup in the bottom-right corner (progress, speed,
  ETA, cancel); finished cards stay until you dismiss them.
- **CPU + GPU together**: the Vulkan runtime build offloads model layers to the
  GPU while the CPU keeps running sampling. On weak integrated GPUs plain CPU
  is usually faster, so the default stays CPU — install the Vulkan build from
  Models → Engine to try both.
- Vision models are supported when an `mmproj` file is present (images are sent
  as OpenAI-style `image_url` parts). Audio and video are reported honestly as
  metadata-only unless a compatible model is installed.

### Providers
- **Managed local**: downloads and runs llama.cpp for you.
- **External / OpenAI-compatible**: point LocalCode at Ollama, LM Studio,
  llama.cpp, vLLM, or a hosted API with a base URL, model name, and optional key.
- Token usage is recorded per message and per session, with a typical hosted-API
  cost estimate for comparison.

### MCP (Model Context Protocol)
- Stdio MCP servers managed from the UI: add, start, stop, restart, view logs,
  and inspect discovered tools.
- One-click presets for common servers, including **Roblox Studio**.
- Portable Node.js downloader, so `npx`-based servers work without a system Node install.
- MCP tools are exposed to the model as `mcp__<server>__<tool>` and go through
  the same approval flow.

#### Roblox Studio setup
1. Open **MCP → Add preset → Roblox Studio**.
2. Start the server. LocalCode downloads portable Node on first use.
3. Install the Studio plugin once (from a terminal):
   ```
   npx -y @chrrxs/robloxstudio-mcp@latest --install-plugin
   ```
   Or run the same command from the MCP panel's helper text.
4. Open Roblox Studio with your place, enable the plugin, and keep the server running.
5. Ask LocalCode to inspect or edit your place. Studio must be open for the
   handshake to succeed.

### Attachments
- Text and code files are injected directly.
- PDFs are parsed locally with `pypdf`; scanned PDFs are flagged when no text is found.
- Images are stored and sent to vision models; non-vision models receive a clear note.
- Audio/video files are accepted as metadata placeholders and are not transcribed
  without a dedicated model.

### Private by default
- The server binds to `127.0.0.1` only.
- Chats, settings, attachments, models, logs, and the browser profile live in the
  app folder (or `%LOCALAPPDATA%\LocalCode`).
- No telemetry, no accounts, no cloud dependency.
- The only network access is what you trigger: model/engine/Node downloads,
  optional `fetch_url`, and any hosted provider you configure.

---

## Data locations

For a portable install everything is next to the executable:

```
LocalCode.exe
data/            chats (localcode.db), settings, logs, uploads, browser profile
models/          downloaded GGUF models
runtime/         llama.cpp engine and portable Node.js
workspace/       default working folder for the agent
```

If the executable's folder is read-only, `data/`, `models/`, and `runtime/` are
created under `%LOCALAPPDATA%\LocalCode` instead.

Deleting a chat removes it from the sidebar and database. **Export** produces a
Markdown transcript from the chat menu.

**Settings → Start from scratch** wipes everything — chats, memories, MCP
tools, attachments and settings — and shows the new-user setup again, exactly
like a brand-new install. Optionally also delete downloaded models and the
engine binaries. Downloaded models and the runtime are otherwise kept.

---

## Run from source

Requires Python 3.10+ (tested on 3.14) and Windows 10/11.

```bat
start.bat
```

`start.bat` creates `.venv`, installs `requirements.txt`, and launches the app at
`http://127.0.0.1:8765`. Useful flags:

```bat
.venv\Scripts\python.exe app.py --port 8766 --no-browser --dev
```

- `--no-browser` does not open the app window.
- `--dev` uses the Flask development server (never expose it publicly).
- `--port` selects the local port; if busy, the next free port is used.

### Tests

The deterministic suite uses a mock OpenAI-compatible model and covers chat,
tools, approvals, denials, `ask_user`, shell, native tool calls, MCP, queue and
interrupt steering, stop, reconnect, the loop breaker, compaction, export, and
error paths.

```bat
.venv\Scripts\python.exe examples\mock_llm.py
.venv\Scripts\python.exe app.py --no-browser --port 8765
.venv\Scripts\python.exe examples\e2e_agent.py
.venv\Scripts\python.exe examples\test_attachments.py
```

With a real model installed and loaded:

```bat
.venv\Scripts\python.exe examples\real_model_test.py
```

The test scripts accept `LOCALCODE_BASE` (default `http://127.0.0.1:8765`) and
`LOCALCODE_MOCK` (default `http://127.0.0.1:8899/v1`).

---

## Build the single executable

```bat
build.bat
```

This installs build dependencies, runs PyInstaller with `LocalCode.spec`, and
writes `dist\LocalCode.exe`. The build is windowed (`console=False`) and embeds
the icon and Windows version metadata. The executable is self-contained; it does
not bundle models or the llama.cpp runtime, which are downloaded on demand.

Manual build:

```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.venv\Scripts\pyinstaller.exe --clean --noconfirm LocalCode.spec
```

---

## Limitations (stated honestly)

- **Unlimited context is not physically possible.** LocalCode makes chats
  effectively unbounded through auto-sizing, compaction, persistent summaries,
  and overflow retries. Very old details can be summarised rather than kept verbatim.
- Small models are less reliable at tool use. The loop breaker and strict tool
  prompts reduce failure modes but cannot make a 1.5B model behave like a 70B one.
  Simple chat messages skip the tool protocol entirely, so greetings and questions
  get direct answers instead of stray tool calls.
- Actual vision requires a vision-capable model plus its `mmproj` file. Audio and
  video transcription are not implemented.
- Roblox Studio integration requires Studio to be installed and open; the plugin
  handshake cannot work without it.
- The bundled llama.cpp build is CPU-oriented. GPU acceleration depends on the
  runtime build and your hardware.
- Hosted cost figures are estimates for comparison, not billing statements.

---

## License

MIT. See [LICENSE](LICENSE).

LocalCode is an independent project. It is not affiliated with OpenCode,
llama.cpp, Hugging Face, Roblox, or any model vendor. Model licenses are set by
their respective publishers — check them before redistribution or commercial use.
