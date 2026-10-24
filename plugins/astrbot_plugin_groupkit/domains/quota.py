"""额度：余额 / 抽奖 / 变动记录。

额度是跨功能的通用货币，后面接表情包、AI 对话都靠它。
约定：所有加减都走 `ctx.db.add_quota`，不要自己 UPDATE members.quota，
否则流水表会和余额对不上，群友问「我钱怎么没了」时查不出来。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, exact
from ..core.context import PluginContext


async def wallet(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """余额 + 额度命名 + 抽奖进度。"""
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.quota.render, env.group_id, env.sender_id, env.member)
    )


async def draw(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """消耗一次抽奖机会，随机给额度。"""
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.quota.draw, env.group_id, env.sender_id)
    )


async def history(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """最近 5 笔额度变动。"""
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.quota.render_history, env.group_id, env.sender_id)
    )


COMMANDS = [
    Command(
        key="quota",
        matcher=exact(("额度", "我的额度", "余额", "查额度")),
        handler=wallet,
        perm_short="额度",
        help=(HelpEntry("额度", "余额", page="base", order=40, slot="wallet"),),
    ),
    Command(
        key="draw",
        matcher=exact(("抽奖", "抽一次", "出手气")),
        handler=draw,
        perm_short="抽奖",
        help=(HelpEntry("抽奖", "抽奖机会", page="base", order=41, slot="wallet"),),
    ),
    Command(
        key="quota_history",
        matcher=exact(("额度记录", "额度流水", "流水", "我的额度记录")),
        handler=history,
        perm_short="额度记录",
        help=(HelpEntry("额度记录", "最近 5 笔变动", page="base", order=50),),
    ),
]
