"""插件上下文：把配置和服务打包成一个对象传给每个功能模块。

处理函数只接 `(ctx, env)` 两个参数，不直接碰 Star 实例。
好处是功能模块能被单元测试直接调用 —— 造一个 ctx 就行，不需要起机器人。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PluginConfig
from .database import Database
from .meme_api import MemeApi
from .services import Permission, ProfileService, QuotaService, StatsService
from ..utils.card import CardRenderer


@dataclass
class PluginContext:
    """一次插件生命周期内长期存在的依赖。"""

    config: PluginConfig
    db: Database
    permission: Permission
    profiles: ProfileService
    stats: StatsService
    quota: QuotaService
    cards: CardRenderer
    meme: MemeApi
    plugin_dir: Path
    data_dir: Path

    # 小转发：很多功能要「同一句话发给群」，省得每处都写 event.plain_result
    @staticmethod
    def plain(event: Any, text: str) -> Any:
        return event.plain_result(text)
