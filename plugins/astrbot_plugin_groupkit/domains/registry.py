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
from . import admin, help as help_domain, profile, quota, stats


async def _help_handler(ctx, env: CommandEnv) -> AsyncGenerator:
    async for reply in help_domain.send_help(ctx, env.event):
        yield reply


def _help_command() -> Command:
    return Command(
        key="help",
        matcher=exact(("帮助", "口令", "菜单", "功能", "指令", "help", "menu")),
        handler=_help_handler,
        help=(HelpEntry("帮助", "本菜单", page="base", order=90),),
    )


# ---------------------------------------------------------------------------
# 匹配顺序（从上到下，先命中先用）：
#   信息卡 → 帮助 → 改名 → 爱好 → 额度/抽奖/今日/额度记录
#   → 全群禁言/全群解禁 → 解禁 → 禁言 → 踢人 → 发额度/扣额度 → 授权
# ---------------------------------------------------------------------------
COMMANDS: list[Command] = [
    profile.COMMANDS[0],      # info
    _help_command(),          # help
    profile.COMMANDS[1],      # rename
    profile.COMMANDS[2],      # hobby
    quota.COMMANDS[0],        # 额度
    quota.COMMANDS[1],        # 抽奖
    stats.COMMANDS[0],        # 今日
    quota.COMMANDS[2],        # 额度记录
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
