"""排行榜：日发言榜 + 额度榜。

**为什么发言榜是「今天」而不是「累计」。** 累计榜上永远是那几个人，
新来的再怎么聊也挤不上去 —— 那榜单就没人看了。按天排才有悬念：今天
你多说两句就能上榜，明天重新开始比。所以这张榜读的是 ``daily_stats``，
今天一句话没说的人**不会出现在榜上**。

**为什么要分页。** 前十名之外的人也想看自己在第几，而一次列 50 个人
在手机上要滑好几屏。所以「发言榜」给 1~10，「发言榜2」给 11~20，
最多到面板里配的那个页数（默认 5 页、前 50 名）。

可见性：
- **额度榜限 2 级。** 余额是一个人的家底，全群可查等于把「谁穷」挂在
  墙上；而且额度能靠管理发放，榜单一公开立刻有人来问「为什么他比我多」。
- **日榜默认所有人可看**，面板上有开关（``rank_member_enabled``）。
  关掉之后普通群员发这个口令会收到权限提示，而不是看到一个空榜。
- **发言榜整体可以关**，关着时口令**完全失效** —— 匹配器直接不接，
  当成普通聊天。不给「这个功能没开」的解释，因为解释本身就在提示
  「这里本来有个榜」。

前三名用奖牌 emoji。它铺在行首、不参与对齐计算：奖牌实际宽度由客户端
字体决定，进表格会把后面几列推歪（这条规矩见 `utils/text.py`）。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, Match, custom, require_level
from ..core.context import PluginContext
from ..core.database import _today
from ..utils import deco
from ..utils.text import pad, width

RULE = "=" * 26
PAGE_SIZE = 10

# 发言榜的触发词。放在模块级是因为匹配器要在开关关掉时把它们「藏起来」，
# 两个地方必须用同一份清单。
MESSAGE_WORDS = ("发言榜", "日榜", "水榜", "活跃榜", "说话榜")
QUOTA_WORDS = ("额度排行", "额度榜", "富豪榜", "财富榜")


def _name(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("qq_name") or row.get("user_id") or "")


def _table(
    rows: list[dict[str, Any]], values: list[str], tails: list[str], start: int = 1
) -> list[str]:
    """名次 + 名字 + 主数值 + 尾巴。

    ``start`` 是这一页第一个人的真实名次：翻到第二页时第一行应该是
    「11.」而不是「1.」，否则群友会以为自己突然变第一了。
    """
    names = [_name(row) for row in rows]
    name_w = max(width(name) for name in names) if names else 0
    value_w = max(width(value) for value in values) if values else 0
    lines: list[str] = []
    for index, (name, value, tail) in enumerate(zip(names, values, tails), start):
        line = f"{deco.medal(index)} {pad(name, name_w)}  {pad(value, value_w)}{tail}"
        lines.append(line.rstrip())
    return lines


def _page_from(lower: str, words: tuple[str, ...]) -> str | None:
    """从「发言榜2」「发言榜 第2页」里抠出页码。

    返回 None 表示这句不是这个口令；返回 "1" 表示是口令但没写页码。
    两件事必须分开 —— 前者要让消息继续往下走（可能命中别的命令），
    后者要正常出第一页。
    """
    cleaned = lower.strip()
    for word in words:
        if not cleaned.startswith(word):
            continue
        rest = cleaned[len(word):].strip(" 第页")
        return rest or "1"
    return None


def _page(value: str | None, cfg) -> tuple[int, bool]:
    """返回 (页码, 是否被上限夹过)。

    超出上限就夹到最后一页，并在群里说明一句 —— 直接回「没有第 99 页」
    等于把一次手滑变成一条错误信息。
    """
    try:
        page = int(str(value or "1").strip() or "1")
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    limit = max(1, int(cfg.rank_max_page))
    return (limit, True) if page > limit else (page, False)


def _page_note(page: int, pages: int, word: str) -> str:
    """页码提示。只有真的还有下一页才提示 —— 单页榜说「第 1/1 页」是噪音。"""
    if pages <= 1:
        return ""
    if page < pages:
        return f"第 {page}/{pages} 页 · 发「{word}{page + 1}」看下一页"
    return f"第 {page}/{pages} 页 · 到头了"


# ---------------------------------------------------------------- 日发言榜
def message_matcher(ctx: PluginContext, text: str, lower: str) -> Match | None:
    """开关关掉时返回 None —— 口令就像从来没存在过。"""
    if not ctx.config.rank_message_enabled:
        return None
    page = _page_from(lower, MESSAGE_WORDS)
    return None if page is None else Match(args=page)


async def message_rank(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """发言榜 [页码]：今天的发言排行。"""
    if not ctx.config.rank_message_enabled:  # 兜住「命中之后开关被关掉」
        return

    # 普通成员能不能看，面板上一个开关说了算（默认能看）。
    if not ctx.config.rank_member_enabled:
        warn = await require_level(ctx, env, 2)
        if warn:  # 只对不够等级的人发提示，2 级以上照常看
            yield env.event.plain_result(warn)
            return

    page, overflow = _page(env.args, ctx.config)
    date_key = _today()
    offset = (page - 1) * PAGE_SIZE

    rows = await asyncio.to_thread(
        ctx.db.daily_ranking, env.group_id, date_key, PAGE_SIZE, offset
    )
    people = await asyncio.to_thread(ctx.db.daily_rank_total, env.group_id, date_key)
    pages = max(1, (people + PAGE_SIZE - 1) // PAGE_SIZE)

    if not rows:
        yield env.event.plain_result(
            deco.head("今天还没人上榜", "stats")
            + "\n"
            + ("这一页没有人了，发「发言榜」看第一页" if offset else "说句话就能上榜")
        )
        return

    values = [f"{int(row.get('total_messages') or 0)} 条" for row in rows]
    tails = []
    for row in rows:
        title = ctx.checkin.title_name(str(row.get("title_id") or ""))
        tails.append(f" · {title}" if title else "")

    lines = [deco.head(f"今日发言榜 · {date_key[5:]}", "stats"), RULE]
    lines.extend(_table(rows, values, tails, start=offset + 1))

    mine = await asyncio.to_thread(ctx.db.counts, env.group_id, env.sender_id)
    lines.extend(["", RULE, f"{deco.mark('stats')} 我的今日 {mine['today']} 条"])

    if overflow:
        lines.append(f"⚠️ 最多只看得到第 {ctx.config.rank_max_page} 页")
    note = _page_note(page, pages, MESSAGE_WORDS[0])
    if note:
        lines.append(note)
    yield env.event.plain_result("\n".join(lines))


# ---------------------------------------------------------------- 额度榜
def quota_matcher(ctx: PluginContext, text: str, lower: str) -> Match | None:
    page = _page_from(lower, QUOTA_WORDS)
    return None if page is None else Match(args=page)


async def quota_rank(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    page, overflow = _page(env.args, ctx.config)
    offset = (page - 1) * PAGE_SIZE

    rows = await asyncio.to_thread(
        ctx.db.quota_ranking, env.group_id, PAGE_SIZE, offset
    )
    people = await asyncio.to_thread(ctx.db.quota_rank_total, env.group_id)
    pages = max(1, (people + PAGE_SIZE - 1) // PAGE_SIZE)
    cur = ctx.config.currency_name

    if not rows:
        yield env.event.plain_result(
            deco.head("还没人攒下额度", "quota")
            + "\n"
            + ("这一页没有人了，发「额度榜」看第一页" if offset else "发「签到」每天领一点")
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
    lines.extend(_table(rows, values, tails, start=offset + 1))

    me = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
    lines.extend(["", RULE, f"{deco.mark('quota')} 我的余额 {me} {cur}"])

    if overflow:
        lines.append(f"⚠️ 最多只看得到第 {ctx.config.rank_max_page} 页")
    note = _page_note(page, pages, QUOTA_WORDS[0])
    if note:
        lines.append(note)
    yield env.event.plain_result("\n".join(lines))


COMMANDS = [
    Command(
        key="quota_rank",
        matcher=custom(quota_matcher),
        handler=quota_rank,
        level=2,
        perm_short="额度排行",
        help=(
            HelpEntry(
                "额度排行 [页码]",
                "本群余额榜，加数字翻页（2 级以上）",
                page="base",
                level=2,
                order=43,
                group="排行",
            ),
        ),
    ),
    Command(
        key="message_rank",
        matcher=custom(message_matcher),
        handler=message_rank,
        perm_short="发言榜",
        # 开关关掉时不仅匹配器不接，菜单里也整条消失 —— 一个用不了的口令
        # 挂在菜单上，比没有这个功能更让人困惑。
        enabled=lambda ctx: ctx.config.rank_message_enabled,
        help=(
            HelpEntry(
                "发言榜 [页码]",
                "今天的发言排行，加数字翻页",
                page="base",
                order=44,
                group="排行",
            ),
        ),
    ),
]
