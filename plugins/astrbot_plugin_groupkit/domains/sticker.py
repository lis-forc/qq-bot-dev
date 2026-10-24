"""表情包：把 meme-generator 的画图能力接进群助手。

**渲染不在这个插件里。** 真正画图的是外面那个 meme-generator 服务
（见 core/meme_api.py），这个文件只负责把人话翻译成它的三个接口调用。

口令设计取向和群里其它功能一致：零前缀、白话、一件事一个口令。

    表情 摸头 @小明        ← 主用法
    表情 亲亲 "你好" @小明
    表情列表               ← 有哪些模板
    搜表情 猫              ← 按关键词找
    表情详情 摸头          ← 这个模板要几个参数

额度只在这一张表情真的做出来之后才扣。做失败、图片下载失败、
服务连不上，一律不扣 —— 用户没拿到东西就不该付钱。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.message_components import Image

from ..core.command import Command, CommandEnv, HelpEntry, exact, prefix
from ..core.context import PluginContext
from ..core.meme_api import MemeApiError, MemeInfo
from ..core.services import ProfileService
from ..utils import event as ev
from ..utils import media, onebot

# 同一个人 4 秒内只能做一张。不是为了省钱，是为了防连点 ——
# QQ 上「点两下没反应」太常见，第二下往往是真的只是想再点一次。
THROTTLE_SECONDS = 4.0
_LAST_USE: dict[str, float] = {}

# 模板列表一次最多显示个数，多了就刷屏
LIST_SAMPLE = 12
SEARCH_LIMIT = 15

QUOTE_PAIRS = {'"': '"', "'": "'", "`": "`", "“": "”", "‘": "’"}
DIRECTIONS = {"左": "left", "右": "right", "上": "top", "下": "bottom"}
SELF_WORDS = {"自己", "@自己"}
# 只认 ASCII 开头的 key=value，避免把「1=1」这种正常文字当成选项
OPTION_RE = re.compile(r"^([A-Za-z_][\w-]*)=(.+)$")


def split_tokens(text: str) -> list[str]:
    """按空格切分，但引号里的空格不算。

    `表情 亲亲 "你 好"` 要解析出两段文字，而不是三段。
    """
    tokens: list[str] = []
    current: list[str] = []
    closing = ""
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if closing:
            if char == closing:
                closing = ""
            else:
                current.append(char)
            continue
        if char in QUOTE_PAIRS:
            closing = QUOTE_PAIRS[char]
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current.clear()
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


@dataclass
class ParsedArgs:
    """一条「表情」命令里除模板名以外的部分，各归各位。"""

    texts: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    want_self: bool = False


def parse_args(tokens: list[str]) -> ParsedArgs:
    """把参数分成文字 / 图片链接 / 模板选项三类。

    选项写法跟 meme-generator 生态保持一致，会玩的人不用重新学：
        #circle         → {"circle": true}
        #name=value     → {"name": "value"}
        左 / 右 / 上 / 下 → {"direction": "left"} 这类
    """
    parsed = ParsedArgs()
    for token in tokens:
        if token in SELF_WORDS:
            parsed.want_self = True
            continue
        if token in DIRECTIONS:
            parsed.options["direction"] = DIRECTIONS[token]
            continue
        if token.startswith("#") and len(token) > 1:
            body = token[1:]
            match = OPTION_RE.match(body)
            if match:
                parsed.options[match.group(1).replace("-", "_")] = match.group(2)
            else:
                parsed.options[body.replace("-", "_")] = True
            continue
        match = OPTION_RE.match(token)
        if match:
            parsed.options[match.group(1).replace("-", "_")] = match.group(2)
            continue
        if token.startswith(("http://", "https://")):
            parsed.urls.append(token)
            continue
        parsed.texts.append(token)
    return parsed


# ---------------------------------------------------------------- 限流
def _throttle(user_id: str) -> float:
    """返回还要等几秒；0 表示可以继续。"""
    now = time.time()
    last = _LAST_USE.get(user_id, 0.0)
    remain = THROTTLE_SECONDS - (now - last)
    if remain > 0:
        return remain
    _LAST_USE[user_id] = now
    return 0.0


# ---------------------------------------------------------------- 素材组装
@dataclass
class Asset:
    data: bytes
    content_type: str
    filename: str
    user_info: dict[str, Any] = field(default_factory=dict)


async def _display_name(ctx: PluginContext, group_id: str, user_id: str) -> str:
    try:
        member = await asyncio.to_thread(ctx.db.get_member, group_id, user_id)
    except Exception:
        member = None
    return ProfileService.display_name(member, user_id)


async def _collect_assets(
    ctx: PluginContext,
    env: CommandEnv,
    info: MemeInfo,
    parsed: ParsedArgs,
) -> tuple[list[Asset], list[str]]:
    """凑够模板需要的图片。返回 (素材, 没能取到的原因)。

    优先级是有讲究的，按「用户表达的明确程度」排：
    1. 引用消息里的图、这条消息里的图 —— 用户已经指了实物
    2. 消息里附带的图片链接
    3. 被 @ 的人的头像 —— 表情包最经典的玩法就是拿别人头像开涮
    4. 还不够就补发送者自己的头像

    顺序不能换：meme-generator 是按位置吃图的，第一张常常代表
    「被摸头的那个」，谁在前谁在后结果完全不同。
    """
    assets: list[Asset] = []
    problems: list[str] = []
    max_bytes = ctx.config.meme_max_image_mb * 1024 * 1024
    timeout = ctx.config.meme_timeout

    if info.max_images <= 0:
        return assets, problems

    sources = await media.all_image_sources(env.event)
    sources.extend(parsed.urls)
    if sources:
        loaded, errors = await media.load_many(sources, max_bytes, timeout)
        problems.extend(errors)
        for data, content_type, filename in loaded:
            assets.append(Asset(data, content_type, filename))

    # 数量对不对由调用方统一判断，这里只负责「尽量凑够」
    if len(assets) >= info.min_images:
        return assets, problems

    # 用头像补缺口。先 @ 的人，再自己。
    at_ids = ev.at_targets(env.event, env.sender_id)
    wanted: list[str] = list(at_ids)
    if parsed.want_self or not wanted:
        wanted.append(env.sender_id)

    for user_id in wanted:
        if len(assets) >= info.min_images:
            break
        avatar = await media.load_avatar(user_id, max_bytes, timeout)
        if avatar is None:
            problems.append(f"取不到 {user_id} 的头像")
            continue
        assets.append(
            Asset(
                *avatar,
                user_info={
                    "name": await _display_name(ctx, env.group_id, user_id),
                    "gender": "unknown",
                },
            )
        )

    # 还差就补自己：QQ 表情模板极少有「一张图都不给也合理」的情况
    if len(assets) < info.min_images and ctx.config.meme_auto_avatar:
        avatar = await media.load_avatar(env.sender_id, max_bytes, timeout)
        if avatar is not None:
            assets.append(
                Asset(
                    *avatar,
                    user_info={
                        "name": await _display_name(ctx, env.group_id, env.sender_id),
                        "gender": "unknown",
                    },
                )
            )

    return assets, problems


# ---------------------------------------------------------------- 发送
def _write_temp(ctx: PluginContext, data: bytes, content_type: str) -> Path:
    folder = ctx.data_dir / "memes"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type, ".png")
    path = folder / f"meme_{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    # 只留最近 40 个，表情发出去之后本地文件就没用了
    try:
        old_files = sorted(folder.glob("meme_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old_files[40:]:
            stale.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(f"[群助手] 清理表情临时文件失败: {exc}")
    return path


def _group_number(group_id: str) -> Any:
    """OneBot 要的是数字群号，但群号万一不是纯数字也别让整条命令崩掉。"""
    text = str(group_id or "").strip()
    return int(text) if text.isdigit() else text


async def _send_sticker(
    ctx: PluginContext, env: CommandEnv, data: bytes, content_type: str
) -> Path | None:
    """按「表情小图」发出去。返回 None 表示已经发成功；返回路径表示要降级发普通图片。

    OneBot 的 image 段带 sub_type=1 时，客户端会当成表情渲染（显示成
    [动画表情]、占位和原生表情一样大）。普通图片则会把聊天框撑开一大块，
    发一堆表情的时候非常难看。不同协议端认的字段名不一样，所以几个
    拼写都塞进去。
    """
    b64 = base64.b64encode(data).decode("ascii")
    message = [
        {
            "type": "image",
            "data": {
                "file": f"base64://{b64}",
                "sub_type": 1,
                "subType": 1,
                "subtype": 1,
                "summary": "[动画表情]",
            },
        }
    ]
    result = await onebot.call(
        env.event, "send_group_msg", group_id=_group_number(env.group_id), message=message
    )
    if onebot.succeeded(result):
        return None

    logger.warning("[群助手] 表情小图发送失败，降级为普通图片")
    return await asyncio.to_thread(_write_temp, ctx, data, content_type)


# ---------------------------------------------------------------- 提示文案
def _usage(ctx: PluginContext) -> str:
    cost = ctx.config.meme_cost
    note = f"成功扣 {cost} {ctx.config.currency_name}" if cost > 0 else "免费"
    return (
        "用法：表情 <模板> [文字/@某人/图片…]\n"
        "例：表情 摸头 @小明\n"
        "　  表情 亲亲 \"你好\" @小明\n"
        f"找模板：表情列表 / 搜表情 猫（{note}）"
    )


def _info_line(info: MemeInfo) -> str:
    return f"{info.key}（{info.image_span()}，{info.text_span()}）"


# ---------------------------------------------------------------- 命令
async def make(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """表情 <模板> [参数…]：做一张表情。"""
    raw = (env.args or "").strip()
    if not raw:
        yield env.event.plain_result(_usage(ctx))
        return

    tokens = split_tokens(raw)
    if not tokens:
        yield env.event.plain_result(_usage(ctx))
        return
    query, rest = tokens[0], tokens[1:]

    # 模板清单连不上就没法继续，这里要给出「怎么把服务跑起来」
    try:
        catalog = await ctx.meme.catalog()
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return

    found = ctx.meme.find(query, catalog)
    if found is None:
        hints = "、".join(random.sample(sorted(catalog), min(6, len(catalog))))
        yield env.event.plain_result(
            f"没找到「{query}」这个表情\n"
            f"常用的有：{hints}\n"
            "发「搜表情 关键词」找找看"
        )
        return

    # 列表里可能只有 key 没有参数，制作前必须问一次准确的数字
    try:
        info = await ctx.meme.info(found.key)
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return

    # 参数里混着文字、链接、@人、模板选项，先分好类再谈凑图
    parsed = parse_args(rest)
    assets, problems = await _collect_assets(ctx, env, info, parsed)
    if problems:
        # 图片的问题必须说清楚是缺图还是图坏了，否则用户只会以为「又坏了」
        yield env.event.plain_result("⚠️ " + "\n".join(problems[:3]))
        return

    if len(assets) > info.max_images:
        yield env.event.plain_result(
            f"这个表情最多用 {info.max_images} 张图，你给了 {len(assets)} 张\n"
            f"少发几张再试：表情 {info.key} @某人"
        )
        return
    if len(assets) < info.min_images:
        yield env.event.plain_result(
            f"这个表情需要 {info.image_span()}，现在只有 {len(assets)} 张\n"
            f"发图或 @人 都可以：表情 {info.key} @小明"
        )
        return

    texts = list(parsed.texts)
    if not texts and ctx.config.meme_auto_texts:
        texts = list(info.default_texts)
    if not (info.min_texts <= len(texts) <= info.max_texts):
        yield env.event.plain_result(
            f"这个表情需要 {info.text_span()}，现在给了 {len(texts)} 段\n"
            f"例：表情 {info.key} \"文字\""
        )
        return

    cost = ctx.config.meme_cost
    if cost > 0:
        balance = await asyncio.to_thread(ctx.db.balance, env.group_id, env.sender_id)
        if balance < cost:
            yield env.event.plain_result(
                f"⚠️ {ctx.config.currency_name}不够：需要 {cost}，你还有 {balance}\n"
                "多聊几句、攒够发言抽个奖就有了"
            )
            return

    # 参数齐了、钱也够，才占用限流名额。
    # 放在最前面的话，打错模板名或图没发对的人也要干等 4 秒。
    wait = _throttle(env.sender_id)
    if wait > 0:
        yield env.event.plain_result(f"慢点，{wait:.0f} 秒后再试")
        return

    # 真的开始画
    try:
        data, content_type = await ctx.meme.render(
            info.key,
            [(a.data, a.content_type, a.filename) for a in assets],
            texts,
            [a.user_info for a in assets],
            parsed.options,
            timeout=ctx.config.meme_timeout,
        )
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 兜底，别让一句口令把群消息卡死
        logger.error(f"[群助手] 表情渲染异常: {exc}", exc_info=True)
        yield env.event.plain_result(f"⚠️ 制作表情失败：{exc}")
        return

    # 图片到手才扣费。失败不扣，用户没拿到东西就不该付钱。
    if cost > 0:
        ok, left = await asyncio.to_thread(
            ctx.db.add_quota, env.group_id, env.sender_id, -cost, "制作表情", env.sender_id
        )
        if not ok:
            logger.warning(f"[群助手] 表情扣费失败（余额 {left}），本次免费")

    fallback = await _send_sticker(ctx, env, data, content_type)
    if fallback is not None:
        yield env.event.chain_result([Image.fromFileSystem(str(fallback))])


async def meme_list(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """表情列表：给个大概，别把几百个模板全倒出来。"""
    try:
        catalog = await ctx.meme.catalog()
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return
    keys = sorted(catalog)
    sample = random.sample(keys, min(LIST_SAMPLE, len(keys)))
    yield env.event.plain_result(
        f"现在有 {len(keys)} 个表情模板，随手挑几个：\n"
        + "、".join(sample)
        + "\n\n做一张：表情 模板名 @某人\n找特定的：搜表情 关键词"
    )


async def meme_search(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """搜表情 <关键词>。"""
    query = (env.args or "").strip()
    if not query:
        yield env.event.plain_result("用法：搜表情 猫\n（按模板名或关键词找）")
        return
    try:
        catalog = await ctx.meme.catalog()
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return
    hits = ctx.meme.search(query, catalog, limit=SEARCH_LIMIT)
    if not hits:
        yield env.event.plain_result(
            f"没搜到「{query}」\n换个词试试，或者发「表情列表」看看都有什么"
        )
        return
    lines = [f"搜到 {len(hits)} 个跟「{query}」有关的："]
    for index, info in enumerate(hits, 1):
        lines.append(f"{index}. {_info_line(info)}")
    lines.append("\n挑一个：表情 模板名 @某人")
    yield env.event.plain_result("\n".join(lines))


async def meme_detail(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """表情详情 <模板>：这个模板到底要几个图几段字。"""
    query = (env.args or "").strip()
    if not query:
        yield env.event.plain_result("用法：表情详情 摸头")
        return
    try:
        catalog = await ctx.meme.catalog()
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return
    found = ctx.meme.find(query, catalog)
    if found is None:
        yield env.event.plain_result(f"没找到「{query}」这个表情")
        return
    try:
        info = await ctx.meme.info(found.key)
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return

    lines = [f"{info.key}", f"需要 {info.image_span()}，{info.text_span()}"]
    if info.keywords and info.keywords != [info.key]:
        lines.append("也能这么叫：" + "、".join(info.keywords[:6]))
    if info.default_texts and not info.min_texts:
        lines.append("不写文字时用：" + "、".join(info.default_texts[:3]))
    lines.append(f"\n做一张：表情 {info.key} @某人")
    yield env.event.plain_result("\n".join(lines))


COMMANDS = [
    Command(
        key="meme_list",
        matcher=exact(("表情列表", "表情包列表", "表情模板", "有哪些表情", "表情大全")),
        handler=meme_list,
        perm_short="表情",
        help=(HelpEntry("表情列表", "看有哪些模板", page="base", order=50),),
    ),
    Command(
        key="meme_search",
        matcher=prefix(("搜表情", "找表情", "表情搜索")),
        handler=meme_search,
        help=(HelpEntry("搜表情 猫", "按关键词找模板", page="base", order=51),),
    ),
    Command(
        key="meme_detail",
        matcher=prefix(("表情详情", "表情说明", "表情参数")),
        handler=meme_detail,
        help=(HelpEntry("表情详情 摸头", "这个模板要几个参数", page="base", order=52),),
    ),
    Command(
        key="meme_make",
        matcher=prefix(("表情", "做表情", "制作表情")),
        handler=make,
        help=(HelpEntry("表情 摸头 @某人", "做一张表情包", page="base", order=53),),
    ),
]
