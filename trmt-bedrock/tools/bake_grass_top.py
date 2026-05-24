"""Bake the eroded-grass overlay textures onto a neutral grey grass-top base
so the resulting combined textures can be tinted at runtime (tint_method: grass)
and rendered as a single top face per stage.

The base texture is procedurally generated to look like grass speckle when
multiplied by a biome colour: many pixels in the 110-160 brightness band with
small noise, no green bias. The eroded overlay is then alpha-blended on top.
"""
from PIL import Image
import random
import os

random.seed(20251101)


def generate_grass_base() -> Image.Image:
    """Procedural noise base similar to Java's grass_block_top: a roughly grey
    texture that tints to a believable grass colour when multiplied."""
    img = Image.new("RGBA", (16, 16))
    px = img.load()
    for y in range(16):
        for x in range(16):
            # Cluster of speckle blobs to imitate grass tufts.
            n1 = random.randint(110, 160)
            jitter = random.randint(-8, 8)
            val = max(0, min(255, n1 + jitter))
            px[x, y] = (val, val, val, 255)
    return img


def overlay(base: Image.Image, top: Image.Image) -> Image.Image:
    """Alpha-composite top onto base, returning a fully opaque result."""
    out = base.copy()
    out.alpha_composite(top)
    return out


def main() -> None:
    blocks_dir = "/home/ubuntu/work/trmt-addon/build/trmt_rp/textures/blocks"
    base = generate_grass_base()
    base.save(os.path.join(blocks_dir, "trmt_grass_top_base.png"))
    for stage in range(5):
        eroded = Image.open(os.path.join(blocks_dir, f"eroded_grass_block_top_{stage}.png")).convert("RGBA")
        combined = overlay(base, eroded)
        combined.save(os.path.join(blocks_dir, f"trmt_eroded_grass_top_combined_{stage}.png"))
    print("Baked 5 combined grass-top textures")


if __name__ == "__main__":
    main()
