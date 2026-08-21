# Gateway workspace override

This page-specific direction overrides the older dashboard-oriented guidance in
`../MASTER.md`. The gateway is a conversation workspace, not a cybersecurity
dashboard or landing page.

## Product direction

- Use an AI-native, content-first shell with minimal chrome.
- Keep one persistent 260px navigation rail on wide screens and an off-canvas
  drawer on smaller screens.
- Keep the conversation header quiet: current model, conversation title, and
  one overflow menu for voice, appearance, and settings.
- Keep the reading and composer column at 768px with 16px message copy.
- On an empty chat, order content as greeting, the one live composer, then up
  to three quiet suggestion rows. Visual order and keyboard order must match.
- In an active chat, keep the transcript scrollable and the composer anchored
  below it.
- Put durable charts, tables, previews, and specialty widgets in one docked
  output workspace. It shares the desktop canvas at wide widths, overlays at
  intermediate widths, and becomes a full-screen sheet below 768px.

## Visual language

- Preserve the CLI-inspired neutral identity and dual light/dark themes: near-black
  or white foundations, layered bubbles and shadows, and one cool-blue accent.
- Prefer spacing, flat surfaces, and subtle state layers over bordered cards,
  glows, gradients, or decorative dashboard elements.
- Use the existing inline SVG sprite and four radius tokens. Accent color is
  reserved for brand, focus, selection, and the primary send action.
- Motion is functional and subtle (120-240ms) with reduced-motion support.

## Interaction and accessibility

- Keep every mobile target at least 44px and every input at least 16px.
- Preserve one composer DOM node; never clone its textarea, attachments, or
  controls for the landing state.
- Sidebar disclosures, chat actions, dialogs, and model selection remain fully
  keyboard operable with visible focus and correct focus return.
- Output cards are ordinary labelled regions with keyboard-operable close and
  restore controls; they never require pointer drag or resize gestures.
- On phones, newly generated outputs wait until the turn finishes before the
  modal sheet opens, keeping Stop and approval controls reachable.
- Empty content must scroll on short or zoomed viewports rather than being
  absolutely positioned or clipped.
