"""口令解析工具。纯函数，不依赖 AstrBot，方便单测。

核心是 drop_prefix：返回 None 表示「没命中这个口令」，返回 "" 表示
「命中了但用户没给参数」，返回非空字符串表示「命中了，参数在这里」。
这个三态区分是整套路由的地基。
"""

from __future__ import annotations

import re

MAX_HOBBY_LEN = 12
MAX_MUTE_SECONDS = 2_592_000  # 30 天，超过协议端会拒绝


def compact(text: str | None) -> str:
    """把连续空白压成一个空格并去掉首尾空白。"""
    return re.sub(r"\s+", " ", text or "").strip()


def strip_at(text: str) -> str:
    """剥掉文本里残留的 @某人 / @12345 / 张三(12345)。"""
    text = re.sub(r"@[.(]*\d+[)]*", "", text or "")
    text = re.sub(r"@\S+", "", text)
    text = re.sub(r"[\u4e00-\u9fff\w]*\(\d+\)", "", text)
    return compact(text.replace("@", ""))


def drop_prefix(text: str, prefixes: tuple[str, ...] | list[str]) -> str | None:
    """命中前缀则返回剩余参数，否则返回 None。

    要求前缀后面必须跟分隔符（空格 / 全角冒号 / 逗号）或直接结束，
    这样「改名片的」不会误命中「改名」。
    """
    cleaned = compact(text)
    for prefix in prefixes:
        if cleaned == prefix:
            return ""
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix):]
            if rest and (rest[0].isspace() or rest[0] in "：:，,、"):
                return strip_at(rest.lstrip("：:，,、 "))
    return None


def parse_int(text: str | None, default: int = 0) -> int:
    """抠出文本里第一个整数。"""
    match = re.search(r"-?\d+", text or "")
    return int(match.group(0)) if match else default


def parse_duration(text: str | None, default_unit: str = "分钟") -> int | None:
    """把「10分钟 / 1小时 / 30秒 / 2天 / 600」解析成秒数。

    没写单位时按 default_unit（默认分钟）算——QQ 场景里「禁言 @某人 10」
    通常就是指 10 分钟。返回 None 表示解析不出。
    """
    cleaned = compact(text)
    match = re.search(r"(\d+(?:\.\d+)?)\s*(秒|秒钟|分钟|分|小时|时|天|s|m|h|d)?", cleaned)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or default_unit).lower()
    factor = {
        "秒": 1, "秒钟": 1, "s": 1,
        "分": 60, "分钟": 60, "m": 60,
        "时": 3600, "小时": 3600, "h": 3600,
        "天": 86400, "d": 86400,
    }.get(unit, 60)
    return max(0, min(int(value * factor), MAX_MUTE_SECONDS))


def fmt_duration(seconds: int) -> str:
    """秒数转人话：3600 → 「1 小时」。除不尽就老实报秒。"""
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def validate_hobby(hobby: str) -> tuple[bool, str]:
    cleaned = compact(hobby)
    if not cleaned:
        return False, "爱好不能为空。"
    if len(cleaned) > MAX_HOBBY_LEN:
        return False, f"爱好请控制在 {MAX_HOBBY_LEN} 个字以内。"
    return True, ""
