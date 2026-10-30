"""插件管理台的 Web API。

AstrBot 从 4.16 起提供 ``Context.register_web_api()``：插件可以在 WebUI 里
挂自己的页面（``pages/<slug>/``）和一组 JSON 端点。这里每个方法只做
「取数据 → jsonify」，SQL 在 ``core/database.py``，规则在 ``core/checkin.py``。

三个刻意的设计
--------------

**1. 路由表是数据，不是散布在代码里的装饰器。**
``ROUTES`` 是个模块级元组，注册和自测都读它。这样「加了端点忘了注册」
或者「前端调了个不存在的端点」都能在自测里当场发现，不用等页面白屏。

**2. 注册失败只告警，不抛。**
``register_web_api`` 是 4.16 才有的能力。老版本上硬调会让插件加载失败，
用户连聊天功能都用不了 —— 为了一个管理页面废掉整个插件不划算。
探测不到就记一条日志，页面不出现，聊天照常。

**3. 全量 try/except 只在注册这一层包一次。**
每个 handler 写起来跟普通函数一样，不用自己 try。异常统一在这里收口成
``{"success": false}``，前端才能区分「真的没数据」和「后端炸了」——
否则一个 SQL 错误会显示成「本群还没有成员」，管理员会以为群是空的。
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Callable

try:  # 本地单测时没有 astrbot，退化成标准库 logging（和 database.py 同一套做法）
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

from . import settings
from .checkin import CheckinService
from .config import PluginConfig
from .database import Database
from .services import Permission

try:  # 本地自测没有装 quart，导不进来也要能让模块被 import
    from quart import jsonify, request
except Exception:  # pragma: no cover
    jsonify = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]


# (路径末段, 方法名, HTTP 方法, 说明)
#
# 前端通过宿主 bridge 调 ``apiGet("overview")``，宿主拼成
# ``/<plugin_name>/overview`` 发给后端，所以这里只写末段。
ROUTES: tuple[tuple[str, str, list[str], str], ...] = (
    ("overview", "overview", ["GET"], "全局概览数字"),
    ("groups", "groups", ["GET"], "群列表与各群今日活跃"),
    ("trend", "trend", ["GET"], "单群近 N 天发言与签到"),
    ("ranking", "ranking", ["GET"], "单群排行（连签 / 累计 / 额度）"),
    ("members", "members", ["GET"], "成员分页与搜索"),
    ("checkin-feed", "checkin_feed", ["GET"], "最近签到流水"),
    ("quota-feed", "quota_feed", ["GET"], "最近额度流水"),
    ("storage", "storage", ["GET"], "数据库体积与各表行数"),
    ("assets", "assets", ["GET"], "图片素材：三个目录与名片皮肤清单"),
    ("config", "config", ["GET"], "配置分组、字段与当前值"),
    ("commands", "commands", ["GET"], "全部口令与所需等级（按级分组）"),
    ("config-save", "config_save", ["POST"], "保存配置并落盘"),
    ("member-update", "member_update", ["POST"], "调整成员额度"),
)


def _arg(name: str, default: str = "") -> str:
    """读 query string。request 缺失时返回默认值，方便脱离 Web 环境调用。"""
    if request is None:
        return default
    try:
        return str(request.args.get(name, default) or default)
    except Exception:
        return default


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_arg(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def command_catalog(cfg: Any) -> dict[str, Any]:
    """全部口令，按所需等级分组。

    数据源是 ``domains/registry.py`` 的 ``COMMANDS`` —— 群里发「帮助」
    和「管理」读的是同一份。所以**这个页面永远不会出现「群里有、面板
    没有」**：加一个新功能只改注册表，这里自动跟上。

    以前面板上只有几个开关，管理员想知道「我能发什么口令」得去翻代码，
    这正是之前最要命的一处信息缺口。

    抽成模块级函数而不是写在 handler 里，是为了自测能直接断言内容 ——
    「口令表里有给额度、有发额度」这种话，只能真的读一遍数据才验得了。
    """
    from ..domains.registry import COMMANDS

    # 命令的 enabled 钩子只读 ctx.config，给个最小替身就够用，
    # 不需要构造完整的 PluginContext。
    probe = types.SimpleNamespace(config=cfg)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for cmd in COMMANDS:
        active = True
        hook = getattr(cmd, "enabled", None)
        if callable(hook):
            try:
                active = bool(hook(probe))
            except Exception:
                active = True
        for entry in getattr(cmd, "help", ()) or ():
            usage = str(getattr(entry, "usage", "") or "").strip()
            if not usage:
                continue
            level = int(getattr(entry, "level", 1) or 1)
            # 同一条用法可能在两页各挂一次，按 (等级, 用法) 去重
            if (level, usage) in seen:
                continue
            seen.add((level, usage))
            rows.append(
                {
                    "usage": usage,
                    "desc": str(getattr(entry, "desc", "") or ""),
                    "level": level,
                    "page": str(getattr(entry, "page", "base") or "base"),
                    "group": str(getattr(entry, "group", "") or ""),
                    "key": cmd.key,
                    "active": active,
                }
            )

    # 按等级分组：看这张表的人第一个问题永远是「我能不能用」。
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(row["level"], []).append(row)
    for items in buckets.values():
        items.sort(key=lambda item: (item["group"], item["usage"]))

    groups = [
        {
            "level": level,
            "title": Permission.level_name(level),
            "items": buckets[level],
            "count": len(buckets[level]),
        }
        for level in sorted(buckets)
    ]
    return {"groups": groups, "total": len(rows)}


class WebApi:
    """把数据库读接口翻译成 HTTP JSON。"""

    def __init__(
        self,
        db: Database,
        config: PluginConfig,
        plugin_name: str,
        checkin: CheckinService | None = None,
        plugin_dir: Any = None,
        images: Any = None,
    ):
        self.db = db
        # 注意：这里**不能**叫 self.config。
        # ROUTES 里有一个端点方法也叫 config()，实例属性会把方法遮蔽掉，
        # getattr(self, "config") 拿到的是这个配置对象，调用就报
        # `'PluginConfig' object is not callable` —— 线上真的踩过一次。
        self.cfg = config
        self.plugin_name = plugin_name
        # 配置的字段表从 _conf_schema.json 读，所以要拿到插件目录。
        # 允许为 None（自测/老调用）：那时配置页只是一个空页面，
        # 其余端点不受影响。
        self.plugin_dir = plugin_dir
        self.checkin = checkin
        # 图片素材库（utils/assets.py）。管理台的「素材」页靠它列出
        # 三个目录里各有几张、名片卖多少钱 —— 允许为 None。
        self.images = images
        self.context: Any = None
        self._registered: list[str] = []

    def _title(self, title_id: str) -> str:
        """内部 id → 中文名。

        数据库里存的是 ``nightwalker`` 这种英文 id，直接丢到页面上就是
        「内部字段名泄露」—— 和之前在聊天卡片上把插件名画进去是同一类错误。
        翻译只在展示层做，存储层不动。
        """
        if not title_id or self.checkin is None:
            return ""
        try:
            return CheckinService.title_name(title_id) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------ 注册 / 注销
    def register(self, context: Any) -> bool:
        """挂上全部路由。返回是否真的挂上了。"""
        if jsonify is None or request is None:
            logger.warning("[群助手] 当前环境没有 quart，管理台 API 跳过")
            return False

        hook = getattr(context, "register_web_api", None)
        if not callable(hook):
            logger.warning(
                "[群助手] 当前 AstrBot 不支持插件页面（需 4.16+），"
                "管理台不会出现在 WebUI 里；聊天功能不受影响"
            )
            return False

        self.context = context
        for path, method_name, methods, description in ROUTES:
            handler = getattr(self, method_name, None)
            # `callable` 这一层不是多余的：实例属性会遮蔽同名的类方法，
            # 遮蔽之后 getattr 拿到的是一个对象，注册上去要到用户点页面
            # 那一刻才炸。宁可注册时当场报出来。
            if not callable(handler):
                logger.error(
                    f"[群助手] 端点 {path} 的处理器 {method_name} 不可调用"
                    f"（拿到 {type(handler).__name__}），已跳过。"
                    f"多半是 __init__ 里的属性名和端点方法重名了"
                )
                continue
            route = f"/{self.plugin_name}/{path}"
            try:
                hook(route, self._guard(path, handler), list(methods), description)
            except Exception as exc:  # 单条失败不该带走其余端点
                logger.error(f"[群助手] 注册管理台路由 {route} 失败: {exc}")
                continue
            self._registered.append(route)

        if self._registered:
            logger.info(f"[群助手] 管理台已注册 {len(self._registered)} 个接口")
        return bool(self._registered)

    def unregister(self) -> None:
        """把本实例挂的路由摘掉。

        AstrBot 没有公开的 ``unregister_web_api()``，只能操作内部 registry。
        参考实现遇到结构不符会直接 raise —— 那会让插件卸载失败、热重载卡住。
        这里的判断反过来：**认不出来就只记日志放过**。少摘几条路由最多
        在重载后多留一份旧 handler，而卸载失败会让整个插件卡在加载态。
        """
        if not self._registered or self.context is None:
            return

        registry = getattr(self.context, "registered_web_apis", None)
        if not isinstance(registry, list):
            logger.warning("[群助手] 无法识别 AstrBot 的 Web API registry，跳过注销")
            return

        owned = set(self._registered)
        try:
            registry[:] = [
                item
                for item in registry
                if not (isinstance(item, (tuple, list)) and item and item[0] in owned)
            ]
        except Exception as exc:
            logger.warning(f"[群助手] 注销管理台路由失败: {exc}")
            return

        self._registered.clear()
        logger.info("[群助手] 管理台接口已注销")

    def _guard(self, label: str, handler: Callable[[], Any]) -> Callable[[], Any]:
        """统一兜异常。业务代码因此不需要自己 try。"""

        async def guarded():
            try:
                return await handler()
            except Exception as exc:
                logger.error(
                    f"[群助手] 管理台 {label} 失败: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                return jsonify(
                    {"success": False, "error": f"{label} 失败：{type(exc).__name__}"}
                )

        return guarded

    @staticmethod
    def _ok(**payload: Any):
        return jsonify({"success": True, **payload})

    # ------------------------------------------------------------ 只读端点
    async def overview(self):
        data = await asyncio.to_thread(self.db.web_overview)
        return self._ok(**data)

    async def groups(self):
        rows = await asyncio.to_thread(self.db.web_groups)
        return self._ok(groups=rows)

    async def trend(self):
        group_id = _arg("group_id")
        if not group_id:
            return self._ok(rows=[])
        days = _int_arg("days", 7, 3, 30)
        rows = await asyncio.to_thread(self.db.web_trend, group_id, days)
        return self._ok(rows=rows, days=days)

    async def ranking(self):
        group_id = _arg("group_id")
        if not group_id:
            return self._ok(rows=[], by="streak")
        by = _arg("by")
        if by == "quota":
            # 额度榜和签到场次不同源：一个看「攒了多少」，一个看「签了多少」。
            # 混在同一个查询里会漏掉从没签到但靠发言攒到钱的人。
            rows = await asyncio.to_thread(self.db.quota_ranking, group_id, 12)
            for row in rows:
                row["title_name"] = self._title(row.get("title_id") or "")
                # quota_ranking 是给人看的「榜」，不是给前端看的结构：它把
                # display_name / qq_name 两列原样带出来，挑哪个显示由调用方定。
                # 前端只有一列名字，所以这里替它挑一次数字号码兜底。
                row["name"] = str(
                    row.get("display_name") or row.get("qq_name") or row.get("user_id")
                )
            return self._ok(rows=rows, by="quota")
        if by == "message":
            # 发言榜是「资历」不是「日报」：读的是 members.total_messages，
            # 不受按天统计的保留期裁剪影响。
            rows = await asyncio.to_thread(self.db.message_ranking, group_id, 12)
            for row in rows:
                row["title_name"] = self._title(row.get("title_id") or "")
                row["name"] = str(
                    row.get("display_name") or row.get("qq_name") or row.get("user_id")
                )
            return self._ok(rows=rows, by="message")
        by = "total" if by == "total" else "streak"
        rows = await asyncio.to_thread(self.db.web_ranking, group_id, by, 12)
        for row in rows:
            row["title_name"] = self._title(row.get("title_id") or "")
        return self._ok(rows=rows, by=by)

    async def members(self):
        group_id = _arg("group_id")
        if not group_id:
            return self._ok(rows=[], total=0, offset=0, limit=50)
        page = await asyncio.to_thread(
            self.db.web_members,
            group_id,
            _arg("q"),
            _int_arg("offset", 0, 0, 1_000_000),
            _int_arg("limit", 50, 1, 200),
        )
        for row in page.get("rows", []):
            row["title_name"] = self._title(row.get("title_id") or "")
        return self._ok(**page)

    async def checkin_feed(self):
        rows = await asyncio.to_thread(
            self.db.web_checkin_feed, _arg("group_id"), _int_arg("limit", 40, 1, 200)
        )
        return self._ok(rows=rows)

    async def quota_feed(self):
        rows = await asyncio.to_thread(
            self.db.web_quota_feed, _arg("group_id"), _int_arg("limit", 40, 1, 200)
        )
        return self._ok(rows=rows)

    async def storage(self):
        data = await asyncio.to_thread(self.db.web_storage)
        return self._ok(**data)

    async def commands(self):
        """全部口令，按所需等级分组。汇总逻辑在 ``command_catalog``。"""
        data = await asyncio.to_thread(command_catalog, self.cfg)
        return self._ok(**data)

    async def assets(self):
        """图片素材清单。

        这一版插件的「配置」有相当一部分不在 JSON 里，而在三个文件夹里 ——
        加一张名片皮肤等于上架一件商品。所以面板必须能看见这几个目录：
        各自几张、路径在哪、名片叫什么卖多少。看不到的话，用户永远不知道
        「我把图放对了没有」，只能去问代码。
        """
        if self.images is None:
            return self._ok(root="", folders=[], skins=[], missing=True)

        def collect() -> dict[str, Any]:
            folders = [
                {
                    "key": key,
                    "title": title,
                    "note": note,
                    "count": len(self.images.files(key)),
                    "path": str(self.images.folder(key)),
                    "files": [path.name for path in self.images.files(key)][:12],
                }
                for key, title, note in (
                    ("checkin", "签到运势图", "发「签到」时按种子抽一张"),
                    ("catalog", "表情图鉴", "发「表情列表」时按文件名顺序发"),
                    ("cards", "个人名片", "男/女是基础名片，其余文件名就是商品名"),
                )
            ]
            # 基础名片（男/女）不在商品表里，但群主必须能看出「我放对了没有」——
            # 它决定了所有人的默认卡长什么样，缺一张就会有一半人用兜底色。
            base_cards = []
            for name in ("男", "女"):
                found = self.images.base_card(name)
                base_cards.append(
                    {
                        "name": name,
                        "filename": found.name if found else "",
                        "ready": found is not None,
                    }
                )
            return {
                "root": str(self.images.root),
                "folders": folders,
                "base_cards": base_cards,
                "skins": [
                    {
                        "name": skin.name,
                        "price": skin.price,
                        "filename": skin.filename,
                    }
                    for skin in self.images.skins()
                ],
            }

        return self._ok(**(await asyncio.to_thread(collect)))

    async def config(self):
        """配置页要的全部东西：分组、字段、当前值、实际生效值。

        字段表来自 ``_conf_schema.json``（见 ``core/settings.py``），
        前端不再自己抄一份 —— 抄一份的后果是「加了配置项，页面上改不到，
        用户以为配置就这么多」。
        """
        meta = (
            await asyncio.to_thread(settings.describe, self.cfg.raw, self.plugin_dir)
            if self.plugin_dir is not None
            else {"sections": [], "schema_keys": [], "field_count": 0}
        )
        return self._ok(
            **meta,
            effective=await asyncio.to_thread(settings.effective_values, self.cfg.raw),
            can_persist=self._saver() is not None,
            plugin_name=self.plugin_name,
            # 老版页面读这三个字段（浏览器会把 app.js 缓存一阵），
            # 留着它们只是为了让旧缓存不白屏，新页面不再用。
            currency=self.cfg.currency_name,
            checkin_enabled=bool(self.cfg.checkin_enabled),
            checkin_base=(self.cfg.checkin_base_min, self.cfg.checkin_base_max),
        )

    def _saver(self) -> Callable[[], Any] | None:
        """拿到 AstrBot 配置对象的落盘方法，没有就返回 None。

        ``PluginConfig.raw`` 是 AstrBot 注入的 ``AstrBotConfig``，它带
        ``save_config()``；自测里传的是普通 dict，没有这个方法 ——
        所以「改了但存不下来」必须能被告知，而不是假装保存成功。
        """
        saver = getattr(self.cfg.raw, "save_config", None)
        return saver if callable(saver) else None

    async def config_save(self):
        """写配置。

        两件事必须做对：
        ① **原地改** ``self.cfg.raw`` —— 属性每次都读 raw，所以改完立刻
           生效，不需要重启；
        ② 落盘失败要**回滚内存里的值**。只改内存不落盘，重启后配置回退
           而页面显示已保存，是最难排查的一种不一致。
        """
        body = await request.get_json(silent=True) or {}
        payload = body.get("values") if isinstance(body.get("values"), dict) else body
        if not isinstance(payload, dict) or not payload:
            return jsonify({"success": False, "error": "没有收到要保存的配置"})
        if self.plugin_dir is None:
            return jsonify({"success": False, "error": "插件目录未知，无法校验配置项"})

        snapshot = dict(self.cfg.raw)
        result = await asyncio.to_thread(
            settings.apply, self.cfg.raw, payload, self.plugin_dir
        )

        saver = self._saver()
        if saver is None:
            # 没有落盘能力时保留内存改动（本次运行有效），但必须说清楚。
            return self._ok(
                **result,
                persisted=False,
                warning="当前环境不支持写配置文件，改动只在本次运行有效",
            )

        try:
            await asyncio.to_thread(saver)
        except Exception as exc:
            self.cfg.raw.clear()
            self.cfg.raw.update(snapshot)
            logger.error(f"[群助手] 配置落盘失败，已回滚: {exc}", exc_info=True)
            return jsonify(
                {
                    "success": False,
                    "error": f"写入配置文件失败：{type(exc).__name__}",
                    "values": await asyncio.to_thread(
                        settings.effective_values, self.cfg.raw
                    ),
                }
            )

        logger.info(f"[群助手] 管理台保存配置：{len(result['changed'])} 项")
        return self._ok(**result, persisted=True)

    # ------------------------------------------------------------ 唯一写端点
    async def member_update(self):
        """调整某个成员的额度。

        只开这一个写口，而且**必须走 add_quota**：额度变动只有写进
        ``quota_logs`` 才算数，管理员事后能查到「谁在什么时候改的」。
        直接 UPDATE members.quota 会把这个人的余额变成一笔无来源的钱。

        连签天数、累计天数不支持在管理台改 —— 它们和 ``checkin_records``
        的快照是一套账，单改一处会让第二天签到算错加成。
        """
        body = await request.get_json(silent=True) or {}
        group_id = str(body.get("group_id") or "")
        user_id = str(body.get("user_id") or "")
        if not group_id or not user_id:
            return jsonify({"success": False, "error": "缺少 group_id / user_id"})

        try:
            delta = int(body.get("delta") or 0)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "delta 必须是整数"})
        if delta == 0:
            return jsonify({"success": False, "error": "delta 不能为 0"})
        # 单次上限挡住手滑多打一个 0，同时留足补偿空间。
        if abs(delta) > 100_000:
            return jsonify({"success": False, "error": "单次调整不能超过 100000"})

        ok, balance = await asyncio.to_thread(
            self.db.add_quota, group_id, user_id, delta, "管理台调整", "web"
        )
        if not ok:
            return jsonify(
                {"success": False, "error": f"余额不足，当前 {balance}", "balance": balance}
            )
        return self._ok(balance=balance, delta=delta)
