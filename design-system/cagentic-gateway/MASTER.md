# Cagentic product design system

This is the shared visual contract for the CLI, web gateway, and browser extension.
Page-specific guidance in `pages/` may refine layout, but it must not replace these
brand, color, typography, state, or accessibility rules.

## Product character

Cagentic is a calm local AI workspace. It should feel capable and precise without
looking like a security console, a game HUD, or a generic purple SaaS dashboard.

- Content first, with quiet product chrome.
- Flat, layered surfaces instead of decorative gradients or glow.
- One dusk-mauve brand hue. Green, amber, and red are reserved for meaning.
- Native to each medium: the web and extension use system UI controls; the CLI
  uses terminal-safe color and compact text structure.
- Light and dark themes are designed together.

## Identity

### Name and mark

- Product name: **Cagentic** (sentence case; never all caps or letter-spaced).
- Mark: the four-point spark used by the gateway's `i-spark` symbol.
- Web and extension surfaces use the same 1.6-1.8px outline SVG.
- The CLI may use the monochrome `✦` text equivalent where SVG is impossible.
- Pair the mark with the wordmark in primary product chrome. Do not put the mark
  in a gradient tile or substitute a letter avatar.

### Icon language

- Use 24px outline icons, 1.6px stroke, round caps and joins.
- Icons inherit `currentColor` and never introduce a second brand color.
- Use semantic symbols or accompanying text for status so color is not the only cue.

## Color

| Role | Dark | Light | Terminal 256-color mapping |
|---|---|---|---|
| Background | `#151118` | `#f7f4f9` | terminal background |
| Stage / deepest surface | `#120f16` | `#fdfbfe` | terminal background |
| Elevated surface | `rgba(255,255,255,.028)` | `rgba(0,0,0,.022)` | default background |
| Primary text | `#f0eaf2` | `#221c27` | `255` |
| Secondary text | `#a99fb1` | `#5d5366` | `248` |
| Tertiary text | `#8a8292` | `#6f6a7c` | `245` |
| Border | `#332b38` | `#ded5e4` | `96` when structural color is needed |
| Brand / focus | `#c79bd8` | `#7b4f92` | `176` primary, `182` bright |
| On brand | `#151118` | `#ffffff` | terminal background |
| Success | `#8ecf95` | `#2f7d43` | `114` |
| Warning | `#d9c069` | `#8a6d10` | `179` |
| Error | `#d98a87` | `#b03a35` | `174` |

Rules:

- Brand color is for the mark, focus, selection, active navigation, and the one
  primary action in a view.
- Do not use peach, blue, cyan, or a second purple as decorative accents.
- Do not use gradients for brand marks, buttons, panels, or backgrounds.
- Status colors always accompany a label, icon, or symbol.

## Typography

- UI: `-apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif`.
  Do not fetch web fonts; the gateway is local and buildless.
- Code: `ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace`.
- Base web reading copy is 16px with a 1.6-1.7 line height and a maximum 72ch measure.
- Product chrome uses 12/13/14/16/18/22/28px roles. Avoid text below 12px.
- Use weight and spacing for hierarchy. Avoid uppercase transforms, decorative
  letter spacing, text shadows, and overly bold body copy.
- CLI headings keep the caller's sentence case and use indentation plus color for
  hierarchy rather than all-caps labels.

## Layout tokens

- Spacing: 4, 8, 12, 16, 24, 32, 48px.
- Radii: 8px controls, 12px cards, 16px sheets, pill only for true pills.
- Icon controls: at least 44x44px on touch layouts; at least 36x36px in compact
  desktop extension chrome.
- Borders separate adjacent regions. Shadows are reserved for floating layers.
- Motion: 120ms for local feedback, 240ms for disclosure/sheet movement, using a
  calm ease-out curve. Respect reduced motion everywhere.

## Cross-surface mapping

### Web gateway

- This is the fullest expression of the system: persistent chat navigation,
  content-first conversation, one live composer, and dual themes.
- Keep the reading column at 72ch and avoid dashboard density.

### Browser extension

- The popup is a compact connection and permission surface, not a miniature
  analytics dashboard.
- The side panel shell uses the same mark, stage colors, type hierarchy, and
  control treatment as the gateway inside its frame.
- Assistant activity injected into a page uses the same mauve accent.

### CLI

- Preserve terminal conventions and redirected-output safety.
- Use the spark wordmark, mauve brand markers, neutral text, and the same semantic
  success/warning/error palette.
- No boxes around ordinary conversation. Use short rules, indentation, and a
  stable marker vocabulary (`●`, `·`, `→`, `✓`, `×`, `!`).

## Interaction and accessibility

- Every interactive element has native semantics, a visible focus state, and a
  descriptive accessible name.
- Labels are real `<label for>` relationships.
- Visual and keyboard order match.
- Disabled and loading states are explicit and prevent duplicate actions.
- Minimum contrast: 4.5:1 for normal text and 3:1 for large text/UI graphics.
- Never communicate success, warning, or failure by color alone.
- Reduced-motion mode removes looping or decorative animation.

## Do not use

- Gradients, glassmorphism, ambient blobs, neon edges, scanlines, or HUD styling.
- Emoji as structural icons.
- Multiple brand accent colors.
- Tiny uppercase metadata or arbitrary radii/spacing.
- Hover transforms that move layout.
- Dark-only extension states or light themes produced by simple color inversion.

## Release checks

- Compare gateway, popup, side panel, and CLI output side by side.
- Verify dark and light web/extension themes independently.
- Verify 320/375px narrow layouts and a desktop gateway viewport.
- Verify keyboard focus, labels, reduced motion, offline/loading/disabled states,
  and no horizontal overflow.
- Run frontend contract tests plus terminal UI tests before shipping.
