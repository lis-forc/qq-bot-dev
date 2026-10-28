"""读 metadata.yaml 里的顶层字段。

单独一个文件，是为了让 card.py / gallery.py / help.py 三处都能用，
而它们之间不必互相 import（卡片不该依赖图鉴，图鉴也不该依赖卡片）。

不 import yaml：插件零第三方依赖，为了读两行字符串去引入 PyYAML 不划算，
而 metadata.yaml 的格式是我们自己写的，稳定。只认行首字段，不会撞上
嵌套键（比如 support_platforms 下面的同名项）。
"""

from __future__ import annotations

from pathlib import Path

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")


def read_meta(plugin_dir: Path | str | None, key: str) -> str:
    if not plugin_dir:
        return ""
    try:
        path = Path(plugin_dir) / "metadata.yaml"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except Exception as exc:  # noqa: BLE001 - 读不到就用默认值，不该影响出图
        logger.debug(f"[群助手] 读 metadata 的 {key} 失败: {exc}")
    return ""
