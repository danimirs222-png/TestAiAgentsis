"""Generate 16x16 PNG textures for the four Lightness item variants.

The Java mod registered a color of 0xFBED13 (pale yellow) for the Lightness
effect. We use that as the liquid color in all four bottle/arrow variants and
build a simple but legible icon for each one.
"""
from PIL import Image, ImageDraw

LIQUID = (0xFB, 0xED, 0x13, 0xFF)  # matches the Java effect color exactly
LIQUID_HIGHLIGHT = (0xFF, 0xF7, 0x99, 0xFF)
GLASS = (0xC8, 0xCF, 0xE0, 0xFF)
GLASS_DARK = (0x67, 0x73, 0x8E, 0xFF)
CORK = (0x6F, 0x4D, 0x29, 0xFF)
CORK_DARK = (0x39, 0x23, 0x10, 0xFF)
TRANSPARENT = (0, 0, 0, 0)


def base_potion(splash: bool = False, lingering: bool = False) -> Image.Image:
    img = Image.new("RGBA", (16, 16), TRANSPARENT)
    px = img.load()

    # Glass bottle outline
    bottle_shape = [
        (6, 1), (7, 1), (8, 1), (9, 1),
        (6, 2), (9, 2),
        (6, 3), (9, 3),
        (5, 4), (10, 4),
        (4, 5), (11, 5),
        (3, 6), (12, 6),
        (3, 7), (12, 7),
        (3, 8), (12, 8),
        (3, 9), (12, 9),
        (3, 10), (12, 10),
        (4, 11), (11, 11),
        (5, 12), (10, 12),
        (6, 13), (7, 13), (8, 13), (9, 13),
    ]
    for x, y in bottle_shape:
        px[x, y] = GLASS_DARK

    # Glass body fill and highlight
    for y in range(2, 13):
        for x in range(4, 12):
            if (x, y) in bottle_shape:
                continue
            if 3 < x < 12 and 5 < y < 11:
                continue  # leave room for liquid
            px[x, y] = GLASS
    # Cork on top
    for x in (7, 8):
        px[x, 0] = CORK_DARK
    for x in (7, 8):
        px[x, 1] = CORK

    # Liquid fill
    for y in range(6, 11):
        for x in range(4, 12):
            if (x, y) in bottle_shape:
                continue
            px[x, y] = LIQUID
    # Liquid highlight
    for x in range(5, 7):
        px[x, 7] = LIQUID_HIGHLIGHT

    if splash:
        # Add a triangular splash mark in the corner.
        for x, y in [(13, 1), (13, 2), (14, 1), (14, 2), (12, 0), (15, 3)]:
            if 0 <= x < 16 and 0 <= y < 16:
                px[x, y] = LIQUID
    if lingering:
        # Indicate a lingering halo around the bottle.
        for x in range(2, 14):
            px[x, 14] = LIQUID
            px[x, 15] = LIQUID_HIGHLIGHT
    return img


def arrow_icon() -> Image.Image:
    img = Image.new("RGBA", (16, 16), TRANSPARENT)
    px = img.load()
    # Diagonal arrow shaft.
    for i in range(14):
        x = 1 + i
        y = 14 - i
        if 0 <= x < 16 and 0 <= y < 16:
            px[x, y] = (0x9B, 0x6B, 0x42, 0xFF)
    # Arrowhead (front)
    for x, y in [(13, 1), (14, 1), (15, 1), (14, 2), (13, 2), (12, 1)]:
        if 0 <= x < 16 and 0 <= y < 16:
            px[x, y] = LIQUID
    for x, y in [(14, 0), (15, 0)]:
        if 0 <= x < 16 and 0 <= y < 16:
            px[x, y] = LIQUID_HIGHLIGHT
    # Fletching (back)
    for x, y in [(0, 14), (0, 15), (1, 15), (1, 13)]:
        if 0 <= x < 16 and 0 <= y < 16:
            px[x, y] = (0xEE, 0xEE, 0xEE, 0xFF)
    return img


if __name__ == "__main__":
    out_dir = "/home/ubuntu/work/trmt-addon/build/trmt_rp/textures/items"
    import os
    os.makedirs(out_dir, exist_ok=True)
    base_potion().save(f"{out_dir}/potion_lightness.png")
    base_potion(splash=True).save(f"{out_dir}/splash_potion_lightness.png")
    base_potion(lingering=True).save(f"{out_dir}/lingering_potion_lightness.png")
    arrow_icon().save(f"{out_dir}/tipped_arrow_lightness.png")
    print("Wrote potion icons to", out_dir)
