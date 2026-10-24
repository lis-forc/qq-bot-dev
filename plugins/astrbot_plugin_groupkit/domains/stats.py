"""发言统计：今日页 + 每条消息的计数。

计数是插件的热路径（每条群消息都会走到），所以这里的原则是
「出错也绝不影响聊天」——统计挂了只记日志，不让消息发不出去。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from astrbot.api import logger

from ..core.command import Command, CommandEnv, HelpEntry, exact
from ..core.context import PluginContext


async def today(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """今日 / 本月 / 累计发言，附带抽奖进度。"""
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.stats.render, env.group_id, env.sender_id, env.member)
    )


async def count_message(ctx: PluginContext, event, group_id: str, sender_id: str) -> AsyncGenerator:
    """记一条发言。攒满抽奖机会时按配置决定要不要提醒。"""
    try:
        result = await asyncio.to_thread(ctx.stats.record_message, group_id, sender_id)
    except Exception as exc:
        logger.error(f"[群助手] 发言统计失败: {exc}", exc_info=True)
        return
    if ctx.config.notify_draw_ready and result.get("draws_gained"):
        yield event.plain_result(
            "🎟 抽奖机会 +1\n"
            f"抽奖进度：{result.get('msg_progress')} / {ctx.config.draw_per}\n"
            "发送「抽奖」试试手气"
        )


COMMANDS = [
    Command(
        key="today",
        matcher=exact(("今日", "发言", "统计", "今日发言", "我的发言")),
        handler=today,
        perm_short="今日",
        help=(HelpEntry("今日", "发言统计", page="base", order=42, slot="wallet"),),
    ),
]
