"""假装消息：用别人的身份发一条合并转发。

用途是玩梗 —— 「假装消息 @某人 你好呀」会以那个人的头像和昵称，发一条
聊天记录出去。QQ 的合并转发节点本来就带 ``uin`` 和 ``name``，客户端拿
uin 去拉头像，所以这件事不需要任何 hack，是协议自身的正常功能。

**为什么要收费。** 这是整套插件里唯一一个「能替别人发言」的能力。
免费就等于把全群人的身份借给所有人玩，而且刷起来没成本。一次几额度
是个门槛：不贵到没人玩，也不至于变成骚扰工具。

**为什么坚持用合并转发。** 它在群里显示成一条聊天记录（右侧写着
「1 条聊天记录」），点开才看到内容 —— 一看就知道是打包的，而不是
「他本人刚说了这句话」。这是这个玩法唯一的安全边界，不能省。构造失败
时就退回普通消息，并且**明确标注「假装是 X 说」**，绝不裸着冒充。

**名字优先用 QQ 昵称。** 群名片里带性别记号（「🦊 陈嘉豪」），拿它当
发言人名字会把这个记号也印到伪造气泡上，一眼假。群里显示的名字本来就
是昵称时更自然。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, prefix
from ..core.context import PluginContext
from ..utils import deco
from ..utils import event as ev
from ..utils import nickname as nick_util
from ..utils import onebot
from ..utils.forward import build_forward_as
from ..utils.router import compact
from .sticker import chain_text, strip_mentions

# 前缀越长越先匹配：`假装消息` 必须在 `假装` 之前。
PREFIXES: tuple[str, ...] = ("假装消息", "假装说", "假装发言")

# 正文最短/最长。一个字是「嗯」「哦」这种真实回复，再短就没意义了；
# 太长会把一个玩笑变成小作文。
MIN_BODY = 1
MAX_BODY = 120


def _strip_prefix(text: str) -> str:
    """切掉「假装消息」前缀，留下正文。不是这个命令返回空串。

    **不要求前缀后面必须有空格。** 消息链里的 Plain 段可能不带尾随空格
    （`Plain("假装消息")` + At + `Plain("你好")`），拼起来就是「假装消息你好」。
    如果坚持「必须有分隔符」，这种拼法会被判成「没写正文」，于是群友
    明明说了话，却收到一条用法提示 —— 这正是修过的那个 bug。
    """
    cleaned = compact(text)
    for word in PREFIXES:
        if not cleaned.startswith(word):
            continue
        return cleaned[len(word):].strip("：:，,、 ")
    return ""


def split_target(body: str, event: Any, sender_id: str) -> tuple[str | None, str]:
    """把「@某人 你好呀」拆成 (QQ 号, 正文)。

    优先取消息组件里的 @ —— 那是可信的；没有 @ 时退一步认开头的
    QQ 号（想假装不在群里的人，这是唯一写法）。
    """
    target = ev.first_at(event, sender_id)
    text = body.strip()
    if target:
        return target, text
    head, _, rest = text.partition(" ")
    if head.isdigit() and 5 <= len(head) <= 12:
        return head, rest.strip()
    return None, text


async def _speaker(ctx: PluginContext, env: CommandEnv, user_id: str) -> tuple[str, str]:
    """这个 QQ 号在群里叫什么。返回 (uin, 显示名)。

    顺序是「QQ 昵称 → 群名片（剥掉性别记号）→ 库里的名字」：
    伪造气泡上印的是昵称最自然，印「🦊 陈嘉豪」会一眼看出是机器人干的。
    """
    info = await onebot.get_member_info(env.event, env.group_id, user_id)
    if isinstance(info, dict):
        raw = str(info.get("nickname") or "").strip()
        if raw:
            return user_id, raw
        card = str(info.get("card") or "").strip()
        if card:
            stripped = nick_util.strip(card, ctx.config)
            if stripped:
                return user_id, stripped
    member = await asyncio.to_thread(ctx.db.get_member, env.group_id, user_id)
    return user_id, ctx.profiles.display_name(member, user_id)


def _usage(ctx: PluginContext) -> str:
    cur = ctx.config.currency_name
    cost = ctx.config.fake_cost
    note = f"每次 {cost} {cur}" if cost > 0 else "免费"
    return (
        "🎭 假装消息：借用别人的头像和昵称说一句\n"
        "\n"
        "  假装消息 @某人 你好呀\n"
        "  假装消息 123456789 你好呀　　（不在群里就填 QQ 号）\n"
        "\n"
        f"发出去是合并转发，点开才看到内容 · {note}"
    )


async def fake(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """假装消息 @某人 正文。"""
    # 参数从**消息链的纯文本**里取：@ 段天然被跳过，不会像 message_str
    # 那样留下「🦊 陈嘉豪」这种被空格截断的半截名字（表情命令踩过这个坑）。
    source = chain_text(env.event) or (env.text or "")
    body = _strip_prefix(source)
    # 走 message_str 兜底时，@ 会被渲染成「@🦊 陈嘉豪」留在正文里。
    # 不剥掉的话，伪造出来的那条消息开头会多一个 @，一眼就看得出是假的。
    body = strip_mentions(
        body, env.event, nick_util.known_marks(ctx.config)
    ).strip()
    if not body:
        yield env.event.plain_result(_usage(ctx))
        return

    target, body = split_target(body, env.event, env.sender_id)
    if not target or not body.strip():
        yield env.event.plain_result(_usage(ctx))
        return
    if len(body) > MAX_BODY:
        yield env.event.plain_result(
            f"⚠️ 太长了一点，控制在 {MAX_BODY} 字以内"
        )
        return

    cost = ctx.config.fake_cost
    if cost > 0:
        balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
        if balance < cost:
            yield env.event.plain_result(
                f"⚠️ 不够 {cost} {ctx.config.currency_name}（你有 {balance}）"
            )
            return
        # 先扣再发：发失败了钱已经花掉，所以要在这里就把「失败不扣」的判断
        # 做成不可能发生 —— 构造转发是纯内存操作，下面不会失败到需要退钱。
        await asyncio.to_thread(
            ctx.db.add_quota,
            env.group_id,
            env.sender_id,
            -cost,
            "假装消息",
            env.sender_id,
        )

    uin, name = await _speaker(ctx, env, target)
    chain = build_forward_as(env.event, uin, name, body)
    if chain:
        yield env.event.chain_result(chain)
        return

    # 协议端不支持伪造节点：退回普通消息，但必须写明这是假装。
    yield env.event.plain_result(f"{deco.mark('edit')} 假装是 {name} 说：\n{body}")


COMMANDS = [
    Command(
        key="fake_message",
        matcher=prefix(PREFIXES),
        handler=fake,
        perm_short="假装消息",
        help=(
            HelpEntry(
                "假装消息 @某人 你好呀",
                "用他的头像和昵称发一条聊天记录",
                page="fun",
                order=30,
                group="玩一玩",
            ),
        ),
    ),
]
