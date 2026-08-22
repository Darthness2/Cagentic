# Cagentic iOS design review

Reviewed against `brand-spec.md` on an iPhone 17 simulator in light and dark
appearances. The active transcript and an Accessibility Dynamic Type size were
exercised as running app states rather than static mockups. iPad refinement is
intentionally outside the current design phase.

## Scorecard

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Visual hierarchy | 9/10 | Transcript and composer remain primary; model, history, and metrics are progressively disclosed. |
| Composition | 8/10 | The phone uses a focused reading column, restrained message surfaces, and quiet composer chrome. |
| Typography | 9/10 | Dynamic Type-aware Inter styles carry display, UI, body, and metadata; literal code alone remains monospaced. |
| Color | 9/10 | Gateway Graphite tokens remain consistent in both appearances, with cool blue reserved for interaction and identity. |
| Craft | 9/10 | Streaming, thinking, failure, cancellation, copy/share, model metadata, connection guidance, and platform navigation states are complete. |
| Accessibility | 9/10 | Semantic snapshots expose labelled controls, targets are at least 44 points, Reduce Motion is respected, and the largest Dynamic Type size remains operable and scrollable. |

## Review outcome

All dimensions clear the 7/10 release bar. The largest Dynamic Type pass led to
one concrete refinement: transcript actions and composer controls reflow into
vertical groups at Accessibility sizes, while message content and metadata keep
their full semantic scaling. The visible safety note shortens in that layout but
retains its complete accessibility label.

No gradients, decorative secondary accents, glow, HUD motifs, emoji branding, or
competing hero surfaces were introduced.
