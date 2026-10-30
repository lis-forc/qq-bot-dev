"""昵称业务：改名流程、发言守卫、入群提示。

这是整个插件里唯一「会主动改别人东西」的地方 —— 改的是群名片。
所以三件事写得很保守：

**1. 只在必要时发请求。**
守卫每条消息都跑，但它做的第一件事是**字符串比对**：拿「机器人认为
你的群名片该长什么样」和报文里带的真实名片比一次。一样就直接返回，
零 API 调用。只有被人手动改掉的那一条消息才会真的发一次
``set_group_card``。

**2. 名字合规就采纳，不合规才催。**
新成员进群时群名片是 QQ 昵称（多半带符号或超长），那就催他改；
如果他本来就叫「小明」，一进群就合规 —— 那就直接采纳，不打扰。
「用户自己已经做对了，就不该再要求他做一遍」是这里最重要的一条。

**3. 改不动就放手。**
机器人不是管理员、接口报错、QQ 那头拒了 —— 一律只记日志，绝不在群里
喊「我改不了」。群友不该为机器人的权限问题买单。

流程分工：
- 校验规则在 ``utils/nickname.py``（纯函数，可单测）；
- 「写库」在 ``core/services.py``（不碰 IO）；
- 「写群名片 + 催改名 + 静音」在这里。群头衔只有群主能改，跟这里无关（见 bag.py）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

try:  # 本地自测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

from ..core.context import PluginContext
from ..core.database import _today
from ..utils import nickname, onebot
from ..utils import event as ev
from ..utils.router import compact

# ---------------------------------------------------------------- 文案
DEFAULT_JOIN = "名字定一下：发「改名 你的名字」· {rule}"

DEFAULT_NAG = (
    "{name}，名字还没定 😅 发「改名 你的名字」\n"
    "{rule}\n"
    "第 {n}/{limit} 次提醒，超了静音 {mute} 分钟"
)

EXAMPLE = "阿橘"


def _fill(template: str, values: dict[str, Any]) -> str:
    """填占位符。模板里有单个花括号（用户想写具体文字）也不许抛。"""
    try:
        return template.format_map(values)
    except (KeyError, IndexError, ValueError):
        out = template
        for key, value in values.items():
            out = out.replace("{" + key + "}", str(value))
        return out


def _values(cfg, name: str = "", n: int = 0) -> dict[str, Any]:
    return {
        "name": name or "朋友",
        "min": cfg.nick_min_len,
        "max": cfg.nick_max_len,
        "rule": nickname.rule_text(cfg),
        "example": EXAMPLE,
        "n": max(1, n),
        "limit": cfg.nick_nag_limit,
        "mute": cfg.nick_nag_mute_minutes,
    }


def render_join_hint(cfg, name: str = "") -> str:
    """入群时追加在欢迎语后面的那段。"""
    template = cfg.nick_join_text or DEFAULT_JOIN
    return _fill(template, _values(cfg, name)).strip()


def render_nag(cfg, name: str = "", n: int = 1) -> str:
    """没改名的人发言时的提醒。"""
    template = cfg.nick_nag_text or DEFAULT_NAG
    return _fill(template, _values(cfg, name, n)).strip()


# ---------------------------------------------------------------- 机器人权限
# 「机器人是不是管理员」每条消息都问一次太贵（一次网络往返），
# 但它又必须跟着群设置实时变。折中：60 秒缓存。
# 这个数字的依据是「人刚把机器人升成管理员，一分钟内生效」已经够快，
# 而一分钟内最多省下几十次接口调用。
_ADMIN_CACHE: dict[str, tuple[bool, float]] = {}
ADMIN_TTL = 60.0


def forget_admin_cache() -> None:
    """自测和热重载用：把缓存清掉，下一次重新问协议端。"""
    _ADMIN_CACHE.clear()


async def can_manage(event, group_id: str) -> bool:
    key = str(group_id)
    now = time.monotonic()
    hit = _ADMIN_CACHE.get(key)
    if hit and now - hit[1] < ADMIN_TTL:
        return hit[0]
    ok = await onebot.is_bot_admin(event, group_id)
    _ADMIN_CACHE[key] = (ok, now)
    return ok


# ---------------------------------------------------------------- 改名
async def apply_name(
    ctx: PluginContext,
    event,
    group_id: str,
    user_id: str,
    name: str,
    *,
    operator_id: str = "",
) -> str:
    """一次完整的改名：校验 → 写库 → 改群名片 → 回话。

    不扣额度。改名在这个群里是「进来就要做的事」，不是消费；要收钱
    由管理员发「扣额度 @某人」，那是显式动作，会进审计日志。
    """
    cfg = ctx.config
    operator = str(operator_id or user_id)
    name = compact(name or "")
    if not name:
        return format_hint(cfg)

    member = await asyncio.to_thread(
        ctx.db.ensure_member, group_id, user_id, cfg.welcome_quota
    )
    freeman = bool(int(member.get("freeman") or 0))

    # 自由人只限长度（能存进库、排得下卡就行），不再管内容和符号。
    if cfg.nick_enabled and not freeman:
        ok, why = nickname.validate(name, cfg)
    else:
        ok, why = nickname.validate_loose(name)
    if not ok:
        lines = [f"⚠️ {why}"]
        if cfg.nick_enabled and not freeman:
            lines.append(f"规则：{nickname.rule_text(cfg)}")
            lines.append("买「自由人」之后就不限这些了，发「商店」看价格。")
        return "\n".join(lines)

    old = ctx.profiles.display_name(member, user_id)
    updated = await asyncio.to_thread(
        ctx.profiles.set_display_name, group_id, user_id, name
    )
    sex = ctx.profiles.sex(updated)
    # 库里没表过态时，顺手用报文里的性别补一次 —— 不然新人的名字前面会挂
    # 一个 🧸，直到有人翻了他的信息卡为止。
    #
    # 只在「给自己改」时补：代别人改名时，报文里的性别是**操作者**的，
    # 拿它去定别人的记号，会把一个男管理员的资料按到女成员头上。
    if (
        not sex
        and not str(updated.get("sex") or "")
        and str(user_id) == str(getattr(event, "get_sender_id", lambda: "")() or "")
    ):
        derived = nickname.normalize_sex(ev.sender_sex(event))
        if derived:
            sex = derived
            updated = await asyncio.to_thread(ctx.db.set_sex, group_id, user_id, derived)

    # 群名片：库里只存名字，记号是这里现拼的。
    # 后缀一并拼上——它是惩罚，不能让「改个名」就洗掉。
    suffix, _until = await asyncio.to_thread(ctx.db.nick_suffix, group_id, user_id)
    card = nickname.compose(name, sex, cfg, suffix) if cfg.nick_enabled else name
    card_ok = await onebot.set_group_card(event, group_id, user_id, card)

    try:
        await asyncio.to_thread(
            ctx.db.log_op, group_id, operator, "改名", user_id, f"{old or '—'} → {name}"
        )
    except Exception as exc:  # 审计写失败不该把改名也一起废掉
        logger.warning(f"[群助手] 记录改名失败: {exc}")

    lines = ["✅ 改名成功"]
    lines.append(f"{old} → {name}" if old and old != name else f"名字：{name}")
    if cfg.nick_enabled:
        lines.append(f"群名片：{card}")
    if not card_ok:
        # 群名片没改成不影响库里的名字，但用户会去群里看名片 —— 必须先说清。
        lines.append("⚠️ 群名片没改成（机器人可能不是管理员），信息卡上已经生效")
    return "\n".join(lines)


def format_hint(cfg) -> str:
    """没写名字时的用法。规则文案跟着配置走，不写死数字。"""
    return (
        "改名 你的名字\n"
        f"规则：{nickname.rule_text(cfg)}\n"
        f"比如：改名 {EXAMPLE}"
    )


# ---------------------------------------------------------------- 发言守卫
# 「今天已经提示过他把名片改回来了」——键是 (群, 人)，值是日期。
# 只放在内存里：重启之后最多多提示一次，不值得为它建表。
_NOTICED: dict[tuple[str, str], str] = {}


async def guard(
    ctx: PluginContext, event, group_id: str, user_id: str, member: dict[str, Any] | None
) -> str | None:
    """一条普通发言要不要被昵称系统管。返回要发出去的话，或 None。

    三种结局：
    - 名字合规 → 顺手把名片掰回该有的样子（不一致才动手）；
    - 名字不合规且没登记过 → 催改名，够次数就静音；
    - 自由人 / 群主 / 昵称系统关掉 / 机器人没权限 → 什么都不做。
    """
    cfg = ctx.config
    if not cfg.nick_enabled or not member:
        return None
    if int(member.get("freeman") or 0):
        return None
    # 群主不碰：QQ 里管理员改不了群主的群名片，与其每条消息都发一次
    # 注定失败的请求、在日志里刷一行警告，不如开头就绕开。角色是消息
    # 报文里白捡的字段（见 ``ev.sender_role``），这里零网络请求。
    if ev.sender_role(event) == "owner":
        return None

    card = ev.sender_card(event)
    name = str(member.get("display_name") or "") or nickname.strip(card, cfg)
    if not name:
        return None

    sex = ctx.profiles.sex(member)
    # 库里没表过态（空串）时才用报文里的性别补一次。存成 "-" 表示
    # 群友明确选了「不设」，那种情况不能覆盖。
    if not str(member.get("sex") or ""):
        derived = nickname.normalize_sex(ev.sender_sex(event))
        if derived:
            sex = derived
            await asyncio.to_thread(ctx.db.set_sex, group_id, user_id, derived)

    if not str(member.get("display_name") or ""):
        # 不合规就催，没有开关 —— 「不合规的名字必须改」是昵称系统的
        # 存在前提，不是它的一个可调选项；能调的只有催几次、静音多久。
        if not nickname.is_valid(name, cfg):
            return await _nag(ctx, event, group_id, user_id, name)
        # 合规就采纳：他自己已经把名字弄对了，不必再走一遍「改名」。
        await asyncio.to_thread(ctx.db.set_display_name, group_id, user_id, name)

    # 名片 = 记号 + 名字 + 临时后缀。后缀（大富翁的「我是猪」这类）
    # 到期后这里自然拼不出来，下一次发言就会把名片掰回没有后缀的样子。
    suffix, _until = await asyncio.to_thread(ctx.db.nick_suffix, group_id, user_id)
    expected = nickname.compose(name, sex, cfg, suffix)
    if not card or card == expected:
        return None
    if not await can_manage(event, group_id):
        return None

    if not await onebot.set_group_card(event, group_id, user_id, expected):
        logger.warning(
            f"[群助手] 群 {group_id} 成员 {user_id} 的群名片改不回去（检查机器人权限）"
        )
        return None

    logger.info(f"[群助手] 群名片已更新：{user_id} @ 群 {group_id} → {expected}")
    key = (str(group_id), str(user_id))
    today = _today()
    if _NOTICED.get(key) != today:
        # 一天最多说一次。不说的话他会以为自己的名片被谁改了；
        # 每改一次都说，就成了自己跟自己刷屏。
        _NOTICED[key] = today
        # 文案不预设「他之前改过名」：自动采纳的新人和被人手改掉名片的
        # 老人走的是同一条路，说「帮你改回来了」对前者是凭空一句。
        return f"（群名片已自动更新：{expected}）"
    return None


async def _nag(
    ctx: PluginContext, event, group_id: str, user_id: str, name: str
) -> str:
    """催改名。够次数就静音一会儿，并把计数清零重新开始。"""
    cfg = ctx.config
    limit = cfg.nick_nag_limit
    count = await asyncio.to_thread(ctx.db.bump_nick_nag, group_id, user_id)

    muted = False
    if count >= limit and await can_manage(event, group_id):
        seconds = cfg.nick_nag_mute_minutes * 60
        if await onebot.mute(event, group_id, user_id, seconds):
            muted = True
            logger.info(
                f"[群助手] 成员 {user_id} @ 群 {group_id} 提醒 {count} 次仍未改名，"
                f"已静音 {cfg.nick_nag_mute_minutes} 分钟"
            )
        else:
            logger.warning(f"[群助手] 静音 {user_id} 失败（检查机器人权限）")
        # 不管静音成没成，计数都清零：成了是重新开始，没成也避免下一条消息
        # 又去调一次接口 —— 失败了还每条都试，等于拿群消息打接口。
        await asyncio.to_thread(ctx.db.reset_nick_nag, group_id, user_id)

    text = render_nag(cfg, name, min(count, limit))
    if muted:
        text += f"\n（已经静音 {cfg.nick_nag_mute_minutes} 分钟，改完名字照常说话）"
    return text


def reset_notice_cache() -> None:
    """自测用：清掉「今天提示过」的标记。"""
    _NOTICED.clear()
