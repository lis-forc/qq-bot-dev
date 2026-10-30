"""个人信息：信息卡 / 改名 / 爱好。

信息卡是这个插件的门面，所以这里的逻辑比别处保守：
- 查别人时绝不顺手建档（`查 10001` 这种随手输的号码会污染成员表）；
- 画图失败一定要退化成文字卡，不能什么都不回。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.message_components import Image

from ..core.command import (
    Command,
    CommandEnv,
    HelpEntry,
    Match,
    custom,
    exact,
    prefix,
    require_level,
)
from ..core.context import PluginContext
from ..utils import deco
from ..utils import event as ev
from ..utils import nickname as nick_rule
from ..utils import onebot

# 改名卡的 item_id。和商店里那件商品的 item_id 必须一致 ——
# 这两个字符串分居两个文件，写错了买到的卡就永远用不上。
RENAME_CARD = "rename"
from ..utils.router import compact, strip_at
from . import nickname as nick_flow


def info_matcher(ctx: PluginContext, text: str, lower: str) -> Match | None:
    """`查` / `查 @某人` / `查 123456`。

    触发词来自配置（`info_triggers`），可以随时加别名，所以不用固定前缀匹配。

    **@ 那一支曾经漏了。** 文档一直写着「查 @某人」，但实现只认「纯口令」
    和「口令 + 数字」，于是 `查 @某人` 谁也接不住，群友看到的是「没反应」。
    这里补上：只要后面跟的是 ``@`` 就算命中，具体是谁交给消息组件
    （`ev.first_at`）—— 文本里的 @ 名字可能带空格（群名片是「🦊 陈嘉豪」），
    在这里解析它只会自找麻烦。
    """
    triggers = ctx.config.info_triggers
    if lower in triggers:
        return Match()
    for trigger in triggers:
        found = re.fullmatch(rf"{re.escape(trigger)}\s*(\d{{5,12}})", lower)
        if found:
            return Match(args=found.group(1), value=found.group(1))
        if re.fullmatch(rf"{re.escape(trigger)}\s*@.+", lower):
            return Match()
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
                    deco.head("没找到这个成员", "profile") + "\n"
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

    # 卡上不贴头像了：底图本身就是一张人像，再贴一个圆形头像就是一张图
    # 上两个人；而且头像要走一次网络下载，失败率不低，代价远大于收益。
    path = await asyncio.to_thread(ctx.cards.render, data)
    if path is None:
        yield event.plain_result(text_card(data))  # 画图失败也要有结果
        return
    yield event.chain_result([Image.fromFileSystem(str(path))])


def text_card(data: dict) -> str:
    """没装 Pillow 或画图失败时的文字版。

    字段与图片上那六项严格一致（姓名 / 性别 / 爱好 / 额度 / 注册天数 / 关系）
    —— 降级不该变成「另一种卡」，否则群友会以为自己查错了人。
    """
    lines = [deco.head("个人信息卡", "profile")]
    lines.append(f"姓名：{data['name']}")
    lines.append(f"性别：{data['gender']}")
    lines.append(f"爱好：{data.get('hobby') or '还没写'}")
    lines.append(f"{data['currency']}：{int(data.get('quota') or 0):,}")
    days = int(data.get("days") or 0)
    lines.append(f"注册天数：{days} 天" if days > 0 else "注册天数：今天刚来")
    lines.append(f"关系：{data.get('relation') or '无'}")
    if data.get("skin"):
        lines.append(f"名片：{data['skin']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 口令处理
async def info(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """信息卡：`查` 看自己，`查 @某人` / `查 123456` 看别人（2 级起）。

    **看别人要 2 级。** 信息卡上有余额、关系、注册天数，是别人的档案；
    普通成员只能看自己那张。判断放在这里而不是 Command.level，是因为
    同一个命令有两种用法 —— 看自己永远允许。
    """
    target = (
        ev.first_at(env.event, env.sender_id)
        or (env.args or "").strip()
        or env.sender_id
    )
    if str(target) != str(env.sender_id):
        warn = await require_level(ctx, env, 2)
        if warn:
            yield env.event.plain_result(warn)
            return
    async for reply in card_reply(
        ctx, env.event, env.group_id, env.sender_id, target, env.member
    ):
        yield reply


async def rename(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """改名。

    两条不同的路，分界线是**等级**，不是钱：

    - **3 级以上（管理、群主）**：直接改，自己改或代别人改都行。
      管人的手上有这个权限，是因为他们本来就在管事。
    - **1~2 级（普通成员、助手）**：改自己要消耗一张**改名卡**
      （商店「权限卡」里买，一张 50）。代别人改不行 —— 那是 3 级的事。

    为什么用卡而不是直接扣额度：卡在背包里能看见、能囤、能当礼物，
    而「改一次扣 50」在群里完全没有存在感，还容易被误解成罚款。
    """
    cfg = ctx.config
    targets = ev.at_targets(env.event, env.sender_id)

    if targets:
        # 代别人改：3 级起，不消耗卡。
        warn = await require_level(ctx, env, 3)
        if warn:
            yield env.event.plain_result(warn)
            return
        target_id = targets[0]
    else:
        target_id = env.sender_id
        # 给自己改：3 级以下要卡。等级要连手动授权一起算，所以走
        # permission.level 而不是看群角色。
        level = await ctx.permission.level(
            env.event, env.group_id, env.sender_id, env.member
        )
        if level < 3:
            # **第一次登记不收卡。** 名字还没定下来的人就是新人，而新人
            # 正被欢迎语催着改名 —— 这时候再让他先去买卡，就成了死循环：
            # 没名字 → 被催 → 买不起卡 → 改不了 → 一直催到静音。
            # 所以卡的收费点定在「改第二次及以后」。
            has_name = bool(str((env.member or {}).get("display_name") or ""))
            if has_name:
                stock = await asyncio.to_thread(
                    ctx.db.bag_count, env.group_id, env.sender_id, "permit", RENAME_CARD
                )
                if stock <= 0:
                    yield env.event.plain_result(
                        "🪪 改名要一张改名卡\n"
                        f"发「购买 改名卡」（{cfg.rename_card_price} {cfg.currency_name}）"
                    )
                    return

    # 改名**之前**的快照：apply_name 会把新名字写进库，之后再看
    # env.member 就分不清「他本来有没有名字」了。
    had_name = bool(str((env.member or {}).get("display_name") or ""))

    name = compact(strip_at(env.args))
    if not name:
        yield env.event.plain_result(nick_flow.format_hint(cfg))
        return

    # 先执行改名，成功了才扣卡 —— 名字不合规（太短、带符号）时不该白花一张。
    said = await nick_flow.apply_name(
        ctx, env.event, env.group_id, target_id, name, operator_id=env.sender_id
    )
    if not said.startswith("✅"):
        yield env.event.plain_result(said)
        return

    if not targets:
        level = await ctx.permission.level(
            env.event, env.group_id, env.sender_id, env.member
        )
        if level < 3 and had_name:
            await asyncio.to_thread(
                ctx.db.bag_take, env.group_id, env.sender_id, "permit", RENAME_CARD
            )
            left = await asyncio.to_thread(
                ctx.db.bag_count, env.group_id, env.sender_id, "permit", RENAME_CARD
            )
            said += f"\n🪪 用掉一张改名卡（还剩 {left} 张）"
    yield env.event.plain_result(said)


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

    和三态对应，第三个选项是「不设」（就是 🧸 那个记号）。原来这里叫
    「保密」，但保密和「没说」在展示上没有区别，两个词反而让人犹豫该选哪个。
    """
    targets = ev.at_targets(env.event, env.sender_id)
    if targets:
        warn = await require_level(ctx, env, 3)
        if warn:
            yield env.event.plain_result(warn)
            return
        target_id = targets[0]
    else:
        target_id = env.sender_id

    value = nick_rule.parse_sex(strip_at(env.args))
    if value is None:
        yield env.event.plain_result(
            "格式：性别 男\n"
            "可选：男 / 女 / 不设（不设的人挂 🧸）\n"
            "（代改：性别 @某人 女）"
        )
        return
    yield env.event.plain_result(
        await asyncio.to_thread(ctx.profiles.set_sex, env.group_id, target_id, value)
    )


# ---------------------------------------------------------------- 名片皮肤
RULE = "=" * 26


async def my_card(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """名片的现状：现在用哪张、有哪些、还能买什么。

    和「商店」刻意分开：商店是「挑东西」，这里是「我的档案」。
    混在一起的话，想确认「我现在用的是哪张」得在一堆商品里找。
    """
    wearing = await asyncio.to_thread(
        ctx.checkin.current_skin, env.group_id, env.sender_id
    )
    items = await asyncio.to_thread(
        ctx.checkin.skin_items, env.group_id, env.sender_id
    )
    cur = ctx.config.currency_name

    lines = [deco.head("我的名片", "card"), RULE, f"当前：{wearing or '基础名片（按性别自动给）'}"]
    if not items:
        lines.extend(
            [
                "",
                "现在只有基础名片（按性别自动发，不花钱），商店里还没上架别的。",
                "把图片放进 assets/images/cards/ 就会自动出现在这里，",
                "文件名就是名片名，比如「樱花.png」「樱花@50.png」。",
            ]
        )
        yield env.event.plain_result("\n".join(lines))
        return

    owned = [item for item in items if item.owned]
    lines.append("已拥有：" + ("、".join(item.name for item in owned) or "无"))
    lines.append("")
    lines.append(f"{deco.mark('shop')} 全部名片")
    for item in items:
        mark = "✓" if item.owned else "·"
        lines.append(f"{mark} {item.name}　{item.price} {cur}")
    lines.extend(["", RULE, "换名片 名字（换回默认：换名片 默认）"])
    yield env.event.plain_result("\n".join(lines))


async def wear_card(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """换名片。买过才能换，换回默认不需要花钱。"""
    word = env.args.strip()
    if not word:
        yield env.event.plain_result(
            "格式：换名片 樱花\n换回默认：换名片 默认\n发「名片」看看有哪些"
        )
        return

    ok, name, reason = await asyncio.to_thread(
        ctx.checkin.wear_skin, env.group_id, env.sender_id, word
    )
    if not ok:
        if reason == "还没买":
            yield env.event.plain_result(
                f"「{name}」还没买\n发「商店」看看价钱，买完自动换上"
            )
            return
        yield env.event.plain_result(
            f"⚠️ 找不到「{word}」这张名片\n发「名片」看看有哪些"
        )
        return
    yield env.event.plain_result(f"✅ 名片已换成「{name}」\n发「查」看看效果")


# ---------------------------------------------------------------- 命令定义
COMMANDS = [
    Command(
        key="info",
        matcher=custom(info_matcher),
        handler=info,
        perm_short="信息卡",
        help=(
            HelpEntry("查", "个人信息卡（图片）", page="base", order=10, group="我的信息"),
            # level=2：普通成员照抄这条会撞权限墙，那比不显示更让人恼火。
            HelpEntry(
                "查 @某人  /  查 QQ号", "看别人的信息卡（2 级起）",
                page="base", level=2, order=11, group="我的信息",
            ),
        ),
    ),
    Command(
        key="rename",
        matcher=prefix(("改名", "改名字", "改个名")),
        handler=rename,
        perm_short="改名",
        perm_extra=((3, "代改名"),),
        help=(
            HelpEntry("改名 李四", "改名字；3 级以下要消耗一张改名卡", page="base", order=20, group="资料"),
            HelpEntry("改名 @某人 李四", "替他改名（3 级起，不用卡）", page="admin", level=3, order=30),
        ),
    ),
    Command(
        key="hobby",
        matcher=prefix(("爱好", "设置爱好")),
        handler=hobby,
        help=(HelpEntry("爱好 打游戏", "设置信息卡上的爱好", page="base", order=30, group="资料"),),
    ),
    Command(
        key="sex",
        matcher=prefix(("性别", "设置性别", "改性别")),
        handler=sex,
        perm_short="性别",
        perm_extra=((3, "代改性别"),),
        help=(
            HelpEntry(
                "性别 男", "改信息卡上的性别（男 / 女 / 保密）", page="base", order=35, group="资料"
            ),
            HelpEntry("性别 @某人 女", "替他改性别（3 级起）", page="admin", level=3, order=31),
        ),
    ),
    Command(
        key="my_card",
        matcher=exact(("名片", "我的名片", "名片皮肤")),
        handler=my_card,
        perm_short="名片",
        help=(HelpEntry("名片", "我现在用的是哪张名片", page="base", order=70, group="资料"),),
    ),
    Command(
        key="wear_card",
        matcher=prefix(("换名片", "用名片", "佩戴名片")),
        handler=wear_card,
        perm_short="换名片",
        help=(HelpEntry("换名片 名字", "换一张买过的名片", page="base", order=71, group="资料"),),
    ),
]
