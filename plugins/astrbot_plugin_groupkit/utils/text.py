"""终端对齐用的小工具。

QQ 的聊天气泡用等宽中文字体，但一个中文字占两格、一个英文字母占一格。
直接用 `len()` 补空格，中文和英文混排的表格就会歪：

    连续签到 7 天
    累计签到  14 天     ← 差一格

所以按「显示宽度」补：东亚字符算 2，其余算 1。抽出来单独一个模块，
是因为额度记录、排行榜、称号列表都要用，放在某一处当私有函数迟早要复制。
"""

from __future__ import annotations

import re

# QQ 名片里塞满 emoji 是常态，但图片字体画出来是豆腐块，统一清掉。
# 放在这里而不是 card.py：图鉴、信息卡、将来的任何出图功能都要用，
# 而且它跟「文本怎么处理」是一类事。
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


def safe_text(text: object, fallback: str = "") -> str:
    """清掉 emoji 和控制符，避免出图时渲染成方块。"""
    cleaned = _EMOJI_RE.sub("", str(text or ""))
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch == " ")
    return cleaned.strip() or fallback

# 这几个区间的字符在等宽字体里占两格：CJK 统一表意文字、假名、谚文、
# 兼容表意文字、竖排/全角标点。判断方法沿用 wcwidth 的经典做法。
_WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x2E80, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
)


def width(text: str) -> int:
    """一段文字占几个格子。"""
    total = 0
    for ch in str(text):
        code = ord(ch)
        total += 2 if any(low <= code <= high for low, high in _WIDE_RANGES) else 1
    return total


def pad(text: str, target: int, align: str = "left") -> str:
    """补空格到 target 格宽。放不下就原样返回，绝不截断。"""
    gap = max(0, target - width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def table(rows: list[tuple[str, ...]], gap: str = "  ") -> str:
    """把若干行按列对齐。每列宽度取该列最宽的那一行。"""
    if not rows:
        return ""
    columns = len(rows[0])
    widths = [
        max(width(row[index]) if index < len(row) else 0 for row in rows)
        for index in range(columns)
    ]
    return "\n".join(
        gap.join(pad(cell, widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    )
