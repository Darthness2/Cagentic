# Cagentic app icon sources

The app icon is generated, not drawn by hand. Its geometry lives in
[`design-system/brand/make-icons.py`](../../design-system/brand/make-icons.py), which draws the
same mark into every surface that carries it: the three iOS app-icon appearances, the
browser-extension icons, and the web favicon.

Regenerate the committed PNGs from the repository root:

```bash
python3 design-system/brand/make-icons.py
```

There used to be a second generator here, a Swift one that drew the previous four-point spark from
its own copy of the geometry. It went when the mark changed: a generator that still emits the old
artwork is worse than none, because running it silently undoes the change.

Only the default appearance is opaque. iOS draws its own backdrop behind the dark and tinted
variants and expects to be handed the artwork alone, so those two are transparent — which is also
why the tinted one is drawn in white: the system builds its tint from luminance, and a light
background would take the colour and leave the ring as a hole punched in it.

All variants are flat artwork: no gradients, text, glow, or pre-rounded mask.

`ThemeStudies/` is a record of earlier explorations and is not wired into anything.
