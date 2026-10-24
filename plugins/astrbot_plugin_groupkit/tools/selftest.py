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
import importlib
import importlib.util
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

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
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def await_perm(perm, event, user_id: str, member) -> int:
    """同步地跑一次异步权限计算。"""

    async def runner() -> int:
        return await perm.level(event, "1001", user_id, member)

    return asyncio.run(runner())


def main() -> None:
    install_stubs()
    mod = load_plugin()
    router = importlib.import_module("gk_plugin.utils.router")
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
    check("显示名 通过", router.validate_display_name("李四")[0])
    check("显示名 超长拒绝", not router.validate_display_name("一" * 13)[0])
    check("显示名 空拒绝", not router.validate_display_name("   ")[0])
    check("爱好 超长拒绝", not router.validate_hobby("爱" * 13)[0])
    check("性别 男", router.parse_sex("男") == "男")
    check("性别 女生", router.parse_sex("女生") == "女")
    check("性别 female", router.parse_sex("Female") == "女")
    check("性别 保密", router.parse_sex("保密") == "保密")
    check("性别 未知→保密", router.parse_sex("未知") == "保密")
    check("性别 看不懂", router.parse_sex("外星人") is None)
    check("性别 空", router.parse_sex("  ") is None)

    print("\n=== 2. 配置兜底 ===")
    cfg = config_mod.PluginConfig({})
    check("默认额度名", cfg.currency_name == "额度")
    check("默认抽奖区间", (cfg.draw_min, cfg.draw_max) == (1, 5))
    check(
        "上限低于下限自动纠正",
        config_mod.PluginConfig({"draw_min": 9, "draw_max": 3}).draw_max == 9,
    )
    check("触发词默认", cfg.info_triggers == {"查", "我的信息", "名片"})
    check(
        "触发词自定义",
        config_mod.PluginConfig({"info_triggers": "I，名片"}).info_triggers == {"i", "名片"},
    )
    # 老版本默认值是 i,我的信息,名片。服务器上那份配置没人会去手动改，
    # 所以必须自动跟着新默认走，否则「代码改成查了，群里还得发 i」。
    check(
        "老默认触发词自动迁移到查",
        config_mod.PluginConfig({"info_triggers": "i,我的信息,名片"}).info_triggers
        == {"查", "我的信息", "名片"},
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
    check("结构版本已写入", db.schema_version == 1, str(db.schema_version))

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
            out = call()
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
    for i in range(7):
        db.add_quota(gid, "hist1", i + 1, f"第{i}笔", "hist1")
    hist_text = hist_quota.render_history(gid, "hist1")
    hist_rows = [line for line in hist_text.splitlines() if line[:1] in ("🟢", "🔴")]
    check("默认只显示 5 笔", len(hist_rows) == 5, f"{len(hist_rows)} 行")
    check("最新一笔在最上面", "+7" in hist_rows[0], hist_rows[0])
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

    renamed = profiles.set_display_name(gid2, uid2, "王小五", charge=True)
    check("改名扣额度", "消耗额度：10" in renamed, renamed.replace("\n", " | "))
    check("改名后显示名生效", profiles.display_name(pdb.get_member(gid2, uid2)) == "王小五")

    poor_gid, poor_uid = "1001", "5005"
    pdb.ensure_member(poor_gid, poor_uid, welcome_quota=0)
    broke = profiles.set_display_name(poor_gid, poor_uid, "穷鬼", charge=True)
    check("额度不足拒绝改名", "额度不足" in broke, broke.replace("\n", " | "))

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
    print(f"\n--- 帮助（{len(pages)} 页） ---")
    for title, body in pages:
        print(f"\n[{title}]\n{body}")
    check("帮助 3 页", len(pages) == 3)
    check("帮助里没有立名", all("立名" not in body for _, body in pages))
    check("帮助说明授权不看身份", "不看身份" in pages[2][1])
    # 「额度 / 抽奖 / 今日」必须是独立条目，不能缩进挂在「爱好」的说明下面
    check(
        "帮助里额度是独立条目",
        "\n额度 / 抽奖 / 今日\n" in pages[0][1],
        pages[0][1].replace("\n", " | "),
    )
    check("帮助里有额度记录", "\n额度记录\n" in pages[0][1])
    check("帮助里说明拉黑", "拉黑" in pages[1][1], pages[1][1].replace("\n", " | "))

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
        """记录所有 call_action，用来断言参数真的传到了协议端。"""

        def __init__(self, role: str = "admin"):
            self.role = role
            self.calls: list[tuple[str, dict]] = []

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_info":
                return {"data": {"user_id": str(params.get("user_id")), "role": self.role}}
            return None

    class _KickEvent:
        def __init__(self, target: str | None = None, role: str = "admin"):
            self.bot = types.SimpleNamespace(api=_KickApi(role))
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

    print("\n=== 4.10 禁言：不写时长 / @全体成员 ===")
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

    # @全体成员 走的是 set_group_whole_ban，不是对某个人 set_group_ban。
    # 这两者搞混会「以为只禁了一个人，其实全群被封」。
    whole_replies, whole_calls = asyncio.run(_say("禁言 @全体成员", "all", role="owner"))
    whole = [p for a, p in whole_calls if a == "set_group_whole_ban"]
    single = [p for a, p in whole_calls if a == "set_group_ban"]
    check("@全体成员走全群禁言", bool(whole) and whole[0].get("enable") is True, str(whole_calls))
    check("@全体成员不会误禁单人", not single, str(whole_calls))
    check("全员禁言有明确提示", any("全群禁言" in str(r) for r in whole_replies), str(whole_replies))

    # 管理员只有 3 级，全员禁言要 4 级 —— 必须拦住。
    denied, denied_calls = asyncio.run(_say("禁言 @全体成员", "all", role="admin"))
    check(
        "3 级不能全员禁言",
        not [p for a, p in denied_calls if a == "set_group_whole_ban"],
        str(denied_calls),
    )
    check("拒绝时说明要群主权限", any("群主" in str(r) for r in denied), str(denied))

    # 有 @ 目标时，参数里出现「全体」绝不能被当成全员禁言。
    # 判错的话操作者以为只禁了一个人，实际上整个群被封了。
    _, tricky_calls = asyncio.run(_say("禁言 @7007 全体", "7007", role="owner"))
    check(
        "带 @目标时「全体」不触发全群禁言",
        not [p for a, p in tricky_calls if a == "set_group_whole_ban"],
        str(tricky_calls),
    )

    # 「禁言 全体」不 @ 人才是全员禁言
    _, bare_calls = asyncio.run(_say("禁言 全体", None, role="owner"))
    check(
        "只写全体=全员禁言",
        bool([p for a, p in bare_calls if a == "set_group_whole_ban"]),
        str(bare_calls),
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

    print("\n=== 5. 信息卡渲染 ===")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = profiles.card_data(gid2, uid2, pdb.get_member(gid2, uid2), pdb.counts(gid2, uid2))
    check("卡片用真实天数", data["days"] == 30, str(data["days"]))
    path = plugin.ctx.cards.render(data)
    check("渲染成功", path is not None and Path(path).exists())
    if path:
        target = OUT_DIR / "card_preview.png"
        target.write_bytes(Path(path).read_bytes())
        print(f"  预览已保存：{target}")

    # 没装 Pillow 时不能抛异常：必须返回 None，让上层退化成文字卡
    original_pil = card_mod.PIL_AVAILABLE
    try:
        card_mod.PIL_AVAILABLE = False
        check("无 Pillow 时退回文字卡", plugin.ctx.cards.render(dict(data)) is None)
    finally:
        card_mod.PIL_AVAILABLE = original_pil

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
    check("极端数据不崩", weird_path is not None and Path(weird_path).exists())

    # 背景图尺寸不一致时应自动缩放（会走 RESAMPLE 分支，顺带验证 Pillow 兼容性）
    from PIL import Image as PILImage

    small_bg = TMP / "small_bg.png"
    PILImage.new("RGB", (600, 340), (255, 230, 240)).save(small_bg)
    odd_renderer = card_mod.CardRenderer(
        ROOT, str(small_bg), "assets/fonts/LXGWWenKai-Medium.ttf", OUT_DIR / "tmp_odd_bg"
    )
    odd_path = odd_renderer.render(dict(data))
    check(
        "背景尺寸不符自动缩放",
        odd_path is not None and PILImage.open(odd_path).size == (1200, 680),
        str(PILImage.open(odd_path).size) if odd_path else "渲染失败",
    )

    # 头像三种情况：正常、没有（首字占位）、拿到坏数据（解码失败也不能崩）
    import io as _io

    avatar_buf = _io.BytesIO()
    PILImage.new("RGB", (640, 640), (200, 180, 240)).save(avatar_buf, "PNG")
    avatar_bytes = avatar_buf.getvalue()

    with_avatar = plugin.ctx.cards.render(dict(data), avatar_bytes)
    check("带头像渲染成功", with_avatar is not None and Path(with_avatar).exists())
    without_avatar = plugin.ctx.cards.render(dict(data), None)
    check("无头像走首字占位", without_avatar is not None and Path(without_avatar).exists())
    broken_avatar = plugin.ctx.cards.render(dict(data), b"not-an-image")
    check("头像数据损坏不崩", broken_avatar is not None and Path(broken_avatar).exists())
    check("关闭头像开关时跳过", config_mod.PluginConfig({"card_show_avatar": False}).card_show_avatar is False)
    check("默认开启头像", config_mod.PluginConfig({}).card_show_avatar is True)
    check("默认新人礼 10", config_mod.PluginConfig({}).welcome_quota == 10, str(config_mod.PluginConfig({}).welcome_quota))
    check("默认改名 1", config_mod.PluginConfig({}).rename_cost == 1)
    check("默认禁言 10 分钟", config_mod.PluginConfig({}).default_mute_minutes == 10)
    check(
        "禁言时长可配且收敛",
        config_mod.PluginConfig({"default_mute_minutes": 0}).default_mute_minutes == 1
        and config_mod.PluginConfig({"default_mute_minutes": 999999}).default_mute_minutes == 43200,
    )

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
    def _dispatch_key(text: str) -> str | None:
        lower = text.lower()
        for c in cmd_list:
            if c.matcher(plugin.ctx, text, lower) is not None:
                return c.key
        return None

    mismatched = []
    for c in cmd_list:
        for entry in c.help:
            head = entry.usage.split("/")[0].split(" ")[0].strip()
            if head and _dispatch_key(head) != c.key:
                mismatched.append(f"{c.key}:{head}->{_dispatch_key(head)}")
    check("帮助里的用法都能命中对应命令", not mismatched, str(mismatched))

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
        check("新命令自动进权限页", "探测功能" in probe_pages[2][1])
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

    for phrase, keyword in (("额度", "额度"), ("今日", "今日"), ("额度记录", "额度")):
        got = asyncio.run(_reply_of(phrase))
        check(f"口令「{phrase}」端到端有回复", bool(got) and keyword in str(got[0]), str(got))

    plugin.ctx.db.close()

    print(f"\n=== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 ===")
    for item in FAIL:
        print(f"  FAIL: {item}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
