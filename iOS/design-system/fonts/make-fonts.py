"""Instance the canonical Google Fonts variable files down to exactly the
weights the app asks for. Shipping a variable font would carry 300-800 when the
theme only ever names four of them."""
import os
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

# family dir name -> (source file, postscript family, [(weight, style name)])
JOBS = [
    ("src/SpaceGrotesk.ttf", "SpaceGrotesk", "Space Grotesk", False,
     [(400, "Regular"), (500, "Medium"), (600, "SemiBold"), (700, "Bold")]),
    ("src/PlusJakartaSans.ttf", "PlusJakartaSans", "Plus Jakarta Sans", False,
     [(400, "Regular"), (500, "Medium"), (600, "SemiBold"), (700, "Bold")]),
    ("src/PlusJakartaSans-It.ttf", "PlusJakartaSans", "Plus Jakarta Sans", True,
     [(400, "Italic")]),
    ("src/JetBrainsMono.ttf", "JetBrainsMono", "JetBrains Mono", False,
     [(400, "Regular")]),
]

# The four subfamilies the classic name table allows. Anything else has to be
# folded into the family name or macOS resolves it to the wrong face.
CLASSIC = {"Regular", "Bold", "Italic", "Bold Italic"}
os.makedirs("out", exist_ok=True)

for source, ps_family, display_family, italic, weights in JOBS:
    for weight, style in weights:
        font = TTFont(source)
        axes = {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes}
        lo, hi = axes["wght"]
        assert lo <= weight <= hi, f"{source}: wght {weight} outside {lo}-{hi}"
        instancer.instantiateVariableFont(font, {"wght": weight}, inplace=True)

        ps_name = f"{ps_family}-{style.replace(' ', '')}"
        if style in CLASSIC:
            family, subfamily = display_family, style
        else:
            family, subfamily = f"{display_family} {style}", "Regular"

        name = font["name"]
        for text, ids in (
            (family, [1, 21]),
            (subfamily, [2, 22]),
            (f"{display_family} {style}", [3, 4]),
            (ps_name, [6]),
            (display_family, [16]),
            (style, [17]),
        ):
            for nid in ids:
                name.setName(text, nid, 3, 1, 0x409)   # Windows/Unicode
                name.setName(text, nid, 1, 0, 0)       # Macintosh/Roman

        # OS/2 and head must agree with the name table or UIKit picks synthetic
        # bold over the real face.
        os2, head = font["OS/2"], font["head"]
        os2.usWeightClass = weight
        os2.fsSelection = (os2.fsSelection & ~0b1100001) | (
            (0b0100000 if style == "Bold" else 0)
            | (0b0000001 if italic else 0)
            | (0b1000000 if style == "Regular" else 0)
        )
        head.macStyle = (0b01 if style == "Bold" else 0) | (0b10 if italic else 0)

        out = f"out/{ps_name}.ttf"
        font.save(out)
        print(f"{out:44} {os.path.getsize(out)//1024:>5} KB  wght={weight}")
