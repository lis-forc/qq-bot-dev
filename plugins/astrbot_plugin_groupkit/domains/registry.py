"""命令注册表：整个插件「有哪些功能」的唯一清单。

两个顺序必须分开看，混在一起会出 bug：
- **列表顺序 = 匹配顺序**。谁先命中谁生效，所以「全群禁言」必须排在
  「禁言」前面、「解禁」排在「禁言」前面。
- **HelpEntry.order = 帮助里的顺序**。菜单排序未必等于匹配顺序
  （比如「帮助」是第一个写的功能，但菜单里必须排最后）。

加功能的完整动作：
1. 在 domains/ 里写个 `xxx.py`，导出 `COMMANDS = [Command(...)]`；
2. 把它的 COMMANDS 展开进下面这张表，注意放在正确的匹配位置；
3. 跑 `python tools/selftest.py`。会有一条断言盯着「管理命令必须声明等级」，
   忘记写 level 的话权限就是 1 级，等于把管理功能开放给所有人。
"""

from __future__ import annotations

from typing import AsyncGenerator

from ..core.command import Command, CommandEnv, HelpEntry, exact, require_level
from . import (
    admin,
    bag,
    checkin,
    fake,
    help as help_domain,
    monopoly,
    profile,
    quota,
    rank,
    redpacket,
    shop,
    stats,
    sticker,
)


async def _page_handler(ctx, env: CommandEnv, which: str) -> AsyncGenerator:
    async for reply in help_domain.send_menu(ctx, env.event, which):
        yield reply


async def _help_handler(ctx, env: CommandEnv) -> AsyncGenerator:
    """「帮助」= 日常 + 娱乐一整份，不再只给基础页。

    以前分三页，是因为一页放不下；现在文字排得紧，两页并成一条正好,
    而且少一次「我该发哪个口令」的犹豫。
    """
    async for reply in _page_handler(ctx, env, "all"):
        yield reply


async def _fun_handler(ctx, env: CommandEnv) -> AsyncGenerator:
    async for reply in _page_handler(ctx, env, "fun"):
        yield reply


async def _admin_page_handler(ctx, env: CommandEnv) -> AsyncGenerator:
    async for reply in _page_handler(ctx, env, "admin"):
        yield reply


def _help_command() -> Command:
    return Command(
        key="help",
        matcher=exact(("帮助", "口令", "菜单", "功能", "指令", "help", "menu")),
        handler=_help_handler,
        help=(HelpEntry("帮助", "全部口令，一条消息看全", page="base", order=90, group="菜单"),),
    )


def _fun_command() -> Command:
    """表情包那一页。单独一个口令是因为基础页塞不下，
    而且想玩的人不需要先看完额度、统计这些。"""
    return Command(
        key="fun_menu",
        matcher=exact(("娱乐", "娱乐功能", "玩什么", "娱乐菜单")),
        handler=_fun_handler,
        help=(HelpEntry("娱乐", "只看表情包那一块", page="base", order=91, group="菜单"),),
    )


def _admin_page_command() -> Command:
    return Command(
        key="admin_menu",
        matcher=exact(("管理", "管理功能", "群管菜单", "管理口令")),
        handler=_admin_page_handler,
        perm_short="管理菜单",
        # 放在基础页的「菜单」组里，说明写清楚「要权限」——
        # 普通群员点开只会看到自己那几行，不会困惑。
        help=(HelpEntry("管理", "只看群管那一块，按你的等级列出", page="base", order=92, group="菜单"),),
    )


# ---------------------------------------------------------------------------
# 匹配顺序（从上到下，先命中先用）：
#   信息卡 → 帮助/娱乐/管理 三个菜单 → 改名 → 爱好 → 性别 → 名片/换名片
#   → 额度/抽奖/今日/额度记录 → 签到/签到记录/称号/佩戴 → 额度排行/签到排行
#   → 商店/购买 → 表情列表/搜表情/表情详情/表情
#   → 全群禁言/全群解禁 → 解禁 → 禁言 → 踢人 → 发额度/扣额度 → 授权
# ---------------------------------------------------------------------------
COMMANDS: list[Command] = [
    profile.COMMANDS[0],      # info
    _help_command(),          # 帮助（基础页）
    _fun_command(),           # 娱乐（表情页）
    _admin_page_command(),    # 管理（按等级分块）
    profile.COMMANDS[1],      # rename
    profile.COMMANDS[2],      # hobby
    profile.COMMANDS[3],      # sex
    profile.COMMANDS[4],      # my_card（名片）
    profile.COMMANDS[5],      # wear_card（换名片）
    quota.COMMANDS[0],        # 额度
    quota.COMMANDS[1],        # 抽奖
    stats.COMMANDS[0],        # 今日
    quota.COMMANDS[2],        # 额度记录
    # 「给额度」是**普通群员**之间转账（扣自己的，对方收 80%），
    # 必须排在「发额度 / 扣额度」之前：那两条是管理命令，靠前缀匹配，
    # 而这是 `给额度`/`转额度`/`转账` 三个前缀，两者不会撞。
    # 之前漏了注册 —— 命令写好了、帮助条目也写了，但群里发出去没人接，
    # 表现就是「这个指令我不知道在哪」。注册表漏一条不会有任何报错。
    quota.COMMANDS[3],        # 给额度
    *checkin.COMMANDS,        # 签到（含记录 / 排行）
    *bag.COMMANDS,            # 背包 / 头衔 / 关系（「头衔」前缀要在通用口令之前）
    *rank.COMMANDS,           # 额度排行
    *shop.COMMANDS,           # 商店 / 购买（「兑换」整句归商店，带参数归购买）
    *sticker.COMMANDS,        # 表情（列表/搜索/详情 排在「表情」前缀之前）
    *fake.COMMANDS,           # 假装消息（「假装」前缀，和表情不冲突）
    *redpacket.COMMANDS,      # 红包（「发红包」必须在「红包」前缀之前，列表里已排好）
    *monopoly.COMMANDS,       # 大富翁（两条都是 exact，顺序不影响匹配）
    *admin.COMMANDS,          # 群管（内部已按「全群在前」的顺序排好）
]


def _validate() -> None:
    """导入时自检：key 重复会让后一条永远不生效，且完全不报错。"""
    seen: set[str] = set()
    for cmd in COMMANDS:
        if cmd.key in seen:
            raise RuntimeError(f"[群助手] 命令 key 重复：{cmd.key}")
        seen.add(cmd.key)
        if cmd.matcher is None or cmd.handler is None:
            raise RuntimeError(f"[群助手] 命令 {cmd.key} 缺少 matcher 或 handler")


_validate()


async def dispatch(ctx, env: CommandEnv) -> AsyncGenerator:
    """把一句话派发给第一个命中的命令。

    权限在这里统一校验：命令声明了 level > 1 就先查权限，
    处理函数里不再重复写「require + 报错 + return」。
    """
    lower = env.text.lower()
    for cmd in COMMANDS:
        match = cmd.matcher(ctx, env.text, lower)
        if match is None:
            continue
        env.args = match.args or match.value
        env.command = cmd
        if cmd.level > 1:
            warn = await require_level(ctx, env, cmd.level)
            if warn:
                yield env.event.plain_result(warn)
                return
        async for reply in cmd.handler(ctx, env):
            yield reply
        return
