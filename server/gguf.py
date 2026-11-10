"""Minimal GGUF metadata reader used for memory estimates.

Only the header and metadata section are parsed; tensor data is never read, so
this stays cheap even for 30 GB model files. The parser is intentionally
defensive: a malformed or unusual file simply returns no profile and callers
fall back to conservative heuristics.
"""
from __future__ import annotations

import struct
from pathlib import Path

__all__ = ["GGUFError", "read_metadata", "model_profile", "kv_bytes_per_token"]


class GGUFError(Exception):
    pass


# GGUF metadata value types -> (struct format, byte size)
_SCALARS = {
    0: ("<B", 1),    # uint8
    1: ("<b", 1),    # int8
    2: ("<H", 2),    # uint16
    3: ("<h", 2),    # int16
    4: ("<I", 4),    # uint32
    5: ("<i", 4),    # int32
    6: ("<f", 4),    # float32
    7: ("<?", 1),    # bool
    10: ("<Q", 8),   # uint64
    11: ("<q", 8),   # int64
    12: ("<d", 8),   # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9


class _Reader:
    def __init__(self, fh):
        self.fh = fh

    def read(self, n: int) -> bytes:
        data = self.fh.read(n)
        if len(data) != n:
            raise GGUFError("Unexpected end of GGUF file")
        return data

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def string(self) -> str:
        length = self.u64()
        if length > 4_000_000:
            raise GGUFError("Unreasonable GGUF string length")
        return self.read(length).decode("utf-8", errors="replace")

    def skip(self, vtype: int) -> None:
        scalar = _SCALARS.get(vtype)
        if scalar:
            self.fh.seek(scalar[1], 1)
            return
        if vtype == _TYPE_STRING:
            length = self.u64()
            self.fh.seek(length, 1)
            return
        if vtype == _TYPE_ARRAY:
            item_type = self.u32()
            count = self.u64()
            for _ in range(count):
                self.skip(item_type)
            return
        raise GGUFError(f"Unknown GGUF metadata type {vtype}")

    def value(self, vtype: int):
        scalar = _SCALARS.get(vtype)
        if scalar:
            fmt, size = scalar
            return struct.unpack(fmt, self.read(size))[0]
        if vtype == _TYPE_STRING:
            return self.string()
        if vtype == _TYPE_ARRAY:
            item_type = self.u32()
            count = self.u64()
            # Tokenizer vocabularies can hold hundreds of thousands of entries.
            # We do not need them, so skip large arrays instead of building them.
            if count > 4096:
                for _ in range(count):
                    self.skip(item_type)
                return []
            return [self.value(item_type) for _ in range(count)]
        raise GGUFError(f"Unknown GGUF metadata type {vtype}")


def read_metadata(path: str | Path) -> dict:
    with open(path, "rb") as fh:
        if fh.read(4) != b"GGUF":
            raise GGUFError("Not a GGUF file")
        reader = _Reader(fh)
        version = reader.u32()
        if version not in (2, 3):
            raise GGUFError(f"Unsupported GGUF version {version}")
        tensor_count = reader.u64()
        kv_count = reader.u64()
        metadata: dict = {}
        for _ in range(kv_count):
            key = reader.string()
            vtype = reader.u32()
            metadata[key] = reader.value(vtype)
        return {
            "version": version,
            "tensor_count": tensor_count,
            "kv_count": kv_count,
            "metadata": metadata,
        }


def _as_int(value, default: int = 0) -> int:
    if isinstance(value, list):
        numbers = [v for v in value if isinstance(v, (int, float))]
        value = max(numbers) if numbers else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def model_profile(path: str | Path) -> dict:
    """Return architecture details needed for context/VRAM estimates."""
    try:
        metadata = read_metadata(path)["metadata"]
    except Exception:
        return {}
    arch = str(metadata.get("general.architecture") or "")
    if not arch:
        return {}

    def number(name: str, default: int = 0) -> int:
        return _as_int(metadata.get(f"{arch}.{name}", default), default)

    layers = number("block_count")
    heads = number("attention.head_count")
    kv_heads = number("attention.head_count_kv") or heads
    embedding = number("embedding_length")
    context = number("context_length")
    return {
        "architecture": arch,
        "layers": layers,
        "heads": heads,
        "kv_heads": kv_heads,
        "embedding": embedding,
        "context": context,
    }


def kv_bytes_per_token(profile: dict) -> int:
    """Approximate KV-cache bytes per token (K+V, fp16)."""
    layers = _as_int(profile.get("layers"))
    heads = _as_int(profile.get("heads"))
    kv_heads = _as_int(profile.get("kv_heads")) or heads
    embedding = _as_int(profile.get("embedding"))
    if not (layers and heads and kv_heads and embedding):
        return 0
    head_dim = max(1, embedding // heads)
    return int(2 * layers * kv_heads * head_dim * 2)
