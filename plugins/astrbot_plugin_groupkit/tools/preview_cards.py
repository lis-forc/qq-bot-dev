"""生成几张不同的人 / 不同名片的信息卡，验证「同模板、不同数据」不串不崩。

用法：python tools/preview_cards.py
输出：tools/out/card_*.png

四件事必须能看到，否则这个预览没意义：

1. **按性别挑底图**：没写性别的人要走男版，写了女的人才走女版；
2. **买的名片压过基础名片**：花过额度的那张要真的换上去；
3. **超长昵称 + 超长爱好 + 十二位额度**会被怎么收（最容易溢出的一档）；
4. **左侧白幕**：底图右侧是插画，字只能待在左边的白区里 ——
   自定义皮肤故意做成「右侧有图」，白幕铺得对不对一眼能看见。

底图来自 `assets/images/cards/`，预览会造一个临时素材目录：把真实的
男/女两张复制进去，再补两张假皮肤，走的是和管理台、商店完全同一条链路。

这不是插件运行时代码，只是本地看版式的工具。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"

# 以包的形式导入：card.py 用相对导入共享 utils/text 与 utils/meta，
# 按文件单独加载会报 "attempted relative import with no known parent package"。
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_groupkit.utils import card as card_mod  # noqa: E402
from astrbot_plugin_groupkit.utils.assets import ImageBank  # noqa: E402


def fake_skin(name: str, tint: tuple[int, int, int]) -> Path:
    """造一张「左白右图」的假名片，模仿真实底图的构图。

    右侧画一块色板当插画：白幕要是没铺对，字会直接压在上面。
    """
    folder = OUT / "cards"
    folder.mkdir(parents=True, exist_ok=True)
    width, height = 1280, 720
    image = Image.new("RGB", (width, height), (252, 251, 250))
    draw = ImageDraw.Draw(image)
    # 右侧的「插画」：两块错开的圆 + 一条斜线，够乱就行
    draw.ellipse((760, -120, 1360, 520), fill=tint)
    draw.ellipse((900, 260, 1420, 780), fill=tuple(min(255, c + 26) for c in tint))
    draw.line([(700, 720), (1280, 80)], fill=(255, 255, 255), width=18)
    path = folder / f"{name}.png"
    image.save(path, "PNG")
    return path


BASE = {
    "show_qq": False,
    "currency": "额度",
    "draw_per": 100,
    "draw_progress": 0,
    "draws": 0,
    "title": "",
}

PERSONAS = [
    (
        "新人",
        dict(BASE, name="月下🌸", gender="男", hobby="", quota=5, days=1, qq="10001",
             draw_progress=12),
        "",
    ),
    (
        "老成员",
        dict(
            BASE,
            name="一只名字特别特别长的水群选手",
            gender="女",
            hobby="打游戏、听歌、看番、摸鱼、发呆、写代码、养猫",
            quota=128000,
            days=365,
            qq="10002",
            draw_progress=37,
            draws=3,
            title="老面孔",
        ),
        "樱花",
    ),
    (
        "选保密",
        dict(BASE, name="阿澜", gender="保密", hobby="篮球", quota=0, days=42, qq="10003",
             draws=2, title="常客"),
        "夜空",
    ),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    card_dir = OUT / "cards"
    card_dir.mkdir(parents=True, exist_ok=True)
    # 先清掉上一轮的假图，否则皮肤表里会混着旧名字
    for stale in card_dir.glob("*.png"):
        stale.unlink()

    # 真实的男女基础名片复制进来（预览必须和线上看到的一致）
    real = ROOT / "assets" / "images" / "cards"
    for name in ("男", "女"):
        source = real / f"{name}.png"
        if source.is_file():
            shutil.copy2(source, card_dir / f"{name}.png")
    fake_skin("樱花@50", (232, 200, 214))
    fake_skin("夜空@80", (86, 100, 138))

    bank = ImageBank(OUT, ROOT, default_skin_price=50)
    print("在售名片：", ", ".join(f"{s.name}({s.price})" for s in bank.skins()) or "（无）")
    print("基础名片：", "男" if bank.base_card("男") else "缺", "/", "女" if bank.base_card("女") else "缺")

    renderer = card_mod.CardRenderer(ROOT, "assets/fonts/LXGWWenKai-Medium.ttf", OUT, bank)
    for label, data, skin in PERSONAS:
        data = dict(data, skin=skin)
        path = renderer.render(data)
        if not path:
            print(f"[FAIL] {label} 渲染失败")
            continue
        target = OUT / f"card_{label}.png"
        target.write_bytes(Path(path).read_bytes())
        print(
            f"{label}: {target}"
            f"  (性别 {data['gender']} / 名片 {skin or '基础名片'})"
        )


if __name__ == "__main__":
    main()
