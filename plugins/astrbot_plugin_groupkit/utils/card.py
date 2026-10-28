"""个人信息卡：把字排到别人做好的名片底图上。

这一版只有一件事要做 —— **排字**。底图是设计好的成品（`assets/images/cards/`），
1280×720，左边留白、右边一个人物。代码不画框、不画渐变、不糊头像，只负责
把六项信息按设计规范摆进左边那片白：

    姓名      最大的一行，一张卡只有一个视觉重心
    性别      ┐
    爱好      │ 五行「标签 + 值」，格式模仿群主给的参考模板：
    额度      │ 每行一个小灰标签，值跟在右边的固定列上
    注册天数  │
    关系      ┘

**为什么去掉头像**：名片底图本身就是一张「人像」，再贴一个圆形头像就是
一张图上两个人。而且头像要走网络下载，失败率高、还慢 —— 一个只为了
「证明是本人」的小图，代价是每次查卡都要多一次可能超时的请求。

**为什么铺一层白幕**：底图是群主自己塞进来的，谁也不知道左边到底白不白。
铺一层由左向右淡出的白幕（纯白区看不出来，花图区会被柔化成可读的浅底），
深色文字就永远读得清 —— 这比「要求底图必须留白」现实得多。

颜色、字体、度量全部来自 `utils/theme.py`，本文件只管「摆哪儿」。
渲染是纯 CPU 运算，调用方应放在 asyncio.to_thread 里跑，别阻塞事件循环。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import theme as T
from .text import safe_text

try:  # 本地单测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

# 兼容旧调用点（自测/预览脚本读这两个常量）
PIL_AVAILABLE = T.PIL_AVAILABLE
Image = T.Image
ImageDraw = T.ImageDraw

# ---------------------------------------------------------------- 画布
# 画布尺寸 = 底图尺寸。底图是按 1280×720 排好版的，改这个数等于把
# 人家留好的白区挤变形 —— 所以它不是一个「可调参数」，是底图的规格。
CARD_W, CARD_H = 1280, 720

MARGIN = 88             # 左边距
TEXT_RIGHT = 596        # 文字区右边界：两张底图的人物都在 600 之后
TEXT_W = TEXT_RIGHT - MARGIN

# 白幕宽度：到 700 就已经完全透明了，比文字区宽一截，淡出才看不出边界。
SCRIM_W = 700

# 浅色底用浅色主题：ink 是近黑，accent 是深粉（亮粉压白纸上是糊的）
TONE = T.LIGHT

# ---- 纵向节奏。写死坐标是有意的：这是一张固定版式的卡，不是流式文档，
# 拆成「上一个元素的高度 + 间距」反而更难看出整张卡的呼吸是否均匀。
LABEL_Y = 76            # 顶部小标
NAME_Y = 124            # 姓名
NAME_SIZE = 56
RULE_Y = 204            # 分隔线
ROW_TOP = 244           # 第一行（性别）标签的 y
ROW_STEP = 74           # 行距：五行铺到 540，离页脚还有一屏呼吸
VALUE_COL = MARGIN + 148  # 值那一列的左边，五行的值全对齐在这一条线上
FOOT_Y = 650            # 页脚


class CardRenderer:
    def __init__(self, plugin_dir: Path, font_path: str, tmp_dir: Path, images: Any = None):
        self.plugin_dir = Path(plugin_dir)
        self.font_path = self._resolve(font_path)
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.book = T.FontBook(self.font_path)
        # 底图缓存按路径分：一个人换了名片，不能因为缓存还留着旧图。
        # 上限 8 张 —— 一个群里同时活跃的名片不会更多，再多就该考虑内存了。
        self._base_cache: dict[str, Any] = {}
        # ImageBank 允许为 None（自测里不建素材目录）：那时用程序画的浅色底。
        self.images = images
        if not T.PIL_AVAILABLE:
            logger.warning(
                "[群助手] 未安装 Pillow，信息卡退化为纯文字。"
                "装上 Pillow 并重启 AstrBot 即可恢复图片（pip install Pillow）"
            )

    # ------------------------------------------------------------ 资源
    def _resolve(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = self.plugin_dir / path
        return path if path.is_file() else None

    def _font(self, size: int, weight: str = "regular"):
        return self.book.get(size, weight)

    def _card_base(self, skin_name: str, gender: str) -> Path | None:
        """该用哪张底图：买的皮肤 → 按性别的基础名片 → None。"""
        if self.images is None:
            return None
        try:
            return self.images.resolve_card(skin_name, gender)
        except Exception as exc:  # 素材目录权限之类的问题，不该让整张卡画不出来
            logger.warning(f"[群助手] 解析名片失败，退回默认底: {exc}")
            return None

    # ------------------------------------------------------------ 背景
    def _background(self, skin_name: str, gender: str) -> Any:
        """底图 + 左侧白幕。"""
        path = self._card_base(skin_name, gender)
        base = self._base_image(path) if path else self._fallback_base()
        return self._scrim(base)

    def _base_image(self, path: Path) -> Any:
        """底图：cover 裁到画布大小，不拉伸、不模糊。

        不模糊是有意的：底图是设计好的成品，糊一下只会把插画的线条
        和留白的干净度一起毁掉，而字画在留白上本来就不需要它变糊。
        """
        key = str(path)
        cached = self._base_cache.get(key)
        if cached is not None:
            return cached
        try:
            image = T.Image.open(path).convert("RGB")
        except Exception as exc:
            logger.error(f"[群助手] 读取名片底图失败 {path}: {exc}")
            return self._fallback_base()
        rendered = T.cover(image, CARD_W, CARD_H)
        if len(self._base_cache) >= 8:
            self._base_cache.clear()
        self._base_cache[key] = rendered
        return rendered

    @staticmethod
    def _fallback_base() -> Any:
        """一张底图都没有时的兜底：干净的浅色渐变，别让卡开天窗。"""
        return T.glow_background(
            CARD_W,
            CARD_H,
            (250, 249, 247),
            (238, 236, 242),
            glows=(
                ((760, -200, 1500, 520), (244, 226, 232, 255)),
                ((-200, 420, 640, 1100), (232, 230, 240, 220)),
            ),
            blur=90,
        )

    @staticmethod
    def _scrim(image: Any) -> Any:
        """左侧铺一层由白到透明的横向渐变。

        纯白区铺上去看不出任何区别；万一群主换了一张满图的花底，
        文字也会被柔化成可读的浅底，而不是糊在插画上。
        """
        scrim = T.Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = T.ImageDraw.Draw(scrim)
        for x in range(SCRIM_W):
            # 前 55% 几乎全白，之后快速淡出 —— alpha 线性下降会看到一条边。
            ratio = x / SCRIM_W
            alpha = int(236 * max(0.0, 1 - ratio) ** 1.7)
            if alpha <= 0:
                break
            draw.line([(x, 0), (x, image.height)], fill=(255, 255, 255, alpha))
        return T.Image.alpha_composite(image.convert("RGBA"), scrim)

    # ------------------------------------------------------------ 主流程
    def render(self, data: dict[str, Any]) -> Path | None:
        if not T.PIL_AVAILABLE:
            return None
        try:
            image = self._background(
                str(data.get("skin") or ""), str(data.get("gender") or "")
            )
            layer = T.Image.new("RGBA", image.size, (0, 0, 0, 0))
            draw = T.ImageDraw.Draw(layer)

            self._draw_label(draw, data)
            self._draw_name(draw, data)
            self._draw_rows(draw, data)
            self._draw_footer(draw, data)

            image = T.Image.alpha_composite(image, layer)

            T.cleanup_dir(self.tmp_dir, "card_*.png", max_age=3600)
            out = self.tmp_dir / T.temp_name("card", data.get("qq", "0"))
            return T.save_png(image.convert("RGB"), out)
        except Exception as exc:
            logger.error(f"[群助手] 渲染信息卡失败: {exc}", exc_info=True)
            return None

    # ------------------------------------------------------------ 小标
    def _draw_label(self, draw: Any, data: dict[str, Any]) -> None:
        """顶部一行：左边一句身份，右边在册天数。

        不放「个人信息卡」四个大字 —— 卡上有名字有额度，谁都知道这是什么。
        那行位置用来写在册多久，是这个群里唯一有份量、又只有一行能表达的数据。
        """
        draw.rectangle((MARGIN, LABEL_Y + 11, MARGIN + 22, LABEL_Y + 12), fill=TONE.accent)
        draw.text(
            (MARGIN + 36, LABEL_Y), "MEMBER CARD", font=self._font(15, "bold"), fill=TONE.ink_dim
        )


    # ------------------------------------------------------------ 姓名
    def _draw_name(self, draw: Any, data: dict[str, Any]) -> None:
        name = safe_text(data.get("name"), "未命名")
        text, font = T.fit_text(self.book, name, NAME_SIZE, TEXT_W, "bold", 32, draw)
        draw.text((MARGIN, NAME_Y), text, font=font, fill=TONE.ink)

    # ------------------------------------------------------------ 五行
    def _draw_rows(self, draw: Any, data: dict[str, Any]) -> None:
        """五行「标签 + 值」。标签同一列、值同一列，是这版式的骨架。

        版式模仿群主给的参考模板：每行一个小灰标签，值跟在右边的固定列上，
        一眼从上扫到下。值缺省时给占位文案而不是空着 —— 空行看起来像 bug。
        """
        draw.rectangle((MARGIN, RULE_Y, TEXT_RIGHT, RULE_Y + 1), fill=(20, 20, 24, 32))

        label_font = self._font(16)
        value_font = self._font(26)
        quota = int(data.get("quota") or 0)

        rows = [
            ("性别", safe_text(data.get("gender"), "未设定"), "text"),
            ("爱好", safe_text(data.get("hobby"), "还没写"), "text"),
            (safe_text(data.get("currency"), "额度"), f"{quota:,}", "quota"),
            ("注册天数", self._days_text(data), "text"),
            ("关系", safe_text(data.get("relation"), "无"), "text"),
        ]
        for index, (label, value, kind) in enumerate(rows):
            y = ROW_TOP + index * ROW_STEP
            draw.text((MARGIN, y), label, font=label_font, fill=TONE.ink_dim)
            if kind == "quota":
                # 额度是这五项里唯一值得强调的数字：品牌色 + 数字字体。
                draw.text(
                    (VALUE_COL, y), value,
                    font=self._font(38, "digit"), fill=TONE.accent,
                )
            else:
                text, font = T.fit_text(
                    self.book, value, 26, TEXT_RIGHT - VALUE_COL, "regular", 16, draw
                )
                draw.text((VALUE_COL, y), text, font=font, fill=TONE.ink)

    @staticmethod
    def _days_text(data: dict[str, Any]) -> str:
        days = int(data.get("days") or 0)
        return f"{days} 天" if days > 0 else "今天刚来"

    # ------------------------------------------------------------ 页脚
    def _draw_footer(self, draw: Any, data: dict[str, Any]) -> None:
        # 页脚压一条细线：不加的话，最后一屏的空白会让页脚看起来像
        # 「不知道该放哪，就先搁在底下」。
        draw.rectangle((MARGIN, FOOT_Y - 24, TEXT_RIGHT, FOOT_Y - 23), fill=(20, 20, 24, 26))

        hint_font = self._font(15)
        # 右下角标出名片款式。买来的东西要让人看见 —— 不然群友花了额度
        # 却没有任何一处显示「我用的是哪张」，等于没买。
        # 基础名片（男/女）不标：每个人都在用，标出来只是噪音。
        skin = safe_text(data.get("skin"))
        if skin:
            label = f"名片 · {skin}"
            draw.text(
                (TEXT_RIGHT - T.text_width(label, hint_font, draw), FOOT_Y),
                label, font=hint_font, fill=TONE.ink_soft,
            )

