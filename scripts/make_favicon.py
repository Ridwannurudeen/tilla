#!/usr/bin/env python3
"""Rasterize www/favicon.svg into www/favicon.ico (PNG-in-ICO, 32x32).

The box has rsvg-convert but no ImageMagick / icotool / png2ico, so the ICO is
assembled here instead. An ICO directory entry may hold a whole PNG rather than a
BMP bitmap — that is a real .ico, not a PNG with the extension changed, and every
current browser plus anything using the Windows shell icon loader reads it.

Serving a genuine ICO matters only for the clients that hit /favicon.ico blind:
crawlers, bookmark importers, feed readers. Modern browsers already use the
<link rel="icon"> SVG the pages carry inline.

Usage:  python3 scripts/make_favicon.py [svg_path] [ico_path]
Requires rsvg-convert on PATH.
"""

from __future__ import annotations

import pathlib
import struct
import subprocess
import sys
import tempfile

SIZE = 32


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    svg = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / "www/favicon.svg"
    ico = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else root / "www/favicon.ico"
    if not svg.is_file():
        print("no such svg: %s" % svg, file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        png_path = pathlib.Path(tmp) / "favicon.png"
        try:
            subprocess.run(
                [
                    "rsvg-convert",
                    "-w",
                    str(SIZE),
                    "-h",
                    str(SIZE),
                    str(svg),
                    "-o",
                    str(png_path),
                ],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            print("rsvg-convert not on PATH", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as exc:
            print("rsvg-convert failed: %s" % exc.stderr.decode(errors="replace"))
            return 1
        png = png_path.read_bytes()

    # ICONDIR: reserved=0, type=1 (icon), count=1
    header = struct.pack("<HHH", 0, 1, 1)
    # ICONDIRENTRY: w, h (0 would mean 256), colours, reserved, planes, bpp,
    # byte length, offset to the image data
    entry = struct.pack(
        "<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(png), len(header) + 16
    )
    ico.write_bytes(header + entry + png)
    print(
        "wrote %s (%d bytes, %dx%d PNG-in-ICO)" % (ico, ico.stat().st_size, SIZE, SIZE)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
