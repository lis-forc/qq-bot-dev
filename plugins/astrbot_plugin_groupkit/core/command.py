"""命令注册表的骨架：数据结构 + 匹配器 + 帮助条目。

为什么要有这一层：
- `main.py` 不再随功能增长而变长。加功能 = 在 domains/ 里写处理函数，
  再去 registry.py 登记一行，入口文件一个字都不用改。
- 权限等级声明在命令上（`level`），校验在分发时统一做一遍。
  处理函数里不再重复写「require + 报错 + return」这几行。
- 帮助菜单从注册表生成。新命令只要登记，菜单和权限页自动跟着长，
  不会再出现「功能做了但没人知道怎么用」。

匹配器统一签名：(ctx, text, lower) -> Match | None。
返回 None 表示「这条命令不接这句话」，返回 Match 表示命中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Sequence

from ..utils.router import drop_prefix
from .services import Permission


@dataclass
class Match:
    """一次口令命中的结果。"""

    args: str = ""   # 去掉口令后的剩余部分，可能是空字符串（命中了但没带参数）
    value: str = ""  # 参数里抠出来的有效载荷，比如 `i 123456` 的号码


@dataclass
class HelpEntry:
    """帮助菜单里的一条。

    同一个命令可以有多条（比如 `查` 有两种用法各占一行），
    也可以声明在管理页（`page="admin"`）里单独出现。
    """

    usage: str = ""
    desc: str = ""
    page: str = "base"       # base（基础功能）| fun（娱乐功能）| admin
    level: int = 1           # 只在 page="admin" 时用来分组
    order: int = 0           # 页内排序，与注册顺序解耦（帮助的排序未必等于匹配顺序）
    slot: str = ""           # 相邻且同 slot 的条目合并成一行：额度 / 抽奖 / 今日
    group: str = ""          # 图片菜单里的分块标题；同 group 落进同一张卡片


@dataclass
class Command:
    """一条命令。"""

    key: str
    matcher: Callable[[Any, str, str], Match | None]
    handler: Callable[[Any, "CommandEnv"], AsyncGenerator]
    level: int = 1
    help: tuple[HelpEntry, ...] = ()
    perm_short: str = ""     # 权限页里的简称；留空 = 不在权限页展示
    perm_extra: tuple[tuple[int, str], ...] = ()  # 额外出现在某等级的简称
    # 这条命令现在算不算「存在」。None = 永远存在。
    #
    # 和 matcher 自己检查配置的区别：那种写法能让口令失效，但菜单里仍然
    # 列着它 —— 一个用不了的口令挂在菜单上，比没有这个功能更让人困惑。
    # 所以开关写成这个回调，匹配和菜单都读同一处判断。
    enabled: Callable[[Any], bool] | None = None


@dataclass
class CommandEnv:
    """一次口令执行的上下文，处理函数只认这一个参数。"""

    event: Any
    group_id: str
    sender_id: str
    member: dict
    text: str
    args: str = ""
    command: Command | None = None


# ---------------------------------------------------------------- 匹配器工厂
def exact(words: Sequence[str]) -> Callable[[Any, str, str], Match | None]:
    """整句相等。用于「额度」「抽奖」这种不该出现在正常聊天里的短语。"""
    pool = {w.lower() for w in words}

    def matcher(ctx, text: str, lower: str) -> Match | None:
        return Match() if lower in pool else None

    return matcher


def prefix(words: Sequence[str]) -> Callable[[Any, str, str], Match | None]:
    """前缀命中，剩余部分作为参数。`禁言 @某人 10分钟` → args="@某人 10分钟"。"""
    words = tuple(words)

    def matcher(ctx, text: str, lower: str) -> Match | None:
        args = drop_prefix(text, words)
        return None if args is None else Match(args=args)

    return matcher


def exact_or_mention(words: Sequence[str]) -> Callable[[Any, str, str], Match | None]:
    """整句相等，**或者**「口令 + 一个 @ / QQ 号」。

    为什么不能直接用 ``exact``：`额度` 用 exact 时，`额度 @某人` 匹配不上 ——
    群友看到的是「没反应」，还以为机器人挂了。这个坑踩过一次：三个
    「看别人」的命令全都没反应，因为它们都在用 exact。

    为什么不能改用 ``prefix``：`额度怎么涨`、`背包满了怎么办` 会被当成命令
    吃掉，群友的提问再也得不到回答。

    折中是只放行两种后缀：以 ``@`` 开头（看别人），或者是纯数字（QQ 号）。
    """
    pool = tuple(word.lower() for word in words)

    def matcher(ctx, text: str, lower: str) -> Match | None:
        cleaned = lower.strip()
        if cleaned in pool:
            return Match()
        for word in pool:
            if not cleaned.startswith(word):
                continue
            rest = cleaned[len(word):].strip()
            if rest.startswith("@") or rest.isdigit():
                return Match()
        return None

    return matcher


def regex(pattern: str, group: int = 1) -> Callable[[Any, str, str], Match | None]:
    """正则全匹配。用于 `i 123456` 这种「口令 + 纯数字」的形式。"""
    compiled = re.compile(pattern)

    def matcher(ctx, text: str, lower: str) -> Match | None:
        found = compiled.fullmatch(lower)
        if not found:
            return None
        value = found.group(group) or ""
        return Match(args=value, value=value)

    return matcher


def custom(fn: Callable[[Any, str, str], Match | None]) -> Callable[[Any, str, str], Match | None]:
    """自己写判定。需要读配置（比如 `info_triggers` 可变）时用这个。"""
    return fn


async def require_level(ctx, env: "CommandEnv", level: int) -> str | None:
    """统一的权限校验。通过返回 None，不通过返回要发给群里的那句话。

    绝大多数命令的等级声明在 Command.level 上，由分发器代劳；
    只有「同一个命令在某些用法下要更高权限」时才需要自己调
    （比如 `改名`：改自己免费，改别人要 2 级）。
    """
    ok, _ = await ctx.permission.require(
        env.event, env.group_id, env.sender_id, env.member, level
    )
    return None if ok else f"⚠️ 需要「{Permission.level_name(level)}」权限"
