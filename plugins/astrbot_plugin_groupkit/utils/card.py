"""个人信息卡渲染。

一张图搞定：左侧半透明信息板（标题 / 名牌 / 若干行信息 + 分隔线），右侧留给背景立绘。
背景图、字体都可配置；换掉 assets/card_bg.png 就是换皮，不用改代码。

渲染是纯 CPU 运算，调用方应放在 asyncio.to_thread 里跑，别阻塞事件循环。
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

try:  # 本地单测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

# Pillow 10+ 推荐 Image.Resampling.LANCZOS，老版本只有 Image.LANCZOS，两个都兜住
try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 老版本 Pillow
    RESAMPLE = Image.LANCZOS

# QQ 名片里塞满 emoji 是常态，但图片字体画出来是豆腐块，统一清掉
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 表情、符号、补充符号
    "\U00002600-\U000027BF"   # 杂项符号、装饰符号
    "\U0001F1E6-\U0001F1FF"   # 区域指示符（国旗）
    "\U00002B00-\U00002BFF"   # 杂项符号与箭头
    "\U0000FE00-\U0000FE0F"   # 变体选择符
    "\U0000200D"              # 零宽连接符
    "\U000020E3"              # 组合用键帽
    "]+"
)


def safe_text(text: Any, fallback: str = "") -> str:
    """清掉 emoji 和控制符，避免渲染成方块。"""
    cleaned = _EMOJI_RE.sub("", str(text or ""))
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch == " ")
    return cleaned.strip() or fallback

CARD_W, CARD_H = 1200, 680
PANEL = (44, 36, 668, 644)          # 左面板 left, top, right, bottom
PANEL_RADIUS = 30
PANEL_PAD = 52

TITLE_TEXT = "个人信息卡"
TITLE_SIZE = 48
TITLE_COLOR = (178, 84, 122)
RULE_COLOR = (226, 150, 180)
DOT_COLOR = (232, 138, 172)

NAME_PILL_TOP = 176
NAME_PILL_H = 62
PILL_COLOR = (232, 138, 170)

ROWS_TOP = 276
ROW_GAP = 52
LABEL_X = 142          # 相对面板左侧内边距
VALUE_X = 300
SEP_COLOR = (238, 170, 194, 80)

LABEL_COLOR = (176, 132, 152)
VALUE_COLOR = (48, 34, 46)
HILITE_COLOR = (206, 92, 134)     # 额度那一行用主题色强调

# 右侧头像
AVATAR_CX, AVATAR_CY, AVATAR_R = 936, 340, 196
AVATAR_MARGIN = 14               # 白圈宽度

# 系统字体兜底顺序（Windows / Linux / macOS 常见中文字体）
FALLBACK_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


class CardRenderer:
    def __init__(self, plugin_dir: Path, bg_path: str, font_path: str, tmp_dir: Path):
        self.plugin_dir = Path(plugin_dir)
        self.bg_path = self._resolve(bg_path)
        self.font_path = self._resolve(font_path)
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._bg_cache: Image.Image | None = None
        self._font_cache: dict[int, Any] = {}

    # ------------------------------------------------------------ 资源解析
    def _resolve(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = self.plugin_dir / path
        return path if path.is_file() else None

    def _load_font(self, size: int):
        if size in self._font_cache:
            return self._font_cache[size]
        candidates: list[Path] = []
        if self.font_path:
            candidates.append(self.font_path)
        candidates.extend(Path(p) for p in FALLBACK_FONTS)
        for path in candidates:
            if not path.is_file():
                continue
            try:
                font = ImageFont.truetype(str(path), size)
                self._font_cache[size] = font
                return font
            except Exception:
                continue
        logger.warning("[群助手] 没找到可用中文字体，信息卡将使用默认字体")
        font = ImageFont.load_default()
        self._font_cache[size] = font
        return font

    def _background(self) -> Image.Image:
        if self._bg_cache is not None:
            return self._bg_cache
        if self.bg_path:
            try:
                image = Image.open(self.bg_path).convert("RGB")
                if image.size != (CARD_W, CARD_H):
                    image = image.resize((CARD_W, CARD_H), RESAMPLE)
                self._bg_cache = image
                return image
            except Exception as exc:
                logger.error(f"[群助手] 读取信息卡背景失败: {exc}")
        self._bg_cache = self._default_background()
        return self._bg_cache

    @staticmethod
    def _default_background() -> Image.Image:
        """没有背景图时的兜底：粉紫渐变 + 两团柔光。"""
        base = Image.new("RGB", (CARD_W, CARD_H), (255, 245, 249))
        draw = ImageDraw.Draw(base)
        for y in range(CARD_H):
            ratio = y / CARD_H
            draw.line(
                [(0, y), (CARD_W, y)],
                fill=(int(255 - 6 * ratio), int(243 - 18 * ratio), int(247 - 6 * ratio)),
            )
        glow = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        gdraw.ellipse((620, -160, 1360, 520), fill=(255, 214, 228, 90))
        gdraw.ellipse((820, 300, 1500, 900), fill=(226, 214, 255, 80))
        return Image.alpha_composite(base.convert("RGBA"), glow).convert("RGB")

    # ------------------------------------------------------------ 绘制
    def render(self, data: dict[str, Any], avatar_bytes: bytes | None = None) -> Path | None:
        try:
            image = self._background().convert("RGBA")
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            self._draw_panel(draw)
            self._draw_title(draw)
            self._draw_name_pill(draw, str(data.get("name") or "未命名"))
            self._draw_rows(draw, data)

            image = Image.alpha_composite(image, overlay)
            # 头像画在最上层，失败时自动退回首字占位
            image = self._draw_avatar(image, avatar_bytes, str(data.get("name") or ""))

            self._cleanup()
            out = self.tmp_dir / f"card_{int(time.time() * 1000)}_{data.get('qq', '0')}.png"
            image.convert("RGB").save(out, "PNG", optimize=True)
            return out
        except Exception as exc:
            logger.error(f"[群助手] 渲染信息卡失败: {exc}", exc_info=True)
            return None

    # ------------------------------------------------------------ 头像
    def _draw_avatar(
        self, image: Image.Image, avatar_bytes: bytes | None, name: str
    ) -> Image.Image:
        cx, cy, r = AVATAR_CX, AVATAR_CY, AVATAR_R

        # 投影：稍微下移的柔和椭圆
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse(
            (cx - r - 10, cy - r + 16, cx + r + 10, cy + r + 16), fill=(150, 96, 124, 55)
        )
        image = Image.alpha_composite(image, layer)

        # 白圈 + 内衬
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse(
            (cx - r - AVATAR_MARGIN, cy - r - AVATAR_MARGIN,
             cx + r + AVATAR_MARGIN, cy + r + AVATAR_MARGIN),
            fill=(255, 255, 255, 238),
        )
        d.ellipse((cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6), fill=(255, 238, 245, 255))
        image = Image.alpha_composite(image, layer)

        avatar = self._decode_avatar(avatar_bytes)
        if avatar is None:
            return self._draw_avatar_placeholder(image, name)

        # 居中裁成正方形
        w, h = avatar.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        avatar = avatar.crop((left, top, left + side, top + side))

        diameter = r * 2
        avatar = avatar.resize((diameter, diameter), RESAMPLE)

        # 4 倍超采样做圆形遮罩，边缘才不会有锯齿
        big = diameter * 4
        mask_big = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask_big).ellipse((0, 0, big - 1, big - 1), fill=255)
        mask = mask_big.resize((diameter, diameter), RESAMPLE)

        image.paste(avatar, (cx - r, cy - r), mask)
        return image

    @staticmethod
    def _decode_avatar(avatar_bytes: bytes | None) -> Image.Image | None:
        if not avatar_bytes:
            return None
        try:
            return Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        except Exception as exc:
            logger.warning(f"[群助手] 头像解码失败，改用首字占位: {exc}")
            return None

    def _draw_avatar_placeholder(self, image: Image.Image, name: str) -> Image.Image:
        """没有头像时用名字首字画一个圆形占位，别留一块空白。"""
        cx, cy, r = AVATAR_CX, AVATAR_CY, AVATAR_R
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(236, 150, 182, 255))

        char = safe_text(name, "?")[:1] or "?"
        font = self._load_font(int(r * 0.95))
        width = d.textlength(char, font=font)
        bbox = d.textbbox((0, 0), char, font=font)
        height = bbox[3] - bbox[1]
        d.text((cx - width / 2, cy - height / 2 - bbox[1]), char, font=font, fill=(255, 255, 255))
        return Image.alpha_composite(image, layer)

    def _draw_panel(self, draw: ImageDraw.ImageDraw) -> None:
        # 外发光：底部偏移一点的浅粉，制造一点立体感
        draw.rounded_rectangle(
            (PANEL[0] + 2, PANEL[1] + 8, PANEL[2] + 2, PANEL[3] + 8),
            radius=PANEL_RADIUS, fill=(214, 150, 180, 46),
        )
        draw.rounded_rectangle(
            PANEL, radius=PANEL_RADIUS, fill=(255, 255, 255, 168), outline=(255, 255, 255, 215), width=2
        )

    def _draw_title(self, draw: ImageDraw.ImageDraw) -> None:
        font = self._load_font(TITLE_SIZE)
        x, y = PANEL[0] + PANEL_PAD, 78
        draw.text((x, y), TITLE_TEXT, font=font, fill=TITLE_COLOR)
        width = int(draw.textlength(TITLE_TEXT, font=font))

        line_y = 154
        draw.line([(x, line_y), (x + width, line_y)], fill=RULE_COLOR, width=3)
        # 线尾的小星，代替生硬的圆点
        cx, cy, r = x + width + 22, line_y, 9
        draw.polygon(
            [(cx, cy - r), (cx + 3, cy - 3), (cx + r, cy), (cx + 3, cy + 3),
             (cx, cy + r), (cx - 3, cy + 3), (cx - r, cy), (cx - 3, cy - 3)],
            fill=DOT_COLOR,
        )

    def _draw_name_pill(self, draw: ImageDraw.ImageDraw, name: str) -> None:
        raw = safe_text(name, "未命名")
        name = raw if len(raw) <= 10 else raw[:10] + "…"
        size = 34
        font = self._load_font(size)
        text_width = int(draw.textlength(name, font=font))
        # 名字太长就先缩字号，再不行截断，保证不溢出名牌
        while text_width > 360 and size > 24:
            size -= 2
            font = self._load_font(size)
            text_width = int(draw.textlength(name, font=font))
        width = max(184, min(430, text_width + 66))
        left = PANEL[0] + PANEL_PAD
        box = (left, NAME_PILL_TOP, left + width, NAME_PILL_TOP + NAME_PILL_H)

        # 名牌阴影
        draw.rounded_rectangle(
            (box[0] + 1, box[1] + 4, box[2] + 1, box[3] + 4),
            radius=NAME_PILL_H // 2, fill=(214, 132, 162, 70),
        )
        draw.rounded_rectangle(box, radius=NAME_PILL_H // 2, fill=PILL_COLOR)
        text_y = box[1] + (NAME_PILL_H - size) // 2 - 4
        draw.text((box[0] + (width - text_width) / 2, text_y), name, font=font, fill=(255, 255, 255))

    def _draw_icon(self, draw: ImageDraw.ImageDraw, kind: str, cx: float, cy: float) -> None:
        """在徽章里画一个 14px 左右的小矢量图标。

        用图形而不是文字，是因为竖着读一列汉字容易读成别的词；
        图形没有这个问题，也顺带避开了字体缺字。
        """
        w = (255, 255, 255, 255)
        if kind == "person":
            draw.ellipse((cx - 3, cy - 7.5, cx + 3, cy - 1.5), fill=w)
            draw.arc((cx - 6, cy + 1.5, cx + 6, cy + 13.5), 180, 360, fill=w, width=2)
        elif kind == "heart":
            # 两个圆做心耳 + 一个朝下的尖，比单三角形像心得多
            draw.ellipse((cx - 7, cy - 6.5, cx + 0.4, cy + 1.2), fill=w)
            draw.ellipse((cx - 0.4, cy - 6.5, cx + 7, cy + 1.2), fill=w)
            draw.polygon(
                [(cx - 6.2, cy - 1.6), (cx + 6.2, cy - 1.6), (cx, cy + 7)], fill=w
            )
        elif kind == "coin":
            draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=w, width=2)
            draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=w)
        elif kind == "calendar":
            draw.rounded_rectangle((cx - 5.5, cy - 4.5, cx + 5.5, cy + 6), radius=2, outline=w, width=2)
            draw.line([(cx - 5.5, cy - 0.5), (cx + 5.5, cy - 0.5)], fill=w, width=2)
            draw.line([(cx - 3, cy - 8), (cx - 3, cy - 3.5)], fill=w, width=2)
            draw.line([(cx + 3, cy - 8), (cx + 3, cy - 3.5)], fill=w, width=2)
        elif kind == "clock":
            draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=w, width=2)
            draw.line([(cx, cy), (cx, cy - 3.4)], fill=w, width=2)
            draw.line([(cx, cy), (cx + 2.8, cy + 1.2)], fill=w, width=2)
        elif kind == "chart":
            for offset, height in ((-4.5, 5), (0, 9), (4.5, 12.5)):
                draw.line([(cx + offset, cy + 6), (cx + offset, cy + 6 - height)], fill=w, width=2)
        elif kind == "star":
            r, k = 6.5, 2.2
            draw.polygon(
                [(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                 (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)],
                fill=w,
            )
        else:  # 兜底：小圆点
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=w)

    def _fit_text(
        self, draw: ImageDraw.ImageDraw, text: str, size: int, max_width: float, min_size: int = 18
    ) -> tuple[str, Any]:
        """先缩字号，缩到底还放不下就截断加省略号。保证任何数据都不溢出面板。"""
        font = self._load_font(size)
        if draw.textlength(text, font=font) <= max_width:
            return text, font
        while size > min_size:
            size -= 2
            font = self._load_font(size)
            if draw.textlength(text, font=font) <= max_width:
                return text, font
        truncated = text
        while truncated and draw.textlength(truncated + "…", font=font) > max_width:
            truncated = truncated[:-1]
        return (truncated + "…") if truncated else "", font

    def _draw_rows(self, draw: ImageDraw.ImageDraw, data: dict[str, Any]) -> None:
        currency = safe_text(data.get("currency"), "额度")
        rows = [
            ("person", "性别", safe_text(data.get("gender"), "保密"), False),
            ("heart", "爱好", safe_text(data.get("hobby"), "无"), False),
            ("coin", currency, str(data.get("quota", 0)), True),
            ("calendar", "在册", f"{data.get('days', 1)} 天", False),
            ("clock", "今日", f"{data.get('today', 0)} 条", False),
            ("chart", "本月", f"{data.get('month', 0)} 条", False),
            ("star", "累计", f"{data.get('total', 0)} 条", False),
        ]
        if data.get("show_qq"):
            rows.append(("tag", "QQ", str(data.get("qq") or ""), False))

        x0 = PANEL[0] + PANEL_PAD
        sep_right = PANEL[2] - PANEL_PAD
        label_width = VALUE_X - LABEL_X - 12
        value_width = sep_right - (x0 + VALUE_X)

        # 行数会变（开了 card_show_qq 就多一行），行距按面板剩余高度自适应，
        # 保证永远不顶出面板。block = 最后一行可用的纵向空间（已扣掉行高和底部留白）
        block = PANEL[3] - 12 - ROWS_TOP - 34
        row_gap = ROW_GAP if len(rows) <= 1 else min(ROW_GAP, int(block / (len(rows) - 1)))

        for index, (icon, label, value, highlight) in enumerate(rows):
            y = ROWS_TOP + index * row_gap
            if index > 0:  # 行间分隔线
                draw.line([(x0 + 46, y - 9), (sep_right, y - 9)], fill=SEP_COLOR, width=1)

            draw.rounded_rectangle(
                (x0, y + 2, x0 + 32, y + 34), radius=10, fill=(238, 158, 186, 235)
            )
            self._draw_icon(draw, icon, x0 + 16, y + 18)

            label_text, label_font = self._fit_text(draw, label, 28, label_width, 22)
            draw.text((x0 + LABEL_X, y + 3), label_text, font=label_font, fill=LABEL_COLOR)

            value_text, value_font = self._fit_text(draw, value, 28, value_width, 20)
            draw.text(
                (x0 + VALUE_X, y + 3), value_text, font=value_font,
                fill=HILITE_COLOR if highlight else VALUE_COLOR,
            )

    def _cleanup(self, max_age: int = 3600) -> None:
        """清掉一小时前的临时卡片，避免堆积。"""
        now = time.time()
        try:
            for path in self.tmp_dir.glob("card_*.png"):
                if now - path.stat().st_mtime > max_age:
                    path.unlink(missing_ok=True)
        except Exception:
            pass
