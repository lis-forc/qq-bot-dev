"""业务层：权限、资料、统计、额度。

这一层不直接发消息，只返回「要发什么文本」，由 main.py 决定怎么发。
好处是后面接 AI 或改文案时只动这里。
"""

from __future__ import annotations

import random
from datetime import date, datetime
from typing import Any

from .config import PluginConfig
from .database import Database, _day_before, _today
from ..utils import deco, nickname, onebot
from ..utils.router import validate_hobby
from ..utils.text import pad as _pad
from ..utils.text import width as _width

try:  # 本地单测没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

LEVEL_NAMES = {1: "成员", 2: "助手", 3: "管理", 4: "群主"}


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


def _short_time(value: Any) -> str:
    """'2026-10-24 14:20:01' → '10-24 14:20'。格式不对就原样给。"""
    text = str(value or "")
    return text[5:16] if len(text) >= 16 else text


def _live_streak(checkin: dict[str, Any]) -> int:
    """库里存的连签天数，断签就当 0。

    `streak_days` 只在「这次签到」那一刻被重算，之后一直躺着。所以一个人
    上周连签 20 天、这周没签，库里还是 20 —— 直接印到卡上就是在骗他。
    判断标准很简单：最后一次签到是今天或昨天，连签才算还活着。
    （昨天也算：今天还没签，不等于断了。）
    """
    streak = int(checkin.get("streak_days") or 0)
    if streak <= 0:
        return 0
    last = str(checkin.get("last_date") or "")
    today = _today()
    return streak if last in (today, _day_before(today)) else 0


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
        """展示用的性别：男 / 女 / 空串（未设定）。

        **空串是「没说」，不是「男」。** 名片底图仍然会给一个默认（按男版画），
        但群名片前面的记号必须是 🧸 —— 把没填性别的人一律标成男生，
        是替别人做主，而且做错了没人会来纠正，只会默默离群。
        """
        if not member:
            return ""
        return nickname.normalize_sex(member.get("sex"))

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
        # 群友自己用「性别」改过的值优先。need 是按多个字段一起判断的，
        # 所以很可能因为 join_time 还缺就来查一次协议端 —— 那时如果无脑写
        # sex，手动改的性别就被 QQ 资料冲掉了，而且悄无声息。
        #
        # 映射成空串（unknown）时写 None 而不是空串：set_join_info 只写非空值，
        # 所以「协议端也不知道」不会把库里已有的值擦掉。
        manual_sex = str((member or {}).get("sex") or "")
        sex = None if manual_sex else (nickname.normalize_sex(raw_sex) or None)
        # 群名片可能带着我们自己挂上去的性别记号，存进库前先剥掉 ——
        # 库里存的永远是「名字」，记号是展示层现拼的。
        card_text = str(info.get("card") or "").strip()
        qq_name = (
            nickname.strip(card_text, self.config) or str(info.get("nickname") or "").strip()
        ) or None

        return self.db.set_join_info(
            group_id, user_id, join_time=join_ts, sex=sex, qq_name=qq_name
        )

    # ---------------------------------------------------------- 修改
    def set_display_name(self, group_id: str, user_id: str, name: str) -> dict[str, Any]:
        """只写库。校验、拼群名片、调协议端都不在这里。

        这一层刻意不碰 IO：改了名字还要不要顺带改群名片、失败了怎么提示，
        属于「一次改名」这条业务流程，归 ``domains/nickname.py`` 管。
        职责分开的好处是这一层能被自测直接调、不会因为协议端挂了而失败。
        """
        self.db.ensure_member(group_id, user_id)
        return self.db.set_display_name(group_id, user_id, name)

    def set_hobby(self, group_id: str, user_id: str, hobby: str) -> str:
        ok, message = validate_hobby(hobby)
        if not ok:
            return f"⚠️ {message}"
        self.db.ensure_member(group_id, user_id)
        self.db.set_hobby(group_id, user_id, hobby)
        return f"✅ 爱好已更新\n{hobby}"

    def set_sex(self, group_id: str, user_id: str, sex: str) -> str:
        """改信息卡上的性别。sex 已经由 ``nickname.parse_sex`` 归一过。

        空串是合法值（= 未设定），所以这里不看真假只看 None ——
        「性别 保密」和「性别 不设」都要能落到空串上。
        """
        self.db.ensure_member(group_id, user_id)
        updated = self.db.set_sex(group_id, user_id, nickname.store_sex(sex))
        return f"✅ 性别已更新\n{nickname.sex_label(self.sex(updated))}"

    # ---------------------------------------------------------- 信息卡数据
    def card_data(
        self,
        group_id: str,
        user_id: str,
        member: dict[str, Any],
        counts: dict[str, int],
    ) -> dict[str, Any]:
        """信息卡要展示的全部数据。

        签到相关的三项（连签 / 累计签到 / 称号）也在这里取：
        卡是群友唯一会反复看的一页，「我连签几天了」不该逼他再发一次
        「签到记录」。查的是同一条 checkins 行，一次主键查询，几乎没成本。
        """
        checkin = {}
        try:
            checkin = self.db.get_checkin(group_id, user_id) or {}
        except Exception as exc:  # 签到表缺失也不该让信息卡整张画不出来
            logger.warning(f"[群助手] 读签到状态失败，信息卡留空: {exc}")

        return {
            "name": self.display_name(member, user_id),
            # 展示用「未设定」，底图那边会自己把它当男版处理（见 ImageBank.base_card）。
            "gender": nickname.sex_label(self.sex(member)),
            # 原始三态。群名片前面的记号按它选 🦊 / 🐰 / 🧸，不能用展示文案。
            "sex": self.sex(member),
            # 空串交给渲染层兜底：图片上写「还没写」，文字版可以干脆不占一行。
            "hobby": str(member.get("hobby") or ""),
            "quota": int(member.get("quota") or 0),
            "days": self.days(member),
            # 卡上六项里唯一来自「别人的记录」的字段。结缔成功时双方各写一份。
            "relation": str(member.get("relation") or ""),
            "relation_with": str(member.get("relation_with") or ""),
            "today": int(counts.get("today", 0)),
            "month": int(counts.get("month", 0)),
            "total": int(counts.get("total", 0)),
            "qq": user_id,
            "currency": self.config.currency_name,
            "streak": _live_streak(checkin),
            "checkin_days": int(checkin.get("total_days") or 0),
            # 名片皮肤名。渲染层拿它去 assets/images/cards 里找图；
            # 找不到（图被删了）就自动退回默认皮肤。
            "skin": str(member.get("card_skin") or ""),
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
            deco.head("发言统计", "stats"),
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
            deco.head(f"我的{self.config.currency_name}", "quota"),
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

    def render_history(self, group_id: str, user_id: str, limit: int = 20) -> str:
        """最近几笔额度变动。

        流水本来就在记（每次加扣都写 quota_logs），但一直没有入口，
        出问题时没法向群友解释「我这额度怎么没了」。这里把它露出来。
        """
        rows = self.db.quota_history(group_id, user_id, limit)
        balance = self.db.balance(group_id, user_id)
        if not rows:
            return (
                f"{deco.head('还没有额度记录', 'quota')}\n"
                f"当前{self.config.currency_name}：{balance}\n"
                f"发一条消息就能领新人礼"
            )

        # 两列对齐：金额列固定宽，原因列按显示宽度补空格（中文算两格）。
        amount_cell = max(_width(f"{int(row['amount']):+d}") for row in rows)
        reason_cell = max(_width(str(row["reason"] or "—")) for row in rows)
        lines = [deco.head("额度记录", "quota"), ""]
        for row in rows:
            amount = int(row["amount"])
            # 原来这里是 🟢/🔴。去掉有两个原因：astral emoji 在部分
            # QQ 客户端没有字形（显示成豆腐块），而且 width() 按 1 格算、
            # 实际可能占 2 格，会把金额列推歪。正负号本身已经说明了方向。
            cell = _pad(f"{amount:+d}", amount_cell)
            reason = _pad(str(row["reason"] or "—"), reason_cell)
            lines.append(f"{cell}  {reason}  {_short_time(row['created_at'])}")
        lines.append("")
        lines.append(f"当前{self.config.currency_name}：{balance}")
        return "\n".join(lines)

    def draw(self, group_id: str, user_id: str) -> str:
        """抽一次。先扣机会再结算，所以出错也不会多抽。"""
        ok, remaining = self.db.consume_draw(group_id, user_id)
        cur = self.config.currency_name
        if not ok:
            member = self.db.get_member(group_id, user_id) or {}
            progress = int(member.get("msg_progress") or 0)
            need = max(0, self.config.draw_per - progress)
            return (
                deco.head("没有抽奖机会了", "draw") + "\n"
                f"抽奖进度：{progress} / {self.config.draw_per}\n"
                f"再发 {need} 条就能再抽一次"
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

        lines = [deco.head(f"抽奖结果{' · 暴击！' if crit else ''}", "draw")]
        lines.append(f"获得{cur}：+{amount}")
        lines.append(f"当前{cur}：{balance}")
        # 剩下的机会放在最后一行，回答「我还能抽几次」这个必然会问的问题。
        if remaining > 0:
            lines.append(f"还剩 {remaining} 次机会")
        else:
            member = self.db.get_member(group_id, user_id) or {}
            progress = int(member.get("msg_progress") or 0)
            lines.append(f"机会用完了 · 再发 {self.config.draw_per - progress} 条可再抽")
        return "\n".join(lines)
