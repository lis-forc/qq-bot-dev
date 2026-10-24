"""meme-generator 的 HTTP 客户端，同时支持 Python 版和 Rust 版服务端。

**这个插件自己不会画图。** 真正绘图的是外面那个 meme-generator 服务
（默认监听 2233），我们只做三件事：

    拿到模板清单 → 问到某个模板要几个参数 → 把图和文字发过去换回一张图

服务端有两个流派，接口完全不同，这里启动时自动探测、结果缓存：

  Python 版（meetwq/meme-generator）
    GET  /memes                  → 模板列表
    GET  /memes/{key}/info       → 参数要求
    POST /memes/{key}/render     → 出图，multipart，一次拿回图片

  Rust 版（meetwq/meme-generator-rs）
    GET  /meme/keys              → 模板列表（只有 key）
    GET  /meme/infos             → 全部模板（带参数，优先用它）
    GET  /memes/{key}/info       → 单个模板参数
    POST /image/upload           → 先传图换 image_id
    POST /memes/{key}            → 提交生成，换另一个 image_id
    GET  /image/{id}             → 按 id 取图

两者的区别不只是路径：Python 版一次 multipart 就拿到图，Rust 版要走
「上传素材 → 提交生成 → 按 id 取图」三步。用户不需要配「我用的是哪个
版本」，探测两个路径哪个活就知道。

所以这里的报错必须说人话。最常见的问题不是代码错，而是 2233 端口上
根本没东西在跑，用户只会看到一句「制作失败」，然后完全不知道该干嘛。

缓存策略：模板清单 5 分钟、单个模板详情 10 分钟。表情模板属于
「装一次用半年」的东西，没必要每次做图都重新问一遍。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from astrbot.api import logger

# AstrBot 跑在 aiocqhttp 上，aiohttp 一定在。这里做可选导入只是为了让
# 自测脚本能在裸 Python 环境里跑起来 —— 没有 aiohttp 时给出人话提示，
# 而不是一个 ImportError 把整个插件加载干掉。
try:
    import aiohttp  # noqa: F811
except ImportError:  # pragma: no cover - 只影响裸环境
    aiohttp = None  # type: ignore[assignment]


class MemeApiError(Exception):
    """调用表情服务失败。

    message 会被直接发到群里，所以必须是人能看懂并且能据此行动的话。
    """


@dataclass
class MemeInfo:
    """一个模板的参数要求。"""

    key: str
    keywords: list[str] = field(default_factory=list)
    min_images: int = 0
    max_images: int = 0
    min_texts: int = 0
    max_texts: int = 0
    default_texts: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_image(self) -> bool:
        return self.max_images > 0

    def image_span(self) -> str:
        if self.min_images == self.max_images:
            return f"{self.min_images} 张图"
        return f"{self.min_images}~{self.max_images} 张图"

    def text_span(self) -> str:
        if self.max_texts == 0:
            return "不要文字"
        if self.min_texts == self.max_texts:
            return f"{self.min_texts} 段文字"
        return f"{self.min_texts}~{self.max_texts} 段文字"


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_info(key: str, payload: dict[str, Any]) -> MemeInfo:
    """把 API 返回的模板详情转成固定结构。

    字段名是个坑：meme-generator 把参数放在 `params_type` 里（不是 `params`），
    而不同版本的列表接口又会把参数直接摊在顶层。三处都读一遍，
    否则会把「需要 1 张图」读成「不需要图」，做出来的表情全是空的。
    """
    params: dict[str, Any] = {}
    for field_name in ("params_type", "params"):
        candidate = payload.get(field_name)
        if isinstance(candidate, dict):
            params.update(candidate)
    merged = {**payload, **params}

    raw_keywords = payload.get("keywords")
    if not raw_keywords:
        # Rust 版的字段名不一定叫 keywords，两个常见别名也认一下。
        # 少了关键词后果很直接：群友说「摸头」搜不到 petpet。
        raw_keywords = payload.get("names") or payload.get("tags") or []
    keywords = [
        str(item).strip()
        for item in _as_list(raw_keywords)
        if str(item).strip()
    ]
    if key not in keywords:
        keywords.insert(0, key)

    return MemeInfo(
        key=key,
        keywords=keywords,
        min_images=_as_int(merged.get("min_images"), 0),
        max_images=_as_int(merged.get("max_images"), 0),
        min_texts=_as_int(merged.get("min_texts"), 0),
        max_texts=_as_int(merged.get("max_texts"), 0),
        default_texts=[str(item) for item in _as_list(merged.get("default_texts"))],
        raw=payload,
    )


class MemeApi:
    """对 meme-generator 的薄封装。一个实例全程复用一条连接池。"""

    KEYS_TTL = 300
    INFO_TTL = 600

    def __init__(self, base_url: str, timeout: int = 30, max_bytes: int = 10 * 1024 * 1024):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = max(5, int(timeout))
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self._session: aiohttp.ClientSession | None = None
        # "rs" / "py"，第一次请求时探测出来并一直沿用
        self._proto: str | None = None
        self._catalog: dict[str, MemeInfo] = {}
        self._catalog_at = 0.0
        self._infos: dict[str, tuple[float, MemeInfo]] = {}

    # ---------------------------------------------------------------- 连接
    def _ensure_session(self) -> aiohttp.ClientSession:
        if aiohttp is None:
            raise MemeApiError(
                "缺少 aiohttp 依赖，没法调用表情服务\n"
                "在 AstrBot 的运行环境里执行：pip install aiohttp"
            )
        # 懒创建：第一次请求时已经在事件循环里，避免「no running loop」告警。
        # 不设默认超时，各接口的超时按请求单独传。
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _timeout(self, total: int | None = None) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=total or self.timeout, sock_connect=5, sock_read=total or self.timeout
        )

    def _unreachable(self, exc: Exception) -> MemeApiError:
        """连不上时给出可执行的建议，而不是把 aiohttp 的异常原样甩出去。"""
        hint = ""
        if "127.0.0.1" in self.base_url or "localhost" in self.base_url:
            hint = (
                "\n如果你的 AstrBot 也跑在 Docker 里，127.0.0.1 指的是容器自己，"
                "\n要改成宿主机在 Docker 内网里的地址，通常是 http://172.17.0.1:2233"
            )
        return MemeApiError(
            f"连不上表情服务（{self.base_url}）\n"
            "确认容器在跑：docker ps | grep meme\n"
            f"原始错误：{type(exc).__name__}: {exc}{hint}"
        )

    async def _get_json(self, path: str) -> Any:
        session = self._ensure_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.get(url, timeout=self._timeout()) as resp:
                data = await resp.read()
                if resp.status == 404:
                    raise MemeApiError(f"表情服务里没有这个模板：{path}")
                if resp.status >= 400:
                    text = data.decode("utf-8", errors="replace")[:300]
                    raise MemeApiError(f"表情服务返回 HTTP {resp.status}：{text}")
                try:
                    return json.loads(data.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise MemeApiError(f"表情服务返回的不是 JSON（{path}）：{exc}") from exc
        except MemeApiError:
            raise
        except Exception as exc:  # 网络层
            raise self._unreachable(exc) from exc

    async def _get_json_soft(self, path: str) -> tuple[int, Any]:
        """GET 一个 JSON，不抛异常，返回 (状态码, 内容)。

        只服务于协议探测：404 也是一种有效答案，不能让它变成异常。
        连不上时状态码给 0。
        """
        session = self._ensure_session()
        try:
            async with session.get(f"{self.base_url}{path}", timeout=self._timeout()) as resp:
                raw = await resp.read()
                status = resp.status
        except Exception:
            return 0, None
        try:
            return status, json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return status, None

    async def _post_json(
        self, path: str, body: dict[str, Any], timeout: int | None = None
    ) -> tuple[int, Any]:
        """POST 一个 JSON 体。Rust 版的接口全是这种风格。"""
        session = self._ensure_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.post(url, json=body, timeout=self._timeout(timeout)) as resp:
                raw = await resp.read()
                text = raw.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = None
                if resp.status >= 400:
                    # Rust 版错误体：{"code":550,"message":"...","data":{...}}
                    message = ""
                    if isinstance(payload, dict):
                        message = str(payload.get("message") or payload.get("error") or "")
                        detail = payload.get("data")
                        if detail:
                            message = f"{message}（{json.dumps(detail, ensure_ascii=False)}）"
                    raise MemeApiError(f"表情服务报错：{message or text[:200]}")
                return resp.status, payload
        except MemeApiError:
            raise
        except Exception as exc:
            raise self._unreachable(exc) from exc

    async def _detect(self) -> str:
        """判断对面是 Python 版还是 Rust 版，探测一次就缓存。

        Rust 版没有 `/memes`（返回 404），Python 版没有 `/meme/keys`。
        各打一枪，谁活着就是谁。用户不用配「我装的是哪个版本」。
        """
        if self._proto:
            return self._proto

        status, _ = await self._get_json_soft("/meme/keys")
        if status == 200:
            self._proto = "rs"
        else:
            py_status, _ = await self._get_json_soft("/memes")
            if py_status == 200:
                self._proto = "py"
            elif py_status == 0:
                raise MemeApiError(
                    f"连不上表情服务（{self.base_url}）\n"
                    "确认容器在跑：docker ps | grep meme"
                )
            else:
                raise MemeApiError(
                    f"表情服务的接口对不上（{self.base_url}）\n"
                    f"/meme/keys 返回 {status}，/memes 返回 {py_status}\n"
                    "确认装的是 meme-generator 或 meme-generator-rs"
                )
        logger.info(f"[群助手] 表情服务协议：{self._proto}")
        return self._proto

    # ---------------------------------------------------------------- 模板清单
    async def catalog(self, force: bool = False) -> dict[str, MemeInfo]:
        """全部模板，key → MemeInfo。带缓存，force=True 时强制刷新。

        `/memes` 在不同版本里返回的东西不一样：
        - 老版本：`["摸头", "亲亲", ...]` 纯 key 列表
        - 新版本：`[{key, keywords, params, ...}, ...]` 完整清单

        这里统一解析成 MemeInfo。纯 key 列表的情况下参数全是 0，
        不能拿去校验 —— 制作流程会再单独调 `info()` 问准确的数字。
        """
        now = time.time()
        if not force and self._catalog and now - self._catalog_at < self.KEYS_TTL:
            return dict(self._catalog)

        proto = await self._detect()
        if proto == "rs":
            # 优先 /meme/infos：它一次把全部模板带参数吐出来，省掉几百次
            # 单查。拿不到再退回 /meme/keys（只有 key，没有关键词，搜索会难用）。
            status, payload = await self._get_json_soft("/meme/infos")
            if status != 200 or not payload:
                status, payload = await self._get_json_soft("/meme/keys")
                if status != 200:
                    raise MemeApiError(f"取模板列表失败（HTTP {status}）")
            if isinstance(payload, dict):
                for name in ("infos", "memes", "keys", "data", "result"):
                    candidate = payload.get(name)
                    if isinstance(candidate, list):
                        payload = candidate
                        break
        else:
            payload = await self._get_json("/memes")
            if isinstance(payload, dict):
                payload = payload.get("memes") or payload.get("data") or payload.get("keys")
        if not isinstance(payload, list):
            raise MemeApiError("表情服务返回的模板列表格式不对")

        catalog: dict[str, MemeInfo] = {}
        for item in payload:
            if isinstance(item, dict):
                key = str(item.get("key") or item.get("meme_key") or "").strip()
            else:
                key = str(item).strip()
            if not key or key in catalog:
                continue
            catalog[key] = _parse_info(key, item) if isinstance(item, dict) else MemeInfo(key=key, keywords=[key])

        if not catalog:
            raise MemeApiError(
                "表情服务里一个模板都没有\n"
                "容器需要挂载 memes 目录，否则模板库是空的"
            )
        self._catalog = catalog
        self._catalog_at = now
        logger.info(f"[群助手] 表情模板已加载：{len(catalog)} 个")
        return dict(catalog)

    @staticmethod
    def find(query: str, catalog: dict[str, MemeInfo]) -> MemeInfo | None:
        """按用户输入找模板。精确优先，其次关键词，最后包含匹配。

        QQ 群里没人会去记 `petpet` 这种 key，大家只会说「摸头」。
        所以关键词优先级要高于「key 里含这几个字」。
        """
        query = (query or "").strip().lower()
        if not query:
            return None
        if query in catalog:
            return catalog[query]
        for info in catalog.values():
            if query in {kw.lower() for kw in info.keywords}:
                return info
        for info in catalog.values():
            if query in info.key.lower():
                return info
        for info in catalog.values():
            if any(query in kw.lower() for kw in info.keywords):
                return info
        return None

    @staticmethod
    def search(query: str, catalog: dict[str, MemeInfo], limit: int = 20) -> list[MemeInfo]:
        """关键词搜索，返回最多 limit 条。"""
        query = (query or "").strip().lower()
        if not query:
            return []
        hits: list[MemeInfo] = []
        for info in catalog.values():
            haystack = [info.key.lower()] + [kw.lower() for kw in info.keywords]
            if any(query in item for item in haystack):
                hits.append(info)
                if len(hits) >= limit:
                    break
        return hits

    async def info(self, key: str, force: bool = False) -> MemeInfo:
        """单个模板的参数要求。带缓存。"""
        now = time.time()
        cached = self._infos.get(key)
        if cached and not force and now - cached[0] < self.INFO_TTL:
            return cached[1]

        payload = await self._get_json(f"/memes/{quote(key, safe='')}/info")
        if not isinstance(payload, dict):
            raise MemeApiError(f"模板 {key} 的详情格式不对")
        info = _parse_info(key, payload)
        self._infos[key] = (now, info)
        return info

    async def preview(self, key: str) -> tuple[bytes, str]:
        """模板预览图。列表里给人看图比给一串 key 直观。"""
        return await self._get_image(f"/memes/{quote(key, safe='')}/preview")

    async def _get_image(self, path: str) -> tuple[bytes, str]:
        session = self._ensure_session()
        try:
            async with session.get(f"{self.base_url}{path}", timeout=self._timeout()) as resp:
                if resp.status >= 400:
                    text = (await resp.read()).decode("utf-8", errors="replace")[:300]
                    raise MemeApiError(f"取图片失败 HTTP {resp.status}：{text}")
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                data = await self._read_limited(resp)
                if not content_type.startswith("image/"):
                    raise MemeApiError(f"预览图返回的不是图片：{content_type or '未知类型'}")
                return data, content_type
        except MemeApiError:
            raise
        except Exception as exc:
            raise self._unreachable(exc) from exc

    async def _read_limited(self, resp: aiohttp.ClientResponse) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > self.max_bytes:
                raise MemeApiError(
                    f"表情结果超过 {self.max_bytes // 1024 // 1024} MB，已中断\n"
                    "调大配置里的图片上限，或换个轻一点的模板"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    # ---------------------------------------------------------------- 出图
    async def render(
        self,
        key: str,
        images: list[tuple[bytes, str, str]],
        texts: list[str],
        user_infos: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> tuple[bytes, str]:
        """渲染一张表情，返回 (图片字节, content_type)。

        两个版本走完全不同的路，但对调用方表现一致：

        Python 版一次 multipart 拿回图片；Rust 版要上传素材 → 提交生成
        → 按 id 取图。传参里有 user_infos 和 options，Python 版塞进 args，
        Rust 版只认 options。
        """
        proto = await self._detect()
        if proto == "rs":
            return await self._render_rs(key, images, texts, options, timeout)

        # ↓↓↓ 以下是 Python 版：multipart，一次换回图片
        form = aiohttp.FormData()
        for data, content_type, filename in images:
            form.add_field("images", data, filename=filename, content_type=content_type)
        for text in texts:
            form.add_field("texts", str(text))

        args: dict[str, Any] = {"user_infos": list(user_infos or [])}
        if options:
            args.update(options)
        form.add_field("args", json.dumps(args, ensure_ascii=False))

        session = self._ensure_session()
        url = f"{self.base_url}/memes/{quote(key, safe='')}/render"
        try:
            async with session.post(url, data=form, timeout=self._timeout(timeout)) as resp:
                if resp.status == 404:
                    raise MemeApiError(f"表情服务里没有模板：{key}")
                if resp.status >= 400:
                    text = (await resp.read()).decode("utf-8", errors="replace")[:300]
                    raise MemeApiError(f"渲染失败 HTTP {resp.status}：{text}")
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                data = await self._read_limited(resp)
                if not content_type.startswith("image/"):
                    text = data.decode("utf-8", errors="replace")[:300]
                    raise MemeApiError(f"渲染返回的不是图片：{text}")
                return data, content_type
        except MemeApiError:
            raise
        except Exception as exc:
            raise self._unreachable(exc) from exc

    async def _render_rs(
        self,
        key: str,
        images: list[tuple[bytes, str, str]],
        texts: list[str],
        options: dict[str, Any] | None,
        timeout: int | None,
    ) -> tuple[bytes, str]:
        """Rust 版的三段式渲染。

        1. 每张图先 `POST /image/upload`（base64 模式），换一个 image_id
        2. `POST /memes/{key}` 提交素材 id、文字、选项，换结果 id
        3. `GET /image/{结果 id}` 取图

        user_infos 这里刻意不传：Rust 版没有这个参数，硬塞会被当成
        未知选项拒掉（570 参数错误），结果就是一张图都做不出来。
        """
        ready: list[dict[str, str]] = []
        for index, (data, _content_type, filename) in enumerate(images, start=1):
            body = {"type": "data", "data": base64.b64encode(data).decode("ascii")}
            _status, payload = await self._post_json("/image/upload", body, timeout)
            image_id = ""
            if isinstance(payload, dict):
                image_id = str(payload.get("image_id") or payload.get("id") or "")
            if not image_id:
                raise MemeApiError("上传表情素材失败：服务没返回 image_id")
            ready.append({"name": filename or f"image{index}", "id": image_id})

        _status, payload = await self._post_json(
            f"/memes/{quote(key, safe='')}",
            {
                "images": ready,
                "texts": [str(item) for item in texts],
                "options": dict(options or {}),
            },
            timeout,
        )
        result_id = ""
        if isinstance(payload, dict):
            result_id = str(payload.get("image_id") or payload.get("id") or "")
        if not result_id:
            raise MemeApiError(f"生成表情失败：服务没返回图片 id（{key}）")

        return await self._get_image(f"/image/{quote(result_id, safe='')}")
