#!/usr/bin/env python3
"""Generate assets/pilotmarkets.ico with the standard library only.

An amber candlestick on black, at the four sizes Windows asks for. Writing
the ICO by hand avoids adding Pillow as a dependency for one 15KB file
that changes approximately never.

    python tools/make_icon.py
"""

import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 256)

BG = (5, 5, 5, 255)
AMBER = (255, 160, 0, 255)
GREEN = (18, 209, 142, 255)
RED = (255, 74, 74, 255)


def draw(size: int) -> list[list[tuple]]:
    px = [[BG for _ in range(size)] for _ in range(size)]
    u = size / 16.0            # one design unit

    def rect(x0, y0, x1, y1, colour):
        for y in range(max(0, int(y0)), min(size, int(y1))):
            for x in range(max(0, int(x0)), min(size, int(x1))):
                px[y][x] = colour

    # Three candles: down, up, up — reading left to right like a chart.
    # Wick then body, so the body covers the wick where they overlap.
    candles = [
        (2.0, RED,   3.0, 13.0,  5.0, 10.0),
        (7.0, GREEN, 2.0, 12.0,  4.5, 9.0),
        (11.5, AMBER, 1.5, 11.0, 3.0, 8.5),
    ]
    for x, colour, wick_top, wick_bottom, body_top, body_bottom in candles:
        cx = x * u
        wick_w = max(1.0, u * 0.4)
        body_w = max(2.0, u * 2.4)
        rect(cx + body_w / 2 - wick_w / 2, wick_top * u,
             cx + body_w / 2 + wick_w / 2, wick_bottom * u, colour)
        rect(cx, body_top * u, cx + body_w, body_bottom * u, colour)

    # A baseline, so it reads as a chart rather than three bars.
    rect(1 * u, 14.0 * u, 15 * u, 14.0 * u + max(1.0, u * 0.35), AMBER)
    return px


def png(size: int) -> bytes:
    """A PNG-encoded frame. ICO has accepted embedded PNGs since Vista and
    it is far less code than a BMP with its upside-down rows and AND mask."""
    px = draw(size)
    raw = b"".join(
        b"\x00" + b"".join(bytes(px[y][x]) for x in range(size))
        for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> int:
    frames = [(s, png(s)) for s in SIZES]
    out = [struct.pack("<HHH", 0, 1, len(frames))]
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        out.append(struct.pack("<BBBBHHII",
                               0 if size >= 256 else size,
                               0 if size >= 256 else size,
                               0, 0, 1, 32, len(data), offset))
        offset += len(data)
    out.extend(data for _, data in frames)

    path = Path(__file__).resolve().parents[1] / "assets" / "pilotmarkets.ico"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(out))
    print(f"wrote {path} ({path.stat().st_size:,} bytes, "
          f"{len(frames)} sizes: {', '.join(str(s) for s in SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
