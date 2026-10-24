"""OneBot v11（NapCat）动作封装。

约定：所有函数都不抛异常，失败只记日志并返回 False / None / []。
这样上层的口令处理里就不用到处包 try，只判断返回值即可。

⚠️ 判定接口是否成功，统一走 `succeeded()`，**不要用「返回值不是 None」**。
OneBot 的写操作成功时 data 是 null，aiocqhttp 会把 data 直接返回（也就是
None），用 None 判失败会把每次成功都当成失败。
"""

from __future__ import annotations

import re
from typing import Any

try:  # 本地单测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

try:
    from astrbot.api.message_components import At
except Exception:  # pragma: no cover
    At = None  # type: ignore[assignment]

PLATFORM = "aiocqhttp"


class _Failed:
    """哨兵对象：把「调用抛异常」和「调用成功但 data 是 null」区分开。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只为日志好读
        return "<FAILED>"

    def __bool__(self) -> bool:
        return False


_FAILED = _Failed()


def is_supported(event) -> bool:
    """只在 aiocqhttp（OneBot v11）下工作。"""
    try:
        return event.get_platform_name() == PLATFORM
    except Exception:
        return False


async def call(event, action: str, **params: Any) -> Any:
    """调用协议端接口。失败时返回 _FAILED 哨兵。

    **不能用 None 表示失败。** OneBot v11 的写操作（set_group_ban /
    set_group_kick / set_group_whole_ban）成功时 data 就是
    null，aiocqhttp 把 data 直接返回，Python 里拿到的就是 None。
    用「返回值 is None」判失败，会把每一次成功都记成失败 —— 表现就是
    群管操作明明生效了，机器人却回一句「⚠️ 失败，检查机器人权限」。
    """
    try:
        return await event.bot.api.call_action(action, **params)
    except Exception as exc:
        logger.error(f"[群助手] 调用 {action} 失败: {exc}", exc_info=True)
        return _FAILED


def succeeded(result: Any) -> bool:
    """判断一次 call 是否真的成功。

    三种形态：
    1. 哨兵 → 抛异常了，失败
    2. dict 且带 status / retcode → 按协议端的返回值判定
    3. 其它（None / True / list / 不带状态字段的 dict）→ 没报错就算成功
    """
    if result is _FAILED:
        return False
    if isinstance(result, dict):
        status = result.get("status")
        if status is not None and str(status).lower() != "ok":
            return False
        retcode = result.get("retcode")
        if retcode is not None:
            try:
                return int(retcode) == 0
            except (TypeError, ValueError):
                return True
    return True


# ---------------------------------------------------------------- 群成员信息
async def get_member_info(event, group_id: Any, user_id: Any) -> dict | None:
    result = await call(event, "get_group_member_info", group_id=int(group_id), user_id=int(user_id))
    if not succeeded(result):
        return None
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        return result["data"]
    return result if isinstance(result, dict) else None


async def get_member_role(event, group_id: Any, user_id: Any) -> str:
    """返回 owner / admin / member，拿不到时返回空串。"""
    member = await get_member_info(event, group_id, user_id)
    return str((member or {}).get("role") or "")


async def is_bot_admin(event, group_id: Any) -> bool:
    """机器人自己有没有群管理权限。所有群管动作前都先问一遍。"""
    try:
        bot_id = event.get_self_id() or event.message_obj.self_id
    except Exception:
        bot_id = None
    if not bot_id:
        return False
    return await get_member_role(event, group_id, bot_id) in {"owner", "admin"}


# ---------------------------------------------------------------- 群管动作
async def mute(event, group_id: Any, user_id: Any, seconds: int) -> bool:
    seconds = max(0, min(int(seconds), 2_592_000))
    result = await call(event, "set_group_ban", group_id=int(group_id), user_id=int(user_id), duration=seconds)
    return succeeded(result)


async def unmute(event, group_id: Any, user_id: Any) -> bool:
    return await mute(event, group_id, user_id, 0)


async def kick(event, group_id: Any, user_id: Any, reject_add_request: bool = False) -> bool:
    result = await call(
        event, "set_group_kick", group_id=int(group_id), user_id=int(user_id),
        reject_add_request=reject_add_request,
    )
    return succeeded(result)


async def set_whole_ban(event, group_id: Any, enable: bool) -> bool:
    result = await call(event, "set_group_whole_ban", group_id=int(group_id), enable=bool(enable))
    return succeeded(result)


# ---------------------------------------------------------------- @ 目标提取
def get_at_targets(event) -> list[str]:
    """提取消息里 @ 的人（QQ 号）。

    只处理 OneBot v11 的三种形态：
    1. AstrBot 消息链里的 At 组件
    2. raw_message 里的 at 段
    3. 纯文本里残留的 @12345
    """
    targets: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text != "all" and text.isdigit() and text not in seen:
            seen.add(text)
            targets.append(text)

    message_obj = getattr(event, "message_obj", None)
    if message_obj:
        for comp in getattr(message_obj, "message", None) or []:
            if At is not None and isinstance(comp, At):
                add(getattr(comp, "qq", None) or getattr(comp, "user_id", None))
                continue
            if str(getattr(comp, "type", "")).lower() == "at":
                add(getattr(comp, "qq", None) or getattr(comp, "user_id", None))

        raw = getattr(message_obj, "raw_message", None)
        segments = getattr(raw, "message", None)
        if segments is None and isinstance(raw, dict):
            segments = raw.get("message")
        if isinstance(segments, list):
            for seg in segments:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    add((seg.get("data") or {}).get("qq"))

    text = getattr(event, "message_str", "") or ""
    for match in re.finditer(r"@(\d{5,12})", text):
        add(match.group(1))
    return targets
