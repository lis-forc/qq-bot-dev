"""配置读取层。

把 AstrBot 注入的 dict 包成带默认值和值域收敛的强类型属性，
插件其它地方只读属性，不再直接摸 dict，避免配置缺项导致崩溃。
"""

from __future__ import annotations

from typing import Any

DEFAULT_INFO_TRIGGERS = ("查", "我的信息", "我的卡片")

# 老版本的默认触发词。升级到「查」之后，如果服务器上存的还是这一串原封不动的
# 默认值，就当作用户没改过、直接换成新默认 —— 否则会出现「代码改成查了，
# 群里还得发 i」的怪事，而 WebUI 里那串配置没人会想起来去改。
LEGACY_INFO_TRIGGERS = {"i", "我的信息", "名片"}

# v3 起「名片」这三个字被「名片皮肤」那条口令占了。老的默认值里带着它，
# 会让「名片」永远先命中信息卡，新口令等于不存在 —— 所以这一串也要迁移掉。
LEGACY_INFO_TRIGGERS_V3 = {"查", "我的信息", "名片"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PluginConfig:
    """群助手配置。所有属性都自带兜底，缺项不会抛异常。"""

    def __init__(self, raw: dict[str, Any] | None):
        self.raw = raw or {}

    # ---------- 基础 ----------
    @property
    def enabled_group_ids(self) -> set[str]:
        values = self.raw.get("enabled_group_ids") or []
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        return {str(item).strip() for item in values if str(item).strip()}

    def is_group_enabled(self, group_id: Any) -> bool:
        if not group_id:
            return False
        enabled = self.enabled_group_ids
        # 留空 = 所有群生效，方便第一次上手测试。
        return not enabled or str(group_id) in enabled

    @property
    def currency_name(self) -> str:
        return str(self.raw.get("currency_name") or "额度").strip() or "额度"

    @property
    def count_command_messages(self) -> bool:
        return bool(self.raw.get("count_command_messages", False))

    # ---------- 数据保留 ----------
    @property
    def log_retention_days(self) -> int:
        """额度流水 / 操作日志保留天数，0 = 永久保留。"""
        return max(0, _as_int(self.raw.get("log_retention_days"), 180))

    @property
    def stats_retention_days(self) -> int:
        """按天发言统计保留天数，0 = 永久保留。累计数不受影响。"""
        return max(0, _as_int(self.raw.get("stats_retention_days"), 730))

    # ---------- 额度 ----------
    @property
    def welcome_quota(self) -> int:
        """新人礼。第一次在本群发言时到账，只发一次。"""
        return max(0, _as_int(self.raw.get("welcome_quota"), 10))

    @property
    def rename_cost(self) -> int:
        """改名时自动扣多少额度。

        **默认 0，也就是不自动扣。** 改名在这个群里是「进来就要做的事」，
        不是消费：新人不改名会被一直提醒到被静音，这时候再收他钱，等于
        罚他两次。要收钱由管理员用「扣额度 @某人」手动扣，那句是显式动作，
        谁扣的、扣了多少都进审计日志。
        """
        return max(0, _as_int(self.raw.get("rename_cost"), 0))

    # ---------- 入群：欢迎 ----------
    @property
    def welcome_enabled(self) -> bool:
        """有人入群时要不要说话。默认开 —— 静悄悄进群的群体验很差。"""
        return bool(self.raw.get("welcome_enabled", True))

    @property
    def welcome_text(self) -> str:
        """欢迎语模板。留空表示用内置文案。

        保留用户自己写的那几行换行，所以不做 ``strip`` 之外的处理：
        模板里 ``{at}`` 之类的占位符由 ``domains/welcome.py`` 填。
        """
        return str(self.raw.get("welcome_text") or "").strip()

    # ---------- 入群：验证 ----------
    @property
    def verify_enabled(self) -> bool:
        """入群验证。默认关 —— 它会禁言真人，属于「想清楚了才开」的功能。

        和欢迎语是两个独立开关：只想热闹的群只开欢迎，
        被广告号骚扰过的群两个都开。
        """
        return bool(self.raw.get("verify_enabled", False))

    @property
    def verify_question(self) -> str:
        return str(self.raw.get("verify_question") or "").strip()

    @property
    def verify_answers(self) -> tuple[str, ...]:
        """可接受的答案。空元组 = 任意一条消息都算通过。

        「任意消息即通过」是有意保留的默认：绝大多数群要的是「挡住机器人
        和广告号」，而不是真考人。真需要设门槛的群自己填答案。
        """
        raw = str(self.raw.get("verify_answers") or "")
        items = raw.replace("，", ",").replace("、", ",").split(",")
        return tuple(item.strip().lower() for item in items if item.strip())

    @property
    def verify_timeout_minutes(self) -> int:
        # 上限 60 分钟：再长等于没验证，新人在群里待一小时足够发广告了。
        return min(60, max(1, _as_int(self.raw.get("verify_timeout_minutes"), 5)))

    # ---------- 抽奖 ----------
    @property
    def draw_per(self) -> int:
        return max(1, _as_int(self.raw.get("draw_per"), 100))

    @property
    def draw_min(self) -> int:
        return max(0, _as_int(self.raw.get("draw_min"), 10))

    @property
    def draw_max(self) -> int:
        # 保证上限不低于下限，配置写反了也不会炸。
        return max(self.draw_min, _as_int(self.raw.get("draw_max"), 20))

    @property
    def draw_bonus_chance(self) -> float:
        return min(1.0, max(0.0, _as_float(self.raw.get("draw_bonus_chance"), 0.0)))

    @property
    def draw_bonus_amount(self) -> int:
        return max(0, _as_int(self.raw.get("draw_bonus_amount"), 8))

    @property
    def notify_draw_ready(self) -> bool:
        # 默认关闭。攒满一次抽奖就在群里喊一句，200 人的活跃群一天能喊几十次，
        # 这是纯噪音 —— 而且「额度」「今日」里本来就写着抽奖进度，信息不会丢。
        # 想要热闹的群自己开。
        return bool(self.raw.get("notify_draw_ready", False))

    # ---------- 签到 ----------
    @property
    def checkin_enabled(self) -> bool:
        return bool(self.raw.get("checkin_enabled", True))

    @property
    def checkin_base_min(self) -> int:
        return max(0, _as_int(self.raw.get("checkin_base_min"), 3))

    @property
    def checkin_base_max(self) -> int:
        # 上下限写反了也不炸：直接以 min 为准。
        return max(self.checkin_base_min, _as_int(self.raw.get("checkin_base_max"), 10))

    @property
    def checkin_streak_bonus(self) -> int:
        """连签每多一天多给多少额度。"""
        return max(0, _as_int(self.raw.get("checkin_streak_bonus"), 0))

    @property
    def checkin_streak_cap(self) -> int:
        """连签加成封顶天数。封顶是为了不让老群友的收益高到没人追得上。"""
        return min(365, max(1, _as_int(self.raw.get("checkin_streak_cap"), 10)))

    @property
    def checkin_bot_name(self) -> str:
        """签到卡的落款。"""
        return str(self.raw.get("checkin_bot_name") or "群助手").strip() or "群助手"

    @property
    def checkin_double_price(self) -> int:
        return max(0, _as_int(self.raw.get("checkin_double_price"), 6))

    @property
    def checkin_double_limit(self) -> int:
        """双倍卡每日限购。0 = 不限购（不建议，等于无限印钱）。"""
        return max(0, _as_int(self.raw.get("checkin_double_limit"), 1))

    @property
    def checkin_ticket_limit(self) -> int:
        """抽奖券每日限购。0 = 不限购。"""
        return max(0, _as_int(self.raw.get("checkin_ticket_limit"), 5))

    # ---------- 昵称 ----------
    @property
    def nick_enabled(self) -> bool:
        """昵称系统总开关：性别记号 + 名片回改 + 名字规则。

        关掉之后退回「只存一个显示名」的老行为：不加记号、不管名片、
        名字只限 12 字。适合不想被机器人碰群名片的群。
        """
        return bool(self.raw.get("nick_enabled", True))

    @property
    def nick_emoji_male(self) -> str:
        return str(self.raw.get("nick_emoji_male", "🦊") or "").strip()

    @property
    def nick_emoji_female(self) -> str:
        return str(self.raw.get("nick_emoji_female", "🐰") or "").strip()

    @property
    def nick_emoji_none(self) -> str:
        """性别没填时用的记号。空串 = 不加记号。"""
        return str(self.raw.get("nick_emoji_none", "🧸") or "").strip()

    @property
    def nick_separator(self) -> str:
        """记号与名字之间的分隔符。默认一个空格 —— 粘在一起会读成
        「🦊张三」是一个词，中间空一格才看得出那是前缀。"""
        raw = self.raw.get("nick_separator", " ")
        return " " if raw is None else str(raw)

    @property
    def nick_min_len(self) -> int:
        return max(1, _as_int(self.raw.get("nick_min_len"), 2))

    @property
    def nick_max_len(self) -> int:
        # 上限不低于下限：写反了按 4 字收，不能让它变成「必须 5 个字」。
        return max(self.nick_min_len, _as_int(self.raw.get("nick_max_len"), 4))

    @property
    def nick_allow_digits(self) -> bool:
        return bool(self.raw.get("nick_allow_digits", False))

    @property
    def nick_force(self) -> bool:
        """新成员没改名时要不要催、催几次之后静音。

        只在 ``nick_enabled`` 开着时有意义 —— 关了昵称系统就没有
        「什么算合规名字」这个判据，也就无从催起。
        """
        return bool(self.raw.get("nick_force", True))

    @property
    def nick_join_text(self) -> str:
        """入群时追加的改名提示。留空用内置文案，填了就用填的那段。"""
        return str(self.raw.get("nick_join_text") or "").strip()

    @property
    def nick_nag_text(self) -> str:
        """没改名的人发言时的提醒模板。留空用内置文案。"""
        return str(self.raw.get("nick_nag_text") or "").strip()

    @property
    def nick_nag_limit(self) -> int:
        """提醒几次之后静音。默认 3。"""
        return max(1, _as_int(self.raw.get("nick_nag_limit"), 3))

    @property
    def nick_nag_mute_minutes(self) -> int:
        """提醒超限后静音多少分钟。默认 1 —— 目的是打断他一下，
        不是惩罚，所以短到「他去改个名就自动恢复了」。"""
        return min(43200, max(1, _as_int(self.raw.get("nick_nag_mute_minutes"), 1)))

    @property
    def freeman_price(self) -> int:
        """「自由人」的价格：买了之后改名不再受字数与符号限制。"""
        return max(0, _as_int(self.raw.get("freeman_price"), 500))

    @property
    def title_price(self) -> int:
        """改一次群头衔的价格：买的是「一次修改权」，改什么内容买的人自己定。"""
        return max(0, _as_int(self.raw.get("title_price"), 100))

    # ---------- 排行 ----------
    @property
    def rank_message_enabled(self) -> bool:
        """发言榜开关。默认关。

        它关掉时口令**完全失效**（匹配器直接不接），群友发「发言榜」
        会变成一句普通聊天 —— 不会有人收到「这个功能没开」的解释。
        """
        return bool(self.raw.get("rank_message_enabled", False))

    # ---------- 群管 ----------
    @property
    def default_mute_minutes(self) -> int:
        """禁言不写时长时用多少分钟。

        QQ 里「禁言他一下」是最常见的说法，逼着写「10分钟」纯属多余。
        上限 43200 分钟 = 30 天，再长协议端会拒绝。
        """
        return min(43200, max(1, _as_int(self.raw.get("default_mute_minutes"), 10)))

    # ---------- 表情包 ----------
    @property
    def meme_api_base_url(self) -> str:
        """meme-generator 的地址。

        这个插件自己不画图，图是外面那个服务画的。地址填错的表现是
        「做了半天没反应」，所以默认值就按容器最常用的写法给。
        """
        value = str(self.raw.get("meme_api_base_url") or "").strip()
        return (value or "http://127.0.0.1:2233").rstrip("/")

    @property
    def meme_cost(self) -> int:
        """做一张表情扣多少额度。0 = 免费。"""
        return max(0, _as_int(self.raw.get("meme_cost"), 3))

    @property
    def meme_timeout(self) -> int:
        """请求表情服务的超时秒数。动图模板会慢，给宽一点。"""
        return min(300, max(5, _as_int(self.raw.get("meme_timeout"), 30)))

    @property
    def meme_max_image_mb(self) -> int:
        """单张素材图的大小上限。群里偶尔会有人发很大的原图。"""
        return min(50, max(1, _as_int(self.raw.get("meme_max_image_mb"), 10)))

    @property
    def meme_auto_avatar(self) -> bool:
        """图不够时自动补发送者头像。关掉就得自己 @ 或发图。"""
        return bool(self.raw.get("meme_auto_avatar", True))

    @property
    def meme_auto_texts(self) -> bool:
        """没写文字时用模板自带的默认文字。"""
        return bool(self.raw.get("meme_auto_texts", True))

    @property
    def meme_catalog_size(self) -> int:
        """一张图鉴放多少个模板。

        图鉴是「一张图看全」而不是「翻 13 页」，所以这个数决定一共几张图。
        取 4 的倍数是因为网格四列 —— 不是 4 的倍数最后一行会缺角，
        看着像漏画了几个。
        """
        value = _as_int(self.raw.get("meme_catalog_size"), 60)
        return min(120, max(24, value // 4 * 4))

    # ---------- 交互 ----------
    @property
    def info_triggers(self) -> set[str]:
        raw = self.raw.get("info_triggers")
        text = "" if raw is None else str(raw).strip()
        if not text:
            return set(DEFAULT_INFO_TRIGGERS)
        items = {item.strip().lower() for item in text.replace("，", ",").split(",")}
        items.discard("")
        # 还没被改过、仍是老默认值 → 跟着新默认走（i → 查，名片 → 我的卡片）
        if items and items in (LEGACY_INFO_TRIGGERS, LEGACY_INFO_TRIGGERS_V3):
            return set(DEFAULT_INFO_TRIGGERS)
        return items or set(DEFAULT_INFO_TRIGGERS)

    @property
    def help_style(self) -> str:
        value = str(self.raw.get("help_style") or "forward").strip().lower()
        return value if value in {"forward", "text"} else "forward"

    # ---------- 信息卡 ----------
    @property
    def card_font_path(self) -> str:
        return str(self.raw.get("card_font_path") or "assets/fonts/LXGWWenKai-Medium.ttf").strip()

    # ---------- 图片素材 ----------
    @property
    def image_root(self) -> str:
        """三类图片的根目录。

        留空就是插件目录下的 `assets/images`，里面再分 checkin / catalog / cards。
        填绝对路径可以直接指向服务器上任意一个图库 —— 换图不用碰代码。
        """
        return str(self.raw.get("image_root") or "assets/images").strip() or "assets/images"

    @property
    def card_skin_price(self) -> int:
        """名片皮肤的默认售价。单个皮肤可以在文件名里写 `名字@价格` 覆盖。"""
        return max(0, _as_int(self.raw.get("card_skin_price"), 50))

    @property
    def checkin_image(self) -> bool:
        """签到要不要带一张运势图。关掉就只发文字，适合没有图可用的场景。"""
        return bool(self.raw.get("checkin_image", True))

    # ---------- 权限 ----------
    @property
    def owner_default_level(self) -> int:
        return min(4, max(1, _as_int(self.raw.get("owner_default_level"), 4)))

    @property
    def admin_default_level(self) -> int:
        return min(4, max(1, _as_int(self.raw.get("admin_default_level"), 3)))
