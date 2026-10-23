"""生成占位用的信息卡背景图 assets/card_bg.png。

用法：python tools/make_bg.py
以后有立绘了，直接把 assets/card_bg.png 换成自己的图即可（1200x680）。
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

W, H = 1200, 680
OUT = Path(__file__).resolve().parent.parent / "assets" / "card_bg.png"


def gradient() -> Image.Image:
    base = Image.new("RGB", (W, H), (255, 247, 250))
    draw = ImageDraw.Draw(base)
    top = (255, 247, 250)
    bottom = (250, 228, 240)
    for y in range(H):
        t = y / max(1, H - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=color)
    return base


def glows() -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((560, -220, 1420, 420), fill=(255, 206, 224, 130))
    draw.ellipse((820, 300, 1580, 960), fill=(224, 208, 255, 105))
    draw.ellipse((760, 60, 1180, 480), fill=(255, 236, 246, 120))
    return layer.filter(ImageFilter.GaussianBlur(60))


def sparkles(seed: int = 7) -> Image.Image:
    random.seed(seed)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def star(cx: int, cy: int, r: int, color: tuple[int, int, int, int]) -> None:
        k = max(1, r // 3)
        points = [
            (cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
            (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k),
        ]
        draw.polygon(points, fill=color)

    for _ in range(26):
        cx = random.randint(700, W - 40)
        cy = random.randint(30, H - 30)
        star(cx, cy, random.randint(3, 8), (255, 255, 255, random.randint(120, 220)))
    for _ in range(10):
        cx = random.randint(700, W - 40)
        cy = random.randint(30, H - 30)
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=(255, 226, 238, 180))
    return layer


def main() -> None:
    image = gradient().convert("RGBA")
    image = Image.alpha_composite(image, glows())
    image = Image.alpha_composite(image, sparkles())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(OUT, "PNG", optimize=True)
    print(f"已生成 {OUT}  ({image.width}x{image.height})")


if __name__ == "__main__":
    main()
