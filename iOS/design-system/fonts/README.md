# Bundled type

The app ships three families, chosen by what the text is rather than where it
sits — see `design-system/brand-spec.md` for the split.

| Role | Family | Faces bundled |
| --- | --- | --- |
| Chrome | Space Grotesk | Regular, Medium, SemiBold, Bold |
| Conversation | Plus Jakarta Sans | Regular, Medium, SemiBold, Bold, Italic |
| Code and output | JetBrains Mono | Regular |

All three are SIL Open Font License 1.1; each license ships next to the fonts in
`Cagentic/Resources/Fonts/`.

## Why static faces and not the variable fonts

Google Fonts publishes these three only as variable fonts, which carry the whole
weight axis — 300–700 for Space Grotesk, 200–800 for Plus Jakarta Sans, 100–800
for JetBrains Mono. The theme names ten faces and never asks for anything else,
so `make-fonts.py` pins the `wght` axis at each of those ten values and drops the
variation tables. The result is smaller than the two Inter variable files it
replaced (952 KB against 1.79 MB) and contains nothing the app cannot reach.

`TypographyTests` asserts the set both ways: every face the theme can name is
registered, and nothing is registered that the theme cannot name. Both directions
matter, because `UIFont(name:)` returns nil for a missing face and every call
site then falls back to the system font without any visible error.

## Regenerating

Needs `fonttools` (not a project dependency — this is a one-off tool):

```bash
python3 -m pip install fonttools
```

Fetch the upstream variable fonts and their licenses:

```bash
base=https://raw.githubusercontent.com/google/fonts/main/ofl
mkdir -p src && cd src
curl -sSLo SpaceGrotesk.ttf        "$base/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf"
curl -sSLo PlusJakartaSans.ttf     "$base/plusjakartasans/PlusJakartaSans%5Bwght%5D.ttf"
curl -sSLo PlusJakartaSans-It.ttf  "$base/plusjakartasans/PlusJakartaSans-Italic%5Bwght%5D.ttf"
curl -sSLo JetBrainsMono.ttf       "$base/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf"
```

Then instance and install:

```bash
python3 make-fonts.py
```

```bash
cp out/*.ttf ../../Cagentic/Resources/Fonts/
```

Adding or removing a face means updating `UIAppFonts` in
`Cagentic/Resources/Info.plist` to match, or `TypographyTests` fails.
