"""昵称规则：性别三态、性别记号、群名片拼装与还原、名字校验。

**为什么单独一个模块。**
昵称这一套有四个消费者，任何一个自己写一套规则，群里就会出现
「改名说他太长了，但他手动改的群名片机器人又认」这种事：

1. `domains/nickname.py` —— 改名流程与发言守卫（真正干活的那层）；
2. `domains/profile.py` —— 信息卡要显示的名字；
3. `domains/welcome.py` —— 入群欢迎语里的称呼；
4. `core/services.py` —— 从协议端同步群名片时要剥掉记号。

所以这里全是纯函数，不碰数据库、不碰事件、不发消息。配置从
`PluginConfig` 上读，但只读，不改。

**群名片的形状。**
    ::

        🦊 张三
        ↑   ↑
        │   └── 名字（2~4 字，不含数字与符号）
        └────── 性别记号（男 🦊 / 女 🐰 / 未设定 🧸）

记号与名字之间那个分隔符也是可配的（默认一个空格）。整个字符串就是
「机器人认为你的群名片应该长什么样」，守卫逻辑做的唯一一件事就是拿它
和真实群名片比对，不一致就改回来 —— 比对是纯字符串操作，零 API 调用。

**为什么「剥记号」要认三套。**
只认当前配置的话，群主把 🦊 改成 🐯 之后，所有老卡片都会被解析成
「名字叫『🦊 张三』」—— 带空格和 emoji 的名字一律不合规，于是全群
开始被催改名。所以内置的三个默认记号永远参与剥离，配置里的额外加上。
"""

from __future__ import annotations

import re

# 三套内置记号。即使群主改了配置也要认，否则改名之后就认不出旧卡片了。
BUILTIN_MARKS: tuple[str, str, str] = ("🦊", "🐰", "🧸")

# 群名片里的名字最长多少 —— 这是「昵称系统关掉」时的兜底上限，
# 也是自由人可以取到的长度。开着昵称系统时用配置里的 2~4。
LOOSE_MAX_LEN = 12

# 性别只存这三种，第三种用空串表示（「没填」就是空，不是第四个值）。
SEX_MALE = "男"
SEX_FEMALE = "女"
SEX_NONE = ""

# 「明确说了不设」在库里存成这个记号。
#
# 为什么不能就用空串：空串在存储层表示「从没管过」，而 `refresh_from_qq`
# 会拿协议端的资料去补它。如果群友主动发「性别 不设」，我们存空串，
# 下一条消息又会被 QQ 资料填成「男」—— 他的选择被无声地推翻了。
# 存一个非空记号，补全逻辑就会把它当成「他已经表过态，别动」。
SEX_UNSET_MARK = "-"

_SEX_ALIASES = {
    "男": SEX_MALE, "男生": SEX_MALE, "男的": SEX_MALE, "男性": SEX_MALE, "♂": SEX_MALE,
    "male": SEX_MALE, "m": SEX_MALE, "man": SEX_MALE, "1": SEX_MALE,
    "女": SEX_FEMALE, "女生": SEX_FEMALE, "女的": SEX_FEMALE, "女性": SEX_FEMALE,
    "♀": SEX_FEMALE,
    "female": SEX_FEMALE, "f": SEX_FEMALE, "woman": SEX_FEMALE, "2": SEX_FEMALE,
    # 「保密」「未知」不是第四种性别，它就是「没说」—— 记号是 🧸。
    "未设定": SEX_NONE, "保密": SEX_NONE, "未知": SEX_NONE, "不说": SEX_NONE,
    "隐藏": SEX_NONE, "不告诉你": SEX_NONE, "无": SEX_NONE, "清除": SEX_NONE,
    "unknown": SEX_NONE, "secret": SEX_NONE, "0": SEX_NONE,
    # 「明确不设」的存储记号要能被读回空串，否则每次读库都得特判。
    SEX_UNSET_MARK: SEX_NONE,
}

# 表单里能填的三个值，顺序即展示顺序。
SEX_CHOICES: tuple[str, str, str] = (SEX_MALE, SEX_FEMALE, "未设定")


def normalize_sex(raw: object) -> str:
    """把任意写法归一成「男 / 女 / 空串」。看不懂也返回空串（未设定）。

    空串而不是 None：库里 sex 的默认值就是空串，「没填」这件事本身
    就是一个合法状态，不需要再多一个 None 来表示它。
    """
    return _SEX_ALIASES.get(str(raw or "").strip().lower(), SEX_NONE)


def store_sex(sex: str) -> str:
    """展示层的三态 → 库里存的值。空串要落成显式记号，见 ``SEX_UNSET_MARK``。"""
    return str(sex or "").strip() or SEX_UNSET_MARK


def sex_label(sex: str) -> str:
    """展示用：空串 → 「未设定」。"""
    return sex or "未设定"


def parse_sex(text: str | None) -> str | None:
    """口令参数 → 三态之一。看不懂返回 None（和上面的 normalize 区分开：
    这里 None 表示「用户打错字了」，要报错；空串表示「用户就是想清掉」）。"""
    key = str(text or "").strip().lower()
    return _SEX_ALIASES.get(key)


# ---------------------------------------------------------------- 记号与拼装
def mark_for(sex: str, cfg) -> str:
    """这个性别对应哪个记号。配置里清空了就返回空串（不加记号）。"""
    if sex == SEX_FEMALE:
        return str(getattr(cfg, "nick_emoji_female", "") or "")
    if sex == SEX_MALE:
        return str(getattr(cfg, "nick_emoji_male", "") or "")
    return str(getattr(cfg, "nick_emoji_none", "") or "")


def _known_marks(cfg) -> tuple[str, ...]:
    """所有要参与剥离的记号：内置三个 + 配置里的三个。去重、去掉空值。"""
    extra = (
        str(getattr(cfg, "nick_emoji_male", "") or ""),
        str(getattr(cfg, "nick_emoji_female", "") or ""),
        str(getattr(cfg, "nick_emoji_none", "") or ""),
    )
    seen: list[str] = []
    for mark in (*BUILTIN_MARKS, *extra):
        if mark and mark not in seen:
            seen.append(mark)
    return tuple(seen)


def _separator(cfg) -> str:
    """记号与名字之间的分隔符。配置成空串也能用（就是粘在一起）。"""
    return str(getattr(cfg, "nick_separator", " ") or "")


def compose(name: str, sex: str, cfg) -> str:
    """拼出「机器人认为的群名片」。记号为空时就是名字本身。"""
    name = str(name or "").strip()
    mark = mark_for(sex, cfg)
    if not mark:
        return name
    return f"{mark}{_separator(cfg)}{name}"


def strip(card: str, cfg) -> str:
    """从群名片里剥掉记号与分隔符，留下名字。

    对没有记号的卡片是幂等的 —— 这一条很重要：群里绝大多数人的群名片
    是自己在 QQ 里起的，不该因为「我们的规则里有记号」就被改坏。
    """
    text = str(card or "").strip()
    for mark in _known_marks(cfg):
        if text.startswith(mark):
            text = text[len(mark):]
            break
    sep = _separator(cfg)
    if sep and text.startswith(sep):
        text = text[len(sep):]
    # `strip()` 兜住「记号后面跟着一串空格」这种手改出来的形态。
    return text.strip()


# ---------------------------------------------------------------- 名字校验
_NEWLINE = re.compile(r"[\r\n\t]")


def validate(name: str, cfg) -> tuple[bool, str]:
    """校验一个**名字**（不含记号）。返回 ``(是否通过, 不通过的原因)``。

    三条规则，顺序就是用户最容易犯错的顺序：

    1. 长度（默认 2~4 字）—— 一个字的昵称在群里没法称呼，五个字以上
       在手机上会把一行撑开；
    2. 不能有数字 —— 号码、年份、生日都是「个人信息」，不是名字；
    3. 不能有符号 —— emoji、标点、空格都算。``str.isalnum()`` 对汉字和
       字母为真、对标点和 emoji 为假，正好是我们要的判定。
    """
    text = str(name or "").strip()
    if not text:
        return False, "名字不能为空。"
    if _NEWLINE.search(text):
        return False, "名字里不能有换行。"

    low = max(1, int(getattr(cfg, "nick_min_len", 2)))
    high = max(low, int(getattr(cfg, "nick_max_len", 4)))
    if len(text) < low or len(text) > high:
        if low == high:
            return False, f"名字要 {low} 个字。"
        return False, f"名字要 {low}~{high} 个字，你写的是 {len(text)} 个。"

    allow_digits = bool(getattr(cfg, "nick_allow_digits", False))
    for char in text:
        if char.isdigit():
            if not allow_digits:
                return False, "名字里不能有数字。"
            continue
        if not char.isalnum():
            return False, f"名字里不能有「{char}」这种符号。"
    return True, ""


def is_valid(name: str, cfg) -> bool:
    return validate(name, cfg)[0]


def validate_loose(name: str) -> tuple[bool, str]:
    """宽松版：只限长度和换行。给「昵称系统关掉」和「自由人」用。

    自由人买了之后机器人确实不管了，但名字仍然要能存进库、能在卡片上
    排得下 —— 12 个字的上限是排版限制，不是身份限制。
    """
    text = str(name or "").strip()
    if not text:
        return False, "名字不能为空。"
    if _NEWLINE.search(text):
        return False, "名字里不能有换行。"
    if len(text) > LOOSE_MAX_LEN:
        return False, f"名字请控制在 {LOOSE_MAX_LEN} 个字以内。"
    return True, ""


# ---------------------------------------------------------------- 文案
def rule_text(cfg) -> str:
    """一句话概括规则，给报错和帮助文案复用，避免两处写出不同的数字。"""
    low = max(1, int(getattr(cfg, "nick_min_len", 2)))
    high = max(low, int(getattr(cfg, "nick_max_len", 4)))
    digits = "可以有数字" if getattr(cfg, "nick_allow_digits", False) else "不要数字"
    span = f"{low} 个字" if low == high else f"{low}~{high} 个字"
    return f"{span}，{digits}，不要符号"
