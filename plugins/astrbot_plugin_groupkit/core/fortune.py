"""今日运势：签到那一下的「仪式感」，纯装饰。

为什么单独成文件而不是塞进 `checkin.py`：这一块的规则**和额度一点关系都没有**。
它只影响「今天签到这条消息读起来像不像一张签」，将来想换词库、加彩蛋、
甚至按节日换一套，都不该碰额度那段钱账。

和奖励一样用「确定性随机」：种子是 `日期 + 群号 + QQ号`，
所以同一个人同一天重发「签到」，看到的运势和第一次完全一致。
反过来如果这里用 `random.choice`，群友重发一次就换一个吉凶，
那「运势」立刻变成「抽卡」—— 他会一直发到抽到大吉为止。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

# 吉凶档位与权重。刻意不给「凶」：签到是件轻松事，
# 让人一早被自己的机器人判个凶，图什么呢。
LEVELS: tuple[tuple[str, int], ...] = (
    ("大吉", 8),
    ("中吉", 22),
    ("吉", 40),
    ("小吉", 22),
    ("末吉", 8),
)

# 「宜 / 忌」的词库。写成具体的小动作而不是抽象词（「宜：努力」没有信息量，
# 「宜：把待办清空」才像一句人话）。都控制在一行内，排版不会折行。
YI: tuple[str, ...] = (
    "摸鱼",
    "早睡",
    "点一杯奶茶",
    "认真夸人",
    "清空待办",
    "出门走走",
    "听首老歌",
    "请人吃饭",
    "翻翻旧照片",
    "把心一横",
    "慢慢来",
    "多说一句好话",
    "重启一下",
    "收拾桌面",
)

JI: tuple[str, ...] = (
    "熬夜",
    "空腹喝咖啡",
    "翻旧账",
    "冲动下单",
    "硬撑",
    "较真",
    "半夜发长消息",
    "拖延",
    "把话说满",
    "算小账",
    "等一条不会来的消息",
    "连续刷手机",
    "赶最后一分钟",
    "跟自己较劲",
)

# 幸运色。选的都是能一眼说出颜色名字的词，不写「#FF6B9D」。
COLORS: tuple[str, ...] = (
    "雾蓝",
    "杏色",
    "苔绿",
    "奶油白",
    "藕粉",
    "墨黑",
    "赭石",
    "烟灰",
)


@dataclass(frozen=True)
class Fortune:
    level: str
    yi: str
    ji: str
    color: str
    number: int

    def line(self) -> str:
        """一行装下宜与忌。签到文字里只留一行，多了会盖过额度。

        中点分隔而不是「，」：两件事并列，视觉上要有个硬隔断。
        """
        return f"宜 {self.yi} · 忌 {self.ji}"

    def brief(self) -> str:
        return f"幸运色 {self.color} · 幸运数字 {self.number}"


def _rng(*parts: str) -> random.Random:
    text = "|".join(parts).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(text).digest()[:8], "big"))


def today(group_id: str, user_id: str, date_key: str) -> Fortune:
    """算今天的运势。同输入必同输出，跨进程跨重启都一样。"""
    rng = _rng("fortune", str(date_key), str(group_id), str(user_id))

    pool = [level for level, weight in LEVELS for _ in range(weight)]
    level = rng.choice(pool)
    yi = rng.choice(YI)
    ji = rng.choice(JI)
    if ji == yi:
        # 两份词库不重叠，正常撞不上；万一以后合并词库，
        # 这里保证不会印出「宜摸鱼 · 忌摸鱼」。
        ji = JI[(JI.index(ji) + 1) % len(JI)]
    return Fortune(
        level=level,
        yi=yi,
        ji=ji,
        color=rng.choice(COLORS),
        number=rng.randint(1, 99),
    )
