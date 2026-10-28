"""配置的元信息、校验与写回。

管理台要能改「全部」配置。但配置项的定义在 ``_conf_schema.json`` 里，
如果前端再手抄一份字段表，两边必然会漂 —— 加了配置项忘了改前端，
那一项在页面上就永远不出现，而用户会以为「配置就这么多」。

所以这个模块只做三件事，且**不重复写任何字面量**：

1. 从 schema 读类型 / 标签 / 提示 / 滑杆范围；
2. ``SECTIONS`` 只决定「哪个字段归到哪一页、按什么顺序」，标题用表情起头；
3. ``apply()`` 负责值域收敛，并把**写回后实际生效的值**读出来返回 ——
   避免页面上显示 999、运行时被夹到 365 这种两套数字。

``missing_keys()`` 是给自测用的硬约束：schema 里每一个键都必须被某个
分组认领，否则就是「页面上改不到」。这条断言让「加配置忘了登记」在
自测阶段就红，而不是等用户来问。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..utils import deco
from .config import PluginConfig

# 分组：(key, 标题, 说明, 字段列表)
#
# 标题的表情和聊天里的那套共用同一张记号表（见 utils/deco.py）——
# 只写 deco.mark(...)，不写字面量，加一个分组不会漏掉记号，也不会两处不一致。
SECTIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "basic",
        f"{deco.mark('base')} 基础",
        "哪些群生效、额度叫什么名字。留空群号表示所有群都生效。",
        ("enabled_group_ids", "currency_name", "count_command_messages"),
    ),
    (
        "join",
        f"{deco.mark('join')} 入群",
        "新人进来先说一句，要不要答题。两个开关独立，可以只开一个。",
        (
            "welcome_enabled",
            "welcome_text",
            "welcome_quota",
            "verify_enabled",
            "verify_question",
            "verify_answers",
            "verify_timeout_minutes",
        ),
    ),
    (
        "quota",
        f"{deco.mark('quota')} 额度与抽奖",
        "每 100 条发言攒一次抽奖机会，抽到的数值在这个区间里。券价是按区间自动算的，所以没有单独的价格项。"
        "改名默认不自动扣费——要收钱由管理员手动扣。",
        (
            "rename_cost",
            "freeman_price",
            "title_price",
            "draw_per",
            "draw_min",
            "draw_max",
            "draw_bonus_chance",
            "draw_bonus_amount",
            "notify_draw_ready",
        ),
    ),
    (
        "nick",
        f"{deco.mark('nick')} 昵称与记号",
        "群名片由机器人管：最前面挂性别记号，名字被人手改掉会自动改回来。"
        "名字只认 2~4 个字、不含数字和符号——买了「自由人」的人不受这条限制。",
        (
            "nick_enabled",
            "nick_emoji_male",
            "nick_emoji_female",
            "nick_emoji_none",
            "nick_separator",
            "nick_min_len",
            "nick_max_len",
            "nick_allow_digits",
            "nick_force",
            "nick_join_text",
            "nick_nag_text",
            "nick_nag_limit",
            "nick_nag_mute_minutes",
        ),
    ),
    (
        "checkin",
        f"{deco.mark('checkin')} 签到",
        "同一天同一人永远是区间里同一个数，不是每次随机。连签默认不加钱，只影响称号和里程碑。",
        (
            "checkin_enabled",
            "checkin_image",
            "checkin_base_min",
            "checkin_base_max",
            "checkin_streak_bonus",
            "checkin_streak_cap",
            "checkin_double_price",
            "checkin_double_limit",
            "checkin_ticket_limit",
            "checkin_bot_name",
        ),
    ),
    (
        "card",
        f"{deco.mark('profile')} 信息卡与名片",
        "卡上排的是姓名 / 性别 / 爱好 / 额度 / 注册天数 / 关系。cards 里的「男.png」「女.png」"
        "是基础名片，按性别自动发，不上架；其余图片才是商店里的商品，文件名即商品名。",
        (
            "card_font_path",
            "image_root",
            "card_skin_price",
        ),
    ),
    (
        "meme",
        f"{deco.mark('meme')} 表情包",
        "插件自己不画图，图是外面那个 meme-generator 画的。地址填错的表现是「做了半天没反应」。",
        (
            "meme_api_base_url",
            "meme_cost",
            "meme_timeout",
            "meme_max_image_mb",
            "meme_auto_avatar",
            "meme_auto_texts",
            "meme_catalog_size",
        ),
    ),
    (
        "panel",
        f"{deco.mark('admin')} 面板与群管",
        "权限等级：4 = 只认群主，3 = 群主和管理员，2 及以下不建议开放。",
        (
            "owner_default_level",
            "admin_default_level",
            "default_mute_minutes",
            "rank_message_enabled",
            "info_triggers",
            "help_style",
        ),
    ),
    (
        "data",
        f"{deco.mark('data')} 数据保留",
        "每天最多自动清理一次。「累计发言」存在成员表里，不受这两个值影响。",
        ("log_retention_days", "stats_retention_days"),
    ),
)

# 这些字段的内容会换行，页面上给多行输入框而不是单行。
MULTILINE = {"welcome_text", "verify_question", "nick_join_text", "nick_nag_text"}

# 这些字段的**首尾空白本身是内容**，写回时不能 strip。
#
# 目前只有 `nick_separator`：它的默认值就是一个空格。strip 一次就变成空串，
# 群名片跟着从「🦊 张三」变成「🦊张三」—— 而管理员什么都没改，只是在面板上
# 点了保存。这类「保存即损坏」是最难查的一种，因为改动的不是他动过的那项。
KEEP_SPACE = {"nick_separator"}

# 同一组里互相有大小关系的两列。写反了运行时会被 `max(min, ...)` 悄悄纠正，
# 页面上却还显示写反的数字 —— 所以写回后要显式拉平并把原因带回去。
PAIRS = (
    ("checkin_base_min", "checkin_base_max"),
    ("draw_min", "draw_max"),
)

# 概率型字段：schema 的 slider 已经写了 0..1，这里再兜一次，
# 防止有人绕开滑杆直接 POST 一个 5。
RATIOS = {"draw_bonus_chance"}

_SCHEMA_FILE = "_conf_schema.json"


def load_schema(plugin_dir: Path | str) -> dict[str, dict[str, Any]]:
    """读 ``_conf_schema.json``。读不到就返回空字典，页面退化成「没有可改的项」。"""
    path = Path(plugin_dir) / _SCHEMA_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 描述
def _kind(meta: dict[str, Any]) -> str:
    """把 schema 的 type 收敛成前端认识的几种控件类型。"""
    raw = str(meta.get("type") or "string").lower()
    if raw == "bool":
        return "bool"
    if raw in {"int", "float"}:
        return "number"
    if raw == "list":
        return "list"
    if meta.get("options"):
        return "select"
    return "text"


def _field(key: str, meta: dict[str, Any], value: Any) -> dict[str, Any]:
    kind = _kind(meta)
    field: dict[str, Any] = {
        "key": key,
        "label": str(meta.get("description") or key),
        "kind": kind,
        "value": value,
        "default": meta.get("default"),
        "hint": str(meta.get("hint") or ""),
        "wide": kind == "text" and key in MULTILINE,
    }
    slider = meta.get("slider")
    if kind == "number" and isinstance(slider, dict):
        field["slider"] = {
            "min": slider.get("min", 0),
            "max": slider.get("max", 100),
            "step": slider.get("step", 1),
        }
        field["step"] = slider.get("step", 1)
    if kind == "select":
        options = list(meta.get("options") or [])
        labels = list(meta.get("labels") or [])
        field["options"] = [
            {
                "value": str(option),
                # 没给 labels 就把值本身当标签，不要拼一个「选项 1」出来
                "label": str(labels[index]) if index < len(labels) else str(option),
            }
            for index, option in enumerate(options)
        ]
    return field


def describe(raw: dict[str, Any], plugin_dir: Path | str) -> dict[str, Any]:
    """整页配置：分组 + 每组的字段 + 当前值。

    ``raw`` 直接给 ``PluginConfig.raw`` —— 用原字典而不是属性值，是因为
    属性会做各种收敛（默认值、夹取、旧值迁移）。页面上要显示「用户存了什么」，
    用户改了才有意义；想看他实际生效多少，``apply()`` 会把生效值带回来。
    """
    schema = load_schema(plugin_dir)
    sections = []
    for key, title, note, keys in SECTIONS:
        fields = []
        for field_key in keys:
            meta = schema.get(field_key)
            if not isinstance(meta, dict):
                # 字段登记了但 schema 里没有 —— 配置项被删了却没改这里。
                # 跳过而不是渲染一个空控件，页面不该出现「改不了的空行」。
                continue
            fields.append(_field(field_key, meta, raw.get(field_key, meta.get("default"))))
        sections.append({"key": key, "title": title, "note": note, "fields": fields})
    return {
        "sections": sections,
        "schema_keys": list(schema),
        "field_count": sum(len(section["fields"]) for section in sections),
    }


def missing_keys(plugin_dir: Path | str) -> list[str]:
    """schema 里有、但没有任何分组认领的键。自测断言它为空。"""
    claimed = {key for _, _, _, keys in SECTIONS for key in keys}
    return [key for key in load_schema(plugin_dir) if key not in claimed]


# ---------------------------------------------------------------- 写回
def clamp_number(meta: dict[str, Any], value: Any) -> Any:
    """按 schema 的 slider 夹取，再把 float 字段还成 float。"""
    is_float = str(meta.get("type") or "").lower() == "float"
    number = float(value) if is_float else int(value)
    slider = meta.get("slider")
    if isinstance(slider, dict):
        low, high = slider.get("min"), slider.get("max")
        if low is not None:
            number = max(float(low) if is_float else int(low), number)
        if high is not None:
            number = min(float(high) if is_float else int(high), number)
    return round(number, 4) if is_float else int(number)


def coerce(meta: dict[str, Any], value: Any, key: str = "") -> tuple[bool, Any, str]:
    """把一个前端传来的值收敛成配置里该有的类型。

    返回 ``(是否接受, 收敛后的值, 说明)``。说明只在被改动时非空 ——
    用户填 999 被夹到 365，要让他看见为什么。

    ``key`` 只用来查 ``KEEP_SPACE``（首尾空白有意义的字段），默认空串 ——
    单测里可以不传。
    """
    kind = _kind(meta)

    if kind == "bool":
        if isinstance(value, bool):
            return True, value, ""
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "是"}:
            return True, True, ""
        if text in {"0", "false", "no", "off", "否", ""}:
            return True, False, ""
        return False, None, f"{meta.get('description') or '该项'}要的是开关值"

    if kind == "number":
        try:
            number = clamp_number(meta, value)
        except (TypeError, ValueError):
            return False, None, f"{meta.get('description') or '该项'}要的是数字"
        raw_number = value
        try:
            normalized = float(raw_number)
        except (TypeError, ValueError):
            normalized = None
        if normalized is not None and abs(normalized - float(number)) > 1e-9:
            return True, number, f"已按允许范围调整到 {number}"
        return True, number, ""

    if kind == "select":
        options = [str(option) for option in (meta.get("options") or [])]
        text = str(value)
        if text in options:
            return True, text, ""
        return False, None, f"{meta.get('description') or '该项'}只能是 {'/'.join(options)}"

    if kind == "list":
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
        else:
            text = str(value or "").replace("，", ",").replace("、", ",")
            items = [chunk.strip() for chunk in text.replace("\n", ",").split(",")]
        return True, [item for item in items if item], ""

    # text：保留换行（欢迎语模板里会用到），只去掉首尾空白。
    # KEEP_SPACE 里的字段连空白都不动，理由见那个常量的注释。
    text = str(value if value is not None else "").replace("\r\n", "\n")
    if key in KEEP_SPACE:
        return True, text, ""
    return True, text.strip(), ""


def apply(
    raw: dict[str, Any], payload: dict[str, Any], plugin_dir: Path | str
) -> dict[str, Any]:
    """把页面上改的值写进 ``raw``（原地），返回改动与提示。

    **原地改**是有意的：``PluginConfig.raw`` 就是 AstrBot 注入的那个 dict，
    改它等于立刻改运行时行为，不需要重启。落盘由调用方走
    ``config.save_config()`` —— 那一步会失败，得由知道 context 的那一层处理。
    """
    schema = load_schema(plugin_dir)
    changed: list[str] = []
    notes: list[str] = []
    problems: list[str] = []

    for key, value in (payload or {}).items():
        meta = schema.get(key)
        if not isinstance(meta, dict):
            # 未知键不静默吞：可能是前端和 schema 版本不一致，
            # 说出来比「保存成功但其实没保存」好。
            problems.append(f"{key} 不是已知配置项，已忽略")
            continue
        ok, normalized, note = coerce(meta, value, key)
        if not ok:
            problems.append(note)
            continue
        if raw.get(key) != normalized:
            raw[key] = normalized
            changed.append(key)
        if note:
            notes.append(f"{meta.get('description') or key}：{note}")

    # 大小关系拉平。放在全部写入之后：一页里两个值可能同时被改，
    # 逐项校验的话先处理谁都会出现「通过了一个错的状态」。
    for low_key, high_key in PAIRS:
        try:
            low, high = raw.get(low_key), raw.get(high_key)
            if low is None or high is None:
                continue
            if int(high) < int(low):
                raw[high_key] = int(low)
                if high_key not in changed:
                    changed.append(high_key)
                notes.append(
                    f"{schema.get(high_key, {}).get('description') or high_key}"
                    f"：不能低于下限，已改成 {raw[high_key]}"
                )
        except (TypeError, ValueError):
            continue

    for key in RATIOS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fixed = min(1.0, max(0.0, float(value)))
            if abs(fixed - float(value)) > 1e-9:
                raw[key] = fixed
                if key not in changed:
                    changed.append(key)
                notes.append(f"{key}：概率只能是 0~1，已改成 {fixed}")

    return {
        "changed": changed,
        "notes": notes,
        "problems": problems,
        "values": effective_values(raw),
    }


def effective_values(raw: dict[str, Any]) -> dict[str, Any]:
    """写回之后**实际生效**的值。

    用 ``PluginConfig`` 的属性读出来，而不是回显 ``raw``：配置层会把
    「写反的上下限」「0 表示不限购」这类值收敛成运行时真正用的数字。
    页面上显示这套数字，用户才不会遇到「填了 0 结果还是限购 1 次」
    这种看不懂的事。
    """
    cfg = PluginConfig(raw)
    return {
        "enabled_group_ids": sorted(cfg.enabled_group_ids),
        "currency_name": cfg.currency_name,
        "welcome_enabled": cfg.welcome_enabled,
        "verify_enabled": cfg.verify_enabled,
        "verify_timeout_minutes": cfg.verify_timeout_minutes,
        "checkin_enabled": cfg.checkin_enabled,
        "checkin_base": [cfg.checkin_base_min, cfg.checkin_base_max],
        "draw_range": [cfg.draw_min, cfg.draw_max],
        "draw_bonus_chance": cfg.draw_bonus_chance,
        "nick_name_len": [cfg.nick_min_len, cfg.nick_max_len],
        "nick_marks": [
            cfg.nick_emoji_male or "（空）",
            cfg.nick_emoji_female or "（空）",
            cfg.nick_emoji_none or "（空）",
        ],
        "rank_message_enabled": cfg.rank_message_enabled,
        "log_retention_days": cfg.log_retention_days,
        "stats_retention_days": cfg.stats_retention_days,
    }
