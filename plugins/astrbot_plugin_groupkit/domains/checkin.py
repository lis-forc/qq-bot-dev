"""每日签到：签到 / 记录 / 排行。

**签到不再画卡片。** 这一版是「一句话 + 一张图」：文字负责讲清楚今天拿到
什么、连了几天、运势如何；图是用户自己放在 `assets/images/checkin/` 里的
运势图，按种子随机挑一张。

为什么放弃画卡（前三版都是 Pillow 画的）：
- 代码画的卡，永远比不上在本地用设计工具做的那张 —— 这一点群友一眼就能看出来；
- 签到是每天都会刷一次的图，底图固定会腻，换成一组图轮着来才耐看；
- 「图 + 文字」在手机上是一条消息，图和字可以分别长按、分别保存，
  比一张整图更灵活。

画不出来也不兜底成别的：图没有就只发文字，文字是永远排得出来的。

头衔不在这里了 —— 它是背包里的消耗品，看 `domains/bag.py`。
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from astrbot.api.message_components import Image, Plain

from ..core import fortune as fortune_mod
from ..core.checkin import MILESTONES
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
from ..utils.text import table

# 全插件统一的分界线长度（见 utils/deco.py 的说明）。写在这里而不是每处
# 各写一个 26，是因为「三块文字的分界线看起来要一样长」是个视觉约定，
# 一旦有人只改了其中一处，群里立刻能看出来。
RULE = "=" * 26


def _checkin_head(data: dict) -> str:
    """图片**上方**那一行：谁、哪天。就一行，不加解释。

    签到是每天都要看一次的东西，标题写成三行等于每天多读两行废话。
    """
    who = str(data["username"])
    if data.get("user_title"):
        who += f" · {data['user_title']}"
    # 用「｜」而不是换行：名字和日期本来就是并列的两件事，
    # 拆两行只是让每天要看的那一屏多一行。
    return f"{deco.mark('checkin')} {who}｜{data['date_label']}"


def _checkin_body(data: dict, group_id: str, user_id: str) -> str:
    """图片**下方**那几行：运势一句 + 拿到多少 + 连签。

    运势只留 level 和 line，`brief()` 那行砍掉 —— 图和文字凑一条消息时，
    下面超过三行就要开始划屏了。
    """
    date_key = str(data.get("date_key") or "")[:10]
    today = fortune_mod.today(group_id, user_id, date_key)

    lines: list[str] = []
    if data.get("note"):
        lines.append(f"「{data['note']}」")
    lines.append(f"{deco.mark('fortune')} {today.level}｜{today.line()}")

    reward = f"{deco.mark('quota')} +{int(data.get('reward') or 0)} {data['currency']}"
    if data.get("doubled"):
        reward += "（双倍卡翻倍）"
    # 拿到多少 / 连签 / 累计 / 余额 全是并列的数字，压成一行。
    lines.append(
        f"{reward}｜连签 {int(data.get('streak') or 0)} 天"
        f"｜累计 {int(data.get('total_days') or 0)} 天"
        f"｜余额 {int(data.get('balance') or 0)}"
    )
    if data.get("milestone_text"):
        lines.append(str(data["milestone_text"]))
    return "\n".join(lines)


def _checkin_text(data: dict, group_id: str, user_id: str) -> str:
    """没有图时的合并版：标题 + 结果，一次发完。"""
    return f"{_checkin_head(data)}\n{_checkin_body(data, group_id, user_id)}"


async def check_in(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """签一次到，出一条「文字 + 运势图」。"""
    if not ctx.config.checkin_enabled:
        yield env.event.plain_result("签到功能已关闭")
        return

    nickname = ctx.profiles.display_name(env.member, env.sender_id)
    record = await asyncio.to_thread(ctx.checkin.check_in, env.group_id, env.sender_id)
    data = await asyncio.to_thread(
        ctx.checkin.card_payload,
        env.group_id,
        env.sender_id,
        nickname,
        record,
    )
    # 日期键从快照里取：重复签到时显示的是「那一天」，不是今天。
    data["date_key"] = str(record.get("date_key") or "")

    # 已经签过：一句就够。不出图、不重播运势 —— 他今天已经看过一次了，
    # 再贴一遍整段只是拿同一个信息占两屏。
    if data.get("already"):
        yield env.event.plain_result(
            f"{deco.mark('checkin')} 今天已经签过了 · 明天再来\n"
            f"{deco.mark('quota')} 余额 {int(data.get('balance') or 0)}"
        )
        return

    head = _checkin_head(data)
    body = _checkin_body(data, env.group_id, env.sender_id)

    # 运势图：种子和奖励同一套（日期+群+人），所以重发还是同一张。
    image_path = None
    if ctx.config.checkin_image:
        image_path = ctx.images.pick_checkin(
            f"{data['date_key']}|{env.group_id}|{env.sender_id}"
        )

    if image_path is None:
        yield env.event.plain_result(f"{head}\n{body}")
        return

    # 文字 → 图 → 文字：图夹在中间，上面只有一行「谁 · 哪天」，下面三行
    # 结果一眼扫完。整条是一条消息（不是三条），图仍然可以单独长按保存。
    yield env.event.chain_result(
        [
            Plain(head + "\n"),
            Image.fromFileSystem(str(image_path)),
            Plain("\n" + body),
        ]
    )


async def record(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """最近若干天的签到明细。@别人要 2 级（助手）。"""
    targets = ev.at_targets(env.event, env.sender_id)
    who = env.sender_id
    label = "我的签到"
    if targets:
        warn = await require_level(ctx, env, 2)
        if warn:
            yield env.event.plain_result(warn)
            return
        who = targets[0]
        other = await asyncio.to_thread(ctx.db.get_member, env.group_id, who)
        label = f"{ctx.profiles.display_name(other, who)} 的签到"

    rows = await asyncio.to_thread(ctx.checkin.recent, env.group_id, who, 10)
    state = await asyncio.to_thread(ctx.checkin.state, env.group_id, who)
    cur = ctx.config.currency_name

    if not rows:
        yield env.event.plain_result(
            deco.head("还没有签到记录", "checkin") + "\n"
            "发「签到」就开始了，第一次签到当天就能领"
            f"{ctx.config.checkin_base_min}-{ctx.config.checkin_base_max} {cur}"
        )
        return

    lines = [
        deco.head(label, "checkin"),
        RULE,
        f"连续 {state['streak_days']} 天 · 累计 {state['total_days']} 天"
        f" · 最长连签 {state['best_streak']} 天",
        "",
    ]
    body = []
    for index, item in enumerate(rows, 1):
        date_key = str(item.get("date_key") or "")
        reward = int(item.get("reward") or 0)
        # 表格单元格里**不放 emoji**：`table()` 按显示宽度补空格，
        # 而 emoji 的实际宽度取决于客户端字体，算错一格整列就歪。
        # 双倍用文字标出来，宽度可控。
        tail = f"+{reward}"
        if item.get("doubled"):
            tail += " ×2"
        if int(item.get("streak_after") or 0) == 1:
            tail += " · 连签起始"
        body.append((f"{index}.", date_key[5:], tail))
    lines.append(table(body))
    lines.append("")

    # 把「还差多少到下一个里程碑」算清楚，比只报总天数有用。
    for days in sorted(MILESTONES):
        if state["total_days"] < days:
            lines.append(f"再签 {days - state['total_days']} 天到 {days} 天里程碑（+{MILESTONES[days]}）")
            break
    else:
        lines.append("累计里程碑已全部达成")
    # 头尾各一根 =====：和签到 / 商店同一套分界约定，三段内容一眼分清。
    lines.insert(2, RULE)
    lines.append(RULE)
    yield env.event.plain_result("\n".join(lines))


async def ranking(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """群内签到排行。默认连签榜，`签到排行 累计` 看总量。"""
    word = env.args.strip()
    by = "total" if word in {"累计", "总榜", "累计榜", "总量"} else "streak"
    rows = await asyncio.to_thread(ctx.checkin.ranking, env.group_id, by, 10)

    if not rows:
        yield env.event.plain_result(
            deco.head("这个群还没有人签到", "stats") + "\n发「签到」当第一个"
        )
        return

    body = []
    for index, item in enumerate(rows, 1):
        name = (
            str(item.get("display_name") or "")
            or str(item.get("qq_name") or "")
            or str(item.get("user_id") or "")
        )
        if by == "streak":
            body.append((f"{index}.", name, f"{int(item.get('streak_days') or 0)} 天",
                         f"累计 {int(item.get('total_days') or 0)}"))
        else:
            body.append((f"{index}.", name, f"{int(item.get('total_days') or 0)} 天",
                         f"连签 {int(item.get('streak_days') or 0)}"))

    title = deco.head("连签排行" if by == "streak" else "累计签到排行", "stats")
    hint = "发「签到排行 累计」看总量榜" if by == "streak" else "发「签到排行」看连签榜"
    yield env.event.plain_result("\n".join([title, "", table(body), "", hint]))


RECORD_WORDS = ("签到记录", "我的签到", "签到历史", "签到明细")


def _record_matcher(ctx, text: str, lower: str) -> Match | None:
    """「签到记录」+ 可选的一个 @。

    只放行两种形态：纯口令，或者口令后面跟一个 @。其余（比如
    「签到记录怎么看」）一律不接 —— 那句话是真的在提问。
    """
    cleaned = lower.strip()
    for word in RECORD_WORDS:
        if cleaned == word:
            return Match()
        if cleaned.startswith(word):
            rest = cleaned[len(word):].strip()
            if rest.startswith("@") or rest.isdigit():
                return Match()
    return None


COMMANDS = [
    Command(
        key="checkin",
        matcher=exact(("签到", "打卡", "签个到", "每日签到")),
        handler=check_in,
        perm_short="签到",
        help=(
            HelpEntry("签到", "每日签到领额度，出运势图", page="base", order=5, group="签到"),
        ),
    ),
    Command(
        key="checkin_record",
        matcher=custom(_record_matcher),
        handler=record,
        perm_short="签到记录",
        help=(
            HelpEntry("签到记录", "最近 10 天明细", page="base", order=6, group="签到"),
            HelpEntry(
                "签到记录 @某人", "看别人的签到（2 级起）",
                page="base", level=2, order=6, group="我的信息",
            ),
        ),
    ),
    Command(
        key="checkin_ranking",
        matcher=prefix(("签到排行", "签到榜", "连签榜")),
        handler=ranking,
        perm_short="签到排行",
        help=(
            HelpEntry("签到排行", "群内连签榜（加「累计」看总量）", page="base", order=7, group="排行"),
        ),
    ),
]
