"""入群等通知事件。

职责：新人进群时立刻把真实入群时间落库 —— 这样「在册 N 天」不用等他
自己说话、也不用等下一次查协议端接口。

然后把「这个人是刚进来的」交给 ``domains/welcome.py``：欢迎语和入群验证
都在那边。这里保持薄，是因为通知事件还有别的类型（退群、撤回、禁言
通知……）要加，入口只该负责分流，不该长出业务。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from ..core.context import PluginContext
from ..utils import event as ev
from ..utils import onebot
from . import welcome


async def handle_notice(ctx: PluginContext, event, group_id: str) -> None:
    raw = ev.raw_message(event)
    if ev.raw_get(raw, "post_type") != "notice":
        return
    if ev.raw_get(raw, "notice_type") != "group_increase":
        return
    user_id = ev.raw_get(raw, "user_id")
    if not user_id:
        return
    try:
        join_ts = int(ev.raw_get(raw, "time") or 0) or int(time.time())
    except (TypeError, ValueError):
        join_ts = int(time.time())

    # 黑名单成员一进群就踢，而且**不建成员记录、不发新人礼**。
    # 顺序很重要：先挂账再踢的话，他每次重进都能白拿一次新人额度。
    # 也不发欢迎语 —— 对着一张「欢迎进来坐」的图把人踢出去更难看。
    if await asyncio.to_thread(ctx.db.is_blacklisted, group_id, str(user_id)):
        kicked = await onebot.kick(event, group_id, str(user_id))
        logger.info(
            f"[群助手] 黑名单成员进群，已自动踢出：{user_id} @ 群 {group_id}"
            f"（成功={kicked}，失败一般是机器人没有群管权限）"
        )
        return

    await asyncio.to_thread(
        ctx.db.ensure_member, group_id, str(user_id), ctx.config.welcome_quota
    )
    await asyncio.to_thread(ctx.db.set_join_info, group_id, str(user_id), join_time=join_ts)
    logger.info(f"[群助手] 记录入群时间：{user_id} @ 群 {group_id}")

    # 欢迎 / 验证的异常不能把「入群时间已落库」这件事一起带走 ——
    # 前者是锦上添花，后者是「在册天数」的依据。
    try:
        await welcome.on_join(ctx, event, str(group_id), str(user_id))
    except Exception as exc:
        logger.error(f"[群助手] 入群欢迎/验证失败: {exc}", exc_info=True)
