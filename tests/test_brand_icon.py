"""The brand mark, and the one definition every copy of it comes from.

The mark appears on six surfaces: the iOS app icon and its in-app shape, the gateway's inline
symbol and favicon, and the browser extension's toolbar icons and shells. They are generated from
`design-system/brand/make-icons.py` rather than drawn one at a time — the icon this one replaced
had drifted into two Swift generators and three hand-pasted copies of its path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cagentic.agent import Agent
from cagentic.gateway import Gateway
from cagentic.ollama_client import OllamaClient

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_ASSETS = ROOT / "cagentic" / "gateway_assets"
EXTENSION = ROOT / "extension"
GENERATOR = ROOT / "design-system" / "brand" / "make-icons.py"

LIGHT_BACKGROUND, LIGHT_MARK = "#F7F9FB", "#0969DA"
DARK_BACKGROUND, DARK_MARK = "#0A0C10", "#7CC4FF"


def canonical_path() -> str:
    """The mark's path data.

    Read from a committed artifact rather than by running the generator, which needs Pillow —
    a tool dependency, not a project one. What matters here is that every surface carries the
    same path; `BrandMarkTests` on the iOS side is what ties the geometry back to the generator.
    """
    svg = (GATEWAY_ASSETS / "favicon-dark.svg").read_text(encoding="utf-8")
    match = re.search(r'\sd="(M[^"]+)"', svg)
    assert match, "favicon-dark.svg has no path"
    return match.group(1)


@pytest.fixture(scope="module")
def mark_path() -> str:
    return canonical_path()


def test_every_surface_draws_the_same_mark(mark_path: str) -> None:
    surfaces = [
        GATEWAY_ASSETS / "index.html",
        GATEWAY_ASSETS / "favicon.svg",
        GATEWAY_ASSETS / "favicon-light.svg",
        EXTENSION / "icons" / "cagentic-mark.svg",
        EXTENSION / "popup.html",
        EXTENSION / "sidepanel.html",
    ]
    missing = [
        str(path.relative_to(ROOT))
        for path in surfaces
        if mark_path not in path.read_text(encoding="utf-8")
    ]
    assert missing == []


def test_the_previous_mark_is_gone_everywhere() -> None:
    # The four-point spark. A leftover copy is invisible until someone opens that one screen.
    spark = "M12 3c.7 5.1 3.9 8.3 9 9"
    stale = []
    for path in list(GATEWAY_ASSETS.iterdir()) + list(EXTENSION.rglob("*")):
        if path.is_file() and path.suffix in {".html", ".css", ".js", ".svg"}:
            if spark in path.read_text(encoding="utf-8", errors="ignore"):
                stale.append(str(path.relative_to(ROOT)))
    assert stale == []


def test_the_favicons_carry_the_two_palettes() -> None:
    light = (GATEWAY_ASSETS / "favicon-light.svg").read_text(encoding="utf-8")
    dark = (GATEWAY_ASSETS / "favicon-dark.svg").read_text(encoding="utf-8")
    auto = (GATEWAY_ASSETS / "favicon.svg").read_text(encoding="utf-8")

    assert LIGHT_BACKGROUND in light and LIGHT_MARK in light
    assert DARK_BACKGROUND in dark and DARK_MARK in dark
    # The one the markup links has to work before any script runs, so it follows the OS itself.
    assert "prefers-color-scheme: dark" in auto


def test_the_page_links_a_favicon_and_follows_its_own_theme() -> None:
    html = (GATEWAY_ASSETS / "index.html").read_text(encoding="utf-8")
    app_js = (GATEWAY_ASSETS / "app.js").read_text(encoding="utf-8")

    assert '<link rel="icon" href="/favicon.svg"' in html
    # The page's theme is a setting of its own, so the tab cannot be left to the OS media query.
    assert "icon.href=dark?'/favicon-dark.svg':'/favicon-light.svg'" in app_js


def test_the_gateway_serves_every_favicon(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAGENTIC_WORKSPACE_ROOTS", str(tmp_path / "root"))
    (tmp_path / "root").mkdir(exist_ok=True)
    agent = Agent(OllamaClient("http://127.0.0.1:1"), "test", tmp_path / "root")
    gateway = Gateway(agent, {"gateway": {"port": 19412}}, port=19412)

    import urllib.request

    assert gateway.start()
    try:
        for name in ("favicon.svg", "favicon-light.svg", "favicon-dark.svg"):
            # Unauthenticated on purpose: a favicon is fetched by the browser chrome, which
            # sends no token.
            with urllib.request.urlopen(f"http://127.0.0.1:{gateway.port}/{name}") as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("image/svg+xml")
                assert b"<svg" in response.read()
    finally:
        gateway.stop()
