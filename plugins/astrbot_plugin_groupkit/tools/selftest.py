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

    print("\n=== 2. 配置兜底 ===")
    cfg = config_mod.PluginConfig({})
    check("默认额度名", cfg.currency_name == "额度")
    check("默认抽奖区间", (cfg.draw_min, cfg.draw_max) == (1, 5))
    check(
        "上限低于下限自动纠正",
        config_mod.PluginConfig({"draw_min": 9, "draw_max": 3}).draw_max == 9,
    )
    check("触发词默认", cfg.info_triggers == {"i", "我的信息", "名片"})
    check(
        "触发词自定义",
        config_mod.PluginConfig({"info_triggers": "I，名片"}).info_triggers == {"i", "名片"},
    )
    check("群白名单留空=全开", cfg.is_group_enabled("123"))
    check(
        "群白名单生效",
        not config_mod.PluginConfig({"enabled_group_ids": ["1"]}).is_group_enabled("2"),
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

    print("\n=== 4. 真实入群天数 / 显示名 / 文案 ===")
    plugin = mod.GroupKit(
        context=None,
        config={
            "welcome_quota": 20,
            "rename_cost": 10,
            "draw_per": 3,
            "info_triggers": "i",
            "enabled_group_ids": ["1001"],
        },
    )
    profiles = plugin.profiles
    pdb = plugin.db  # 第 4/5 节统一用插件自己的库，别和上面的临时 db 混用

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

    perm = plugin.permission
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
    draw_text = plugin.quota.draw("1001", "6006")
    print(draw_text)
    check("抽奖有结果", "获得额度" in draw_text, draw_text.replace("\n", " | "))

    print("\n--- 额度 ---")
    quota_text = plugin.quota.render("1001", "6006", pdb.get_member("1001", "6006"))
    print(quota_text)
    check("额度页有进度", "抽奖进度" in quota_text)

    print("\n--- 统计 ---")
    stats_text = plugin.stats.render("1001", "6006", pdb.get_member("1001", "6006"))
    print(stats_text)
    check("统计页有今日", "今日：3 条" in stats_text, stats_text.replace("\n", " | "))

    pages = plugin._help_pages()
    print(f"\n--- 帮助（{len(pages)} 页） ---")
    for title, body in pages:
        print(f"\n[{title}]\n{body}")
    check("帮助 3 页", len(pages) == 3)
    check("帮助里没有立名", all("立名" not in body for _, body in pages))
    check("帮助说明授权不看身份", "不看身份" in pages[2][1])

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

    print("\n=== 5. 信息卡渲染 ===")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = profiles.card_data(gid2, uid2, pdb.get_member(gid2, uid2), pdb.counts(gid2, uid2))
    check("卡片用真实天数", data["days"] == 30, str(data["days"]))
    path = plugin.cards.render(data)
    check("渲染成功", path is not None and Path(path).exists())
    if path:
        target = OUT_DIR / "card_preview.png"
        target.write_bytes(Path(path).read_bytes())
        print(f"  预览已保存：{target}")

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
    weird_path = plugin.cards.render(weird)
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

    with_avatar = plugin.cards.render(dict(data), avatar_bytes)
    check("带头像渲染成功", with_avatar is not None and Path(with_avatar).exists())
    without_avatar = plugin.cards.render(dict(data), None)
    check("无头像走首字占位", without_avatar is not None and Path(without_avatar).exists())
    broken_avatar = plugin.cards.render(dict(data), b"not-an-image")
    check("头像数据损坏不崩", broken_avatar is not None and Path(broken_avatar).exists())
    check("关闭头像开关时跳过", config_mod.PluginConfig({"card_show_avatar": False}).card_show_avatar is False)
    check("默认开启头像", config_mod.PluginConfig({}).card_show_avatar is True)
    check("默认新人礼 5", config_mod.PluginConfig({}).welcome_quota == 5)
    check("默认改名 1", config_mod.PluginConfig({}).rename_cost == 1)

    plugin.db.close()

    print(f"\n=== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 ===")
    for item in FAIL:
        print(f"  FAIL: {item}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
