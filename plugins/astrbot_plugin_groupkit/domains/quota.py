"""额度：余额 / 抽奖 / 变动记录。

额度是跨功能的通用货币，后面接表情包、AI 对话都靠它。
约定：所有加减都走 `ctx.db.add_quota`，不要自己 UPDATE members.quota，
否则流水表会和余额对不上，群友问「我钱怎么没了」时查不出来。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from astrbot.api.message_components import Plain

from ..core.command import (
    Command,
    CommandEnv,
    HelpEntry,
    exact,
    exact_or_mention,
    prefix,
    require_level,
)
from ..core.context import PluginContext
from ..utils import event as ev
from ..utils.forward import build_forward
from ..utils.router import parse_int, strip_at

# 流水一次给 20 条 —— 攒了一周的人 5 条根本看不完；
# 但上限必须存在，不然刷一晚上表情包的人能倒出几百行。
HISTORY_LIMIT = 20

# 转账手续费：到账 = 给出的 80%，除不尽**进一**（3 额度到账 3，不吃小字号亏）。
# 这是「群内交易有摩擦」的设计：额度不能零成本搬家，套利空间就小。
TRANSFER_RATE_NUM = 8   # 分子
TRANSFER_RATE_DEN = 10  # 分母


def transfer_receive(amount: int) -> int:
    """amount * 80% 向上取整。单独抽出来是为了能单测进一行为。"""
    return -(-amount * TRANSFER_RATE_NUM // TRANSFER_RATE_DEN)


async def transfer(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """给额度 @某人 数量：从自己账上转，对方收 80%（进一）。

    **所有人都能用** —— 和「发额度」是两回事：发是无中生有（4 级），
    给是把自己的搬给别人（1 级）。扣的是转出方的账，到账多少只跟
    金额挂钩，跟双方身份都无关。
    """
    cur = ctx.config.currency_name
    targets = ev.at_targets(env.event, env.sender_id)
    amount = parse_int(strip_at(env.args))
    fmt = f"格式：给{cur} @某人 50"
    if not targets:
        yield env.event.plain_result(fmt)
        return
    if amount <= 0:
        yield env.event.plain_result(f"金额要是正数\n{fmt}")
        return

    target = targets[0]
    if target == env.sender_id:
        yield env.event.plain_result("转给自己？左手倒右手就不用花钱了")
        return
    if target == ev.self_id(env.event):
        yield env.event.plain_result("机器人不收转账，它没有地方花")
        return

    received = transfer_receive(amount)
    # 先扣转出方，扣不动就停 —— 顺序不能反，不然「没钱也能转」。
    ok, balance = await asyncio.to_thread(
        ctx.db.add_quota, env.group_id, env.sender_id, -amount,
        f"转账给 {target}", env.sender_id,
    )
    if not ok:
        yield env.event.plain_result(
            f"⚠️ 你的{cur}不够\n想转 {amount}，你当前只有 {balance}"
        )
        return
    _, target_balance = await asyncio.to_thread(
        ctx.db.add_quota, env.group_id, target, received,
        f"来自 {env.sender_id} 的转账", env.sender_id,
    )
    fee = amount - received
    lines = [f"✅ 已转出 {amount} {cur}，对方到账 {received}"]
    if fee > 0:
        lines.append(f"（手续费 20%：{fee}）")
    lines.append(f"你的余额：{balance} · 对方余额：{target_balance}")
    yield env.event.plain_result("\n".join(lines))


async def wallet(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """余额 + 额度命名 + 抽奖进度。@别人要 2 级（助手）。"""
    targets = ev.at_targets(env.event, env.sender_id)
    if targets:
        warn = await require_level(ctx, env, 2)
        if warn:
            yield env.event.plain_result(warn)
            return
        who = targets[0]
        member = await asyncio.to_thread(ctx.db.get_member, env.group_id, who)
        name = ctx.profiles.display_name(member, who)
        cur = ctx.config.currency_name
        yield env.event.plain_result(
            f"💰 {name} 的{cur}\n余额 {int((member or {}).get('quota') or 0)} {cur}"
        )
        return
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.quota.render, env.group_id, env.sender_id, env.member)
    )


async def draw(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """消耗一次抽奖机会，随机给额度。"""
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.quota.draw, env.group_id, env.sender_id)
    )


async def history(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """最近 20 笔额度变动，打包成合并转发 —— 二十行直接甩群里就是刷屏。"""
    text = await asyncio.to_thread(
        ctx.quota.render_history, env.group_id, env.sender_id, HISTORY_LIMIT
    )
    lines = text.split("\n")
    # 第一行是页头（💰 额度记录），拿它当转发节点的标题；正文装进节点。
    body = "\n".join(lines[1:]).strip("\n")
    chain = build_forward(env.event, [(lines[0], [Plain(body)])])
    if chain:
        yield env.event.chain_result(chain)
        return
    # 构造转发失败（框架版本不兼容之类）：宁可刷屏也不能一条都不发。
    yield env.event.plain_result(text)


COMMANDS = [
    Command(
        key="quota",
        # exact_or_mention 而不是 exact：`额度 @某人` 要看别人的余额，
        # 而 exact 只认整句 —— 那样发出去是「没反应」。
        matcher=exact_or_mention(("额度", "我的额度", "余额", "查额度")),
        handler=wallet,
        perm_short="额度",
        help=(
            HelpEntry("额度", "余额", page="base", order=40, slot="wallet", group="额度与统计"),
            HelpEntry(
                "额度 @某人", "看别人的余额（2 级起）",
                page="base", level=2, order=42, group="我的信息",
            ),
        ),
    ),
    Command(
        key="draw",
        matcher=exact(("抽奖", "抽一次", "出手气")),
        handler=draw,
        perm_short="抽奖",
        help=(HelpEntry("抽奖", "抽奖机会", page="base", order=41, slot="wallet", group="额度与统计"),),
    ),
    Command(
        key="quota_history",
        matcher=exact(("额度记录", "额度流水", "流水", "我的额度记录")),
        handler=history,
        perm_short="额度记录",
        help=(HelpEntry("额度记录", "最近 20 笔变动（合并转发）", page="base", order=50, group="额度与统计"),),
    ),
    Command(
        key="transfer",
        matcher=prefix(("给额度", "转额度", "转账")),
        handler=transfer,
        perm_short="给额度",
        help=(HelpEntry("给额度 @某人 50", "从自己账上转，对方收 80%（进一）", page="base", order=51, group="额度与统计"),),
    ),
]
