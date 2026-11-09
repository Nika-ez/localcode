"""Model capability detection and user-facing capability badges."""
from __future__ import annotations

import re
from pathlib import Path

from . import config
from .runtime import runtime

VISION_FAMILIES = re.compile(
    r"vl|vision|llava|moondream|minicpm-v|pixtral|internvl|gemma-?3|glm-?4.*v|"
    r"deepseek.*vl|qwen.*vl|idefics|paligemma|florence|molmo|smolvlm|ovis",
    re.IGNORECASE,
)
REASONING_FAMILIES = re.compile(
    r"qwen3|deepseek.*r1|reason|thinking|magistral|glm-?4\.5|kimi.*k2|"
    r"deepseek.*v3\.1|deepseek.*v4",
    re.IGNORECASE,
)
AUDIO_FAMILIES = re.compile(r"audio|whisper|qwen.*audio|voxtral|musicgen", re.IGNORECASE)
VIDEO_FAMILIES = re.compile(r"video|qwen.*video|tarsier|video-llava", re.IGNORECASE)


def _has_mmproj(files: list[str] | None) -> bool:
    for f in files or []:
        name = Path(str(f)).name.lower()
        if "mmproj" in name and name.endswith(".gguf"):
            return True
    return False


def detect(name: str | None = None, *, files: list[str] | None = None,
           context: int | None = None, loaded: bool = False,
           external: bool = False, model_id: str | None = None) -> dict:
    name = (name or "").strip()
    mmproj = _has_mmproj(files)
    vision_family = bool(VISION_FAMILIES.search(name))
    reasoning = bool(REASONING_FAMILIES.search(name))
    audio = bool(AUDIO_FAMILIES.search(name)) and not external
    video = bool(VIDEO_FAMILIES.search(name)) and not external
    # Gemma 3 1B has no vision tower; everything else in the family does.
    if re.search(r"gemma-?3.*1b", name, re.IGNORECASE):
        vision_family = False
    image_ready = bool(mmproj and vision_family)
    if external:
        # Hosted vision models generally accept images directly.
        image_ready = vision_family
    ctx = int(context or 0)
    if not ctx:
        cfg = config.get("context_size")
        ctx = int(cfg or 0)
    input_modes = ["text"]
    if image_ready:
        input_modes.append("image")
    if video:
        input_modes.append("video")
    if audio:
        input_modes.append("audio")
    input_modes.append("pdf (text extraction)")
    notes: list[str] = []
    if vision_family and not image_ready:
        notes.append("This model family supports vision, but no mmproj projector is installed. Download the vision projector to enable images.")
    if not mmproj:
        notes.append("PDFs are read by extracting their text. Scanned/image-only PDFs need a vision model.")
    notes.append("Audio and video generation/understanding are not enabled in this build.")
    notes.append("Chats can run indefinitely: older context is compacted into persistent memory automatically.")
    return {
        "model": name or "No model",
        "model_id": model_id,
        "loaded": loaded,
        "external": external,
        "input": input_modes,
        "output": ["text", "tool calls", "code edits", "Markdown"],
        "text": True,
        "image": image_ready,
        "image_family": vision_family,
        "video": video,
        "audio": audio,
        "pdf": True,
        "pdf_scanned": image_ready,
        "tools": True,
        "mcp": True,
        "reasoning": reasoning,
        "context": ctx,
        "mmproj": mmproj,
        "notes": notes,
        "badges": _badges(input_modes, reasoning, True),
    }


def _badges(inputs: list[str], reasoning: bool, tools: bool) -> list[dict]:
    out = [
        {"key": "text", "label": "Text", "on": True},
        {"key": "image", "label": "Images", "on": "image" in inputs},
        {"key": "pdf", "label": "PDF text", "on": True},
        {"key": "audio", "label": "Audio", "on": "audio" in inputs},
        {"key": "video", "label": "Video", "on": "video" in inputs},
        {"key": "tools", "label": "Tools", "on": tools},
        {"key": "mcp", "label": "MCP", "on": True},
        {"key": "reasoning", "label": "Reasoning", "on": reasoning},
    ]
    return out


def for_installed(item: dict) -> dict:
    return detect(
        item.get("name"),
        files=item.get("files"),
        context=item.get("context"),
        loaded=bool(item.get("loaded")),
        model_id=item.get("id"),
    )


def current() -> dict:
    cfg = config.load_config()
    if (cfg.get("provider") or "managed") == "managed":
        st = runtime.status()
        if not st.get("model_name"):
            return detect("No model loaded", context=st.get("context_size"))
        item = None
        try:
            from . import models
            item = models.get_installed(st.get("model_id") or "")
        except Exception:
            item = None
        return detect(
            st.get("model_name"),
            files=(item or {}).get("files"),
            context=st.get("context_size") or (item or {}).get("context"),
            loaded=bool(st.get("ready")),
            model_id=st.get("model_id"),
        )
    name = str(cfg.get("external_model") or "External model")
    return detect(name, external=True, context=cfg.get("context_size"))


def describe(cap: dict) -> str:
    bits = ["text"]
    if cap.get("image"):
        bits.append("images")
    if cap.get("pdf"):
        bits.append("PDF")
    if cap.get("audio"):
        bits.append("audio")
    if cap.get("video"):
        bits.append("video")
    bits.append("tools")
    return ", ".join(bits)
