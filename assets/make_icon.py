"""Generate the LocalCode app icon (PNG + Windows ICO) with no dependencies."""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZE = 256
SS = 4  # supersampling factor
W = SIZE * SS

BG_TOP = (10, 15, 28)
BG_BOTTOM = (16, 24, 40)
BORDER = (47, 129, 247)
CYAN = (76, 194, 255)
PALE = (165, 214, 255)


def _round_rect_sdf(x: float, y: float, x0: float, y0: float,
                    x1: float, y1: float, r: float) -> float:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(x - cx) - hx, abs(y - cy) - hy
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ox, oy) + min(max(qx, qy), 0.0) - r


def _seg_dist(px: float, py: float, ax: float, ay: float,
              bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _blend(dst: list[int], src: tuple[int, int, int], alpha: float) -> None:
    if alpha <= 0:
        return
    a = min(1.0, alpha)
    for i in range(3):
        dst[i] = int(dst[i] * (1 - a) + src[i] * a)


def build_pixels() -> bytearray:
    # Work at 4x and box-downsample for smooth edges.
    hi = bytearray(W * W * 4)
    margin = 10 * SS
    x0, y0 = margin, margin
    x1, y1 = W - margin, W - margin
    radius = 46 * SS
    border_w = 5 * SS
    half = 9 * SS

    # Scaled line geometry.
    def s(v: float) -> float:
        return v * SS

    lines = [
        ((s(99), s(92)), (s(66), s(128)), s(9), CYAN),
        ((s(66), s(128)), (s(99), s(164)), s(9), CYAN),
        ((s(157), s(92)), (s(190), s(128)), s(9), CYAN),
        ((s(190), s(128)), (s(157), s(164)), s(9), CYAN),
        ((s(146), s(84)), (s(112), s(172)), s(7), PALE),
    ]

    for y in range(W):
        row = y * W * 4
        # Background gradient.
        t = y / max(1, W - 1)
        bg = (
            int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t),
            int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t),
            int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t),
        )
        for x in range(W):
            i = row + x * 4
            d = _round_rect_sdf(x, y, x0, y0, x1, y1, radius)
            if d > 0.75:
                continue  # transparent outside
            col = list(bg)
            # Border ring.
            if d > -border_w:
                _blend(col, BORDER, min(1.0, (d + border_w) / 1.2))
            # Code glyphs.
            for (ax, ay), (bx, by), width, color in lines:
                dist = _seg_dist(x, y, ax, ay, bx, by)
                if dist <= width:
                    _blend(col, color, 1.0)
            hi[i] = col[0]
            hi[i + 1] = col[1]
            hi[i + 2] = col[2]
            hi[i + 3] = 255

    # Downsample.
    out = bytearray(SIZE * SIZE * 4)
    n = SS * SS
    for y in range(SIZE):
        for x in range(SIZE):
            r = g = b = a = 0
            for sy in range(SS):
                base = ((y * SS + sy) * W + x * SS) * 4
                for sx in range(SS):
                    i = base + sx * 4
                    r += hi[i]
                    g += hi[i + 1]
                    b += hi[i + 2]
                    a += hi[i + 3]
            o = (y * SIZE + x) * 4
            out[o] = r // n
            out[o + 1] = g // n
            out[o + 2] = b // n
            out[o + 3] = a // n
    return out


def png_bytes(pixels: bytearray) -> bytes:
    raw = bytearray()
    stride = SIZE * 4
    for y in range(SIZE):
        raw.append(0)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def main() -> None:
    root = Path(__file__).resolve().parent
    pixels = build_pixels()
    png = png_bytes(pixels)
    (root / "localcode.png").write_bytes(png)
    ico = struct.pack("<HHH", 0, 1, 1)
    ico += struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    ico += png
    (root / "localcode.ico").write_bytes(ico)
    print("wrote", root / "localcode.ico", len(ico), "bytes")


if __name__ == "__main__":
    main()
