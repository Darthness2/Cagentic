"""Cross-surface guardrails for Cagentic's shared product identity."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from cagentic import ui

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "cagentic" / "gateway_assets"
EXTENSION = ROOT / "extension"
IOS = ROOT / "iOS" / "Cagentic"


def test_gateway_uses_the_cli_inspired_terminal_palette() -> None:
    gateway = (GATEWAY / "app.css").read_text(encoding="utf-8")

    for dark, light in (
        ("#0a0c10", "#f7f9fb"),
        ("#7cc4ff", "#0969da"),
        ("#e7edf4", "#111820"),
    ):
        assert dark in gateway and light in gateway


def test_gateway_and_extension_share_semantic_state_colors() -> None:
    gateway = (GATEWAY / "app.css").read_text(encoding="utf-8")
    extension = (EXTENSION / "theme.css").read_text(encoding="utf-8")

    for dark, light in (
        ("#8ecf95", "#2f7d43"),
        ("#d9c069", "#8a6d10"),
        ("#d98a87", "#b03a35"),
    ):
        assert dark in gateway and light in gateway
        assert dark in extension and light in extension


def test_gateway_extension_and_ios_share_the_product_palette() -> None:
    gateway = (GATEWAY / "app.css").read_text(encoding="utf-8").lower()
    extension = (EXTENSION / "theme.css").read_text(encoding="utf-8").lower()
    ios = (IOS / "DesignSystem" / "CagenticTheme.swift").read_text(encoding="utf-8").lower()

    for dark, light in (
        ("#0a0c10", "#f7f9fb"),
        ("#7cc4ff", "#0969da"),
        ("#e7edf4", "#111820"),
        ("#b9c2ce", "#46515d"),
        ("#8f9baa", "#596675"),
        ("#27313c", "#d0d7de"),
    ):
        assert dark in gateway and light in gateway
        assert dark in extension and light in extension
        assert dark.removeprefix("#") in ios and light.removeprefix("#") in ios

    background = (EXTENSION / "background.js").read_text(encoding="utf-8").lower()
    assert 'const cag_accent = "#7cc4ff"' in background


def test_extension_shells_use_one_flat_brand_system() -> None:
    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    sidepanel = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")
    spark = "M12 3c.7 5.1 3.9 8.3 9 9-5.1.7-8.3 3.9-9 9-.7-5.1-3.9-8.3-9-9 5.1-.7 8.3-3.9 9-9z"

    for shell in (popup, sidepanel):
        assert '<link rel="stylesheet" href="theme.css"' in shell
        assert spark in shell
        assert "linear-gradient" not in shell
        assert "text-transform: uppercase" not in shell
        assert "letter-spacing:" not in shell


def test_legacy_warm_accents_are_gone_from_product_surfaces() -> None:
    paths = (
        ROOT / "cagentic" / "gateway.py",
        GATEWAY / "app.css",
        GATEWAY / "app.js",
        ROOT / "cagentic" / "personal_os.py",
        ROOT / "cagentic" / "projects.py",
        ROOT / "design-system" / "cagentic-gateway" / "MASTER.md",
        EXTENSION / "background.js",
        EXTENSION / "icons" / "cagentic-mark.svg",
        EXTENSION / "popup.html",
        EXTENSION / "sidepanel.html",
        EXTENSION / "theme.css",
        IOS / "DesignSystem" / "CagenticTheme.swift",
        ROOT / "iOS" / "DesignAssets" / "CagenticSpark.svg",
        ROOT / "iOS" / "DesignAssets" / "generate_app_icons.swift",
    )
    legacy = (
        "#f0a87a",
        "#c79ccf",
        "#e5928f",
        "#e6c073",
        "rgba(255,140,66",
        "rgba(255,200,120",
        "rgba(229,146,143",
        "rgba(230,192,115",
        "#c79bd8",
        "#7b4f92",
        "#c3a9c9",
        "#735c7d",
    )
    for path in paths:
        content = path.read_text(encoding="utf-8").lower()
        assert all(value not in content for value in legacy), path


def test_cli_palette_is_the_terminal_mapping_of_the_product_roles() -> None:
    assert ui.ACCENT == "\033[38;5;117m"
    assert ui.ACCENT_BRIGHT == "\033[38;5;153m"
    assert ui.STRUCTURE == "\033[38;5;67m"
    assert ui.PRIMARY_TEXT == "\033[38;5;255m"
    assert ui.SECONDARY_TEXT == "\033[38;5;248m"
    assert ui.TERTIARY_TEXT == "\033[38;5;245m"
    assert ui.WARNING == "\033[38;5;179m"
    assert ui.ERROR == "\033[38;5;174m"
    assert ui.SUCCESS == "\033[38;5;114m"

    assert ui.DUSK == ui.ACCENT
    assert ui.GLOW == ui.ACCENT_BRIGHT
    assert ui.PLUM == ui.STRUCTURE
    assert ui.GOLD == ui.WARNING
    assert ui.SURFACE == ui.PRIMARY_TEXT
    assert ui.MUTED == ui.SECONDARY_TEXT
    assert ui.SOFT == ui.TERTIARY_TEXT
    assert ui.WARN == ui.WARNING
    assert ui.ERR == ui.ERROR
    assert ui.OK == ui.SUCCESS


def test_cli_headings_keep_product_sentence_case(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    ui.heading("Browser bridge")
    assert capsys.readouterr().out.strip() == "Browser bridge"


def test_extension_manifest_uses_the_shared_toolbar_mark() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "Cagentic Browser Companion"
    assert manifest["action"]["default_title"] == "Cagentic"

    for size in (16, 32, 48, 128):
        icon = EXTENSION / manifest["icons"][str(size)]
        data = icon.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (size, size)
