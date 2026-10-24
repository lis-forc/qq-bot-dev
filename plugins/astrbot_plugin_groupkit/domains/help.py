"""帮助菜单：从命令注册表生成，不再手写。

这是这次重构最实际的收益 —— 新加一个功能，只要在 registry.py 里登记时
写了 help 条目，菜单和权限页就自动带上它。不会再出现「功能做了但
群里没人知道怎么用」。

排版规则（为了和原来的三段式菜单保持一致）：
- 有说明的条目 → `用法` + 换行缩进两格的说明；
- 没有说明的条目（管理页）→ 只列用法，紧凑排；
- 相邻且 slot 相同的条目合并成一行，用 ` / ` 连用法、`、` 连说明，
  于是「额度 / 抽奖 / 今日」自然合成一条。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Node, Nodes, Plain

from ..core.command import HelpEntry
from ..core.context import PluginContext
from ..core.services import Permission


def _commands():
    """延迟导入注册表。

    registry 要 import 本模块来构造「帮助」这条命令，本模块又要读
    registry 的命令列表 —— 顶层互相 import 会成环。函数内导入解开这个环，
    而且帮助是低频操作，这点开销可以忽略。
    """
    from .registry import COMMANDS

    return COMMANDS

# 权限页那段话是「权限模型」的说明，不是命令索引，所以固定手写。
PERM_FOOTER = (
    "QQ 群主默认 4 级，QQ 管理员默认 3 级。\n"
    "授权不看身份，只看等级：任何被授到 4 级的人都能继续授权别人。"
)

def _admin_footer(ctx: PluginContext) -> str:
    """管理页脚注。带配置值的写成函数，配置一改菜单跟着变。"""
    return (
        f"禁言默认单位是分钟，最长 30 天；不写时长默认 {ctx.config.default_mute_minutes} 分钟。\n"
        "禁言 @全体成员 等于全群禁言，需要群主权限。\n"
        "踢人加「拉黑」，对方就不能再申请入群。"
    )


def _tokens(ctx: PluginContext) -> dict[str, Any]:
    """帮助文案里可以引用的配置值。改配置，菜单里的数字跟着变。"""
    return {
        "cur": ctx.config.currency_name,
        "rename_cost": ctx.config.rename_cost,
        "draw_per": ctx.config.draw_per,
        "welcome_quota": ctx.config.welcome_quota,
    }


def _fill(text: str, tokens: dict[str, Any]) -> str:
    """把 {cur} 这类占位符换成实际值。缺 key 就原样留着，不要抛异常。"""
    if not text:
        return text
    try:
        return text.format_map(tokens)
    except (KeyError, IndexError, ValueError):
        return text


def _entries(page: str) -> list[HelpEntry]:
    """取出某一页的所有条目，按 order 排序（与匹配顺序解耦）。"""
    collected = [entry for cmd in _commands() for entry in cmd.help if entry.page == page]
    return sorted(collected, key=lambda entry: entry.order)


def _group(entries: list[HelpEntry]) -> list[list[HelpEntry]]:
    """把相邻且同 slot 的条目并成一组。slot 为空则各自成组。"""
    groups: list[list[HelpEntry]] = []
    for entry in entries:
        if (
            entry.slot
            and groups
            and groups[-1][0].slot == entry.slot
        ):
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def _render(entries: list[HelpEntry], tokens: dict[str, Any], compact_style: bool) -> str:
    blocks = []
    for group in _group(entries):
        usage = " / ".join(_fill(e.usage, tokens) for e in group if e.usage)
        if not usage:
            continue
        desc = "、".join(_fill(e.desc, tokens) for e in group if e.desc)
        if compact_style or not desc:
            blocks.append(usage)
        else:
            blocks.append(f"{usage}\n  {desc}")
    return "\n\n".join(blocks) if not compact_style else "\n".join(blocks)


def _base_page(ctx: PluginContext, tokens: dict[str, Any]) -> str:
    body = _render(_entries("base"), tokens, compact_style=False)
    footer = (
        f"新手礼：首次发言自动到账 {ctx.config.welcome_quota} {ctx.config.currency_name}；"
        f"每发言 {ctx.config.draw_per} 条攒一次抽奖。"
    )
    return f"🧩 群助手 · 基础\n\n{body}\n\n{footer}"


def _admin_page(ctx: PluginContext, tokens: dict[str, Any]) -> str:
    entries = _entries("admin")
    chunks = []
    for level in sorted({e.level for e in entries}):
        usages = _render([e for e in entries if e.level == level], tokens, compact_style=True)
        if not usages:
            continue
        chunks.append(f"【{level} 级 · {Permission.level_name(level)}】\n{usages}")
    body = "\n\n".join(chunks)
    return f"🛡 群助手 · 管理\n\n{body}\n\n{_admin_footer(ctx)}"


def _perm_page(ctx: PluginContext) -> str:
    """按等级列出各等级能用的功能，全部来自注册表。"""
    by_level: dict[int, list[str]] = {}
    # 两趟：先按命令顺序收 primary，再补 perm_extra（比如「代改名」要排在
    # 禁言/解禁之后，因为它其实是「改名」的一个高权限用法）。
    for cmd in _commands():
        if cmd.perm_short:
            by_level.setdefault(cmd.level, []).append(cmd.perm_short)
    for cmd in _commands():
        for level, short in cmd.perm_extra:
            by_level.setdefault(level, []).append(short)

    chunks = []
    for level in sorted(by_level):
        names = " / ".join(by_level[level])
        chunks.append(f"{level} {Permission.level_name(level)}\n  {names}")
    return f"📖 群助手 · 权限\n\n" + "\n\n".join(chunks) + f"\n\n{PERM_FOOTER}"


def pages(ctx: PluginContext) -> list[tuple[str, str]]:
    """三页菜单。返回 (标题, 正文)，标题用于合并转发的节点名。"""
    tokens = _tokens(ctx)
    return [
        ("群助手 · 基础", _base_page(ctx, tokens)),
        ("群助手 · 管理", _admin_page(ctx, tokens)),
        ("群助手 · 权限", _perm_page(ctx)),
    ]


def _forward_chain(event, items: list[tuple[str, str]]) -> list | None:
    """帮助打包成合并转发，点开才展开，避免刷屏。"""
    try:
        uin = str(event.get_self_id() or "10000")
        nodes = [Node(uin=uin, name=title, content=[Plain(body)]) for title, body in items]
        if len(nodes) == 1:
            return [nodes[0]]
        try:
            return [Nodes(nodes=nodes)]
        except TypeError:
            return [Nodes(nodes)]
    except Exception as exc:
        logger.error(f"[群助手] 构造合并转发失败，改用文字: {exc}")
        return None


async def send_help(ctx: PluginContext, event):
    """发帮助。合并转发失败就降级成三条普通消息。"""
    items = pages(ctx)
    if ctx.config.help_style == "forward":
        chain = _forward_chain(event, items)
        if chain:
            yield event.chain_result(chain)
            return
    for _, body in items:
        yield event.plain_result(body)
