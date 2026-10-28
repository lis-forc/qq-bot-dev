"""把管理台页面渲染成本地 HTML 并截图，用来验收版式。

为什么需要这个
--------------
``pages/console/app.js`` 只通过宿主注入的 ``window.AstrBotPluginPage`` 取数，
本地直接用浏览器打开会停在「不在 AstrBot 内」。要看到真实排版，就得
在页面里补一个假的桥，喂一批够乱的数据：

- 群号是 9-10 位长数字（真实 QQ 群号）
- 昵称有长有短，含「群主」「管理」和没称号的人
- 额度流水有正有负
- 有一个群完全没签到（验证空状态）

只用于本地看版式，不参与插件运行。产物落在 ``tools/out/console_*.png``。
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 配置页的字段表要从插件自己的 schema 读 —— 手抄一份进 mock 就失去了
# 「预览能不能反映真实页面」的意义。
sys.path.insert(0, str(BASE.parent))
from astrbot_plugin_groupkit.core import settings as settings_mod  # noqa: E402
PAGES = BASE / "pages" / "console"
OUT = BASE / "tools" / "out"
DEBUG = bool(os.environ.get("CONSOLE_DEBUG"))

GROUPS = ["922337203", "1054321987", "758231904"]

NAMES = [
    "张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十",
    "一个就很长的群昵称需要被截断", "夜猫子", "早睡失败", "摸鱼选手",
    "管理员小号", "群主本人", "潜水员", "新来的", "只会发表情", "赛博摆烂",
    "甲", "乙", "丙", "丁", "戊", "己", "庚", "辛",
]

# 头衔是自定义消耗品，佩戴位里存的就是用户写的原文 —— 没有翻译层，
# 预览直接用中文原文当 title_id，和真实数据一致。
TITLE_IDS = ["夜行者", "摸鱼冠军", "群宠"]


def config_payload(raw: dict) -> dict:
    """配置页的数据：真实分组 + 真实字段 + 真实生效值。"""
    return {
        "success": True,
        **settings_mod.describe(raw, BASE),
        "effective": settings_mod.effective_values(raw),
        "can_persist": True,
        "plugin_name": "astrbot_plugin_groupkit",
    }


def empty_mock() -> dict:
    """一条数据都没有时的样子。

    空状态最容易写崩：除零、undefined、表格变一条缝、图上留一片白。
    每次改版式都应该顺手看一眼这张。
    """
    return {
        "overview": {
            "groups": 0, "members": 0, "messages_today": 0, "messages_total": 0,
            "checkins_today": 0, "checkins_total": 0, "quota_total": 0,
            "new_today": 0, "today": "2026-10-26",
        },
        "groups": [], "trend": [], "ranking": [], "members": [], "checkins": [], "quotas": [],
        "storage": {
            "db_kb": 4, "schema_version": 3,
            "tables": {k: 0 for k in (
                "members", "daily_stats", "quota_logs", "op_logs", "checkins",
                "checkin_records", "member_skins", "shop_purchases", "bag",
            )},
        },
        "assets": {"root": "", "folders": [], "skins": [], "base_cards": [], "missing": True},
        "config": config_payload({}),
    }


def build_mock() -> dict:
    rng = random.Random(20261026)
    today = "2026-10-26"

    # ---------- 群 ----------
    groups = []
    for index, gid in enumerate(GROUPS):
        groups.append(
            {
                "group_id": gid,
                "members": [24, 41, 9][index],
                "quota": [3204, 12880, 210][index],
                "messages": [18420, 45210, 640][index],
                "new_today": [2, 5, 0][index],
                "messages_today": [456, 1288, 12][index],
                "checkins_today": [12, 27, 0][index],
            }
        )

    # ---------- 趋势 ----------
    days = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
            "2026-10-24", "2026-10-25", "2026-10-26"]
    trend = [
        {"date": d, "messages": v, "checkins": c}
        for d, v, c in zip(
            days,
            [312, 508, 421, 690, 877, 1043, 456],
            [9, 14, 11, 18, 22, 25, 12],
        )
    ]

    # ---------- 排行 ----------
    ranking = []
    for index, name in enumerate(["张三", "夜猫子", "早睡失败", "管理员小号", "摸鱼选手",
                                  "赛博摆烂", "只会发表情", "潜水员", "甲", "乙"]):
        ranking.append(
            {
                "user_id": f"20{rng.randint(10000000, 99999999)}",
                "name": name,
                "total_days": 41 - index * 3,
                "streak_days": 27 - index * 2,
                "best_streak": 33 - index,
                "title_id": TITLE_IDS[index] if index < 3 else "",
                "quota": 1200 - index * 90,
                # 发言榜读的是累计发言。和签到天数不同源，所以单独给一个数。
                "total_messages": 2860 - index * 240,
            }
        )

    # ---------- 成员 ----------
    members = []
    for index, name in enumerate(NAMES):
        members.append(
            {
                "user_id": f"20{rng.randint(10000000, 99999999)}",
                "name": name,
                "qq_name": name,
                "sex": ["男", "女", "保密"][index % 3],
                "hobby": "打游戏",
                "quota": max(0, 620 - index * 23),
                "messages": 3400 - index * 118,
                "draws": index % 4,
                "level": 4 if index == 13 else (3 if index == 12 else 0),
                "join_time": 0,
                "first_seen": "2026-08-11",
                "total_days": max(0, 32 - index * 2),
                "streak_days": max(0, 18 - index),
                "title_id": TITLE_IDS[index % 3] if index % 3 == 0 else "",
            }
        )

    # ---------- 签到 ----------
    notes = ["连续 7 天，奖励加成已经生效", "今天也来了", "夜里的群最安静",
             "再签 16 天到 30 天里程碑"]
    checkins = []
    for index in range(18):
        checkins.append(
            {
                "group_id": GROUPS[0],
                "user_id": members[index % len(members)]["user_id"],
                "name": members[index % len(members)]["name"],
                "date": today if index < 12 else "2026-10-25",
                "reward": rng.choice([8, 11, 13, 18, 24, 38]),
                "doubled": index == 2,
                "streak": max(1, 28 - index),
                "total": max(1, 44 - index),
                "created_at": f"{today} 08:{rng.randint(10, 59)}:00",
                "note": notes[index % len(notes)],
            }
        )

    # ---------- 额度流水 ----------
    reasons = ["签到", "购买双倍卡", "购买抽奖券", "改名", "管理台调整", "抽奖"]
    quotas = []
    for index in range(22):
        amount = rng.choice([8, 13, 24, -1, -4, -10, 200])
        quotas.append(
            {
                "group_id": GROUPS[0],
                "user_id": members[index % len(members)]["user_id"],
                "name": members[index % len(members)]["name"],
                "amount": amount,
                "reason": reasons[index % len(reasons)],
                "operator_id": "web" if amount == 200 else "system",
                "created_at": f"2026-10-26 {9 + index % 9:02d}:{rng.randint(10, 59)}:00",
            }
        )

    storage = {
        "db_kb": 812,
        "schema_version": 3,
        "tables": {
            "members": 74,
            "daily_stats": 1288,
            "quota_logs": 3415,
            "op_logs": 62,
            "checkins": 68,
            "checkin_records": 906,
            "bag": 19,
            "member_skins": 12,
            "shop_purchases": 41,
        },
    }

    # ---------- 图片素材 ----------
    assets = {
        "root": "/opt/astrbot/data/plugins/astrbot_plugin_groupkit/assets/images",
        "folders": [
            {
                "key": "checkin",
                "title": "签到运势图",
                "count": 3,
                "path": "/opt/astrbot/data/plugins/astrbot_plugin_groupkit/assets/images/checkin",
                "files": ["01-贪吃不胖.jpg", "02-逢考必过.jpg", "03-人见人爱.jpg"],
            },
            {
                "key": "catalog",
                "title": "表情图鉴",
                "count": 1,
                "path": "/opt/astrbot/data/plugins/astrbot_plugin_groupkit/assets/images/catalog",
                "files": ["01-表情模板目录.png"],
            },
            {
                "key": "cards",
                "title": "个人名片",
                "count": 4,
                "path": "/opt/astrbot/data/plugins/astrbot_plugin_groupkit/assets/images/cards",
                "files": ["男.png", "女.png", "樱花@50.png", "夜空@80.png"],
            },
        ],
        "base_cards": [
            {"name": "男", "filename": "男.png", "ready": True},
            {"name": "女", "filename": "女.png", "ready": True},
        ],
        "skins": [
            {"name": "樱花", "price": 50, "filename": "樱花@50.png"},
            {"name": "夜空", "price": 80, "filename": "夜空@80.png"},
        ],
    }

    return {
        "overview": {
            "groups": 3,
            "members": 74,
            "messages_today": 1756,
            "messages_total": 64270,
            "checkins_today": 39,
            "checkins_total": 1188,
            "quota_total": 16294,
            "new_today": 7,
            "today": today,
        },
        "groups": groups,
        "trend": trend,
        "ranking": ranking,
        "members": members,
        "checkins": checkins,
        "quotas": quotas,
        "storage": storage,
        "assets": assets,
        # 配置页：跑真实的 settings.describe()，字段、提示、滑杆范围、
        # 分组标题全部来自插件自己，预览不会跟真实页面漂。
        "config": config_payload({"currency_name": "额度"}),
    }


MOCK_JS = """
const MOCK = __MOCK__;
// 头衔佩戴位存的就是原文，后端不做翻译，页面直接用。
const withTitle = (rows) => rows.map((r) => ({ ...r, title_name: r.title_id || "" }));
const pick = (rows, params) => {
  const limit = Number(params?.limit || 50);
  const offset = Number(params?.offset || 0);
  return { rows: rows.slice(offset, offset + limit), total: rows.length, offset, limit };
};
window.AstrBotPluginPage = {
  async apiGet(path, params = {}) {
    const m = MOCK;
    switch (path) {
      case "overview": return { success: true, ...m.overview };
      case "groups": return { success: true, groups: m.groups };
      case "trend": return { success: true, rows: m.trend, days: 7 };
      case "ranking": return { success: true, rows: withTitle(m.ranking), by: params.by || "streak" };
      case "members": {
        const page = pick(m.members, params);
        return { success: true, ...page, rows: withTitle(page.rows) };
      }
      case "checkin-feed": return { success: true, rows: m.checkins.slice(0, params.limit || 40) };
      case "quota-feed": return { success: true, rows: m.quotas.slice(0, params.limit || 40) };
      case "storage": return { success: true, ...m.storage };
      case "assets": return { success: true, ...m.assets };
      case "config": return { success: true, ...m.config };
      default: return { success: true };
    }
  },
  async apiPost(path, body = {}) {
    if (path === "config-save") {
      const keys = Object.keys((body && body.values) || {});
      return { success: true, changed: keys, notes: [], problems: [], persisted: true };
    }
    if (path === "member-update") {
      const row = MOCK.members.find((m) => m.user_id === body.user_id);
      if (row) row.quota += Number(body.delta || 0);
      return { success: true, balance: row ? row.quota : 0, delta: body.delta };
    }
    return { success: true };
  },
};
"""


def build_page(
    mock: dict, view: str, sub: str = "overview", section: str = "", rank: str = ""
) -> str:
    html = (PAGES / "index.html").read_text(encoding="utf-8")
    css = (PAGES / "styles.css").read_text(encoding="utf-8")
    app = (PAGES / "app.js").read_text(encoding="utf-8")

    mock_js = MOCK_JS.replace("__MOCK__", json.dumps(mock, ensure_ascii=False))

    html = html.replace(
        '<link rel="stylesheet" href="./styles.css" />',
        f"<style>\n{css}\n</style>",
    )
    html = html.replace(
        '<script type="module" src="./app.js"></script>',
        f"<script>{mock_js}</script>\n"
        f"<script type=\"module\">\n{app}\n</script>\n"
        # 截图脚本：把页面切到指定标签页，否则永远是默认页（配置）。
        #
        # 不能只 setTimeout 一次就点：无头浏览器的虚拟时钟和真实时序不一致，
        # 偶尔会在事件还没绑定好的时候点空，截出来就是「内容是成员表、
        # 高亮还在群概览」的假故障。改成轮询到高亮真的切过去为止。
        #
        # 三级切法：一级 tab（配置/素材/数据）→ 数据页的二级 tab → 配置页的
        # 某个分组。每一级都靠"高亮到位"来判断点成功，不靠固定延时。
        f"<script>\n"
        f"(function () {{\n"
        f"  var tries = 0;\n"
        f"  var timer = setInterval(function () {{\n"
        f"    var tab = document.querySelector('.tab[data-view=\"{view}\"]');\n"
        f"    if (tab && !tab.classList.contains('is-active')) tab.click();\n"
        f"    if (tab && tab.classList.contains('is-active')) {{\n"
        f"      var subTab = document.querySelector('.tab[data-sub=\"{sub}\"]');\n"
        f"      if (subTab && !subTab.classList.contains('is-active')) subTab.click();\n"
        f"      var sec = document.querySelector('.rail-item[data-section=\"{section}\"]');\n"
        f"      if (sec && !sec.classList.contains('is-active')) sec.click();\n"
        f"      var seg = document.querySelector('.seg-btn[data-rank=\"{rank}\"]');\n"
        f"      if (seg && !seg.classList.contains('is-active')) seg.click();\n"
        f"    }}\n"
        f"    var done = tab && tab.classList.contains('is-active')\n"
        f"      && (!document.querySelector('.tab[data-sub=\"{sub}\"]')\n"
        f"          || document.querySelector('.tab[data-sub=\"{sub}\"]').classList.contains('is-active'))\n"
        f"      && (!document.querySelector('.rail-item[data-section=\"{section}\"]')\n"
        f"          || document.querySelector('.rail-item[data-section=\"{section}\"]').classList.contains('is-active'))\n"
        f"      && (!document.querySelector('.seg-btn[data-rank=\"{rank}\"]')\n"
        f"          || document.querySelector('.seg-btn[data-rank=\"{rank}\"]').classList.contains('is-active'));\n"
        f"    if (done || ++tries > 120) clearInterval(timer);\n"
        f"  }}, 60);\n"
        f"}})();\n</script>",
    )

    if DEBUG:
        # 版式排查用：把标签状态直接画在页面右下角，截图就能看到，
        # 不用去猜「点了没生效」还是「样式没跟上」。
        html = html.replace(
            "</body>",
            f"""<script>
setTimeout(() => {{
  const tabs = [...document.querySelectorAll('.tab')].map(t => `${{t.dataset.view}}=${{t.classList.contains('is-active') ? 'ON' : 'off'}}`);
  const views = [...document.querySelectorAll('.view')].map(v => `${{v.dataset.view}}=${{v.hidden ? 'hidden' : 'SHOWN'}}`);
  const box = document.createElement('pre');
  box.textContent = `want: {view}\\n` + tabs.join('\\n') + '\\n' + views.join('\\n')
    + `\\nbridge: ${{typeof window.AstrBotPluginPage}}`;
  box.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:999;background:#fffbe6;'
    + 'border:1px solid #d9c98a;padding:8px 10px;font:11px/1.6 monospace;margin:0;white-space:pre';
  document.body.append(box);
}}, 1400);
</script>\n</body>""",
        )
    return html


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    mock = build_mock()

    # 页面的三个一级分区 + 数据页的每个二级分区 + 配置页里两组代表性的分组
    # （基础：全是短字段；信息卡与名片：有路径和价格表，最容易排歪）
    pages = (
        ("config_basic", dict(view="config", section="basic")),
        ("config_card", dict(view="config", section="card")),
        ("config_nick", dict(view="config", section="nick")),
        ("assets", dict(view="assets")),
        ("data_overview", dict(view="data", sub="overview")),
        # 排行换了口径就换主数字，最容易把行内对齐搞歪，单独出一张
        ("data_rank_quota", dict(view="data", sub="overview", rank="quota")),
        ("data_rank_message", dict(view="data", sub="overview", rank="message")),
        ("data_members", dict(view="data", sub="members")),
        ("data_checkin", dict(view="data", sub="checkin")),
        ("data_quota", dict(view="data", sub="quota")),
        ("data_storage", dict(view="data", sub="storage")),
    )
    for name, kwargs in pages:
        page = build_page(mock, **kwargs)
        target = OUT / f"console_{name}.html"
        target.write_text(page, encoding="utf-8")
        print(f"  · {target.name}  {len(page) // 1024} KB")

    # 空库：验证「什么都没有」时页面不塌
    empty = build_page(empty_mock(), view="data", sub="overview")
    (OUT / "console_empty.html").write_text(empty, encoding="utf-8")
    print("  · console_empty.html")

    empty_assets = build_page(empty_mock(), view="assets")
    (OUT / "console_assets_empty.html").write_text(empty_assets, encoding="utf-8")
    print("  · console_assets_empty.html")
    print("HTML 已生成，用无头 Chrome 截图即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
