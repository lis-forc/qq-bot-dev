"""群助手插件入口。

事件流：
    平台过滤 → 群白名单 → 通知事件（只记入群时间）→ 屏蔽机器人自己
        → 口令路由：命中了就发结果并 event.stop_event()（避免 AI 再回一遍）
        → 没命中：普通发言计数

设计取向：
- 一个入口 + 手工路由，不用 @filter.command，口令不需要任何前缀。
- 没有「注册」这一步：谁说话谁就有档案，资料从 QQ 侧自动补全。
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.config import PluginConfig
from .core.database import Database
from .core.services import Permission, ProfileService, QuotaService, StatsService
from .utils import onebot
from .utils.card import CardRenderer
from .utils.router import compact, drop_prefix, parse_duration, parse_int, strip_at

PLUGIN_NAME = "astrbot_plugin_groupkit"

# 「踢 @某人 拉黑」里的拉黑后缀。命中任意一个就走 reject_add_request。
# 只做包含匹配，位置随意：拉黑可以写在 @ 前面也可以写在后面。
KICK_REJECT_WORDS = ("拉黑", "拒收", "拒绝", "黑名单")


def parse_kick_reject(args: str) -> bool:
    """踢人参数里有没有「拉黑」。

    抽成函数只为了能单测：这是「他还能不能再进来」的分叉，
    判错了不会报错，只会在几天后有人重新申请入群时才发现。
    """
    return any(word in (args or "") for word in KICK_REJECT_WORDS)


def _raw_get(raw: Any, key: str, default: Any = None) -> Any:
    """OneBot 的 raw_message 是平铺 dict；兼容一下对象形态。"""
    if raw is None:
        return default
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


class GroupKit(Star):
    """轻量群助手：信息卡 / 额度 / 发言统计 / 基础群管。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = PluginConfig(config)
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME

        self.db = Database(self.data_dir)
        self.permission = Permission(self.config)
        self.profiles = ProfileService(self.db, self.config)
        self.stats = StatsService(self.db, self.config)
        self.quota = QuotaService(self.db, self.config)
        self.cards = CardRenderer(
            self.plugin_dir,
            self.config.card_bg_path,
            self.config.card_font_path,
            self.data_dir / "cards",
        )
        self._pruned_on: str = ""  # 每天最多清理一次
        logger.info(f"[群助手] 已加载，数据目录：{self.data_dir}")

    async def terminate(self):
        self.db.close()
        logger.info("[群助手] 已卸载")

    # ================================================================= 事件入口
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent) -> AsyncGenerator:
        if not onebot.is_supported(event):
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or not self.config.is_group_enabled(group_id):
            return

        await self._maybe_prune()

        # 非聊天消息：只用来记真实入群时间，其余忽略。
        # 这样通知事件既不会被误算成发言，也不会打扰群。
        if not self._is_message_post(event):
            await self._handle_notice(event, group_id)
            return

        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return

        member = await asyncio.to_thread(
            self.db.ensure_member, group_id, sender_id, self.config.welcome_quota
        )
        text = self._clean_text(event.message_str or "")

        if text:
            handled = False
            async for reply in self._route(event, group_id, sender_id, member, text):
                handled = True
                yield reply
            if handled:
                event.stop_event()
                if not self.config.count_command_messages:
                    return

        async for reply in self._count_message(event, group_id, sender_id):
            yield reply

    async def _maybe_prune(self) -> None:
        """每天最多跑一次按保留期裁剪，避免流水表无限长大。"""
        today = date.today().isoformat()
        if self._pruned_on == today:
            return
        self._pruned_on = today
        try:
            removed = await asyncio.to_thread(
                self.db.prune,
                self.config.log_retention_days,
                self.config.stats_retention_days,
            )
            if any(removed.values()):
                logger.info(f"[群助手] 按保留期清理旧数据：{removed}")
            # 裁剪之后顺便让 SQLite 刷新查询计划统计。
            # 放在这里而不是每条消息后跑：一天一次就够，也不会挡在群消息的路径上。
            await asyncio.to_thread(self.db.optimize)
        except Exception as exc:
            logger.error(f"[群助手] 清理旧数据失败: {exc}", exc_info=True)

    async def _handle_notice(self, event, group_id: str) -> None:
        """入群通知 → 立刻记下真实入群时间，不用等下次查接口。"""
        raw = _raw_get(getattr(event, "message_obj", None), "raw_message")
        if _raw_get(raw, "post_type") != "notice":
            return
        if _raw_get(raw, "notice_type") != "group_increase":
            return
        user_id = _raw_get(raw, "user_id")
        if not user_id:
            return
        try:
            join_ts = int(_raw_get(raw, "time") or 0) or int(time.time())
        except (TypeError, ValueError):
            join_ts = int(time.time())

        await asyncio.to_thread(
            self.db.ensure_member, group_id, str(user_id), self.config.welcome_quota
        )
        await asyncio.to_thread(self.db.set_join_info, group_id, str(user_id), join_time=join_ts)
        logger.info(f"[群助手] 记录入群时间：{user_id} @ 群 {group_id}")

    async def _count_message(self, event, group_id: str, sender_id: str) -> AsyncGenerator:
        try:
            result = await asyncio.to_thread(self.stats.record_message, group_id, sender_id)
        except Exception as exc:
            logger.error(f"[群助手] 发言统计失败: {exc}", exc_info=True)
            return
        if self.config.notify_draw_ready and result.get("draws_gained"):
            yield event.plain_result(
                "🎟 抽奖机会 +1\n"
                f"抽奖进度：{result.get('msg_progress')} / {self.config.draw_per}\n"
                "发送「抽奖」试试手气"
            )

    # ================================================================= 口令路由
    async def _route(self, event, group_id: str, sender_id: str, member, text: str) -> AsyncGenerator:
        lower = text.lower()
        triggers = self.config.info_triggers

        # ---------- 个人信息卡 ----------
        if lower in triggers:
            target = self._at_target(event, sender_id) or sender_id
            async for reply in self._card_reply(event, group_id, sender_id, target, member):
                yield reply
            return
        for trigger in triggers:  # i 123456789
            match = re.fullmatch(rf"{re.escape(trigger)}\s+(\d{{5,12}})", lower)
            if match:
                async for reply in self._card_reply(
                    event, group_id, sender_id, match.group(1), member
                ):
                    yield reply
                return

        # ---------- 帮助 ----------
        if text in {"帮助", "口令", "菜单", "功能", "指令"} or lower in {"help", "menu"}:
            async for reply in self._send_help(event):
                yield reply
            return

        # ---------- 改名 / 爱好 ----------
        args = drop_prefix(text, ("改名", "改名字", "改个名"))
        if args is not None:
            async for reply in self._cmd_rename(event, group_id, sender_id, member, args):
                yield reply
            return

        args = drop_prefix(text, ("爱好", "设置爱好"))
        if args is not None:
            if not args:
                yield event.plain_result("格式：爱好 打游戏")
                return
            yield event.plain_result(await self._cmd_hobby(group_id, sender_id, args))
            return

        # ---------- 额度 / 抽奖 / 统计 ----------
        if text in {"额度", "我的额度", "余额", "查额度"}:
            yield event.plain_result(await self._cmd_quota(group_id, sender_id, member))
            return

        if text in {"抽奖", "抽一次", "出手气"}:
            yield event.plain_result(await self._cmd_draw(group_id, sender_id, member))
            return

        if text in {"今日", "发言", "统计", "今日发言", "我的发言"}:
            yield event.plain_result(await self._cmd_stats(group_id, sender_id, member))
            return

        if text in {"额度记录", "额度流水", "流水", "我的额度记录"}:
            yield event.plain_result(await self._cmd_quota_history(group_id, sender_id))
            return

        # ---------- 群管（顺序：全群类必须在「禁言/解禁」前面） ----------
        if text in {"全群禁言", "全体禁言", "禁言全体", "全员禁言"}:
            async for reply in self._cmd_whole_ban(event, group_id, sender_id, member, True):
                yield reply
            return

        if text in {"全群解禁", "解除全群禁言", "解除全员禁言", "解除禁言全体"}:
            async for reply in self._cmd_whole_ban(event, group_id, sender_id, member, False):
                yield reply
            return

        if drop_prefix(text, ("解禁", "解除禁言")) is not None:
            async for reply in self._cmd_unmute(event, group_id, sender_id, member):
                yield reply
            return

        args = drop_prefix(text, ("禁言", "口球"))
        if args is not None:
            async for reply in self._cmd_mute(event, group_id, sender_id, member, args):
                yield reply
            return

        args = drop_prefix(text, ("踢", "踢人", "踢出", "移出"))
        if args is not None:
            async for reply in self._cmd_kick(event, group_id, sender_id, member, args):
                yield reply
            return

        args = drop_prefix(text, ("发额度", "给额度", "加额度"))
        if args is not None:
            async for reply in self._cmd_adjust_quota(event, group_id, sender_id, member, args, 1):
                yield reply
            return

        args = drop_prefix(text, ("扣额度", "减额度", "罚额度"))
        if args is not None:
            async for reply in self._cmd_adjust_quota(event, group_id, sender_id, member, args, -1):
                yield reply
            return

        args = drop_prefix(text, ("授权", "设权限", "设置权限"))
        if args is not None:
            async for reply in self._cmd_grant(event, group_id, sender_id, member, args):
                yield reply
            return

    # ================================================================= 资料
    async def _cmd_rename(self, event, group_id: str, sender_id: str, member, args: str) -> AsyncGenerator:
        targets = self._at_targets(event, sender_id)
        if targets:
            ok, _ = await self.permission.require(event, group_id, sender_id, member, 2)
            if not ok:
                yield event.plain_result(f"⚠️ 需要「{Permission.level_name(2)}」权限")
                return
            target_id = targets[0]
            charge = False  # 管理代改不扣对方额度
        else:
            target_id = sender_id
            charge = self.config.rename_cost > 0

        name = compact(strip_at(args))
        if not name:
            yield event.plain_result("✏️ 改名格式\n改名 李四\n改名 @某人 李四")
            return
        yield event.plain_result(
            await asyncio.to_thread(
                self.profiles.set_display_name, group_id, target_id, name, charge=charge
            )
        )

    async def _cmd_hobby(self, group_id: str, sender_id: str, args: str) -> str:
        return await asyncio.to_thread(
            self.profiles.set_hobby, group_id, sender_id, strip_at(args)
        )

    # ================================================================= 额度 / 统计
    async def _cmd_quota(self, group_id: str, sender_id: str, member) -> str:
        return await asyncio.to_thread(self.quota.render, group_id, sender_id, member)

    async def _cmd_draw(self, group_id: str, sender_id: str, member) -> str:
        return await asyncio.to_thread(self.quota.draw, group_id, sender_id)

    async def _cmd_stats(self, group_id: str, sender_id: str, member) -> str:
        return await asyncio.to_thread(self.stats.render, group_id, sender_id, member)

    async def _cmd_quota_history(self, group_id: str, sender_id: str) -> str:
        return await asyncio.to_thread(self.quota.render_history, group_id, sender_id)

    # ================================================================= 信息卡
    async def _card_reply(
        self, event, group_id: str, sender_id: str, target_id: str, member
    ) -> AsyncGenerator:
        info: dict | None = None
        if target_id == sender_id and member:
            target_member = member
        else:
            # 先只读，绝不顺手建档。否则 `i 10001` 这种随手输的号码会给
            # 陌生人凭空写一行 members，永久污染成员表。
            target_member = await asyncio.to_thread(self.db.get_member, group_id, target_id)
            if target_member is None:
                # 库里没有 = 从没在本群说过话，先问协议端他在不在群
                info = await onebot.get_member_info(event, group_id, target_id)
                if not info:
                    yield event.plain_result(
                        "🔍 没找到这个成员\n"
                        "确认一下 QQ 号，或者人不在这个群里"
                    )
                    return
                target_member = await asyncio.to_thread(
                    self.db.ensure_member, group_id, target_id, 0
                )

        # 补全真实入群时间 / 性别 / 群名片。缺字段才发请求，一个成员基本只查一次。
        target_member = await self.profiles.refresh_from_qq(
            event, group_id, target_id, target_member, info=info
        )

        counts = await asyncio.to_thread(self.db.counts, group_id, target_id)
        data = self.profiles.card_data(group_id, target_id, target_member, counts)

        avatar = await self._fetch_avatar(target_id)
        path = await asyncio.to_thread(self.cards.render, data, avatar)
        if path is None:
            yield event.plain_result(self._text_card(data))  # 画图失败也要有结果
            return
        yield event.chain_result([Image.fromFileSystem(str(path))])

    async def _fetch_avatar(self, user_id: str) -> bytes | None:
        """拉 QQ 头像。

        用公开的头像 CDN 而不是协议端接口：不占用 NapCat 的调用配额，
        也不需要机器人有任何额外权限。失败就返回 None，卡片会自动画首字占位。
        """
        if not self.config.card_show_avatar:
            return None
        url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

        def _download() -> bytes | None:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=8) as response:
                data = response.read()
            return data if len(data) > 512 else None

        try:
            return await asyncio.to_thread(_download)
        except Exception as exc:
            logger.warning(f"[群助手] 获取头像失败 {user_id}: {exc}")
            return None

    def _text_card(self, data: dict) -> str:
        return "\n".join(
            [
                "🪪 个人信息卡",
                f"姓名：{data['name']}",
                f"性别：{data['gender']}",
                f"爱好：{data['hobby']}",
                f"{data['currency']}：{data['quota']}",
                f"在册：{data['days']} 天",
                f"今日：{data['today']} 条",
                f"本月：{data['month']} 条",
                f"累计：{data['total']} 条",
            ]
        )

    # ================================================================= 帮助
    def _help_pages(self) -> list[tuple[str, str]]:
        cur = self.config.currency_name
        page1 = (
            f"🧩 群助手 · 基础\n\n"
            f"i\n"
            f"  个人信息卡（图片）\n\n"
            f"i @某人  /  i QQ号\n"
            f"  看别人的\n\n"
            f"改名 李四\n"
            f"  改显示名，消耗 {self.config.rename_cost} {cur}\n\n"
            f"爱好 打游戏\n"
            f"  设置信息卡上的爱好\n\n"
            f"{cur} / 抽奖 / 今日\n"
            f"  余额、抽奖机会、发言统计\n\n"
            f"额度记录\n"
            f"  最近 5 笔变动\n\n"
            f"帮助\n"
            f"  本菜单\n\n"
            f"新手礼：首次发言自动到账 {self.config.welcome_quota} {cur}；"
            f"每发言 {self.config.draw_per} 条攒一次抽奖。"
        )
        page2 = (
            "🛡 群助手 · 管理\n\n"
            "【2 级 · 助手】\n"
            "禁言 @某人 10分钟\n"
            "解禁 @某人\n"
            "改名 @某人 李四\n\n"
            "【3 级 · 管理】\n"
            "踢 @某人 [拉黑]\n"
            "发额度 @某人 50\n"
            "扣额度 @某人 50\n\n"
            "【4 级 · 群主】\n"
            "全群禁言 / 全群解禁\n"
            "授权 @某人 3\n\n"
            "禁言默认单位是分钟，最长 30 天。\n"
            "踢人加「拉黑」，对方就不能再申请入群。"
        )
        page3 = (
            "📖 群助手 · 权限\n\n"
            f"1 {Permission.level_name(1)}\n"
            "  信息卡 / 额度 / 抽奖 / 今日 / 额度记录 / 改名\n\n"
            f"2 {Permission.level_name(2)}\n"
            "  禁言 / 解禁 / 代改名\n\n"
            f"3 {Permission.level_name(3)}\n"
            "  踢人 / 发额度 / 扣额度\n\n"
            f"4 {Permission.level_name(4)}\n"
            "  全群禁言 / 授权\n\n"
            "QQ 群主默认 4 级，QQ 管理员默认 3 级。\n"
            "授权不看身份，只看等级：任何被授到 4 级的人"
            "都能继续授权别人。"
        )
        return [("群助手 · 基础", page1), ("群助手 · 管理", page2), ("群助手 · 权限", page3)]

    async def _send_help(self, event) -> AsyncGenerator:
        pages = self._help_pages()
        if self.config.help_style == "forward":
            chain = self._forward_chain(event, pages)
            if chain:
                yield event.chain_result(chain)
                return
        for _, body in pages:
            yield event.plain_result(body)

    def _forward_chain(self, event, pages: list[tuple[str, str]]) -> list | None:
        """帮助打包成合并转发，点开才展开，避免刷屏。"""
        try:
            uin = str(event.get_self_id() or "10000")
            nodes = [Node(uin=uin, name=title, content=[Plain(body)]) for title, body in pages]
            if len(nodes) == 1:
                return [nodes[0]]
            try:
                return [Nodes(nodes=nodes)]
            except TypeError:
                return [Nodes(nodes)]
        except Exception as exc:
            logger.error(f"[群助手] 构造合并转发失败，改用文字: {exc}")
            return None

    # ================================================================= 群管
    async def _cmd_mute(self, event, group_id: str, sender_id: str, member, args: str) -> AsyncGenerator:
        ok, _ = await self.permission.require(event, group_id, sender_id, member, 2)
        if not ok:
            yield event.plain_result(f"⚠️ 需要「{Permission.level_name(2)}」权限")
            return
        targets = self._at_targets(event, sender_id)
        if not targets:
            yield event.plain_result(self._target_hint(event, "格式：禁言 @某人 10分钟"))
            return
        seconds = parse_duration(strip_at(args))
        if seconds is None:
            yield event.plain_result("⚠️ 没看懂时长\n格式：禁言 @某人 10分钟")
            return
        if seconds == 0:
            yield event.plain_result("⚠️ 0 分钟就是没禁言，要解禁请用「解禁 @某人」")
            return
        if not await onebot.is_bot_admin(event, group_id):
            yield event.plain_result("⚠️ 机器人没有群管理权限，没法禁言")
            return
        target = targets[0]
        if target in {sender_id, str(event.get_self_id() or "")}:
            yield event.plain_result("⚠️ 这个目标不能禁言")
            return

        done = await onebot.mute(event, group_id, target, seconds)
        if done:
            await asyncio.to_thread(self.db.log_op, group_id, sender_id, "禁言", target, str(seconds))
        yield event.plain_result(
            f"🔇 已禁言 {self._fmt_duration(seconds)}" if done else "⚠️ 禁言失败，检查机器人权限"
        )

    async def _cmd_unmute(self, event, group_id: str, sender_id: str, member) -> AsyncGenerator:
        ok, _ = await self.permission.require(event, group_id, sender_id, member, 2)
        if not ok:
            yield event.plain_result(f"⚠️ 需要「{Permission.level_name(2)}」权限")
            return
        targets = self._at_targets(event, sender_id)
        if not targets:
            yield event.plain_result(self._target_hint(event, "格式：解禁 @某人"))
            return
        if not await onebot.is_bot_admin(event, group_id):
            yield event.plain_result("⚠️ 机器人没有群管理权限，没法解禁")
            return
        done = await onebot.unmute(event, group_id, targets[0])
        if done:
            await asyncio.to_thread(self.db.log_op, group_id, sender_id, "解禁", targets[0])
        yield event.plain_result("🔊 已解除禁言" if done else "⚠️ 解禁失败，检查机器人权限")

    async def _cmd_kick(
        self, event, group_id: str, sender_id: str, member, args: str = ""
    ) -> AsyncGenerator:
        ok, _ = await self.permission.require(event, group_id, sender_id, member, 3)
        if not ok:
            yield event.plain_result(f"⚠️ 需要「{Permission.level_name(3)}」权限")
            return
        targets = self._at_targets(event, sender_id)
        if not targets:
            yield event.plain_result(self._target_hint(event, "格式：踢 @某人  [拉黑]"))
            return
        target = targets[0]
        if target in {sender_id, str(event.get_self_id() or "")}:
            yield event.plain_result("⚠️ 这个目标不能踢")
            return
        if not await onebot.is_bot_admin(event, group_id):
            yield event.plain_result("⚠️ 机器人没有群管理权限，没法踢人")
            return

        # 「踢 @某人 拉黑」= 踢出去并且不再同意他入群。
        # 不带后缀就是普通踢人，他随时能再申请进来 —— 这个区别值得一个口令区分开。
        reject = parse_kick_reject(args)
        done = await onebot.kick(event, group_id, target, reject_add_request=reject)
        if not done:
            yield event.plain_result("⚠️ 踢人失败，检查机器人权限")
            return
        await asyncio.to_thread(
            self.db.log_op, group_id, sender_id, "踢人+拉黑" if reject else "踢人", target
        )
        yield event.plain_result(
            "👋 已踢出并拉黑\n该成员不能再申请入群" if reject else "👋 已踢出该成员"
        )

    async def _cmd_whole_ban(
        self, event, group_id: str, sender_id: str, member, enable: bool
    ) -> AsyncGenerator:
        ok, _ = await self.permission.require(event, group_id, sender_id, member, 4)
        if not ok:
            yield event.plain_result(f"⚠️ 需要「{Permission.level_name(4)}」权限")
            return
        if not await onebot.is_bot_admin(event, group_id):
            yield event.plain_result("⚠️ 机器人没有群管理权限，没法操作全群禁言")
            return
        done = await onebot.set_whole_ban(event, group_id, enable)
        if done:
            await asyncio.to_thread(
                self.db.log_op, group_id, sender_id, "全群禁言" if enable else "解除全群禁言"
            )
            yield event.plain_result("🔇 已开启全群禁言" if enable else "🔊 已解除全群禁言")
        else:
            yield event.plain_result("⚠️ 操作失败，检查机器人权限")

    async def _cmd_adjust_quota(
        self, event, group_id: str, sender_id: str, member, args: str, sign: int
    ) -> AsyncGenerator:
        ok, _ = await self.permission.require(event, group_id, sender_id, member, 3)
        if not ok:
            yield event.plain_result(f"⚠️ 需要「{Permission.level_name(3)}」权限")
            return
        targets = self._at_targets(event, sender_id)
        amount = abs(parse_int(strip_at(args)))
        fmt = f"格式：{'发' if sign > 0 else '扣'}{self.config.currency_name} @某人 50"
        if not targets:
            yield event.plain_result(self._target_hint(event, fmt))
            return
        if amount <= 0:
            yield event.plain_result(fmt)
            return

        target = targets[0]
        ok, balance = await asyncio.to_thread(
            self.db.add_quota, group_id, target, amount * sign,
            "管理发放" if sign > 0 else "管理扣除", sender_id,
        )
        if not ok:
            yield event.plain_result(
                f"⚠️ 对方{self.config.currency_name}不足，扣不到负数\n当前：{balance}"
            )
            return
        await asyncio.to_thread(
            self.db.log_op, group_id, sender_id,
            "发额度" if sign > 0 else "扣额度", target, str(amount),
        )
        yield event.plain_result(
            f"{'💎 已发放' if sign > 0 else '💸 已扣除'} {amount} {self.config.currency_name}\n"
            f"对方当前：{balance}"
        )

    async def _cmd_grant(
        self, event, group_id: str, sender_id: str, member, args: str
    ) -> AsyncGenerator:
        # 这里只校验「等级 ≥ 4」，不校验身份。
        # 所以群主授到 4 级的人，同样可以继续给别授权。
        ok, _ = await self.permission.require(event, group_id, sender_id, member, 4)
        if not ok:
            yield event.plain_result(f"⚠️ 需要「{Permission.level_name(4)}」权限")
            return
        targets = self._at_targets(event, sender_id)
        level = parse_int(strip_at(args))
        if not targets or level < 1 or level > 4:
            yield event.plain_result("格式：授权 @某人 3\n（1~4 级）")
            return
        target = targets[0]
        await asyncio.to_thread(self.db.ensure_member, group_id, target, 0)
        await asyncio.to_thread(self.db.set_permission, group_id, target, level)
        await asyncio.to_thread(self.db.log_op, group_id, sender_id, "授权", target, str(level))
        yield event.plain_result(f"✅ 已将该成员设为 {level} 级（{Permission.level_name(level)}）")

    # ================================================================= 小工具
    @staticmethod
    def _is_message_post(event) -> bool:
        """这是不是一条真正的聊天消息（而不是 notice / request 等）。"""
        raw = _raw_get(getattr(event, "message_obj", None), "raw_message")
        post_type = _raw_get(raw, "post_type")
        if not post_type:
            return True  # 拿不到就按消息处理，宁可多算也不要漏
        return str(post_type) in {"message", "message_sent"}

    @staticmethod
    def _clean_text(text: str) -> str:
        """压空白。口令不需要任何前缀，所以这里不做前缀剥离。"""
        return compact(text)

    def _at_targets(self, event, sender_id: str) -> list[str]:
        self_id = str(event.get_self_id() or "")
        return [t for t in onebot.get_at_targets(event) if t not in {sender_id, self_id}]

    def _target_hint(self, event, fmt: str) -> str:
        """没解析出目标时给出准确原因。

        目标里会滤掉「自己」和「机器人」，所以 `禁言 @自己 10分钟` 会得到
        空目标。这时回一句「格式：禁言 @某人」等于骗人——他明明 @ 了人。
        """
        return "⚠️ 不能对自己或机器人操作" if onebot.get_at_targets(event) else fmt

    def _at_target(self, event, sender_id: str) -> str | None:
        targets = self._at_targets(event, sender_id)
        return targets[0] if targets else None

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        if seconds >= 86400 and seconds % 86400 == 0:
            return f"{seconds // 86400} 天"
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"{seconds // 3600} 小时"
        if seconds >= 60 and seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"
