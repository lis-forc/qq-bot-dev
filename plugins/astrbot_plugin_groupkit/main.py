"""群助手插件入口。

这个文件只做三件事：建依赖、编排事件流、每天跑一次维护。
**功能不写在这里。** 加新功能去 `domains/` 建文件、去 `domains/registry.py`
登记一行，本文件不用改。

事件流：
    平台过滤 → 群白名单 → 通知事件（记入群时间 + 欢迎/改名提示/验证）→ 屏蔽机器人自己
        → 入群验证拦截（待验证的人发什么都不算口令、不计发言）
        → 命令分发：命中了就发结果并 stop_event（避免 AI 再回一遍）
        → 没命中：昵称守卫（催改名 / 把被手改的群名片改回来）→ 普通发言计数

昵称守卫**只跑在非口令消息上**。这是因为 `改名` 那条消息里带的群名片是
「改之前」的，守卫拿它比对会把刚改好的名字又推回去，和改名流程打架。

设计取向：
- 单入口 + 手工路由，不用 @filter.command，口令不需要任何前缀。
- 没有「注册」这一步：谁说话谁就有档案，资料从 QQ 侧自动补全。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.command import CommandEnv
from .core.checkin import CheckinService
from .core.config import PluginConfig
from .core.context import PluginContext
from .core.database import Database
from .core.meme_api import MemeApi
from .core.services import Permission, ProfileService, QuotaService, StatsService
from .core.web_api import WebApi
from .domains import bag as bag_flow
from .domains import nickname as nickname_guard
from .domains import notice, stats, welcome
from .domains.registry import dispatch
from .utils import event as ev
from .utils import onebot
from .utils.assets import ImageBank
from .utils.card import CardRenderer

PLUGIN_NAME = "astrbot_plugin_groupkit"


class GroupKit(Star):
    """轻量群助手：信息卡 / 额度 / 发言统计 / 基础群管。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = PluginConfig(config)
        plugin_dir = Path(__file__).resolve().parent
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        db = Database(data_dir)

        # 图片素材先建：签到（挑运势图）和信息卡（名片底图）都要它。
        # 顺序是有意的 —— 先有图库，再把同一个对象注入给两个消费者，
        # 保证「商店里买的皮肤」和「信息卡用的底图」查的是同一份目录。
        images = ImageBank(
            cfg.image_root,
            plugin_dir,
            default_skin_price=cfg.card_skin_price,
        )
        images.ensure_dirs()

        self.ctx = PluginContext(
            config=cfg,
            db=db,
            permission=Permission(cfg),
            profiles=ProfileService(db, cfg),
            stats=StatsService(db, cfg),
            quota=QuotaService(db, cfg),
            checkin=CheckinService(db, cfg, images),
            # 现在只剩这一个渲染器：把字排到别人做好的图上。
            cards=CardRenderer(
                plugin_dir,
                cfg.card_font_path,
                data_dir / "cards",
                images,
            ),
            images=images,
            meme=MemeApi(
                cfg.meme_api_base_url,
                cfg.meme_timeout,
                cfg.meme_max_image_mb * 1024 * 1024,
            ),
            plugin_dir=plugin_dir,
            data_dir=data_dir,
        )
        self._pruned_on: str = ""  # 每天最多清理一次
        # 管理台的 HTTP 接口。单独挂在实例上而不是放进 PluginContext：
        # ctx 只装「命令处理函数要用的东西」，WebApi 只被插件生命周期调用。
        self.web = WebApi(
            db, cfg, PLUGIN_NAME, self.ctx.checkin, plugin_dir, self.ctx.images
        )
        logger.info(f"[群助手] 已加载，数据目录：{data_dir}")

    async def initialize(self):
        """插件启用时挂上管理台接口。

        放在 initialize 而不是 __init__ 里：AstrBot 要等插件真正加载完成
        才把 context 的完整能力交出来。注册失败不影响聊天，只少一个页面。
        """
        try:
            self.web.register(self.context)
        except Exception as exc:  # 管理台挂不上不该阻断插件本身
            logger.error(f"[群助手] 管理台注册异常，已跳过: {exc}", exc_info=True)

    async def terminate(self):
        try:
            # 先停掉入群验证的定时器：热重载之后旧任务会拿旧 event 去踢人，
            # 而新实例看不到这批 pending，没人能认领。
            await welcome.shutdown()
        except Exception as exc:
            logger.warning(f"[群助手] 停止入群验证任务失败: {exc}")
        try:
            # 结缔请求挂着的资格也要退回去 —— 那是花额度买的。
            await bag_flow.shutdown(self.ctx.db)
        except Exception as exc:
            logger.warning(f"[群助手] 停止结缔任务失败: {exc}")
        try:
            self.web.unregister()
        except Exception as exc:
            logger.warning(f"[群助手] 注销管理台接口失败: {exc}")
        try:
            await self.ctx.meme.close()
        except Exception as exc:  # 卸载时关连接池失败不该影响插件卸载
            logger.warning(f"[群助手] 关闭表情服务连接失败: {exc}")
        self.ctx.db.close()
        logger.info("[群助手] 已卸载")

    # ================================================================= 事件入口
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent) -> AsyncGenerator:
        config = self.ctx.config

        if not onebot.is_supported(event):
            return
        group_id = str(event.get_group_id() or "")
        if not group_id:
            # 私聊一律不接 —— 这个插件是群工具，不装聊天机器人。
            # （验证早就回退成纯群内答题了，私聊没有任何要处理的事。）
            return
        if not config.is_group_enabled(group_id):
            return

        await self._maybe_prune()

        # 非聊天消息：只用来记真实入群时间，其余忽略。
        # 这样通知事件既不会被误算成发言，也不会打扰群。
        if not ev.is_message_post(event):
            await notice.handle_notice(self.ctx, event, group_id)
            return

        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == ev.self_id(event):
            return

        member = await asyncio.to_thread(
            self.ctx.db.ensure_member, group_id, sender_id, config.welcome_quota
        )
        text = ev.clean_text(event.message_str or "")

        # 正在答题的新人：他的消息属于验证流程，不能进命令分发，也不该
        # 计入发言数 —— 否则「发一串答案蒙对」同时还在刷抽奖进度。
        if text:
            verdict = await welcome.intercept(
                self.ctx, event, group_id, sender_id, text
            )
            if verdict.handled:
                if verdict.reply:
                    yield event.plain_result(verdict.reply)
                # 不吃掉的话，验证用的那句话还会被 AI 当普通聊天回一遍。
                event.stop_event()
                return

            # 被结缔请求 @ 的人：只精确拦截「同意 / 不同意」，其他消息照常。
            verdict = await bag_flow.relation_intercept(
                self.ctx, event, group_id, sender_id, text
            )
            if verdict is not None and verdict.handled:
                if verdict.reply:
                    yield event.plain_result(verdict.reply)
                event.stop_event()
                return

        command_hit = False
        if text:
            env = CommandEnv(
                event=event,
                group_id=group_id,
                sender_id=sender_id,
                member=member,
                text=text,
            )
            async for reply in dispatch(self.ctx, env):
                yield reply
            # 命中就算处理完，不能只看「有没有回复」：
            # 表情包这类命令会把结果直接通过协议端发出去（小图模式），
            # 走不到 yield，但那句话已经有人负责了，不能再当普通聊天。
            command_hit = env.command is not None
            if command_hit:
                event.stop_event()
                if not config.count_command_messages:
                    return

        # 昵称守卫只跑在「普通发言」上。
        #
        # 为什么不跑在口令上：`改名` 这条消息本身带的是**改之前**的群名片，
        # 守卫拿它去比对，会把刚改好的名字又改回旧的 —— 和改名流程抢同一张
        # 名片，改一次回一次，永远对不上。跳过口令就没有这个时序问题。
        if text and not command_hit:
            try:
                prompt = await nickname_guard.guard(
                    self.ctx, event, group_id, sender_id, member
                )
            except Exception as exc:  # 守卫出问题不该让这条消息彻底没反应
                logger.error(f"[群助手] 昵称守卫失败: {exc}", exc_info=True)
                prompt = None
            if prompt:
                yield event.plain_result(prompt)

        async for reply in stats.count_message(self.ctx, event, group_id, sender_id):
            yield reply

    # ================================================================= 每日维护
    async def _maybe_prune(self) -> None:
        """每天最多跑一次按保留期裁剪，避免流水表无限长大。"""
        today = date.today().isoformat()
        if self._pruned_on == today:
            return
        self._pruned_on = today
        try:
            removed = await asyncio.to_thread(
                self.ctx.db.prune,
                self.ctx.config.log_retention_days,
                self.ctx.config.stats_retention_days,
            )
            if any(removed.values()):
                logger.info(f"[群助手] 按保留期清理旧数据：{removed}")
            # 裁剪之后顺便让 SQLite 刷新查询计划统计。
            # 放在这里而不是每条消息后跑：一天一次就够，也不会挡在群消息的路径上。
            await asyncio.to_thread(self.ctx.db.optimize)
        except Exception as exc:
            logger.error(f"[群助手] 清理旧数据失败: {exc}", exc_info=True)
