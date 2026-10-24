"""事件读取小工具。

OneBot 的原始报文放在 `event.message_obj.raw_message` 里，有时是平铺 dict、
有时是对象，这里统一兜住。所有函数都不抛异常 —— 读不到就给默认值，
让上层按「拿不到就当没有」处理，不要因为一个字段缺失把整条消息搞崩。
"""

from __future__ import annotations

from typing import Any

from . import onebot
from .router import compact


def raw_get(raw: Any, key: str, default: Any = None) -> Any:
    """读原始报文里的一个字段，兼容 dict / 对象两种形态。"""
    if raw is None:
        return default
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


def raw_message(event) -> Any:
    return raw_get(getattr(event, "message_obj", None), "raw_message")


def is_message_post(event) -> bool:
    """这是不是一条真正的聊天消息（而不是 notice / request 等）。"""
    post_type = raw_get(raw_message(event), "post_type")
    if not post_type:
        return True  # 拿不到就按消息处理：宁可多算一条，也不要漏算
    return str(post_type) in {"message", "message_sent"}


def clean_text(text: str) -> str:
    """压空白。口令不加任何前缀，所以这里只做归一化。"""
    return compact(text)


def self_id(event) -> str:
    return str(event.get_self_id() or "")


def at_targets(event, sender_id: str) -> list[str]:
    """被 @ 的人，去掉「操作者自己」和「机器人自己」。

    多个 @ 时保留顺序，由调用方决定用第几个。
    """
    blocked = {str(sender_id), self_id(event)}
    return [t for t in onebot.get_at_targets(event) if t not in blocked]


def first_at(event, sender_id: str) -> str | None:
    targets = at_targets(event, sender_id)
    return targets[0] if targets else None


def at_all(event) -> bool:
    """消息里有没有 @全体成员。

    `get_at_targets` 会刻意过滤掉它（qq == "all" 不是数字），因为那个函数
    只服务于「对某个人执行操作」。所以需要单独识别一次 —— 但识别出来
    并不执行任何操作，只是把「禁言 @全体成员」这种写法引导到
    「全群禁言」口令上（见 domains/admin.py）。

    OneBot 里 @全体成员 的 qq 是字符串 "all"；AstrBot 的 message_str
    里则通常渲染成「@全体成员」，兜底也认这个。
    """
    message_obj = getattr(event, "message_obj", None)
    for comp in getattr(message_obj, "message", None) or []:
        if onebot.At is not None and isinstance(comp, onebot.At):
            target = getattr(comp, "qq", None) or getattr(comp, "user_id", None)
            if str(target or "") == "all":
                return True
            continue
        if str(getattr(comp, "type", "")).lower() == "at":
            target = getattr(comp, "qq", None) or getattr(comp, "user_id", None)
            if str(target or "") == "all":
                return True
    return "全体成员" in str(getattr(event, "message_str", "") or "")


def target_hint(event, fmt: str) -> str:
    """没解析出操作目标时，给出准确原因。

    目标里会滤掉「自己」和「机器人」，所以 `禁言 @自己 10分钟` 会得到空目标。
    这时回一句「格式：禁言 @某人」等于骗人 —— 他明明 @ 了人。
    """
    return "⚠️ 不能对自己或机器人操作" if onebot.get_at_targets(event) else fmt
