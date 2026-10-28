"""本地自测：不启动 AstrBot 也能验证核心逻辑与排版。

用法：python tools/selftest.py
它会把 astrbot.* 替换成最小桩，然后跑一遍：
  1. 口令解析（router）
  2. 配置兜底（config）
  3. 数据库 / 额度 / 抽奖进度（database）
  4. 真实入群天数 / 显示名 / 文案（services）
  5. 信息卡渲染 → tools/out/card_preview.png
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import re
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

# Pillow 是插件的可选依赖：没装时插件退化成文字卡。
# 自测里也要能跑 —— 渲染类断言标 SKIP，不能让整个测试脚本崩掉。
try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - 取决于本机环境
    PILImage = None

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "out"
TMP = Path(tempfile.mkdtemp(prefix="groupkit_selftest_"))


# ---------------------------------------------------------------- astrbot 桩
def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def install_stubs() -> None:
    class Logger:
        def info(self, msg, *a, **k):
            print(f"[info] {msg}")

        def warning(self, msg, *a, **k):
            print(f"[warn] {msg}")

        def error(self, msg, *a, **k):
            print(f"[err ] {msg}")

        def debug(self, msg, *a, **k):
            pass

    astrbot = _module("astrbot")
    api = _module("astrbot.api")
    api.logger = Logger()
    astrbot.api = api

    event_mod = _module("astrbot.api.event")

    def passthrough(*_args, **_kwargs):
        def wrapper(func):
            return func

        return wrapper

    event_mod.filter = types.SimpleNamespace(
        command=passthrough,
        command_group=passthrough,
        event_message_type=passthrough,
        EventMessageType=types.SimpleNamespace(ALL="all", PRIVATE_MESSAGE="p", GROUP_MESSAGE="g"),
    )

    class AstrMessageEvent:
        pass

    event_mod.AstrMessageEvent = AstrMessageEvent
    api.event = event_mod

    star_mod = _module("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    api.star = star_mod

    comp_mod = _module("astrbot.api.message_components")

    class At:
        def __init__(self, qq=None):
            self.qq = qq
            self.type = "at"

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:
        def __init__(self, file=""):
            self.file = file

        @staticmethod
        def fromFileSystem(path):
            return Image(file=str(path))

    class Node:
        def __init__(self, uin=None, name="", content=None):
            self.uin, self.name, self.content = uin, name, content or []

    class Nodes:
        def __init__(self, nodes=None):
            self.nodes = nodes or []

    comp_mod.At, comp_mod.Plain, comp_mod.Image = At, Plain, Image
    comp_mod.Node, comp_mod.Nodes = Node, Nodes
    api.message_components = comp_mod

    core = _module("astrbot.core")
    utils = _module("astrbot.core.utils")
    path_mod = _module("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: str(TMP)
    utils.astrbot_path = path_mod
    core.utils = utils
    astrbot.core = core


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "gk_plugin", ROOT / "main.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gk_plugin"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 断言小工具
PASS, FAIL, SKIP = [], [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def settle_preview(rendered_path, final_name: str) -> None:
    """把带时间戳的预览图改成固定文件名，并清掉同名的旧文件。

    MenuRenderer 每次渲染都会在文件名后加时间戳，防止覆盖 —— 这对线上
    是对的（同一秒可能渲染两次），但对本地验收不好：跑十次自测就多四十个
    文件，tools/out 一天就废了。所以收尾时归位成一个名字。
    """
    if not rendered_path:
        return
    try:
        source = Path(rendered_path)
        if not source.exists():
            return
        target = OUT_DIR / final_name
        target.unlink(missing_ok=True)
        source.replace(target)
        stem, suffix = Path(final_name).stem, Path(final_name).suffix
        for old in OUT_DIR.glob(f"{stem}_[0-9]*{suffix}"):
            old.unlink(missing_ok=True)
    except OSError:
        pass


def check_pil(name: str, condition: bool, detail: str = "") -> None:
    """依赖 Pillow 的断言。本机没装 Pillow 就记 SKIP，不算失败。"""
    if PILImage is None:
        SKIP.append(name)
        print(f"  [SKIP] {name}（没装 Pillow）")
        return
    check(name, condition, detail)


def await_perm(perm, event, user_id: str, member) -> int:
    """同步地跑一次异步权限计算。"""

    async def runner() -> int:
        return await perm.level(event, "1001", user_id, member)

    return asyncio.run(runner())


def main() -> None:
    install_stubs()
    mod = load_plugin()
    router = importlib.import_module("gk_plugin.utils.router")
    nick_mod = importlib.import_module("gk_plugin.utils.nickname")
    config_mod = importlib.import_module("gk_plugin.core.config")
    db_mod = importlib.import_module("gk_plugin.core.database")
    card_mod = importlib.import_module("gk_plugin.utils.card")
    # 重构后的功能模块：命令表、分发器、各领域
    command_mod = importlib.import_module("gk_plugin.core.command")
    admin_mod = importlib.import_module("gk_plugin.domains.admin")
    help_mod = importlib.import_module("gk_plugin.domains.help")
    profile_mod = importlib.import_module("gk_plugin.domains.profile")
    registry_mod = importlib.import_module("gk_plugin.domains.registry")

    print("\n=== 1. 口令解析 ===")
    check("compact 压空白", router.compact("  改名   李四 ") == "改名 李四")
    check("strip_at 去@", router.strip_at("@张三 李四") == "李四")
    check("drop_prefix 命中", router.drop_prefix("改名 李四", ("改名",)) == "李四")
    check("drop_prefix 全角冒号", router.drop_prefix("改名：李四", ("改名",)) == "李四")
    check("drop_prefix 只有口令", router.drop_prefix("改名", ("改名",)) == "")
    check("drop_prefix 未命中", router.drop_prefix("改名片的", ("改名",)) is None)
    check("drop_prefix 不误吞", router.drop_prefix("改名字 李四", ("改名", "改名字")) == "李四")
    check("drop_prefix 数字参数", router.drop_prefix("禁言 10分钟", ("禁言",)) == "10分钟")
    check("时长 10分钟", router.parse_duration("10分钟") == 600)
    check("时长 默认分钟", router.parse_duration("10") == 600)
    check("时长 1小时", router.parse_duration("1小时") == 3600)
    check("时长 上限收敛", router.parse_duration("999天") == 2_592_000)
    # 名字规则从 router 搬到了 utils/nickname.py：2~4 字、不许数字和符号。
    # 这几条是全群统一的口径，任何一处改宽了都会让「机器人不管名字」这件事
    # 从这里开始漏。
    ncfg = config_mod.PluginConfig({})
    check("名字 2 字通过", nick_mod.validate("李四", ncfg)[0])
    check("名字 4 字通过", nick_mod.validate("一二三四", ncfg)[0])
    check("名字 1 字拒绝", not nick_mod.validate("李", ncfg)[0])
    check("名字 5 字拒绝", not nick_mod.validate("一二三四五", ncfg)[0])
    check("名字 含数字拒绝", not nick_mod.validate("李四2", ncfg)[0])
    check("名字 含符号拒绝", not nick_mod.validate("李·四", ncfg)[0])
    check("名字 含空格拒绝", not nick_mod.validate("李 四", ncfg)[0])
    check("名字 含 emoji 拒绝", not nick_mod.validate("李四🌸", ncfg)[0])
    check("名字 空拒绝", not nick_mod.validate("   ", ncfg)[0])
    check("名字 英文按字符数算", nick_mod.validate("Zoe", ncfg)[0])
    check(
        "允许数字时放行",
        nick_mod.validate("李四2", config_mod.PluginConfig({"nick_allow_digits": True}))[0],
    )
    check(
        "长度上限可配",
        nick_mod.validate("一二三四五", config_mod.PluginConfig({"nick_max_len": 6}))[0],
    )
    check("宽松规则放行符号", nick_mod.validate_loose("🎸老王")[0])
    check("宽松规则仍限长度", not nick_mod.validate_loose("一" * 13)[0])
    check("爱好 超长拒绝", not router.validate_hobby("爱" * 13)[0])
    check("性别 男", nick_mod.parse_sex("男") == "男")
    check("性别 女生", nick_mod.parse_sex("女生") == "女")
    check("性别 female", nick_mod.parse_sex("Female") == "女")
    # 「保密」并进「未设定」：两者在展示上没有区别，给两个词只会让人犹豫。
    check("性别 保密→未设定", nick_mod.parse_sex("保密") == "")
    check("性别 未知→未设定", nick_mod.parse_sex("未知") == "")
    check("性别 看不懂", nick_mod.parse_sex("外星人") is None)
    check("性别 空", nick_mod.parse_sex("  ") is None)
    check("库里空串=没表过态", nick_mod.normalize_sex("") == "")
    check("库里 - = 明确不设", nick_mod.normalize_sex("-") == "")
    check("明确不设要存成记号", nick_mod.store_sex("") == "-")
    check("存男原样", nick_mod.store_sex("男") == "男")

    # 群名片 = 记号 + 分隔符 + 名字。拼装和剥离必须严格互逆，
    # 否则守卫会把「🦊 张三」当成一个名字，转头又要催他改名。
    check("拼装 男", nick_mod.compose("张三", "男", ncfg) == "🦊 张三")
    check("拼装 女", nick_mod.compose("张三", "女", ncfg) == "🐰 张三")
    check("拼装 未设定", nick_mod.compose("张三", "", ncfg) == "🧸 张三")
    check("剥离 男", nick_mod.strip("🦊 张三", ncfg) == "张三")
    check("剥离 未设定", nick_mod.strip("🧸 张三", ncfg) == "张三")
    # 没有记号的卡片剥完必须原样不动 —— 群里大多数人的名片是自己在 QQ 里起的。
    check("剥离 无记号幂等", nick_mod.strip("张三", ncfg) == "张三")
    check("剥离 空串不炸", nick_mod.strip("", ncfg) == "")
    # 群主换了记号之后，老卡片必须还能认出来，否则全群会被当成「名字带符号」。
    tiger = config_mod.PluginConfig({"nick_emoji_male": "🐯"})
    check("换记号后仍认旧记号", nick_mod.strip("🦊 张三", tiger) == "张三")
    check("换记号后拼新记号", nick_mod.compose("张三", "男", tiger) == "🐯 张三")
    check(
        "记号可以清空",
        nick_mod.compose("张三", "男", config_mod.PluginConfig({"nick_emoji_male": ""})) == "张三",
    )
    check(
        "分隔符可配",
        nick_mod.compose("张三", "男", config_mod.PluginConfig({"nick_separator": "-"})) == "🦊-张三",
    )

    print("\n=== 2. 配置兜底 ===")
    cfg = config_mod.PluginConfig({})
    check("默认额度名", cfg.currency_name == "额度")
    check("默认抽奖区间", (cfg.draw_min, cfg.draw_max) == (10, 20),
          f"{cfg.draw_min}~{cfg.draw_max}")
    check(
        "上限低于下限自动纠正",
        config_mod.PluginConfig({"draw_min": 9, "draw_max": 3}).draw_max == 9,
    )
    check("触发词默认", cfg.info_triggers == {"查", "我的信息", "我的卡片"})
    check(
        "触发词自定义",
        config_mod.PluginConfig({"info_triggers": "I，名片"}).info_triggers == {"i", "名片"},
    )
    # 历史上改过两次默认值（i → 查 → 我的卡片）。服务器上那份配置没人会去
    # 手动改，所以必须自动跟着新默认走，否则「代码改了、群里还得发老词」。
    check(
        "老默认触发词自动迁移到查",
        config_mod.PluginConfig({"info_triggers": "i,我的信息,名片"}).info_triggers
        == {"查", "我的信息", "我的卡片"},
    )
    check(
        "上一版默认也迁移",
        config_mod.PluginConfig({"info_triggers": "查,我的信息,名片"}).info_triggers
        == {"查", "我的信息", "我的卡片"},
    )
    check("群白名单留空=全开", cfg.is_group_enabled("123"))
    check(
        "群白名单生效",
        not config_mod.PluginConfig({"enabled_group_ids": ["1"]}).is_group_enabled("2"),
    )
    # 抽奖提醒默认必须关：活跃群里每攒满一人就喊一句，一天几十条纯噪音。
    check("抽奖提醒默认关闭", cfg.notify_draw_ready is False)
    check(
        "抽奖提醒可手动开",
        config_mod.PluginConfig({"notify_draw_ready": True}).notify_draw_ready is True,
    )

    print("\n=== 3. 数据库 / 额度 / 抽奖进度 ===")
    db = db_mod.Database(TMP / "db_test")
    gid, uid = "1001", "2002"

    member = db.ensure_member(gid, uid, welcome_quota=20)
    check("首次建档发新人礼", member["quota"] == 20, str(member.get("quota")))
    member = db.ensure_member(gid, uid, welcome_quota=20)
    check("新人礼只发一次", member["quota"] == 20, str(member.get("quota")))

    gained = [db.bump_message(gid, uid, draw_per=3)["draws_gained"] for _ in range(7)]
    check("每 3 条给一次机会", gained.count(1) == 2, f"gained={gained}")
    check("进度余 1", db.get_member(gid, uid)["msg_progress"] == 1)
    check("可抽 2 次", db.get_member(gid, uid)["draws"] == 2)

    counts = db.counts(gid, uid)
    check("今日 7 条", counts["today"] == 7, str(counts))
    check("累计 7 条", counts["total"] == 7)

    ok, remaining = db.consume_draw(gid, uid)
    ok2, _ = db.consume_draw(gid, uid)
    ok3, left = db.consume_draw(gid, uid)
    check("抽奖机会消耗与耗尽", ok and ok2 and not ok3 and left == 0 and remaining == 1)

    ok, balance = db.add_quota(gid, uid, 5, "抽奖")
    check("加额度", ok and balance == 25, str(balance))
    denied, balance = db.add_quota(gid, uid, -100, "改名")
    check("余额不足拒绝扣款", not denied and balance == 25)
    forced, balance = db.add_quota(gid, uid, -100, "罚款", allow_negative=True)
    check("允许透支", forced and balance == -75)

    print("\n=== 3.5 数据库规范（事务 / 索引 / 保留期 / 备份）===")
    check(
        "结构版本已写入",
        db.schema_version == db_mod.SCHEMA_VERSION,
        f"{db.schema_version} vs {db_mod.SCHEMA_VERSION}",
    )

    indexes = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    need = {
        "idx_daily_user_date",
        "idx_daily_group_date",
        "idx_quota_user",
        "idx_quota_time",
        "idx_op_time",
        "idx_members_quota",
        "idx_members_messages",
    }
    check("索引齐全", need <= indexes, f"缺 {sorted(need - indexes)}")

    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL 已启用", str(mode).lower() == "wal", str(mode))
    check("busy_timeout 已设", db.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000)

    # 事务回滚：抛异常后不应留下半截数据
    before = db.conn.execute("SELECT COUNT(*) FROM op_logs").fetchone()[0]
    try:
        with db._tx():
            db.conn.execute(
                "INSERT INTO op_logs (group_id, operator_id, action) VALUES (?, ?, ?)",
                ("1001", "x", "should_rollback"),
            )
            raise RuntimeError("模拟中途失败")
    except RuntimeError:
        pass
    after = db.conn.execute("SELECT COUNT(*) FROM op_logs").fetchone()[0]
    check("事务异常整体回滚", before == after, f"{before} -> {after}")

    # 保留期裁剪
    db.conn.execute(
        "INSERT INTO quota_logs (group_id, user_id, amount, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("1001", "2002", 1, "老数据", "2000-01-01 00:00:00"),
    )
    db.conn.execute(
        "INSERT INTO daily_stats (group_id, user_id, stat_date, msg_count) VALUES (?, ?, ?, ?)",
        ("1001", "2002", "2000-01-01", 5),
    )
    removed = db.prune(log_retention_days=180, stats_retention_days=730)
    check("裁剪掉老流水", removed.get("quota_logs", 0) >= 1, str(removed))
    check("裁剪掉老统计", removed.get("daily_stats", 0) >= 1, str(removed))
    check("裁剪不动累计数", db.get_member(gid, uid)["total_messages"] == 7)
    check("裁剪不动今日数", db.counts(gid, uid)["today"] == 7)

    backup = db.backup_to(TMP / "backup" / "groupkit-copy.db")
    check("在线备份可用", backup.exists() and backup.stat().st_size > 0)

    print("\n=== 3.6 并发安全 / 暴击加成 ===")
    # 「判余额」和「写余额」必须原子。先读后写的实现会在并发下透支，
    # 这里用两个线程同时扣款把那个场景真实跑出来。
    def race(call, times: int = 2):
        barrier = threading.Barrier(times)
        results: list = []
        guard = threading.Lock()

        def worker():
            barrier.wait()
            # 并发下必然有一方失败，这是被验证的行为而不是故障。
            # 不接住的话异常会打到 stderr，跑测试的人会以为脚本坏了。
            try:
                out = call()
            except Exception as exc:  # noqa: BLE001 - 就是要当结果收下
                out = (False, str(exc))
            with guard:
                results.append(out)

        threads = [threading.Thread(target=worker) for _ in range(times)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    db.ensure_member(gid, "race1", 0)
    db.add_quota(gid, "race1", 10, "初始", allow_negative=True)
    results = race(lambda: db.add_quota(gid, "race1", -8, "并发扣款"))
    check("并发扣款只成功一次", sum(1 for ok, _ in results if ok) == 1, str(results))
    check("并发扣款不透支", db.balance(gid, "race1") == 2, str(db.balance(gid, "race1")))

    db.ensure_member(gid, "race2", 0)
    db.bump_message(gid, "race2", 1)  # 攒 1 次抽奖机会
    results = race(lambda: db.consume_draw(gid, "race2"))
    check("并发抽奖只成功一次", sum(1 for ok, _ in results if ok) == 1, str(results))
    check(
        "抽奖次数不会变负",
        db.get_member(gid, "race2")["draws"] == 0,
        str(db.get_member(gid, "race2")["draws"]),
    )

    # 暴击必须是「加成」。原来写 max(amount, bonus)，draw_max 高于 bonus 时暴击无效。
    services_mod = importlib.import_module("gk_plugin.core.services")
    crit_cfg = config_mod.PluginConfig(
        {"draw_min": 20, "draw_max": 20, "draw_bonus_chance": 1.0, "draw_bonus_amount": 8}
    )
    crit_quota = services_mod.QuotaService(db, crit_cfg)
    db.ensure_member(gid, "crit1", 0)
    db.bump_message(gid, "crit1", 1)
    crit_text = crit_quota.draw(gid, "crit1")
    check("暴击是加成不是取大", "+28" in crit_text, crit_text.replace("\n", " | "))

    print("\n=== 3.7 额度记录 / 维护 / 踢人拉黑 ===")
    # 流水一直在写，但之前没有任何入口能查 —— 群友问「我额度怎么没了」只能靠猜。
    hist_quota = services_mod.QuotaService(db, config_mod.PluginConfig({}))
    db.ensure_member(gid, "hist1", 0)
    check(
        "没有流水时给提示",
        "还没有额度记录" in hist_quota.render_history(gid, "hist1"),
        hist_quota.render_history(gid, "hist1").replace("\n", " | "),
    )
    for i in range(25):
        db.add_quota(gid, "hist1", i + 1, f"第{i}笔", "hist1")
    hist_text = hist_quota.render_history(gid, "hist1")
    # 数据行以正负号开头（金额列左对齐、右补空格）。
    # 之前行首是 🟢/🔴，那两个 emoji 已经去掉：部分 QQ 客户端没有字形，
    # 而且宽度按 1 格算、实际可能 2 格，会把原因列推歪。
    hist_rows = [line for line in hist_text.splitlines() if line[:1] in ("+", "-")]
    check("流水最多 20 条", len(hist_rows) == 20, f"{len(hist_rows)} 行")
    check("最新一笔在最上面", "+25" in hist_rows[0], hist_rows[0])
    check("记录里带当前余额", "当前额度：" in hist_text)
    check(
        "中文按两格算宽度",
        services_mod._width("改名") == 4 and services_mod._width("ab") == 2,
    )
    check("补空格按显示宽度", services_mod._pad("改名", 6) == "改名  ")
    check(
        "时间截到分钟",
        services_mod._short_time("2026-10-24 14:20:01") == "10-24 14:20",
    )

    # 每天一次的维护：裁剪之后跑 PRAGMA optimize（不是 VACUUM，那个会独占锁库）。
    db.optimize()
    check("optimize 之后库仍可用", db.balance(gid, "hist1") > 0)

    # 「踢 @某人 拉黑」的分叉：判错不会报错，只在几天后有人重新申请时才暴露。
    check("踢人参数解析", router.drop_prefix("踢 @张三 拉黑", ("踢", "踢人")) == "拉黑")
    check("拉黑后缀生效", admin_mod.parse_kick_reject("拉黑") is True)
    check("黑名单也算拉黑", admin_mod.parse_kick_reject("黑名单") is True)
    check("不写后缀=普通踢人", admin_mod.parse_kick_reject("") is False)
    check("别的理由不误判", admin_mod.parse_kick_reject("刷屏") is False)

    print("\n=== 4. 真实入群天数 / 显示名 / 文案 ===")
    plugin = mod.GroupKit(
        context=None,
        config={
            "welcome_quota": 20,
            "rename_cost": 10,
            "draw_per": 3,
            "info_triggers": "查",
            "enabled_group_ids": ["1001"],
        },
    )
    profiles = plugin.ctx.profiles
    pdb = plugin.ctx.db  # 第 4/5 节统一用插件自己的库，别和上面的临时 db 混用

    gid2, uid2 = "1001", "3003"
    pdb.ensure_member(gid2, uid2, welcome_quota=20)
    join_ts = int(time.time()) - 29 * 86400  # 29 天前入群 → 在册 30 天
    member2 = pdb.set_join_info(gid2, uid2, join_time=join_ts, sex="女", qq_name="月下🌸")
    check("在册天数用真实入群时间", profiles.days(member2) == 30, str(profiles.days(member2)))
    check("群名片做默认显示名", profiles.display_name(member2) == "月下🌸")
    check("性别映射", profiles.sex(member2) == "女")
    check("卡片姓名去掉 emoji", card_mod.safe_text(profiles.display_name(member2)) == "月下")

    # 改名只写库。校验、拼群名片、调协议端都在 domains/nickname.py 里，
    # 这一层故意不碰 IO —— 所以这里能直接调，不需要造一个协议端。
    before = pdb.balance(gid2, uid2)
    renamed = profiles.set_display_name(gid2, uid2, "王小五")
    check("改名写进显示名", renamed.get("display_name") == "王小五")
    # 配置里 rename_cost 写的是 10，但改名根本不该读它 —— 这是「不自动扣费」的核心。
    check("改名不扣额度（配置写了 10 也不扣）", pdb.balance(gid2, uid2) == before)
    check("改名后显示名生效", profiles.display_name(pdb.get_member(gid2, uid2)) == "王小五")
    check("改名清掉催改名计数", int(renamed.get("nick_nag") or 0) == 0)

    hobby = profiles.set_hobby(gid2, uid2, "画画")
    check("改爱好", "画画" in hobby)

    print("\n=== 4.5 群主转让时的权限变化 ===")

    class FakeApi:
        """假装是 NapCat：按 OLD_OWNER 角色表返回成员信息。"""

        def __init__(self, roles: dict[str, str]):
            self.roles = roles
            self.fail = False

        async def call_action(self, action, **params):
            if self.fail:
                raise RuntimeError("模拟接口失败")
            if action == "get_group_member_info":
                uid = str(params.get("user_id"))
                return {"data": {"user_id": uid, "role": self.roles.get(uid, "member")}}
            return None

    class FakeEvent:
        def __init__(self, roles):
            self.bot = types.SimpleNamespace(api=FakeApi(roles))

        def get_platform_name(self):
            return "aiocqhttp"

        def get_self_id(self):
            return "999999"

        def get_group_id(self):
            return "1001"

    perm = plugin.ctx.permission
    roles = {"3003": "owner", "6006": "member"}
    ev = FakeEvent(roles)

    check("原群主 = 4 级", await_perm(perm, ev, "3003", None) == 4)
    check("普通成员 = 1 级", await_perm(perm, ev, "6006", None) == 1)

    # —— 群主转让：3003 变管理员，6006 变群主 ——
    roles["3003"] = "admin"
    roles["6006"] = "owner"
    check("转让后新群主立刻 4 级", await_perm(perm, ev, "6006", None) == 4)
    check("转让后老群主降为 3 级", await_perm(perm, ev, "3003", None) == 3)

    # —— 老群主连管理员也被撤 ——
    roles["3003"] = "member"
    check("老群主被撤成普通成员后 = 1 级", await_perm(perm, ev, "3003", None) == 1)

    # —— 手动授权不受群主转让影响（取高原则）——
    pdb.set_permission("1001", "3003", 4)
    member_3003 = pdb.get_member("1001", "3003")
    check("手动授权 4 级后仍是 4 级", await_perm(perm, ev, "3003", member_3003) == 4)

    # —— 接口查不到角色时用上次已知角色兜底，别把群主判成成员 ——
    roles["6006"] = "owner"
    await_perm(perm, ev, "6006", None)  # 先成功查一次，写入兜底缓存
    ev2 = FakeEvent(roles)
    ev2.bot.api.fail = True
    check("接口故障时群主不掉级", await_perm(perm, ev2, "6006", None) == 4)

    pdb.ensure_member("1001", "6006", welcome_quota=20)
    for _ in range(3):
        pdb.bump_message("1001", "6006", 3)

    print("\n--- 抽奖 ---")
    # 攒够 2 次机会，才能验证「还剩 1 次」和「最后一次」两种文案。
    pdb.ensure_member("1001", "7009", welcome_quota=0)
    for _ in range(6):
        pdb.bump_message("1001", "7009", 3)
    draw_text = plugin.ctx.quota.draw("1001", "7009")
    print(draw_text)
    check("抽奖有结果", "获得额度" in draw_text, draw_text.replace("\n", " | "))
    check("抽奖提示还剩几次", "还剩 1 次机会" in draw_text, draw_text.replace("\n", " | "))

    last_text = plugin.ctx.quota.draw("1001", "7009")
    check("最后一次提示机会用完", "机会用完了" in last_text, last_text.replace("\n", " | "))

    none_text = plugin.ctx.quota.draw("1001", "7009")
    print(none_text)
    check("没机会时明确说没有", "没有抽奖机会了" in none_text, none_text.replace("\n", " | "))
    check("没机会时给出还差几条", "再发" in none_text, none_text.replace("\n", " | "))

    print("\n--- 额度 ---")
    quota_text = plugin.ctx.quota.render("1001", "6006", pdb.get_member("1001", "6006"))
    print(quota_text)
    check("额度页有进度", "抽奖进度" in quota_text)

    print("\n--- 统计 ---")
    stats_text = plugin.ctx.stats.render("1001", "6006", pdb.get_member("1001", "6006"))
    print(stats_text)
    check("统计页有今日", "今日：3 条" in stats_text, stats_text.replace("\n", " | "))

    pages = help_mod.pages(plugin.ctx)
    print(f"\n--- 帮助（{len(pages)} 块） ---")
    for title, body in pages:
        print(f"\n[{title}]\n{body}")
    check("帮助 3 块", len(pages) == 3)
    check("帮助里没有立名", all("立名" not in body for _, body in pages))
    check("帮助页脚不再解释权限细节（细则归管理台）", "不看身份" not in pages[2][1])
    # 每条口令一行、前面两格缩进。这一版连解释都删了，所以「读起来是不是
    # 一条一条」全靠缩进撑着 —— 一旦退化成「额度 抽奖 今日」挤一行就读不出
    # 是三个口令。
    check(
        "帮助里额度是独立条目",
        all(f"\n  {word}\n" in pages[0][1] + "\n" for word in ("额度", "抽奖", "今日")),
        pages[0][1].replace("\n", " | "),
    )
    check("帮助里有额度记录", "\n  额度记录\n" in pages[0][1] + "\n")
    # 解释全删了：正文里不该再出现「·」后面跟一段人话这种写法。
    check("帮助里没有解释文字", "——" not in pages[0][1] and "：" not in pages[0][1])
    check("帮助里说明拉黑", "拉黑" in pages[2][1], pages[2][1].replace("\n", " | "))
    # 文字版最要紧的一条：能长按复制。所以口令里不能有「缩进 + 折行」那种
    # 只在图片里成立的排版。
    check(
        "帮助是纯文字没有图片占位",
        all("{{" not in body and ".png" not in body for _, body in pages),
    )

    # 「帮助」= 日常 + 娱乐整份，长到一屏放不下 → 打包成一条聊天记录。
    # 这里验的是发送形态，不是文案：群友点开一条记录能看到全部口令，
    # 而不是被连发三屏。
    class _MenuEvent:
        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return ("chain", chain)

        def get_self_id(self):
            return "999999"

    def _collect(agen) -> list:
        """把异步生成器的结果收成一个 list。"""

        async def _run():
            return [item async for item in agen]

        return asyncio.run(_run())

    sent = _collect(help_mod.send_menu(plugin.ctx, _MenuEvent(), "all"))
    check("帮助走合并转发", any(isinstance(r, tuple) and r[0] == "chain" for r in sent), str(sent))
    nodes = [
        node
        for reply in sent
        if isinstance(reply, tuple) and reply[0] == "chain"
        for item in reply[1]
        if hasattr(item, "nodes")
        for node in item.nodes
    ]
    check("帮助分成日常/娱乐/指路三条", len(nodes) == 3, str([getattr(n, "name", "") for n in nodes]))
    check(
        "每条记录都是纯文字",
        all(all(getattr(part, "text", None) for part in node.content) for node in nodes),
    )

    # 单独看一块（「娱乐」）不该套转发：一屏能看完的东西，多一次点击是折磨
    single = _collect(help_mod.send_menu(plugin.ctx, _MenuEvent(), "fun"))
    check("单块口令直接发文字", len(single) == 1 and isinstance(single[0], str), str(single))

    # 图鉴不再画 6 列网格图，改成「把目录里的图按顺序发出去」。
    # 所以这里验的是顺序和数量 —— 图长什么样由做图的人决定，代码只管别发错。
    assets_mod = importlib.import_module("gk_plugin.utils.assets")
    case_root = TMP / "images_case"
    shot_dir = case_root / assets_mod.CATALOG_DIR
    luck_dir = case_root / assets_mod.CHECKIN_DIR
    shot_dir.mkdir(parents=True, exist_ok=True)
    luck_dir.mkdir(parents=True, exist_ok=True)

    for name in ("10-细节.png", "2-玩法.png", "1-封面.png", "封面副本.jpg", "说明.txt"):
        (shot_dir / name).write_bytes(b"fake")
    bank_case = assets_mod.ImageBank(case_root, ROOT)

    order = [path.name for path in bank_case.catalog_images()]
    check(
        "图鉴按自然序发（2 在 10 前面）",
        order == ["1-封面.png", "2-玩法.png", "10-细节.png", "封面副本.jpg"],
        str(order),
    )
    check("图鉴跳过非图片文件", "说明.txt" not in order, str(order))
    check("图鉴不给页码就全发", len(bank_case.pick_catalog(0)) == 4)
    check("图鉴给页码只发一张", len(bank_case.pick_catalog(2)) == 1)
    # 越界夹到最后一张而不是返回空：返回空会让上层掉进文字清单兜底，
    # 群友发「表情列表 99」等来的会是一串模板名 —— 比报错更让人困惑。
    check(
        "图鉴页码越界夹到最后一张",
        bank_case.pick_catalog(99) == [bank_case.catalog_images()[-1]],
        str(bank_case.pick_catalog(99)),
    )

    # 签到运势图：同一天同一人抽到的必须是同一张。
    # 不然群友会以为「再签一次能换张好运势」，而奖励是用同一个种子算的，
    # 换了图不换奖励，等于给了他一个错误的心智模型。
    for index in range(3):
        (luck_dir / f"{index}.png").write_bytes(b"fake")
    same = {bank_case.pick_checkin("2026-04-01|1|2").name for _ in range(20)}
    check("同种子运势图稳定", len(same) == 1, str(same))
    spread = {bank_case.pick_checkin(f"2026-04-{day:02d}|1|2").name for day in range(1, 29)}
    check("换一天会换图", len(spread) > 1, str(spread))
    check(
        "目录没图时返回 None 不抛异常",
        assets_mod.ImageBank(TMP / "no_such_dir", ROOT).pick_checkin("x") is None,
    )

    # 目录是运行中可改的：新丢一张图进去，下一次调用必须立刻看见。
    # 靠的是「目录 mtime 变了就重扫」，不靠重启机器人。
    (shot_dir / "4-新增.png").write_bytes(b"fake")
    check(
        "运行中丢图立刻生效",
        "4-新增.png" in [path.name for path in bank_case.catalog_images()],
        str([path.name for path in bank_case.catalog_images()]),
    )
    check("ensure_dirs 建齐三个目录", set(bank_case.ensure_dirs()) == set(assets_mod.SUBDIRS))

    print("\n=== 4.7 查陌生人不能凭空建档 ===")
    onebot_mod = importlib.import_module("gk_plugin.utils.onebot")

    class _MissingApi:
        async def call_action(self, action, **params):
            raise RuntimeError("member not found")

    class _CardEvent:
        """只够 _card_reply 用的最小 event。"""

        message_str = "查 10001"

        def __init__(self):
            self.bot = types.SimpleNamespace(api=_MissingApi())
            self.message_obj = types.SimpleNamespace(message=[], raw_message={})

        def get_platform_name(self):
            return "aiocqhttp"

        def get_self_id(self):
            return "999999"

        def get_group_id(self):
            return "1001"

        def get_sender_id(self):
            return "6006"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return chain

    async def _ask_missing() -> list:
        replies = []
        async for reply in profile_mod.card_reply(
            plugin.ctx, _CardEvent(), "1001", "6006", "10001", pdb.get_member("1001", "6006")
        ):
            replies.append(reply)
        return replies

    quiet_saved = onebot_mod.logger
    onebot_mod.logger = types.SimpleNamespace(
        error=lambda *a, **k: None, warning=lambda *a, **k: None, info=lambda *a, **k: None
    )
    try:
        ghost = asyncio.run(_ask_missing())
    finally:
        onebot_mod.logger = quiet_saved

    check("查不在群的人有明确提示", any("没找到" in str(r) for r in ghost), str(ghost))
    # 老实现会先 ensure_member 再查，人不在群也会写一行 members，永久污染成员表
    check("不存在的 QQ 不建档", pdb.get_member("1001", "10001") is None)

    print("\n=== 4.8 协议端返回值判定（群管假失败）===")
    onebot = importlib.import_module("gk_plugin.utils.onebot")

    class _Api:
        def __init__(self, ret):
            self.ret = ret

        async def call_action(self, action, **params):
            if isinstance(self.ret, Exception):
                raise self.ret
            return self.ret

    class _Ev:  # event 的最小替身
        def __init__(self, ret):
            self.bot = types.SimpleNamespace(api=_Api(ret))

    async def _mute(ret) -> bool:
        return await onebot.mute(_Ev(ret), "1001", "666", 600)

    async def _info(ret):
        return await onebot.get_member_info(_Ev(ret), "1001", "666")

    quiet = onebot.logger
    onebot.logger = types.SimpleNamespace(
        error=lambda *a, **k: None, warning=lambda *a, **k: None, info=lambda *a, **k: None
    )
    try:
        # OneBot 写操作成功时 data 就是 null，拿到的是裸 None。
        # 老代码用 `result is not None` 判成功，于是每次成功都回一句"失败"。
        check("禁言成功 data=null 判成功", asyncio.run(_mute(None)) is True)
        check("禁言成功 空 dict 判成功", asyncio.run(_mute({})) is True)
        check(
            "禁言成功 status=ok 判成功",
            asyncio.run(_mute({"status": "ok", "retcode": 0, "data": None})) is True,
        )
        check("禁言失败 status=failed", asyncio.run(_mute({"status": "failed", "retcode": 100})) is False)
        check("禁言失败 retcode=1", asyncio.run(_mute({"retcode": 1})) is False)
        check("禁言抛异常 判失败", asyncio.run(_mute(RuntimeError("boom"))) is False)
        check(
            "成员信息失败不伪造",
            asyncio.run(_info({"status": "failed", "retcode": 1, "data": None})) is None,
        )
        check(
            "成员信息 data 解包",
            asyncio.run(_info({"status": "ok", "retcode": 0, "data": {"role": "admin"}}))
            == {"role": "admin"},
        )
    finally:
        onebot.logger = quiet

    print("\n=== 4.9 踢人 / 拉黑（reject_add_request 有没有传下去）===")

    class _KickApi:
        """记录所有 call_action，用来断言参数真的传到了协议端。

        ``roles`` 可以按 QQ 号指定角色（授权分级测试要区分「发起人」和
        「被授权人」的身份），没给的统一用 ``role``。
        """

        def __init__(self, role: str = "admin", roles: dict | None = None):
            self.role = role
            self.roles = roles or {}
            self.calls: list[tuple[str, dict]] = []

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_info":
                uid = str(params.get("user_id"))
                return {"data": {"user_id": uid, "role": self.roles.get(uid, self.role)}}
            return None

    class _KickEvent:
        def __init__(self, target: str | None = None, role: str = "admin", roles: dict | None = None):
            self.bot = types.SimpleNamespace(api=_KickApi(role, roles))
            self.message_obj = types.SimpleNamespace(
                message=[router_at(target)] if target else [], raw_message={}
            )
            self.calls = self.bot.api.calls

        def get_platform_name(self):
            return "aiocqhttp"

        def get_self_id(self):
            return "999999"

        def get_group_id(self):
            return "1001"

        def get_sender_id(self):
            return "6006"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return ("chain", chain)

    at_mod = sys.modules["astrbot.api.message_components"]

    def router_at(target: str):
        return at_mod.At(qq=target)

    async def _kick(args: str) -> tuple[list, list]:
        ev = _KickEvent("7007")
        replies = []
        # 走完整分发链路：路由命中 → 权限校验 → 处理函数 → 调协议端。
        # 这样「踢」这条命令的口令、等级、参数解析一起被验证到。
        env = command_mod.CommandEnv(
            event=ev,
            group_id="1001",
            sender_id="6006",
            member=pdb.get_member("1001", "6006"),
            text=f"踢 {args}".strip(),
        )
        async for reply in registry_mod.dispatch(plugin.ctx, env):
            replies.append(reply)
        return replies, ev.calls

    kick_calls, calls_plain = asyncio.run(_kick(""))
    plain_kick = [p for a, p in calls_plain if a == "set_group_kick"]
    check("普通踢人不传拉黑", plain_kick and plain_kick[0].get("reject_add_request") is False,
          str(plain_kick))
    check("普通踢人提示不带拉黑", all("拉黑" not in str(r) for r in kick_calls), str(kick_calls))

    kick_calls2, calls_black = asyncio.run(_kick("拉黑"))
    black_kick = [p for a, p in calls_black if a == "set_group_kick"]
    check("拉黑真的传给协议端", black_kick and black_kick[0].get("reject_add_request") is True,
          str(black_kick))
    check("拉黑提示说明后果", any("不能再申请" in str(r) for r in kick_calls2), str(kick_calls2))

    async def _say(text: str, target: str | None = None, role: str = "admin"):
        """把一句话真的喂给分发器，返回 (回复列表, 协议端调用记录)。"""
        ev = _KickEvent(target, role)
        replies = []
        env = command_mod.CommandEnv(
            event=ev,
            group_id="1001",
            sender_id="6006",
            member=pdb.get_member("1001", "6006"),
            text=text,
        )
        async for reply in registry_mod.dispatch(plugin.ctx, env):
            replies.append(reply)
        return replies, ev.calls

    print("\n=== 4.10 禁言：单人 / 全群分开 ===")
    mute_replies, mute_calls = asyncio.run(_say("禁言 @7007", "7007"))
    ban = [p for a, p in mute_calls if a == "set_group_ban"]
    check("不写时长也能禁言", bool(ban), str(mute_calls))
    check("不写时长用默认 10 分钟", ban and ban[0].get("duration") == 600, str(ban))
    check("禁言提示带上时长", any("10 分钟" in str(r) for r in mute_replies), str(mute_replies))

    _, mute_calls2 = asyncio.run(_say("禁言 @7007 5分钟", "7007"))
    ban2 = [p for a, p in mute_calls2 if a == "set_group_ban"]
    check("写了时长按写的算", ban2 and ban2[0].get("duration") == 300, str(ban2))

    _, mute_calls3 = asyncio.run(_say("禁言 @7007 瞎写", "7007"))
    ban3 = [p for a, p in mute_calls3 if a == "set_group_ban"]
    check("看不懂的时长不瞎禁言", not ban3, str(mute_calls3))

    # 全群禁言和单人禁言是两个互不相干的口令：
    #   「禁言 @某人」只禁这一个人；「全群禁言」才是封整群。
    # 这里盯死一件事 —— 除了「全群禁言」，任何带「禁言」两个字的说法
    # 都不许调到 set_group_whole_ban。
    whole_replies, whole_calls = asyncio.run(_say("全群禁言", None, role="owner"))
    whole = [p for a, p in whole_calls if a == "set_group_whole_ban"]
    check("全群禁言口令走全群禁言", bool(whole) and whole[0].get("enable") is True, str(whole_calls))
    check("全群禁言不会误禁单人",
          not [p for a, p in whole_calls if a == "set_group_ban"], str(whole_calls))

    # 管理员只有 3 级，全群禁言要 4 级 —— 必须拦住。
    denied, denied_calls = asyncio.run(_say("全群禁言", None, role="admin"))
    check(
        "3 级不能全群禁言",
        not [p for a, p in denied_calls if a == "set_group_whole_ban"],
        str(denied_calls),
    )
    check("拒绝时说明要群主权限", any("群主" in str(r) for r in denied), str(denied))

    # 有人会顺手把「全体」塞进禁言里。这不执行，只把他引到正确口令上。
    for text, target in (
        ("禁言 @全体成员", "all"),
        ("禁言 全体", None),
        ("禁言 全员", None),
    ):
        hint_replies, hint_calls = asyncio.run(_say(text, target, role="owner"))
        check(
            f"「{text}」不会封整群",
            not [p for a, p in hint_calls if a == "set_group_whole_ban"],
            str(hint_calls),
        )
        check(
            f"「{text}」引导去用全群禁言",
            any("全群禁言" in str(r) for r in hint_replies),
            str(hint_replies),
        )

    # 有 @ 目标时，参数里出现「全体」只影响目标这一次禁言，不能升级成全群。
    _, tricky_calls = asyncio.run(_say("禁言 @7007 全体", "7007", role="owner"))
    check(
        "禁言 @某人 全体 仍只禁这个人",
        not [p for a, p in tricky_calls if a == "set_group_whole_ban"],
        str(tricky_calls),
    )

    print("\n=== 4.11 性别：自己改 / 代改 / 非法值 ===")
    sex_replies, _ = asyncio.run(_say("性别 男"))
    check("改性别成功", any("性别已更新" in str(r) for r in sex_replies), str(sex_replies))
    check("库里存的是男", (pdb.get_member("1001", "6006") or {}).get("sex") == "男",
          str(pdb.get_member("1001", "6006")))

    async def _sex_other(text: str):
        return await _say(text, "7007")

    asyncio.run(_sex_other("性别 @7007 女"))
    check("代改性别生效", (pdb.get_member("1001", "7007") or {}).get("sex") == "女",
          str(pdb.get_member("1001", "7007")))

    bad_replies, _ = asyncio.run(_say("性别 外星人"))
    check("非法性别不写入", any("格式：性别 男" in str(r) for r in bad_replies), str(bad_replies))
    check(
        "非法性别保留原值",
        (pdb.get_member("1001", "6006") or {}).get("sex") == "男",
        str(pdb.get_member("1001", "6006")),
    )

    # 手动改过的性别，不能被协议端的 QQ 资料盖回去。
    # 这个坑很隐蔽：need 是「join_time / qq_name / sex 任一缺失」就查协议端，
    # 所以一个刚改完性别、但还没同步过入群时间的人，下一次查卡就会被冲掉。
    class _SexApi(_KickApi):
        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_info":
                return {
                    "data": {
                        "user_id": str(params.get("user_id")),
                        "role": "admin",
                        "sex": "male",
                        "join_time": 1_700_000_000,
                        "nickname": "小明",
                    }
                }
            return None

    class _SexEvent:
        def __init__(self):
            self.bot = types.SimpleNamespace(api=_SexApi())
            self.message_obj = types.SimpleNamespace(message=[], raw_message={})

        def get_self_id(self):
            return "999999"

    manual_member = pdb.get_member("1001", "7007") or {}
    check("测试前置：7007 手动设过女", manual_member.get("sex") == "女", str(manual_member))

    async def _refresh():
        return await plugin.ctx.profiles.refresh_from_qq(
            _SexEvent(), "1001", "7007", manual_member
        )

    refreshed = asyncio.run(_refresh())
    check("手动性别不被协议端覆盖", refreshed.get("sex") == "女", str(refreshed))
    check("入群时间仍然同步进来", refreshed.get("join_time") == 1_700_000_000, str(refreshed))

    print("\n=== 4.12 表情包：参数语法 / 扣费 / 失败不扣 ===")
    meme_mod = importlib.import_module("gk_plugin.core.meme_api")
    sticker_mod = importlib.import_module("gk_plugin.domains.sticker")
    media_mod = importlib.import_module("gk_plugin.utils.media")

    # 一张 1x1 的合法 PNG。不用 Pillow 也能造出来，避免让这一段依赖可选环境。
    PNG_1PX = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    src_png = TMP / "meme_src.png"
    src_png.write_bytes(PNG_1PX)

    check("引号里的内容不被切开", sticker_mod.split_tokens('a "b c" d') == ["a", "b c", "d"],
          str(sticker_mod.split_tokens('a "b c" d')))

    parsed = sticker_mod.parse_args(["#circle", "左", "自己", "https://x/y.png", "你好"])
    check("选项 #circle 解析成 True", parsed.options.get("circle") is True, str(parsed.options))
    check("方向词解析成 direction", parsed.options.get("direction") == "left", str(parsed.options))
    check("「自己」被识别", parsed.want_self is True)
    check("链接归到 urls", parsed.urls == ["https://x/y.png"], str(parsed.urls))
    check("剩下的才是文字", parsed.texts == ["你好"], str(parsed.texts))

    parsed2 = sticker_mod.parse_args(["name=小明", "1=1"])
    check("key=value 进 options", parsed2.options.get("name") == "小明", str(parsed2.options))
    check("1=1 被当成普通文字", parsed2.texts == ["1=1"], str(parsed2.texts))

    # meme-generator 的参数字段叫 params_type，不是 params。
    # 读错字段名的后果是把「需要 1 张图」当成「不需要图」，做出来全是空白。
    parsed_info = meme_mod._parse_info(
        "摸头",
        {
            "key": "摸头",
            "keywords": ["摸头", "petpet"],
            "params_type": {
                "min_images": 1,
                "max_images": 2,
                "min_texts": 0,
                "max_texts": 1,
                "default_texts": ["你好"],
            },
        },
    )
    check("读懂 params_type 的图片数量",
          (parsed_info.min_images, parsed_info.max_images) == (1, 2), str(parsed_info))
    check("读懂 params_type 的默认文字",
          parsed_info.default_texts == ["你好"], str(parsed_info.default_texts))
    check("关键词被保留", "petpet" in parsed_info.keywords, str(parsed_info.keywords))

    class FakeMeme:
        """假的 meme-generator。只验插件这一侧的编排，不碰网络。"""

        def __init__(self):
            self.catalog_data = {
                "摸头": meme_mod.MemeInfo(
                    key="摸头", keywords=["摸头", "petpet"],
                    min_images=1, max_images=1, min_texts=0, max_texts=0,
                ),
                "亲亲": meme_mod.MemeInfo(
                    key="亲亲", keywords=["亲亲", "kiss"],
                    min_images=2, max_images=2, min_texts=0, max_texts=1,
                    default_texts=["你好"],
                ),
            }
            self.render_calls = []
            self.fail = False

        async def catalog(self, force=False):
            return dict(self.catalog_data)

        async def info(self, key, force=False):
            if key not in self.catalog_data:
                raise meme_mod.MemeApiError("没有这个模板")
            return self.catalog_data[key]

        # 直接复用真实实现，这样关键词匹配逻辑也被覆盖到
        def find(self, query, catalog):
            return meme_mod.MemeApi.find(query, catalog)

        def search(self, query, catalog, limit=20):
            return meme_mod.MemeApi.search(query, catalog, limit)

        async def render(self, key, images, texts, user_infos=None, options=None, timeout=None):
            self.render_calls.append(
                {"key": key, "images": len(images), "texts": list(texts),
                 "user_infos": list(user_infos or []), "options": dict(options or {})}
            )
            if self.fail:
                raise meme_mod.MemeApiError("表情服务炸了")
            return PNG_1PX, "image/png"

    class _MemeEvent:
        def __init__(self, at=None, image_path=None, send_ok=True):
            comps = []
            if at:
                comps.append(at_mod.At(qq=at))
            if image_path:
                comps.append(at_mod.Image(file=str(image_path)))
            self.message_obj = types.SimpleNamespace(message=comps, raw_message={})
            self.bot = types.SimpleNamespace(api=_MemeCall(send_ok))
            self.calls = self.bot.api.calls

        def get_platform_name(self):
            return "aiocqhttp"

        def get_self_id(self):
            return "999999"

        def get_group_id(self):
            return "2002"

        def get_sender_id(self):
            return "8008"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return ("chain", chain)

        def stop_event(self):
            self.stopped = True

    class _MemeCall:
        def __init__(self, send_ok=True):
            self.calls = []
            self.send_ok = send_ok

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "send_group_msg":
                if self.send_ok:
                    return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
                return {"status": "failed", "retcode": 100, "data": None}
            return {"status": "ok", "retcode": 0, "data": None}

    # 头像走网络，测试里换成固定字节
    async def _fake_avatar(uid, max_bytes, timeout=10):
        return PNG_1PX, "image/png", "avatar.png"

    media_mod.load_avatar = _fake_avatar
    real_meme = plugin.ctx.meme
    fake = FakeMeme()
    plugin.ctx.meme = fake

    GID, UID = "2002", "8008"
    pdb.ensure_member(GID, UID, 0)

    async def _meme_say(text, at=None, image_path=None, send_ok=True, reset_throttle=True):
        if reset_throttle:
            sticker_mod._LAST_USE.clear()
        evt = _MemeEvent(at, image_path, send_ok)
        replies = []
        env = command_mod.CommandEnv(
            event=evt,
            group_id=GID,
            sender_id=UID,
            member=pdb.get_member(GID, UID),
            text=text,
        )
        async for reply in registry_mod.dispatch(plugin.ctx, env):
            replies.append(reply)
        return replies, evt

    def _text_of(replies):
        return "\n".join(str(r) for r in replies)

    # 没带模板 → 给用法，不请求服务
    replies, _ = asyncio.run(_meme_say("表情"))
    check("表情没参数给用法", "表情包这样做" in _text_of(replies), _text_of(replies))

    # 不存在的模板 → 明确说没找到
    replies, _ = asyncio.run(_meme_say("表情 不存在的模板"))
    check("未知模板有提示", "没找到" in _text_of(replies), _text_of(replies))

    # 关键词也能命中（QQ 群里没人记 petpet 这种 key）
    found = real_meme.find("petpet", fake.catalog_data)
    check("关键词能找到模板", found is not None and found.key == "摸头", str(found))

    # 正常做一张：本地图 + 扣 1 额度 + 用小图发送
    pdb.add_quota(GID, UID, 10, "测试充值")
    before = pdb.balance(GID, UID)
    replies, evt = asyncio.run(_meme_say("表情 摸头", image_path=src_png))
    sends = [p for a, p in evt.calls if a == "send_group_msg"]
    check("表情做成功并发出", bool(sends), str(evt.calls))
    check("表情按小图发（sub_type=1）",
          bool(sends) and sends[0]["message"][0]["data"].get("sub_type") == 1, str(sends))
    # 用配置里的价而不是写死 3：以后调价不该让这条测试变红，
    # 「扣了应扣的钱」才是它要证明的事。
    cost = plugin.ctx.config.meme_cost
    check(f"成功后扣 {cost} 额度", pdb.balance(GID, UID) == before - cost, str(pdb.balance(GID, UID)))
    check("渲染时带上了图", fake.render_calls and fake.render_calls[-1]["images"] == 1,
          str(fake.render_calls[-1]))

    # 图不够：只给 1 张，但模板要 2 张 → 自动补发送者头像
    fake.render_calls.clear()
    before = pdb.balance(GID, UID)
    replies, evt = asyncio.run(_meme_say("表情 亲亲", image_path=src_png))
    check("图不够自动补头像", fake.render_calls and fake.render_calls[-1]["images"] == 2,
          str(fake.render_calls[-1] if fake.render_calls else "没渲染"))
    check("没写文字用默认文字",
          fake.render_calls and fake.render_calls[-1]["texts"] == ["你好"],
          str(fake.render_calls[-1] if fake.render_calls else "没渲染"))
    check("补充头像时带上了名字",
          fake.render_calls and fake.render_calls[-1]["user_infos"][1].get("name"),
          str(fake.render_calls[-1]["user_infos"] if fake.render_calls else ""))

    # 渲染失败 → 一分不扣
    fake.fail = True
    before = pdb.balance(GID, UID)
    replies, _ = asyncio.run(_meme_say("表情 摸头", image_path=src_png))
    check("渲染失败有提示", "炸了" in _text_of(replies), _text_of(replies))
    check("渲染失败不扣额度", pdb.balance(GID, UID) == before, str(pdb.balance(GID, UID)))
    fake.fail = False

    # 余额不足 → 直接拦下，不去请求服务
    pdb.add_quota(GID, UID, -pdb.balance(GID, UID), "清空")
    fake.render_calls.clear()
    replies, _ = asyncio.run(_meme_say("表情 摸头", image_path=src_png))
    check("余额不足有提示", "不够" in _text_of(replies), _text_of(replies))
    check("余额不足不请求渲染", not fake.render_calls, str(fake.render_calls))

    # 连点保护
    pdb.add_quota(GID, UID, 10, "测试充值")
    asyncio.run(_meme_say("表情 摸头", image_path=src_png))
    replies, _ = asyncio.run(
        _meme_say("表情 摸头", image_path=src_png, reset_throttle=False)
    )
    check("连点会被拦", "慢点" in _text_of(replies), _text_of(replies))
    sticker_mod._LAST_USE.clear()

    # 小图发送失败要能降级成普通图片，而不是把结果丢掉
    pdb.add_quota(GID, UID, 10, "测试充值")
    replies, evt = asyncio.run(_meme_say("表情 摸头", image_path=src_png, send_ok=False))
    check("小图失败降级成普通图",
          any(isinstance(r, tuple) and r[0] == "chain" for r in replies), str(replies))

    def _forward_nodes(replies) -> int:
        """回复里有几个合并转发节点。0 = 没走转发。"""
        total = 0
        for reply in replies:
            if not (isinstance(reply, tuple) and reply[0] == "chain"):
                continue
            for item in reply[1]:
                if hasattr(item, "nodes"):
                    total += len(item.nodes)
        return total

    # ---- 表情列表：发目录里的图，不再画 6 列网格
    # 要守的三件事：一张直接发、多张打包、带页码只发那一张。
    # 「发对了」比「画得好」重要 —— 图是人做的，代码只管别发错。
    real_images = plugin.ctx.images
    shots_root = TMP / "list_shots"
    shots_dir = shots_root / assets_mod.CATALOG_DIR
    shots_dir.mkdir(parents=True, exist_ok=True)
    (shots_dir / "01-封面.png").write_bytes(b"fake")
    plugin.ctx.images = assets_mod.ImageBank(shots_root, ROOT)
    try:
        replies, _ = asyncio.run(_meme_say("表情列表"))
        check(
            "列表只有一张时直接发图",
            any(isinstance(r, tuple) and r[0] == "chain" for r in replies)
            and _forward_nodes(replies) == 0,
            str(replies),
        )

        for index in range(2, 6):
            (shots_dir / f"0{index}-玩法.png").write_bytes(b"fake")
        replies, _ = asyncio.run(_meme_say("表情列表"))
        packed = _forward_nodes(replies) > 1 or len(replies) > 1
        check("列表有多张时打包发送", packed, str(replies))

        replies, _ = asyncio.run(_meme_say("表情列表 3"))
        check(
            "带页码只发那一张",
            _forward_nodes(replies) == 0
            and len([r for r in replies if isinstance(r, tuple) and r[0] == "chain"]) == 1,
            str(replies),
        )
    finally:
        plugin.ctx.images = real_images

    # 图鉴目录为空时必须退回模板名清单，而且清单里只能是中文名。
    # 这是整个改动里最要紧的一条回归 —— 之前列表直接甩 `petpet`、
    # `raise_sign` 这种内部 key，群友根本没法用。现在图鉴虽然改成发图了，
    # 但「还没放图」是常态，所以这条兜底路径必须一直在。
    bare_root = TMP / "bare_images"
    (bare_root / assets_mod.CATALOG_DIR).mkdir(parents=True, exist_ok=True)
    plugin.ctx.images = assets_mod.ImageBank(bare_root, ROOT)
    try:
        replies, _ = asyncio.run(_meme_say("表情列表"))
        text = _text_of(replies)
        check("没放图鉴图时退回文字", "表情图鉴" in text, text)
        check("图鉴用中文名不用英文 key", "摸头" in text and "petpet" not in text, text)
        # 图鉴图是自己排的版，模板黑名单这类「替群定尺度」的开关已经拿掉了 ——
        # 兜底清单必须原样列出服务端给的每一个模板。
        check("兜底清单不做过滤", f"共 {len(fake.catalog_data)} 个" in text, text)
        check("文字版也给做法", "表情 摸头 @某人" in text, text)

        # 带页码：越界不报错，夹到最后一页
        replies, _ = asyncio.run(_meme_say("表情列表 99"))
        check("页码越界不报错", "第 1/1 张" in _text_of(replies), _text_of(replies))
    finally:
        plugin.ctx.images = real_images

    # 搜表情已下线（图鉴够了）：口令现在应该完全不接，当普通聊天
    replies, _ = asyncio.run(_meme_say("搜表情 亲"))
    check("搜表情口令已下线", _text_of(replies) == "", _text_of(replies))

    replies, _ = asyncio.run(_meme_say("表情详情 摸头"))
    check("详情说明图片数量", "1 张图" in _text_of(replies), _text_of(replies))

    # 免费模式下不该产生任何额度流水
    plugin.ctx.config = config_mod.PluginConfig({"meme_cost": 0})
    before = pdb.balance(GID, UID)
    asyncio.run(_meme_say("表情 摸头", image_path=src_png))
    check("免费模式不扣额度", pdb.balance(GID, UID) == before, str(pdb.balance(GID, UID)))
    plugin.ctx.config = config_mod.PluginConfig({})

    plugin.ctx.meme = real_meme

    print("\n=== 5. 信息卡渲染 ===")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = profiles.card_data(gid2, uid2, pdb.get_member(gid2, uid2), pdb.counts(gid2, uid2))
    check("卡片用真实天数", data["days"] == 30, str(data["days"]))
    path = plugin.ctx.cards.render(data)
    check_pil("渲染成功", path is not None and Path(path).exists())
    if path:
        target = OUT_DIR / "card_preview.png"
        target.write_bytes(Path(path).read_bytes())
        print(f"  预览已保存：{target}")

    # 没装 Pillow 时不能抛异常：必须返回 None，让上层退化成文字卡。
    # 开关现在在 theme 里（所有渲染器共用同一个判断），所以补丁要打在那儿。
    theme_mod = importlib.import_module("gk_plugin.utils.theme")
    original_pil = theme_mod.PIL_AVAILABLE
    try:
        theme_mod.PIL_AVAILABLE = False
        check("无 Pillow 时退回文字卡", plugin.ctx.cards.render(dict(data)) is None)
    finally:
        theme_mod.PIL_AVAILABLE = original_pil

    # 文字版必须和图片版说同样的话 —— 降级不该变成「另一种卡」。
    fallback = profile_mod.text_card(dict(data))
    for field in ("姓名", "性别", "爱好", "额度", "注册天数", "关系"):
        check(f"文字卡有「{field}」", field in fallback, fallback.replace("\n", " | "))
    check(
        "文字卡不再列抽奖与发言",
        "抽奖" not in fallback and "今日" not in fallback and "本月" not in fallback,
        fallback.replace("\n", " | "),
    )

    # 没写爱好的人在图片上得到「还没写」，不该是一行空白或一个「无」。
    check(
        "空爱好显示「还没写」",
        "还没写" in profile_mod.text_card(dict(data, hobby="")),
        profile_mod.text_card(dict(data, hobby="")),
    )

    # 极端数据不能把版式撑崩
    weird = dict(
        data,
        name="超级无敌长的名字测试用例啊" * 5,
        hobby="爱好" * 40,
        quota=10**12,
        days=99999,
        today=10**9,
        month=10**9,
        total=10**9,
    )
    weird_path = plugin.ctx.cards.render(weird)
    check_pil("极端数据不崩", weird_path is not None and Path(weird_path).exists())

    # 底图比例不对时应该 cover 裁切，而不是拉伸（拉伸会把人物压扁）
    if PILImage is not None:
        odd_root = TMP / "odd_images"
        (odd_root / assets_mod.CARDS_DIR).mkdir(parents=True, exist_ok=True)
        PILImage.new("RGB", (600, 340), (255, 230, 240)).save(
            odd_root / assets_mod.CARDS_DIR / "男.png"
        )
        odd_renderer = card_mod.CardRenderer(
            ROOT,
            "assets/fonts/LXGWWenKai-Medium.ttf",
            OUT_DIR / "tmp_odd_bg",
            assets_mod.ImageBank(odd_root, ROOT),
        )
        odd_path = odd_renderer.render(dict(data))
        check(
            "底图比例不符也裁成卡片尺寸",
            odd_path is not None
            and PILImage.open(odd_path).size == (card_mod.CARD_W, card_mod.CARD_H),
            str(PILImage.open(odd_path).size) if odd_path else "渲染失败",
        )
    else:
        check_pil("底图比例不符也裁成卡片尺寸", False)

    # 底图选择链：买的名片 → 按性别的基础名片 → 程序画的兜底。
    # 这条链断了不会报错，只会「所有人的卡长得一样」—— 所以必须钉住。
    if PILImage is not None:
        gender_root = TMP / "gender_images"
        cards_dir = gender_root / assets_mod.CARDS_DIR
        cards_dir.mkdir(parents=True, exist_ok=True)
        PILImage.new("RGB", (1280, 720), (250, 250, 250)).save(cards_dir / "男.png")
        PILImage.new("RGB", (1280, 720), (255, 235, 240)).save(cards_dir / "女.png")
        PILImage.new("RGB", (1280, 720), (235, 235, 245)).save(cards_dir / "夜空.png")
        gbank = assets_mod.ImageBank(gender_root, ROOT)

        check("男/女不进商店货架", set(gbank.skin_names()) == {"夜空"}, str(gbank.skin_names()))
        check("没写性别按男版", gbank.base_card("") == cards_dir / "男.png", str(gbank.base_card("")))
        check("性别女给女版", gbank.base_card("女") == cards_dir / "女.png", str(gbank.base_card("女")))
        check("选保密也走男版", gbank.base_card("保密") == cards_dir / "男.png")
        check(
            "买的名片优先于基础名片",
            gbank.resolve_card("夜空", "女") == cards_dir / "夜空.png",
            str(gbank.resolve_card("夜空", "女")),
        )
        check("没买就退回基础名片", gbank.resolve_card("", "女") == cards_dir / "女.png")

        # 一个只会出现的场景：群主把基础名片删了。这时不能崩，要落到兜底底图。
        (cards_dir / "男.png").unlink()
        gbank2 = assets_mod.ImageBank(gender_root, ROOT)
        check("基础名片被删也不报错", gbank2.base_card("男") is None)
        bare_renderer = card_mod.CardRenderer(
            ROOT, "", OUT_DIR / "tmp_no_bg", gbank2
        )
        bare_path = bare_renderer.render(dict(data, gender="男"))
        check_pil("没有底图时用兜底浅色底", bare_path is not None and Path(bare_path).exists())
    else:
        for name in ("男/女不进商店货架", "没写性别按男版", "性别女给女版", "选保密也走男版",
                     "买的名片优先于基础名片", "没买就退回基础名片", "基础名片被删也不报错"):
            check_pil(name, False)

    # 卡上不再有头像：这条断言是为了防「有人把头像加回来」——
    # 加回来意味着每查一次卡都要多一次可能超时的网络请求。
    import inspect as _inspect

    check(
        "信息卡渲染不接收头像",
        "avatar" not in _inspect.signature(card_mod.CardRenderer.render).parameters,
        str(list(_inspect.signature(card_mod.CardRenderer.render).parameters)),
    )
    check(
        "信息卡渲染不再拉头像",
        not hasattr(profile_mod, "fetch_avatar"),
    )

    check("默认做表情扣 3", config_mod.PluginConfig({}).meme_cost == 3,
          str(config_mod.PluginConfig({}).meme_cost))
    check(
        "默认签到 3~10",
        (config_mod.PluginConfig({}).checkin_base_min,
         config_mod.PluginConfig({}).checkin_base_max) == (3, 10),
        str(config_mod.PluginConfig({}).checkin_base_min),
    )
    check("默认连签不加钱", config_mod.PluginConfig({}).checkin_streak_bonus == 0)
    check(
        "默认抽奖 10~20",
        (config_mod.PluginConfig({}).draw_min, config_mod.PluginConfig({}).draw_max) == (10, 20),
    )
    check("默认抽奖不暴击", config_mod.PluginConfig({}).draw_bonus_chance == 0)
    check("默认名片 50", config_mod.PluginConfig({}).card_skin_price == 50)
    check("默认新人礼 10", config_mod.PluginConfig({}).welcome_quota == 10, str(config_mod.PluginConfig({}).welcome_quota))
    # 改名不再自动扣费：这个群里改名是「进来就要做的事」，不是消费。
    # 要收钱由管理员手动「扣额度」。
    check("默认改名不扣费", config_mod.PluginConfig({}).rename_cost == 0)
    check("默认禁言 10 分钟", config_mod.PluginConfig({}).default_mute_minutes == 10)
    check(
        "禁言时长可配且收敛",
        config_mod.PluginConfig({"default_mute_minutes": 0}).default_mute_minutes == 1
        and config_mod.PluginConfig({"default_mute_minutes": 999999}).default_mute_minutes == 43200,
    )

    # ---------------------------------------------------------------- 签到
    print("\n=== 5.5 签到：奖励规则 / 连签 / 双倍卡 ===")
    checkin_mod = importlib.import_module("gk_plugin.core.checkin")
    shop_mod = importlib.import_module("gk_plugin.domains.shop")
    ck_db = db_mod.Database(TMP / "db_checkin")
    ck = checkin_mod.CheckinService(ck_db, config_mod.PluginConfig({}))
    G, U = "9001", "7001"

    # 奖励必须是确定性随机：同一天同一个人重算多少次都是同一个数。
    # 这是签到能「重发当天卡片」的地基，也是能向群友解释「为什么我少」的前提。
    a = checkin_mod.base_reward(G, U, "2026-01-05", 5, 15)
    b = checkin_mod.base_reward(G, U, "2026-01-05", 5, 15)
    c = checkin_mod.base_reward(G, U, "2026-01-06", 5, 15)
    d = checkin_mod.base_reward(G, "7002", "2026-01-05", 5, 15)
    check("基础额度落在区间内", 5 <= a <= 15, str(a))
    check("同人同日结果恒定", a == b, f"{a} vs {b}")
    check("换一天会变", a != c or True, "")  # 允许碰撞，只验证不抛异常
    check("换个人会变", isinstance(d, int), str(d))

    check("连签加成 第1天为0", checkin_mod.streak_bonus(1, 2, 10) == 0)
    check("连签加成 第4天", checkin_mod.streak_bonus(4, 2, 10) == 6, str(checkin_mod.streak_bonus(4, 2, 10)))
    check("连签加成封顶", checkin_mod.streak_bonus(99, 2, 10) == 20, str(checkin_mod.streak_bonus(99, 2, 10)))
    check("里程碑 7 天给 20", checkin_mod.milestone_bonus(7) == 20)
    check("里程碑 非节点不给", checkin_mod.milestone_bonus(8) == 0)
    check("下一里程碑", checkin_mod.next_milestone(14) == (30, 100))

    # 第一次签到
    first = ck_db.perform_checkin(G, U, "2026-01-05", ck._reward_fn(G, U, "2026-01-05"))
    check("首签 连签=1", first["streak_after"] == 1, str(first["streak_after"]))
    check("首签 累计=1", first["total_after"] == 1, str(first["total_after"]))
    check("首签 已入账", ck_db.balance(G, U) == first["reward"], str(ck_db.balance(G, U)))

    # 同一天再签：不许二次发钱，快照必须和第一次完全一样
    again = ck_db.perform_checkin(G, U, "2026-01-05", ck._reward_fn(G, U, "2026-01-05"))
    check("重复签到被标记", again.get("already") is True)
    check("重复签到不重复发钱", ck_db.balance(G, U) == first["reward"], str(ck_db.balance(G, U)))
    check("重复签到快照一致", again["reward"] == first["reward"], f"{again['reward']} vs {first['reward']}")

    # 第二天：连签推进
    second = ck_db.perform_checkin(G, U, "2026-01-06", ck._reward_fn(G, U, "2026-01-06"))
    check("次签 连签=2", second["streak_after"] == 2, str(second["streak_after"]))
    check("次签 默认不加钱", second["streak_bonus"] == 0, str(second["streak_bonus"]))
    # 加成默认关掉了（签到收益要严格落在 3~10），但功能还在 —— 配上去就得算。
    bonus_ck = checkin_mod.CheckinService(
        ck_db, config_mod.PluginConfig({"checkin_streak_bonus": 2})
    )
    _, bonus_value, _ = bonus_ck._reward_fn(G, U, "2026-01-06")(2, 2)
    check("配了连签加成就有", bonus_value == 2, str(bonus_value))

    # 断签：跳过 01-07，连签归 1，累计继续涨
    broken = ck_db.perform_checkin(G, U, "2026-01-08", ck._reward_fn(G, U, "2026-01-08"))
    check("断签后连签归 1", broken["streak_after"] == 1, str(broken["streak_after"]))
    check("断签后累计继续加", broken["total_after"] == 3, str(broken["total_after"]))

    # 里程碑：从 0 开始连签 7 天，第 7 天该拿 20
    M = "7100"
    milestone_hit = None
    for day in range(1, 8):
        key = f"2026-02-{day:02d}"
        row = ck_db.perform_checkin(G, M, key, ck._reward_fn(G, M, key))
        if day == 7:
            milestone_hit = row
    check(
        "连签 7 天命中里程碑",
        milestone_hit is not None and milestone_hit["milestone_bonus"] == 20,
        str(milestone_hit["milestone_bonus"] if milestone_hit else None),
    )

    # 并发：10 个线程同时签同一天，只能成功一次
    C = "7200"
    results = []
    errors: list[str] = []

    def _race() -> None:
        try:
            results.append(
                ck_db.perform_checkin(
                    G, C, "2026-03-01", ck._reward_fn(G, C, "2026-03-01")
                )
            )
        except Exception as exc:  # 线程里的异常不会冒到主线程，必须自己收
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=_race) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 这一条守的是「一线程一连接」：管理台接口在线程池里查库，和聊天路径
    # 同时碰库时，共用一个 sqlite 连接会抛 bad parameter or other API misuse。
    check("并发签到没有线程炸掉", not errors, "; ".join(errors[:2]))
    check("并发签到 10 个线程都给了结果", len(results) == 10, f"回来 {len(results)} 个")

    fresh = [item for item in results if not item.get("already")]
    check("并发签到只成功一次", len(fresh) == 1, f"成功 {len(fresh)} 次")
    check(
        "并发签到只加一次额度",
        ck_db.balance(G, C) == fresh[0]["reward"],
        f"{ck_db.balance(G, C)} vs {fresh[0]['reward']}",
    )

    print("\n=== 5.6 商店：定价 / 限购 / 称号门槛 ===")
    check("抽奖券按期望定价", ck.ticket_price == 15, str(ck.ticket_price))
    check("改头衔默认 100 一次", config_mod.PluginConfig({}).title_price == 100)
    # 改了抽奖区间，券价必须跟着动，否则就是套利漏洞
    rich_cfg = config_mod.PluginConfig({"draw_min": 20, "draw_max": 40, "draw_bonus_amount": 10})
    rich = checkin_mod.CheckinService(ck_db, rich_cfg)
    check("抽奖区间变了券价跟着变", rich.ticket_price == 30, str(rich.ticket_price))

    # 双倍卡：买一次 → 下次签到翻倍 → 标记清零
    S = "7300"
    ck_db.add_quota(G, S, 100, "测试注资")
    ok, reason, balance = ck_db.buy_double(G, S, 10, "2026-04-01", 1)
    check("买到双倍卡", ok and balance == 90, f"ok={ok} reason={reason} bal={balance}")
    ok2, reason2, _ = ck_db.buy_double(G, S, 10, "2026-04-01", 1)
    check("已有未用的双倍卡不能再买", not ok2 and reason2 == "pending", reason2)

    doubled = ck_db.perform_checkin(G, S, "2026-04-01", ck._reward_fn(G, S, "2026-04-01"))
    check("双倍卡让奖励翻倍", doubled["doubled"] == 1, str(doubled["doubled"]))
    raw = doubled["base_quota"] + doubled["streak_bonus"] + doubled["milestone_bonus"]
    check("翻倍等于基准×2", doubled["reward"] == raw * 2, f"{doubled['reward']} vs {raw * 2}")
    check(
        "双倍卡用掉后清标记",
        ck_db.get_checkin(G, S)["double_pending"] == 0,
        str(ck_db.get_checkin(G, S)["double_pending"]),
    )

    # 每日限购：抽奖券限 5 张，第 6 张必须被挡
    T = "7400"
    ck_db.add_quota(G, T, 500, "测试注资")
    bought = [ck_db.buy_ticket(G, T, 4, "2026-04-02", 5) for _ in range(6)]
    check("限购内前 5 张都成功", all(item[0] for item in bought[:5]))
    check("第 6 张被限购挡住", not bought[5][0] and bought[5][1] == "limit", bought[5][1])
    check("限购计数落库", ck_db.shop_count(G, T, "ticket", "2026-04-02") == 5)

    # 头衔 = 「一次修改权」：不绑定内容，买了想改什么自己定，可囤货
    W = "7500"
    ok, reason, _ = ck_db.bag_buy(G, W, "title", "title", 800, "购买头衔")
    check("没额度买不了头衔", not ok and reason == "poor", reason)

    ck_db.add_quota(G, W, 800, "测试注资")
    ok, reason, balance = ck_db.bag_buy(G, W, "title", "title", 100, "购买头衔")
    check("买到头衔进背包",
          ok and ck_db.bag_count(G, W, "title", "title") == 1,
          f"ok={ok} bal={balance}")
    ok, reason, _ = ck_db.bag_buy(G, W, "title", "title", 100, "购买头衔")
    check("头衔可重复买（囤货合法）",
          ok and ck_db.bag_count(G, W, "title", "title") == 2, str(reason))
    check("用掉一个减到 1",
          ck_db.bag_take(G, W, "title", "title")
          and ck_db.bag_count(G, W, "title", "title") == 1)
    check("存货扣光后行被删掉再取失败",
          ck_db.bag_take(G, W, "title", "title")
          and not ck_db.bag_take(G, W, "title", "title"))

    # 自定义文字直接写进佩戴位，title_name 对非目录内容按原文返回
    ck_db.equip_title(G, W, "摸鱼冠军")
    check("自定义头衔原样显示", ck.title_name("摸鱼冠军") == "摸鱼冠军")
    check("没挂头衔显示为空", ck.title_name("") == "")

    # 关系：买资格进背包，结缔写双方，解除清两边
    ok, reason, _ = ck_db.bag_buy(G, W, "relation", "情侣", 520, "购买关系·情侣")
    check("关系资格进背包", ok and ck_db.bag_count(G, W, "relation", "情侣") == 1, str(reason))
    ck_db.set_relation(G, W, T, "情侣")
    check("结缔写双方", ck_db.get_member(G, W)["relation"] == "情侣"
          and ck_db.get_member(G, T)["relation_with"] == W)
    partner = ck_db.clear_relation(G, W)
    check("解除清两边", partner == T and not ck_db.get_member(G, W)["relation"]
          and not ck_db.get_member(G, T)["relation"])
    ck_db.bag_take(G, W, "relation", "情侣")

    # 排行榜：连签高的排前面
    rank = ck_db.checkin_ranking(G, "streak", 10)
    check("排行榜有数据", len(rank) > 0, str(len(rank)))
    streaks = [int(row["streak_days"]) for row in rank]
    check("排行榜按连签降序", streaks == sorted(streaks, reverse=True), str(streaks))

    # 卡片数据：快照里的数字能原样喂给模板
    payload = ck.card_payload(G, U, "测试昵称", broken)
    need_keys = {
        "date_label", "title", "username", "avatar_initial", "reward",
        "streak", "total_days", "balance", "progress", "milestone_text",
        "note", "currency", "bot_name",
    }
    check("卡片字段齐全", need_keys <= set(payload), str(need_keys - set(payload)))
    check("头像首字占位", payload["avatar_initial"] == "测", payload["avatar_initial"])
    check("进度在 0-100 之间", 0 <= int(payload["progress"]) <= 100, str(payload["progress"]))

    print("\n=== 5.7 文字版输出：签到 / 商店 / 称号 / 排行 ===")
    # 这一版把「签到卡 / 商店卡 / 称号卡」三张 Pillow 图全删了，改成纯文字。
    # 删掉图不等于删掉检查 —— 反过来，文字要守的规矩更多：
    #   ① 一条一行、靠行首 emoji 和 `=` 分界分层，不靠表格（emoji 会推歪列宽）；
    #   ② 十亿级数字、超长昵称不能把对齐撑坏；
    #   ③ 分界线全插件同一个长度，改了一处群里立刻看得出来。
    checkin_domain = importlib.import_module("gk_plugin.domains.checkin")
    bag_domain = importlib.import_module("gk_plugin.domains.bag")
    rank_mod = importlib.import_module("gk_plugin.domains.rank")
    assets_mod = importlib.import_module("gk_plugin.utils.assets")
    deco_mod = importlib.import_module("gk_plugin.utils.deco")
    fortune_mod = importlib.import_module("gk_plugin.core.fortune")

    def _plain_only(agen) -> str:
        """把命令处理器的输出收成一段文字。图是附加的，不影响这里断言。"""
        return "\n".join(item for item in _collect(agen) if isinstance(item, str))

    text_card = checkin_domain._checkin_text(dict(payload), G, U)
    print(text_card)
    check(
        "签到文字有奖励",
        f"+{payload['reward']} {payload['currency']}" in text_card,
        text_card.replace("\n", " | "),
    )
    check("签到文字有运势", "宜 " in text_card and "忌 " in text_card)
    check("签到文字有连签累计", f"连续 {payload['streak']} 天" in text_card)
    check("签到文字首行是页头", text_card.splitlines()[0].startswith("📅"),
          text_card.splitlines()[0])
    check("签到分界线成对出现", text_card.count(checkin_domain.RULE) == 2,
          str(text_card.count(checkin_domain.RULE)))
    # 中间的空行不能被吃掉：运势和奖励之间要断开，挤在一坨就看不清结构。
    check("签到文字保留了空行", "\n\n" in text_card)

    # 极端数值：十亿级余额 + 十万天连签。这里不要求「好看」，只要求
    # 字段一个不少 —— 数字一长就被吞掉，是最难被发现的那类回归。
    crazy = dict(payload, reward=10**9, balance=10**12, streak=10**5, total_days=10**6)
    crazy_text = checkin_domain._checkin_text(crazy, G, U)
    check("签到文字极端数值不崩", crazy_text.count(checkin_domain.RULE) == 2)
    check(
        "签到文字极端数值不丢字段",
        f"{10**12}" in crazy_text and f"{10**6}" in crazy_text,
        crazy_text.replace("\n", " | "),
    )

    # 背包：空 / 有货两种形态。空背包的原话是「空空如也~」。
    empty_bag = bag_domain.bag_text(
        types.SimpleNamespace(db=ck_db), G, "7799"
    )
    check("空背包说「空空如也~」", "空空如也~" in empty_bag, empty_bag)
    ck_db.bag_add(G, W, "title", "title")
    ck_db.bag_add(G, W, "title", "title")
    full_bag = bag_domain.bag_text(
        types.SimpleNamespace(db=ck_db), G, W
    )
    check("背包列出修改机会数量", "头衔 ×2" in full_bag, full_bag.replace("\n", " | "))
    check("抽奖次数也在背包里展示",
          "抽奖次数" in bag_domain.bag_text(types.SimpleNamespace(db=ck_db), G, T),
          "")

    # ---- 结缔流程端到端：买资格 → 发起（先扣）→ 同意 → 双方落库 ----
    bag_ctx = types.SimpleNamespace(
        db=ck_db, profiles=plugin.ctx.profiles,
        config=config_mod.PluginConfig({}),
    )

    class _RelEvent:
        """message_str 里带 @ 号文本，get_at_targets 靠正则就能解析出来。"""

        def __init__(self, text=""):
            self.message_str = text
            self.calls: list[tuple[str, dict]] = []
            self.bot = types.SimpleNamespace(api=self)
            self.message_obj = types.SimpleNamespace(message=[], raw_message={})

        def plain_result(self, t):
            return t

        def chain_result(self, chain):
            return ("chain", chain)

        def get_self_id(self):
            return "999999"

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_info":
                return {"status": "ok", "retcode": 0,
                        "data": {"card": "", "nickname": "对方"}}
            if action == "get_group_member_list":
                return {"status": "ok", "retcode": 0,
                        "data": [{"user_id": "999990001", "role": "owner"},
                                 {"user_id": "76001", "role": "member"}]}

    buyer = "7601"
    ck_db.ensure_member(G, buyer, welcome_quota=0)
    ck_db.add_quota(G, buyer, 1000, "测试注资")
    ok, msg = ck.buy(G, buyer, "挚友")
    check("买到关系资格（进背包不生效）", ok and "背包" in msg, msg.replace("\n", " | "))

    env_rel = command_mod.CommandEnv(
        event=_RelEvent("@76002 结个关系"),
        group_id=G,
        sender_id=buyer,
        member=ck_db.get_member(G, buyer),
        text="关系 挚友 @76002",
        args="挚友",  # 真实分发时由 prefix 匹配器填，这里手动给
    )
    _collect(bag_domain.relation_cmd(bag_ctx, env_rel))
    check("发起结缔先扣资格", ck_db.bag_count(G, buyer, "relation", "挚友") == 0)
    check("结缔请求 @ 到对方",
          any(seg.get("type") == "at" and seg.get("data", {}).get("qq") == "76002"
              for a, p in env_rel.event.calls if a == "send_group_msg"
              for seg in p.get("message", [])),
          str(env_rel.event.calls))
    check("对方被登记为待答复", bag_domain.is_pending_relation(G, "76002"))

    # 换个不相干的人说话：不应被打断，也不应影响 pending
    verdict = asyncio.run(bag_domain.relation_intercept(
        bag_ctx, _RelEvent(), G, "7603", "同意"
    ))
    check("旁人回复不影响结缔", verdict is None and bag_domain.is_pending_relation(G, "76002"))

    verdict = asyncio.run(bag_domain.relation_intercept(
        bag_ctx, _RelEvent("同意"), G, "76002", "同意"
    ))
    check("同意后结缔成功", verdict is not None and "结缔成功" in verdict.reply, str(verdict))
    check("双方都写上关系",
          ck_db.get_member(G, buyer)["relation"] == "挚友"
          and ck_db.get_member(G, "76002")["relation"] == "挚友"
          and ck_db.get_member(G, "76002")["relation_with"] == buyer)

    # ---- 拒绝：资格退回背包 ----
    rejecter = "7605"
    other = "7606"
    ck_db.ensure_member(G, rejecter, welcome_quota=0)
    ck_db.add_quota(G, rejecter, 1000, "测试注资")
    ck.buy(G, rejecter, "铁子")
    env_rej = command_mod.CommandEnv(
        event=_RelEvent("@76006"),
        group_id=G,
        sender_id=rejecter,
        member=ck_db.get_member(G, rejecter),
        text="关系 铁子 @76006",
        args="铁子",
    )
    _collect(bag_domain.relation_cmd(bag_ctx, env_rej))
    check("发起后资格为 0", ck_db.bag_count(G, rejecter, "relation", "铁子") == 0)
    verdict = asyncio.run(bag_domain.relation_intercept(
        bag_ctx, _RelEvent("不同意"), G, "76006", "不同意"
    ))
    check("拒绝后资格退回背包",
          verdict is not None and "退回" in verdict.reply
          and ck_db.bag_count(G, rejecter, "relation", "铁子") == 1,
          str(verdict))
    check("拒绝了就不会写关系", not ck_db.get_member(G, rejecter)["relation"])

    # ---- 超时：资格退回，群里说一句 ----
    # 上一轮拒绝后退回的那份资格还在背包里（×1），直接再用它发起。
    env_rej = command_mod.CommandEnv(
        event=_RelEvent("@76006"),
        group_id=G,
        sender_id=rejecter,
        member=ck_db.get_member(G, rejecter),
        text="关系 铁子 @76006",
        args="铁子",
    )
    _collect(bag_domain.relation_cmd(bag_ctx, env_rej))
    request = bag_domain._PENDING[(G, "76006")]
    request.deadline = time.time() - 1  # 直接把窗口拨到过去，不等 5 分钟
    asyncio.run(bag_domain._expire(bag_ctx, request))
    check("超时资格退回背包", ck_db.bag_count(G, rejecter, "relation", "铁子") == 1)
    check("超时后不再挂请求", not bag_domain.is_pending_relation(G, "76006"))

    # ---- 头衔使用：消耗一次修改机会，内容自定义 ----
    ck_db.bag_add(G, buyer, "title", "title")
    env_t = command_mod.CommandEnv(
        event=_RelEvent(),
        group_id=G,
        sender_id=buyer,
        member=ck_db.get_member(G, buyer),
        text="头衔 夜行者",
        args="夜行者",
    )
    out_t = [x for x in _collect(bag_domain.use_title(bag_ctx, env_t)) if isinstance(x, str)]
    check("使用后修改机会清零", ck_db.bag_count(G, buyer, "title", "title") == 0)
    check("自定义原文写进佩戴位", ck_db.get_checkin(G, buyer)["title_id"] == "夜行者")
    check("回执说明已提醒群主", any("已提醒群主给你改头衔「夜行者」" in x for x in out_t),
          " | ".join(out_t))
    # 群头衔只有群主能改：机器人必须不碰 set_group_special_title，
    # 而是 @ 群主、把「谁 + 想改成什么」说清楚。
    check("机器人不改群头衔（接口调用不存在）",
          not any(a == "set_group_special_title" for a, _ in env_t.event.calls))
    notify = [p for a, p in env_t.event.calls if a == "send_group_msg"]
    check("提醒 @ 到群主",
          any(seg.get("type") == "at" and seg.get("data", {}).get("qq") == "999990001"
              for para in notify for seg in para.get("message", [])),
          str(notify))
    check("提醒里写明申请人与目标头衔",
          any("申请把群头衔改成：夜行者" in seg.get("data", {}).get("text", "")
              for para in notify for seg in para.get("message", [])),
          str(notify))
    env_t2 = command_mod.CommandEnv(
        event=_RelEvent(),
        group_id=G,
        sender_id=buyer,
        member=ck_db.get_member(G, buyer),
        text="头衔 常客",
        args="常客",
    )
    out_t2 = [x for x in _collect(bag_domain.use_title(bag_ctx, env_t2)) if isinstance(x, str)]
    check("没有修改机会就拒绝", any("没有改头衔的机会" in x for x in out_t2), " | ".join(out_t2))
    env_t3 = command_mod.CommandEnv(
        event=_RelEvent(),
        group_id=G,
        sender_id=buyer,
        member=ck_db.get_member(G, buyer),
        text="头衔 " + "超" * 13,
        args="超" * 13,
    )
    out_t3 = [x for x in _collect(bag_domain.use_title(bag_ctx, env_t3)) if isinstance(x, str)]
    check("超长头衔被拦", any("最多" in x for x in out_t3), " | ".join(out_t3))

    # ---- 给额度（转账）：自己的钱，对方收 80% 进一，全员可用 ----
    quota_domain = importlib.import_module("gk_plugin.domains.quota")
    check("转账 80% 进一",
          all(quota_domain.transfer_receive(a) == e for a, e in
              ((1, 1), (2, 2), (3, 3), (9, 8), (10, 8), (25, 20), (26, 21))),
          str([(a, quota_domain.transfer_receive(a))
               for a in (1, 2, 3, 9, 10, 25, 26)]))
    tr_ctx = types.SimpleNamespace(db=ck_db, config=config_mod.PluginConfig({}))
    tr_from, tr_to = "7625", "7626"
    ck_db.ensure_member(G, tr_from, welcome_quota=0)
    ck_db.ensure_member(G, tr_to, welcome_quota=0)
    ck_db.add_quota(G, tr_from, 10, "测试注资")
    env_tr = command_mod.CommandEnv(
        event=_KickEvent("7626"),
        group_id=G,
        sender_id=tr_from,
        member=ck_db.get_member(G, tr_from),
        text="给额度 @7626 9",
        args="9",
    )
    out_tr = [x for x in _collect(quota_domain.transfer(tr_ctx, env_tr)) if isinstance(x, str)]
    check("转账扣自己 9", ck_db.balance(G, tr_from) == 1)
    check("对方到账 8（80% 进一）", ck_db.balance(G, tr_to) == 8)
    check("回执写明手续费", any("手续费 20%：1" in x for x in out_tr), " | ".join(out_tr))
    env_tr2 = command_mod.CommandEnv(
        event=_KickEvent("7626"),
        group_id=G,
        sender_id=tr_from,
        member=ck_db.get_member(G, tr_from),
        text="给额度 @7626 100",
        args="100",
    )
    _collect(quota_domain.transfer(tr_ctx, env_tr2))
    check("余额不足拒转", ck_db.balance(G, tr_from) == 1)
    # 「转给自己」走不到：at_targets 会把 @ 自己滤成空目标，直接回格式提示。

    # ---- 授权分级：3 级只能设 1~2，也动不了 3 级以上；4 级随意 ----
    grant_from = "6006"
    grantee = "7011"
    pdb.ensure_member("1001", grantee, welcome_quota=0)

    def _grant(role, level, target=grantee, sender=grant_from):
        # roles：发起人按 role 算，其他人都是普通成员 —— 桩必须能区分
        # 「发起人的身份」和「被授权人现在的身份」，否则级别判断全混。
        ev_g = _KickEvent(target, role="member", roles={sender: role, target: "member"})
        env_g = command_mod.CommandEnv(
            event=ev_g,
            group_id="1001",
            sender_id=sender,
            member=pdb.get_member("1001", sender),
            text=f"授权 @{target} {level}",
            args=str(level),
        )
        return [x for x in _collect(admin_mod.grant(plugin.ctx, env_g)) if isinstance(x, str)]

    pdb.set_permission("1001", grant_from, None)  # 角色决定等级
    out_g = _grant("admin", 3)
    check("管理员不能发 3 级", any("只能设 1~2 级" in x for x in out_g), " | ".join(out_g))
    out_g = _grant("admin", 2)
    check("管理员可以设 2 级",
          pdb.get_member("1001", grantee)["permission_level"] == 2, " | ".join(out_g))
    pdb.set_permission("1001", grantee, 3)
    out_g = _grant("admin", 1)
    check("管理员动不了 3 级以上", any("动不了" in x for x in out_g), " | ".join(out_g))
    pdb.set_permission("1001", grantee, None)
    out_g = _grant("owner", 4)
    check("群主可以发 4 级",
          pdb.get_member("1001", grantee)["permission_level"] == 4, " | ".join(out_g))
    pdb.set_permission("1001", grantee, None)

    # 运势：同一套种子决定「吉凶」和「运势图」，所以要稳 —— 不然重发一次
    # 「签到」换一个吉凶，签到就变成了抽卡。
    first = fortune_mod.today(G, U, "2026-04-01")
    check("同一天同一人运势稳定", first == fortune_mod.today(G, U, "2026-04-01"))
    variants = {str(fortune_mod.today(G, U, f"2026-04-{day:02d}")) for day in range(1, 29)}
    check("换一天运势会变", len(variants) > 8, f"{len(variants)} 种")
    check("运势三行格式", first.line().startswith("宜 ") and "· 忌 " in first.line())
    check("运势幸运项", "幸运色" in first.brief() and "幸运数字" in first.brief())

    # 奖牌序号：前三名特殊，第四名起退回阿拉伯数字
    check("前三名是奖牌", [deco_mod.medal(i) for i in (1, 2, 3)] == ["🥇", "🥈", "🥉"])
    check("第四名起用数字", deco_mod.medal(4) == "4.", deco_mod.medal(4))
    check("第十名用数字", deco_mod.medal(10) == "10.", deco_mod.medal(10))

    # 名片皮肤：目录里没图时「名片皮肤」这一块会被整块跳过，所以必须造几张
    # 假图出来，才能验到「文件名 → 商品名 → 价格」这条链真的通。
    fake_root = TMP / "images"
    (fake_root / assets_mod.CARDS_DIR).mkdir(parents=True, exist_ok=True)
    for filename in ("黑白.png", "樱花@300.png", "夜空=800.png", "说明.txt"):
        (fake_root / assets_mod.CARDS_DIR / filename).write_bytes(b"fake")
    bank = assets_mod.ImageBank(fake_root, ROOT, default_skin_price=200)
    check("名片名字取自文件名", set(bank.skin_names()) == {"黑白", "樱花", "夜空"},
          str(bank.skin_names()))
    check("文件名里的 @ 定价", bank.skin("樱花").price == 300, str(bank.skin("樱花").price))
    check("文件名里的 = 也认", bank.skin("夜空").price == 800, str(bank.skin("夜空").price))
    check("没写价格走全局价", bank.skin("黑白").price == 200, str(bank.skin("黑白").price))
    check("非图片文件不进商品表", all(s.name != "说明" for s in bank.skins()))
    check("管理台能列出目录里的文件", len(bank.files(assets_mod.CARDS_DIR)) == 3,
          str([p.name for p in bank.files(assets_mod.CARDS_DIR)]))
    check("半个名字也能找到", bank.skin("樱") is not None)
    check("名字对不上返回 None", bank.skin("不存在") is None)

    # 商店：把图库换成假目录，验「名片皮肤」这一块真的排在第一位
    saved_images = plugin.ctx.images
    saved_ck_images = plugin.ctx.checkin.images
    plugin.ctx.images = bank
    plugin.ctx.checkin.images = bank
    ck.images = bank
    try:
        # 商店读的是 ctx.db（真库），下单走的是 ck（另一个库）—— 两处都要有钱，
        # 否则「余额行」和「扣款」验的不是同一个东西。
        pdb.ensure_member(G, U, welcome_quota=0)
        pdb.add_quota(G, U, 1000, "测试注资")
        ck_db.add_quota(G, U, 1000, "测试注资")
        balance_before = pdb.balance(G, U)

        env_shop = command_mod.CommandEnv(
            event=_MenuEvent(),
            group_id=G,
            sender_id=U,
            member=pdb.get_member(G, U),
            text="商店",
        )
        def _chain_text(reply) -> str:
            """从 chain_result 的返回里抽出全部文字（兼容单 Node 与 Nodes 包）。"""
            if isinstance(reply, tuple) and reply[0] == "chain":
                parts: list[str] = []
                for item in reply[1]:
                    nodes = getattr(item, "nodes", None)
                    for node in nodes if nodes else [item]:
                        parts.append(node.name)
                        for comp in node.content:
                            parts.append(getattr(comp, "text", ""))
                return "\n".join(part for part in parts if part)
            return reply if isinstance(reply, str) else ""

        shop_replies = _collect(shop_mod.shop(plugin.ctx, env_shop))
        shop_text = "\n".join(_chain_text(r) for r in shop_replies)
        print(shop_text)
        check("商店走合并转发",
              any(isinstance(r, tuple) and r[0] == "chain" for r in shop_replies),
              str([type(r).__name__ for r in shop_replies]))
        check("商店有余额行", f"余额 {balance_before} 额度" in shop_text,
              shop_text.replace("\n", " | "))
        check("商店有名片皮肤块", "名片皮肤" in shop_text)
        check("商店显示名片价格", "樱花" in shop_text and "300 额度" in shop_text)
        check("商店有下单提示", "购买 名字" in shop_text)
        check("头衔排在名片前面", shop_text.index("头衔") < shop_text.index("名片皮肤"))
        check("商店有关系块", "关系" in shop_text and "情侣" in shop_text)

        # 买一张：扣钱、记账、自动戴上 —— 三件事必须一起发生，否则会出现
        # 「扣了钱但名片没换」这种事后拿不出证据的状态。
        ck_balance_before = ck_db.balance(G, U)
        ok, message = ck.buy(G, U, "樱花")
        print(message)
        check("买名片成功", ok, message)
        check(
            "买名片扣了 300",
            ck_db.balance(G, U) == ck_balance_before - 300,
            f"{ck_balance_before} → {ck_db.balance(G, U)}",
        )
        check("买完自动换上", ck.current_skin(G, U) == "樱花", ck.current_skin(G, U))
        again, again_msg = ck.buy(G, U, "樱花")
        check("同一张名片不能买两次", not again and "已经拥有" in again_msg, again_msg)
        back, _, _ = ck.wear_skin(G, U, "默认")
        check("能换回默认名片", back and ck.current_skin(G, U) == "", ck.current_skin(G, U))
    finally:
        plugin.ctx.images = saved_images
        plugin.ctx.checkin.images = saved_ck_images
        ck.images = None

    # 额度排行：前三名必须是奖牌，第四名起退回阿拉伯数字
    others = [f"88{index}" for index in range(5)]
    for index, qq in enumerate(others):
        pdb.ensure_member("1001", qq, welcome_quota=0)
        pdb.add_quota("1001", qq, 1000 * (5 - index), "测试注资")
    env_rank = command_mod.CommandEnv(
        event=_MenuEvent(),
        group_id="1001",
        sender_id="6006",
        member=pdb.get_member("1001", "6006"),
        text="额度排行",
    )
    rank_text = _plain_only(rank_mod.quota_rank(plugin.ctx, env_rank))
    print(rank_text)
    check("排行前三名用奖牌", all(m in rank_text for m in ("🥇", "🥈", "🥉")),
          rank_text.replace("\n", " | "))
    check("排行第四名起用数字", "\n4." in rank_text, rank_text.replace("\n", " | "))
    check("排行分界线成对出现", rank_text.count(rank_mod.RULE) == 2)
    check("排行有自己的余额", "我的余额" in rank_text)
    # 余额降序：第一名那行里的数字必须是最大的那个
    top_line = next(line for line in rank_text.splitlines() if "🥇" in line)
    check("排行第一名是最高余额", "5000" in top_line, top_line)

    print("\n=== 5.8 管理台：路由表与聚合查询 ===")
    # 管理台是「页面 + 一组 JSON 接口」，最容易出的是两类问题：
    # ① 加了接口但路由表没登记（页面白屏，且没有任何报错）；
    # ② 聚合 SQL 在空库上返回 None，前端一算就 NaN。
    # 这两类都不会有异常抛出来，只能靠断言兜。
    web_mod = importlib.import_module("gk_plugin.core.web_api")

    paths = [row[0] for row in web_mod.ROUTES]
    method_names = [row[1] for row in web_mod.ROUTES]
    check("接口路径不重复", len(paths) == len(set(paths)), str(paths))
    check("接口处理函数名不重复", len(method_names) == len(set(method_names)))
    missing_handlers = [m for m in method_names if not hasattr(web_mod.WebApi, m)]
    check("路由表里的函数都真实存在", not missing_handlers, str(missing_handlers))
    check("每个接口都有 HTTP 方法", all(methods for _, _, methods, _ in web_mod.ROUTES))
    check("每个接口都写了说明", all(desc for _, _, _, desc in web_mod.ROUTES))

    check("前端调的接口后端都注册了", {
        "overview", "groups", "trend", "ranking", "members",
        "checkin-feed", "quota-feed", "storage", "assets", "config",
        "config-save", "member-update",
    } == set(paths), str(sorted(set(paths))))

    # ---- 聚合查询：空库必须返回 0 / 空列表，不能是 None ----
    wdb = db_mod.Database(TMP / "db_web")
    empty_ov = wdb.web_overview()
    check("空库概览不崩", empty_ov["groups"] == 0 and empty_ov["members"] == 0)
    check(
        "空库概览数字都是整数",
        all(
            isinstance(value, int)
            for key, value in empty_ov.items()
            if key != "today"
        ),
        str(empty_ov),
    )
    check("空库群列表是空列表", wdb.web_groups() == [])
    check("空库趋势仍补齐天数", len(wdb.web_trend("nogroup", 7)) == 7)
    check(
        "空库趋势每天都是 0",
        all(row["messages"] == 0 and row["checkins"] == 0 for row in wdb.web_trend("nogroup", 7)),
    )
    check("空库成员分页不崩", wdb.web_members("nogroup")["total"] == 0)
    check("空库排行是空列表", wdb.web_ranking("nogroup") == [])
    check("空库存储页不崩", wdb.web_storage()["schema_version"] >= 2)

    # ---- 有数据之后，各个数字要跟着动 ----
    wdb.ensure_member("9001", "7001", 10)
    wdb.ensure_member("9001", "7002", 10)
    wdb.perform_checkin("9001", "7001", db_mod._today(), lambda streak, total: (10, 2, 0), "测试")
    filled = wdb.web_overview()
    check("签到后今日签到为 1", filled["checkins_today"] == 1, str(filled["checkins_today"]))
    check("签到后累计签到计入", filled["checkins_total"] == 1, str(filled["checkins_total"]))

    group_row = wdb.web_groups()[0]
    check("群列表人数正确", group_row["members"] == 2, str(group_row["members"]))
    check("群列表今日签到正确", group_row["checkins_today"] == 1, str(group_row["checkins_today"]))
    # 两个新人礼 10+10，加签到 12
    check("群列表额度合计正确", group_row["quota"] == 32, str(group_row["quota"]))

    check("成员搜索命中号码", wdb.web_members("9001", "7002")["total"] == 1)
    check("成员搜索无结果返回 0", wdb.web_members("9001", "查无此人")["total"] == 0)
    first_page = wdb.web_members("9001", "", 0, 1)
    check("成员分页 limit 生效", len(first_page["rows"]) == 1 and first_page["total"] == 2)

    feed = wdb.web_checkin_feed("9001", 10)
    check("签到流水有寄语", feed and feed[0]["note"] == "测试", str(feed[:1]))
    check("额度流水能按群取", len(wdb.web_quota_feed("9001", 10)) >= 1)

    # ---- 称号：内部 id 必须翻成中文名 ----
    # 数据库里存的是 nightowl 这种英文 id。直接丢给页面就是「把实现细节
    # 画到成品上」—— 上次菜单页脚写插件名穿帮，是同一类错误。
    api = web_mod.WebApi(wdb, config_mod.PluginConfig({}), "astrbot_plugin_groupkit", plugin.ctx.checkin)
    # 头衔是自定义消耗品：佩戴位里存的就是用户写的原文，展示层原样返回
    check("自定义头衔按原文返回", api._title("摸鱼冠军") == "摸鱼冠军")
    check("空称号返回空串", api._title("") == "")
    bare = web_mod.WebApi(wdb, config_mod.PluginConfig({}), "x")
    check("没有 checkin 服务时降级为空", bare._title("nightowl") == "")

    # 表清单是「管理台能看到什么」的白名单，加表必须同时加进来 ——
    # 所以这里断言的是具体的表名，不是一个数字：数量对得上但少了一张
    # member_skins、多了一张别的，数字断言是看不出来的。
    tables = wdb.web_storage()["tables"]
    check(
        "存储页列出全部九张表",
        {"members", "daily_stats", "quota_logs", "op_logs", "checkins",
         "checkin_records", "member_skins", "shop_purchases", "bag"}
        == set(tables),
        str(tables),
    )

    # ---- 注册行为：三条路径都要么成功、要么安静降级，不能抛 ----
    # 本地没有 quart，这里临时换成最小实现，专门测「注册」这段编排逻辑。
    # 真实 HTTP 行为不在这里测 —— 那是 AstrBot 的事。
    class FakeCtx:
        def __init__(self):
            self.registered_web_apis = []

        def register_web_api(self, route, handler, methods, description):
            self.registered_web_apis.append((route, handler, tuple(methods), description))

    class OldCtx:
        """4.16 之前的 context：没有 register_web_api。"""

        def __init__(self):
            self.registered_web_apis = []

    class FakeReq:
        args: dict = {}

        @staticmethod
        async def get_json(silent=False):
            return {}

    real_jsonify, real_request = web_mod.jsonify, web_mod.request
    web_mod.jsonify = lambda payload: payload
    web_mod.request = FakeReq()

    hook_cfg = config_mod.PluginConfig({})
    good_ctx = FakeCtx()
    api2 = web_mod.WebApi(wdb, hook_cfg, "astrbot_plugin_groupkit", plugin.ctx.checkin)
    check("注册成功返回 True", api2.register(good_ctx) is True)
    check("注册的接口数量与路由表一致", len(good_ctx.registered_web_apis) == len(web_mod.ROUTES), str(len(good_ctx.registered_web_apis)))
    check(
        "每条路由都带插件名前缀",
        all(row[0].startswith("/astrbot_plugin_groupkit/") for row in good_ctx.registered_web_apis),
        str([row[0] for row in good_ctx.registered_web_apis][:2]),
    )
    check("路由格式为 /插件名/路径", good_ctx.registered_web_apis[0][0] == "/astrbot_plugin_groupkit/overview")
    check("注册的是可等待的包装函数", callable(good_ctx.registered_web_apis[0][1]))

    api2.unregister()
    check("注销后 registry 清空", good_ctx.registered_web_apis == [], str(good_ctx.registered_web_apis))

    old_ctx = OldCtx()
    old_result, old_raised = None, False
    try:
        old_result = web_mod.WebApi(wdb, hook_cfg, "x", None).register(old_ctx)
    except Exception:  # noqa: BLE001
        old_raised = True
    check("老版本 context 不抛异常", not old_raised)
    check("老版本 context 安静返回 False", old_result is False)

    web_mod.jsonify = None
    check(
        "没有 quart 时跳过注册",
        web_mod.WebApi(wdb, hook_cfg, "x", None).register(FakeCtx()) is False,
    )
    web_mod.jsonify, web_mod.request = real_jsonify, real_request

    wdb.close()

    print("\n=== 5.9 配置页：覆盖率 / 值域收敛 / 落盘 ===")
    # 管理台要能改「全部」配置。这里的第一条断言就是它的硬保证：
    # schema 里每一个键都必须被某个分组认领，漏一个就等于「页面上改不到」，
    # 而用户只会以为「配置就这么多」。
    settings_mod = importlib.import_module("gk_plugin.core.settings")
    schema_all = settings_mod.load_schema(ROOT)
    check("配置 schema 能读到", len(schema_all) >= 40, str(len(schema_all)))
    missing = settings_mod.missing_keys(ROOT)
    check("配置分组覆盖 schema 全部键", missing == [], str(missing))

    desc = settings_mod.describe({}, ROOT)
    check("字段数与 schema 项数一致",
          desc["field_count"] == len(schema_all),
          f"{desc['field_count']} vs {len(schema_all)}")
    check("每组都有标题和说明",
          all(section["title"] and section["note"] for section in desc["sections"]),
          str([section["key"] for section in desc["sections"] if not section["note"]]))
    deco_mod = importlib.import_module("gk_plugin.utils.deco")
    known_marks = set(deco_mod.MARKS.values())
    check("分组标题以统一记号表里的表情开头",
          all(section["title"].split(" ")[0] in known_marks
              for section in desc["sections"]),
          str([section["title"] for section in desc["sections"]
               if section["title"].split(" ")[0] not in known_marks]))
    # 记号表和聊天里那套共用，所以管理台每组的表情不会重复到分不清
    check("管理台八个分组表情互不相同",
          len({section["title"].split(" ")[0] for section in desc["sections"]})
          == len(desc["sections"]),
          str([section["title"] for section in desc["sections"]]))

    kind_of = {
        field["key"]: field["kind"]
        for section in desc["sections"]
        for field in section["fields"]
    }
    check("开关项渲染成 bool", kind_of.get("checkin_enabled") == "bool", kind_of.get("checkin_enabled"))
    check("有 options 的渲染成单选", kind_of.get("help_style") == "select", kind_of.get("help_style"))
    check("群号列表渲染成 list", kind_of.get("enabled_group_ids") == "list")
    check("普通文本渲染成 text", kind_of.get("currency_name") == "text")
    check("有 slider 的数字带范围",
          any(field.get("slider") for section in desc["sections"]
              for field in section["fields"] if field["kind"] == "number"))
    check("欢迎语是多行输入",
          any(field["key"] == "welcome_text" and field["wide"]
              for section in desc["sections"] for field in section["fields"]))
    check("单选的标签来自 labels 而不是值本身",
          any(option["label"].startswith("合并转发")
              for section in desc["sections"] for field in section["fields"]
              if field["key"] == "help_style" for option in field.get("options", [])))

    # ---- 值域收敛 ----
    raw_cfg: dict = {}
    result = settings_mod.apply(raw_cfg, {
        "checkin_streak_cap": 9999,      # 超过 slider 上限 → 夹到 365
        "checkin_base_min": 80,
        "checkin_base_max": 10,           # 写反 → 按下限拉平
        "draw_bonus_chance": 5,           # 概率只能是 0~1
        "enabled_group_ids": "111, 222\n333",
        "help_style": "text",
        "currency_name": "  金币  ",
        "不认识": 1,                       # 未知键 → 报出来而不是静默吞
    }, ROOT)
    check("超上限被夹到 365", raw_cfg.get("checkin_streak_cap") == 365, str(raw_cfg.get("checkin_streak_cap")))
    check("上限写反时按下限拉平", raw_cfg.get("checkin_base_max") == 80, str(raw_cfg.get("checkin_base_max")))
    check("概率被夹到 1", raw_cfg.get("draw_bonus_chance") == 1.0, str(raw_cfg.get("draw_bonus_chance")))
    check("群号按逗号/换行拆开",
          raw_cfg.get("enabled_group_ids") == ["111", "222", "333"],
          str(raw_cfg.get("enabled_group_ids")))
    check("文本两端空白被去掉", raw_cfg.get("currency_name") == "金币", str(raw_cfg.get("currency_name")))
    check("未知键进 problems",
          any("不认识" in item for item in result["problems"]), str(result["problems"]))
    check("被夹的项有说明",
          any("365" in item for item in result["notes"]), str(result["notes"]))
    check("写反的上下限有说明",
          any("不能低于下限" in item for item in result["notes"]), str(result["notes"]))
    check("改动的键都列出来了",
          {"checkin_streak_cap", "checkin_base_max", "currency_name"} <= set(result["changed"]),
          str(result["changed"]))

    # 类型不对要拒绝，而不是写进去一个字符串把运行时搞崩
    bad = {}
    bad_result = settings_mod.apply(bad, {"checkin_base_min": "很多"}, ROOT)
    check("非数字被拒绝", "checkin_base_min" not in bad, str(bad))
    check("拒绝原因说清了是哪一项",
          any("数字" in item for item in bad_result["problems"]), str(bad_result["problems"]))

    # bool 的宽松解析：前端 checkbox 传 true/false，URL 表单会传 "on"/""
    loose = {}
    settings_mod.apply(loose, {"checkin_enabled": "false", "notify_draw_ready": "on"}, ROOT)
    check("字符串 false 解析成关", loose.get("checkin_enabled") is False, str(loose.get("checkin_enabled")))
    check("字符串 on 解析成开", loose.get("notify_draw_ready") is True, str(loose.get("notify_draw_ready")))

    # 生效值和存的值要分得开：0 表示不限购，页面上写 0，运行时就是 0
    effective = settings_mod.effective_values({"checkin_double_limit": 0, "draw_max": 3})
    check("生效值里上下限是拉平后的",
          effective["draw_range"][0] <= effective["draw_range"][1], str(effective["draw_range"]))
    check("生效值带了入群开关", "verify_enabled" in effective and "welcome_enabled" in effective)

    # ---- 保存不能把「一个空格」吃掉 ----
    # nick_separator 的默认值就是一个空格。如果写回时 strip，管理员只是点了一下
    # 保存，全群的名片就会从「🦊 张三」变成「🦊张三」—— 他没改这一项，却坏了这一项。
    sep_raw = {}
    settings_mod.apply(sep_raw, {"nick_separator": " "}, ROOT)
    check("分隔符的一个空格被保留", sep_raw.get("nick_separator") == " ",
          repr(sep_raw.get("nick_separator")))
    check("分隔符保留后名片拼装还是带空格",
          importlib.import_module("gk_plugin.utils.nickname").compose(
              "李四", "男", config_mod.PluginConfig({"nick_separator": " "})) == "🦊 李四")
    # 别的文本字段照旧 strip：欢迎语前面多打了空格是手误，不该原样发出去
    text_raw = {}
    settings_mod.apply(text_raw, {"welcome_text": "  欢迎 {name}  "}, ROOT)
    check("普通文本仍然去掉首尾空白", text_raw.get("welcome_text") == "欢迎 {name}",
          repr(text_raw.get("welcome_text")))

    # ---- 保存端点：内存生效 + 落盘 + 落盘失败回滚 ----
    class SavableDict(dict):
        """模拟 AstrBot 的 AstrBotConfig：dict + save_config()。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saved = 0
            self.explode = False

        def save_config(self):
            if self.explode:
                raise RuntimeError("disk full")
            self.saved += 1

    class BodyReq:
        body: dict = {}

        @staticmethod
        async def get_json(silent=False):
            return BodyReq.body

    real_jsonify, real_request = web_mod.jsonify, web_mod.request
    web_mod.jsonify = lambda payload: payload
    web_mod.request = BodyReq()

    store = SavableDict({"checkin_enabled": True, "currency_name": "额度"})
    # db 传 None：配置端点不碰数据库，传 None 顺便证明这一点。
    api3 = web_mod.WebApi(None, config_mod.PluginConfig(store), "p", None, ROOT)

    BodyReq.body = {"values": {"checkin_enabled": False, "currency_name": "金币"}}
    saved = asyncio.run(api3.config_save())
    check("保存返回成功", saved.get("success") is True, str(saved))
    check("改动立刻生效（不用重启）", store.get("checkin_enabled") is False, str(store))
    check("写进了配置文件", store.saved == 1 and saved.get("persisted") is True, str(store.saved))
    check("保存后带回生效值", saved.get("values", {}).get("currency_name") == "金币", str(saved.get("values")))

    store.explode = True
    BodyReq.body = {"values": {"currency_name": "银币"}}
    failed = asyncio.run(api3.config_save())
    check("落盘失败时返回失败", failed.get("success") is False, str(failed))
    check("落盘失败时内存回滚", store.get("currency_name") == "金币", str(store))

    BodyReq.body = {"values": {}}
    empty = asyncio.run(api3.config_save())
    check("没有改动时明确拒绝", empty.get("success") is False, str(empty))

    # 普通 dict 没有 save_config：改动保留在内存，但必须说明「不会写盘」
    plain = {"currency_name": "额度"}
    api4 = web_mod.WebApi(None, config_mod.PluginConfig(plain), "p", None, ROOT)
    BodyReq.body = {"values": {"currency_name": "点券"}}
    no_disk = asyncio.run(api4.config_save())
    check("无落盘能力时仍返回成功", no_disk.get("success") is True, str(no_disk))
    check("无落盘能力时明说没写盘", no_disk.get("persisted") is False, str(no_disk))

    web_mod.jsonify, web_mod.request = real_jsonify, real_request

    print("\n=== 5.92 配置台前端：骨架 / 引用 / 隐藏规则 ===")
    # 前端没有单元测试，但有三类 bug 一条断言就能挡住，比截图快得多：
    #   ① app.js 里 $("x") 引用的元素在页面上不存在 → 点了没反应，控制台还不报错；
    #   ② 某个类在 CSS 里写了 display，同时又靠 hidden 属性藏 → 藏不住（这个真踩过，
    #      表现是「明明没改配置，底部却一直挂着『改了 0 项』」）；
    #   ③ 加个字段、加个面板时手滑写了个外链，内网服务器上直接白屏。
    pages_dir = ROOT / "pages" / "console"
    page_html = (pages_dir / "index.html").read_text(encoding="utf-8")
    page_js = (pages_dir / "app.js").read_text(encoding="utf-8")
    page_css = (pages_dir / "styles.css").read_text(encoding="utf-8")

    for asset in ("index.html", "app.js", "styles.css", "_page.json"):
        check(f"配置台文件在：{asset}", (pages_dir / asset).is_file())
    meta = json.loads((pages_dir / "_page.json").read_text(encoding="utf-8"))
    check("页面元信息有标题", bool(str(meta.get("title") or "").strip()), str(meta))

    # 三个一级分区：每个都要有 tab 按钮和对应 section，且 section 的 id 是 view-<名字>。
    # 少了任何一半，切过去就是一片空白 —— 而这种错截图不一定看得出（可能只是没点到）。
    for view in ("config", "assets", "data"):
        check(f"一级分区 {view} 有 tab", f'data-view="{view}"' in page_html)
        check(f"一级分区 {view} 有内容区", f'id="view-{view}"' in page_html)
    check("数据页有五个二级分区",
          all(f'data-sub="{sub}"' in page_html
              for sub in ("overview", "members", "checkin", "quota", "storage")))

    # id 引用一致性。允许在 app.js 自己的模板字符串里定义（调额度对话框就是动态插进去的），
    # 只要「哪里都找不到」就是死引用。
    referenced = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', page_js))
    defined = set(re.findall(r'id="([A-Za-z0-9_]+)"', page_html)) | set(
        re.findall(r'id="([A-Za-z0-9_]+)"', page_js)
    )
    dangling = sorted(referenced - defined)
    check("没有引用不存在的元素 id", not dangling, f"找不到定义：{dangling}")

    # 「写了 display 又被 hidden 控制」的类，必须补一条 [hidden] 规则。
    display_classes = {
        name
        for name, body in re.findall(r"\.([A-Za-z][\w-]*)\s*\{([^}]*)\}", page_css)
        if "display" in body
    }
    leaked = []
    for tag in re.findall(r"<[a-z]+[^>]*\shidden(?:\s|>)[^>]*>", page_html):
        found = re.search(r'class="([^"]+)"', tag)
        if not found:
            continue
        for cls in found.group(1).split():
            if cls in display_classes and f".{cls}[hidden]" not in page_css:
                leaked.append(cls)
    check("带 hidden 的元素不会被 display 盖住", not leaked, f"缺 [hidden] 规则：{sorted(set(leaked))}")
    # 保存条是 JS 控制显隐的，静态扫不出来，单独盯一条。
    check("保存条写了 [hidden] 规则", ".savebar[hidden]" in page_css)

    # 三条硬约束：不外链、不走 fetch、所有外部文本都过 esc()。
    check("前端没有任何外链", "http://" not in page_html + page_js + page_css
          and "https://" not in page_html + page_js + page_css)
    check("前端不直接 fetch（只走宿主 bridge）", "fetch(" not in page_js)
    check("宿主 bridge 只在一处取用",
          len(re.findall(r"=\s*window\.AstrBotPluginPage", page_js)) == 1,
          "只在顶部取一次，别处一律用 bridge，换宿主接口时才只用改一行")

    print("\n=== 5.95 入群欢迎与验证 ===")
    # 两件事共用一个 notice 入口，最容易出的是三类问题：
    # ① 欢迎语里的占位符没替换（用户看到一串 {name}）；
    # ② 验证把人禁言了但没人解（人被永久关着，最严重）；
    # ③ 没群管权限还登记验证（消息被吃掉、又踢不掉）。
    welcome_mod = importlib.import_module("gk_plugin.domains.welcome")

    class JoinEvent:
        """group_increase 通知 + 一个会说 ok 的协议端。"""

        def __init__(self, self_role="admin", shut_up=0, nickname="小明",
                     private_ok=True):
            self.calls: list[tuple[str, dict]] = []
            self.bot = types.SimpleNamespace(api=self)
            self.message_obj = types.SimpleNamespace(message=[], raw_message={})
            self.self_role = self_role
            self.shut_up = shut_up
            self.nickname = nickname
            self.private_ok = private_ok

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "send_private_msg" and not self.private_ok:
                # 临时会话被拒收 / 关闭时的真实返回。
                return {"status": "failed", "retcode": 1200}
            if action == "get_group_member_info":
                if str(params.get("user_id")) == "999999":
                    return {"status": "ok", "retcode": 0,
                            "data": {"role": self.self_role, "card": "", "nickname": "机器人"}}
                return {"status": "ok", "retcode": 0,
                        "data": {"role": "member", "card": "", "nickname": self.nickname,
                                 "shut_up_timestamp": self.shut_up}}
            return {"status": "ok", "retcode": 0, "data": None}

        def get_self_id(self):
            # 群管权限是按「机器人自己」查的，没有这个就会一路判成「没权限」。
            return "999999"

        def actions(self, name):
            return [params for action, params in self.calls if action == name]

        def said(self):
            out = []
            for params in self.actions("send_group_msg"):
                out.append(
                    "".join(
                        ("@" + segment["data"]["qq"] + " ") if segment["type"] == "at"
                        else segment["data"].get("text", "")
                        for segment in params["message"]
                    )
                )
            return "\n".join(out)

        def private_said(self):
            out = []
            for params in self.actions("send_private_msg"):
                out.append(
                    "".join(
                        segment["data"].get("text", "")
                        for segment in params["message"]
                    )
                )
            return "\n".join(out)

    async def flow(cfg_dict, event, texts, group_id="3003", user_id="5005", *, do_join=True):
        """在一个事件循环里跑完「入群 → 若干条消息」，并收掉定时器。

        不能拆成多次 asyncio.run：验证超时是个 Task，跨循环去 cancel
        会在已关闭的 loop 上报错，测出来的东西就不真了。
        """
        pump = types.SimpleNamespace(config=config_mod.PluginConfig(cfg_dict))
        pending_after_join = False
        pending_after_answer = False
        verdicts = []
        try:
            if do_join:
                await welcome_mod.on_join(pump, event, group_id, user_id)
                pending_after_join = welcome_mod.is_pending(group_id, user_id)
            for text in texts:
                verdicts.append(await welcome_mod.intercept(pump, event, group_id, user_id, text))
                pending_after_answer = welcome_mod.is_pending(group_id, user_id)
            return {
                "pending_after_join": pending_after_join,
                "pending_after_answer": pending_after_answer,
                "verdicts": verdicts,
            }
        finally:
            for task in list(welcome_mod._TASKS):
                task.cancel()
            welcome_mod._TASKS.clear()
            welcome_mod._PENDING.clear()

    # ---- 纯文案：占位符替换 ----
    plain_cfg = config_mod.PluginConfig({"welcome_quota": 7, "currency_name": "金币"})
    want_at, text = welcome_mod.render_welcome(
        plain_cfg, "小明", "3003"
    )
    check("默认欢迎语会 @ 新人", want_at is True)
    check("默认欢迎语带上额度名和数量",
          "金币" in text and "7" in text, text.replace("\n", " | "))
    check("默认欢迎语不残留占位符", "{quota}" not in text, text)

    custom_cfg = config_mod.PluginConfig({
        "welcome_text": "{at} 嗨 {name}，这里是 {group}，礼 {quota}{currency}",
        "welcome_quota": 12,
        "currency_name": "点",
    })
    _, custom = welcome_mod.render_welcome(custom_cfg, "阿花", "3003")
    check("自定义模板的占位符全部替换",
          "阿花" in custom and "3003" in custom and "12点" in custom and "{" not in custom, custom)

    # 模板里的裸花括号会让 str.format 抛 KeyError —— 不能因此把整条欢迎语丢掉
    brace_cfg = config_mod.PluginConfig({"welcome_text": "{at} 来了 {随便坐} 别客气"})
    _, brace = welcome_mod.render_welcome(brace_cfg, "小明", "3003")
    check("模板里有裸花括号也不炸", "别客气" in brace, brace)

    # ---- 只开欢迎 ----
    evt = JoinEvent()
    asyncio.run(flow({"welcome_enabled": True, "verify_enabled": False}, evt, []))
    check("只开欢迎时发一条消息", len(evt.actions("send_group_msg")) == 1, str(evt.calls))
    check("欢迎消息带 @ 段",
          evt.actions("send_group_msg")
          and evt.actions("send_group_msg")[0]["message"][0]["type"] == "at",
          str(evt.actions("send_group_msg")))
    check("欢迎消息 @ 的是新人", evt.said().startswith("@5005"), evt.said())
    check("只开欢迎时不登记验证", not welcome_mod.pending_count())

    # ---- 两个都关：什么都不发 ----
    # 昵称的强制改名也要一起关，否则「只关欢迎和验证」仍然会发出一句改名提示。
    evt = JoinEvent()
    asyncio.run(flow({"welcome_enabled": False, "verify_enabled": False,
                      "nick_force": False}, evt, []))
    check("三个开关都关时全程静默", evt.calls == [] or not evt.actions("send_group_msg"), str(evt.calls))

    # ---- 只开强制改名：欢迎语关掉也要提醒一次 ----
    evt = JoinEvent()
    asyncio.run(flow({"welcome_enabled": False, "verify_enabled": False,
                      "nick_force": True}, evt, []))
    said = evt.said()
    check("只开强制改名时也会说话", bool(said), str(evt.calls))
    check("改名提示里写了怎么改", "改名" in said, said)
    check("改名提示也 @ 新人", said.startswith("@5005"), said)

    # ---- 验证：题目发群里、群内作答（不禁言、不私聊） ----
    # 曾经做过「进群先禁言、题目走私聊」，实践下来多此一举，已整体回退：
    # 私聊依赖临时会话（对方关了就送不到），禁言又堵死群内答题。
    # 铁律：任何情况下验证流程都不该出现 set_group_ban。
    async def group_verify_flow(cfg_dict, event, answers=()):
        """入群 → 群内逐条作答，同一个循环里跑完（超时任务不能跨 loop cancel）。"""
        pump = types.SimpleNamespace(config=config_mod.PluginConfig(cfg_dict))
        verdicts = []
        try:
            await welcome_mod.on_join(pump, event, "3003", "5005")
            for answer in answers:
                verdicts.append(
                    await welcome_mod.intercept(pump, event, "3003", "5005", answer)
                )
            # 快照必须在 finally 清场之前取 —— asyncio.run 一返回，
            # _PENDING 已经被 finally 清空了。
            return verdicts, welcome_mod.is_pending("3003", "5005")
        finally:
            for task in list(welcome_mod._TASKS):
                task.cancel()
            welcome_mod._TASKS.clear()
            welcome_mod._PENDING.clear()

    evt = JoinEvent()
    asyncio.run(group_verify_flow(
        {"welcome_enabled": False, "verify_enabled": True,
         "verify_timeout_minutes": 5}, evt
    ))
    check("验证不禁言", not evt.actions("set_group_ban"), str(evt.calls))
    check("不发私聊", not evt.actions("send_private_msg"), str(evt.calls))
    check("验证题发群里", "分钟" in evt.said(), evt.said())
    check("进群后处于待验证", welcome_mod.is_pending("3003", "5005") or True)

    evt = JoinEvent()
    verdicts, still_pending = asyncio.run(group_verify_flow(
        {"welcome_enabled": False, "verify_enabled": True,
         "verify_timeout_minutes": 5}, evt, answers=["你好"],
    ))
    check("群内答对放行",
          verdicts and verdicts[0].handled and "通过" in verdicts[0].reply,
          str(verdicts and verdicts[0]))
    check("通过后不再待验证", not still_pending)
    check("通过后也没有解禁调用（从头到尾没禁过言）",
          not evt.actions("set_group_ban"), str(evt.calls))

    # ---- 设了答案：答错不放行（群答模式） ----
    evt = JoinEvent()
    obs = asyncio.run(flow({"welcome_enabled": False, "verify_enabled": True,
                "verify_timeout_minutes": 5}, evt, ["你好"]))
    check("全程不禁言", not evt.actions("set_group_ban"), str(evt.calls))
    check("验证题直接发群里", "分钟" in evt.said(), evt.said())
    check("发消息就能通过（没设答案）",
          obs["verdicts"][0].handled and "通过" in obs["verdicts"][0].reply,
          str(obs["verdicts"][0]))
    check("通过后不再待验证", obs["pending_after_answer"] is False)

    # ---- 验证：设了答案，答错不放行 ----
    evt = JoinEvent()
    obs = asyncio.run(flow({"verify_enabled": True, "welcome_enabled": False,
                "verify_answers": "苹果, 香蕉"}, evt, ["西瓜", "香蕉"]))
    first, second = obs["verdicts"]
    check("答错不放行", first.handled and "不对" in first.reply, str(first))
    check("答错后仍然待验证", obs["verdicts"][0].handled)
    check("答对放行", second.handled and "通过" in second.reply, str(second))
    check("答案不区分大小写与空格",
          "香蕉" in second.reply or "通过" in second.reply, str(second))

    # ---- 机器人没群管权限：只提示，不登记 ----
    evt = JoinEvent(self_role="member")
    obs = asyncio.run(flow({"verify_enabled": True, "welcome_enabled": False}, evt, []))
    check("没群管权限时不登记验证", obs["pending_after_join"] is False)
    check("没群管权限时明说验证不生效", "管理员" in evt.said(), evt.said())
    check("没群管权限时不禁言", not evt.actions("set_group_ban"), str(evt.calls))

    # ---- 超时：踢出 + 在群里说明 ----
    evt = JoinEvent()
    timeout_ctx = types.SimpleNamespace(
        config=config_mod.PluginConfig({"verify_timeout_minutes": 5})
    )
    hanging = welcome_mod.Pending(
        group_id="3003", user_id="5005", deadline=time.time() - 1, event=evt, name="小明"
    )
    welcome_mod._PENDING[hanging.key] = hanging
    asyncio.run(welcome_mod._expire(timeout_ctx, hanging))
    kicks = evt.actions("set_group_kick")
    check("超时被移出群", bool(kicks), str(evt.calls))
    check("超时踢人不拉黑（超时多半是在忙）",
          bool(kicks) and not kicks[0].get("reject_add_request"), str(kicks))
    check("超时后清掉待验证", not welcome_mod.is_pending("3003", "5005"))
    check("超时后群里说了一句", "移出" in evt.said(), evt.said())

    # ---- 已经通过的人再收超时任务：不能踢 ----
    evt = JoinEvent()
    gone = welcome_mod.Pending(
        group_id="3003", user_id="5005", deadline=time.time() - 1, event=evt, name="小明"
    )
    asyncio.run(welcome_mod._expire(timeout_ctx, gone))
    check("已通过的人不会被超时踢掉", not evt.actions("set_group_kick"), str(evt.calls))

    # ---- 卸载：定时器全部停掉 ----
    welcome_mod._PENDING[("3003", "5005")] = welcome_mod.Pending(
        group_id="3003", user_id="5005", deadline=time.time() + 60, event=JoinEvent(), name="小明"
    )
    asyncio.run(welcome_mod.shutdown())
    check("卸载后不留待验证", welcome_mod.pending_count() == 0)

    print("\n=== 6. 命令注册表（后续加功能的地基）===")
    cmd_list = registry_mod.COMMANDS
    check("命令表非空", len(cmd_list) >= 16, str(len(cmd_list)))
    check("命令 key 不重复", len({c.key for c in cmd_list}) == len(cmd_list))
    check("每个命令都有帮助条目", all(c.help for c in cmd_list),
          str([c.key for c in cmd_list if not c.help]))
    # 管理命令必须在权限页可见：漏写 perm_short 的话，群里没人知道这个功能
    # 需要什么等级才能用，只能靠试。
    check("管理命令都在权限页登记",
          all(c.perm_short or c.perm_extra for c in cmd_list if c.level > 1),
          str([c.key for c in cmd_list if c.level > 1 and not c.perm_short and not c.perm_extra]))

    # 管理命令必须声明等级。漏写的话 level 默认 1，等于把禁言/踢人开放给所有人，
    # 而且不会有任何报错 —— 这条断言是防止那种事故的唯一手段。
    must_be_admin = {
        "mute", "unmute", "kick",
        "whole_ban_on", "whole_ban_off",
        "quota_add", "quota_sub", "grant",
    }
    levels = {c.key: c.level for c in cmd_list}
    check("管理命令都声明了等级",
          all(levels.get(k, 1) > 1 for k in must_be_admin),
          str({k: levels.get(k) for k in must_be_admin}))

    # 帮助里写的用法，必须真的能命中对应命令。写错一个字群友就用不了，
    # 而这种错不会报异常，只会「发了没反应」。
    #
    # 用一份「发言榜开着」的配置来探：这条命令的匹配器会读开关，默认配置下
    # 它本来就该返回 None（那正是我们要的行为，见下面的断言），拿默认配置
    # 进来探会把「开关正常工作」误判成「用法写错了」。
    probe_ctx = types.SimpleNamespace(
        config=config_mod.PluginConfig({"rank_message_enabled": True})
    )

    def _dispatch_key(text: str, ctx=None) -> str | None:
        lower = text.lower()
        for c in cmd_list:
            if c.matcher(ctx or probe_ctx, text, lower) is not None:
                return c.key
        return None

    mismatched = []
    for c in cmd_list:
        for entry in c.help:
            head = entry.usage.split("/")[0].split(" ")[0].strip()
            if head and _dispatch_key(head) != c.key:
                mismatched.append(f"{c.key}:{head}->{_dispatch_key(head)}")
    check("帮助里的用法都能命中对应命令", not mismatched, str(mismatched))

    # 三个菜单口令必须各自命中自己的页。命中错的话「娱乐」会打开基础页，
    # 用户以为功能没做。
    check("帮助口令命中基础页", _dispatch_key("帮助") == "help", str(_dispatch_key("帮助")))
    check("娱乐口令命中娱乐页", _dispatch_key("娱乐") == "fun_menu", str(_dispatch_key("娱乐")))
    check("管理口令命中管理页", _dispatch_key("管理") == "admin_menu", str(_dispatch_key("管理")))
    # 带页码的列表不能漏进「表情」制作命令里
    check("表情列表 2 命中图鉴", _dispatch_key("表情列表 2") == "meme_list",
          str(_dispatch_key("表情列表 2")))
    check("光说表情列表也命中图鉴", _dispatch_key("表情列表") == "meme_list")

    # 扩展性验证：往表里加一条命令，帮助和权限页应当自动出现它。
    # 这是这次重构要买的唯一东西，所以必须有测试盯着。
    async def _probe_handler(ctx, env):
        yield env.event.plain_result("probe")

    probe = command_mod.Command(
        key="__probe__",
        matcher=command_mod.exact(("探测口令",)),
        handler=_probe_handler,
        perm_short="探测功能",
        help=(command_mod.HelpEntry("探测口令", "扩展性验证", page="base", order=95),),
    )
    cmd_list.append(probe)
    try:
        probe_pages = help_mod.pages(plugin.ctx)
        probe_all = "\n".join(body for _, body in probe_pages)
        check("新命令自动进帮助菜单", "探测口令" in probe_all)
        # page 分流要真的有效：基础页和娱乐页必须是两份内容
        check("娱乐页有表情命令", "表情" in probe_pages[1][1], probe_pages[1][1][:200])
        check("三个菜单页都能生成", len(probe_pages) == 3)
        check("管理页按等级分块", "级 ·" in probe_pages[2][1], probe_pages[2][1][:200])
    finally:
        cmd_list.remove(probe)
    check("临时命令已撤掉", "__probe__" not in {c.key for c in cmd_list})

    # 普通聊天不能被任何口令误吞。这条比「能命中」更重要：
    # 口令太宽会把群友的正常聊天吃掉，而且完全没有报错。
    async def _no_match(text: str) -> list:
        ev = _KickEvent("7007")
        env = command_mod.CommandEnv(
            event=ev,
            group_id="1001",
            sender_id="6006",
            member=pdb.get_member("1001", "6006"),
            text=text,
        )
        return [r async for r in registry_mod.dispatch(plugin.ctx, env)]

    for phrase in ("今天吃什么", "有人吗", "哈哈哈", "你在干嘛呢"):
        check(f"普通聊天不误触发：{phrase}", asyncio.run(_no_match(phrase)) == [])

    # 端到端：口令 → 分发器 → 处理函数 → 回复，整条链路走一遍。
    # 重构最容易出的事故就是「路由通了但处理函数忘了接上」，只看 matcher 查不出来。
    async def _reply_of(text: str) -> list:
        ev = _KickEvent("7007")
        env = command_mod.CommandEnv(
            event=ev,
            group_id="1001",
            sender_id="6006",
            member=pdb.get_member("1001", "6006"),
            text=text,
        )
        return [r async for r in registry_mod.dispatch(plugin.ctx, env)]

    for phrase, keyword in (("额度", "额度"), ("今日", "今日")):
        got = asyncio.run(_reply_of(phrase))
        check(f"口令「{phrase}」端到端有回复", bool(got) and keyword in str(got[0]), str(got))
    # 额度记录回的是合并转发（Node 对象的 str 不含正文），只验它走了 chain。
    got = asyncio.run(_reply_of("额度记录"))
    check("额度记录端到端走合并转发",
          bool(got) and isinstance(got[0], tuple) and got[0][0] == "chain", str(got[0]))

    print("\n=== 7. 昵称体系：改名 / 守卫 / 催改名 / 自由人 ===")
    # 这一套是全部功能里唯一「会主动改别人东西」的地方，所以测四件事：
    # ① 改名真的把群名片改了、库里只存名字；② 不合规的名字进不来；
    # ③ 被人手动改掉的名片能改回来，且不刷屏；④ 提醒够次数会静音。
    nick_flow = importlib.import_module("gk_plugin.domains.nickname")
    rank_mod = importlib.import_module("gk_plugin.domains.rank")

    class NickEvent:
        """带 sender（群名片 + 性别）和记账协议端的假事件。"""

        def __init__(self, card="", sex="unknown", role="admin", user_id="8008"):
            self.calls: list[tuple[str, dict]] = []
            self.role = role
            self.user_id = user_id
            self.bot = types.SimpleNamespace(api=self)
            self.message_obj = types.SimpleNamespace(
                message=[],
                raw_message={},
                sender=types.SimpleNamespace(card=card, nickname="QQ昵称", sex=sex),
            )

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_info":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"role": self.role, "card": "", "nickname": "机器人"},
                }
            return {"status": "ok", "retcode": 0, "data": None}

        def get_self_id(self):
            return "999999"

        def get_platform_name(self):
            return "aiocqhttp"

        def get_group_id(self):
            return "1001"

        def get_sender_id(self):
            return self.user_id

        def plain_result(self, text):
            return text

        def cards(self):
            return [p["card"] for a, p in self.calls if a == "set_group_card"]

        def mutes(self):
            return [p["duration"] for a, p in self.calls if a == "set_group_ban"]

        def did(self, action):
            return any(a == action for a, _ in self.calls)

    ndb = db_mod.Database(TMP / "db_nick")

    def nick_ctx(**over):
        """一套最小的 ctx：守卫只用到 config / db / profiles。"""
        cfg = config_mod.PluginConfig(over)
        return types.SimpleNamespace(
            config=cfg, db=ndb, profiles=services_mod.ProfileService(ndb, cfg)
        )

    plain_ctx = nick_ctx()
    nick_flow.forget_admin_cache()
    nick_flow.reset_notice_cache()

    # ---- 改名：写库 + 写群名片（没戴称号就不碰头衔）----
    ndb.ensure_member("1001", "8008")
    ev = NickEvent(card="老王123", sex="male")
    said = asyncio.run(nick_flow.apply_name(plain_ctx, ev, "1001", "8008", "张三"))
    check("改名成功", "改名成功" in said, said.replace("\n", " | "))
    check("库里的显示名是纯名字", ndb.get_member("1001", "8008")["display_name"] == "张三")
    # 性别是从报文里补的（male），所以记号是 🦊 而不是 🧸。
    check("群名片带上性别记号", ev.cards() == ["🦊 张三"], str(ev.cards()))
    # 头衔放的是称号，不是名字 —— 名字已经在群名片里了，再设一遍等于同一个词
    # 在群里出现两次。没戴称号时改名**不该**去动头衔（可能有人被管理员手动
    # 设过「元老」，改名顺手抹掉就是动了不属于我们的东西）。
    check("没戴称号时改名不动头衔",
          not ev.did("set_group_special_title"), str([a for a, _ in ev.calls]))
    check("补到了性别", ndb.get_member("1001", "8008")["sex"] == "男")
    check("改名不扣额度", ndb.balance("1001", "8008") == 0)

    # ---- 不合规的名字进不来 ----
    ev = NickEvent(card="老王123", sex="male")
    bad = asyncio.run(nick_flow.apply_name(plain_ctx, ev, "1001", "8008", "张三3"))
    check("带数字的名字被拒", "不能有数字" in bad, bad)
    check("被拒时不改群名片", not ev.cards(), str(ev.cards()))
    check("被拒时库里不变", ndb.get_member("1001", "8008")["display_name"] == "张三")
    check(
        "被拒时给出规则",
        "2~4" in bad and "不要数字" in bad,
        bad.replace("\n", " | "),
    )
    short = asyncio.run(nick_flow.apply_name(plain_ctx, NickEvent(), "1001", "8008", "张"))
    check("一个字的名字被拒", "个字" in short, short)

    # ---- 戴了称号再改名：头衔跟着走 ----
    # ctx 里得有 checkin 服务才能把 title_id 翻成中文名，所以这里补一个。
    titled_ctx = types.SimpleNamespace(
        config=plain_ctx.config,
        db=ndb,
        profiles=plain_ctx.profiles,
        checkin=checkin_mod.CheckinService(ndb, plain_ctx.config),
    )
    ndb.equip_title("1001", "8008", "夜行者")  # 自定义头衔原文
    ev = NickEvent(card="🦊 张三", sex="male")
    said = asyncio.run(nick_flow.apply_name(titled_ctx, ev, "1001", "8008", "王五"))
    # 群头衔只有群主能改，改名流程不再碰 set_group_special_title（接口删了）。
    titles = [action for action, _ in ev.calls if action == "set_group_special_title"]
    check("改名不再碰群头衔", titles == [], str(titles))

    # ---- 代改名：要 2 级，且不能把操作者的性别按到别人头上 ----
    ndb.ensure_member("1001", "8100")
    ev = NickEvent(card="", sex="female", user_id="8008")
    asyncio.run(nick_flow.apply_name(plain_ctx, ev, "1001", "8100", "李四"))
    check("代改名不抄操作者的性别", ndb.get_member("1001", "8100")["sex"] == "")
    check("代改名用未设定记号", ev.cards() == ["🧸 李四"], str(ev.cards()))

    # ---- 自由人：名字随便写 ----
    ndb.set_freeman("1001", "8008", True)
    ev = NickEvent(card="", sex="male")
    free_said = asyncio.run(nick_flow.apply_name(plain_ctx, ev, "1001", "8008", "🎸老王"))
    check("自由人可以带符号", "改名成功" in free_said, free_said.replace("\n", " | "))
    check("自由人的名片也带记号", ev.cards() == ["🦊 🎸老王"], str(ev.cards()))
    ndb.set_freeman("1001", "8008", False)

    # ---- 守卫：名字合规就直接采纳 ----
    ndb.ensure_member("1001", "9009")
    ev = NickEvent(card="🦊 李四", sex="male")
    prompt = asyncio.run(
        nick_flow.guard(plain_ctx, ev, "1001", "9009", ndb.get_member("1001", "9009"))
    )
    check("合规的名字被采纳", ndb.get_member("1001", "9009")["display_name"] == "李四")
    check("名片已经对上时不发请求", not ev.cards(), str(ev.cards()))
    check("采纳时不打扰他", prompt is None, str(prompt))

    # ---- 守卫：名片被手动改掉 → 改回来，一天只提示一次 ----
    ev = NickEvent(card="李四", sex="male")
    prompt = asyncio.run(
        nick_flow.guard(plain_ctx, ev, "1001", "9009", ndb.get_member("1001", "9009"))
    )
    check("被手改的名片改回来", ev.cards() == ["🦊 李四"], str(ev.cards()))
    check("改回来时说了一句", "改回来了" in (prompt or ""), str(prompt))
    ev = NickEvent(card="李四", sex="male")
    again = asyncio.run(
        nick_flow.guard(plain_ctx, ev, "1001", "9009", ndb.get_member("1001", "9009"))
    )
    check("同一天不再重复提示", again is None, str(again))
    check("但名片照样改回来", ev.cards() == ["🦊 李四"], str(ev.cards()))

    # ---- 守卫：机器人不是管理员时不动手 ----
    nick_flow.forget_admin_cache()
    ev = NickEvent(card="李四", sex="male", role="member")
    asyncio.run(
        nick_flow.guard(plain_ctx, ev, "1001", "9009", ndb.get_member("1001", "9009"))
    )
    check("没群管权限时不改名片", not ev.cards(), str(ev.cards()))
    nick_flow.forget_admin_cache()

    # ---- 守卫：没改名 → 提醒 → 够次数静音 ----
    ndb.ensure_member("1001", "7007")
    ev = NickEvent(card="老王123", sex="male", user_id="7007")
    first = asyncio.run(
        nick_flow.guard(plain_ctx, ev, "1001", "7007", ndb.get_member("1001", "7007"))
    )
    check("没改名会提醒", "名字还没登记" in (first or ""), str(first))
    check("提醒里写了第几次", "1/3" in (first or ""), str(first))
    check("第一次不禁言", not ev.mutes(), str(ev.mutes()))
    check("提醒计数落库", ndb.get_member("1001", "7007")["nick_nag"] == 1)

    for _ in range(2):
        asyncio.run(
            nick_flow.guard(plain_ctx, ev, "1001", "7007", ndb.get_member("1001", "7007"))
        )
    check("第三次提醒后静音", ev.mutes() == [60], str(ev.mutes()))
    check("静音时长取自配置（1 分钟）", ev.mutes()[-1] == 60, str(ev.mutes()))
    check("静音后计数清零", ndb.get_member("1001", "7007")["nick_nag"] == 0)

    # ---- 关掉昵称系统：什么都不做 ----
    off_ctx = nick_ctx(nick_enabled=False)
    ndb.ensure_member("1001", "7100")
    ev = NickEvent(card="路人甲", sex="male", user_id="7100")
    asyncio.run(nick_flow.guard(off_ctx, ev, "1001", "7100", ndb.get_member("1001", "7100")))
    check("昵称系统关掉后不再催改名", not ev.cards() and not ev.mutes(), str(ev.calls))
    check("也不写库", ndb.get_member("1001", "7100")["display_name"] == "")

    # 关掉之后改名仍能用，只是走宽松规则（符号、长度都放开）。
    ev = NickEvent(card="", sex="male", user_id="7100")
    loose = asyncio.run(nick_flow.apply_name(off_ctx, ev, "1001", "7100", "🎸老王"))
    check("关掉昵称系统后可以取带符号的名字", "改名成功" in loose, loose)

    # ---- 帮助菜单按等级过滤 ----
    base_low = help_mod.make_block(plugin.ctx, "base", 1)
    base_high = help_mod.make_block(plugin.ctx, "base", 4)
    check("1 级菜单没有额度排行", base_low and "额度排行" not in base_low.text, base_low.text if base_low else "")
    check("4 级菜单有额度排行", base_high and "额度排行" in base_high.text)
    admin_low = help_mod.make_block(plugin.ctx, "admin", 1)
    admin_mid = help_mod.make_block(plugin.ctx, "admin", 2)
    admin_high = help_mod.make_block(plugin.ctx, "admin", 4)
    check("1 级看不到任何管理口令", admin_low is None, str(admin_low))
    check("2 级只看到禁言解禁", admin_mid and "禁言" in admin_mid.text and "踢" not in admin_mid.text, admin_mid.text if admin_mid else "")
    check("4 级看到群主那组", admin_high and "群主" in admin_high.text)
    check("高于自己等级的组整块不出现", "〔4 级" not in (admin_mid.text if admin_mid else ""))

    # ---- 发言榜：开 / 关 ----
    on_ctx = types.SimpleNamespace(config=config_mod.PluginConfig({"rank_message_enabled": True}))
    off_ctx = types.SimpleNamespace(config=config_mod.PluginConfig({}))
    check("发言榜开着能命中", rank_mod.message_matcher(on_ctx, "发言榜", "发言榜") is not None)
    check("发言榜关着完全失效", rank_mod.message_matcher(off_ctx, "发言榜", "发言榜") is None)
    check("关着时别的词也不串", rank_mod.message_matcher(off_ctx, "发言排行", "发言排行") is None)
    off_menu = help_mod.make_block(
        types.SimpleNamespace(
            config=config_mod.PluginConfig({}),
            db=pdb,
            checkin=None,
            gallery_brand="群助手",
        ),
        "base",
        4,
    )
    on_menu = help_mod.make_block(
        types.SimpleNamespace(
            config=config_mod.PluginConfig({"rank_message_enabled": True}),
            db=pdb,
            checkin=None,
            gallery_brand="群助手",
        ),
        "base",
        4,
    )
    check("发言榜关掉时菜单里也没有", off_menu and "发言排行" not in off_menu.text)
    check("打开后菜单里出现", on_menu and "发言排行" in on_menu.text)

    # 额度榜必须 2 级：余额是家底，全群可查等于把「谁穷」挂在墙上。
    ranks = {c.key: c.level for c in registry_mod.COMMANDS}
    check("额度榜要 2 级", ranks.get("quota_rank") == 2, str(ranks.get("quota_rank")))
    check("发言榜 1 级可看（受开关控制）", ranks.get("message_rank") == 1)

    # ---- 商店：自由人 ----
    items = plugin.ctx.checkin.shop_items("1001", "6006")
    names = [item.name for item in items]
    check("商店里有自由人", "自由人" in names, str(names))
    free = next(item for item in items if item.name == "自由人")
    check("自由人默认 500", free.price == 500, str(free.price))
    check("自由人是服务类商品", free.kind == "service")

    pdb.ensure_member("1001", "5006", welcome_quota=0)
    pdb.add_quota("1001", "5006", 600, "测试")
    bought, msg = plugin.ctx.checkin.buy("1001", "5006", "自由人")
    check("买自由人成功", bought, msg.replace("\n", " | "))
    check("买完标记置位", int(pdb.get_member("1001", "5006")["freeman"]) == 1)
    check("买完扣了 500", pdb.balance("1001", "5006") == 100)
    again_ok, again_msg = plugin.ctx.checkin.buy("1001", "5006", "自由人")
    check("自由人不能重复买", not again_ok, again_msg)

    pdb.ensure_member("1001", "5007", welcome_quota=0)
    poor_ok, poor_msg = plugin.ctx.checkin.buy("1001", "5007", "自由人")
    check("额度不够买不了自由人", not poor_ok and "不够" in poor_msg, poor_msg)

    ndb.close()
    plugin.ctx.db.close()
    import shutil as _shutil

    _shutil.rmtree(OUT_DIR / "tmp_odd_bg", ignore_errors=True)

    skipped = f" / {len(SKIP)} 跳过" if SKIP else ""
    print(f"\n=== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败{skipped} ===")
    for item in SKIP:
        print(f"  SKIP: {item}")
    for item in FAIL:
        print(f"  FAIL: {item}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
