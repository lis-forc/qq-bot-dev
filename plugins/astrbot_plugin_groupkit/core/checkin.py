"""签到与商店：规则 + 数据组装。

这一层不做 IO、不发消息、不碰 HTML。它只回答三个问题：
1. 这个人今天能拿多少额度（`check_in`）；
2. 卡片上要写什么字（`card_payload`）；
3. 商店里有什么、这一下买不买得成（`shop_items` / `buy`）。

为什么奖励要用「确定性随机」
----------------------------
get_px 那套里最值得抄的一点：奖励不是 `random.randint()` 直接取，
而是用 `sha256("checkin|日期|群号|QQ号")` 当种子生成。同一天同一个人
重算一百次都是同一个数。带来的直接好处是：

- 重发当天卡片不需要缓存奖励值，重算即可，数字不会变；
- 机器人重启、配置改了一半、代码热重载，都不会让已发出的卡片对不上；
- 想查「为什么他拿 13 我拿 8」时，能当场复现。

代价是「改基础区间配置」只影响明天起的新签到 —— 这正是我们想要的。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .config import PluginConfig
from .database import Database
from .database import _today as today_key

# ---------------------------------------------------------------- 关系目录
# 关系是挂在**两个人**身上的商品：买来的只是「结缔资格」（放进背包），
# 用「关系 挚友 @某人」发起，对方同意才算数。价格是节奏设计而非运营参数，
# 写死在代码里：520 这种数字本身就是商品说明的一部分。
RELATIONS: tuple[tuple[str, int, str], ...] = (
    ("挚友", 200, "见过面聊过天的，才算朋友"),
    ("铁子", 200, "怎么闹都闹不散的那种"),
    ("搭子", 300, "饭搭子游戏搭子，干啥都有伴"),
    ("情侣", 520, "这个数字不用解释了吧"),
)
RELATION_NAMES = tuple(name for name, _, _ in RELATIONS)

# ---------------------------------------------------------------- 里程碑 / 寄语
# 累计签到达到这些天数时额外奖励一次。做成常量而不是配置项，
# 因为它是「节奏设计」而非运营参数：写进帮助里，群友才知道自己在追什么。
MILESTONES: dict[int, int] = {7: 20, 30: 100, 100: 300, 365: 1000}

NOTES: tuple[str, ...] = (
    "明天也要来哦",
    "今天也有好好见面",
    "脚步很轻，但确实在靠近",
    "把今天记下来了",
    "这里一直留着一个位置",
    "日子被认真地数着",
)

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _rng(*parts: str) -> random.Random:
    """由字符串决定种子的随机源。同输入必同输出，跨进程跨重启都一样。"""
    text = "|".join(parts).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(text).digest()[:8], "big"))


def base_reward(
    group_id: str, user_id: str, date_key: str, low: int, high: int
) -> int:
    """当天的基础额度。带上群号，所以同一个人在两个群的数字不一样。"""
    return _rng("checkin-base", date_key, str(group_id), str(user_id)).randint(low, high)


def streak_bonus(streak_days: int, per_day: int, cap: int) -> int:
    """连签加成：从第二天起每天 +per_day，封顶 cap 天。

    用「每多一天 +n」而不是「达到某档给一笔」，是因为前者每一天都有反馈，
    后者在档位之间会有好几天毫无变化，群友会以为坏了。
    """
    return max(0, min(max(0, streak_days - 1), cap)) * per_day


def milestone_bonus(total_days: int, milestones: dict[int, int] | None = None) -> int:
    """累计天数正好踩在里程碑上时给一笔。踩过之后不给，靠日期相等判断。"""
    return (milestones or MILESTONES).get(int(total_days), 0)


def note_for(group_id: str, user_id: str, date_key: str, streak_days: int) -> str:
    """一句固定寄语。连签满 7 的倍数时说明加成生效，其余按种子挑一句。"""
    if streak_days and streak_days % 7 == 0:
        return f"连续 {streak_days} 天，奖励加成已经生效"
    index = _rng("checkin-note", date_key, str(group_id), str(user_id)).randrange(
        len(NOTES)
    )
    return NOTES[index]


def next_milestone(total_days: int, milestones: dict[int, int] | None = None) -> tuple[int, int] | None:
    """下一个还没拿到的累计里程碑 → (天数, 奖励)；全拿到了返回 None。"""
    for days in sorted((milestones or MILESTONES)):
        if int(total_days) < days:
            return days, (milestones or MILESTONES)[days]
    return None


# ---------------------------------------------------------------- 商店目录
@dataclass(frozen=True)
class ShopItem:
    item_id: str
    name: str
    price: int
    desc: str
    kind: str        # title | double | ticket | skin
    limit: int = 0   # 每日限购，0 = 不限
    require_days: int = 0
    owned: bool = False
    locked: bool = False  # 门槛没到
    wearing: bool = False  # 皮肤专用：这件是不是正戴着

    @property
    def price_label(self) -> str:
        return "免费" if self.price <= 0 else str(self.price)

    @property
    def limit_label(self) -> str:
        if self.limit <= 0:
            return "不限购"
        return f"每日 {self.limit} 件"


class CheckinService:
    """签到结算 + 卡片数据 + 商店。"""

    def __init__(self, db: Database, config: PluginConfig, images: Any = None):
        self.db = db
        self.config = config
        # 名片皮肤来自磁盘上的图片文件（见 utils/assets.py）。
        # 允许为 None：自测里不建素材目录，商店就只剩称号和消耗品。
        self.images = images

    @property
    def ticket_price(self) -> int:
        """抽奖券卖多少，按抽奖本身的期望值算，不另设一个配置项。

        抽一次的平均收益是 `(最低+最高)/2 + 暴击概率×暴击额度`。
        券卖得比这个高 → 没人买；卖得比这个低 → 拿券套利，等于无限印钱。
        所以直接取期望值，四舍五入到整数。管理员改了抽奖区间，
        券价自动跟着动，不会出现「把 draw_max 调到 50 之后券价还是 4」的漏洞。
        """
        cfg = self.config
        expected = (cfg.draw_min + cfg.draw_max) / 2 + (
            cfg.draw_bonus_chance * cfg.draw_bonus_amount
        )
        return max(1, int(round(expected)))

    # ---------------------------------------------------------- 奖励规则
    def _reward_fn(self, group_id: str, user_id: str, date_key: str):
        cfg = self.config

        def compute(streak_after: int, total_after: int) -> tuple[int, int, int]:
            return (
                base_reward(
                    group_id, user_id, date_key, cfg.checkin_base_min, cfg.checkin_base_max
                ),
                streak_bonus(
                    streak_after, cfg.checkin_streak_bonus, cfg.checkin_streak_cap
                ),
                milestone_bonus(total_after),
            )

        return compute

    # ---------------------------------------------------------- 签到
    def check_in(self, group_id: str, user_id: str) -> dict[str, Any]:
        """签一次。重复签返回当天快照，`already=True`。"""
        date_key = today_key()
        return self.db.perform_checkin(
            group_id,
            user_id,
            date_key,
            self._reward_fn(group_id, user_id, date_key),
            note=note_for(group_id, user_id, date_key, 0),
        )

    def state(self, group_id: str, user_id: str) -> dict[str, Any]:
        """当前签到状态，没签过也给一份零值，调用方不用到处判 None。"""
        row = self.db.get_checkin(group_id, user_id) or {}
        return {
            "total_days": int(row.get("total_days") or 0),
            "streak_days": int(row.get("streak_days") or 0),
            "best_streak": int(row.get("best_streak") or 0),
            "last_date": str(row.get("last_date") or ""),
            "title_id": str(row.get("title_id") or ""),
            "double_pending": int(row.get("double_pending") or 0),
            "today_done": str(row.get("last_date") or "") == today_key(),
        }

    def today_record(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        return self.db.today_record(group_id, user_id)

    def recent(self, group_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.db.recent_records(group_id, user_id, limit)

    def ranking(self, group_id: str, by: str = "streak", limit: int = 10) -> list[dict[str, Any]]:
        return self.db.checkin_ranking(group_id, by, limit)

    # ---------------------------------------------------------- 头衔
    @staticmethod
    def title_name(title_id: str) -> str:
        """佩戴位 → 展示文字。

        头衔是自定义消耗品：`头衔 想要的字` 把**原文**写进佩戴位
        （title_id 列），所以展示就是原样返回 —— 没有目录、没有翻译层。
        """
        return title_id or ""

    # ---------------------------------------------------------- 商店
    def shop_items(self, group_id: str, user_id: str) -> list[ShopItem]:
        """当前在售清单。顺序即展示顺序，也是「购买 双倍卡」时的匹配顺序。

        商店卖的东西必须满足一条：**不能是「额度买额度」**。
        所以双倍卡和抽奖券都带每日限购 —— 「今天的额度翻倍」本质是把明天的
        钱提前花，不限购就等于无限套利；限一件，它就退化成一张优惠券。
        """
        cfg = self.config
        state = self.state(group_id, user_id)
        total_days = state["total_days"]
        member = self.db.get_member(group_id, user_id) or {}
        freeman = bool(int(member.get("freeman") or 0))

        items: list[ShopItem] = [
            # 服务放在第一位：它是货架上唯一「买完改变身份」的东西，
            # 也是唯一能让人长期惦记的目标（新人的第一笔大额消费）。
            ShopItem(
                item_id="freeman",
                name="自由人",
                price=cfg.freeman_price,
                desc="改名不再受字数与符号限制，机器人也不再管你的群名片",
                kind="service",
                owned=freeman,
            ),
            ShopItem(
                item_id="double",
                name="双倍卡",
                price=cfg.checkin_double_price,
                desc="下一次签到额度翻倍，立刻生效",
                kind="double",
                limit=cfg.checkin_double_limit,
            ),
            ShopItem(
                item_id="ticket",
                name="抽奖券",
                price=self.ticket_price,
                desc="换一次抽奖机会",
                kind="ticket",
                limit=cfg.checkin_ticket_limit,
            ),
        ]
        # 头衔卖的是「一次修改权」：不指定内容，买的人想改什么自己定，
        # `头衔 想要的字` 消耗一个并写上。所以货架上只有一件，可囤。
        items.append(
            ShopItem(
                item_id="title",
                name="头衔",
                price=cfg.title_price,
                desc="自定义群头衔的一次修改机会，想改什么自己定",
                kind="title",
            )
        )
        # 权限卡：用额度买一次「临时权力」。
        #
        # 禁言卡只给 10 秒，是故意的 —— 它卖的是「玩一下」，不是「管人」。
        # 真要管人有「禁言」口令，那看的是等级，不是钱。两条路分开，
        # 钱就永远买不到真正的管理权。
        items.extend(
            [
                ShopItem(
                    item_id="mute",
                    name="禁言卡",
                    price=cfg.mute_card_price,
                    desc="禁别人 10 秒，一次一张",
                    kind="permit",
                ),
                ShopItem(
                    item_id="rename",
                    name="改名卡",
                    price=cfg.rename_card_price,
                    desc="改一次名字的资格，一次一张",
                    kind="permit",
                ),
            ]
        )
        items.extend(
            ShopItem(
                item_id=name,
                name=name,
                price=price,
                desc=desc,
                kind="relation",
            )
            for name, price, desc in RELATIONS
        )
        items.extend(self._skin_items(group_id, user_id))
        return items

    def skin_items(self, group_id: str, user_id: str) -> list[ShopItem]:
        """名片皮肤（公开给商店和口令用，避免外部拼 item_id）。"""
        return self._skin_items(group_id, user_id)

    def _skin_items(self, group_id: str, user_id: str) -> list[ShopItem]:
        """把 `assets/images/cards/` 里的图变成商品。

        名字 = 文件名（去掉扩展名），价格 = 文件名里的 `@价格`
        或管理台配的那张价格表，两者都没有就用默认价。
        所以「上架一张新名片」的完整操作是：往文件夹里丢一张图。
        """
        if self.images is None:
            return []
        try:
            skins = self.images.skins()
        except Exception:
            return []
        if not skins:
            return []
        owned = self.db.owned_skins(group_id, user_id)
        wearing = self.current_skin(group_id, user_id)
        return [
            ShopItem(
                item_id=f"skin:{skin.name}",
                name=skin.name,
                price=skin.price,
                desc=f"信息卡底图 · {skin.filename}",
                kind="skin",
                owned=skin.name in owned,
                wearing=skin.name == wearing,
            )
            for skin in skins
        ]

    def current_skin(self, group_id: str, user_id: str) -> str:
        """现在戴着的名片名。没戴或读不到都返回空串。"""
        try:
            member = self.db.get_member(group_id, user_id) or {}
        except Exception:
            return ""
        return str(member.get("card_skin") or "")

    def wear_skin(self, group_id: str, user_id: str, keyword: str) -> tuple[bool, str, str]:
        """换名片。返回 (成功, 皮肤名, 失败原因)。

        只允许戴自己买过的；`keyword` 为空表示还原默认。
        """
        word = keyword.strip()
        if not word or word in {"默认", "无", "原版", "摘掉"}:
            self.db.equip_skin(group_id, user_id, "")
            return True, "默认", ""

        skin = self._match_skin(keyword)
        if skin is None:
            return False, "", "找不到这张名片"
        if skin.name not in self.db.owned_skins(group_id, user_id):
            return False, skin.name, "还没买"
        self.db.equip_skin(group_id, user_id, skin.name)
        return True, skin.name, ""

    def _match_skin(self, keyword: str):
        """先精确，再唯一包含。名片名字是中文，群友经常懒得打全。"""
        if self.images is None:
            return None
        try:
            return self.images.skin(keyword)
        except Exception:
            return None


    def buy(self, group_id: str, user_id: str, keyword: str) -> tuple[bool, str]:
        """购买。返回 (成功, 要说的话)。所有扣费都在 DB 层的事务里完成。"""
        word = keyword.strip()
        if not word:
            return False, "买什么？发「商店」看看有什么"

        item = self._match_item(group_id, user_id, word)
        if item is None:
            return False, f"没有「{word}」这件东西\n发「商店」看看有什么"

        if item.owned:
            return False, f"你已经拥有「{item.name}」了"
        if item.locked:
            return False, (
                f"「{item.name}」要先累计签到 {item.require_days} 天才能买\n"
                f"你当前累计 {self.state(group_id, user_id)['total_days']} 天"
            )

        date_key = today_key()
        cur = self.config.currency_name

        if item.kind == "title":
            # 买的是「一次修改机会」，不绑定内容 —— 买了想改什么自己定。
            # 买完不自动生效：得发「头衔 想要的字」才消耗并写上。
            ok, reason, balance = self.db.bag_buy(
                group_id, user_id, "title", "title", item.price, "购买头衔"
            )
            if not ok:
                return False, self._fail_text(reason, item, balance, cur)
            n = self.db.bag_count(group_id, user_id, "title", "title")
            return True, (
                f"✅ 买到一次改头衔的机会，已放进背包（×{n}）\n"
                f"发「头衔 想要的字」就能改，只限这一次\n"
                f"花费 {item.price} {cur} · 余额 {balance}"
            )

        if item.kind == "relation":
            # 关系买的也只是「结缔资格」，要等对方同意才真正挂上。
            ok, reason, balance = self.db.bag_buy(
                group_id, user_id, "relation", item.item_id, item.price,
                f"购买关系·{item.name}",
            )
            if not ok:
                return False, self._fail_text(reason, item, balance, cur)
            n = self.db.bag_count(group_id, user_id, "relation", item.item_id)
            return True, (
                f"✅ 买到「{item.name}」结缔资格，已放进背包（×{n}）\n"
                f"{item.desc}\n"
                f"发「关系 {item.name} @对方」结缔 · 花费 {item.price} {cur} · 余额 {balance}"
            )

        if item.kind == "permit":
            # 权限卡和头衔一样是消耗品：买进背包，用的时候扣一张。
            # 不进背包直接生效的话，「我有几张禁言卡」就说不清了。
            ok, reason, balance = self.db.bag_buy(
                group_id, user_id, "permit", item.item_id, item.price,
                f"购买{item.name}",
            )
            if not ok:
                return False, self._fail_text(reason, item, balance, cur)
            n = self.db.bag_count(group_id, user_id, "permit", item.item_id)
            return True, (
                f"✅ {item.name} 已进背包（×{n}）\n"
                f"花费 {item.price} {cur} · 余额 {balance}"
            )

        if item.kind == "double":
            ok, reason, balance = self.db.buy_double(
                group_id, user_id, item.price, date_key, item.limit
            )
            if not ok:
                if reason == "pending":
                    return False, "你手上还有一张没用掉的双倍卡\n下次签到就会生效"
                return False, self._fail_text(reason, item, balance, cur)
            return True, (
                f"✅ 买到「{item.name}」\n"
                f"下一次签到额度翻倍 · 只生效一次\n"
                f"花费 {item.price} {cur} · 余额 {balance}"
            )

        if item.kind == "service":
            ok, reason, balance = self.db.buy_freeman(
                group_id, user_id, item.price
            )
            if not ok:
                return False, self._fail_text(reason, item, balance, cur)
            return True, (
                f"✅ 买到「{item.name}」\n"
                f"以后改名想写多长、想带符号都行，机器人不再管你的群名片。\n"
                f"花费 {item.price} {cur} · 余额 {balance}"
            )

        if item.kind == "skin":
            ok, reason, balance = self.db.buy_skin(
                group_id, user_id, item.name, item.price
            )
            if not ok:
                return False, self._fail_text(reason, item, balance, cur)
            # 和称号一个道理：买了不戴等于没买。
            self.db.equip_skin(group_id, user_id, item.name)
            return True, (
                f"✅ 买到名片「{item.name}」并已换上\n"
                f"花费 {item.price} {cur} · 余额 {balance}\n"
                f"发「查」看看新名片"
            )

        ok, reason, balance, draws = self.db.buy_ticket(
            group_id, user_id, item.price, date_key, item.limit
        )
        if not ok:
            return False, self._fail_text(reason, item, balance, cur)
        return True, (
            f"✅ 买到「{item.name}」\n"
            f"抽奖次数 +1，现有 {draws} 次\n"
            f"花费 {item.price} {cur} · 余额 {balance}\n"
            f"发「抽奖」用掉它"
        )

    def note_for(self, item: ShopItem, group_id: str, user_id: str) -> str:
        """商品后面那行小字（已拥有 / 需累计 N 天 / 今日已买）。

        对外的薄包装：`_item_note` 需要 db，而调用方（商店文案）只该拿到文字。
        """
        return _item_note(item, self.db, group_id, user_id)

    def _match_item(self, group_id: str, user_id: str, word: str) -> ShopItem | None:
        """先整名匹配（双倍卡 / 夜行者），再别名，最后包含匹配。"""
        items = self.shop_items(group_id, user_id)
        for item in items:
            if item.name == word:
                return item
        aliases = {
            "双倍": "double",
            "翻倍": "double",
            "双倍卡": "double",
            "抽奖券": "ticket",
            "抽奖": "ticket",
            "券": "ticket",
            "头衔": "title",
            "改头衔": "title",
        }
        alias_id = aliases.get(word)
        if alias_id:
            for item in items:
                if item.item_id == alias_id:
                    return item
        for item in items:
            if word and word in item.name:
                return item
        return None

    def _fail_text(self, reason: str, item: ShopItem, balance: int, currency: str) -> str:
        if reason == "limit":
            return f"「{item.name}」今天买够了\n{item.limit_label}，明天再来"
        if reason == "poor":
            return (
                f"⚠️ {currency}不够\n"
                f"「{item.name}」要 {item.price}，你有 {balance}\n"
                f"发「签到」每天都能领"
            )
        if reason == "already":
            return f"你已经拥有「{item.name}」了"
        return f"没买成「{item.name}」，稍后再试"

    # ---------------------------------------------------------- 卡片数据
    def card_payload(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        record: dict[str, Any],
        avatar: str = "",
    ) -> dict[str, Any]:
        """签到卡上要显示的每一个字段。全部转成字符串/数字，交给模板排版。

        卡片永远读「快照」，不读实时状态 —— 这样重发当天的卡片，
        哪怕期间又改了配置、花掉了额度，图上的数字也和第一次一模一样。
        """
        total_after = int(record.get("total_after") or 0)
        streak_after = int(record.get("streak_after") or 0)
        reward = int(record.get("reward") or 0)
        doubled = bool(record.get("doubled"))
        state = self.state(group_id, user_id)
        title_name = self.title_name(state["title_id"])

        upcoming = next_milestone(total_after)
        if upcoming is None:
            progress = 100
            milestone_text = "累计里程碑已全部达成"
            progress_label = "全部达成"
        else:
            days, bonus = upcoming
            # 进度按「上一个里程碑到下一个里程碑」之间的位置算，
            # 而不是「0 到下一个」—— 后者会让人永远卡在 20% 上。
            lower = max([d for d in MILESTONES if d <= total_after], default=0)
            span = max(1, days - lower)
            progress = int(max(0, min(100, (total_after - lower) / span * 100)))
            milestone_text = f"再签 {days - total_after} 天 · 额外 +{bonus}"
            progress_label = f"{total_after} / {days}"

        reward_parts = [
            int(record.get("base_quota") or 0),
            int(record.get("streak_bonus") or 0),
            int(record.get("milestone_bonus") or 0),
        ]
        bonus_parts = [part for part in reward_parts[1:] if part > 0]

        return {
            "date_label": _date_label(str(record.get("date_key") or today_key())),
            "title": _card_title(total_after, streak_after, bool(record.get("milestone_bonus"))),
            "badge": f"连签 {streak_after} 天" if streak_after >= 3 and streak_after % 7 == 0 else "",
            "username": nickname,
            "avatar": avatar,
            "avatar_initial": (nickname or "?")[:1],
            "user_title": title_name,
            "reward": reward,
            "reward_base": reward_parts[0],
            "reward_bonus": sum(bonus_parts),
            "doubled": doubled,
            "streak": streak_after,
            "total_days": total_after,
            "balance": int(record.get("balance") or 0),
            "progress": progress,
            "progress_label": progress_label,
            "milestone_text": milestone_text,
            "note": str(record.get("note") or "") or note_for(group_id, user_id, today_key(), streak_after),
            "currency": self.config.currency_name,
            "bot_name": self.config.checkin_bot_name,
            "already": bool(record.get("already")),
        }


def _date_label(date_key: str) -> str:
    try:
        day = date.fromisoformat(date_key)
    except ValueError:
        return date_key
    return f"{date_key} · {WEEKDAYS[day.weekday()]}"


def _card_title(total_days: int, streak_days: int, milestone: bool) -> str:
    """卡片大标题。有事件就说事件，没有就说「今日签到」。"""
    if milestone:
        return "签到纪念"
    if streak_days and streak_days % 7 == 0:
        return "连续相遇"
    if total_days == 1:
        return "第一次签到"
    return "今日签到"


def _item_note(item: ShopItem, db: Database, group_id: str, user_id: str) -> str:
    """商品右下角那行小字：背包存量 / 门槛 / 今日已买。

    头衔和关系是消耗品，最有用的信息是「背包里还剩几个」——
    囤了三个夜行者的人看到 ×3，才知道不用重复买。
    """
    if item.kind in ("title", "relation"):
        if item.locked:
            return f"需累计 {item.require_days} 天"
        held = db.bag_count(group_id, user_id, item.kind, item.item_id)
        return f"背包 ×{held}" if held else ""
    if item.owned:
        # 皮肤多一句「正戴着」：一张已经买过的名片，最想知道的是
        # 「我现在用的是不是这张」，而不是「我有没有」。
        return "正戴着" if item.wearing else "已拥有"
    if item.locked:
        return f"需累计 {item.require_days} 天"
    if item.kind == "service":
        # 服务是一次性买断，没有「今天还能买几个」这回事，写「不限购」是废话。
        return "永久有效"
    if item.kind in ("double", "ticket") and item.limit > 0:
        bought = db.shop_count(group_id, user_id, item.item_id)
        if bought >= item.limit:
            return "今日已买满"
        return f"今日已买 {bought}/{item.limit}"
    if item.kind == "title":
        return ""
    return item.limit_label


def iter_shop_names(items: Iterable[ShopItem]) -> str:
    """给帮助文案用的一句话：「双倍卡 / 抽奖券 / 常客 / …」。"""
    return " / ".join(item.name for item in items)
