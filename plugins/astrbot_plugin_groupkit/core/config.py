"""配置读取层。

把 AstrBot 注入的 dict 包成带默认值和值域收敛的强类型属性，
插件其它地方只读属性，不再直接摸 dict，避免配置缺项导致崩溃。
"""

from __future__ import annotations

from typing import Any

DEFAULT_INFO_TRIGGERS = ("i", "我的信息", "名片")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PluginConfig:
    """群助手配置。所有属性都自带兜底，缺项不会抛异常。"""

    def __init__(self, raw: dict[str, Any] | None):
        self.raw = raw or {}

    # ---------- 基础 ----------
    @property
    def enabled_group_ids(self) -> set[str]:
        values = self.raw.get("enabled_group_ids") or []
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        return {str(item).strip() for item in values if str(item).strip()}

    def is_group_enabled(self, group_id: Any) -> bool:
        if not group_id:
            return False
        enabled = self.enabled_group_ids
        # 留空 = 所有群生效，方便第一次上手测试。
        return not enabled or str(group_id) in enabled

    @property
    def currency_name(self) -> str:
        return str(self.raw.get("currency_name") or "额度").strip() or "额度"

    @property
    def count_command_messages(self) -> bool:
        return bool(self.raw.get("count_command_messages", False))

    # ---------- 数据保留 ----------
    @property
    def log_retention_days(self) -> int:
        """额度流水 / 操作日志保留天数，0 = 永久保留。"""
        return max(0, _as_int(self.raw.get("log_retention_days"), 180))

    @property
    def stats_retention_days(self) -> int:
        """按天发言统计保留天数，0 = 永久保留。累计数不受影响。"""
        return max(0, _as_int(self.raw.get("stats_retention_days"), 730))

    # ---------- 额度 ----------
    @property
    def welcome_quota(self) -> int:
        return max(0, _as_int(self.raw.get("welcome_quota"), 5))

    @property
    def rename_cost(self) -> int:
        return max(0, _as_int(self.raw.get("rename_cost"), 1))

    # ---------- 抽奖 ----------
    @property
    def draw_per(self) -> int:
        return max(1, _as_int(self.raw.get("draw_per"), 100))

    @property
    def draw_min(self) -> int:
        return max(0, _as_int(self.raw.get("draw_min"), 1))

    @property
    def draw_max(self) -> int:
        # 保证上限不低于下限，配置写反了也不会炸。
        return max(self.draw_min, _as_int(self.raw.get("draw_max"), 5))

    @property
    def draw_bonus_chance(self) -> float:
        return min(1.0, max(0.0, _as_float(self.raw.get("draw_bonus_chance"), 0.1)))

    @property
    def draw_bonus_amount(self) -> int:
        return max(0, _as_int(self.raw.get("draw_bonus_amount"), 8))

    @property
    def notify_draw_ready(self) -> bool:
        # 默认关闭。攒满一次抽奖就在群里喊一句，200 人的活跃群一天能喊几十次，
        # 这是纯噪音 —— 而且「额度」「今日」里本来就写着抽奖进度，信息不会丢。
        # 想要热闹的群自己开。
        return bool(self.raw.get("notify_draw_ready", False))

    # ---------- 交互 ----------
    @property
    def info_triggers(self) -> set[str]:
        raw = self.raw.get("info_triggers")
        if raw is None or str(raw).strip() == "":
            return set(DEFAULT_INFO_TRIGGERS)
        items = {item.strip().lower() for item in str(raw).replace("，", ",").split(",")}
        items.discard("")
        return items or set(DEFAULT_INFO_TRIGGERS)

    @property
    def help_style(self) -> str:
        value = str(self.raw.get("help_style") or "forward").strip().lower()
        return value if value in {"forward", "text"} else "forward"

    # ---------- 信息卡 ----------
    @property
    def card_bg_path(self) -> str:
        return str(self.raw.get("card_bg_path") or "assets/card_bg.png").strip()

    @property
    def card_font_path(self) -> str:
        return str(self.raw.get("card_font_path") or "assets/fonts/LXGWWenKai-Medium.ttf").strip()

    @property
    def card_show_qq(self) -> bool:
        return bool(self.raw.get("card_show_qq", False))

    @property
    def card_show_avatar(self) -> bool:
        return bool(self.raw.get("card_show_avatar", True))

    # ---------- 权限 ----------
    @property
    def owner_default_level(self) -> int:
        return min(4, max(1, _as_int(self.raw.get("owner_default_level"), 4)))

    @property
    def admin_default_level(self) -> int:
        return min(4, max(1, _as_int(self.raw.get("admin_default_level"), 3)))
