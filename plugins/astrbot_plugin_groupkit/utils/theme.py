"""统一绘图主题：颜色、字体、度量、通用画法。

**为什么要有这一层。**
这个插件前后有三套出图方式：Pillow 手绘（信息卡、图鉴）、HTML 模板 +
Playwright 截图（签到卡、商店）、AstrBot 框架的文本转图（称号这类纯文本）。
三套意味着三种配色、三种字重、三种间距，放在同一个群里像三个插件的截图；
而且 T2I 依赖一个外部服务，那个服务一挂，签到卡就退化成纯文字。

现在全部收敛到 Pillow，图只从这里取颜色和字号。所以本模块只有两件事：
**定义变量**、**提供画法**。任何一张图都不该自己写 (255, 126, 171)。

**两套模式，不是两套风格。**
- `DARK`：封面型。有个人色彩、一次一张、要氛围 —— 信息卡、签到卡。
- `LIGHT`：清单型。密集、要扫读、一次可能好几张 —— 图鉴、商店、称号。

这不是「没统一」，而是同一个产品的两种页面：封面和目录本来就该长得不一样。
统一的是同一个粉、同一套字号阶梯、同一套圆角和留白。

**字号阶梯**（图上一律从这里挑，不要随手写 17）：
    11 脚注 / 14 标签 / 17 正文 / 21 副标题 / 26 小标题 / 34 标题 / 48 名字 / 76 大数字

渲染纯 CPU，调用方放在 `asyncio.to_thread` 里跑，别阻塞事件循环。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Pillow 是可选依赖：没装就返回 None，调用方退化文字
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖没装上
    Image = ImageDraw = ImageFilter = ImageFont = None  # type: ignore[assignment]
    PIL_AVAILABLE = False

try:  # 本地单测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

from .text import safe_text

# Pillow 10+ 推荐 Image.Resampling.LANCZOS，老版本只有 Image.LANCZOS，两个都兜住
RESAMPLE = None
if PIL_AVAILABLE:
    try:
        RESAMPLE = Image.Resampling.LANCZOS
    except AttributeError:  # pragma: no cover - 老版本 Pillow
        RESAMPLE = Image.LANCZOS


# ==================================================================== 字号阶梯
SIZE_FOOT = 11
SIZE_TAG = 14
SIZE_BODY = 17
SIZE_SUB = 21
SIZE_HEAD = 26
SIZE_TITLE = 34
SIZE_NAME = 48
SIZE_HUGE = 76


# ==================================================================== 颜色
# 一支粉贯穿全部图。深色底上用亮粉（要够亮才跳得出来），
# 浅色底上用深粉（亮粉压在白纸上是糊的，对比度不够）。
ACCENT = (255, 126, 171)
ACCENT_DEEP = (196, 72, 118)
ACCENT_TINT = (255, 238, 244)


@dataclass(frozen=True)
class Theme:
    """一套配色。名字按用途取，不按颜色取 —— 换色时不用改调用方。"""

    name: str
    canvas: tuple[int, ...]      # 页面底色（卡片外）
    card: tuple[int, ...]        # 卡片底
    ink: tuple[int, ...]         # 主文字
    ink_soft: tuple[int, ...]    # 次要文字
    ink_dim: tuple[int, ...]     # 提示文字
    line: tuple[int, ...]        # 分隔线
    zebra: tuple[int, ...]       # 隔行底色
    accent: tuple[int, ...]      # 强调（数字、标签）
    accent_fill: tuple[int, ...]  # 强调色块（带透明度）
    panel: tuple[int, ...]       # 面板底（带透明度）
    panel_line: tuple[int, ...]  # 面板描边（带透明度）
    ghost: tuple[int, ...]       # 极淡的填充（进度条底、胶囊底）


# 封面型：深色。信息卡、签到卡。
DARK = Theme(
    name="dark",
    canvas=(13, 12, 16),
    card=(20, 17, 26),
    ink=(255, 255, 255),
    ink_soft=(206, 206, 214),
    ink_dim=(150, 150, 160),
    line=(255, 255, 255, 30),
    zebra=(255, 255, 255, 14),
    accent=ACCENT,
    accent_fill=(255, 126, 171, 46),
    panel=(255, 255, 255, 26),
    panel_line=(255, 255, 255, 46),
    ghost=(255, 255, 255, 40),
)

# 清单型：浅色。图鉴、商店、称号。
LIGHT = Theme(
    name="light",
    canvas=(243, 243, 246),
    card=(255, 255, 255),
    ink=(20, 20, 24),
    ink_soft=(92, 92, 100),
    ink_dim=(136, 136, 146),
    line=(236, 236, 240),
    zebra=(250, 250, 252),
    accent=ACCENT_DEEP,
    accent_fill=(196, 72, 118, 26),
    panel=(20, 20, 24, 10),
    panel_line=(20, 20, 24, 24),
    ghost=(20, 20, 24, 24),
)

# 深色页头 / 页脚：清单型那几张图上下各压一条，把视线框住。
BAND = (17, 17, 19)
BAND_INK = (245, 245, 247)
BAND_SUB = (136, 136, 146)


# ==================================================================== 字体
# 数字用几何无衬线体：大数字要「干净利落」，中文字体做不到这一点。
DIGIT_FONTS = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

# 中文粗体（标题、名字）。配置字体只有一档字重时标题会跟正文一样细，层级全塌。
FONT_BOLD = (
    "C:/Windows/Fonts/Noto Sans SC Bold (TrueType).otf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

FONT_MEDIUM = (
    "C:/Windows/Fonts/Noto Sans SC Medium (TrueType).otf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

FONT_REGULAR = (
    "C:/Windows/Fonts/Noto Sans SC (TrueType).otf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

_WEIGHT_TABLE = {"bold": FONT_BOLD, "medium": FONT_MEDIUM, "regular": FONT_REGULAR}


class FontBook:
    """按 (字号, 字重) 缓存字体。所有出图共用一份，不再各建一份缓存。

    字重只有三档，故意不再多：`regular` 正文、`medium` 次要标签、
    `bold` 标题和名字。而 `digit` 是特殊的 —— 它走数字体，
    找不到时**退回正文体**而不是报错，少一个 arialbd 不该让整张卡画崩。
    """

    def __init__(self, custom: Path | None = None):
        self.custom = custom if (custom and custom.is_file()) else None
        self._cache: dict[tuple[int, str], Any] = {}
        self._warned = False

    def _candidates(self, weight: str) -> list[Path]:
        paths: list[Path] = []
        if weight == "digit":
            return [Path(p) for p in DIGIT_FONTS if Path(p).is_file()]
        # 用户配的字体只替补「常规体」，粗体仍走系统表：
        # 拿一档常规体去画标题，标题和正文会长得一模一样。
        if self.custom and weight == "regular":
            paths.append(self.custom)
        for item in _WEIGHT_TABLE.get(weight, FONT_REGULAR):
            path = Path(item)
            if path.is_file() and path not in paths:
                paths.append(path)
        return paths

    def get(self, size: int, weight: str = "regular"):
        key = (size, weight)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        for path in self._candidates(weight):
            try:
                font = ImageFont.truetype(str(path), size)
                self._cache[key] = font
                return font
            except Exception:
                continue

        if weight in ("digit", "bold"):
            font = self.get(size, "regular")
            self._cache[key] = font
            return font

        if not self._warned:
            self._warned = True
            logger.warning(
                "[群助手] 没找到可用中文字体，图上会掉字。"
                "Linux 装一个：apt install fonts-noto-cjk"
            )
        font = ImageFont.load_default()
        self._cache[key] = font
        return font


# 量文字宽度用的固定画布，跟输出尺寸无关
if PIL_AVAILABLE:
    _MEASURE_IMG = Image.new("RGB", (8, 8))
    _MEASURE_DRAW = ImageDraw.Draw(_MEASURE_IMG)
else:  # pragma: no cover
    _MEASURE_IMG = _MEASURE_DRAW = None


# ==================================================================== 度量
def text_width(text: str, font: Any, draw: Any = None) -> int:
    """量一段文字画出来有多宽。

    `draw` 参数保留只是为了兼容旧调用点；有固定画布就不需要真的 draw。
    """
    if not PIL_AVAILABLE or not text:
        return 0
    target = draw if draw is not None else _MEASURE_DRAW
    try:
        return int(target.textlength(text, font=font))
    except Exception:  # pragma: no cover
        return int(len(text) * getattr(font, "size", 12) * 0.6)


def fit_text(
    book: FontBook,
    text: str,
    size: int,
    max_width: float,
    weight: str = "regular",
    min_size: int = 15,
    draw: Any = None,
) -> tuple[str, Any]:
    """先缩字号，缩到底还放不下就截断加省略号。

    优先级是「缩字号 > 截断」：截断会丢信息，缩小只是难看一点点。
    中文没有空格可以断行，长昵称只能这样收。
    """
    text = safe_text(text)
    font = book.get(size, weight)
    if text_width(text, font, draw) <= max_width:
        return text, font
    while size > min_size:
        size -= 1
        font = book.get(size, weight)
        if text_width(text, font, draw) <= max_width:
            return text, font
    cut = text
    while cut and text_width(cut + "…", font, draw) > max_width:
        cut = cut[:-1]
    return (cut + "…") if cut else "", font


def clip_text(book: FontBook, text: str, font: Any, max_width: float, draw: Any = None) -> str:
    """定字号下的截断。用于副标题这类不允许缩字的位置。"""
    text = safe_text(text)
    if text_width(text, font, draw) <= max_width:
        return text
    cut = text
    while cut and text_width(cut + "…", font, draw) > max_width:
        cut = cut[:-1]
    return (cut + "…") if cut else ""


def wrap_text(
    book: FontBook, text: str, font: Any, max_width: float, max_lines: int = 3
) -> list[str]:
    """按宽度把一段话折成几行。中文按字断，英文数字按词断。

    签到卡的寄语、欢迎语都要折行，而 Pillow 没有自动换行。
    """
    text = safe_text(text)
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        probe = current + char
        if text_width(probe, font) <= max_width:
            current = probe
            continue
        if current:
            lines.append(current)
        current = char
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1]
        while tail and text_width(tail + "…", font) > max_width:
            tail = tail[:-1]
        lines[-1] = tail + "…"
    return lines


# ==================================================================== 图像变换
def cover(image: "Image.Image", width: int, height: int) -> "Image.Image":
    """等比缩放到刚好铺满，多余的部分裁掉。

    比直接 resize 好：直接 resize 会把竖图压扁、横图拉长，人脸尤其明显。
    cover 只丢边角，不改变形状。
    """
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", (width, height), (16, 16, 20))
    scale = max(width / src_w, height / src_h)
    new_w = max(width, int(src_w * scale + 0.5))
    new_h = max(height, int(src_h * scale + 0.5))
    image = image.resize((new_w, new_h), RESAMPLE)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return image.crop((left, top, left + width, top + height))


def center_square(image: "Image.Image") -> "Image.Image":
    """居中裁成正方形，给头像框用。"""
    w, h = image.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return image.crop((left, top, left + side, top + side))


def rounded_mask(size: int, radius: int) -> "Image.Image":
    """4 倍超采样的圆角矩形遮罩，边缘才不会有锯齿。"""
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=radius * 4, fill=255
    )
    return mask.resize((size, size), RESAMPLE)


def vertical_shade(
    image: "Image.Image",
    top: tuple[int, int, int, int],
    bottom: tuple[int, int, int, int],
    curve: float = 0.85,
) -> "Image.Image":
    """从上到下压一层渐变遮罩，保证任何底图上白字都读得清。

    `curve` 小于 1 时上段压得慢（留出底纹颜色）、下段收得快（页脚压住）。
    """
    width, height = image.size
    veil = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(veil)
    for y in range(height):
        t = (y / max(1, height - 1)) ** curve
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(4))
        draw.line([(0, y), (width, y)], fill=color)
    return Image.alpha_composite(image.convert("RGBA"), veil).convert("RGB")


def glow_background(
    width: int,
    height: int,
    base_top: tuple[int, int, int],
    base_bottom: tuple[int, int, int],
    glows: tuple[tuple[tuple[int, int, int, int], tuple[int, int, int, int]], ...] = (),
    blur: int = 80,
) -> "Image.Image":
    """深色渐变底 + 几团柔光。

    不能给纯黑：纯黑上的白字像 PPT 模板。低饱和光斑让底子「有点空气」，
    又不至于抢文字。`glows` 是 ((box), (rgba)) 的序列。
    """
    base = Image.new("RGB", (width, height), base_top)
    draw = ImageDraw.Draw(base)
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(
                int(base_top[i] + (base_bottom[i] - base_top[i]) * ratio) for i in range(3)
            ),
        )
    if not glows:
        return base
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    for box, rgba in glows:
        ldraw.ellipse(box, fill=rgba)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


def blur_cover(
    image: "Image.Image", width: int, height: int, radius_ratio: float = 0.03
) -> "Image.Image":
    """把一张图铺满画布再模糊，当底纹用。

    半径按画布宽的比例算：太小能看出原图（抢字），太大就成了一块纯色。
    """
    big = cover(image, width, height)
    try:
        return big.filter(ImageFilter.GaussianBlur(width * radius_ratio))
    except Exception:
        return big


# ==================================================================== 通用组件
def draw_pill(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    text: str,
    font: Any,
    theme: Theme = DARK,
    height: int = 44,
    pad_x: int = 22,
) -> int:
    """描边胶囊，返回宽度。称号、状态标签用它 —— 是图上唯一「炫耀性」的元素。"""
    width = text_width(text, font)
    total = width + pad_x * 2
    box = (x, y, x + total, y + height)
    draw.rounded_rectangle(box, radius=height // 2, fill=theme.accent_fill)
    draw.rounded_rectangle(box, radius=height // 2, outline=theme.accent, width=1)
    # 用 textbbox 算垂直居中，不依赖 font.size —— load_default() 的位图字体没有这个属性
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        text_y = y + (height - (bottom - top)) // 2 - top
    except Exception:  # pragma: no cover
        text_y = y + 10
    draw.text((x + pad_x, text_y), text, font=font, fill=theme.accent)
    return total


def draw_panel(
    draw: "ImageDraw.ImageDraw",
    box: tuple[int, int, int, int],
    theme: Theme = DARK,
    radius: int = 28,
) -> None:
    """一块半透明面板。主数据用它装，跟背景拉开一层。"""
    draw.rounded_rectangle(
        box, radius=radius, fill=theme.panel, outline=theme.panel_line, width=1
    )


def draw_progress(
    draw: "ImageDraw.ImageDraw",
    box: tuple[int, int, int, int],
    ratio: float,
    theme: Theme = DARK,
    radius: int = 5,
) -> None:
    """细进度条。底槽永远画，填充够宽才画圆角（太窄会糊成一团）。"""
    left, top, right, bottom = box
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=theme.ghost)
    width = right - left
    filled = int(width * max(0.0, min(1.0, ratio)))
    if filled >= 10:
        draw.rounded_rectangle((left, top, left + filled, bottom), radius=radius, fill=theme.accent)


# ==================================================================== 输出
def save_png(image: "Image.Image", path: Path) -> Path | None:
    """统一存 PNG。

    不存 JPEG 是有原因的：线上出现过「图片发出去 rich media 上传失败」，
    而同样内容的 PNG 一直正常。PNG 无损、体积也可控（这些图都是纯色块）。
    """
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path, "PNG", optimize=True)
        return Path(path)
    except Exception as exc:  # pragma: no cover
        logger.error(f"[群助手] 保存图片失败 {path}: {exc}")
        return None


def cleanup_dir(directory: Path, pattern: str, max_age: int = 3600, keep: int = 0) -> None:
    """清掉过期的临时图，避免 data 目录无限长大。

    `keep` 是「最新的 N 个无论如何都留着」—— 图鉴有内存缓存，
    缓存还活着图却被删了，下一次就发出一个死路径。
    """
    now = time.time()
    try:
        files = sorted(
            Path(directory).glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except Exception:
        return
    for index, path in enumerate(files):
        if index < keep:
            continue
        try:
            if now - path.stat().st_mtime > max_age:
                path.unlink(missing_ok=True)
        except Exception:
            continue


def temp_name(prefix: str, key: str | int = "") -> str:
    """带时间戳的临时文件名。同毫秒内不同 QQ 号也不会互相覆盖。"""
    stamp = int(time.time() * 1000)
    return f"{prefix}_{stamp}_{key}.png" if key != "" else f"{prefix}_{stamp}.png"
