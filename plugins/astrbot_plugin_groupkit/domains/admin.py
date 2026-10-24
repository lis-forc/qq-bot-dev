"""群管：禁言 / 解禁 / 踢人 / 全群禁言 / 发扣额度 / 授权。

权限校验不在这里写 —— 每条命令在 registry.py 里声明了 `level`，
分发器会在调用处理函数之前统一校验。所以这里的函数都假设
「调用者已经有权限了」，只管做事和回话。

这样加新的管理命令时不会漏掉权限：忘记声明 level 会被默认成 1 级，
测试里有一条断言盯着「管理命令必须声明等级」。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, exact, prefix
from ..core.context import PluginContext
from ..core.services import Permission
from ..utils import event as ev
from ..utils import onebot
from ..utils.router import fmt_duration, parse_duration, parse_int, strip_at


async def mute(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """禁言 @某人 [时长]。"""
    targets = ev.at_targets(env.event, env.sender_id)
    if not targets:
        yield env.event.plain_result(ev.target_hint(env.event, "格式：禁言 @某人 10分钟"))
        return
    seconds = parse_duration(strip_at(env.args))
    if seconds is None:
        yield env.event.plain_result("⚠️ 没看懂时长\n格式：禁言 @某人 10分钟")
        return
    if seconds == 0:
        yield env.event.plain_result("⚠️ 0 分钟就是没禁言，要解禁请用「解禁 @某人」")
        return
    if not await onebot.is_bot_admin(env.event, env.group_id):
        yield env.event.plain_result("⚠️ 机器人没有群管理权限，没法禁言")
        return
    target = targets[0]
    if target in {env.sender_id, ev.self_id(env.event)}:
        yield env.event.plain_result("⚠️ 这个目标不能禁言")
        return

    done = await onebot.mute(env.event, env.group_id, target, seconds)
    if done:
        await asyncio.to_thread(
            ctx.db.log_op, env.group_id, env.sender_id, "禁言", target, str(seconds)
        )
    yield env.event.plain_result(
        f"🔇 已禁言 {fmt_duration(seconds)}" if done else "⚠️ 禁言失败，检查机器人权限"
    )


async def unmute(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """解禁 @某人。"""
    targets = ev.at_targets(env.event, env.sender_id)
    if not targets:
        yield env.event.plain_result(ev.target_hint(env.event, "格式：解禁 @某人"))
        return
    if not await onebot.is_bot_admin(env.event, env.group_id):
        yield env.event.plain_result("⚠️ 机器人没有群管理权限，没法解禁")
        return
    done = await onebot.unmute(env.event, env.group_id, targets[0])
    if done:
        await asyncio.to_thread(
            ctx.db.log_op, env.group_id, env.sender_id, "解禁", targets[0]
        )
    yield env.event.plain_result("🔊 已解除禁言" if done else "⚠️ 解禁失败，检查机器人权限")


async def kick(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """踢 @某人 [拉黑]。"""
    targets = ev.at_targets(env.event, env.sender_id)
    if not targets:
        yield env.event.plain_result(ev.target_hint(env.event, "格式：踢 @某人  [拉黑]"))
        return
    target = targets[0]
    if target in {env.sender_id, ev.self_id(env.event)}:
        yield env.event.plain_result("⚠️ 这个目标不能踢")
        return
    if not await onebot.is_bot_admin(env.event, env.group_id):
        yield env.event.plain_result("⚠️ 机器人没有群管理权限，没法踢人")
        return

    # 「踢 @某人 拉黑」= 踢出去并且不再同意他入群。
    # 不带后缀就是普通踢人，他随时能再申请进来 —— 这个区别值得一个口令区分开。
    reject = parse_kick_reject(env.args)
    done = await onebot.kick(env.event, env.group_id, target, reject_add_request=reject)
    if not done:
        yield env.event.plain_result("⚠️ 踢人失败，检查机器人权限")
        return
    await asyncio.to_thread(
        ctx.db.log_op, env.group_id, env.sender_id, "踢人+拉黑" if reject else "踢人", target
    )
    yield env.event.plain_result(
        "👋 已踢出并拉黑\n该成员不能再申请入群" if reject else "👋 已踢出该成员"
    )


async def whole_ban(ctx: PluginContext, env: CommandEnv, enable: bool) -> AsyncGenerator:
    """全群禁言 / 全群解禁。"""
    if not await onebot.is_bot_admin(env.event, env.group_id):
        yield env.event.plain_result("⚠️ 机器人没有群管理权限，没法操作全群禁言")
        return
    done = await onebot.set_whole_ban(env.event, env.group_id, enable)
    if done:
        await asyncio.to_thread(
            ctx.db.log_op,
            env.group_id, env.sender_id,
            "全群禁言" if enable else "解除全群禁言",
        )
        yield env.event.plain_result("🔇 已开启全群禁言" if enable else "🔊 已解除全群禁言")
    else:
        yield env.event.plain_result("⚠️ 操作失败，检查机器人权限")


async def adjust_quota(ctx: PluginContext, env: CommandEnv, sign: int) -> AsyncGenerator:
    """发额度 / 扣额度 @某人 数量。扣不到负数。"""
    cur = ctx.config.currency_name
    targets = ev.at_targets(env.event, env.sender_id)
    amount = abs(parse_int(strip_at(env.args)))
    fmt = f"格式：{'发' if sign > 0 else '扣'}{cur} @某人 50"
    if not targets:
        yield env.event.plain_result(ev.target_hint(env.event, fmt))
        return
    if amount <= 0:
        yield env.event.plain_result(fmt)
        return

    target = targets[0]
    ok, balance = await asyncio.to_thread(
        ctx.db.add_quota, env.group_id, target, amount * sign,
        "管理发放" if sign > 0 else "管理扣除", env.sender_id,
    )
    if not ok:
        yield env.event.plain_result(f"⚠️ 对方{cur}不足，扣不到负数\n当前：{balance}")
        return
    await asyncio.to_thread(
        ctx.db.log_op, env.group_id, env.sender_id,
        "发额度" if sign > 0 else "扣额度", target, str(amount),
    )
    yield env.event.plain_result(
        f"{'💎 已发放' if sign > 0 else '💸 已扣除'} {amount} {cur}\n对方当前：{balance}"
    )


async def grant(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """授权 @某人 等级。

    这里只校验「等级 ≥ 4」，不校验身份 —— 群主授到 4 级的人，
    同样可以继续给别人授权。
    """
    targets = ev.at_targets(env.event, env.sender_id)
    level = parse_int(strip_at(env.args))
    if not targets or level < 1 or level > 4:
        yield env.event.plain_result("格式：授权 @某人 3\n（1~4 级）")
        return
    target = targets[0]
    await asyncio.to_thread(ctx.db.ensure_member, env.group_id, target, 0)
    await asyncio.to_thread(ctx.db.set_permission, env.group_id, target, level)
    await asyncio.to_thread(
        ctx.db.log_op, env.group_id, env.sender_id, "授权", target, str(level)
    )
    yield env.event.plain_result(
        f"✅ 已将该成员设为 {level} 级（{Permission.level_name(level)}）"
    )


# ---------------------------------------------------------------- 拉黑识别
# 只做包含匹配，位置随意：拉黑可以写在 @ 前面也可以写在后面。
KICK_REJECT_WORDS = ("拉黑", "拒收", "拒绝", "黑名单")


def parse_kick_reject(args: str) -> bool:
    """踢人参数里有没有「拉黑」。

    抽成函数只为了能单测：这是「他还能不能再进来」的分叉，
    判错了不会报错，只会在几天后有人重新申请入群时才发现。
    """
    return any(word in (args or "") for word in KICK_REJECT_WORDS)


# ---------------------------------------------------------------- 命令定义
def _whole_ban_cmd(enable: bool) -> Command:
    async def handler(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
        async for reply in whole_ban(ctx, env, enable):
            yield reply

    return handler


def _adjust_cmd(sign: int) -> Command:
    async def handler(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
        async for reply in adjust_quota(ctx, env, sign):
            yield reply

    return handler


COMMANDS = [
    Command(
        key="mute",
        matcher=prefix(("禁言", "口球")),
        handler=mute,
        level=2,
        perm_short="禁言",
        help=(HelpEntry("禁言 @某人 10分钟", page="admin", level=2, order=10),),
    ),
    Command(
        key="unmute",
        matcher=prefix(("解禁", "解除禁言")),
        handler=unmute,
        level=2,
        perm_short="解禁",
        help=(HelpEntry("解禁 @某人", page="admin", level=2, order=20),),
    ),
    Command(
        key="kick",
        matcher=prefix(("踢", "踢人", "踢出", "移出")),
        handler=kick,
        level=3,
        perm_short="踢人",
        help=(HelpEntry("踢 @某人 [拉黑]", page="admin", level=3, order=10),),
    ),
    Command(
        key="whole_ban_on",
        matcher=exact(("全群禁言", "全体禁言", "禁言全体", "全员禁言")),
        handler=_whole_ban_cmd(True),
        level=4,
        perm_short="全群禁言",
        help=(HelpEntry("全群禁言", page="admin", level=4, order=10, slot="wholeban"),),
    ),
    Command(
        key="whole_ban_off",
        matcher=exact(("全群解禁", "解除全群禁言", "解除全员禁言", "解除禁言全体")),
        handler=_whole_ban_cmd(False),
        level=4,
        perm_short="全群解禁",
        help=(HelpEntry("全群解禁", page="admin", level=4, order=11, slot="wholeban"),),
    ),
    Command(
        key="quota_add",
        matcher=prefix(("发额度", "给额度", "加额度")),
        handler=_adjust_cmd(1),
        level=3,
        perm_short="发额度",
        help=(HelpEntry("发额度 @某人 50", page="admin", level=3, order=20),),
    ),
    Command(
        key="quota_sub",
        matcher=prefix(("扣额度", "减额度", "罚额度")),
        handler=_adjust_cmd(-1),
        level=3,
        perm_short="扣额度",
        help=(HelpEntry("扣额度 @某人 50", page="admin", level=3, order=30),),
    ),
    Command(
        key="grant",
        matcher=prefix(("授权", "设权限", "设置权限")),
        handler=grant,
        level=4,
        perm_short="授权",
        help=(HelpEntry("授权 @某人 3", page="admin", level=4, order=20),),
    ),
]
