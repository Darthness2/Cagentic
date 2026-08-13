"""Phase 5 regressions — the web UI's design system.

Phase 2 made the browser surface capable; it still looked like a hobby project
next to claude.ai and chatgpt.com. The causes were measurable, and so are the
fixes, so they are asserted here rather than left to whoever next edits a
1,600-line stylesheet with no tests:

  body/message type 12px      -> 16px, the single loudest signal
  93 of 142 size rules <=11px -> a six-step token scale, nothing under 12px
  39 uppercase + 112 tracking -> none (the HUD/sci-fi tell)
  eleven radius values        -> four, assigned by role
  emoji and HTML entities     -> one inline SVG sprite
  --accent-glow/--orb-halo    -> retired with the particle orb and the clock

These are guard rails, not style policing: each one is a specific regression
that had a visible cost.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_ASSETS = Path(__file__).resolve().parents[1] / "cagentic" / "gateway_assets"
_CSS = (_ASSETS / "app.css").read_text(encoding="utf-8")
_JS = (_ASSETS / "app.js").read_text(encoding="utf-8")
_HTML = (_ASSETS / "index.html").read_text(encoding="utf-8")

# The :root blocks legitimately hold literals; rules must not.
_RULES = _CSS[_CSS.index("* { box-sizing") :]


class TestTypeScale(unittest.TestCase):
    def test_the_message_body_is_readable_size(self) -> None:
        """A chat product is a reading product. 12px is dashboard density."""
        self.assertIn("--fs-base: 16px;", _CSS)
        body = re.search(r"^\.msg-body \{[^}]*\}", _CSS, re.M).group(0)
        self.assertIn("font-size: var(--fs-base)", body)
        self.assertIn("line-height: var(--lh-body)", body)

    def test_the_user_turn_matches_the_reply(self) -> None:
        """Two different reading sizes in one transcript is just an error."""
        bubble = re.search(r"^\.msg-row\.user \.bubble \{\n[^}]*\}", _CSS, re.M).group(0)
        self.assertIn("font-size: var(--fs-base)", bubble)

    def test_no_rule_sets_type_below_twelve_pixels(self) -> None:
        """34 rules used to sit at 9px."""
        tiny = [int(n) for n in re.findall(r"font-size: (\d+)px", _RULES) if int(n) < 12]
        self.assertEqual(tiny, [], f"sub-12px type: {tiny}")

    def test_the_font_shorthand_does_not_smuggle_sizes_back_in(self) -> None:
        """`font: 9px var(--mono)` bypassed the font-size sweep entirely."""
        self.assertEqual(re.findall(r"font: \d+px", _RULES), [])

    def test_a_reading_measure_is_enforced(self) -> None:
        """Line length is the other half of readability; the transcript used to
        run the full window width."""
        self.assertIn("--measure:", _CSS)
        self.assertIn("max-width: var(--measure)", _CSS)

    def test_the_legacy_monospace_alias_is_documented_not_load_bearing(self) -> None:
        """--mono named the UI face, a leftover from the all-monospace era."""
        self.assertIn("--ui:", _CSS)


class TestNoHUDVocabulary(unittest.TestCase):
    def test_nothing_is_uppercased(self) -> None:
        """Uppercase letterspaced micro-labels are the sci-fi tell; both
        competitors use essentially none."""
        self.assertEqual(_CSS.count("text-transform: uppercase"), 0)

    def test_body_and_label_tracking_is_gone(self) -> None:
        """112 rules carried positive tracking. Only negative tracking on
        display type survives."""
        kept = set(re.findall(r"letter-spacing: ([^;]+);", _CSS))
        self.assertTrue(all(v.startswith("-") for v in kept), kept)

    def test_nothing_glows(self) -> None:
        """Light bleed reads as a screensaver, and smudges type on light."""
        self.assertEqual(_CSS.count("text-shadow"), 0)

    def test_the_halo_and_grid_tokens_are_retired(self) -> None:
        for token in ("--orb-halo-1", "--orb-halo-2", "--grid:"):
            self.assertNotIn(token, _CSS, token)

    def test_the_clock_is_gone(self) -> None:
        """A wall clock told the user nothing about their conversation."""
        self.assertNotIn("updateClock", _JS)
        self.assertNotIn('id="jClock"', _HTML)

    def test_the_particle_orb_is_gone(self) -> None:
        """220 animated particles, ~40% of the viewport, and a permanently
        running animation loop, before a single message."""
        self.assertNotIn("initOrb", _JS)
        self.assertNotIn("orbCanvas", _HTML)
        self.assertNotIn("orbCanvas", _JS)

    def test_the_terminal_cosplay_is_gone(self) -> None:
        self.assertNotIn("&gt;_", _HTML)  # the `>_` composer prompt
        self.assertNotIn('content: "> "', _CSS)  # the `>` on every user turn
        self.assertNotIn("[ CONFIG ]", _HTML)  # bracket-wrapped nav

    def test_the_header_is_one_row(self) -> None:
        """Two stacked chrome bars above a conversation is heavy."""
        self.assertNotIn('<div class="nav-bar">', _HTML)


class TestRadiusAndSpacingScales(unittest.TestCase):
    def test_radius_is_a_four_step_scale(self) -> None:
        """Eleven arbitrary values is why corners looked arbitrary."""
        for token in ("--r-control:", "--r-card:", "--r-sheet:", "--r-pill:"):
            self.assertIn(token, _CSS, token)

    def test_rules_use_the_radius_tokens_rather_than_raw_values(self) -> None:
        raw = re.findall(r"border-radius: (\d+)px", _RULES)
        # A couple of hairline values (1-2px on marks/scrollbars) are fine.
        self.assertLessEqual(len([r for r in raw if int(r) > 2]), 2, raw)

    def test_a_spacing_scale_exists(self) -> None:
        for token in ("--s-1:", "--s-4:", "--s-7:"):
            self.assertIn(token, _CSS, token)

    def test_elevation_tokens_exist_for_both_themes(self) -> None:
        """Light needs softer shadows; one set would look wrong in one theme."""
        self.assertGreaterEqual(_CSS.count("--e-1:"), 3)


class TestIconSystem(unittest.TestCase):
    def test_a_sprite_is_defined_inline(self) -> None:
        """No external requests — the gateway ships its own assets."""
        self.assertIn('<symbol id="i-send"', _HTML)
        self.assertIn('<symbol id="i-settings"', _HTML)

    def test_no_emoji_are_used_as_icons(self) -> None:
        """Emoji render differently on every platform and sit at a different
        weight and colour than everything around them."""
        for emoji in ("🔍", "🖥", "📊", "📈", "📝", "⏰", "📂", "🎛"):
            self.assertNotIn(emoji, _JS, emoji)

    def test_the_entity_glyphs_are_gone(self) -> None:
        # ✕ close, ✎ edit, 🗑 delete, ⋮ menu, ► caret, ⚡ tool
        for entity in ("&#10005;", "&#9998;", "&#128465;", "&#8942;", "&#9654;", "&#9889;"):
            self.assertNotIn(entity, _HTML, entity)
            self.assertNotIn(entity, _JS, entity)

    def test_icons_inherit_colour_and_stroke(self) -> None:
        ico = re.search(r"\.ico \{[^}]*\}", _CSS).group(0)
        self.assertIn("stroke: currentColor", ico)


class TestThemeIntegrity(unittest.TestCase):
    """Phase 2.5 routed ~40 literals through variables to fix a light-theme
    bug; that work must not be undone, and the same class of bug had crept
    back into the modal (a hardcoded #151118 rendered dark on a light page)."""

    def test_rules_do_not_hardcode_the_dark_surface(self) -> None:
        self.assertNotIn("#151118", _RULES)
        self.assertNotIn("#1e1828", _RULES)

    def test_rules_do_not_hardcode_the_accent_tint(self) -> None:
        """Literal mauve washes rendered dark purple on white."""
        leaks = re.findall(r"rgba\(199,155,216,[^)]*\)", _RULES)
        self.assertEqual(leaks, [], leaks)

    def test_both_light_theme_blocks_stay_in_step(self) -> None:
        """The palette is declared twice — for data-theme=light and for
        data-theme=auto under a media query. A token added to one and not the
        other is a silent half-broken theme."""
        blocks = re.findall(r"--stage-bg: #fdfbfe;(.*?)\n\}", _CSS, re.S)
        self.assertEqual(len(blocks), 2)
        keys = [set(re.findall(r"(--[\w-]+):", b)) for b in blocks]
        self.assertEqual(keys[0], keys[1])

    def test_muted_text_meets_wcag_aa(self) -> None:
        """--text-dim measured 3.40:1 in both themes, below the 4.5:1 floor,
        and it carries timestamps, hints and metadata people actually read."""

        def lum(h: str) -> float:
            h = h.lstrip("#")
            chans = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
            return 0.2126 * f(chans[0]) + 0.7152 * f(chans[1]) + 0.0722 * f(chans[2])

        def ratio(a: str, b: str) -> float:
            la, lb = lum(a), lum(b)
            return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

        dark = re.search(r"--text-dim: (#\w+);", _CSS).group(1)
        light = re.findall(r"--text-dim: (#\w+);", _CSS)[1]
        self.assertGreaterEqual(round(ratio(dark, "#151118"), 2), 4.5, dark)
        self.assertGreaterEqual(round(ratio(light, "#f7f4f9"), 2), 4.5, light)


class TestCodeBlockEscaping(unittest.TestCase):
    """Found during the restyle: the number rule matched the digits inside the
    `&#39;` that `esc()` produced for an apostrophe, split the entity, and any
    code containing a quote rendered as literal `&#39;`."""

    def test_code_bodies_use_the_text_escape(self) -> None:
        self.assertIn("function escText(", _JS)
        self.assertIn("highlight(escText(clean),lang)", _JS)

    def test_the_text_escape_leaves_quotes_alone(self) -> None:
        """Only & < > are significant inside a <pre><code> text node."""
        fn = _JS[_JS.index("function escText(") : _JS.index("function highlight(")]
        self.assertNotIn("&#39;", fn)
        self.assertNotIn("&quot;", fn)

    def test_it_still_escapes_the_dangerous_characters(self) -> None:
        fn = _JS[_JS.index("function escText(") : _JS.index("function highlight(")]
        for entity in ("&amp;", "&lt;", "&gt;"):
            self.assertIn(entity, fn, entity)

    def test_the_attribute_escape_is_unchanged(self) -> None:
        """data-raw is an attribute and still needs full escaping."""
        self.assertIn("data-raw=\"'+esc(clean)+'\"", _JS)

    def test_the_highlighter_rules_match_real_quotes(self) -> None:
        """They were written against escaped text; feeding them unescaped
        quotes without updating them would silently stop highlighting."""
        rules = _JS[_JS.index("function _hlRules(") : _JS.index("function escText(")]
        self.assertNotIn("&quot;", rules)


class TestReducedMotion(unittest.TestCase):
    def test_entry_animations_are_disabled_on_request(self) -> None:
        """Cards and messages animate in; opacity:0 with the animation
        suppressed would leave them invisible."""
        block = _CSS[_CSS.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertIn("prefers-reduced-motion", block)
        self.assertIn("opacity: 1 !important", block)


if __name__ == "__main__":
    unittest.main()
