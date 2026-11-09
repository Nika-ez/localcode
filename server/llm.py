"""OpenAI-compatible streaming client for local and external providers."""
from __future__ import annotations

import json
import time
from typing import Iterator

import requests

from . import config
from .runtime import runtime


class LLMError(Exception):
    pass


class ContextOverflow(LLMError):
    """The request exceeded the model's context window."""


def provider_info() -> dict:
    cfg = config.load_config()
    provider = cfg.get("provider") or "managed"
    if provider == "managed":
        if not runtime.ready:
            if runtime.loading:
                raise LLMError("The local model is still loading. Give it a moment and try again.")
            if runtime.error:
                raise LLMError(runtime.error)
            raise LLMError("No local model is loaded. Open Models, download a model, then press Load.")
        return {
            "kind": "managed",
            "base": runtime.base_url() + "/v1",
            "model": "local",
            "key": "",
            "label": runtime.model_name or "Local model",
            "context": runtime.context_size,
            "vision": bool(getattr(runtime, "mmproj_path", None)),
        }
    base = normalize_base(str(cfg.get("external_base_url") or ""))
    model = str(cfg.get("external_model") or "").strip()
    if not model:
        raise LLMError("Set a model name in Settings → Provider for your external endpoint.")
    return {
        "kind": provider,
        "base": base,
        "model": model,
        "key": str(cfg.get("external_api_key") or ""),
        "label": f"{provider}: {model}",
        "context": int(cfg.get("context_size") or 0),
        "vision": False,
    }


def normalize_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise LLMError("Set the API base URL in Settings → Provider.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise LLMError("The API base URL must start with http:// or https://")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def _headers(key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
    return str(content or "")


def estimate_tokens(content) -> int:
    text = _text_of(content)
    if not text:
        return 0
    return max(1, int(len(text) / 3.6))


def _request_payload(model: str, messages: list[dict], stream: bool,
                     max_tokens: int | None, include_usage: bool = True,
                     local: bool = False) -> dict:
    cfg = config.load_config()
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": float(cfg.get("temperature", 0.2)),
        "top_p": 0.92,
    }
    if local:
        # Sampling tuned for small local models: steadier, less repetitive,
        # fewer hallucinations. llama.cpp accepts top_k / min_p / repeat_penalty
        # on every request; hosted OpenAI-style endpoints generally do not.
        payload.update({
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.1,
            "presence_penalty": 0.0,
        })
    mt = int(max_tokens or cfg.get("max_tokens") or 4096)
    if mt > 0:
        payload["max_tokens"] = mt
    if stream and include_usage:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _is_context_error(status: int, detail: str) -> bool:
    low = (detail or "").lower()
    if status not in {400, 413, 422, 500}:
        return False
    return any(token in low for token in (
        "context", "n_ctx", "too long", "too many tokens", "exceeds",
        "maximum context", "prompt is too long", "input is too long",
    ))


def _stream_once(messages: list[dict], stop_event, steer_event,
                 max_tokens: int | None, include_usage: bool) -> Iterator[dict]:
    info = provider_info()
    payload = _request_payload(info["model"], messages, True, max_tokens, include_usage,
                               local=info["kind"] == "managed")
    url = info["base"] + "/chat/completions"
    native: dict[int, dict] = {}
    usage = None
    interrupted = None
    try:
        with requests.post(
            url, json=payload, headers=_headers(info["key"]), stream=True,
            timeout=(15, 1800),
        ) as resp:
            if resp.status_code != 200:
                body = resp.text[:4000]
                try:
                    detail = json.loads(body).get("error", {}).get("message") or body
                except Exception:
                    detail = body
                if _is_context_error(resp.status_code, detail):
                    raise ContextOverflow(detail)
                raise LLMError(f"Provider error {resp.status_code}: {detail}")
            for raw in resp.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    interrupted = "stop"
                    break
                if steer_event is not None and steer_event.is_set():
                    interrupted = "steer"
                    break
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield {"type": "text", "text": content}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield {"type": "reasoning", "text": reasoning}
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    slot = native.setdefault(idx, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                finish = choice.get("finish_reason")
                if finish:
                    yield {"type": "finish", "reason": finish}
    except LLMError:
        raise
    except requests.exceptions.ConnectionError as exc:
        if interrupted:
            return
        raise LLMError(
            "Could not reach the model provider. If you are using the local engine, check that it is running."
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise LLMError("The model provider timed out.") from exc
    except Exception as exc:
        if interrupted:
            return
        raise LLMError(f"Model request failed: {exc}") from exc
    finally:
        if usage:
            yield {"type": "usage", "usage": usage}
        if native:
            yield {"type": "native_tools", "tool_calls": list(native.values())}
        if interrupted:
            yield {"type": "interrupted", "reason": interrupted}


def stream_chat(messages: list[dict], stop_event=None, steer_event=None,
                max_tokens: int | None = None) -> Iterator[dict]:
    """Stream a completion, retrying once without stream_options if needed."""
    emitted = False
    try:
        for event in _stream_once(messages, stop_event, steer_event, max_tokens, True):
            if event.get("type") == "text":
                emitted = True
            yield event
    except LLMError as exc:
        detail = str(exc).lower()
        if not emitted and "stream_options" in detail:
            for event in _stream_once(messages, stop_event, steer_event, max_tokens, False):
                yield event
            return
        raise


def complete_text(messages: list[dict], max_tokens: int = 900) -> str:
    """Non-streaming completion, used for summaries/compaction."""
    info = provider_info()
    payload = _request_payload(info["model"], messages, False, max_tokens,
                               local=info["kind"] == "managed")
    payload["temperature"] = 0.1
    url = info["base"] + "/chat/completions"
    try:
        r = requests.post(url, json=payload, headers=_headers(info["key"]), timeout=(15, 900))
    except requests.exceptions.ConnectionError as exc:
        raise LLMError("Could not reach the model provider.") from exc
    if r.status_code != 200:
        detail = r.text[:1200]
        if _is_context_error(r.status_code, detail):
            raise ContextOverflow(detail)
        raise LLMError(f"Provider error {r.status_code}: {detail}")
    try:
        obj = r.json()
        msg = obj["choices"][0]["message"]
        return (msg.get("content") or msg.get("reasoning_content") or "").strip()
    except Exception as exc:
        raise LLMError("The provider returned an unexpected response.") from exc


def list_models() -> list[str]:
    info = provider_info()
    if info["kind"] == "managed":
        return [info["model"]]
    try:
        r = requests.get(info["base"] + "/models", headers=_headers(info["key"]), timeout=20)
        r.raise_for_status()
        data = r.json().get("data") or []
        return sorted({str(m.get("id")) for m in data if m.get("id")})
    except Exception as exc:
        raise LLMError(f"Could not list models: {exc}") from exc


def probe() -> dict:
    try:
        info = provider_info()
        start = time.time()
        if info["kind"] == "managed":
            r = requests.get(runtime.base_url() + "/health", timeout=5)
            return {"ok": r.status_code == 200, "ms": int((time.time() - start) * 1000),
                    "label": info["label"], "context": info.get("context")}
        r = requests.get(info["base"] + "/models", headers=_headers(info["key"]), timeout=8)
        return {"ok": r.status_code == 200, "ms": int((time.time() - start) * 1000),
                "label": info["label"], "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
