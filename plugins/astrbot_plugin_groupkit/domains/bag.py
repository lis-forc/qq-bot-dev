"""背包：存放买来的消耗品 —— 头衔、关系，以及抽奖次数。

三样东西住在背包里，但生效方式不同：

- **头衔**：背包里存的是「修改机会」。`头衔 想要的字` 消耗一个 → 把**自定义
  文字**写进佩戴位（签到文字、信息卡都显示它），然后 **@ 群主请他人肉去改**。
  QQ 的群专属头衔只有群主能设置别人，机器人调接口改不动 —— 这是 QQ 的权限
  设计，绕不开。所以机器人的职责是「记录 + 提醒」：提醒一次交易就算完成，
  群主改不改、什么时候改是他的事，插件不追、不催、不重复提醒。
- **关系**：`关系 挚友 @某人` 发起结缔。对方同意才真正挂上（双方各写一份）；
  拒绝或超时，资格退回背包。这是背包里唯一要等别人点头的物品，
  所以这里有一套和入群验证同款的「pending + 定时器」流程。
- **抽奖次数**：它其实存在 members.draws（抽奖口令自己扣），背包只是
  把这个数字**展示**出来 —— 「背包里还可以放抽奖次数」指的就是它。

空背包的固定回复是「空空如也~」—— 群主要求的原话，保留语气词。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

try:  # 本地自测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

from ..core.checkin import RELATIONS
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
from ..utils import deco, event as ev, onebot

RULE = "=" * 26

# 背包里头衔的固定 item_id —— 它不绑定任何具体内容，只是「一次修改机会」。
TITLE_ITEM = "title"

# 禁言卡的 item_id 与它禁多久。10 秒是刻意的：这张卡卖的是「玩一下」，
# 不是「管人」。真有管理等级的人用「禁言」口令，那条路看的是权限不是钱。
MUTE_CARD = "mute"
MUTE_SECONDS = 10

# 自定义头衔的长度上限。QQ 专属头衔本身有上限（各协议端 12~18 不等），
# 取最保守的 12：超了协议端会拒，与其让它静默失败不如这里就拦下。
TITLE_MAX_LEN = 12

# 结缔请求的等待窗口。太短对方来不及看到，太长买家的资格被白扣着。
RELATION_TIMEOUT_SECONDS = 5 * 60

# 对方的答复只认这几个词。**精确匹配**：TA 的其他消息照常走聊天，
# 不打断正常说话 —— 拦全部消息等于把人「扣住」五分钟，那比不打扰更糟。
_AGREE = ("同意", "好", "好呀", "好啊", "可以", "确定", "OK", "ok", "Ok")
_REJECT = ("不同意", "拒绝", "不行", "不要", "不了")


@dataclass
class Verdict:
    """``relation_intercept`` 的返回值，语义与入群验证的 Verdict 一致。"""

    handled: bool = False
    reply: str = ""


@dataclass
class RelationRequest:
    """一次还没被答复的结缔请求。"""

    group_id: str
    target_id: str
    buyer_id: str
    buyer_name: str
    relation: str
    deadline: float
    event: Any = field(default=None, repr=False)
    task: Any = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.group_id, self.target_id)

    def seconds_left(self) -> int:
        return max(0, int(self.deadline - time.time()))


# 进程内状态：key = (group_id, target_id)。一个人同时只挂一个请求 ——
# 两份请求同时等他答复，机器人自己都会先乱。
_PENDING: dict[tuple[str, str], RelationRequest] = {}
_TASKS: set[asyncio.Task] = set()


# ---------------------------------------------------------------- 背包文案
def _item_label(kind: str, item_id: str) -> str:
    """背包里的 item_id → 展示名。头衔只有一种（修改机会），关系存名字本身。"""
    if kind == "title":
        return "头衔"
    return item_id


def bag_text(ctx: PluginContext, group_id: str, user_id: str) -> str:
    """「背包」的正文。空的就一句「空空如也~」，别画蛇添足。"""
    items = ctx.db.bag_items(group_id, user_id)
    member = ctx.db.get_member(group_id, user_id) or {}
    draws = int(member.get("draws") or 0)

    if not items and draws <= 0:
        return deco.head("背包", "bag") + "\n空空如也~\n去商店买点头衔放进来吧"

    buckets: dict[str, list[str]] = {}
    for item in items:
        label = _item_label(item["kind"], str(item["item_id"]))
        buckets.setdefault(str(item["kind"]), []).append(f"{label} ×{item['count']}")

    lines = [deco.head("背包", "bag"), RULE]
    if buckets.get("title"):
        lines.append(f"{deco.mark('titles')} 头衔：" + "、".join(buckets["title"]))
    if buckets.get("relation"):
        lines.append(f"{deco.mark('relations')} 关系：" + "、".join(buckets["relation"]))
    if draws > 0:
        # 不写「（发「抽奖」使用）」：能打开背包的人都见过「抽奖」这个口令，
        # 而每多一句解释，背包就多一行要读的东西。
        lines.append(f"{deco.mark('quota')} 抽奖次数 ×{draws}")
    lines.extend([RULE, "头衔 想要的字 · 关系 名字 @某人"])
    return "\n".join(lines)


async def use_mute_card(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """禁言卡 @某人：拿一张卡换他闭嘴 10 秒。

    三条硬规矩：

    1. **只能对 3 级以下的人用。** 钱买到的是「玩一下」，不是管理权；
       让普通人能禁言管理员，这功能第二天就会被用来打群架。
    2. **机器人必须是管理员**，否则 `set_group_ban` 会失败 —— 失败要把卡
       退回去，不能扣了卡什么也没发生。
    3. **先扣卡再禁言**，失败了退回来。反过来的话（先禁言再扣卡），
       重复点击会禁两次只扣一张。
    """
    targets = ev.at_targets(env.event, env.sender_id)
    if not targets:
        yield env.event.plain_result("格式：禁言卡 @某人\n一张卡让他安静 10 秒")
        return
    target = targets[0]
    if target in {env.sender_id, ev.self_id(env.event)}:
        yield env.event.plain_result("⚠️ 这个目标不能用")
        return

    # 目标等级：管理及以上不吃这张卡。等级要连手动授权一起算。
    target_member = await asyncio.to_thread(ctx.db.get_member, env.group_id, target)
    target_level = await ctx.permission.level(
        env.event, env.group_id, target, target_member
    )
    if target_level >= 3:
        yield env.event.plain_result("⚠️ 管理及以上不吃这张卡\n要管人请用「禁言」口令")
        return

    stock = await asyncio.to_thread(
        ctx.db.bag_count, env.group_id, env.sender_id, "permit", MUTE_CARD
    )
    if stock <= 0:
        yield env.event.plain_result(
            "🔇 你还没有禁言卡\n"
            f"发「购买 禁言卡」（{ctx.config.mute_card_price} {ctx.config.currency_name}）"
        )
        return

    if not await onebot.is_bot_admin(env.event, env.group_id):
        yield env.event.plain_result("⚠️ 机器人没有群管理权限，禁不了")
        return

    taken = await asyncio.to_thread(
        ctx.db.bag_take, env.group_id, env.sender_id, "permit", MUTE_CARD
    )
    if not taken:
        yield env.event.plain_result("🔇 卡不够了，刷新一下背包看看")
        return

    done = await onebot.mute(env.event, env.group_id, target, MUTE_SECONDS)
    if not done:
        # 卡已经扣了、人没禁住：退回去。宁可多一条日志，不能吞卡。
        await asyncio.to_thread(
            ctx.db.bag_add, env.group_id, env.sender_id, "permit", MUTE_CARD, 1
        )
        yield env.event.plain_result("⚠️ 禁言没成功，卡已经退回背包")
        return

    name = ctx.profiles.display_name(target_member, target)
    left = await asyncio.to_thread(
        ctx.db.bag_count, env.group_id, env.sender_id, "permit", MUTE_CARD
    )
    await asyncio.to_thread(
        ctx.db.log_op, env.group_id, env.sender_id, "禁言卡", target, f"{MUTE_SECONDS}秒"
    )
    yield env.event.plain_result(
        f"🔇 {name} 安静 {MUTE_SECONDS} 秒（还剩 {left} 张卡）"
    )


# ---------------------------------------------------------------- 口令
async def _who(ctx: PluginContext, env: CommandEnv) -> tuple[str, str]:
    """看谁的背包。返回 (目标 QQ, 权限不足的提示)。

    没 @ 就是看自己的，永远允许；@ 了别人要 2 级（助手）。普通成员只能
    看自己那份 —— 「别人的包里有什么」属于别人的东西。
    """
    targets = ev.at_targets(env.event, env.sender_id)
    if not targets:
        return env.sender_id, ""
    warn = await require_level(ctx, env, 2)
    return targets[0], (warn or "")


async def bag_cmd(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    target, warn = await _who(ctx, env)
    if warn:
        yield env.event.plain_result(warn)
        return
    yield env.event.plain_result(
        await asyncio.to_thread(bag_text, ctx, env.group_id, target)
    )


async def use_title(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """`头衔 想要的字`：消耗一次修改机会，提醒群主人肉去改群头衔。

    QQ 的专属头衔只有群主能设置别人，机器人调 ``set_group_special_title``
    是改不动的 —— 所以流程是「记录申请 → @ 群主」，提醒一次即交易完成。
    """
    word = env.args.strip()
    if not word:
        stock = await asyncio.to_thread(
            ctx.db.bag_count, env.group_id, env.sender_id, "title", TITLE_ITEM
        )
        lines = ["格式：头衔 想要的字（比如：头衔 夜行者）"]
        lines.append(f"你背包里有 {stock} 次修改机会" if stock else "你背包里还没有修改机会，发「购买 头衔」买一次")
        yield env.event.plain_result("\n".join(lines))
        return

    if word in {"无", "不戴", "摘掉", "空"}:
        # 只清插件里的佩戴位（签到文字 / 信息卡不再显示）。
        # 群头衔本身是群主手改的，插件摘不掉 —— 真要去掉得请群主动手。
        await asyncio.to_thread(ctx.db.equip_title, env.group_id, env.sender_id, "")
        yield env.event.plain_result(
            "✅ 插件里的头衔显示已摘下（修改机会不受影响）\n"
            "群头衔本身要请群主去 QQ 群设置里清掉"
        )
        return

    if len(word) > TITLE_MAX_LEN:
        yield env.event.plain_result(
            f"头衔最多 {TITLE_MAX_LEN} 个字\n「{word}」有 {len(word)} 个字，删短一点"
        )
        return

    # 先找人再扣账：找不到群主时这次申请不生效，机会也不扣 ——
    # 「提醒」是这个交易里机器人唯一要交付的东西，交付不了就不收钱。
    owner_id = await onebot.get_group_owner(env.event, env.group_id)
    if not owner_id:
        yield env.event.plain_result(
            "⚠️ 暂时找不到群主（没拿到群成员列表），这次先不扣机会\n"
            "稍后再试，或者直接私聊群主"
        )
        return

    # 先扣修改机会再记录 —— 顺序不能反，不然「没有也能申请」。
    taken = await asyncio.to_thread(
        ctx.db.bag_take, env.group_id, env.sender_id, "title", TITLE_ITEM
    )
    if not taken:
        yield env.event.plain_result(
            "你背包里没有改头衔的机会了\n发「购买 头衔」买一次，或发「背包」看看"
        )
        return

    # 自定义原文写进佩戴位 —— 签到文字里的「谁 · 头衔」读的就是它。
    await asyncio.to_thread(
        ctx.db.equip_title, env.group_id, env.sender_id, word
    )
    member = await asyncio.to_thread(ctx.db.get_member, env.group_id, env.sender_id) or {}
    applicant = (
        ctx.profiles.display_name(member, env.sender_id)
        if hasattr(ctx, "profiles") else env.sender_id
    )

    # @ 群主提醒一次，到此为止 —— 不建 pending、不催第二遍。
    await onebot.send_group(
        env.event,
        env.group_id,
        deco.head("头衔申请", "bag")
        + f"\n群员 {applicant}（QQ {env.sender_id}）申请把群头衔改成：{word}\n"
        "群头衔只有群主能改，麻烦到群设置里处理一下",
        at=owner_id,
    )
    left = await asyncio.to_thread(
        ctx.db.bag_count, env.group_id, env.sender_id, "title", TITLE_ITEM
    )
    lines = [f"✅ 已提醒群主给你改头衔「{word}」"]
    if left > 0:
        lines.append(f"背包还剩 {left} 次修改机会")
    lines.append("群头衔只有群主能改，等他处理就好")
    yield env.event.plain_result("\n".join(lines))


async def relation_cmd(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """`关系 名字 @某人`：发起结缔；`关系 解除`：散伙。"""
    targets = onebot.get_at_targets(env.event)
    word = env.args.strip()

    if word in {"解除", "散伙", "分开"}:
        partner = await asyncio.to_thread(
            ctx.db.clear_relation, env.group_id, env.sender_id
        )
        if not partner:
            yield env.event.plain_result("你现在没有关系可解除")
            return
        info = await onebot.get_member_info(env.event, env.group_id, partner)
        name = str((info or {}).get("card") or (info or {}).get("nickname") or partner)
        yield env.event.plain_result(
            f"💔 关系已解除（对方：{name}）\n重新结缔要再买一份资格"
        )
        return

    if not targets:
        yield env.event.plain_result(
            "要和谁结成关系？\n"
            "格式：关系 挚友 @某人\n"
            f"可结缔：{' / '.join(name for name, _, _ in RELATIONS)}\n"
            "解除：关系 解除"
        )
        return

    name = word.split(maxsplit=1)[0] if word else ""
    relation = next((r for r in RELATIONS if r[0] == name), None)
    if relation is None:
        yield env.event.plain_result(
            f"没有「{name or '？'}」这种关系\n"
            f"可结缔：{' / '.join(n for n, _, _ in RELATIONS)}"
        )
        return

    target_id = targets[0]
    if target_id == env.sender_id:
        yield env.event.plain_result("和自己结关系？这好像不需要花钱")
        return
    if target_id == ev.self_id(env.event):
        yield env.event.plain_result("不能和机器人结关系，它只会一直陪着你")
        return

    buyer = await asyncio.to_thread(ctx.db.get_member, env.group_id, env.sender_id) or {}
    target = await asyncio.to_thread(ctx.db.get_member, env.group_id, target_id) or {}
    if buyer.get("relation"):
        yield env.event.plain_result(
            f"你已经和别人结着「{buyer['relation']}」了\n先发「关系 解除」再结新的"
        )
        return
    if target.get("relation"):
        yield env.event.plain_result("对方已经有关系了，先让人家解除")
        return
    if (env.group_id, target_id) in _PENDING:
        yield env.event.plain_result("对方正在处理另一份结缔请求，等一下再试")
        return

    # 先扣资格再发问。退款只发生在「拒绝 / 超时」两条路上，
    # 中途宕机导致资格悬空的概率，远小于「先发问后扣」被人钻空子的概率。
    taken = await asyncio.to_thread(
        ctx.db.bag_take, env.group_id, env.sender_id, "relation", relation[0]
    )
    if not taken:
        yield env.event.plain_result(
            f"你背包里没有「{relation[0]}」的结缔资格\n发「购买 {relation[0]}」买一份"
        )
        return

    buyer_name = ctx.profiles.display_name(env.member, env.sender_id)
    request = RelationRequest(
        group_id=env.group_id,
        target_id=target_id,
        buyer_id=env.sender_id,
        buyer_name=buyer_name,
        relation=relation[0],
        deadline=time.time() + RELATION_TIMEOUT_SECONDS,
        event=env.event,
    )
    _PENDING[request.key] = request
    task = asyncio.create_task(_expire(ctx, request))
    request.task = task
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    # 发问直接走协议端（要带 @ 段），不走 yield —— yield 出去的纯文本
    # 里的 @ 只是个普通字符，点不动也算不上提醒。
    await onebot.send_group(
        env.event,
        env.group_id,
        deco.head("结缔请求", "relations")
        + f"\n{buyer_name} 想和你结成「{relation[0]}」！\n"
        f"{relation[2]}\n"
        f"回复「同意」或「不同意」，{RELATION_TIMEOUT_SECONDS // 60} 分钟内有效。",
        at=target_id,
    )


async def _expire(ctx: PluginContext, request: RelationRequest) -> None:
    """超时：资格退回买家背包，群里说一句。"""
    try:
        await asyncio.sleep(max(0, request.deadline - time.time()))
        if _PENDING.get(request.key) is not request:
            return
        _PENDING.pop(request.key, None)
        await asyncio.to_thread(
            ctx.db.bag_add, request.group_id, request.buyer_id, "relation", request.relation
        )
        await onebot.send_group(
            request.event,
            request.group_id,
            deco.mark("meh")
            + f" 结缔超时：「{request.relation}」已退回 {request.buyer_name} 的背包",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 定时任务里的异常没人接，必须自己收
        logger.error(f"[群助手] 结缔请求超时处理失败: {exc}", exc_info=True)


# ---------------------------------------------------------------- 答复拦截
def is_pending_relation(group_id: str, user_id: str) -> bool:
    """这个人手上是不是挂着一个没答复的结缔请求。"""
    return (str(group_id), str(user_id)) in _PENDING


async def relation_intercept(
    ctx: PluginContext, event: Any, group_id: str, user_id: str, text: str
) -> Verdict | None:
    """这条消息是不是对结缔请求的答复。

    返回 ``None`` = 与结缔无关，照常走命令分发。只精确匹配同意 / 拒绝词，
    其他消息（哪怕来自被请求的人）一律放行 —— 不把人扣在流程里。
    """
    key = (str(group_id), str(user_id))
    request = _PENDING.get(key)
    if request is None:
        return None

    answer = (text or "").strip()
    if answer not in _AGREE and answer not in _REJECT:
        return None

    _PENDING.pop(key, None)
    if request.task is not None:
        request.task.cancel()

    if answer in _REJECT:
        ctx.db.bag_add(request.group_id, request.buyer_id, "relation", request.relation)
        return Verdict(
            handled=True,
            reply=deco.mark("meh")
            + f" 结缔失败：「{request.relation}」已退回 {request.buyer_name} 的背包",
        )

    ctx.db.set_relation(request.group_id, request.buyer_id, str(user_id), request.relation)
    return Verdict(
        handled=True,
        reply=deco.mark("ok")
        + f" 结缔成功！你们现在是「{request.relation}」了 💐\n"
        "发「查」就能在信息卡上看到这段关系",
    )


async def shutdown(db: Any = None) -> None:
    """插件卸载时停掉结缔定时器。

    ``db`` 传了就把挂着的请求退款（资格是扣掉的额度买的，卸载不该吞钱）；
    没传（极端情况）只清内存 —— 丢资格可以补，挂一个永远不结算的定时器不行。
    """
    for request in list(_PENDING.values()):
        if request.task is not None:
            request.task.cancel()
        if db is not None:
            try:
                db.bag_add(request.group_id, request.buyer_id, "relation", request.relation)
            except Exception as exc:
                logger.error(f"[群助手] 卸载时退回结缔资格失败: {exc}")
    _TASKS.clear()
    _PENDING.clear()


# ---------------------------------------------------------------- 命令注册
COMMANDS = [
    Command(
        key="bag",
        # 同上：`背包 @某人` 得能被接住，而 exact 只认整句。
        matcher=exact_or_mention(("背包", "我的背包", "物品", "道具")),
        handler=bag_cmd,
        perm_short="背包",
        help=(
            HelpEntry("背包", "头衔 / 关系 / 抽奖次数 / 权限卡都在这里", page="base", order=50, group="背包"),
            HelpEntry(
                "背包 @某人", "看别人的背包（2 级起）",
                page="base", level=2, order=50, group="我的信息",
            ),
        ),
    ),
    Command(
        key="use_title",
        matcher=prefix(("头衔", "设置头衔", "戴头衔")),
        handler=use_title,
        perm_short="头衔",
        help=(
            HelpEntry("头衔 想要的字", "消耗一次修改机会，自定义群头衔", page="base", order=51, group="背包"),
        ),
    ),
    Command(
        key="relation",
        matcher=prefix(("关系",)),
        handler=relation_cmd,
        perm_short="关系",
        help=(
            HelpEntry("关系 挚友 @某人", "结缔一段关系，对方同意才生效", page="base", order=52, group="背包"),
            HelpEntry("关系 解除", "解除现在的关系", page="base", order=53, group="背包"),
        ),
    ),
    Command(
        key="use_mute_card",
        matcher=prefix(("禁言卡", "用禁言卡")),
        handler=use_mute_card,
        perm_short="禁言卡",
        help=(
            HelpEntry(
                "禁言卡 @某人",
                "消耗一张卡，让对方安静 10 秒（管理及以上不吃）",
                page="base",
                order=54,
                group="背包",
            ),
        ),
    ),
]
