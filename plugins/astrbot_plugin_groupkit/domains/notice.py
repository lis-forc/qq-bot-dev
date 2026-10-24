"""入群等通知事件。

现在的职责只有一个：新人进群时立刻把真实入群时间落库，
这样「在册 N 天」不用等他自己说话、也不用等下一次查协议端接口。
以后要做入群欢迎、入群验证，都加在这里。
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from ..core.context import PluginContext
from ..utils import event as ev


async def handle_notice(ctx: PluginContext, event, group_id: str) -> None:
    raw = ev.raw_message(event)
    if ev.raw_get(raw, "post_type") != "notice":
        return
    if ev.raw_get(raw, "notice_type") != "group_increase":
        return
    user_id = ev.raw_get(raw, "user_id")
    if not user_id:
        return
    try:
        join_ts = int(ev.raw_get(raw, "time") or 0) or int(time.time())
    except (TypeError, ValueError):
        join_ts = int(time.time())

    await asyncio.to_thread(
        ctx.db.ensure_member, group_id, str(user_id), ctx.config.welcome_quota
    )
    await asyncio.to_thread(ctx.db.set_join_info, group_id, str(user_id), join_time=join_ts)
    logger.info(f"[群助手] 记录入群时间：{user_id} @ 群 {group_id}")
