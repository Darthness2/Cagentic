"""One product across three surfaces.

The iOS app, the gateway's web page and the browser extension are separate codebases that a user
experiences as one thing. They already shared a palette and a spacing scale; what made them read
as different products was the type — the app was set in Space Grotesk and Plus Jakarta Sans while
both browser surfaces were on system-UI and Inter.

These pin the parts that are easy to change on one surface and forget on the others. Where the two
disagreed, the app is the reference.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "cagentic" / "gateway_assets"
EXTENSION = ROOT / "extension"
IOS = ROOT / "iOS" / "Cagentic"

WEB_CSS = (WEB / "app.css").read_text(encoding="utf-8")
EXTENSION_CSS = (EXTENSION / "theme.css").read_text(encoding="utf-8")
IOS_THEME = (IOS / "DesignSystem" / "CagenticTheme.swift").read_text(encoding="utf-8")

# The three faces, split by what the text is rather than where it sits.
UI_FACE = "Space Grotesk"
READING_FACE = "Plus Jakarta Sans"
CODE_FACE = "JetBrains Mono"

FONT_FILES = (
    "SpaceGrotesk-Regular",
    "SpaceGrotesk-Medium",
    "SpaceGrotesk-SemiBold",
    "SpaceGrotesk-Bold",
    "PlusJakartaSans-Regular",
    "PlusJakartaSans-Medium",
    "PlusJakartaSans-SemiBold",
    "PlusJakartaSans-Bold",
    "PlusJakartaSans-Italic",
    "JetBrainsMono-Regular",
)


def test_every_surface_names_the_same_three_faces() -> None:
    for name, source in (
        ("web", WEB_CSS),
        ("extension", EXTENSION_CSS),
        ("ios", IOS_THEME),
    ):
        for face in (UI_FACE, READING_FACE, CODE_FACE):
            # iOS spells them as PostScript names, without the spaces.
            spelling = face.replace(" ", "") if source is IOS_THEME else face
            assert spelling in source, f"{name} does not use {face}"


def test_both_browser_surfaces_ship_the_faces_they_name() -> None:
    # A @font-face pointing at a file that is not there falls back silently to system type, which
    # is exactly the divergence this is here to prevent — and it looks fine on the machine that
    # has the fonts installed locally.
    for directory in (WEB / "fonts", EXTENSION / "fonts"):
        present = {path.stem for path in directory.glob("*.woff2")}
        assert set(FONT_FILES) <= present, f"{directory} is missing {set(FONT_FILES) - present}"


def test_the_faces_are_the_same_files_the_app_ships() -> None:
    # Same glyphs, not merely the same family name — a differently-cut copy of a font with the
    # same name is a divergence nobody would think to look for. fontTools is the generator's
    # dependency rather than the project's, so this checks when it happens to be installed.
    ttlib = pytest.importorskip("fontTools.ttLib", reason="fontTools is a tool dependency")
    TTFont = ttlib.TTFont

    app_fonts = IOS / "Resources" / "Fonts"
    for stem in FONT_FILES:
        source = TTFont(app_fonts / f"{stem}.ttf")
        web = TTFont(WEB / "fonts" / f"{stem}.woff2")
        assert source["maxp"].numGlyphs == web["maxp"].numGlyphs, stem
        assert source["OS/2"].usWeightClass == web["OS/2"].usWeightClass, stem


def test_the_web_splits_type_by_role_the_way_the_app_does() -> None:
    # Chrome, conversation and payloads each get their own face; using one for everything is what
    # the app deliberately stopped doing.
    assert "--reading:" in WEB_CSS
    body = re.search(r"^\.msg-body \{[^}]*\}", WEB_CSS, re.M).group(0)
    assert "font-family: var(--reading)" in body
    assert '--code: "JetBrains Mono"' in WEB_CSS


def test_the_reading_size_matches_the_app() -> None:
    # The app sets a reply at `.body`, 17pt. A different size on each surface is the kind of
    # mismatch nobody can name but everybody feels.
    assert "--fs-base: 17px;" in WEB_CSS
    assert "--fs-code: 16px;" in WEB_CSS  # the app's `.callout`, which is what it sets code in


def test_the_shared_geometry_has_not_drifted() -> None:
    def declares(css: str, token: str, value: str) -> bool:
        # The stylesheets pad token values into columns, so match on the declaration not the text.
        return re.search(rf"{re.escape(token)}:\s*{re.escape(value)}\s*;", css) is not None

    for token, value in (("--r-control", "8px"), ("--r-card", "12px"), ("--r-sheet", "16px")):
        assert declares(WEB_CSS, token, value), token
        assert declares(EXTENSION_CSS, f"--cag-r-{token.removeprefix('--r-')}", value), token
    # The app's Spacing scale, in the same order.
    for index, value in enumerate(("4px", "8px", "12px", "16px", "24px", "32px", "48px"), start=1):
        assert declares(WEB_CSS, f"--s-{index}", value), f"--s-{index}"
