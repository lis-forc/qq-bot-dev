"""SQLite 数据层。

跑得久的几个前提，这里都落实了：

1. **WAL + busy_timeout**：读写不互相堵，跨进程并发也不会立刻抛 database is locked。
2. **写事务**：所有多语句写操作用 `BEGIN IMMEDIATE ... COMMIT` 包起来，
   中途异常整体回滚，不会出现「扣了钱没记流水」这种半截状态。
3. **进程内写锁**：RLock 串行化本插件的写操作，避免自己和自己抢锁。
4. **索引**：按真实查询模式建，排行榜类查询走 (group_id, quota) 而不是全表扫。
5. **保留期清理**：流水和按天统计会无限增长，定期按天数裁剪（累计数存在 members 上，不受影响）。
6. **结构版本**：PRAGMA user_version 记录结构版本，配合列检查做可重复迁移。

命名约定：表名复数、字段蛇形、时间字段统一
`*_time` 存 unix 秒（便于范围查询）、`created_at` 存 'YYYY-MM-DD HH:MM:SS'（便于人读和按日裁剪）。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # 本地单测时没有 astrbot，退化成标准库 logging
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

SCHEMA_VERSION = 5  # 结构版本；改表时 +1（v2 签到，v3 名片皮肤，v4 昵称，v5 背包与关系）

# 「今天」按北京时间算，不看容器时区。
#
# 为什么必须钉死：Docker 容器默认是 UTC，比北京慢 8 小时。用 date.today()
# 的话，北京时间 0:00-8:00 签到会被算成「昨天」，群友熬夜签到会看到
# 「今天已经签过了」或者连签断掉。签到、发言统计、流水时间戳全部走这里，
# 保证「今日发言」「今日签到」「额度记录的时间」是同一个日历。
CN_TZ = timezone(timedelta(hours=8))


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def _today() -> str:
    return cn_now().date().isoformat()


def _now() -> str:
    return cn_now().strftime("%Y-%m-%d %H:%M:%S")


def _day_before(date_key: str) -> str:
    """'2026-10-26' → '2026-10-25'。用日期减法，不用记月份 / 闰年。"""
    try:
        return (date.fromisoformat(date_key) - timedelta(days=1)).isoformat()
    except ValueError:
        return ""


class Database:
    """群助手数据层。同步方法，调用方用 asyncio.to_thread 包一层。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "groupkit.db"

        self._lock = threading.RLock()
        # 一线程一条连接：管理台接口跑在线程池（asyncio.to_thread），
        # 聊天路径跑在主循环，两边会真的同时碰库。
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._closed = False

        self._local.in_tx = False
        self._connect()

        self._init_db()
        logger.info(
            f"[群助手] 数据库就绪: {self.db_path} "
            f"(schema v{self.schema_version}, {self.db_size_kb()} KB)"
        )

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 关掉隐式事务，全部显式 BEGIN/COMMIT
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row

        # 这些 PRAGMA 必须在事务外执行
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")

        self._local.conn = conn
        self._local.in_tx = False
        self._closed = False
        with self._lock:
            self._all_conns.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的连接，没有就建一条。

        sqlite3 连接不是线程安全的：A 线程正在 fetch 一个游标，B 线程在
        同一连接上 execute，就会抛 "bad parameter or other API misuse"。
        以前全插件共用一条连接，平时没事，是因为聊天路径基本单线程跑；
        一旦管理台接口从线程池里查库，两边重叠就炸。所以按线程分开。
        跨线程的写入仍然由 self._lock 串行化，WAL 让读不挡写。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None or self._closed:
            # 关库之后又有人来查（比如重载插件的间隙），重新开一条，
            # 否则会拿着已关闭的连接报「Cannot operate on a closed database」，
            # 那个报错看不出真正原因。
            conn = self._connect()
        return conn

    # ------------------------------------------------------------------ 事务
    @contextmanager
    def _tx(self) -> Iterator[None]:
        """写事务。嵌套调用复用外层，异常整体回滚。

        `_local.in_tx` 是「本线程已经在事务里」的标志 —— 事务属于连接，
        而连接属于线程，所以这个标志也必须按线程记，不能用类上的一个布尔。
        """
        with self._lock:
            local = self._local
            if getattr(local, "in_tx", False):
                yield
                return
            local.in_tx = True
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            finally:
                local.in_tx = False

    def close(self) -> None:
        """关掉所有线程的连接。关库发生在别人线程上也得关干净，不然 WAL 文件留着。"""
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
            self._closed = True
        for conn in conns:
            try:
                conn.execute("PRAGMA optimize")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    def db_size_kb(self) -> int:
        try:
            return max(1, self.db_path.stat().st_size // 1024)
        except OSError:
            return 0

    @property
    def schema_version(self) -> int:
        try:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        except Exception:
            return 0

    # ------------------------------------------------------------------ 建表 / 迁移
    def _init_db(self) -> None:
        statements = [
            # 成员：一人一行，按群隔离。没有「注册」，首次说话即建档。
            """
            CREATE TABLE IF NOT EXISTS members (
                group_id          TEXT    NOT NULL,
                user_id           TEXT    NOT NULL,
                display_name      TEXT    NOT NULL DEFAULT '',
                qq_name           TEXT    NOT NULL DEFAULT '',
                sex               TEXT    NOT NULL DEFAULT '',
                hobby             TEXT    NOT NULL DEFAULT '',
                quota             INTEGER NOT NULL DEFAULT 0,
                total_messages    INTEGER NOT NULL DEFAULT 0,
                msg_progress      INTEGER NOT NULL DEFAULT 0,
                draws             INTEGER NOT NULL DEFAULT 0,
                permission_level  INTEGER,
                join_time         INTEGER,
                first_seen        TEXT    NOT NULL DEFAULT '',
                welcomed          INTEGER NOT NULL DEFAULT 0,
                card_skin         TEXT    NOT NULL DEFAULT '',
                nick_nag          INTEGER NOT NULL DEFAULT 0,
                freeman           INTEGER NOT NULL DEFAULT 0,
                -- 关系：结缔成功后双方各写一份。relation_with 存对方的 QQ 号，
                -- 没有它就分不清「和谁的关系」，解除时也没法两边一起清。
                relation          TEXT    NOT NULL DEFAULT '',
                relation_with     TEXT    NOT NULL DEFAULT '',
                updated_at        TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            )
            """,
            # 按天发言统计：一人一天一行，只在算「今日/本月」时用，可按保留期裁剪。
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                group_id   TEXT    NOT NULL,
                user_id    TEXT    NOT NULL,
                stat_date  TEXT    NOT NULL,
                msg_count  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id, stat_date)
            )
            """,
            # 额度流水：只增不改，可审计，按保留期裁剪。
            """
            CREATE TABLE IF NOT EXISTS quota_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT    NOT NULL,
                user_id     TEXT    NOT NULL,
                amount      INTEGER NOT NULL,
                reason      TEXT    NOT NULL DEFAULT '',
                operator_id TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT ''
            )
            """,
            # 管理操作审计
            """
            CREATE TABLE IF NOT EXISTS op_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT    NOT NULL,
                operator_id TEXT    NOT NULL DEFAULT '',
                action      TEXT    NOT NULL DEFAULT '',
                target_id   TEXT    NOT NULL DEFAULT '',
                detail      TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT ''
            )
            """,
            # ---------------- 签到（v2）----------------
            # 一人一行，按群隔离。连续天数只存「最后一次签到日期」+「当前连签」，
            # 断没断由签到那一刻现算 —— 每天跑定时任务去清零的写法，
            # 一旦某天机器人没在线，全群连签就集体作废。
            """
            CREATE TABLE IF NOT EXISTS checkins (
                group_id        TEXT    NOT NULL,
                user_id         TEXT    NOT NULL,
                total_days      INTEGER NOT NULL DEFAULT 0,
                streak_days     INTEGER NOT NULL DEFAULT 0,
                best_streak     INTEGER NOT NULL DEFAULT 0,
                last_date       TEXT    NOT NULL DEFAULT '',
                title_id        TEXT    NOT NULL DEFAULT '',
                double_pending  INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT '',
                updated_at      TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            )
            """,
            # 签到快照：一天一条，卡片上的数字全部从这里读。
            # 这样「重发当天卡片」不需要重算奖励，改了配置也不会让历史卡片变样。
            """
            CREATE TABLE IF NOT EXISTS checkin_records (
                group_id        TEXT    NOT NULL,
                user_id         TEXT    NOT NULL,
                date_key        TEXT    NOT NULL,
                base_quota      INTEGER NOT NULL DEFAULT 0,
                streak_bonus    INTEGER NOT NULL DEFAULT 0,
                milestone_bonus INTEGER NOT NULL DEFAULT 0,
                reward          INTEGER NOT NULL DEFAULT 0,
                doubled         INTEGER NOT NULL DEFAULT 0,
                streak_after    INTEGER NOT NULL DEFAULT 0,
                total_after     INTEGER NOT NULL DEFAULT 0,
                balance_after   INTEGER NOT NULL DEFAULT 0,
                note            TEXT    NOT NULL DEFAULT '',
                created_at      TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, user_id, date_key)
            )
            """,
            # 商店购买计数：每日限购靠这张表的行数实现，不依赖内存状态，
            # 重启机器人也不会把「今天已经买过」忘掉。
            """
            CREATE TABLE IF NOT EXISTS shop_purchases (
                group_id TEXT    NOT NULL,
                user_id  TEXT    NOT NULL,
                item_id  TEXT    NOT NULL,
                buy_date TEXT    NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id, item_id, buy_date)
            )
            """,
            # ---------------- 名片皮肤（v3）----------------
            # 已买的名片。名字就是图片文件名（去掉扩展名），
            # 所以删掉一张图之后，用过它的人的记录会变成「指向不存在的皮肤」——
            # 展示层会自动退回默认皮肤，不会报错，也不会丢记录。
            """
            CREATE TABLE IF NOT EXISTS member_skins (
                group_id    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                skin        TEXT NOT NULL,
                acquired_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (group_id, user_id, skin)
            )
            """,
            # ---------------- 背包（v5）----------------
            # 消耗品仓库：头衔、关系这类「买来先存着，用了才生效」的东西。
            # 头衔的行只有一条（item_id = "title"），count 就是还剩几次
            # 「改头衔」的机会；关系的 item_id 是关系名本身。
            # 抽奖次数存在 members.draws，不放这里（它有
            # 自己的结算链路，bump_message 在攒、抽奖在扣）。
            """
            CREATE TABLE IF NOT EXISTS bag (
                group_id TEXT    NOT NULL,
                user_id  TEXT    NOT NULL,
                kind     TEXT    NOT NULL,
                item_id  TEXT    NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id, kind, item_id)
            )
            """,
            # ---------------- 索引：按真实查询模式建 ----------------
            # 今日/本月统计：WHERE group_id,user_id AND stat_date [...]
            "CREATE INDEX IF NOT EXISTS idx_daily_user_date "
            "ON daily_stats (group_id, user_id, stat_date)",
            # 群维度按日汇总（日报/活跃度）
            "CREATE INDEX IF NOT EXISTS idx_daily_group_date "
            "ON daily_stats (group_id, stat_date)",
            # 额度流水：按人查流水
            "CREATE INDEX IF NOT EXISTS idx_quota_user ON quota_logs (group_id, user_id, id)",
            # 裁剪：按时间删
            "CREATE INDEX IF NOT EXISTS idx_quota_time ON quota_logs (created_at)",
            "CREATE INDEX IF NOT EXISTS idx_op_time ON op_logs (created_at)",
            # 排行榜预留：群内按额度排序
            "CREATE INDEX IF NOT EXISTS idx_members_quota ON members (group_id, quota DESC)",
            "CREATE INDEX IF NOT EXISTS idx_members_messages ON members (group_id, total_messages DESC)",
            # 签到排行：按群取连签榜 / 累计榜
            "CREATE INDEX IF NOT EXISTS idx_checkins_streak ON checkins (group_id, streak_days DESC)",
            "CREATE INDEX IF NOT EXISTS idx_checkins_total ON checkins (group_id, total_days DESC)",
            # 签到日历：按群按日期倒序取最近若干天
            "CREATE INDEX IF NOT EXISTS idx_checkin_records_date "
            "ON checkin_records (group_id, date_key DESC)",
        ]
        with self._tx():
            for statement in statements:
                self.conn.execute(statement)
            self._migrate()
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self) -> None:
        """给老库补列。判断依据是列是否存在，所以可以重复执行。"""
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(members)").fetchall()
        } if self._table_exists("members") else set()
        additions = {
            "display_name": "TEXT NOT NULL DEFAULT ''",
            "qq_name": "TEXT NOT NULL DEFAULT ''",
            "sex": "TEXT NOT NULL DEFAULT ''",
            "hobby": "TEXT NOT NULL DEFAULT ''",
            "msg_progress": "INTEGER NOT NULL DEFAULT 0",
            "draws": "INTEGER NOT NULL DEFAULT 0",
            "permission_level": "INTEGER",
            "join_time": "INTEGER",
            "welcomed": "INTEGER NOT NULL DEFAULT 0",
            "card_skin": "TEXT NOT NULL DEFAULT ''",
            # v4：nick_nag 记「已经被提醒几次改名」，改名成功或静音之后清零；
            # freeman 是买了「自由人」服务的人，昵称规则与名片守护对他全部失效。
            "nick_nag": "INTEGER NOT NULL DEFAULT 0",
            "freeman": "INTEGER NOT NULL DEFAULT 0",
            # v5：关系。结缔成功后双方各写一份，解除时两边一起清。
            "relation": "TEXT NOT NULL DEFAULT ''",
            "relation_with": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE members ADD COLUMN {name} {ddl}")

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ 成员
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def get_member(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM members WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        return self._row(cur.fetchone())

    def ensure_member(self, group_id: str, user_id: str, welcome_quota: int = 0) -> dict[str, Any]:
        """拿成员，没有就建行；首次建行时顺手发新人礼（靠 welcomed 标记保证只发一次）。"""
        member = self.get_member(group_id, user_id)
        if member:
            return member

        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO members (group_id, user_id, first_seen, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (group_id, user_id, _now(), _now()),
            )
            if welcome_quota > 0:
                cur = self.conn.execute(
                    "UPDATE members SET quota = quota + ?, welcomed = 1 "
                    "WHERE group_id = ? AND user_id = ? AND welcomed = 0",
                    (welcome_quota, group_id, user_id),
                )
                if cur.rowcount:
                    self._log_quota(group_id, user_id, welcome_quota, "新人礼", "system")
        return self.get_member(group_id, user_id) or {}

    def set_join_info(
        self,
        group_id: str,
        user_id: str,
        *,
        join_time: int | None = None,
        sex: str | None = None,
        qq_name: str | None = None,
    ) -> dict[str, Any]:
        """缓存从协议端拿到的入群时间 / 性别 / 群名片。只写非空值。"""
        sets: list[str] = []
        values: list[Any] = []
        if join_time is not None and int(join_time) > 0:
            sets.append("join_time = ?")
            values.append(int(join_time))
        if sex:
            sets.append("sex = ?")
            values.append(sex)
        if qq_name:
            sets.append("qq_name = ?")
            values.append(qq_name)
        if not sets:
            return self.get_member(group_id, user_id) or {}

        sets.append("updated_at = ?")
        values.append(_now())
        values.extend([group_id, user_id])
        with self._tx():
            self.conn.execute(
                f"UPDATE members SET {', '.join(sets)} WHERE group_id = ? AND user_id = ?",
                values,
            )
        return self.get_member(group_id, user_id) or {}

    def set_display_name(self, group_id: str, user_id: str, name: str) -> dict[str, Any]:
        """写显示名，同时清掉催改名计数。

        两件事必须在同一条 SQL 里：名字一改，「还没改名」这个状态就消失了，
        计数留着只会让下一次被催的人少挨几次提醒 —— 那是别人的账，不是他的。
        """
        with self._tx():
            self.conn.execute(
                "UPDATE members SET display_name = ?, nick_nag = 0, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (name, _now(), group_id, user_id),
            )
        return self.get_member(group_id, user_id) or {}

    def set_hobby(self, group_id: str, user_id: str, hobby: str) -> dict[str, Any]:
        with self._tx():
            self.conn.execute(
                "UPDATE members SET hobby = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (hobby, _now(), group_id, user_id),
            )
        return self.get_member(group_id, user_id) or {}

    def set_sex(self, group_id: str, user_id: str, sex: str) -> dict[str, Any]:
        """手动设置性别。

        写进去之后 `refresh_from_qq` 就不会再从协议端覆盖它 —— 那边只在
        sex 为空时才补全，所以「群友自己改了性别」的优先级天然高于 QQ 资料。
        """
        with self._tx():
            self.conn.execute(
                "UPDATE members SET sex = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (sex, _now(), group_id, user_id),
            )
        return self.get_member(group_id, user_id) or {}

    def set_permission(self, group_id: str, user_id: str, level: int | None) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE members SET permission_level = ?, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (level, _now(), group_id, user_id),
            )

    # ---------------------------------------------------------- 昵称
    def bump_nick_nag(self, group_id: str, user_id: str) -> int:
        """记为「又提醒了一次改名」，返回累计次数。

        计数放在库里而不是内存：机器人重启一次就忘掉的话，一个常年不改名
        的人可以靠重启无限躲过静音，而重启在真实的服务器上是常态。
        """
        self.ensure_member(group_id, user_id)
        with self._tx():
            self.conn.execute(
                "UPDATE members SET nick_nag = nick_nag + 1, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (_now(), group_id, user_id),
            )
            return int(
                self._scalar(
                    "SELECT nick_nag FROM members WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                )
                or 0
            )

    def reset_nick_nag(self, group_id: str, user_id: str) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE members SET nick_nag = 0, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (_now(), group_id, user_id),
            )

    def set_freeman(self, group_id: str, user_id: str, on: bool = True) -> None:
        self.ensure_member(group_id, user_id)
        with self._tx():
            self.conn.execute(
                "UPDATE members SET freeman = ?, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (1 if on else 0, _now(), group_id, user_id),
            )

    def buy_freeman(self, group_id: str, user_id: str, price: int) -> tuple[bool, str, int]:
        """买「自由人」：查重 → 扣额度 → 置位，一个事务。

        和名片皮肤一样是「一次性买断」，所以不需要每日限购表 ——
        freeman 这一列本身就是「已拥有」的凭据。
        """
        self.ensure_member(group_id, user_id)
        with self._tx():
            member = self.get_member(group_id, user_id) or {}
            if int(member.get("freeman") or 0):
                return False, "already", self.balance(group_id, user_id)
            ok, balance = self.add_quota(
                group_id, user_id, -max(0, price), "购买自由人", user_id
            )
            if not ok:
                return False, "poor", balance
            self.conn.execute(
                "UPDATE members SET freeman = 1, nick_nag = 0, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (_now(), group_id, user_id),
            )
            return True, "", balance

    # ------------------------------------------------------------------ 背包
    def bag_add(self, group_id: str, user_id: str, kind: str, item_id: str, n: int = 1) -> None:
        """往背包里放 n 个。已有的行直接累加，没有就插一行。"""
        with self._tx():
            self.conn.execute(
                "INSERT INTO bag (group_id, user_id, kind, item_id, count) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (group_id, user_id, kind, item_id) "
                "DO UPDATE SET count = count + ?",
                (group_id, user_id, kind, item_id, max(1, n), max(1, n)),
            )

    def bag_count(self, group_id: str, user_id: str, kind: str, item_id: str) -> int:
        row = self.conn.execute(
            "SELECT count FROM bag "
            "WHERE group_id = ? AND user_id = ? AND kind = ? AND item_id = ?",
            (group_id, user_id, kind, item_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def bag_items(self, group_id: str, user_id: str) -> list[dict[str, Any]]:
        """背包清单，count>0 的行，按 kind 再按 item_id 排，展示层不用再排。"""
        rows = self.conn.execute(
            "SELECT kind, item_id, count FROM bag "
            "WHERE group_id = ? AND user_id = ? AND count > 0 "
            "ORDER BY kind, item_id",
            (group_id, user_id),
        ).fetchall()
        return [dict(zip(("kind", "item_id", "count"), row)) for row in rows]

    def bag_buy(
        self, group_id: str, user_id: str, kind: str, item_id: str,
        price: int, action: str,
    ) -> tuple[bool, str, int]:
        """买消耗品：扣额度 → 进背包，一个事务。可重复购买（囤货合法）。"""
        self.ensure_member(group_id, user_id)
        with self._tx():
            ok, balance = self.add_quota(
                group_id, user_id, -max(0, price), action, user_id
            )
            if not ok:
                return False, "poor", balance
            self.conn.execute(
                "INSERT INTO bag (group_id, user_id, kind, item_id, count) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT (group_id, user_id, kind, item_id) "
                "DO UPDATE SET count = count + 1",
                (group_id, user_id, kind, item_id),
            )
            return True, "", balance

    def bag_take(self, group_id: str, user_id: str, kind: str, item_id: str) -> bool:
        """从背包里消耗一个。没有（或已用光）返回 False，调用方据此拒绝。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT count FROM bag "
                "WHERE group_id = ? AND user_id = ? AND kind = ? AND item_id = ?",
                (group_id, user_id, kind, item_id),
            ).fetchone()
            if not row or int(row[0]) < 1:
                return False
            if int(row[0]) == 1:
                self.conn.execute(
                    "DELETE FROM bag "
                    "WHERE group_id = ? AND user_id = ? AND kind = ? AND item_id = ?",
                    (group_id, user_id, kind, item_id),
                )
            else:
                self.conn.execute(
                    "UPDATE bag SET count = count - 1 "
                    "WHERE group_id = ? AND user_id = ? AND kind = ? AND item_id = ?",
                    (group_id, user_id, kind, item_id),
                )
            return True

    # ------------------------------------------------------------------ 关系
    def set_relation(
        self, group_id: str, user_a: str, user_b: str, relation: str
    ) -> None:
        """结缔关系：双方各写一份，互相指向对方。"""
        # 两个人都得先有档案，否则 INSERT 会因 NOT NULL 列缺默认值之外的
        # 问题失败 —— ensure_member 是建档的唯一入口，别在这里写第二条。
        self.ensure_member(group_id, user_a)
        self.ensure_member(group_id, user_b)
        now = _now()
        with self._tx():
            for owner, partner in ((user_a, user_b), (user_b, user_a)):
                self.conn.execute(
                    "INSERT INTO members (group_id, user_id, relation, relation_with, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (group_id, user_id) DO UPDATE SET "
                    "relation = ?, relation_with = ?, updated_at = ?",
                    (group_id, owner, relation, partner, now,
                     relation, partner, now),
                )

    def clear_relation(self, group_id: str, user_id: str) -> str | None:
        """解除关系：清掉自己这半边，再按记下的 QQ 号清掉对方那半边。

        返回前任的 QQ 号（没有关系时返回 None），展示层用它报名字。
        """
        member = self.get_member(group_id, user_id) or {}
        partner = str(member.get("relation_with") or "") or None
        with self._tx():
            self.conn.execute(
                "UPDATE members SET relation = '', relation_with = '', updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (_now(), group_id, user_id),
            )
            if partner:
                self.conn.execute(
                    "UPDATE members SET relation = '', relation_with = '', updated_at = ? "
                    "WHERE group_id = ? AND user_id = ? AND relation_with = ?",
                    (_now(), group_id, partner, user_id),
                )
        return partner

    # ------------------------------------------------------------------ 发言
    def bump_message(self, group_id: str, user_id: str, draw_per: int) -> dict[str, Any]:
        """+1 条发言并结算抽奖进度（三步写在一个事务里）。

        返回 {today, month, total, draws_gained, draws, msg_progress}。
        """
        today = _today()
        self.ensure_member(group_id, user_id)  # 保证行存在，否则下面的 UPDATE 会打空
        with self._tx():
            self.conn.execute(
                """
                INSERT INTO daily_stats (group_id, user_id, stat_date, msg_count) VALUES (?, ?, ?, 1)
                ON CONFLICT (group_id, user_id, stat_date) DO UPDATE SET msg_count = msg_count + 1
                """,
                (group_id, user_id, today),
            )
            member = self.get_member(group_id, user_id) or {}
            progress = int(member.get("msg_progress") or 0) + 1
            draws = int(member.get("draws") or 0)
            gained = 0
            while progress >= draw_per:  # while 兜住 draw_per 被调小后跨多档
                progress -= draw_per
                draws += 1
                gained += 1
            self.conn.execute(
                "UPDATE members SET total_messages = total_messages + 1, msg_progress = ?, "
                "draws = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (progress, draws, _now(), group_id, user_id),
            )

        return {
            "today": self._today_count(group_id, user_id, today),
            "month": self._month_count(group_id, user_id, today),
            "total": int((member.get("total_messages") or 0)) + 1,
            "draws_gained": gained,
            "draws": draws,
            "msg_progress": progress,
        }

    def _today_count(self, group_id: str, user_id: str, today: str | None = None) -> int:
        return int(
            self._scalar(
                "SELECT msg_count FROM daily_stats WHERE group_id = ? AND user_id = ? AND stat_date = ?",
                (group_id, user_id, today or _today()),
            )
            or 0
        )

    def _month_count(self, group_id: str, user_id: str, today: str | None = None) -> int:
        prefix = (today or _today())[:7]
        return int(
            self._scalar(
                "SELECT COALESCE(SUM(msg_count), 0) FROM daily_stats "
                "WHERE group_id = ? AND user_id = ? AND stat_date LIKE ?",
                (group_id, user_id, f"{prefix}%"),
            )
            or 0
        )

    def counts(self, group_id: str, user_id: str) -> dict[str, int]:
        member = self.get_member(group_id, user_id) or {}
        return {
            "today": self._today_count(group_id, user_id),
            "month": self._month_count(group_id, user_id),
            "total": int(member.get("total_messages") or 0),
        }

    # ------------------------------------------------------------------ 额度
    def _log_quota(
        self, group_id: str, user_id: str, amount: int, reason: str, operator_id: str
    ) -> None:
        """写额度流水（调用方负责事务）。"""
        self.conn.execute(
            "INSERT INTO quota_logs (group_id, user_id, amount, reason, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, user_id, amount, reason, operator_id, _now()),
        )

    def balance(self, group_id: str, user_id: str) -> int:
        """当前余额（读不到按 0）。"""
        return int(
            self._scalar(
                "SELECT quota FROM members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            or 0
        )

    def add_quota(
        self,
        group_id: str,
        user_id: str,
        amount: int,
        reason: str,
        operator_id: str = "system",
        allow_negative: bool = False,
    ) -> tuple[bool, int]:
        """加减额度。返回 (是否成功, 变更后余额)。

        两个约束都在同一处保证：

        1. 「改余额」和「记流水」在同一个事务里，不会只改一半。
        2. 「够不够扣」的判定塞进 UPDATE 的 WHERE 里，由 SQLite 原子完成。
           如果先 SELECT 余额、判断、再 UPDATE，两个并发的扣款会各自读到
           同一个旧余额、各自判定通过，最后把余额扣成负数
           （余额 10，两笔 -8 同时到 → -6）。
        """
        self.ensure_member(group_id, user_id)
        with self._tx():
            if amount < 0 and not allow_negative:
                cur = self.conn.execute(
                    "UPDATE members SET quota = quota + ?, updated_at = ? "
                    "WHERE group_id = ? AND user_id = ? AND quota + ? >= 0",
                    (amount, _now(), group_id, user_id, amount),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE members SET quota = quota + ?, updated_at = ? "
                    "WHERE group_id = ? AND user_id = ?",
                    (amount, _now(), group_id, user_id),
                )
            if not cur.rowcount:  # 条件没满足 = 余额不足
                return False, self.balance(group_id, user_id)
            self._log_quota(group_id, user_id, amount, reason, operator_id)
            return True, self.balance(group_id, user_id)

    def consume_draw(self, group_id: str, user_id: str) -> tuple[bool, int]:
        """消耗一次抽奖机会。返回 (是否成功, 剩余次数)。

        和 add_quota 同理：递减走 `draws > 0` 的条件 UPDATE。
        先读后写的话，两次并发抽奖会各自看到 draws=1，抽完变成 -1。
        """
        self.ensure_member(group_id, user_id)
        with self._tx():
            cur = self.conn.execute(
                "UPDATE members SET draws = draws - 1, updated_at = ? "
                "WHERE group_id = ? AND user_id = ? AND draws > 0",
                (_now(), group_id, user_id),
            )
            if not cur.rowcount:
                return False, 0
            row = self.conn.execute(
                "SELECT draws FROM members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            return True, int(row[0]) if row else 0

    def quota_history(
        self, group_id: str, user_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT amount, reason, created_at FROM quota_logs "
            "WHERE group_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (group_id, user_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------ 审计
    def log_op(
        self, group_id: str, operator_id: str, action: str, target_id: str = "", detail: str = ""
    ) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO op_logs (group_id, operator_id, action, target_id, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, operator_id, action, target_id, detail, _now()),
            )

    # ------------------------------------------------------------------ 维护
    def prune(self, log_retention_days: int = 180, stats_retention_days: int = 730) -> dict[str, int]:
        """按保留期裁剪流水与按天统计。

        - 累计发言数存在 members.total_messages 上，不受裁剪影响。
        - 「本月/今日」永远在保留期内，不受影响。
        - 传 0 表示不裁剪。
        """
        removed: dict[str, int] = {}
        with self._tx():
            if log_retention_days > 0:
                cutoff = (cn_now() - timedelta(days=log_retention_days)).strftime("%Y-%m-%d")
                removed["quota_logs"] = self.conn.execute(
                    "DELETE FROM quota_logs WHERE created_at < ?", (cutoff,)
                ).rowcount
                removed["op_logs"] = self.conn.execute(
                    "DELETE FROM op_logs WHERE created_at < ?", (cutoff,)
                ).rowcount
            if stats_retention_days > 0:
                cutoff = (cn_now().date() - timedelta(days=stats_retention_days)).isoformat()
                removed["daily_stats"] = self.conn.execute(
                    "DELETE FROM daily_stats WHERE stat_date < ?", (cutoff,)
                ).rowcount
        return removed

    def optimize(self) -> None:
        """让 SQLite 刷新查询计划统计。

        原来这里写的是 `VACUUM`，但那是个重操作：会重写整个库文件、期间独占锁，
        在机器人长期在线的场景下每天跑一次并不合适（大群流水表到几十万行时能卡住几秒，
        正好卡在群消息上）。真正需要的是 `PRAGMA optimize`——它只更新索引统计信息，
        由 SQLite 自己判断哪些表需要分析，毫秒级，可以跟着每日裁剪一起跑。

        空间回收交给 SQLite 的自动行为即可（WAL 模式下 checkpoint 会复用页）；
        真需要 VACUUM 是运维动作，不该进热路径。
        """
        with self._lock:
            self.conn.execute("PRAGMA optimize")

    def backup_to(self, path: Path) -> Path:
        """在线备份（不锁库，安全）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(path))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        return path

    # ------------------------------------------------------------------ 签到
    def get_checkin(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM checkins WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return self._row(cur.fetchone())

    def today_record(
        self, group_id: str, user_id: str, date_key: str | None = None
    ) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM checkin_records "
            "WHERE group_id = ? AND user_id = ? AND date_key = ?",
            (group_id, user_id, date_key or _today()),
        )
        return self._row(cur.fetchone())

    def recent_records(
        self, group_id: str, user_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM checkin_records WHERE group_id = ? AND user_id = ? "
            "ORDER BY date_key DESC LIMIT ?",
            (group_id, user_id, max(1, limit)),
        )
        return [dict(row) for row in cur.fetchall()]

    def checkin_ranking(
        self, group_id: str, by: str = "streak", limit: int = 10
    ) -> list[dict[str, Any]]:
        """群内排行榜。`by` 只可能是两个值，所以拼进 SQL 是安全的。"""
        column = "streak_days" if by == "streak" else "total_days"
        cur = self.conn.execute(
            f"SELECT c.user_id, c.total_days, c.streak_days, c.best_streak, "
            f"       m.display_name, m.qq_name "
            f"FROM checkins c "
            f"LEFT JOIN members m ON m.group_id = c.group_id AND m.user_id = c.user_id "
            f"WHERE c.group_id = ? AND c.{column} > 0 "
            f"ORDER BY c.{column} DESC, c.total_days DESC, c.user_id ASC LIMIT ?",
            (group_id, max(1, limit)),
        )
        return [dict(row) for row in cur.fetchall()]

    def quota_ranking(self, group_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """群内额度排行。只排余额 > 0 的人。

        排序用 `quota DESC, total_messages DESC` 而不是只看余额：
        余额相同的情况在早期很常见（大家都刚领了新人礼），
        这时按发言数排，榜上不会出现一串「并列第一」看起来像坏了。
        """
        # 连接是 check_same_thread=False 的，而这两张榜都跑在 asyncio.to_thread
        # 里 —— 也就是真的在别的线程上读同一个连接。必须自己上锁。
        with self._lock:
            cur = self.conn.execute(
                "SELECT m.user_id, m.quota, m.total_messages, m.card_skin, "
                "       m.display_name, m.qq_name, "
                "       c.title_id, c.streak_days, c.total_days "
                "FROM members m "
                "LEFT JOIN checkins c ON c.group_id = m.group_id AND c.user_id = m.user_id "
                "WHERE m.group_id = ? AND m.quota > 0 "
                "ORDER BY m.quota DESC, m.total_messages DESC, m.user_id ASC LIMIT ?",
                (group_id, max(1, int(limit))),
            )
            return [dict(row) for row in cur.fetchall()]

    def message_ranking(self, group_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """群内发言排行。只排真正说过话的人。

        `total_messages` 是累计值，不受按天统计的保留期裁剪 —— 所以这张榜
        和「今日发言」不是一回事：一个人上个月刷了两千条、这个月没说话，
        他仍然在榜上。要的正是这个效果（榜是资历，不是日报）。

        排序补了 ``quota DESC``：发言数相同时按额度排，早期大家都只有
        几十条的时候不会出现一串并列。
        """
        with self._lock:
            cur = self.conn.execute(
                "SELECT m.user_id, m.total_messages, m.quota, m.card_skin, "
                "       m.display_name, m.qq_name, "
                "       c.title_id, c.streak_days, c.total_days "
                "FROM members m "
                "LEFT JOIN checkins c ON c.group_id = m.group_id AND c.user_id = m.user_id "
                "WHERE m.group_id = ? AND m.total_messages > 0 "
                "ORDER BY m.total_messages DESC, m.quota DESC, m.user_id ASC LIMIT ?",
                (group_id, max(1, int(limit))),
            )
            return [dict(row) for row in cur.fetchall()]

    def perform_checkin(
        self,
        group_id: str,
        user_id: str,
        date_key: str,
        reward_fn: Callable[[int, int], tuple[int, int, int]],
        note: str = "",
    ) -> dict[str, Any]:
        """原子完成一次签到。返回快照 dict，已签过时带 `already=True`。

        `reward_fn(连签后天数, 累计后天数)` 返回 `(基础, 连签加成, 里程碑加成)`。
        把算法当参数传进来，是为了让「奖励规则」留在业务层，
        而「连签推算 + 入账 + 落快照」这三件必须一起成立的事留在事务里。

        并发安全靠两层：
        1. `_tx` 的进程内写锁 —— 同一个进程里两次点击天然串行；
        2. `checkin_records` 的 (group, user, date) 主键 —— 就算真并发进到
           事务里，第二条 INSERT 会被 `INSERT OR IGNORE` 挡下，rowcount 为 0
           就立刻退出，绝不重复发额度。
        """
        self.ensure_member(group_id, user_id)
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM checkins WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            state = dict(row) if row else {}
            last = str(state.get("last_date") or "")
            streak = int(state.get("streak_days") or 0)
            total = int(state.get("total_days") or 0)
            best = int(state.get("best_streak") or 0)
            double_pending = int(state.get("double_pending") or 0)

            if last == date_key:
                # 今天签过了。重发卡片要读到和第一次一模一样的数字，
                # 所以这里只查快照、绝不重新算奖励。
                snapshot = self.today_record(group_id, user_id, date_key)
                if not snapshot:
                    # 状态说今天签过，快照却没了（只可能是手工改过库）。
                    # 不再补发奖励 —— 宁可少给，不能重复给。
                    snapshot = {
                        "date_key": date_key,
                        "base_quota": 0,
                        "streak_bonus": 0,
                        "milestone_bonus": 0,
                        "reward": 0,
                        "doubled": 0,
                        "streak_after": streak,
                        "total_after": total,
                        "note": note,
                    }
                snapshot["already"] = True
                snapshot["balance"] = self.balance(group_id, user_id)
                return snapshot
            elif last and last == _day_before(date_key):
                streak_after, total_after = streak + 1, total + 1
            else:
                # 从没签过，或者断签了：连签归 1，累计继续累加。
                streak_after, total_after = 1, total + 1

            base, streak_bonus, milestone_bonus = reward_fn(streak_after, total_after)
            reward = base + streak_bonus + milestone_bonus
            doubled = 0
            if double_pending:
                reward *= 2
                doubled = 1
            balance_after = self.balance(group_id, user_id) + reward
            best_after = max(best, streak_after)

            cur = self.conn.execute(
                "INSERT OR IGNORE INTO checkin_records "
                "(group_id, user_id, date_key, base_quota, streak_bonus, milestone_bonus, "
                " reward, doubled, streak_after, total_after, balance_after, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    user_id,
                    date_key,
                    base,
                    streak_bonus,
                    milestone_bonus,
                    reward,
                    doubled,
                    streak_after,
                    total_after,
                    balance_after,
                    note,
                    _now(),
                ),
            )
            if not cur.rowcount:
                snapshot = self.today_record(group_id, user_id, date_key) or {}
                snapshot["already"] = True
                snapshot["balance"] = self.balance(group_id, user_id)
                return snapshot

            self.conn.execute(
                "INSERT INTO checkins "
                "(group_id, user_id, total_days, streak_days, best_streak, last_date, "
                " title_id, double_pending, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?) "
                "ON CONFLICT (group_id, user_id) DO UPDATE SET "
                " total_days = excluded.total_days, streak_days = excluded.streak_days, "
                " best_streak = excluded.best_streak, last_date = excluded.last_date, "
                " double_pending = 0, updated_at = excluded.updated_at",
                (
                    group_id,
                    user_id,
                    total_after,
                    streak_after,
                    best_after,
                    date_key,
                    str(state.get("title_id") or ""),
                    _now(),
                    _now(),
                ),
            )
            # 走 add_quota 而不是直接 UPDATE：流水表是「额度怎么没的」唯一凭据。
            # 双倍卡在上面的 upsert 里已经连同 double_pending 一起清掉了。
            self.add_quota(group_id, user_id, reward, "签到", "system")

            return {
                "date_key": date_key,
                "base_quota": base,
                "streak_bonus": streak_bonus,
                "milestone_bonus": milestone_bonus,
                "reward": reward,
                "doubled": doubled,
                "streak_after": streak_after,
                "total_after": total_after,
                "balance": balance_after,
                "note": note,
                "already": False,
            }

    # ------------------------------------------------------------------ 商店
    def equip_title(self, group_id: str, user_id: str, title_id: str) -> str:
        """挂头衔。没签到过的人也能挂 —— 顺手补一行零值状态。"""
        self.ensure_member(group_id, user_id)
        with self._tx():
            self.conn.execute(
                "INSERT INTO checkins "
                "(group_id, user_id, total_days, streak_days, best_streak, last_date, "
                " title_id, double_pending, created_at, updated_at) "
                "VALUES (?, ?, 0, 0, 0, '', ?, 0, ?, ?) "
                "ON CONFLICT (group_id, user_id) DO UPDATE SET "
                " title_id = excluded.title_id, updated_at = excluded.updated_at",
                (group_id, user_id, title_id, _now(), _now()),
            )
        return title_id

    # ---------------------------------------------------------- 名片皮肤
    def owned_skins(self, group_id: str, user_id: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT skin FROM member_skins WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return {str(row[0]) for row in cur.fetchall()}

    def equip_skin(self, group_id: str, user_id: str, skin: str) -> str:
        """换名片。`skin` 为空表示还原成默认皮肤。

        不校验「有没有买过」—— 校验在业务层做，因为「管理员想给别人换一张」
        是合理需求，而 SQL 层不知道谁是管理员。
        """
        self.ensure_member(group_id, user_id)
        with self._tx():
            self.conn.execute(
                "UPDATE members SET card_skin = ?, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (str(skin or ""), _now(), group_id, user_id),
            )
        return str(skin or "")

    def buy_skin(self, group_id: str, user_id: str, skin: str, price: int) -> tuple[bool, str, int]:
        """买名片：查重 → 扣额度 → 记账，一个事务。返回 (成功, 原因, 余额)。"""
        self.ensure_member(group_id, user_id)
        with self._tx():
            if skin in self.owned_skins(group_id, user_id):
                return False, "already", self.balance(group_id, user_id)
            ok, balance = self.add_quota(
                group_id, user_id, -max(0, price), "购买名片", user_id
            )
            if not ok:
                return False, "poor", balance
            self.conn.execute(
                "INSERT OR IGNORE INTO member_skins "
                "(group_id, user_id, skin, acquired_at) VALUES (?, ?, ?, ?)",
                (group_id, user_id, skin, _now()),
            )
            return True, "", balance

    def _take_shop_slot(        self, group_id: str, user_id: str, item_id: str, buy_date: str, limit: int
    ) -> bool:
        """占一个当日购买名额。limit <= 0 表示不限购。调用方负责事务。"""
        if limit <= 0:
            return True
        cur = self.conn.execute(
            "INSERT INTO shop_purchases (group_id, user_id, item_id, buy_date, count) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT (group_id, user_id, item_id, buy_date) DO UPDATE SET "
            " count = count + 1 WHERE shop_purchases.count < ?",
            (group_id, user_id, item_id, buy_date, limit),
        )
        return bool(cur.rowcount)

    def shop_count(
        self, group_id: str, user_id: str, item_id: str, buy_date: str | None = None
    ) -> int:
        return int(
            self._scalar(
                "SELECT count FROM shop_purchases "
                "WHERE group_id = ? AND user_id = ? AND item_id = ? AND buy_date = ?",
                (group_id, user_id, item_id, buy_date or _today()),
            )
            or 0
        )

    def add_draws(self, group_id: str, user_id: str, amount: int = 1) -> int:
        """加抽奖次数。返回加完之后的次数。"""
        self.ensure_member(group_id, user_id)
        with self._tx():
            self.conn.execute(
                "UPDATE members SET draws = draws + ?, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (amount, _now(), group_id, user_id),
            )
            return int(
                self._scalar(
                    "SELECT draws FROM members WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                )
                or 0
            )

    def buy_double(
        self, group_id: str, user_id: str, price: int, buy_date: str, limit: int
    ) -> tuple[bool, str, int]:
        """买双倍卡。已有未用的卡时不允许再买 —— 否则等于把额度提前变现。"""
        self.ensure_member(group_id, user_id)
        with self._tx():
            state = self.get_checkin(group_id, user_id) or {}
            if int(state.get("double_pending") or 0):
                return False, "pending", self.balance(group_id, user_id)
            # 先验余额再占名额：反过来的话，钱不够的那一次已经把当日名额烧掉了，
            # 用户看到「余额不足」，再攒够钱回来发现今天已经买不了了。
            if self.balance(group_id, user_id) < max(0, price):
                return False, "poor", self.balance(group_id, user_id)
            if not self._take_shop_slot(group_id, user_id, "double", buy_date, limit):
                return False, "limit", self.balance(group_id, user_id)
            ok, balance = self.add_quota(
                group_id, user_id, -max(0, price), "购买双倍卡", user_id
            )
            if not ok:  # 理论上到不了这里；真到了就整体回滚，不能留下半截状态
                raise RuntimeError("扣款失败，交易回滚")
            self.conn.execute(
                "INSERT INTO checkins "
                "(group_id, user_id, total_days, streak_days, best_streak, last_date, "
                " title_id, double_pending, created_at, updated_at) "
                "VALUES (?, ?, 0, 0, 0, '', '', 1, ?, ?) "
                "ON CONFLICT (group_id, user_id) DO UPDATE SET "
                " double_pending = 1, updated_at = excluded.updated_at",
                (group_id, user_id, _now(), _now()),
            )
            return True, "", balance

    def buy_ticket(
        self, group_id: str, user_id: str, price: int, buy_date: str, limit: int
    ) -> tuple[bool, str, int, int]:
        """买抽奖券。返回 (成功, 提示, 余额, 抽奖次数)。"""
        self.ensure_member(group_id, user_id)
        with self._tx():
            # 同上：先验余额，避免失败的那一次吃掉当日限购名额。
            if self.balance(group_id, user_id) < max(0, price):
                return False, "poor", self.balance(group_id, user_id), 0
            if not self._take_shop_slot(group_id, user_id, "ticket", buy_date, limit):
                return False, "limit", self.balance(group_id, user_id), 0
            ok, balance = self.add_quota(
                group_id, user_id, -max(0, price), "购买抽奖券", user_id
            )
            if not ok:
                raise RuntimeError("扣款失败，交易回滚")
            draws = self.add_draws(group_id, user_id, 1)
            return True, "", balance, draws

    # ------------------------------------------------------------------ 小工具
    def _scalar(self, sql: str, params: tuple) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # ================================================================ 管理台
    # 下面这些方法只被 Web 管理台调用，全部只读。
    #
    # 分两类写：
    # - 单群维度的查询直接走已建的 (group_id, ...) 索引；
    # - 「全群今日」这类跨群聚合没有合适的索引可用，靠 daily_stats 一天一行、
    #   checkin_records 一人一天一行的粒度把扫描量压住（万级数据仍在毫秒级）。
    #   真到了撑不住的那天再补 (date_key, group_id) 索引，现在加是过早优化。

    def web_overview(self) -> dict[str, Any]:
        """管理台顶部的六个数字。"""
        today = _today()
        with self._lock:
            return {
                "groups": int(self._scalar("SELECT COUNT(DISTINCT group_id) FROM members", ()) or 0),
                "members": int(self._scalar("SELECT COUNT(*) FROM members", ()) or 0),
                "messages_today": int(
                    self._scalar("SELECT COALESCE(SUM(msg_count), 0) FROM daily_stats WHERE stat_date = ?", (today,))
                    or 0
                ),
                "messages_total": int(
                    self._scalar("SELECT COALESCE(SUM(total_messages), 0) FROM members", ()) or 0
                ),
                "checkins_today": int(
                    self._scalar("SELECT COUNT(*) FROM checkin_records WHERE date_key = ?", (today,)) or 0
                ),
                "checkins_total": int(
                    self._scalar("SELECT COALESCE(SUM(total_days), 0) FROM checkins", ()) or 0
                ),
                "quota_total": int(self._scalar("SELECT COALESCE(SUM(quota), 0) FROM members", ()) or 0),
                "new_today": int(
                    self._scalar("SELECT COUNT(*) FROM members WHERE first_seen = ?", (today,)) or 0
                ),
                "today": today,
            }

    def web_groups(self) -> list[dict[str, Any]]:
        """每个群一行。今日发言和今日签到分两条聚合查，再在内存里拼起来。

        写成一条大 LEFT JOIN 也能出结果，但一旦某天没人说话，
        daily_stats 里根本没有那天的行，JOIN 出来是 NULL，
        排序和「有群但零活跃」的判定都会跟着变得别扭。
        """
        today = _today()
        with self._lock:
            rows = self.conn.execute(
                "SELECT group_id, COUNT(*) AS members, "
                "       COALESCE(SUM(quota), 0) AS quota, "
                "       COALESCE(SUM(total_messages), 0) AS messages, "
                "       COALESCE(SUM(CASE WHEN first_seen = ? THEN 1 ELSE 0 END), 0) AS new_today "
                "FROM members GROUP BY group_id ORDER BY messages DESC",
                (today,),
            ).fetchall()
            speaking = {
                row[0]: int(row[1] or 0)
                for row in self.conn.execute(
                    "SELECT group_id, SUM(msg_count) FROM daily_stats "
                    "WHERE stat_date = ? GROUP BY group_id",
                    (today,),
                )
            }
            signed = {
                row[0]: int(row[1] or 0)
                for row in self.conn.execute(
                    "SELECT group_id, COUNT(*) FROM checkin_records "
                    "WHERE date_key = ? GROUP BY group_id",
                    (today,),
                )
            }

        return [
            {
                "group_id": str(row["group_id"]),
                "members": int(row["members"] or 0),
                "quota": int(row["quota"] or 0),
                "messages": int(row["messages"] or 0),
                "new_today": int(row["new_today"] or 0),
                "messages_today": speaking.get(str(row["group_id"]), 0),
                "checkins_today": signed.get(str(row["group_id"]), 0),
            }
            for row in rows
        ]

    def web_trend(self, group_id: str, days: int = 7) -> list[dict[str, Any]]:
        """近 N 天发言与签到。空缺的日期要补 0，否则柱状图会把两天并成一天。"""
        days = max(1, min(30, int(days)))
        start = (cn_now().date() - timedelta(days=days - 1)).isoformat()
        with self._lock:
            speaking = {
                str(row[0]): int(row[1] or 0)
                for row in self.conn.execute(
                    "SELECT stat_date, SUM(msg_count) FROM daily_stats "
                    "WHERE group_id = ? AND stat_date >= ? GROUP BY stat_date",
                    (group_id, start),
                )
            }
            signed = {
                str(row[0]): int(row[1] or 0)
                for row in self.conn.execute(
                    "SELECT date_key, COUNT(*) FROM checkin_records "
                    "WHERE group_id = ? AND date_key >= ? GROUP BY date_key",
                    (group_id, start),
                )
            }

        marks = [
            (cn_now().date() - timedelta(days=offset)).isoformat()
            for offset in range(days - 1, -1, -1)
        ]
        return [
            {
                "date": mark,
                "messages": speaking.get(mark, 0),
                "checkins": signed.get(mark, 0),
            }
            for mark in marks
        ]

    def web_ranking(
        self, group_id: str, by: str = "streak", limit: int = 10
    ) -> list[dict[str, Any]]:
        order = "c.total_days DESC, c.streak_days DESC" if by == "total" else "c.streak_days DESC, c.total_days DESC"
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.user_id, c.total_days, c.streak_days, c.best_streak, c.title_id, "
                "       m.display_name, m.qq_name, m.quota "
                "FROM checkins c "
                "LEFT JOIN members m ON m.group_id = c.group_id AND m.user_id = c.user_id "
                f"WHERE c.group_id = ? ORDER BY {order} LIMIT ?",
                (group_id, max(1, min(100, int(limit)))),
            ).fetchall()
        return [
            {
                "user_id": str(row["user_id"]),
                "name": str(row["display_name"] or row["qq_name"] or row["user_id"]),
                "total_days": int(row["total_days"] or 0),
                "streak_days": int(row["streak_days"] or 0),
                "best_streak": int(row["best_streak"] or 0),
                "title_id": str(row["title_id"] or ""),
                "quota": int(row["quota"] or 0),
            }
            for row in rows
        ]

    def web_members(
        self, group_id: str, keyword: str = "", offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """成员分页。关键词同时匹配显示名、QQ 昵称和号码 —— 群主找人的时候
        记不清对方改成什么名字了，但通常记得号。"""
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        where = "m.group_id = ?"
        params: list[Any] = [group_id]
        word = keyword.strip()
        if word:
            where += " AND (m.display_name LIKE ? OR m.qq_name LIKE ? OR m.user_id LIKE ?)"
            like = f"%{word}%"
            params.extend([like, like, like])

        with self._lock:
            total = int(
                self._scalar(f"SELECT COUNT(*) FROM members m WHERE {where}", tuple(params)) or 0
            )
            rows = self.conn.execute(
                "SELECT m.user_id, m.display_name, m.qq_name, m.sex, m.hobby, m.quota, "
                "       m.total_messages, m.draws, m.permission_level, m.join_time, "
                "       m.first_seen, m.card_skin, m.freeman, m.nick_nag, "
                "       c.total_days, c.streak_days, c.title_id "
                "FROM members m "
                "LEFT JOIN checkins c ON c.group_id = m.group_id AND c.user_id = m.user_id "
                f"WHERE {where} ORDER BY m.total_messages DESC LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()

        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": [
                {
                    "user_id": str(row["user_id"]),
                    "name": str(row["display_name"] or row["qq_name"] or row["user_id"]),
                    "qq_name": str(row["qq_name"] or ""),
                    "sex": str(row["sex"] or ""),
                    "hobby": str(row["hobby"] or ""),
                    "quota": int(row["quota"] or 0),
                    "messages": int(row["total_messages"] or 0),
                    "draws": int(row["draws"] or 0),
                    "level": int(row["permission_level"]) if row["permission_level"] is not None else 0,
                    "join_time": int(row["join_time"]) if row["join_time"] else 0,
                    "first_seen": str(row["first_seen"] or ""),
                    "total_days": int(row["total_days"] or 0),
                    "streak_days": int(row["streak_days"] or 0),
                    "title_id": str(row["title_id"] or ""),
                    "card_skin": str(row["card_skin"] or ""),
                    "freeman": bool(row["freeman"]),
                    "nick_nag": int(row["nick_nag"] or 0),
                }
                for row in rows
            ],
        }

    def web_checkin_feed(
        self, group_id: str = "", limit: int = 40
    ) -> list[dict[str, Any]]:
        """最近的签到流水。group_id 传空表示全群。"""
        limit = max(1, min(200, int(limit)))
        sql = (
            "SELECT r.group_id, r.user_id, r.date_key, r.reward, r.doubled, "
            "       r.streak_after, r.total_after, r.created_at, r.note, "
            "       m.display_name, m.qq_name "
            "FROM checkin_records r "
            "LEFT JOIN members m ON m.group_id = r.group_id AND m.user_id = r.user_id "
        )
        params: tuple = (limit,)
        if group_id:
            sql += "WHERE r.group_id = ? "
            params = (group_id, limit)
        sql += "ORDER BY r.date_key DESC, r.created_at DESC LIMIT ?"

        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "group_id": str(row["group_id"]),
                "user_id": str(row["user_id"]),
                "name": str(row["display_name"] or row["qq_name"] or row["user_id"]),
                "date": str(row["date_key"] or ""),
                "reward": int(row["reward"] or 0),
                "doubled": bool(row["doubled"]),
                "streak": int(row["streak_after"] or 0),
                "total": int(row["total_after"] or 0),
                "created_at": str(row["created_at"] or ""),
                "note": str(row["note"] or ""),
            }
            for row in rows
        ]

    def web_quota_feed(
        self, group_id: str = "", limit: int = 40
    ) -> list[dict[str, Any]]:
        """最近的额度流水。有正有负，页面按符号上色。"""
        limit = max(1, min(200, int(limit)))
        sql = (
            "SELECT q.group_id, q.user_id, q.amount, q.reason, q.operator_id, q.created_at, "
            "       m.display_name, m.qq_name "
            "FROM quota_logs q "
            "LEFT JOIN members m ON m.group_id = q.group_id AND m.user_id = q.user_id "
        )
        params: tuple = (limit,)
        if group_id:
            sql += "WHERE q.group_id = ? "
            params = (group_id, limit)
        sql += "ORDER BY q.id DESC LIMIT ?"

        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "group_id": str(row["group_id"]),
                "user_id": str(row["user_id"]),
                "name": str(row["display_name"] or row["qq_name"] or row["user_id"]),
                "amount": int(row["amount"] or 0),
                "reason": str(row["reason"] or ""),
                "operator_id": str(row["operator_id"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
        ]

    def web_storage(self) -> dict[str, Any]:
        """数据库体积与各表行数。备份前先看这个，心里有数。"""
        tables = (
            "members",
            "daily_stats",
            "quota_logs",
            "op_logs",
            "checkins",
            "checkin_records",
            "shop_purchases",
            "member_skins",
            "bag",
        )
        with self._lock:
            counts = {
                name: int(self._scalar(f"SELECT COUNT(*) FROM {name}", ()) or 0)
                for name in tables
                if self._table_exists(name)
            }
        return {
            "db_kb": self.db_size_kb(),
            "schema_version": self.schema_version,
            "tables": counts,
        }
