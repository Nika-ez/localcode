"""Attachment storage: text/code, PDFs, images, audio and video metadata."""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import uuid
from pathlib import Path

from . import config

UPLOAD_DIR = config.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 240_000

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css",
    ".scss", ".sass", ".less", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".py", ".pyw", ".rb", ".php", ".java", ".kt", ".kts", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".swift", ".m", ".mm", ".lua", ".luau",
    ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".dockerfile",
    ".gitignore", ".env", ".log", ".tex", ".r", ".jl", ".dart", ".ex", ".exs",
    ".erl", ".hrl", ".fs", ".fsx", ".vb", ".asm", ".s", ".proto", ".graphql",
    ".gql", ".ipynb", ".srt", ".vtt", ".diff", ".patch",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}


def _safe_name(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^A-Za-z0-9._ ()\[\]-]+", "_", name).strip(" .")
    return name[:160] or "file"


def _sidecar(att_id: str) -> Path:
    return UPLOAD_DIR / f"{att_id}.json"


def _data_path(att_id: str) -> Path:
    for p in UPLOAD_DIR.glob(f"{att_id}.*"):
        if p.suffix != ".json":
            return p
    return UPLOAD_DIR / att_id


def kind_for(name: str, mime: str) -> str:
    ext = Path(name).suffix.lower()
    mime = (mime or "").lower()
    if ext in IMAGE_EXTENSIONS or mime.startswith("image/"):
        return "image"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in AUDIO_EXTENSIONS or mime.startswith("audio/"):
        return "audio"
    if ext in VIDEO_EXTENSIONS or mime.startswith("video/"):
        return "video"
    if ext in TEXT_EXTENSIONS or mime.startswith("text/") or mime in {
        "application/json", "application/xml", "application/javascript",
        "application/x-yaml", "application/x-sh",
    }:
        return "text"
    return "binary"


def _extract_pdf(data: bytes) -> tuple[str, dict]:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        return "", {"error": f"PDF support is not installed ({exc}). Run: pip install pypdf"}
    try:
        reader = PdfReader(__import__("io").BytesIO(data))
        pages = []
        total = 0
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                pages.append(f"--- Page {i} ---\n{text.strip()}")
                total += len(text)
            if total >= MAX_TEXT_CHARS:
                pages.append("... [PDF text truncated]")
                break
        text = "\n\n".join(pages)
        return text[:MAX_TEXT_CHARS], {
            "pages": len(reader.pages),
            "scanned": not bool(text.strip()),
        }
    except Exception as exc:
        return "", {"error": f"Could not read this PDF: {exc}"}


def save(file_storage) -> dict:
    filename = _safe_name(getattr(file_storage, "filename", "") or "file")
    mime = getattr(file_storage, "mimetype", "") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data = file_storage.read(MAX_BYTES + 1)
    if not data:
        raise ValueError("The uploaded file is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"File is too large (max {config.human_bytes(MAX_BYTES)}).")
    att_id = "att_" + uuid.uuid4().hex[:20]
    kind = kind_for(filename, mime)
    ext = Path(filename).suffix.lower() or ".bin"
    path = UPLOAD_DIR / f"{att_id}{ext}"
    path.write_bytes(data)
    text = ""
    extra: dict = {}
    if kind == "pdf":
        text, extra = _extract_pdf(data)
    elif kind == "text":
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n... [attachment truncated]"
    elif kind == "image":
        extra["data_url"] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    meta = {
        "id": att_id,
        "name": filename,
        "mime": mime,
        "size": len(data),
        "kind": kind,
        "ext": ext,
        "created_at": config.now(),
        "url": f"/api/attachments/{att_id}/raw",
        "text_chars": len(text),
        **extra,
    }
    _sidecar(att_id).write_text(
        json.dumps({**meta, "text": text}, ensure_ascii=False), encoding="utf-8"
    )
    return public(meta)


def public(meta: dict) -> dict:
    out = {k: v for k, v in meta.items() if k not in {"text", "data_url", "path"}}
    if out.get("kind") == "pdf" and not out.get("pages"):
        out["pages"] = 0
    return out


def get(att_id: str) -> dict | None:
    p = _sidecar(str(att_id))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_public(att_id: str) -> dict | None:
    meta = get(att_id)
    return public(meta) if meta else None


def data_path(att_id: str) -> Path | None:
    p = _data_path(str(att_id))
    return p if p.exists() else None


def delete(att_id: str) -> bool:
    found = False
    for p in UPLOAD_DIR.glob(f"{att_id}.*"):
        try:
            p.unlink()
            found = True
        except OSError:
            pass
    return found


def text_for_model(meta: dict, limit: int = 60_000) -> str:
    text = (meta.get("text") or "").strip()
    if not text:
        if meta.get("kind") == "pdf":
            return "[PDF has no extractable text; it may be scanned.]"
        return ""
    if len(text) > limit:
        text = text[:limit] + "\n... [attachment truncated for this turn]"
    return text


def image_data_url(meta: dict) -> str:
    if meta.get("data_url"):
        return meta["data_url"]
    p = data_path(meta["id"])
    if not p:
        return ""
    data = p.read_bytes()
    mime = meta.get("mime") or "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def summary_line(meta: dict) -> str:
    kind = meta.get("kind") or "file"
    size = config.human_bytes(meta.get("size") or 0)
    if kind == "pdf":
        return f"{meta.get('name')} (PDF, {meta.get('pages') or '?'} pages, {size})"
    if kind == "image":
        return f"{meta.get('name')} (image, {size})"
    return f"{meta.get('name')} ({kind}, {size})"
