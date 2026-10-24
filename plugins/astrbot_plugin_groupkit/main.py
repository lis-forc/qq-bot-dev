"""群助手插件入口。

这个文件只做三件事：建依赖、编排事件流、每天跑一次维护。
**功能不写在这里。** 加新功能去 `domains/` 建文件、去 `domains/registry.py`
登记一行，本文件不用改。

事件流：
    平台过滤 → 群白名单 → 通知事件（只记入群时间）→ 屏蔽机器人自己
        → 命令分发：命中了就发结果并 stop_event（避免 AI 再回一遍）
        → 没命中：普通发言计数

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
from .core.config import PluginConfig
from .core.context import PluginContext
from .core.database import Database
from .core.services import Permission, ProfileService, QuotaService, StatsService
from .domains import notice, stats
from .domains.registry import dispatch
from .utils import event as ev
from .utils import onebot
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

        self.ctx = PluginContext(
            config=cfg,
            db=db,
            permission=Permission(cfg),
            profiles=ProfileService(db, cfg),
            stats=StatsService(db, cfg),
            quota=QuotaService(db, cfg),
            cards=CardRenderer(
                plugin_dir,
                cfg.card_bg_path,
                cfg.card_font_path,
                data_dir / "cards",
            ),
            plugin_dir=plugin_dir,
            data_dir=data_dir,
        )
        self._pruned_on: str = ""  # 每天最多清理一次
        logger.info(f"[群助手] 已加载，数据目录：{data_dir}")

    async def terminate(self):
        self.ctx.db.close()
        logger.info("[群助手] 已卸载")

    # ================================================================= 事件入口
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent) -> AsyncGenerator:
        config = self.ctx.config

        if not onebot.is_supported(event):
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or not config.is_group_enabled(group_id):
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

        if text:
            env = CommandEnv(
                event=event,
                group_id=group_id,
                sender_id=sender_id,
                member=member,
                text=text,
            )
            handled = False
            async for reply in dispatch(self.ctx, env):
                handled = True
                yield reply
            if handled:
                event.stop_event()
                if not config.count_command_messages:
                    return

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
