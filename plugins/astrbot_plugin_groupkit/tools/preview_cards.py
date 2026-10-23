"""生成多张不同人的信息卡，用来验证「同模板、不同数据」不会串也不会崩。

用法：python tools/preview_cards.py
输出：tools/out/card_*.png

这不是插件运行时代码，只是本地看版式的工具。
"""

from __future__ import annotations

import importlib.util
import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"

spec = importlib.util.spec_from_file_location("gk_card", ROOT / "utils" / "card.py")
assert spec and spec.loader
card_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(card_mod)


def fake_avatar(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    """造一张像头像的图：竖向渐变 + 一个浅色圆，方便看圆形裁切效果。"""
    size = 640
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / (size - 1)
        draw.line(
            [(0, y), (size, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    draw.ellipse(
        (size * 0.18, size * 0.1, size * 0.82, size * 0.74),
        fill=tuple(min(255, c + 55) for c in bottom),
    )
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


PERSONAS = [
    (
        "新人",
        {
            "name": "月下🌸",
            "gender": "保密",
            "hobby": "无",
            "quota": 5,
            "days": 1,
            "today": 2,
            "month": 2,
            "total": 2,
            "qq": "10001",
            "show_qq": False,
            "currency": "额度",
        },
        None,  # 故意不给头像，验证首字占位
    ),
    (
        "老成员",
        {
            "name": "一只名字特别特别长的水群选手",
            "gender": "女",
            "hobby": "打游戏、听歌、看番、摸鱼、发呆、写代码、养猫",
            "quota": 128000,
            "days": 365,
            "today": 999,
            "month": 12345,
            "total": 987654,
            "qq": "10002",
            "show_qq": False,
            "currency": "额度",
        },
        fake_avatar((255, 214, 226), (198, 178, 246)),
    ),
    (
        "管理员",
        {
            "name": "阿澜",
            "gender": "男",
            "hobby": "篮球",
            "quota": 0,
            "days": 42,
            "today": 0,
            "month": 0,
            "total": 12,
            "qq": "10003",
            "show_qq": True,
            "currency": "额度",
        },
        fake_avatar((188, 226, 255), (128, 168, 232)),
    ),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="card_preview_"))
    renderer = card_mod.CardRenderer(
        ROOT, "assets/card_bg.png", "assets/fonts/LXGWWenKai-Medium.ttf", tmp
    )
    for label, data, avatar in PERSONAS:
        path = renderer.render(data, avatar)
        if not path:
            print(f"[FAIL] {label} 渲染失败")
            continue
        target = OUT / f"card_{label}.png"
        shutil.copyfile(path, target)
        print(f"{label}: {target}  (头像: {'有' if avatar else '无，走首字占位'})")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
