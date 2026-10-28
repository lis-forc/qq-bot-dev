"""合并转发：把一条回复打包成一个「聊天记录」气泡，点开才展开。

为什么需要它：帮助菜单、表情图鉴这类内容一次好几屏，直接发就是刷屏。
合并转发在 QQ 里表现为一条消息，右侧写着「N 条聊天记录」，群友想看再点开。

这里只做「构造消息链」，不负责发送 —— 调用方拿到 chain 自己 yield
（`event.chain_result(chain)`），这样权限校验、超时、日志都还在原来的
处理函数里，不会被抽走。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Node, Nodes, Plain


def build_forward(event: Any, items: list[tuple[str, list]]) -> list | None:
    """把 [(节点标题, 组件列表)] 打成一个合并转发消息链。

    返回 None 表示构造失败（框架版本不兼容、事件不是群聊等），
    调用方应退回普通消息 —— 宁可刷屏，也不能一条都不发。
    """
    if not items:
        return None
    try:
        uin = str(event.get_self_id() or "10000")
        nodes = [Node(uin=uin, name=title, content=content) for title, content in items]
        if len(nodes) == 1:
            return [nodes[0]]
        try:
            return [Nodes(nodes=nodes)]
        except TypeError:
            # 老版本 Nodes 的位置参数签名不一样
            return [Nodes(nodes)]
    except Exception as exc:
        logger.error(f"[群助手] 构造合并转发失败，改用普通消息: {exc}")
        return None


def text_node(title: str, body: str) -> tuple[str, list]:
    return title, [Plain(body)]
