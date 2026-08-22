# Cagentic app icon sources

`CagenticSpark.svg` preserves the product's exact 24-point four-point spark.
`generate_app_icons.swift` draws that geometry into opaque 1024px sRGB PNGs for
the standard, dark, and tinted iOS app-icon appearances.

Regenerate the committed PNGs from the repository root:

```bash
swift iOS/DesignAssets/generate_app_icons.swift
```

The standard and dark icons use the website's graphite-and-blue palette. The tinted
icon is intentionally grayscale because iOS applies the user-selected Home
Screen tint from its luminance values. All variants are flat artwork: no
gradients, text, glow, transparency, or pre-rounded mask.
