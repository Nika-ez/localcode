"""Attachment tests: text, real PDF text extraction, image handling, chat injection."""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get("LOCALCODE_BASE", "http://127.0.0.1:8765").rstrip("/")
FAILS: list[str] = []
PASSES = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name + (f" - {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def make_pdf(text: str) -> bytes:
    """Minimal valid one-page PDF with real extractable text."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 18 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def upload(name: str, data: bytes, mime: str) -> dict:
    r = requests.post(BASE + "/api/attachments",
                      files={"file": (name, io.BytesIO(data), mime)}, timeout=60)
    r.raise_for_status()
    return r.json()["attachment"]


def stream_chat(session_id: str, message: str, attachments: list[str]):
    resp = requests.post(BASE + "/api/chat", json={
        "session_id": session_id, "message": message, "attachments": attachments,
    }, stream=True, timeout=(10, 180))
    resp.raise_for_status()
    text = ""
    events = []
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            events.append(event)
            if event.get("type") == "delta":
                text += event.get("text") or ""
            if event.get("type") == "error":
                print("       ERROR:", event.get("message"))
    finally:
        resp.close()
    return text, events


def main() -> int:
    print("== attachments ==")
    health = requests.get(BASE + "/api/health", timeout=10).json()
    check("app reachable", health.get("ok") is True, str(health))
    cfg = requests.patch(BASE + "/api/settings", json={
        "provider": "openai",
        "external_base_url": os.environ.get("LOCALCODE_MOCK", "http://127.0.0.1:8899/v1"),
        "external_model": "mock-coder",
        "approval_mode": "auto",
    }, timeout=30)
    check("mock provider configured", cfg.status_code == 200, cfg.text[:200])

    text_att = upload("notes.txt", b"hello attachment\nsecond line\n", "text/plain")
    check("text attachment stored", text_att.get("kind") == "text", str(text_att))
    check("text attachment metadata", text_att.get("text_chars", 0) > 10, str(text_att))
    raw = requests.get(BASE + text_att["url"], timeout=10)
    check("text attachment raw download", raw.status_code == 200 and b"hello attachment" in raw.content)

    pdf_bytes = make_pdf("LocalCode PDF extraction test.")
    pdf_att = upload("sample.pdf", pdf_bytes, "application/pdf")
    check("PDF attachment stored", pdf_att.get("kind") == "pdf", str(pdf_att))
    check("PDF has one page", pdf_att.get("pages") == 1, str(pdf_att))
    check("PDF text extracted", pdf_att.get("text_chars", 0) > 10, str(pdf_att))
    check("PDF not marked scanned", pdf_att.get("scanned") is False, str(pdf_att))

    png = Path("assets/localcode.png")
    if png.exists():
        img_att = upload("logo.png", png.read_bytes(), "image/png")
        check("image attachment stored", img_att.get("kind") == "image", str(img_att))
        check("image data URL hidden from metadata", "data_url" not in img_att, str(img_att))
        img_raw = requests.get(BASE + img_att["url"], timeout=10)
        check("image raw download", img_raw.status_code == 200 and img_raw.content[:4] == b"\x89PNG")
    else:
        img_att = None
        check("icon available for image test", False, str(png))

    print("\n== PDF text reaches the model ==")
    sid = requests.post(BASE + "/api/sessions", json={}, timeout=20).json()["session"]["id"]
    reply, events = stream_chat(sid, "Summarize the attached PDF.", [pdf_att["id"]])
    check("PDF chat completed", any(e.get("type") == "done" for e in events), str(events[-1:]))
    check("model received PDF text", "I can see the PDF text" in reply, reply[:200])
    state = requests.get(BASE + f"/api/sessions/{sid}", timeout=20).json()
    user = next(m for m in state["messages"] if m["role"] == "user")
    check("attachment reference persisted on message", bool(user.get("attachments"))
          and user["attachments"][0]["id"] == pdf_att["id"], str(user.get("attachments")))

    print("\n== image on a non-vision model ==")
    if img_att:
        sid = requests.post(BASE + "/api/sessions", json={}, timeout=20).json()["session"]["id"]
        reply, events = stream_chat(sid, "What is in this image?", [img_att["id"]])
        check("image chat completed", any(e.get("type") == "done" for e in events), str(events[-1:]))
        check("model told image cannot be seen", "I received the image attachment note." in reply,
              reply[:200])

    print("\n== cleanup ==")
    for att in [text_att, pdf_att] + ([img_att] if img_att else []):
        r = requests.delete(BASE + f"/api/attachments/{att['id']}", timeout=20)
        check(f"delete {att['kind']} attachment", r.status_code == 200 and r.json().get("ok") is True)
        after = requests.get(BASE + f"/api/attachments/{att['id']}", timeout=20)
        check(f"{att['kind']} attachment gone", after.status_code == 404)

    print(f"\n{'=' * 60}\n{PASSES} passed, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL:", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
