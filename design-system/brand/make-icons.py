#!/usr/bin/env python3
"""Generates every raster and vector copy of the Cagentic mark from one definition.

The mark is a ring broken into four arcs. Because it is pure geometry it is described once,
here, and every surface — the iOS app icon, the browser-extension icons, the web favicon — is
derived from it. Hand-editing any of the outputs would put them out of step with each other,
which is exactly what happened to the icon this one replaces.

Needs Pillow, which is a tool dependency rather than a project one:

    python3 -m pip install pillow
    python3 design-system/brand/make-icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw

# --- geometry ---------------------------------------------------------------
# Angles are the usual mathematical convention: degrees counter-clockwise from the
# positive x-axis. Four arcs on the quarters, each a little narrower than its 90°
# share, which is what leaves the four gaps.
ARC_CENTRES = (30.0, 120.0, 210.0, 300.0)
ARC_SPAN = 65.0
# Fractions of the canvas edge, measured off the artwork: the ring's centre-line
# radius and the stroke that rides on it.
RADIUS = 0.3486
STROKE = 0.09375

# --- palette ----------------------------------------------------------------
# The app's own light and dark surfaces. The tinted variant is drawn in white, because iOS
# builds its tint from the artwork's luminance.
LIGHT = ("#F7F9FB", "#0969DA")
DARK = ("#0A0C10", "#7CC4FF")
# The extension's icon sits on a card rather than bleeding to the edge, because a
# browser toolbar does not mask it the way iOS masks an app icon.
EXTENSION_BORDER = "#27313C"

ROOT = Path(__file__).resolve().parents[2]


def arcs() -> list[tuple[float, float]]:
    """Each arc as (start, end) in mathematical degrees."""
    return [(c - ARC_SPAN / 2, c + ARC_SPAN / 2) for c in ARC_CENTRES]


def point(cx: float, cy: float, r: float, degrees: float) -> tuple[float, float]:
    """A point on the ring. y grows downward, as it does in every target format."""
    radians = math.radians(degrees)
    return (cx + r * math.cos(radians), cy - r * math.sin(radians))


def svg_path(size: float = 24.0) -> str:
    """The mark as one SVG path of four arc segments."""
    cx = cy = size / 2
    r = size * RADIUS
    parts = []
    for start, end in arcs():
        x1, y1 = point(cx, cy, r, start)
        x2, y2 = point(cx, cy, r, end)
        # sweep-flag 0: counter-clockwise on screen, matching increasing maths angles.
        parts.append(f"M{x1:.2f} {y1:.2f}A{r:.2f} {r:.2f} 0 0 0 {x2:.2f} {y2:.2f}")
    return "".join(parts)


def render(size: int, background: str | None, mark: str, *, card: bool = False) -> Image.Image:
    """One raster of the mark. Drawn 4x and reduced, since PIL has no antialiasing."""
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if background is not None:
        if card:
            inset = canvas * 0.03
            draw.rounded_rectangle(
                [inset, inset, canvas - inset, canvas - inset],
                radius=canvas * 0.22,
                fill=background,
                outline=EXTENSION_BORDER,
                width=max(1, int(canvas * 0.012)),
            )
        else:
            draw.rectangle([0, 0, canvas, canvas], fill=background)

    cx = cy = canvas / 2
    r = canvas * RADIUS
    width = canvas * STROKE
    # PIL strokes an arc *inward* from its bounding box, so a box of radius r would put the
    # band's centre-line at r - width/2 and the drawn-on caps would sit half a stroke outside
    # it, leaving a visible step at every end.
    outer = r + width / 2
    box = [cx - outer, cy - outer, cx + outer, cy + outer]
    for start, end in arcs():
        # PIL measures clockwise from 3 o'clock, so its angles are the negated ones and
        # the endpoints swap to keep the sweep going the same way round.
        draw.arc(box, start=-end, end=-start, fill=mark, width=round(width))
        # PIL strokes with flat ends; the caps are drawn on.
        for degrees in (start, end):
            px, py = point(cx, cy, r, degrees)
            draw.ellipse(
                [px - width / 2, py - width / 2, px + width / 2, py + width / 2],
                fill=mark,
            )
    return image.resize((size, size), Image.LANCZOS)


def write(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    print(f"  {path.relative_to(ROOT)}  {path.stat().st_size // 1024 or 1}KB")


# The three families the app is set in, and every weight it names. Converted from the same TTFs
# iOS ships so the web page and the extension are set in the identical letterforms — matching
# colours and spacing count for nothing when the type is somebody else's.
WEB_FONT_TARGETS = (
    ROOT / "cagentic/gateway_assets/fonts",
    ROOT / "extension/fonts",
)


def make_web_fonts() -> None:
    """woff2 copies of the bundled iOS faces, for the two browser surfaces.

    Static weights rather than one variable file per family, deliberately: a browser only fetches
    a `@font-face` source when a glyph actually needs it, so ten small files cost less than three
    big ones that are downloaded whether or not the page uses semibold.
    """
    source = ROOT / "iOS/Cagentic/Resources/Fonts"
    faces = sorted(source.glob("*.ttf"))
    if not faces:
        print("  (no iOS fonts found — run this from a checkout with them)")
        return
    for directory in WEB_FONT_TARGETS:
        directory.mkdir(parents=True, exist_ok=True)
    total = 0
    for face in faces:
        font = TTFont(face)
        font.flavor = "woff2"
        for directory in WEB_FONT_TARGETS:
            out = directory / (face.stem + ".woff2")
            font.save(out)
            total += out.stat().st_size
    print(f"  {len(faces)} faces x {len(WEB_FONT_TARGETS)} targets  {total // 1024}KB total")


def main() -> None:
    print("iOS app icon")
    icons = ROOT / "iOS/Cagentic/Resources/Assets.xcassets/AppIcon.appiconset"
    # Only the default appearance is opaque. iOS draws its own backdrop behind the dark and
    # tinted variants and expects to receive just the artwork, which is also why the tinted one
    # is a *light* mark: the system tints by luminance, so an opaque light background would take
    # the colour and leave the ring as a hole in it.
    write(render(1024, LIGHT[0], LIGHT[1]).convert("RGB"), icons / "AppIcon-Standard.png")
    write(render(1024, None, DARK[1]), icons / "AppIcon-Dark.png")
    write(render(1024, None, "#FFFFFF"), icons / "AppIcon-Tinted.png")

    print("browser extension")
    for size in (16, 32, 48, 128):
        # Transparent outside the card: the previous icons carried a white square that
        # showed as a bright tile in a dark toolbar.
        write(render(size, DARK[0], DARK[1], card=True), ROOT / f"extension/icons/cagentic-{size}.png")

    print("vector")
    mark_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n'
        f'  <rect x=".75" y=".75" width="22.5" height="22.5" rx="5.25" fill="{DARK[0]}" '
        f'stroke="{EXTENSION_BORDER}" stroke-width="1.5"/>\n'
        f'  <path d="{svg_path(24)}" fill="none" stroke="{DARK[1]}" stroke-width="2.2" '
        'stroke-linecap="round"/>\n'
        "</svg>\n"
    )
    (ROOT / "extension/icons/cagentic-mark.svg").write_text(mark_svg, encoding="utf-8")
    print("  extension/icons/cagentic-mark.svg")

    def favicon(background: str, mark: str, *, follow_os: bool) -> str:
        """One favicon. `follow_os` bakes in a media query for the pre-JS default."""
        style = ""
        if follow_os:
            style = (
                "  <style>\n"
                f"    .bg {{ fill: {LIGHT[0]}; }} .mark {{ stroke: {LIGHT[1]}; }}\n"
                "    @media (prefers-color-scheme: dark) {\n"
                f"      .bg {{ fill: {DARK[0]}; }} .mark {{ stroke: {DARK[1]}; }}\n"
                "    }\n"
                "  </style>\n"
            )
        fills = "" if follow_os else f' fill="{background}"'
        strokes = "" if follow_os else f' stroke="{mark}"'
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n'
            f"{style}"
            f'  <rect class="bg" width="24" height="24" rx="5"{fills}/>\n'
            f'  <path class="mark" d="{svg_path(24)}" fill="none"{strokes} stroke-width="2.2" '
            'stroke-linecap="round"/>\n'
            "</svg>\n"
        )

    assets = ROOT / "cagentic/gateway_assets"
    # Three, because the page's theme is its own setting rather than the operating system's. The
    # media-query one is what the markup links so the tab is right before any script runs; the
    # explicit pair is what the theme switch points at once the reader has chosen.
    for name, args in (
        ("favicon.svg", (LIGHT[0], LIGHT[1], True)),
        ("favicon-light.svg", (LIGHT[0], LIGHT[1], False)),
        ("favicon-dark.svg", (DARK[0], DARK[1], False)),
    ):
        (assets / name).write_text(
            favicon(args[0], args[1], follow_os=args[2]), encoding="utf-8"
        )
        print(f"  cagentic/gateway_assets/{name}")

    print("web fonts")
    make_web_fonts()

    print("\npath data for the in-app symbol and the SwiftUI shape:")
    print(f"  {svg_path(24)}")


if __name__ == "__main__":
    main()
