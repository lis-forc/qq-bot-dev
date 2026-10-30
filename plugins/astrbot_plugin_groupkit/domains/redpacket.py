"""红包：自己掏额度，让群里抢。

三条规则，都是从「别让人亏钱」推出来的：

1. **一个群只能有一个没抢完的包。** 同时挂三个，群友发「抢红包」抢到
   哪个全看运气，而发的人根本不知道自己的钱进了哪个池子。所以发之前
   先看有没有剩的，有就拦住 —— 这是用户明确要求的。
2. **不设过期。** 没抢完就一直挂着（也是用户要求的）。代价是它会长期
   占着唯一那个坑位，所以「还剩几个」要能随时查到。
3. **同一个人同一个包只能抢一次。** 主键约束兜着，不是业务层判断。

金额分配在 ``database.split_packet``（二倍均值，每人至少 1）。

**为什么发红包不 @ 全体。** 发红包本身就是给群里的惊喜，再 @ 一遍就
变成通知，人人都在看手机的时候反而没人理。群里总会有人说话的。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, exact, prefix
from ..core.context import PluginContext
from ..utils import deco


def _usage(ctx: PluginContext) -> str:
    cur = ctx.config.currency_name
    count = ctx.config.red_packet_count
    return (
        "🧧 发红包：从自己额度里掏出来让群里抢\n"
        "\n"
        "  发红包 55　　　　默认拆 {} 个\n"
        "  发红包 55 10　　　自己定个数\n"
        "  抢红包　　　　　抢群里那个包\n"
        "\n"
        "一个群同时只有一个包，没抢完之前不能再发".format(count)
    )


async def send_packet(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """发红包 <总额> [个数]。"""
    cur = ctx.config.currency_name
    args = (env.args or "").split()
    if not args:
        yield env.event.plain_result(_usage(ctx))
        return

    try:
        amount = int(args[0])
    except (TypeError, ValueError):
        yield env.event.plain_result(f"⚠️ 格式：发红包 55（你写的是「{args[0]}」）")
        return

    count = ctx.config.red_packet_count
    if len(args) > 1:
        if not args[1].isdigit():
            yield env.event.plain_result("⚠️ 个数要写数字：发红包 55 10")
            return
        count = int(args[1])

    if amount <= 0:
        yield env.event.plain_result("⚠️ 金额要大于 0")
        return
    if count <= 0:
        yield env.event.plain_result("⚠️ 个数要大于 0")
        return
    if count > amount:
        # 每个包至少 1，所以 55 块最多分 55 个。写清楚原因，别只报错。
        yield env.event.plain_result(
            f"⚠️ {amount} {cur} 分不出 {count} 个（每个至少 1）"
        )
        return

    pending = await asyncio.to_thread(ctx.db.red_packet_open, env.group_id)
    if pending:
        yield env.event.plain_result(
            f"🧧 上一个红包还有 {int(pending.get('left_count') or 0)} 个没抢完\n"
            "抢完再发新的"
        )
        return

    balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
    if balance < amount:
        yield env.event.plain_result(f"⚠️ 不够 {amount} {cur}（你有 {balance}）")
        return

    ok, left = await asyncio.to_thread(
        ctx.db.add_quota, env.group_id, env.sender_id, -amount, "发红包", env.sender_id
    )
    if not ok:
        yield env.event.plain_result(f"⚠️ 不够 {amount} {cur}（你有 {left}）")
        return

    created, _reason, _pid = await asyncio.to_thread(
        ctx.db.red_packet_create, env.group_id, env.sender_id, amount, count
    )
    if not created:
        # 钱扣了、包没开出来：必须原路退回。宁可多一条流水，
        # 也不能让额度凭空消失 —— 那种账目对不上的 bug 最难解释。
        await asyncio.to_thread(
            ctx.db.add_quota, env.group_id, env.sender_id, amount, "红包退回", env.sender_id
        )
        yield env.event.plain_result(
            "🧧 没发出去（刚有人抢了个先），额度已经退回"
        )
        return

    yield env.event.plain_result(
        f"🧧 红包已发出 · {amount} {cur} / {count} 个\n抢：发「抢红包」"
    )


async def grab(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """抢红包。"""
    cur = ctx.config.currency_name
    status, amount, packet = await asyncio.to_thread(
        ctx.db.red_packet_grab, env.group_id, env.sender_id
    )

    if status == "empty":
        yield env.event.plain_result("🧧 现在没有红包可抢")
        return
    if status == "taken":
        yield env.event.plain_result("🧧 这个包你已经抢过了")
        return

    await asyncio.to_thread(
        ctx.db.add_quota, env.group_id, env.sender_id, amount, "抢红包", env.sender_id
    )
    balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
    left = int(packet.get("left_count") or 0)

    if left > 0:
        yield env.event.plain_result(
            f"🧧 抢到 {amount} {cur} · 还剩 {left} 个\n余额 {balance}"
        )
    else:
        yield env.event.plain_result(
            f"🧧 抢到 {amount} {cur} · 这一个抢完了\n余额 {balance}"
        )


COMMANDS = [
    Command(
        key="red_packet_send",
        matcher=prefix(("发红包", "包红包", "发个红包")),
        handler=send_packet,
        perm_short="发红包",
        help=(
            HelpEntry(
                "发红包 55",
                "自己掏额度，默认拆成几个让群里抢",
                page="fun",
                order=40,
                group="玩一玩",
            ),
        ),
    ),
    Command(
        key="red_packet_grab",
        matcher=exact(("抢红包", "领红包", "开红包", "红包")),
        handler=grab,
        perm_short="抢红包",
        help=(
            HelpEntry(
                "抢红包",
                "抢群里那个还没抢完的包",
                page="fun",
                order=41,
                group="玩一玩",
            ),
        ),
    ),
]
