"""大富翁：花一点额度掷一次骰子，六种结果，其中五种不是钱。

**为什么不全给额度。** 全是加减钱的骰子，玩三次就腻——而且它只有一种
刺激（数字变大）。混进禁言、挂牌、发卡之后，每一格都有不同的后果，
群友才会真的期待「下一个掷出几」。终极大奖是这套玩法唯一的高光时刻。

**为什么奖池要压住。** 只有 5 点和 6 点发额度，4 点发的是卡（卡不产钱，
只是把额度换成一次「玩」的机会）。算下来每局期望净收益是负的 ——
见 ``_expected()`` 与自测里的断言。真让期望为正，最勤快的人会一直掷，
额度越滚越不值钱，最后这玩法把整个经济搞崩。

**冷却的意义。** 三小时一次（可调）。它不是防刷，防刷靠期望；它是
让「今天掷出什么」变成一件值得等的事。冷却先查再扣钱 —— 反过来会
出现「钱扣了才发现还在冷却」。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, exact
from ..core.context import PluginContext
from ..utils import nickname as nick_util
from ..utils import onebot

RULE = "=" * 26

# 骰子点数 → (类型, 数值, 文案)。列表下标 0 就是 1 点。
#   quota   数值 = 额度增减（含正负）
#   mute    数值 = 禁言秒数
#   suffix  数值 = 挂牌小时数
#   card    数值 = 发几张权限卡
#   jackpot 数值 = 基础额度，另外还有一次极小概率的终极大奖
DICE: tuple[tuple[str, int, str], ...] = (
    ("quota", -5, "💣 踩到地雷，罚款"),
    ("mute", 60, "🤐 被巡警逮住，禁言"),
    ("suffix", 3, "🐷 说漏嘴了，名字上挂个牌"),
    ("card", 1, "🎫 捡到一张权限卡"),
    ("quota", 10, "💰 路过银行，领到"),
    ("jackpot", 20, "👑 直冲终点"),
)

# 挂牌时写的字。短，因为它要接在群名片后面，长了会被 QQ 截断。
SUFFIX_WORD = "我是猪"

CARD_NAMES = {"mute": "禁言卡", "rename": "改名卡"}


def _roll() -> int:
    """掷一次，返回 1~6。抽出来是为了自测能替换它。"""
    return random.randint(1, 6)


def _hm(seconds: int) -> str:
    """剩余秒数 → 「2 小时 15 分」。少于一分钟也说 1 分钟，别出「0 分钟」。"""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{max(1, minutes)} 分钟"


def _expected(cost: int, jackpot: int, chance: float) -> float:
    """每局期望净收益。低于 0 才说明这套数值不会通胀。

    4 点的卡按 0 计算：它不产生额度，只是把额度换成一次「玩」的机会。
    """
    total = 0.0
    for kind, value, _ in DICE:
        if kind == "quota":
            total += value
        elif kind == "jackpot":
            total += value + jackpot * chance
    return total / len(DICE) - cost


def _usage(ctx: PluginContext) -> str:
    cfg = ctx.config
    cost = cfg.monopoly_cost
    cur = cfg.currency_name
    hours = cfg.monopoly_cooldown_hours
    note = f"入场 {cost} {cur}" if cost > 0 else "免费"
    if hours > 0:
        note += f"｜每 {hours} 小时一次"
    rows = [
        f"{index} 点｜{text}" for index, (_kind, _value, text) in enumerate(DICE, 1)
    ]
    return "\n".join(
        [
            f"🎲 大富翁｜{note}",
            RULE,
            *rows,
            RULE,
            f"👑 6 点还有 {cfg.monopoly_jackpot_chance}% 概率中终极大奖"
            f"（+{cfg.monopoly_jackpot} {cur}）",
            "发「大富翁」掷一次",
        ]
    )


async def rules(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """只报规则，不掷。给「先看看值不值得玩」的人。"""
    yield env.event.plain_result(_usage(ctx))


async def _apply_card(ctx: PluginContext, group_id: str, user_id: str) -> str:
    """随机发一张权限卡，返回要写进结果里的那句话。"""
    item = random.choice(tuple(CARD_NAMES))
    await asyncio.to_thread(ctx.db.bag_add, group_id, user_id, "permit", item, 1)
    n = await asyncio.to_thread(ctx.db.bag_count, group_id, user_id, "permit", item)
    return f"🎫 捡到一张「{CARD_NAMES[item]}」（背包里共 {n} 张）"


async def _apply_suffix(
    ctx: PluginContext, env: CommandEnv, hours: int
) -> str:
    """给名字挂上「｜我是猪」，并立刻把群名片改出来。"""
    cfg = ctx.config
    until = int(time.time()) + max(1, hours) * 3600
    await asyncio.to_thread(
        ctx.db.set_nick_suffix, env.group_id, env.sender_id, SUFFIX_WORD, until
    )

    # 立刻改名片，不等他下一次发言 —— 挂牌的乐趣就在于「马上被看见」。
    member = await asyncio.to_thread(ctx.db.get_member, env.group_id, env.sender_id)
    name = ctx.profiles.display_name(member, env.sender_id)
    sex = ctx.profiles.sex(member)
    card = nick_util.compose(name, sex, cfg, SUFFIX_WORD)
    done = await onebot.set_group_card(env.event, env.group_id, env.sender_id, card)
    tail = "，名片已经挂上了" if done else "（名片没改成，机器人大概不是管理员）"
    return f"🐷 名字后面挂了「{SUFFIX_WORD}」，{hours} 小时后自动摘掉{tail}"


async def play(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """大富翁：查冷却 → 扣入场费 → 掷骰 → 按点数结算。"""
    cfg = ctx.config
    cur = cfg.currency_name
    cost = cfg.monopoly_cost
    now = int(time.time())

    # 冷却先查再扣钱。反过来就会出现「钱扣了、才发现还在冷却」。
    cooldown = max(0, cfg.monopoly_cooldown_hours) * 3600
    last = await asyncio.to_thread(ctx.db.monopoly_last, env.group_id, env.sender_id)
    if cooldown and last and now - last < cooldown:
        yield env.event.plain_result(
            f"🎲 刚掷过，{_hm(cooldown - (now - last))} 后再来"
        )
        return

    if cost > 0:
        balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
        if balance < cost:
            yield env.event.plain_result(
                f"🎲 入场要 {cost} {cur}，你只有 {balance}\n"
                "多聊几句、攒够发言抽个奖就有了"
            )
            return
        ok, left = await asyncio.to_thread(
            ctx.db.add_quota, env.group_id, env.sender_id, -cost, "大富翁入场", env.sender_id
        )
        if not ok:
            yield env.event.plain_result(f"🎲 入场要 {cost} {cur}，你只有 {left}")
            return

    # 冷却从「真的掷了」开始算。余额不足或冷却中被拒的人不会被记一笔。
    await asyncio.to_thread(ctx.db.monopoly_mark, env.group_id, env.sender_id, now)

    point = _roll()
    kind, value, text = DICE[max(0, min(len(DICE) - 1, point - 1))]

    lines = [f"🎲 {ctx.profiles.display_name(env.member, env.sender_id)} 掷出 {point} 点"]
    net = -cost
    lines.append(text)

    if kind == "quota":
        lines[-1] = f"{text} {abs(value)}"
        ok, _ = await asyncio.to_thread(
            ctx.db.add_quota, env.group_id, env.sender_id, value, f"大富翁 {point} 点", env.sender_id
        )
        if ok:
            net += value
        else:
            # 罚款扣不动（余额被入场费掏空了）就不扣，余额不能变成负数。
            lines[-1] += "（余额不够，免了）"

    elif kind == "mute":
        if not await onebot.is_bot_admin(env.event, env.group_id):
            lines[-1] = "🤐 巡警没来（机器人不是管理员，这次逃过一劫）"
        elif await onebot.mute(env.event, env.group_id, env.sender_id, value):
            lines[-1] = f"{text} {value // 60} 分钟"
        else:
            lines[-1] = "🤐 巡警走空了（禁言没成功）"

    elif kind == "suffix":
        lines[-1] = await _apply_suffix(ctx, env, value)

    elif kind == "card":
        lines[-1] = await _apply_card(ctx, env.group_id, env.sender_id)

    elif kind == "jackpot":
        _, balance = await asyncio.to_thread(
            ctx.db.add_quota, env.group_id, env.sender_id, value, f"大富翁 {point} 点", env.sender_id
        )
        net += value
        lines[-1] = f"{text}，+{value} {cur}"
        chance = max(0.0, min(100.0, cfg.monopoly_jackpot_chance)) / 100.0
        if chance > 0 and random.random() < chance:
            _, balance = await asyncio.to_thread(
                ctx.db.add_quota,
                env.group_id,
                env.sender_id,
                cfg.monopoly_jackpot,
                "大富翁终极大奖",
                env.sender_id,
            )
            net += cfg.monopoly_jackpot
            lines.append(f"🎉 终极大奖！再 +{cfg.monopoly_jackpot} {cur}")

    balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
    lines.append(RULE)
    lines.append(f"💰 本局 {net:+d}｜余额 {balance}")
    yield env.event.plain_result("\n".join(lines))


COMMANDS = [
    Command(
        key="monopoly",
        matcher=exact(("大富翁", "玩大富翁", "掷骰子", "摇骰子")),
        handler=play,
        perm_short="大富翁",
        help=(
            HelpEntry(
                "大富翁",
                "掷一次骰子，点数决定这一局的命运",
                page="fun",
                order=35,
                group="玩一玩",
            ),
        ),
    ),
    Command(
        key="monopoly_rule",
        matcher=exact(("大富翁规则", "大富翁玩法", "骰子规则")),
        handler=rules,
        perm_short="",
        help=(
            HelpEntry("大富翁规则", "六个点数各是什么", page="fun", order=36, group="玩一玩"),
        ),
    ),
]
