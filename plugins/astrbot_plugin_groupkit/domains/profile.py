"""个人信息：信息卡 / 改名 / 爱好。

信息卡是这个插件的门面，所以这里的逻辑比别处保守：
- 查别人时绝不顺手建档（`查 10001` 这种随手输的号码会污染成员表）；
- 画图失败一定要退化成文字卡，不能什么都不回。
"""

from __future__ import annotations

import asyncio
import re
import urllib.request
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.message_components import Image

from ..core.command import Command, CommandEnv, HelpEntry, Match, custom, prefix, require_level
from ..core.context import PluginContext
from ..utils import event as ev
from ..utils import onebot
from ..utils.router import compact, parse_sex, strip_at


def info_matcher(ctx: PluginContext, text: str, lower: str) -> Match | None:
    """`查` / `查 @某人` / `查 123456`。

    触发词来自配置（`info_triggers`），可以随时加别名，所以不用固定前缀匹配。
    """
    triggers = ctx.config.info_triggers
    if lower in triggers:
        return Match()
    for trigger in triggers:
        found = re.fullmatch(rf"{re.escape(trigger)}\s*(\d{{5,12}})", lower)
        if found:
            return Match(args=found.group(1), value=found.group(1))
    return None


async def card_reply(
    ctx: PluginContext, event, group_id: str, sender_id: str, target_id: str, member
) -> AsyncGenerator:
    """发一张信息卡。target 是自己时直接用已有的 member，省一次查询。"""
    info: dict | None = None
    if target_id == sender_id and member:
        target_member = member
    else:
        # 先只读，绝不顺手建档。否则 `查 10001` 这种随手输的号码会给
        # 陌生人凭空写一行 members，永久污染成员表。
        target_member = await asyncio.to_thread(ctx.db.get_member, group_id, target_id)
        if target_member is None:
            # 库里没有 = 从没在本群说过话，先问协议端他在不在群
            info = await onebot.get_member_info(event, group_id, target_id)
            if not info:
                yield event.plain_result(
                    "🔍 没找到这个成员\n"
                    "确认一下 QQ 号，或者人不在这个群里"
                )
                return
            target_member = await asyncio.to_thread(
                ctx.db.ensure_member, group_id, target_id, 0
            )

    # 补全真实入群时间 / 性别 / 群名片。缺字段才发请求，一个成员基本只查一次。
    target_member = await ctx.profiles.refresh_from_qq(
        event, group_id, target_id, target_member, info=info
    )

    counts = await asyncio.to_thread(ctx.db.counts, group_id, target_id)
    data = ctx.profiles.card_data(group_id, target_id, target_member, counts)

    avatar = await fetch_avatar(ctx, target_id)
    path = await asyncio.to_thread(ctx.cards.render, data, avatar)
    if path is None:
        yield event.plain_result(text_card(data))  # 画图失败也要有结果
        return
    yield event.chain_result([Image.fromFileSystem(str(path))])


async def fetch_avatar(ctx: PluginContext, user_id: str) -> bytes | None:
    """拉 QQ 头像。

    用公开的头像 CDN 而不是协议端接口：不占用 NapCat 的调用配额，
    也不需要机器人有任何额外权限。失败就返回 None，卡片会自动画首字占位。
    """
    if not ctx.config.card_show_avatar:
        return None
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

    def _download() -> bytes | None:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read()
        return data if len(data) > 512 else None

    try:
        return await asyncio.to_thread(_download)
    except Exception as exc:
        logger.warning(f"[群助手] 获取头像失败 {user_id}: {exc}")
        return None


def text_card(data: dict) -> str:
    """没装 Pillow 或画图失败时的文字版。字段顺序与图片一致。"""
    return "\n".join(
        [
            "🪪 个人信息卡",
            f"姓名：{data['name']}",
            f"性别：{data['gender']}",
            f"爱好：{data['hobby']}",
            f"{data['currency']}：{data['quota']}",
            f"在册：{data['days']} 天",
            f"今日：{data['today']} 条",
            f"本月：{data['month']} 条",
            f"累计：{data['total']} 条",
        ]
    )


# ---------------------------------------------------------------- 口令处理
async def info(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """信息卡：`查` 看自己，`查 @某人` / `查 123456` 看别人。"""
    target = env.args or ev.first_at(env.event, env.sender_id) or env.sender_id
    async for reply in card_reply(
        ctx, env.event, env.group_id, env.sender_id, target, env.member
    ):
        yield reply


async def rename(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """改名。给自己改花额度；管理帮别人改不扣对方额度，但需要 2 级。"""
    targets = ev.at_targets(env.event, env.sender_id)
    if targets:
        warn = await require_level(ctx, env, 2)  # 代改名要 2 级，改自己不占额度也不用权限
        if warn:
            yield env.event.plain_result(warn)
            return
        target_id = targets[0]
        charge = False  # 管理代改不扣对方额度
    else:
        target_id = env.sender_id
        charge = ctx.config.rename_cost > 0

    name = compact(strip_at(env.args))
    if not name:
        yield env.event.plain_result("✏️ 改名格式\n改名 李四\n改名 @某人 李四")
        return
    yield env.event.plain_result(
        await asyncio.to_thread(
            ctx.profiles.set_display_name, env.group_id, target_id, name, charge=charge
        )
    )


async def hobby(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """设置信息卡上的爱好。"""
    if not env.args:
        yield env.event.plain_result("格式：爱好 打游戏")
        return
    yield env.event.plain_result(
        await asyncio.to_thread(
            ctx.profiles.set_hobby, env.group_id, env.sender_id, strip_at(env.args)
        )
    )


async def sex(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """改性别：`性别 男` / `性别 @某人 女`。

    和「改名」同一个套路 —— 改自己 1 级，代改要 2 级。
    QQ 资料里的性别经常是空的或者乱填的，所以这里允许群友自己覆盖，
    覆盖之后不会再被协议端同步盖回去。
    """
    targets = ev.at_targets(env.event, env.sender_id)
    if targets:
        warn = await require_level(ctx, env, 2)
        if warn:
            yield env.event.plain_result(warn)
            return
        target_id = targets[0]
    else:
        target_id = env.sender_id

    value = parse_sex(strip_at(env.args))
    if value is None:
        yield env.event.plain_result(
            "格式：性别 男\n"
            "可选：男 / 女 / 保密\n"
            "（代改：性别 @某人 女）"
        )
        return
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.profiles.set_sex, env.group_id, target_id, value)
    )


# ---------------------------------------------------------------- 命令定义
COMMANDS = [
    Command(
        key="info",
        matcher=custom(info_matcher),
        handler=info,
        perm_short="信息卡",
        help=(
            HelpEntry("查", "个人信息卡（图片）", page="base", order=10),
            HelpEntry("查 @某人  /  查 QQ号", "看别人的", page="base", order=11),
        ),
    ),
    Command(
        key="rename",
        matcher=prefix(("改名", "改名字", "改个名")),
        handler=rename,
        perm_short="改名",
        perm_extra=((2, "代改名"),),
        help=(
            HelpEntry(
                "改名 李四", "改显示名，消耗 {rename_cost} {cur}", page="base", order=20
            ),
            HelpEntry("改名 @某人 李四", page="admin", level=2, order=30),
        ),
    ),
    Command(
        key="hobby",
        matcher=prefix(("爱好", "设置爱好")),
        handler=hobby,
        help=(HelpEntry("爱好 打游戏", "设置信息卡上的爱好", page="base", order=30),),
    ),
    Command(
        key="sex",
        matcher=prefix(("性别", "设置性别", "改性别")),
        handler=sex,
        perm_short="性别",
        perm_extra=((2, "代改性别"),),
        help=(
            HelpEntry(
                "性别 男", "改信息卡上的性别（男 / 女 / 保密）", page="base", order=35
            ),
            HelpEntry("性别 @某人 女", page="admin", level=2, order=31),
        ),
    ),
]
