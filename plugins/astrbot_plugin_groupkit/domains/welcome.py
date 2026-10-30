"""入群欢迎与入群验证。

两件事放在同一个文件，因为它们挂在同一个事件上（``group_increase``），
而且要共享同一份「这个人刚进群」的上下文 —— 拆成两个文件只会让两边
各解析一遍通知报文。

四个设计决定
-----------

**1. 欢迎和验证是两个独立开关。**
合并成一个「入群流程」开关的话，想关验证就得连欢迎一起关。默认值也不
一样：欢迎默认开（静悄悄进群的群体验很差），验证默认关（它会禁言真人，
属于「想清楚了才开」）。

**2. 验证全程在群里进行，不禁言。**
曾经做过「进群先禁言、题目走私聊」的版本，实践下来是多此一举：
私聊依赖 QQ 临时会话，对方关了就送不到；禁言又把群内答题这条路堵死。
现在定死为「题目发群里、他在群里答」—— 插件能拦住口令分发，群友看到
的骚扰有限，而流程不会卡死任何人。

**3. 超时的处理是踢出，不是继续关着。**
留在群里不出声的僵尸号比广告号更让人烦，管理员也不知道该不该处理。
踢出用**普通移出**（``reject_add_request=False``）：超时多半是人在忙，
把「不能再申请入群」也一起按上去就过了。

**4. 机器人没群管权限时，只发提示、不登记验证。**
没有权限时登记了 pending 也没有任何手段兜住（踢不掉人），
反而会把新人的消息一直吃掉。宁可退化成一个纯提示，也不要留一个
「说话没人理」的黑洞。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

try:  # 本地自测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

from ..core.config import PluginConfig
from ..core.context import PluginContext
from ..utils import deco, nickname, onebot
from . import nickname as nick_flow

VERIFY_DEFAULT_QUESTION = "先发一句话吧，确认你是真人 —— 随便说点什么就行"

_WELCOME_DEFAULT = (
    "{at} 进来坐 🎉 新人礼 {quota} {currency}，说句话就到账\n"
    "发「帮助」看能玩什么"
)


@dataclass
class Pending:
    """一个正在答题的新人。"""

    group_id: str
    user_id: str
    deadline: float
    event: Any
    name: str = ""
    attempts: int = 0
    task: Any = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.group_id, self.user_id)

    def seconds_left(self) -> int:
        return max(0, int(self.deadline - time.time()))


@dataclass
class Verdict:
    """``intercept`` 的返回值。

    ``handled=False`` —— 这条消息和验证无关，照常走命令分发。
    ``handled=True, reply=""`` —— 消息被验证流程吃掉了，不回复（比如答错）。
    ``handled=True, reply="..."`` —— 吃掉了，并且要回这一句。
    """

    handled: bool = False
    reply: str = ""


# 进程内状态。不落库：验证是有寿命的东西（最长 60 分钟），
# 重启后重新答一次就是了，比留一条恢复不了的脏记录干净。
_PENDING: dict[tuple[str, str], Pending] = {}
_TASKS: set[asyncio.Task] = set()


# ---------------------------------------------------------------- 渲染文案
def render_welcome(cfg: PluginConfig, name: str, group_id: str) -> tuple[bool, str]:
    """把欢迎语模板填好。返回 ``(要不要 @ 新人, 正文)``。

    ``{at}`` 单独拿出来返回值，因为真正的 @ 是消息段，不是文字 ——
    在文本里写个 ``@昵称`` 只是个蓝色普通字，点不动，也不是提醒。
    """
    template = cfg.welcome_text or _WELCOME_DEFAULT
    want_at = "{at}" in template
    text = template.replace("{at}", "")
    try:
        text = text.format(
            name=name or "新朋友",
            group=group_id,
            quota=cfg.welcome_quota,
            currency=cfg.currency_name,
        )
    except (KeyError, IndexError, ValueError):
        # 模板里有单个花括号（用户想写 {具体昵称} 这类占位）会让 format 抛。
        # 这种情况**不能整条丢掉** —— 退化成「只把认识的占位符换掉」，
        # 用户写什么就发什么。
        for key, value in (
            ("{name}", name or "新朋友"),
            ("{group}", str(group_id)),
            ("{quota}", str(cfg.welcome_quota)),
            ("{currency}", cfg.currency_name),
        ):
            text = text.replace(key, value)
    return want_at, text.strip()


def verify_prompt(cfg: PluginConfig, name: str) -> str:
    """验证提示。要说清三件事：叫你做什么、多久、超时会怎样。"""
    minutes = cfg.verify_timeout_minutes
    question = cfg.verify_question or VERIFY_DEFAULT_QUESTION
    lines = [deco.head("进群验证", "verify"), f"{name or '新朋友'}，{question}"]
    if cfg.verify_answers:
        lines.append(f"答案是：{'/'.join(cfg.verify_answers)}")
    lines.append(f"{minutes} 分钟内没通过，会被请出群。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 入群处理
async def on_join(
    ctx: PluginContext, event: Any, group_id: str, user_id: str
) -> None:
    """新人进群：该欢迎就欢迎，该验证就验证，该提醒改名就提醒。失败只记日志。

    三件事拼成**一条**消息发出去。分三条发的话，新人进群的那一刻会被
    「欢迎语」「改名提示」「验证提示」连珠炮轰三下 —— 那不像有人在接待他，
    像三个自动回复在抢话。
    """
    cfg = ctx.config
    if not cfg.welcome_enabled and not cfg.verify_enabled and not cfg.nick_enabled:
        return

    name = await _nickname(event, group_id, user_id, cfg)

    parts: list[str] = []
    want_at = False
    if cfg.welcome_enabled:
        want_at, text = render_welcome(cfg, name, group_id)
        if text:
            parts.append(text)
    # 改名提示独立于欢迎语开关：群主可能想关掉欢迎语，但「进来先改名」
    # 是这个群的规矩，不该跟着一起消失。
    if cfg.nick_enabled:
        hint = nick_flow.render_join_hint(cfg, name)
        if hint:
            parts.append(hint)
            want_at = True

    if parts:
        await onebot.send_group(
            event, group_id, "\n\n".join(parts), at=user_id if want_at else None
        )

    if cfg.verify_enabled:
        await _start_verify(ctx, event, group_id, user_id, name)


async def _start_verify(
    ctx: PluginContext, event: Any, group_id: str, user_id: str, name: str
) -> None:
    cfg = ctx.config
    key = (str(group_id), str(user_id))
    if key in _PENDING:
        return

    # 先问权限再动手：没权限时禁言和踢人都会失败，登记 pending 只会
    # 让这个人的消息一直被吃掉。
    if not await onebot.is_bot_admin(event, group_id):
        logger.warning(
            f"[群助手] 群 {group_id} 开了入群验证，但机器人不是管理员，"
            f"本次只发提示、不做验证"
        )
        await onebot.send_group(
            event, group_id, deco.head("机器人还不是管理员，验证没法生效", "admin")
        )
        return

    seconds = cfg.verify_timeout_minutes * 60

    pending = Pending(
        group_id=str(group_id),
        user_id=str(user_id),
        deadline=time.time() + seconds,
        event=event,
        name=name,
    )
    _PENDING[key] = pending

    # 题目直接发群里。曾经做过「禁言 + 私聊发题」的版本，实践下来
    # 多此一举：私聊依赖临时会话（对方关了就送不到），禁言又堵死群内
    # 答题。现在一条路走到底 —— 群里发题、群里作答，流程永远通。
    await onebot.send_group(event, group_id, verify_prompt(cfg, name), at=user_id)

    task = asyncio.create_task(_expire(ctx, pending))
    pending.task = task
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _expire(ctx: PluginContext, pending: Pending) -> None:
    """到点还没通过 → 移出群。"""
    try:
        await asyncio.sleep(max(0, pending.deadline - time.time()))
        # 已经通过的会被 intercept 从 _PENDING 里摘掉，这时直接收手。
        if _PENDING.get(pending.key) is not pending:
            return
        _PENDING.pop(pending.key, None)

        kicked = await onebot.kick(pending.event, pending.group_id, pending.user_id)
        if kicked:
            logger.info(
                f"[群助手] 入群验证超时，移出 {pending.user_id} @ 群 {pending.group_id}"
            )
            await onebot.send_group(
                pending.event,
                pending.group_id,
                deco.mark("admin") + f" {pending.name or pending.user_id} "
                f"{ctx.config.verify_timeout_minutes} 分钟内没通过验证，已经移出。",
            )
        else:
            logger.warning(
                f"[群助手] 入群验证超时，但移出 {pending.user_id} 失败（检查群管权限）"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 定时任务里的异常没人接，必须自己收
        logger.error(f"[群助手] 入群验证超时处理失败: {exc}", exc_info=True)


# ---------------------------------------------------------------- 消息拦截
async def intercept(
    ctx: PluginContext, event: Any, group_id: str, user_id: str, text: str
) -> Verdict:
    """这条消息是不是在回答验证。是吃掉它，别让它进命令分发。"""
    key = (str(group_id), str(user_id))
    pending = _PENDING.get(key)
    if pending is None:
        return Verdict()

    cfg = ctx.config
    answer = (text or "").strip()
    accepted = cfg.verify_answers

    if accepted and answer.lower() not in accepted:
        pending.attempts += 1
        # 第一次答错就提醒一次，之后每三次提醒一次：
        # 每错一次都回一句会变成刷屏，一句都不回又让人以为没人管。
        if pending.attempts == 1 or pending.attempts % 3 == 0:
            return Verdict(
                handled=True,
                reply=deco.mark("meh")
                + f" 答案不对。{'/'.join(accepted)} 里的任意一个都算通过。",
            )
        return Verdict(handled=True)

    _PENDING.pop(key, None)
    if pending.task is not None:
        pending.task.cancel()
    return Verdict(handled=True, reply=deco.head("验证通过，欢迎进来", "ok"))


async def _nickname(event: Any, group_id: str, user_id: str, cfg: PluginConfig) -> str:
    """称呼用名字：群名片（剥掉性别记号）→ QQ 昵称 → 「新朋友」。

    剥记号是必须的：不然一个已经改好名的人，进群欢迎语里会写着
    「欢迎 🦊 张三」，像是把内部编码漏到了文案里。
    """
    info = await onebot.get_member_info(event, group_id, user_id)
    if isinstance(info, dict):
        card = str(info.get("card") or "").strip()
        if card:
            stripped = nickname.strip(card, cfg)
            if stripped:
                return stripped
        value = str(info.get("nickname") or "").strip()
        if value:
            return value
    return "新朋友"


# ---------------------------------------------------------------- 状态与收尾
def pending_count() -> int:
    return len(_PENDING)


def is_pending(group_id: str, user_id: str) -> bool:
    return (str(group_id), str(user_id)) in _PENDING


async def shutdown() -> None:
    """插件卸载时把定时器停掉。

    不停的话，热重载后旧任务还会拿旧 event 去踢人 —— 而且新实例看不到
    这批 pending，等于放着一堆「没人认领的定时炸弹」。
    """
    if _PENDING:
        logger.warning(
            f"[群助手] 卸载时还有 {len(_PENDING)} 人处于验证中，"
            f"重载后重新进群流程即可"
        )
    for task in list(_TASKS):
        task.cancel()
    _TASKS.clear()
    _PENDING.clear()
