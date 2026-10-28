"""插件上下文：把配置和服务打包成一个对象传给每个功能模块。

处理函数只接 `(ctx, env)` 两个参数，不直接碰 Star 实例。
好处是功能模块能被单元测试直接调用 —— 造一个 ctx 就行，不需要起机器人。

**现在只剩一个渲染器。** 签到、商店、称号、图鉴都改成了文字 / 用户自带图，
插件的出图能力收窄成「信息卡排字」这一件事（`utils/card.py`）。
这不是为了省代码，而是取舍：这台机器上画的图，永远比不上人在本地用
设计工具做的那张；与其画一堆「看得出是代码生成」的卡片，不如把图交出去，
代码只负责把每个人的名字、额度、数据排到该在的位置上。

所以 `images`（`utils/assets.py`）才是这一版的主角：三堆图、一套命名，
签到运势图、表情图鉴、名片皮肤全靠它找。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkin import CheckinService
from .config import PluginConfig
from .database import Database
from .meme_api import MemeApi
from .services import Permission, ProfileService, QuotaService, StatsService
from ..utils.assets import ImageBank
from ..utils.card import CardRenderer
from ..utils.meta import read_meta


@dataclass
class PluginContext:
    """一次插件生命周期内长期存在的依赖。"""

    config: PluginConfig
    db: Database
    permission: Permission
    profiles: ProfileService
    stats: StatsService
    quota: QuotaService
    checkin: CheckinService
    cards: CardRenderer
    images: ImageBank
    meme: MemeApi
    plugin_dir: Path
    data_dir: Path

    @property
    def gallery_brand(self) -> str:
        """群助手自己的名字。菜单页头、卡片落款都用它。

        原来挂在 `gallery.brand` 上（图鉴渲染器的属性）—— 图鉴删掉之后
        这个名字没有归宿了，就收回到 ctx 上。读 metadata.yaml 的
        `display_name`，读不到给个中性默认值。
        """
        return read_meta(self.plugin_dir, "display_name") or "群助手"

    # 小转发：很多功能要「同一句话发给群」，省得每处都写 event.plain_result
    @staticmethod
    def plain(event: Any, text: str) -> Any:
        return event.plain_result(text)
