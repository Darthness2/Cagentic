# Cagentic iOS brand specification

This native specification adapts `design-system/cagentic-gateway/MASTER.md` for
SwiftUI. The app uses the existing Cagentic identity while keeping the transcript
and composer as quiet and direct as the best native AI chat apps.

## Selected direction

The original Quiet Graphite iPhone direction established the app's compact,
content-first structure. Its production palette now follows the shipped website:
neutral graphite layers with one cool-blue accent. This keeps the native app
recognizably Cagentic beside the gateway, CLI, and browser extension while
preserving the polished iPhone interaction model.

## Brand

- Name: **Cagentic** in sentence case.
- Mark: the existing four-point spark, recreated as a native SwiftUI shape.
- Character: calm, private, capable, and content-first.
- Voice: concise and reassuring; errors explain the next useful action.

## Color

| Token | Light | Dark | Purpose |
| --- | --- | --- | --- |
| Accent | `#0969DA` | `#7CC4FF` | Focus, selection, primary send action |
| Accent soft | `#EAF2FB` | `#132636` | Selected rows and quiet highlights |
| On accent | `#FFFFFF` | `#06101A` | Primary-action foreground |
| Background | `#F7F9FB` | `#0A0C10` | App canvas |
| Stage | `#FFFFFF` | `#06080B` | Transcript reading surface |
| Surface | `#FFFFFF` | `#10141A` | Composer, sheets, elevated controls |
| Surface raised | `#F0F4F8` | `#14191F` | User bubbles and selected controls |
| Text primary | `#111820` | `#E7EDF4` | Main copy |
| Text secondary | `#46515D` | `#B9C2CE` | Metadata and supporting copy |
| Text tertiary | `#596675` | `#8F9BAA` | De-emphasized metadata |
| Border | `#D0D7DE` | `#27313C` | Structural separation |
| Success | `#2F7D43` | `#8ECF95` | Connected and complete |
| Warning | `#8A6D10` | `#D9C069` | Insecure or degraded state |
| Error | `#B03A35` | `#D98A87` | Failures and destructive actions |

There are no gradients, glow, neon, or decorative secondary accents. Status is
always expressed with text or a symbol in addition to color.

## Typography and spacing

- Display, UI, body, and metadata: Inter Variable through Dynamic Type-aware
  semantic SwiftUI text styles.
- Weights: regular for reading, medium for labels, semibold for navigation and
  hierarchy, and bold only for major display text.
- Code samples remain system monospaced because preserving code alignment is a
  content requirement rather than app chrome.
- Inter 4.1 is bundled under the SIL Open Font License 1.1; its license ships
  beside the font resources.
- Spacing scale: 4, 8, 12, 16, 24, 32, 48 points.
- Radii: 8 controls, 12 message surfaces, 16 composer and sheets.
- Every interactive target is at least 44 by 44 points.

## Screen signatures

- Sidebar: recent conversations, a slim labelled connection state, and one clear new-chat action.
- Empty chat: the live composer and useful prompt rows, not a marketing hero.
- Active chat: assistant responses read like a document; user messages use one
  subdued raised surface.
- Generation: the spark gently breathes only while the model is working and
  becomes static when Reduce Motion is enabled.
- Settings: connection security is explicit and the bearer token is stored only
  in Keychain.

## Accessibility and release bar

- Dynamic Type, VoiceOver labels, system focus, Reduce Motion, and Reduce
  Transparency are first-class inputs.
- Touch targets are 44 points minimum.
- Body contrast targets WCAG AA in both appearances.
- Empty, loading, offline, streaming, cancelled, and failed states are visible.
- Review every primary view on iPhone in light and dark appearance. iPad layout
  refinement is intentionally deferred during the current design phase.
