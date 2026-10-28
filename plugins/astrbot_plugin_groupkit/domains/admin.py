"""群管：禁言 / 解禁 / 踢人 / 全群禁言 / 发扣额度 / 授权。

权限校验不在这里写 —— 每条命令在 registry.py 里声明了 `level`，
分发器会在调用处理函数之前统一校验。所以这里的函数都假设
「调用者已经有权限了」，只管做事和回话。

这样加新的管理命令时不会漏掉权限：忘记声明 level 会被默认成 1 级，
测试里有一条断言盯着「管理命令必须声明等级」。

额度的两条线别混：
- **发额度 = 无中生有**（4 级，印钱），给自己发也行；
- **扣额度 = 收走**（3 级起）；普通群友之间转钱是「给额度」（1 级，80% 到账），
  在 quota 域里，不在这里。

「授权」按调用者的等级受限：3 级只能设人 1~2 级（也不能动 3 级以上的人），
4 级才能发 3~4 级。详细规则写在管理台的数据页，群里不解释。
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


# 这些词出现在「禁言」后面时，说明对方其实想说的是全群禁言。
# 命中只回一句引导，绝不执行 —— 封整群是不可逆的群体影响，
# 不能靠一句自然语言猜出来。
WHOLE_BAN_HINTS = {"全体", "全体成员", "全员", "全群", "所有人", "全部"}


def mute_format(ctx: PluginContext) -> str:
    return (
        "格式：禁言 @某人 [时长]\n"
        f"不写时长默认 {ctx.config.default_mute_minutes} 分钟"
    )


async def mute(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """禁言 @某人 [时长]。只禁被 @ 的那一个人。

    全群禁言是独立口令「全群禁言」，不跟这里混：一个口令只做一件事，
    误触的代价才可控。有人顺手打成「禁言 全体」时，这里只做引导。
    """
    arg_text = strip_at(env.args)
    targets = ev.at_targets(env.event, env.sender_id)

    if not targets:
        if ev.at_all(env.event) or arg_text in WHOLE_BAN_HINTS:
            yield env.event.plain_result(
                "要全群禁言请用「全群禁言」\n"
                "只禁一个人：禁言 @某人"
            )
            return
        yield env.event.plain_result(ev.target_hint(env.event, mute_format(ctx)))
        return

    # 不写时长就用配置的默认值。QQ 里「禁言他」是最常见的说法，
    # 每次都得打「10分钟」纯属多余。
    if arg_text:
        seconds = parse_duration(arg_text)
        if seconds is None:
            yield env.event.plain_result(f"⚠️ 没看懂时长：{arg_text}\n{mute_format(ctx)}")
            return
    else:
        seconds = ctx.config.default_mute_minutes * 60

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
        f"✅ 已禁言 {fmt_duration(seconds)}" if done else "⚠️ 禁言失败，检查机器人权限"
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
    yield env.event.plain_result("✅ 已解除禁言" if done else "⚠️ 解禁失败，检查机器人权限")


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
        "✅ 已踢出并拉黑\n该成员不能再申请入群" if reject else "✅ 已踢出该成员"
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
        yield env.event.plain_result("✅ 已开启全群禁言" if enable else "✅ 已解除全群禁言")
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
        f"{'✅ 已发放' if sign > 0 else '✅ 已扣除'} {amount} {cur}\n对方当前：{balance}"
    )


async def grant(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """授权 @某人 等级。按调用者的等级受限：

    - 3 级（管理员）：只能设 1~2 级，而且动不了现在已是 3 级以上的人；
    - 4 级：1~4 任意。

    等级是「取高」模型（见 Permission.level），所以把 QQ 管理员降到 1
    实际降不掉 —— 他的 3 级来自群角色。这条不在群里解释，写进管理台。
    """
    targets = ev.at_targets(env.event, env.sender_id)
    level = parse_int(strip_at(env.args))
    if not targets or level < 1 or level > 4:
        yield env.event.plain_result("格式：授权 @某人 2\n（1~4 级）")
        return
    target = targets[0]
    if target == env.sender_id:
        yield env.event.plain_result("给自己授权？你的等级来自你现在的身份")
        return
    if target == ev.self_id(env.event):
        yield env.event.plain_result("机器人不需要权限，它只听配置的")
        return

    ok, viewer = await ctx.permission.require(
        env.event, env.group_id, env.sender_id, env.member, 3
    )
    if not ok:
        yield env.event.plain_result("⚠️ 需要「管理」权限")
        return
    if viewer < 4 and level > 2:
        yield env.event.plain_result(
            "⚠️ 管理员只能设 1~2 级\n3~4 级要群主来发"
        )
        return
    if viewer < 4:
        # 目标的等级要连「手动授权」一起算 —— 只查群角色的话，
        # 被手动授过 3 级的人在管理员眼里就是普通成员，会被越权改动。
        target_member = await asyncio.to_thread(ctx.db.get_member, env.group_id, target)
        current = await ctx.permission.level(env.event, env.group_id, target, target_member)
        if current >= 3:
            yield env.event.plain_result(
                "⚠️ 这位是 3 级以上，管理员动不了\n要调整请找群主"
            )
            return

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
        help=(HelpEntry("禁言 @某人 [时长]", "让他一段时间不能说话", page="admin", level=2, order=10),),
    ),
    Command(
        key="unmute",
        matcher=prefix(("解禁", "解除禁言")),
        handler=unmute,
        level=2,
        perm_short="解禁",
        help=(HelpEntry("解禁 @某人", "提前放出来", page="admin", level=2, order=20),),
    ),
    Command(
        key="kick",
        matcher=prefix(("踢", "踢人", "踢出", "移出")),
        handler=kick,
        level=3,
        perm_short="踢人",
        help=(HelpEntry("踢 @某人 [拉黑]", "移出群，加「拉黑」就不能再申请", page="admin", level=3, order=10),),
    ),
    Command(
        key="whole_ban_on",
        matcher=exact(("全群禁言", "全体禁言", "禁言全体", "全员禁言")),
        handler=_whole_ban_cmd(True),
        level=4,
        perm_short="全群禁言",
        help=(HelpEntry("全群禁言", "全员闭嘴", page="admin", level=4, order=10, slot="wholeban"),),
    ),
    Command(
        key="whole_ban_off",
        matcher=exact(("全群解禁", "解除全群禁言", "解除全员禁言", "解除禁言全体")),
        handler=_whole_ban_cmd(False),
        level=4,
        perm_short="全群解禁",
        help=(HelpEntry("全群解禁", "恢复正常", page="admin", level=4, order=11, slot="wholeban"),),
    ),
    Command(
        key="quota_add",
        matcher=prefix(("发额度", "加额度")),
        handler=_adjust_cmd(1),
        level=4,
        perm_short="发额度",
        help=(HelpEntry("发额度 @某人 50", "无中生有（可以给自己），4 级专用", page="admin", level=4, order=20),),
    ),
    Command(
        key="quota_sub",
        matcher=prefix(("扣额度", "减额度", "罚额度")),
        handler=_adjust_cmd(-1),
        level=3,
        perm_short="扣额度",
        help=(HelpEntry("扣额度 @某人 50", "扣他的钱，3 级起", page="admin", level=3, order=30),),
    ),
    Command(
        key="grant",
        matcher=prefix(("授权", "设权限", "设置权限")),
        handler=grant,
        level=3,
        perm_short="授权",
        help=(HelpEntry("授权 @某人 2", "管理员可设 1~2 级；3~4 级要 4 级来发", page="admin", level=3, order=20),),
    ),
]
