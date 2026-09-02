# Cagentic icon explorations

Five standalone vector marks prepared for brand review. They are deliberately
not wired into the website, extension, iOS app, CLI, manifest, or design-system
specification.

| # | Direction | Core idea | Best quality |
| --- | --- | --- | --- |
| 01 | Halo Spark | The current spark, held in a precise circular orbit. | Safest evolution; clear at all sizes. |
| 02 | Core C | An open circular C with a calm, local core. | Strongest name association. |
| 03 | Faceted Spark | A solid, geometric construction of the four-point spark. | Strongest at favicon and app-icon scale. |
| 04 | Orbit Relay | A four-segment orbit around a central diamond. | Best expression of an agent coordinating work. |
| 05 | Keystone | A structured hexagonal shell around an active core. | Most technical and infrastructure-oriented. |

All five use `currentColor` on a transparent 24×24 canvas, so the same master
can be rendered in Cagentic blue (`#7CC4FF` dark / `#0969DA` light), terminal
monochrome, or a high-contrast accessibility treatment. `icon-review.svg`
provides a side-by-side light, dark, and 16px-size review board.

The design system currently specifies the existing outline four-point spark.
Choosing a new direction would be a separate brand decision; no such change has
been made here.

## Refined iOS studies

The three user-selected directions now have refined SVG masters:

| Direction | Refinement |
| --- | --- |
| Core C | Increased its visual weight and replaced the generic dot with a diamond core. |
| Orbit Relay | Regularized the arc rhythm and opened the center for a quieter silhouette. |
| Keystone | Increased its frame weight and spacing for a less fragile iOS silhouette. |

`refined-ios-app-icon-review.svg` shows each in the standard, dark, and tinted
iOS app-icon appearances already documented by the project. It is a mockup;
none of the generated PNGs or the asset catalog has been touched.

## Approved direction: Orbit Relay

`Cagentic-OrbitRelay.svg` is the approved vector source. Running
`swift generate_orbit_relay_app_icons.swift` creates three final 1024px opaque
sRGB PNGs in `final-orbit-relay-app-icons/`:

- `Cagentic-OrbitRelay-Standard.png`
- `Cagentic-OrbitRelay-Dark.png`
- `Cagentic-OrbitRelay-Tinted.png`

These are separated from `Assets.xcassets` intentionally. Adoption into the
iOS application remains a separate, explicit change.
