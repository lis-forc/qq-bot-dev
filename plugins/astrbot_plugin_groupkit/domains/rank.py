"""排行榜：额度榜（有钱）+ 发言榜（话多）。

两个榜的定位不一样，可见性也不一样 —— 这不是设计上的疏忽，是刻意的：

- **额度榜限 2 级。** 余额是一个人的家底，全群可查等于把「谁穷」挂在
  墙上。而且额度能靠管理发放，榜单一公开，立刻会有人来问「为什么他
  比我多」。所以只给助手以上的人看。
- **发言榜可以关，默认关。** 人少的群里，发言榜就是「今天只有三个人
  说话」的证据，除了制造压力没有别的作用。所以它有一个开关，关掉时
  口令**完全失效** —— 匹配器直接不接这句话，群友发「发言榜」会被当成
  普通聊天。不给「这个功能没开」的解释，是因为解释本身就在提示
  「这里本来有个榜」，等于变相公布了。

前三名用奖牌 emoji。它铺在行首、不参与对齐计算：奖牌实际宽度由客户端
字体决定，进表格会把后面几列推歪（这条规矩见 `utils/text.py`）。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, Match, custom, prefix
from ..core.context import PluginContext
from ..utils import deco
from ..utils.text import pad, width

RULE = "=" * 26
TOP = 10

# 发言榜的触发词。放在模块级是因为匹配器要在开关关掉时把它们「藏起来」，
# 两个地方必须用同一份清单。
MESSAGE_WORDS = ("发言排行", "发言榜", "水榜", "话痨榜", "活跃榜")


def _name(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("qq_name") or row.get("user_id") or "")


def _table(rows: list[dict[str, Any]], values: list[str], tails: list[str]) -> list[str]:
    """名次 + 名字 + 主数值 + 尾巴。名字和数值列各自对齐，尾巴自然排。"""
    names = [_name(row) for row in rows]
    name_w = max(width(name) for name in names) if names else 0
    value_w = max(width(value) for value in values) if values else 0
    lines: list[str] = []
    for index, (name, value, tail) in enumerate(zip(names, values, tails), 1):
        line = f"{deco.medal(index)} {pad(name, name_w)}  {pad(value, value_w)}{tail}"
        lines.append(line.rstrip())
    return lines


# ---------------------------------------------------------------- 额度榜
async def quota_rank(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    rows = await asyncio.to_thread(ctx.db.quota_ranking, env.group_id, TOP)
    cur = ctx.config.currency_name

    if not rows:
        yield env.event.plain_result(
            deco.head("还没人攒下额度", "quota")
            + "\n"
            + "发「签到」每天领一点，发「抽奖」还能再翻一翻"
        )
        return

    values = [f"{int(row.get('quota') or 0)} {cur}" for row in rows]
    # 尾巴写称号：它花钱买，榜上有个称号等于顺带替商店做了一次广告。
    tails = [
        f" · {title}"
        if (title := ctx.checkin.title_name(str(row.get("title_id") or "")))
        else ""
        for row in rows
    ]

    lines = [deco.head("额度排行", "quota"), RULE]
    lines.extend(_table(rows, values, tails))

    me = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
    lines.extend(["", RULE, f"我的余额 {me} {cur}"])
    yield env.event.plain_result("\n".join(lines))


# ---------------------------------------------------------------- 发言榜
def message_matcher(ctx: PluginContext, text: str, lower: str) -> Match | None:
    """开关关掉时返回 None —— 口令就像从来没存在过。"""
    if not ctx.config.rank_message_enabled:
        return None
    return Match() if lower in {word.lower() for word in MESSAGE_WORDS} else None


async def message_rank(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    if not ctx.config.rank_message_enabled:  # 兜住「命中之后开关被关掉」
        return
    rows = await asyncio.to_thread(ctx.db.message_ranking, env.group_id, TOP)
    if not rows:
        yield env.event.plain_result(
            deco.head("还没有人发言", "stats") + "\n说句话就能上榜"
        )
        return

    values = [f"{int(row.get('total_messages') or 0)} 条" for row in rows]
    tails = []
    for row in rows:
        tail = ""
        title = ctx.checkin.title_name(str(row.get("title_id") or ""))
        if title:
            tail = f" · {title}"
        tails.append(tail)

    lines = [deco.head("发言排行", "stats"), RULE]
    lines.extend(_table(rows, values, tails))

    counts = await asyncio.to_thread(ctx.db.counts, env.group_id, env.sender_id)
    lines.extend(
        [
            "",
            RULE,
            f"我的发言 {counts['total']} 条 · 今日 {counts['today']} 条",
        ]
    )
    yield env.event.plain_result("\n".join(lines))


COMMANDS = [
    Command(
        key="quota_rank",
        matcher=prefix(("额度排行", "额度榜", "富豪榜", "财富榜")),
        handler=quota_rank,
        level=2,
        perm_short="额度排行",
        help=(
            HelpEntry(
                "额度排行", "本群余额前十（2 级以上）", page="base", level=2, order=43, group="排行"
            ),
        ),
    ),
    Command(
        key="message_rank",
        matcher=custom(message_matcher),
        handler=message_rank,
        perm_short="发言排行",
        # 开关关掉时不仅匹配器不接，菜单里也整条消失 —— 一个用不了的口令
        # 挂在菜单上，比没有这个功能更让人困惑。
        enabled=lambda ctx: ctx.config.rank_message_enabled,
        help=(
            HelpEntry("发言排行", "本群发言前十（可关）", page="base", order=44, group="排行"),
        ),
    ),
]
