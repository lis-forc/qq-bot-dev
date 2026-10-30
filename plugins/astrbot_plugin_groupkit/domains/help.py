"""口令清单：只列口令，不列解释。

改版思路（第四版）
------------------
前三版分别是：手写三段文字 → 从注册表生成 → 每页渲染成一张图。
这版回到文字，而且**把每条口令后面的解释全删了**，理由是：

- 解释是写给「第一次看菜单的人」的，但真正会翻菜单的人是「已经知道要什么、
  只是想确认怎么拼」的人。他需要的是能直接抄的字符串。
- 一条一行、行首缩进两格之后，20 多条口令只占半屏，比「口令 + 解释」
  那种一行挤两段话的排版好扫得多。
- 解释删了之后，**菜单本身就成了唯一的用法说明书**，所以每一条的写法
  必须是「照着发就能用」的完整形式（带参数的那种要带占位名）。

排版靠三样东西：emoji 当分组记号、`=` 当分界、缩进当层次。

    🔥 群助手 · 日常口令
    ==========================
    📅 签到
      签到
      签到记录
      ...

为什么 emoji 只出现在行首、分界只用 `=`：`utils/text.py` 的 `table()`
按显示宽度补空格对齐，emoji 的实际宽度由客户端字体决定，进表格会把列推歪。
这一版没有表格了，但规矩保留 —— 将来谁想在这里加表格，不会踩到同一个坑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain

from ..core.command import HelpEntry
from ..core.context import PluginContext
from ..core.services import Permission
from ..utils.forward import build_forward
from ..utils import deco
from ..utils.meta import read_meta

# 分界线的长度。QQ 手机端一屏大约 20 个汉字宽，26 个 `=` 刚好比标题长一点，
# 看起来是「一条线」而不是「一段横线」。
RULE = "=" * 26

# 每一条口令前面的缩进。两个半角空格 = 一个汉字宽，层级刚好看得出来。
INDENT = "  "

# 每一组前面挂哪个表情。键是 HelpEntry.group 的值。
GROUP_MARK = {
    "签到": "checkin",
    "我的信息": "profile",
    "额度与统计": "quota",
    "商店": "shop",
    "资料": "edit",
    "菜单": "titles",
    "做一张表情": "meme",
    # 娱乐页两块都用同一个记号会显得像复制粘贴，找模板换成「翻查」那个脸。
    "找模板": "search",
    # 假装消息、红包这些「玩一下」的功能，统一挂个爱心。
    "玩一玩": "relations",
    "排行": "stats",
}

# 分块顺序。不在表里的组排在后面，所以新加组不用改这里也能显示。
GROUP_ORDER = {
    "base": ["签到", "我的信息", "额度与统计", "排行", "商店", "资料", "菜单"],
    "fun": ["做一张表情", "找模板", "玩一玩"],
    "admin": [],
}

# 每一块的页头：记号 / 标题。
BLOCK_HEAD = {
    "base": ("base", "日常口令"),
    "fun": ("fun", "娱乐口令"),
    "admin": ("admin", "管理口令"),
}

# 页脚只留一行「怎么用」。多一行就变成解释了，而解释是这一版要删的东西。
BLOCK_FOOT = {
    "base": "零前缀，直接说",
    "fun": "表情只是开头，更多玩法陆续上",
    # 这句必须指到一个真的能看到完整规则的地方。之前写「看管理台」，
    # 而管理台上根本没有口令表 —— 等于把群主支到一个空房间。
    "admin": "群主 4 级 · 管理员 3 级 · 完整权限表在控制台「口令」页",
}

# 娱乐页末尾的占位块：告诉人「这里还有东西没上线」，
# 比留一片空白好 —— 空白会被读成「就这么点功能」。
# 现在「玩一玩」组已经有内容了，占位块退场：留着它等于对着一屏口令
# 再说一句「敬请期待」，反而显得空。
BLOCK_TAIL: dict[str, tuple[str, str]] = {}

# 一条文字消息能舒服读完的行数。超过就走合并转发 ——
# 不是「发不出去」，而是「铺满屏幕没人读」。
SINGLE_MESSAGE_LINES = 26


def _commands():
    """延迟导入注册表。

    registry 要 import 本模块来构造「帮助」这条命令，本模块又要读
    registry 的命令列表 —— 顶层互相 import 会成环。函数内导入解开这个环，
    而且帮助是低频操作，这点开销可以忽略。
    """
    from .registry import COMMANDS

    return COMMANDS


# ---------------------------------------------------------------- 文案填充
def _tokens(ctx: PluginContext) -> dict[str, Any]:
    """菜单文案里可以引用的配置值。改配置，菜单里的数字跟着变。"""
    return {
        "cur": ctx.config.currency_name,
        "rename_cost": ctx.config.rename_cost,
        "draw_per": ctx.config.draw_per,
        "welcome_quota": ctx.config.welcome_quota,
        "skin_price": ctx.config.card_skin_price,
    }


def _fill(text: str, tokens: dict[str, Any]) -> str:
    """把 {cur} 这类占位符换成实际值。缺 key 就原样留着，不要抛异常。"""
    if not text:
        return text
    try:
        return text.format_map(tokens)
    except (KeyError, IndexError, ValueError):
        return text


def _active(cmd: Any, ctx: PluginContext) -> bool:
    """这条命令现在算不算存在。开关关掉的功能不出现在菜单里。"""
    hook = getattr(cmd, "enabled", None)
    if hook is None:
        return True
    try:
        return bool(hook(ctx))
    except Exception:  # 判断本身出错时宁可不显示，也别显示一个用不了的
        return False


def _entries(ctx: PluginContext, page: str, level: int = 4) -> list[HelpEntry]:
    """取出某一页的条目，按 order 排序（与匹配顺序解耦）。

    ``level`` 是**看菜单这个人的等级**：每条命令可以声明自己需要几级
    （``HelpEntry.level``），超过就不出现在他的菜单里。所以普通群员发
    「帮助」看不到「额度排行」「禁言」，而群主看到的是完整一份 ——
    菜单本身就是「你能做什么」的答案，不该列出一堆点了会报权限不足的条目。
    """
    collected = [
        entry
        for cmd in _commands()
        if _active(cmd, ctx)
        for entry in cmd.help
        if entry.page == page and entry.level <= level
    ]
    return sorted(collected, key=lambda entry: entry.order)


def _usages(entries: list[HelpEntry], tokens: dict[str, Any]) -> list[str]:
    """组内每一条的用法，一行一个。空用法的条目（纯占位）直接跳过。"""
    out: list[str] = []
    for entry in entries:
        usage = _fill(entry.usage, tokens).strip()
        if usage:
            out.append(INDENT + usage)
    return out


# ---------------------------------------------------------------- 组块
@dataclass
class Block:
    """一块口令：一个标题 + 一段可以直接发出去的文字。"""

    key: str
    title: str
    text: str


def _admin_body(ctx: PluginContext, tokens: dict[str, Any], level: int = 4) -> str:
    """管理块：一个权限等级一组。

    等级本身就是最好的分块依据 —— 看管理菜单的人想问的第一个问题
    永远是「我能不能用」，而不是「这个功能属于哪一类」。
    所以高于自己等级的那些组整块不出现，不是留着让人试。
    """
    entries = [entry for entry in _entries(ctx, "admin", level) if entry.usage]
    if not entries:
        return ""

    chunks: list[str] = []
    for at in sorted({entry.level for entry in entries}):
        rows = _usages([entry for entry in entries if entry.level == at], tokens)
        if not rows:
            continue
        chunks.append("\n".join([f"〔{at} 级 · {Permission.level_name(at)}〕", *rows]))
    return "\n\n".join(chunks)


def _generic_body(
    ctx: PluginContext, which: str, tokens: dict[str, Any], level: int = 4
) -> str:
    """base / fun：按 group 分块，组内按 order 排。"""
    buckets: dict[str, list[HelpEntry]] = {}
    for entry in _entries(ctx, which, level):
        if not entry.usage:
            continue
        buckets.setdefault(entry.group or "其它", []).append(entry)

    preferred = GROUP_ORDER.get(which, [])
    ordered = [name for name in preferred if name in buckets]
    ordered += [name for name in buckets if name not in preferred]

    chunks: list[str] = []
    for name in ordered:
        rows = _usages(sorted(buckets[name], key=lambda entry: entry.order), tokens)
        if not rows:
            continue
        head = deco.head(name, GROUP_MARK.get(name, "base"))
        chunks.append("\n".join([head, *rows]))
    return "\n\n".join(chunks)


def make_block(ctx: PluginContext, which: str, level: int = 4) -> Block | None:
    """组装一块。没内容返回 None —— 调用方据此跳过空块。

    ``level`` 默认 4（完整一份），因为自测和「关于」类调用没有具体的人；
    真正发给群友时必须传调用者的等级。
    """
    tokens = _tokens(ctx)
    body = (
        _admin_body(ctx, tokens, level)
        if which == "admin"
        else _generic_body(ctx, which, tokens, level)
    )
    if not body:
        return None

    mark_key, title = BLOCK_HEAD[which]
    brand = ctx.gallery_brand
    head = f"{deco.mark(mark_key)} {brand} · {title}\n{RULE}"

    parts = [head, body]
    tail = BLOCK_TAIL.get(which) if which != "admin" else None
    if tail:
        parts.append(f"{tail[0]}\n{tail[1]}")
    foot = BLOCK_FOOT.get(which, "")
    if foot:
        parts.append(RULE)
        parts.append(foot)
    return Block(key=which, title=f"{brand} · {title}", text="\n\n".join(parts))


async def _viewer_level(ctx: PluginContext, event: Any) -> int:
    """看菜单这个人是几级。查不出来就按 1 级算 —— 少显示几条，不会误导。"""
    try:
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        if not group_id or not sender_id:
            return 1
        member = ctx.db.get_member(group_id, sender_id)
        return await ctx.permission.level(event, group_id, sender_id, member)
    except Exception as exc:  # 权限接口挂了不该让「帮助」打不开
        logger.warning(f"[群助手] 读菜单查看者等级失败，按 1 级处理: {exc}")
        return 1


async def send_menu(ctx: PluginContext, event: Any, which: str = "base"):
    """发口令清单。长就打包成一条聊天记录，短就直接发。

    为什么坚持「多块才转发」：只有一块的时候套转发是多余的 ——
    群友要多点一下才能看见唯一一屏字，反而更麻烦。
    """
    level = await _viewer_level(ctx, event)
    picked = ["base", "fun"] if which == "all" else [which]
    blocks = [block for block in (make_block(ctx, key, level) for key in picked) if block]
    if not blocks:
        yield event.plain_result("这类口令暂时没有，发「帮助」看看别的")
        return

    joined = "\n\n".join(block.text for block in blocks)
    if which == "all":
        joined += f"\n\n{deco.mark('admin')} 管理口令：发「管理」"

    if joined.count("\n") + 1 <= SINGLE_MESSAGE_LINES:
        yield event.plain_result(joined)
        return

    if ctx.config.help_style == "forward":
        items = [(block.title, [Plain(block.text)]) for block in blocks]
        if which == "all":
            items.append(
                (f"{ctx.gallery_brand} · 还有", [Plain("管理口令：发「管理」")])
            )
        chain = build_forward(event, items)
        if chain:
            yield event.chain_result(chain)
            return
        logger.warning("[群助手] 口令清单转发构造失败，改用长文本")

    yield event.plain_result(joined)


async def send_help(ctx: PluginContext, event: Any):
    """默认发全部口令。"""
    async for reply in send_menu(ctx, event, "all"):
        yield reply


def pages(ctx: PluginContext) -> list[tuple[str, str]]:
    """三块纯文字，(标题, 正文)。自测直接断言文案内容，不用起机器人。"""
    return [
        (block.title, block.text)
        for block in (make_block(ctx, key) for key in ("base", "fun", "admin"))
        if block is not None
    ]


def version(ctx: PluginContext) -> str:
    """从 metadata.yaml 读版本号。留着给「关于」类文案用。"""
    return read_meta(ctx.plugin_dir, "version")
