"""业务层：权限、资料、统计、额度。

这一层不直接发消息，只返回「要发什么文本」，由 main.py 决定怎么发。
好处是后面接 AI 或改文案时只动这里。
"""

from __future__ import annotations

import random
from datetime import date, datetime
from typing import Any

from .config import PluginConfig
from .database import Database
from ..utils import onebot
from ..utils.router import validate_display_name, validate_hobby

LEVEL_NAMES = {1: "成员", 2: "助手", 3: "管理", 4: "群主"}

# OneBot 的 sex 字段 → 展示用
SEX_MAP = {"male": "男", "female": "女", "unknown": "保密", "男": "男", "女": "女", "保密": "保密"}


def _as_date(value: Any) -> date | None:
    """把 unix 时间戳或 'YYYY-MM-DD ...' 字符串转成日期。"""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value)).date()
    except (ValueError, OSError, OverflowError):
        pass
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class Permission:
    """权限等级：QQ 群角色映射 与 插件内授权 取较高者。

    注意这里只看「等级」不看身份：任何被授到 4 级的人，都自动拥有
    4 级权限（包括继续授权别人）。所以「授权」不专属于 QQ 群主。

    群角色是**每次用时实时向协议端查**的，不做时间缓存 —— 群主转让、
    升/撤管理员之后，下一条口令立刻按新身份算，不会出现「旧群主还能
    开全群禁言」这种滞后。

    唯一的例外是查询失败：接口挂了会退回到上一次查到的角色（只读缓存，
    不参与正常路径），避免一次网络抖动就把群主判成普通成员。
    """

    def __init__(self, config: PluginConfig):
        self.config = config
        self._last_role: dict[tuple[str, str], str] = {}

    @staticmethod
    def level_name(level: int) -> str:
        return LEVEL_NAMES.get(max(1, min(4, level)), "成员")

    async def _role(self, event, group_id: str, user_id: str) -> str:
        role = await onebot.get_member_role(event, group_id, user_id)
        key = (str(group_id), str(user_id))
        if role:
            self._last_role[key] = role
            return role
        # 查不到（接口失败/该成员不在群）→ 用上次的已知角色兜底
        return self._last_role.get(key, "")

    async def level(
        self, event, group_id: str, user_id: str, member: dict[str, Any] | None
    ) -> int:
        manual = None
        if member and member.get("permission_level") is not None:
            manual = int(member["permission_level"])  # type: ignore[arg-type]

        mapped = 1
        role = await self._role(event, group_id, user_id)
        if role == "owner":
            mapped = self.config.owner_default_level
        elif role == "admin":
            mapped = self.config.admin_default_level

        if manual is None:
            return mapped
        # 取高：防止手动授权把群主/管理员「降权」
        return max(mapped, max(1, min(4, manual)))

    async def require(
        self, event, group_id: str, user_id: str, member: dict[str, Any] | None, need: int
    ) -> tuple[bool, int]:
        current = await self.level(event, group_id, user_id, member)
        return current >= need, current


class ProfileService:
    """资料：显示名、性别、入群时间、爱好。"""

    def __init__(self, db: Database, config: PluginConfig):
        self.db = db
        self.config = config

    # ---------------------------------------------------------- 展示
    @staticmethod
    def display_name(member: dict[str, Any] | None, user_id: str = "") -> str:
        if not member:
            return user_id or "未知"
        return (
            str(member.get("display_name") or "")
            or str(member.get("qq_name") or "")
            or str(member.get("user_id") or user_id)
            or "未知"
        )

    @staticmethod
    def sex(member: dict[str, Any] | None) -> str:
        if not member:
            return "保密"
        return SEX_MAP.get(str(member.get("sex") or ""), "") or "保密"

    @staticmethod
    def days(member: dict[str, Any] | None) -> int:
        """在册天数。优先用真实入群时间，拿不到才退化成首次出现日期。"""
        if not member:
            return 1
        started = _as_date(member.get("join_time")) or _as_date(member.get("first_seen"))
        if started is None:
            return 1
        return max(1, (date.today() - started).days + 1)

    # ---------------------------------------------------------- 协议端补全
    async def refresh_from_qq(
        self,
        event,
        group_id: str,
        user_id: str,
        member: dict[str, Any] | None,
        info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """从协议端补全真实入群时间 / 性别 / 群名片，并缓存进库。

        只在缺字段时才请求接口，所以一个成员一辈子基本只查一次。
        注意：NapCat 的 get_group_member_info 会返回 join_time（unix 秒），
        这是「真实入群时间」的来源，不用等 group_increase 通知。

        info 用来复用一个已经查到手的成员信息，避免同一件事查两遍。
        """
        need = (
            not member
            or member.get("join_time") is None
            or not member.get("qq_name")
            or not member.get("sex")
        )
        if not need:
            return member or {}

        if info is None:
            info = await onebot.get_member_info(event, group_id, user_id)
        if not info:
            return member or {}

        join_time = info.get("join_time")
        try:
            join_ts = int(join_time) if join_time else None
        except (TypeError, ValueError):
            join_ts = None

        raw_sex = str(info.get("sex") or "")
        sex = SEX_MAP.get(raw_sex, "保密") if raw_sex else None
        qq_name = str(info.get("card") or info.get("nickname") or "").strip() or None

        return self.db.set_join_info(
            group_id, user_id, join_time=join_ts, sex=sex, qq_name=qq_name
        )

    # ---------------------------------------------------------- 修改
    def set_display_name(
        self, group_id: str, user_id: str, name: str, *, charge: bool
    ) -> str:
        ok, message = validate_display_name(name)
        if not ok:
            return f"⚠️ {message}"

        member = self.db.ensure_member(group_id, user_id)
        cost = self.config.rename_cost if charge else 0
        old = self.display_name(member, user_id)

        if cost > 0:
            paid, balance = self.db.add_quota(
                group_id, user_id, -cost, "改名", operator_id=user_id
            )
            if not paid:
                return (
                    f"⚠️ {self.config.currency_name}不足\n"
                    f"改名需要 {cost}，你当前有 {balance}\n"
                    f"每发言 {self.config.draw_per} 条可用「抽奖」获得{self.config.currency_name}"
                )
        updated = self.db.set_display_name(group_id, user_id, name)
        self.db.log_op(group_id, user_id, "改名", user_id, f"{old} → {name}")

        lines = ["✅ 改名成功"]
        if old and old != name:
            lines.append(f"{old} → {name}")
        else:
            lines.append(f"显示名：{name}")
        if cost > 0:
            lines.append(f"消耗{self.config.currency_name}：{cost}")
        lines.append(f"当前{self.config.currency_name}：{updated.get('quota', 0)}")
        return "\n".join(lines)

    def set_hobby(self, group_id: str, user_id: str, hobby: str) -> str:
        ok, message = validate_hobby(hobby)
        if not ok:
            return f"⚠️ {message}"
        self.db.ensure_member(group_id, user_id)
        self.db.set_hobby(group_id, user_id, hobby)
        return f"✅ 爱好已更新\n{hobby}"

    # ---------------------------------------------------------- 信息卡数据
    def card_data(
        self,
        group_id: str,
        user_id: str,
        member: dict[str, Any],
        counts: dict[str, int],
    ) -> dict[str, Any]:
        return {
            "name": self.display_name(member, user_id),
            "gender": self.sex(member),
            "hobby": str(member.get("hobby") or "无"),
            "quota": int(member.get("quota") or 0),
            "days": self.days(member),
            "today": int(counts.get("today", 0)),
            "month": int(counts.get("month", 0)),
            "total": int(counts.get("total", 0)),
            "qq": user_id,
            "show_qq": self.config.card_show_qq,
            "currency": self.config.currency_name,
        }


class StatsService:
    """发言统计 + 抽奖进度。"""

    def __init__(self, db: Database, config: PluginConfig):
        self.db = db
        self.config = config

    def record_message(self, group_id: str, user_id: str) -> dict[str, Any]:
        return self.db.bump_message(group_id, user_id, self.config.draw_per)

    def render(self, group_id: str, user_id: str, member: dict[str, Any]) -> str:
        counts = self.db.counts(group_id, user_id)
        draws = int(member.get("draws") or 0)
        progress = int(member.get("msg_progress") or 0)
        lines = [
            "📊 发言统计",
            f"今日：{counts['today']} 条",
            f"本月：{counts['month']} 条",
            f"累计：{counts['total']} 条",
            "",
            f"抽奖进度：{progress} / {self.config.draw_per}",
            f"可抽次数：{draws}",
        ]
        if draws > 0:
            lines.append("发送「抽奖」立即抽取")
        return "\n".join(lines)


class QuotaService:
    """额度查询与抽奖。"""

    def __init__(self, db: Database, config: PluginConfig):
        self.db = db
        self.config = config

    def render(self, group_id: str, user_id: str, member: dict[str, Any]) -> str:
        draws = int(member.get("draws") or 0)
        progress = int(member.get("msg_progress") or 0)
        lines = [
            f"💎 我的{self.config.currency_name}",
            f"当前：{int(member.get('quota') or 0)}",
            "",
            f"抽奖进度：{progress} / {self.config.draw_per}",
            f"可抽次数：{draws}",
        ]
        if draws > 0:
            lines.append("发送「抽奖」立即抽取")
        else:
            lines.append(f"再发 {self.config.draw_per - progress} 条可抽一次")
        return "\n".join(lines)

    def draw(self, group_id: str, user_id: str) -> str:
        ok, remaining = self.db.consume_draw(group_id, user_id)
        if not ok:
            member = self.db.get_member(group_id, user_id) or {}
            progress = int(member.get("msg_progress") or 0)
            need = max(0, self.config.draw_per - progress)
            return (
                "⚠️ 还没有抽奖次数\n"
                f"抽奖进度：{progress} / {self.config.draw_per}\n"
                f"再发 {need} 条就能抽一次"
            )

        amount = random.randint(self.config.draw_min, self.config.draw_max)
        crit = self.config.draw_bonus_chance > 0 and random.random() < self.config.draw_bonus_chance
        if crit:
            # 加成，不是「取较大值」：原来写 max(amount, bonus)，
            # 一旦 draw_max 调到 bonus 之上，暴击就和普通结果完全一样，等于没暴击。
            amount += self.config.draw_bonus_amount
        _, balance = self.db.add_quota(
            group_id, user_id, amount, "抽奖" + ("暴击" if crit else ""), user_id
        )

        lines = [f"🎲 抽奖结果{' · 暴击！' if crit else ''}"]
        lines.append(f"获得{self.config.currency_name}：+{amount}")
        lines.append(f"当前{self.config.currency_name}：{balance}")
        lines.append(f"剩余抽奖次数：{remaining}")
        return "\n".join(lines)
