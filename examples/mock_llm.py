"""Deterministic mock OpenAI-compatible server for LocalCode tests.

Start it, then in LocalCode Settings set:
    Provider: OpenAI-compatible API
    Base URL: http://127.0.0.1:8899/v1
    Model:    mock-coder

It supports streaming text, the <tool>...</tool> protocol, native tool_calls,
reasoning deltas, usage accounting, slow streams (for stop/steer tests), and
non-streaming summaries for compaction.
"""
from __future__ import annotations

import json
import time

from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)
MODEL = "mock-coder"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text") or "") for p in content
            if isinstance(p, dict) and p.get("type") in {"text", "input_text"}
        )
    return str(content or "")


def _last_user(messages) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return _text_of(message.get("content"))
    return ""


def _usage(prompt=128, completion=32):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _sse(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chunks(text: str, size: int = 7):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _stream_text(text: str, *, delay=0.008, reasoning: str = ""):
    if reasoning:
        for piece in _chunks(reasoning, 9):
            yield _sse({"choices": [{"index": 0, "delta": {"reasoning_content": piece}}]})
            time.sleep(delay)
    for piece in _chunks(text):
        yield _sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
        time.sleep(delay)
    yield _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _usage(prompt=max(20, len(text) // 3), completion=max(8, len(text) // 4))})
    yield "data: [DONE]\n\n"


def _stream_native_tool(name: str, arguments: dict):
    call_id = "call_mock_native"
    yield _sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": name, "arguments": ""},
    }]}}]})
    time.sleep(0.02)
    payload = json.dumps(arguments, ensure_ascii=False)
    for piece in _chunks(payload, 5):
        yield _sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": piece},
        }]}}]})
        time.sleep(0.01)
    yield _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": _usage()})
    yield "data: [DONE]\n\n"


def _tool_text(name: str, arguments: dict) -> str:
    return "<tool>\n" + json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False) + "\n</tool>"


def _decide(messages):
    """Return (kind, payload) based on the conversation."""
    last = _last_user(messages)
    low = last.lower()
    tool_result = "[tool result:" in low
    all_text = " ".join(_text_of(m.get("content")) for m in (messages or [])).lower()

    # Deliberately pathological model used to verify the agent's loop breaker.
    if "loop test" in all_text:
        return ("tool", {
            "name": "write_file",
            "arguments": {"path": "loop-output.txt", "content": "looping\n"},
        })

    if "pdf extraction test" in low:
        return ("text", "I can see the PDF text: LocalCode PDF extraction test.")
    if "image attached" in low:
        return ("text", "I received the image attachment note.")
    if tool_result:
        # After a tool result, finish with a short deterministic answer.
        if "mock-output" in low or "write_file" in low:
            return ("text", "Done — I wrote the file and verified the tool result.")
        if "read_file" in low:
            return ("text", "I read the file successfully. The tool result is shown above.")
        if "mcp__everything__echo" in low:
            return ("text", "The MCP echo tool answered successfully.")
        if "ask_user" in low:
            return ("text", "Thanks — I used your answer to continue.")
        if "run_command" in low:
            return ("text", "The command ran successfully.")
        return ("text", "Tool completed. Here is the final answer.")

    if "ask me" in low or low.startswith("ask"):
        return ("tool", {
            "name": "ask_user",
            "arguments": {
                "question": "Which framework should I use for the landing page?",
                "options": ["Plain HTML/CSS", "React", "Vue"],
                "default": "Plain HTML/CSS",
            },
        })
    if "mcp" in low:
        return ("tool", {
            "name": "mcp__everything__echo",
            "arguments": {"message": "hello from LocalCode"},
        })
    if "write" in low or "create file" in low:
        return ("tool", {
            "name": "write_file",
            "arguments": {"path": "mock-output.txt", "content": "hello from the mock model\n"},
        })
    if "shell" in low or "command" in low:
        return ("tool", {
            "name": "run_command",
            "arguments": {"command": "echo mock-shell-ok"},
        })
    if "native" in low:
        return ("native", {"name": "list_files", "arguments": {"path": "."}})
    if "slow" in low or "long" in low:
        return ("slow", "")
    if "reason" in low:
        return ("reason", "Let me think through this carefully. The user wants a demonstration of reasoning output.")
    if "read" in low:
        return ("tool", {
            "name": "read_file",
            "arguments": {"path": "README.md"},
        })
    if "hello" in low or "hi" in low:
        return ("text", "Hello! I am the deterministic mock model. Tool calls, streaming, usage and compaction all work.")
    return ("text", "Mock response: " + (last[:200] or "ready"))


@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [
        {"id": MODEL, "object": "model", "owned_by": "localcode-tests"}
    ]})


@app.post("/v1/chat/completions")
def chat():
    body = request.get_json(force=True, silent=True) or {}
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))

    if not stream:
        prompt = _last_user(messages).lower()
        if "long-term memory" in prompt or "compress" in prompt or "summary" in prompt:
            summary = (
                "Summary: the user built a small project with LocalCode. "
                "The agent read README.md, wrote mock-output.txt, ran a shell command, "
                "and used an MCP echo tool. All requested work is complete."
            )
        else:
            summary = "Mock summary of the conversation."
        return jsonify({
            "id": "chatcmpl-mock", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": summary},
                         "finish_reason": "stop"}],
            "usage": _usage(80, 24),
        })

    kind, payload = _decide(messages)
    if kind == "native":
        return Response(stream_with_context(_stream_native_tool(payload["name"], payload["arguments"])),
                        mimetype="text/event-stream")
    if kind == "tool":
        text = _tool_text(payload["name"], payload["arguments"])
    elif kind == "slow":
        text = " ".join(["streaming"] * 140) + " done."
    elif kind == "reason":
        return Response(stream_with_context(_stream_text(
            "The answer is 42.", reasoning=payload, delay=0.01)), mimetype="text/event-stream")
    else:
        text = payload
    delay = 0.06 if kind == "slow" else 0.006
    return Response(stream_with_context(_stream_text(text, delay=delay)),
                    mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8899, threaded=True, debug=False, use_reloader=False)
