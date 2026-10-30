"""商店：看货 / 下单。

额度需要一个花掉的去处，否则签到领到的只是一串越来越大的数字。
货架上的东西按「不会通胀」排：

- **服务**：一次性买断的身份，目前只有「自由人」—— 买了之后改名不受
  字数与符号限制，机器人也不再管他的群名片。它不产出任何数值，所以
  永远不会破坏平衡，是货架上最安全也最有吸引力的一件；
- **头衔**：消耗品，买一个进一个背包，用「头衔 名字」消耗掉；
- **关系**：结缔资格，买进背包后用「关系 名字 @某人」发起，
  对方同意才真正挂上；
- **名片皮肤**：纯装饰，卖多少都不会破坏平衡，而且它和头衔不一样 ——
  头衔是一行字，名片是整张图，视觉回报更大；
- **双倍卡**：本质是「明天的额度今天先拿」，所以必须限购，否则等于印钱；
- **抽奖券**：价格按抽奖期望值算，买它不赚不亏，赚的是那一下暴击。

**这里不出图了。** 原来是一张 Pillow 画的商店卡，问题是货架一变长，
卡就跟着变长，长到手机上要滑三屏，还不如直接读文字。
文字版靠三样东西保持好看：表情当分区记号、`=` 当分界、两列对齐的品名与价格。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from astrbot.api.message_components import Plain

from ..core.checkin import ShopItem
from ..core.command import Command, CommandEnv, HelpEntry, exact, prefix
from ..core.context import PluginContext
from ..utils import deco
from ..utils.forward import build_forward
from ..utils.text import pad, width

# 每一块的标题与包含的商品类型。顺序即展示顺序。
BLOCKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("service", "服务", ("service",)),
    ("level", "权限卡", ("permit",)),
    ("titles", "头衔", ("title",)),
    ("relations", "关系", ("relation",)),
    ("card", "名片皮肤", ("skin",)),
    ("draw", "消耗品", ("double", "ticket")),
)

RULE = "=" * 26


def _rows(
    ctx: PluginContext, items: list[ShopItem], group_id: str, user_id: str
) -> list[str]:
    """一块商品的行：品名 · 价格 · 状态（已拥有 / 正佩戴）。

    **不写介绍。** 介绍只有第一次看有用，之后每次进商店都要重读一遍，
    而货架真正要回答的只有三件事：有什么、多少钱、我有没有。
    所以 `item.desc` 不在这里出现 —— 想知道是什么，买了看背包。

    只对齐前两列：状态列长短差得远，硬对齐会在中间留一大片空白。
    最后一行 `rstrip`，免得手机端出现莫名换行。
    """
    if not items:
        return []
    name_w = max(width(item.name) for item in items)
    price_w = max(width(f"{item.price} {ctx.config.currency_name}") for item in items)

    lines: list[str] = []
    for item in items:
        price = f"{item.price} {ctx.config.currency_name}" if item.price > 0 else "免费"
        note = ctx.checkin.note_for(item, group_id, user_id)
        lines.append(
            f"{pad(item.name, name_w)}  {pad(price, price_w)}  {note}".rstrip()
        )
    return lines


async def shop(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """货架。整张打包成**一个**合并转发节点。

    曾经每个分区一个节点，点开是一串气泡；实际读起来像被人一条条发过来，
    而且货架越长气泡越多。现在只有一段文字：分区之间用 emoji 起头，
    眼睛从记号往下扫就行。构造转发失败（框架版本不兼容）退回普通消息 ——
    宁可刷屏也不能不开张。
    """
    items = await asyncio.to_thread(
        ctx.checkin.shop_items, env.group_id, env.sender_id
    )
    balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
    state = await asyncio.to_thread(ctx.checkin.state, env.group_id, env.sender_id)
    cur = ctx.config.currency_name

    lines = [
        f"{deco.mark('quota')} 余额 {balance} {cur} · 累计签到 {state['total_days']} 天",
        RULE,
    ]
    for mark_key, title, kinds in BLOCKS:
        rows = _rows(
            ctx,
            [item for item in items if item.kind in kinds],
            env.group_id,
            env.sender_id,
        )
        if not rows:
            continue
        lines.append("")
        lines.append(deco.head(title, mark_key))
        lines.extend(rows)
    lines.append("")
    lines.append(RULE)
    lines.append("📌 发「购买 名字」下单 · 买到的头衔和关系都在「🎒 背包」")

    title = deco.head("商店", "shop")
    body = "\n".join(lines)
    chain = build_forward(env.event, [(title, [Plain(body)])])
    if chain:
        yield env.event.chain_result(chain)
        return
    yield env.event.plain_result(f"{title}\n{body}")


async def buy(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """下单。扣费、发放、限购全部在数据库事务里完成。"""
    word = env.args.strip()
    if not word:
        yield env.event.plain_result(
            "买什么？\n"
            "购买 自由人\n"
            "购买 双倍卡\n"
            "购买 抽奖券\n"
            "购买 夜行者（头衔，进背包）\n"
            "购买 挚友（关系，进背包）\n"
            "购买 樱花\n"
            "发「商店」看看有什么"
        )
        return
    ok, message = await asyncio.to_thread(
        ctx.checkin.buy, env.group_id, env.sender_id, word
    )
    yield env.event.plain_result(message)


COMMANDS = [
    Command(
        key="shop",
        matcher=exact(("商店", "签到商店", "商城", "兑换", "积分商店")),
        handler=shop,
        perm_short="商店",
        help=(
            HelpEntry("商店", "看看能买什么", page="base", order=60, group="商店"),
        ),
    ),
    Command(
        key="buy",
        matcher=prefix(("购买", "买", "兑换")),
        handler=buy,
        perm_short="购买",
        help=(
            HelpEntry("购买 自由人", "改名不再受限，机器人也不管你的名片", page="base", order=61, group="商店"),
            HelpEntry("购买 名字", "买头衔 / 关系 / 名片皮肤，进背包", page="base", order=62, group="商店"),
        ),
    ),
]
