#!/usr/bin/env python3
"""Build the icon and logo files from 1st Energy's own logo.

    uv run --no-project --with cairosvg python brands/make_assets.py

Both outputs are made by changing the SVG's viewBox, never by editing the
artwork: the logo keeps the source viewBox, and the icon centres the same
lockup on a square canvas. The PNGs are copied into
custom_components/first_energy/brand/, where Home Assistant (2026.3 and
later) serves them from.

cairosvg rather than ImageMagick: ImageMagick's built-in SVG renderer drops
the gradient on the three circles and draws them black.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import cairosvg

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "1st_energy_logo.svg"
INSTALL = HERE.parent / "custom_components" / "first_energy" / "brand"

# The source viewBox, which is already tight to the artwork.
WIDTH, HEIGHT = 1280.0, 626.3

# Space around the lockup on the square icon, so it doesn't touch the edge
# of a small tile.
ICON_PADDING = 0.08


def with_viewbox(svg: str, x: float, y: float, w: float, h: float) -> str:
    replaced, count = re.subn(
        r'viewBox="[^"]*"', f'viewBox="{x:g} {y:g} {w:g} {h:g}"', svg, count=1
    )
    if count != 1:
        raise SystemExit("source SVG has no viewBox")
    return replaced


def render(svg: str, path: Path, width: int, height: int) -> None:
    cairosvg.svg2png(
        bytestring=svg.encode(), write_to=str(path), output_width=width, output_height=height
    )
    print(f"wrote {path.relative_to(HERE.parent)} ({width}x{height})")


def main() -> None:
    source = SOURCE.read_text()

    side = WIDTH * (1 + ICON_PADDING)
    icon = with_viewbox(source, -(side - WIDTH) / 2, -(side - HEIGHT) / 2, side, side)
    logo = with_viewbox(source, 0, 0, WIDTH, HEIGHT)
    (HERE / "icon.svg").write_text(icon)
    (HERE / "logo.svg").write_text(logo)

    # Home Assistant's sizes: a 256px square icon, and a logo whose shorter
    # side is 256px; each with a double-size @2x variant.
    logo_width = round(256 * WIDTH / HEIGHT)
    render(icon, HERE / "icon.png", 256, 256)
    render(icon, HERE / "icon@2x.png", 512, 512)
    render(logo, HERE / "logo.png", logo_width, 256)
    render(logo, HERE / "logo@2x.png", logo_width * 2, 512)

    INSTALL.mkdir(parents=True, exist_ok=True)
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        shutil.copyfile(HERE / name, INSTALL / name)
    print(f"installed into {INSTALL.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
