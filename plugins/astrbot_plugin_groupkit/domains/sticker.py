"""表情包：把 meme-generator 的画图能力接进群助手。

**渲染不在这个插件里。** 真正画图的是外面那个 meme-generator 服务
（见 core/meme_api.py），这个文件只负责把人话翻译成它的三个接口调用。

口令设计取向和群里其它功能一致：零前缀、白话、一件事一个口令。

    表情 摸头 @小明        ← 主用法
    表情 摸头 123456789    ← 人不在群里就填 QQ 号
    表情 举牌 你好 @某人   ← 要写字直接跟在后面，不用引号
    表情 亲 @小明 @小红    ← 有的模板要两个人
    表情列表 [第几张]      ← 自己做的图鉴，一张一张发
    表情详情 摸头          ← 这个模板要几个参数

关于「@ 人」这件事踩过一次坑，写在这里提醒后来者
--------------------------------------------------
OneBot 的 @ 是消息链里的一个 **组件**，不是文字。但 AstrBot 把它拼进
`message_str` 时是 `@昵称` 这种纯文本，于是：

- 昵称里带空格 / 特殊符号时，切词会把它切成两段，多出来的半截被当成
  「要写在表情上的文字」，模板只收 0 段字，直接报「段数不符」；
- 昵称是纯数字时更糟：那串数字看起来像 QQ 号，也可能被当成文字。

所以这里做两件事：**先把 @ 提到的人从文本里摘干净**，
再单独从消息组件里读真实 QQ 号 —— 组件里的是可信的，文本里的是渲染结果。
另外支持直接打一串 QQ 号当头像来源：群友想拿没进群的人的头像做表情，
这是很自然的用法，不该只认 @。
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

from ..core.command import Command, CommandEnv, HelpEntry, Match, prefix
from ..core.context import PluginContext
from ..core.meme_api import MemeApiError, MemeInfo
from ..core.services import ProfileService
from ..utils import event as ev
from ..utils import deco, media, nickname, onebot
from ..utils.forward import build_forward
from ..utils.router import compact

# 同一个人 4 秒内只能做一张。不是为了省钱，是为了防连点 ——
# QQ 上「点两下没反应」太常见，第二下往往是真的只是想再点一次。
THROTTLE_SECONDS = 4.0
_LAST_USE: dict[str, float] = {}

# 搜索结果的条数上限。超过这个数就不是「找」而是「逛」了，
# 那种需求应该去翻图鉴。
QUOTE_PAIRS = {'"': '"', "'": "'", "`": "`", "“": "”", "‘": "’"}
DIRECTIONS = {"左": "left", "右": "right", "上": "top", "下": "bottom"}
SELF_WORDS = {"自己", "@自己"}
# 只认 ASCII 开头的 key=value，避免把「1=1」这种正常文字当成选项
OPTION_RE = re.compile(r"^([A-Za-z_][\w-]*)=(.+)$")
# 一串像 QQ 号的数字。5~12 位是 OneBot 生态里的常规取值范围。
QQ_RE = re.compile(r"^@?(\d{5,12})$")

MAKE_PREFIXES = ("表情", "做表情", "制作表情")


def split_tokens_ex(text: str) -> list[tuple[str, bool]]:
    """按空格切分，返回 (词, 是否被引号包住)。

    引号可写可不写（`表情 举牌 你好` 就够了），但写了要成对：引号里的空格不算分隔，`表情 举牌 "你 好"` 解析出的是一段文字。

    为什么要带上「引没引号」：引号是用户明确说「这就是文字」的信号。
    `表情 举牌 123456` 里的数字更像 QQ 号，`表情 举牌 "123456"` 则是
    真的要在图上写 123456 —— 只有引号能区分这两件事。
    """
    tokens: list[tuple[str, bool]] = []
    current: list[str] = []
    closing = ""
    quoted = False
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
            quoted = True
            continue
        if char.isspace():
            if current:
                tokens.append(("".join(current), quoted))
                current.clear()
                quoted = False
            continue
        current.append(char)
    if current:
        tokens.append(("".join(current), quoted))
    return tokens


def split_tokens(text: str) -> list[str]:
    """兼容老调用：只要词，不要引号标记。"""
    return [word for word, _ in split_tokens_ex(text)]


def mention_words(event: Any) -> list[str]:
    """消息里 @ 到的人在这个客户端的显示文本（`@昵称`）。

    用来把这些碎片从参数里摘掉。拿不到就返回空列表 ——
    后面的「整段剔除」还会再兜一次。
    """
    words: list[str] = []
    message_obj = getattr(event, "message_obj", None)
    for comp in getattr(message_obj, "message", None) or []:
        kind = str(getattr(comp, "type", "") or type(comp).__name__).lower()
        if kind != "at":
            continue
        for attr in ("name", "text", "card", "nickname"):
            value = str(getattr(comp, attr, "") or "").strip()
            if value and value != "all":
                words.extend([f"@{value}", value])
    return words


def strip_mentions(raw: str, event: Any = None, marks: tuple[str, ...] = ()) -> str:
    """把 `@某人` 从参数里摘干净，保留真正的文字。

    三遍，顺序不能换：

    1. **已知显示名整段剔除**（可能带空格）；
    2. **`@记号 名字` 整段剔除** —— 群名片是「🦊 陈嘉豪」，客户端的 @ 渲染
       出来就是 `@🦊 陈嘉豪`。记号与名字之间那个空格让「@ 到哪结束」变得
       没法判断，所以先按「@ + 记号 + 空格 + 一段非空白」整段吃掉；
    3. 剩下零散的 `@xxxx`（不带空格的）逐个剔除。

    第二遍是修 bug 加的：只有 1、3 两遍时，第 3 遍的 `@\\S+` 只吃掉了
    `@🦊`，把「陈嘉豪」留在参数里 —— 模板不要文字的命令（比如「摸」）
    于是报「需要 0 段文字，现在给了 1 段」。凡是带性别记号的人都踩。
    """
    text = raw or ""
    for word in mention_words(event):
        if word:
            text = text.replace(word, " ")
    for mark in marks:
        if mark:
            text = re.sub(rf"@{re.escape(mark)}\s*\S+", " ", text)
    # 不带空格的 @xxx：从 @ 一直吃到空白
    text = re.sub(r"@\S+", " ", text)
    return compact(text)


def chain_text(event: Any) -> str:
    """消息链里的**纯文本**段，跳过 @ / 图片 / 引用。

    这是修 @ 解析问题的正解：AstrBot 的 `message_str` 把 @ 渲染成
    `@群名片`，而群名片是「🦊 陈嘉豪」—— 那个空格让它和正常文字无法区分。
    从消息链取文本，@ 段天然不在里面，一个字符都不用猜。

    取不到（某些协议端不带 message 列表）返回空串，调用方退回 `message_str`。
    """
    parts: list[str] = []
    for comp in getattr(getattr(event, "message_obj", None), "message", None) or []:
        if type(comp).__name__ != "Plain":
            continue
        parts.append(str(getattr(comp, "text", "") or ""))
    return "".join(parts)


@dataclass
class ParsedArgs:
    """一条「表情」命令里除模板名以外的部分，各归各位。"""

    texts: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    user_ids: list[str] = field(default_factory=list)
    want_self: bool = False


def parse_args(tokens: list[tuple[str, bool]] | list[str]) -> ParsedArgs:
    """把参数分成文字 / 图片链接 / QQ 号 / 模板选项四类。

    选项写法跟 meme-generator 生态保持一致，会玩的人不用重新学：
        #circle         → {"circle": true}
        #name=value     → {"name": "value"}
        左 / 右 / 上 / 下 → {"direction": "left"} 这类

    `tokens` 可以是 `split_tokens_ex` 的结果，也可以是纯字符串列表
    （老调用方式，那时所有词都按「没加引号」处理）。
    """
    parsed = ParsedArgs()
    for item in tokens:
        token, quoted = item if isinstance(item, tuple) else (item, False)
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
        # 没加引号的纯数字当成 QQ 号：这是「拿不在群里的那个人的头像」的写法。
        # 想在图上写数字就加引号，见 split_tokens_ex 的注释。
        qq = QQ_RE.match(token) if not quoted else None
        if qq:
            parsed.user_ids.append(qq.group(1))
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
    3. 被 @ 的人 / 打出来的 QQ 号的头像 —— 表情包最经典的玩法就是拿别人头像开涮
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

    if len(assets) >= info.min_images:
        return assets, problems

    # 用头像补缺口。先 @ 的人 / 打出来的号，再自己。
    wanted: list[str] = list(ev.at_targets(env.event, env.sender_id))
    wanted.extend(parsed.user_ids)
    if parsed.want_self or not wanted:
        wanted.append(env.sender_id)

    for user_id in dict.fromkeys(wanted):  # 去重且保序
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

    # 凑够了就把过程中的小毛病丢掉：某一个人的头像挂了但模板只要一张，
    # 结果已经拿到了，没必要让他看着一行警告再重发一次。
    if len(assets) >= info.min_images:
        problems = []
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
    """不知道模板名时看到的第一屏。

    写这段的标准是：一个从没用过的人照着抄一遍就能出图。
    所以给的是三个「能直接复制去发」的例子，不是一个语法说明。
    """
    cost = ctx.config.meme_cost
    note = f"做成功扣 {cost} {ctx.config.currency_name}" if cost > 0 else "做图免费"
    return (
        "表情包这样做：\n"
        "\n"
        "  表情 摸头 @某人　　　拿他的头像做一张\n"
        "  表情 摸头 123456789　　不在群里就填 QQ 号\n"
        "  表情 举牌 你好 @某人　　要写的字直接跟在后面\n"
        "  表情 亲 @小明 @小红　　有的模板要两个人\n"
        "\n"
        f"不知道有什么？发「表情列表」看图鉴。{note}"
    )


def _parse_page(raw: str) -> int:
    """从「2」「第2页」「第二页」里抠出页码。抠不到就当第一页。"""
    digits = re.sub(r"\D", "", raw or "")
    if digits:
        return max(1, int(digits))
    return 1


LIST_WORDS = ("表情列表", "表情包列表", "表情模板", "有哪些表情", "表情大全", "表情图鉴")


def _list_matcher(ctx, text: str, lower: str) -> Match | None:
    """「表情列表」或「表情列表 2」。

    不能用 prefix：那样「表情列表怎么加」也会被当成翻页吞掉，
    而那句话是真的在提问。这里只放行「纯口令」和「口令 + 页码」，
    其余一律不接，让它落回正常聊天。
    """
    for word in LIST_WORDS:
        if lower == word:
            return Match()
        if lower.startswith(word):
            rest = lower[len(word):].strip(" \t:：第页")
            if rest.isdigit():
                return Match(args=rest)
    return None


def _strip_make_prefix(text: str) -> str | None:
    """切掉「表情 / 做表情 / 制作表情」前缀；不是这个命令返回 None。

    matcher 和 handler 共用同一套规则：matcher 用它判断这条消息归不归自己，
    handler 用它从**消息链的纯文本**里重新取一次参数。两处如果各写一遍，
    迟早在「前缀后面跟的是冒号还是空格」这种细节上分叉。

    返回空串表示「只发了前缀、没带参数」，和「不是这个命令」的 None 要分清 ——
    前者要回用法提示。
    """
    cleaned = compact(text)
    for word in MAKE_PREFIXES:
        if cleaned == word:
            return ""
        if cleaned.startswith(word):
            rest = cleaned[len(word):]
            if rest and (rest[0].isspace() or rest[0] in "：:，,、"):
                return rest.lstrip("：:，,、 ")
    return None


def _make_matcher(ctx, text: str, lower: str) -> Match | None:
    """「表情 xxx」。

    自己实现的理由：`prefix()` 会顺手把 `@某人` 从参数里删掉，
    而这里需要看到原文才能分辨「@ 提到的人」和「真的要写在图上的 @」。
    交给 `strip_mentions` 统一处理，比在匹配器里删一半再猜另一半可靠。
    """
    args = _strip_make_prefix(text)
    if args is None:
        return None
    return Match(args=args)


async def _load_catalog(ctx: PluginContext) -> tuple[dict | None, str]:
    """取模板清单。返回 (清单, 错误文案)，两个里必有一个是空的。

    三个处理函数都要拿清单，且失败时的表现完全一样（把服务端那句
    人话原样发出去），所以收在这里，免得三处各写一遍 try。
    """
    try:
        return await ctx.meme.catalog(), ""
    except MemeApiError as exc:
        return None, str(exc)


def _sorted_templates(catalog: dict) -> list[MemeInfo]:
    """按 key 排。稳定压倒好看 —— 同一个模板永远排在同一个位置，
    看过一次的人下次还能在同一个地方找到它。"""
    return sorted(catalog.values(), key=lambda item: item.key)


# ---------------------------------------------------------------- 命令
async def make(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """表情 <模板> [参数…]：做一张表情。"""
    # 优先从消息链取纯文本：@ 段天然被跳过，不会像 message_str 那样留下
    # 「陈嘉豪」这种被空格截断的半截名字。协议端没给 message 列表时，
    # 退回 matcher 已经切好的参数，靠 strip_mentions 里的记号规则兜住。
    from_chain = _strip_make_prefix(chain_text(env.event))
    source = from_chain if from_chain is not None else (env.args or "")
    raw = strip_mentions(source, env.event, nickname.known_marks(ctx.config))
    if not raw:
        yield env.event.plain_result(_usage(ctx))
        return

    tokens = split_tokens_ex(raw)
    if not tokens:
        yield env.event.plain_result(_usage(ctx))
        return
    query, rest = tokens[0][0], tokens[1:]

    # 模板清单连不上就没法继续，这里要给出「怎么把服务跑起来」
    catalog, error = await _load_catalog(ctx)
    if catalog is None:
        yield env.event.plain_result(f"⚠️ {error}")
        return

    found = ctx.meme.find(query, catalog)
    if found is None:
        # 提示里给的必须是中文名。给一串 petpet、raise_sign，
        # 用户照着抄也做不出来 —— 那些是内部标识，不是给人念的。
        pool = _sorted_templates(catalog)
        hints = "、".join(item.title for item in random.sample(pool, min(6, len(pool))))
        yield env.event.plain_result(
            f"没找到「{query}」这个表情\n"
            f"手上有的比如：{hints}\n"
            "发「表情列表」翻一遍图鉴"
        )
        return

    # 列表里可能只有 key 没有参数，制作前必须问一次准确的数字
    try:
        info = await ctx.meme.info(found.key)
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return

    # 参数里混着文字、链接、@人、QQ号、模板选项，先分好类再谈凑图
    parsed = parse_args(rest)
    assets, problems = await _collect_assets(ctx, env, info, parsed)
    if problems:
        # 图片的问题必须说清楚是缺图还是图坏了，否则用户只会以为「又坏了」
        yield env.event.plain_result("⚠️ " + "\n".join(problems[:3]))
        return

    if len(assets) > info.max_images:
        yield env.event.plain_result(
            f"「{info.title}」最多用 {info.max_images} 张图，你给了 {len(assets)} 张\n"
            f"少发几张再试：表情 {info.title} @某人"
        )
        return
    if len(assets) < info.min_images:
        yield env.event.plain_result(
            f"「{info.title}」需要 {info.image_span()}，现在只有 {len(assets)} 张\n"
            f"发图、@人 或填 QQ 号都行：表情 {info.title} @小明"
        )
        return

    texts = list(parsed.texts)
    if not texts and ctx.config.meme_auto_texts:
        texts = list(info.default_texts)
    if not (info.min_texts <= len(texts) <= info.max_texts):
        yield env.event.plain_result(
            f"「{info.title}」需要 {info.text_span()}，现在给了 {len(texts)} 段\n"
            f"文字要用英文引号包住：表情 {info.title} \"文字\""
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


# ---------------------------------------------------------------- 图鉴
def _catalog_text(
    items: list, total: int, page: int = 0, pages: int = 1, per_page: int = 0
) -> str:
    """没放图鉴图时的兜底：把模板名挤成几行，一行六个。

    图鉴图是自己做的（`assets/images/catalog/`），没做的时候总得能看 ——
    群友要的是一份「都有什么」的清单，而不是一句报错。
    """
    if page:
        start = (page - 1) * per_page
        shown = items[start:start + per_page]
        head = f"表情图鉴 第 {page}/{pages} 张（共 {total} 个）"
    else:
        shown = items
        head = f"表情图鉴（共 {total} 个）"

    names = [item.title for item in shown]
    lines = [deco.head(head, "meme")]
    for index in range(0, len(names), 6):
        lines.append(" · ".join(names[index:index + 6]))
    lines.append("")
    lines.append("做一张：表情 摸头 @某人　　详情：表情详情 摸头")
    return "\n".join(lines)


async def meme_list(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """表情列表 [第几张]：把自己做的图鉴发出去。

    图从 `assets/images/catalog/` 来，按文件名顺序排，一张一张发：

    - 只有一张 → 直接发；
    - 多张 → 打成一条合并转发，点开就是一整本，不刷屏；
    - 带页码（表情列表 2）→ 只发那一张，给想单独存图的人。

    为什么不在这里画图鉴了：图鉴是「作品」，得能控制字体、配色、排版，
    代码画出来的网格永远比人在设计软件里排的丑一档。所以这件事交给图。
    """
    raw = (env.args or "").strip()
    page = _parse_page(raw) if raw else 0
    shots = ctx.images.pick_catalog(page)

    if not shots:
        # 没有图鉴图 → 退回「模板名清单」。这时才需要连 meme 服务。
        catalog, error = await _load_catalog(ctx)
        if catalog is None:
            yield env.event.plain_result(f"⚠️ {error}")
            return
        items = _sorted_templates(catalog)
        total = len(items)
        per_page = ctx.config.meme_catalog_size
        pages = max(1, (total + per_page - 1) // per_page)
        # 页码夹回有效范围。群友记不住到底有几张，发「表情列表 99」是个手滑，
        # 不是错误请求 —— 回一句「第 99/1 张」等于把一次手滑变成一条错误信息。
        if page:
            page = min(page, pages)
        yield env.event.plain_result(
            _catalog_text(
                items, total,
                page=page if page else 0, pages=pages, per_page=per_page,
            )
        )
        return

    if len(shots) == 1:
        yield env.event.chain_result([Image.fromFileSystem(str(shots[0]))])
        return

    total = len(ctx.images.catalog_images())
    chain = build_forward(
        env.event,
        [
            (
                f"表情图鉴 {index}/{total}",
                [Image.fromFileSystem(str(path))],
            )
            for index, path in enumerate(shots, 1)
        ],
    )
    if chain:
        yield env.event.chain_result(chain)
        return

    # 协议端不支持合并转发：宁可连发几张，也不能一张都不给
    for path in shots:
        yield env.event.chain_result([Image.fromFileSystem(str(path))])


async def meme_detail(ctx: PluginContext, env: CommandEnv) -> AsyncGenerator:
    """表情详情 <模板>：这个模板到底要几个图几段字。"""
    # 这里沿用 matcher 切好的参数：它的前缀是「表情详情」，不是「表情」，
    # 而 chain_text 给的是整句原文，直接喂进去会把口令本身当成模板名。
    query = strip_mentions(
        env.args or "", env.event, nickname.known_marks(ctx.config)
    ).strip()
    if not query:
        yield env.event.plain_result("用法：表情详情 摸头")
        return
    catalog, error = await _load_catalog(ctx)
    if catalog is None:
        yield env.event.plain_result(f"⚠️ {error}")
        return
    found = ctx.meme.find(query, catalog)
    if found is None:
        yield env.event.plain_result(
            f"没找到「{query}」这个表情，发「表情列表」翻一遍"
        )
        return
    try:
        info = await ctx.meme.info(found.key)
    except MemeApiError as exc:
        yield env.event.plain_result(f"⚠️ {exc}")
        return

    spans = [s for s in (info.image_span(), info.text_span()) if s]
    lines = [deco.head(info.title, "meme")]
    if spans:
        lines.append("需要 " + "，".join(spans))
    if info.aliases:
        lines.append("也能这么叫：" + "、".join(info.aliases[:6]))
    if info.default_texts and not info.min_texts:
        lines.append("不写文字时用：" + "、".join(info.default_texts[:3]))
    lines.append("")
    lines.append(f"做一张：表情 {info.title} @某人")
    # 内部标识放最后一行小字：群友不用管，排查问题时要靠它对日志
    if info.key != info.title:
        lines.append(f"（内部标识 {info.key}）")
    yield env.event.plain_result("\n".join(lines))


COMMANDS = [
    Command(
        key="meme_list",
        matcher=_list_matcher,
        handler=meme_list,
        perm_short="表情列表",
        help=(HelpEntry("表情列表 [第几张]", "全部模板一张图看完，多张自动打包", page="fun", order=20, group="找模板"),),
    ),
    Command(
        key="meme_detail",
        matcher=prefix(("表情详情", "表情说明", "表情参数")),
        handler=meme_detail,
        perm_short="",
        help=(HelpEntry("表情详情 摸头", "要几张图、几段字", page="fun", order=22, group="找模板"),),
    ),
    Command(
        key="meme_make",
        matcher=_make_matcher,
        handler=make,
        perm_short="",
        # 三条而不是一条：菜单的作用是教会人怎么发第二句，
        # 只列「表情 摸头 @某人」的话，带文字和要两个人的模板他永远学不会。
        help=(
            HelpEntry("表情 摸头 @某人", "拿他的头像做一张", page="fun", order=10, group="做一张表情"),
            HelpEntry("表情 摸头 QQ号", "人不在群里就填号", page="fun", order=11, group="做一张表情"),
            HelpEntry("表情 举牌 你好 @某人", "要写的字直接跟在后面", page="fun", order=12, group="做一张表情"),
            HelpEntry("表情 亲 @小明 @小红", "有的模板要两个人", page="fun", order=13, group="做一张表情"),
        ),
    ),
]
