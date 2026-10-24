"""素材获取：把「群消息里的一张图」变成「一段可以直接发出去的字节」。

表情制作要的素材只有三种来源，全都在这儿收口：

1. 这条消息自己带的图
2. 引用消息里的图（`回复一张图 + 表情 摸头` 是最顺手的用法）
3. QQ 头像（q4.qlogo.cn，按 QQ 号直接拼，不需要任何接口权限）

为什么单独一个文件：这里全是「外部世界不可靠」的处理 —— 群里的图可能
是 NapCat 的本地缓存路径、可能是 CDN 链接、可能挂了、可能几百 MB。
把这些脏活和表情业务逻辑混在一起，后者就没法读了。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 同 core/meme_api.py：线上必有 aiohttp，这里只为了让自测能在裸环境跑。
try:
    import aiohttp  # noqa: F811
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

# spec=640 是 QQ 头像能拿到的较大尺寸；表情模板里头像经常要铺满整块画布，
# 太小会糊。这个地址不需要登录态，也不需要 cookie。
QQ_AVATAR = "https://q4.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640"

MAX_CONCURRENT_DOWNLOAD = 4


def _sniff_type(data: bytes) -> str:
    """按文件头判断图片类型。

    不用 imghdr：Python 3.13 已经把它删了，插件得同时能在 3.10~3.13 上跑。
    meme-generator 自己会用 PIL 解码，这里给对类型只是让它少一次猜测。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


def _ext_for(content_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(content_type, ".png")


# ---------------------------------------------------------------- 消息里的图
def _comp_kind(comp: Any) -> str:
    if isinstance(comp, dict):
        return str(comp.get("type") or "").lower()
    return type(comp).__name__.lower()


def _comp_data(comp: Any) -> dict:
    if isinstance(comp, dict):
        data = comp.get("data")
        return data if isinstance(data, dict) else {}
    return {}


def _image_source(comp: Any) -> str:
    """从一个 image 组件里取出可用的来源。

    这里有个坑：AstrBot 的 Image 组件在 aiocqhttp 下，`file` 常常只是
    `xxxx.image` 这种 QQ 缓存的文件名，不是能打开的路径。拿它去读盘必然失败。

    所以顺序是：
    1. `file`/`path` 且看起来像绝对路径 → 直接用（读盘比走 HTTP 快）
    2. `url` 是 http 链接 → 下载
    3. 都没有就只能试 `file`，赌它是相对路径
    """
    values: dict[str, str] = {}
    if isinstance(comp, dict):
        data = _comp_data(comp)
        for key in ("url", "file", "path"):
            value = data.get(key)
            if value:
                values[key] = str(value)
    else:
        for attr in ("url", "file", "path"):
            value = getattr(comp, attr, None)
            if value:
                values[attr] = str(value)

    def _looks_absolute(text: str) -> bool:
        if text.startswith(("/", "file://")):
            return True
        # Windows 盘符路径，自测和本地调试会用到
        return len(text) > 2 and text[1] == ":" and text[0].isalpha()

    for key in ("file", "path"):
        value = values.get(key, "")
        if value and _looks_absolute(value):
            return value
    url = values.get("url", "")
    if url.startswith(("http://", "https://")):
        return url
    for key in ("file", "path", "url"):
        if values.get(key):
            return values[key]
    return ""


def message_image_sources(event: Any) -> list[str]:
    """这条消息自带的图片（按出现顺序）。"""
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", None) or []
    sources: list[str] = []
    for comp in components:
        if _comp_kind(comp) != "image":
            continue
        source = _image_source(comp)
        if source:
            sources.append(source)
    return sources


def _reply_id(comp: Any) -> str:
    if isinstance(comp, dict):
        if _comp_kind(comp) != "reply":
            return ""
        data = _comp_data(comp)
        return str(data.get("id") or data.get("message_id") or "").strip()
    if _comp_kind(comp) != "reply":
        return ""
    for attr in ("id", "message_id", "reply_id"):
        value = getattr(comp, attr, None)
        if value:
            return str(value).strip()
    return ""


async def reply_image_sources(event: Any) -> list[str]:
    """引用消息里的图片。

    取不到就返回空列表 —— 引用一张纯文字消息是正常用法，
    不该因此让整个命令失败。
    """
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", None) or []
    reply_id = next((rid for rid in map(_reply_id, components) if rid), "")
    if not reply_id:
        return []

    bot = getattr(event, "bot", None)
    if bot is None:
        return []
    try:
        fetched = await bot.get_msg(message_id=int(reply_id))
    except Exception as exc:
        logger.debug(f"[群助手] 取引用消息失败: {exc}")
        return []

    payload = fetched
    if isinstance(fetched, dict) and isinstance(fetched.get("data"), dict):
        payload = fetched["data"]
    if not isinstance(payload, dict):
        return []

    sources: list[str] = []
    for comp in payload.get("message") or []:
        if _comp_kind(comp) != "image":
            continue
        source = _image_source(comp)
        if source:
            sources.append(source)
    return sources


async def all_image_sources(event: Any) -> list[str]:
    """引用图 + 本条消息的图。

    顺序有讲究：meme-generator 是按顺序吃图片的（第一张常被当成「被摸头的
    那个人」）。引用图放前面，因为「引用这张图 + 表情 xxx」是最常见的操作。
    """
    sources = await reply_image_sources(event)
    for source in message_image_sources(event):
        if source not in sources:
            sources.append(source)
    return sources


# ---------------------------------------------------------------- 下载
async def load_source(
    source: str, max_bytes: int, timeout: int = 15, session: aiohttp.ClientSession | None = None
) -> tuple[bytes, str, str]:
    """把一个来源变成 (字节, content_type, 文件名)。

    支持 http(s) 链接、base64:// 、file:// 和本地路径。
    超限直接抛错 —— 宁可让这张表情失败，也不要把内存吃光。
    """
    source = (source or "").strip()
    if not source:
        raise ValueError("图片来源是空的")

    if source.startswith("base64://"):
        raw = base64.b64decode(source[len("base64://"):])
        if len(raw) > max_bytes:
            raise ValueError(f"图片超过 {max_bytes // 1024 // 1024} MB")
        content_type = _sniff_type(raw)
        return raw, content_type, f"image{_ext_for(content_type)}"

    if source.startswith(("http://", "https://")):
        if aiohttp is None:
            raise ValueError("缺少 aiohttp，无法下载图片")
        owns_session = session is None
        active = session or aiohttp.ClientSession()
        try:
            async with active.get(
                source, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status >= 400:
                    raise ValueError(f"下载图片失败 HTTP {resp.status}")
                declared = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(32 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"图片超过 {max_bytes // 1024 // 1024} MB")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                content_type = _sniff_type(raw)
                if content_type == "application/octet-stream" and declared.startswith("image/"):
                    content_type = declared
                return raw, content_type, f"image{_ext_for(content_type)}"
        finally:
            if owns_session:
                await active.close()

    # 本地路径（含 file:// 前缀）
    path_text = source[7:] if source.startswith("file://") else source
    path = Path(path_text)
    if not path.is_file():
        raise ValueError(f"图片不存在：{source}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"图片超过 {max_bytes // 1024 // 1024} MB")
    raw = await asyncio.to_thread(path.read_bytes)
    content_type = _sniff_type(raw)
    return raw, content_type, f"image{_ext_for(content_type)}"


async def load_many(
    sources: list[str], max_bytes: int, timeout: int = 15
) -> tuple[list[tuple[bytes, str, str]], list[str]]:
    """并发下载一组来源。

    返回 (成功的图片列表, 每个来源对应的错误信息)。
    失败的不静默丢掉 —— 调用方要能说清楚「第几张图挂了」。
    """
    if not sources:
        return [], []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOAD)

    async def one(source: str) -> tuple[bytes, str, str] | Exception:
        async with semaphore:
            try:
                return await load_source(source, max_bytes, timeout)
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响其它
                return exc

    results = await asyncio.gather(*(one(source) for source in sources))
    images: list[tuple[bytes, str, str]] = []
    errors: list[str] = []
    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result) or type(result).__name__)
        else:
            images.append(result)
    return images, errors


def avatar_url(user_id: str) -> str:
    return QQ_AVATAR.format(uid=str(user_id).strip())


async def load_avatar(
    user_id: str, max_bytes: int, timeout: int = 10
) -> tuple[bytes, str, str] | None:
    """取一张 QQ 头像。拿不到就返回 None，让调用方决定是报错还是跳过。"""
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return None
    try:
        return await load_source(avatar_url(uid), max_bytes, timeout)
    except Exception as exc:
        logger.warning(f"[群助手] 取头像失败 {uid}: {exc}")
        return None
