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
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

try:  # 本地单测时没有 astrbot，退化成标准库 logging
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_groupkit")

SCHEMA_VERSION = 1  # 结构版本；改表时 +1


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """群助手数据层。同步方法，调用方用 asyncio.to_thread 包一层。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "groupkit.db"

        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 关掉隐式事务，全部显式 BEGIN/COMMIT
            timeout=5.0,
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._in_tx = False

        # 这些 PRAGMA 必须在事务外执行
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA temp_store=MEMORY")

        self._init_db()
        logger.info(
            f"[群助手] 数据库就绪: {self.db_path} "
            f"(schema v{self.schema_version}, {self.db_size_kb()} KB)"
        )

    # ------------------------------------------------------------------ 事务
    @contextmanager
    def _tx(self) -> Iterator[None]:
        """写事务。嵌套调用复用外层，异常整体回滚。"""
        with self._lock:
            if self._in_tx:
                yield
                return
            self._in_tx = True
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            finally:
                self._in_tx = False

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.execute("PRAGMA optimize")
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass

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
        with self._tx():
            self.conn.execute(
                "UPDATE members SET display_name = ?, updated_at = ? "
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

    def set_permission(self, group_id: str, user_id: str, level: int | None) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE members SET permission_level = ?, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (level, _now(), group_id, user_id),
            )

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

        「改余额」和「记流水」在同一个事务里，不会出现只改一半。
        """
        member = self.ensure_member(group_id, user_id)
        current = int(member.get("quota") or 0)
        if amount < 0 and not allow_negative and current + amount < 0:
            return False, current
        with self._tx():
            self.conn.execute(
                "UPDATE members SET quota = quota + ?, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (amount, _now(), group_id, user_id),
            )
            self._log_quota(group_id, user_id, amount, reason, operator_id)
        return True, current + amount

    def consume_draw(self, group_id: str, user_id: str) -> tuple[bool, int]:
        """消耗一次抽奖机会。返回 (是否成功, 剩余次数)。"""
        member = self.ensure_member(group_id, user_id)
        draws = int(member.get("draws") or 0)
        if draws <= 0:
            return False, 0
        with self._tx():
            self.conn.execute(
                "UPDATE members SET draws = draws - 1, updated_at = ? "
                "WHERE group_id = ? AND user_id = ?",
                (_now(), group_id, user_id),
            )
        return True, draws - 1

    def quota_history(
        self, group_id: str, user_id: str, limit: int = 5
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
                cutoff = (datetime.now() - timedelta(days=log_retention_days)).strftime("%Y-%m-%d")
                removed["quota_logs"] = self.conn.execute(
                    "DELETE FROM quota_logs WHERE created_at < ?", (cutoff,)
                ).rowcount
                removed["op_logs"] = self.conn.execute(
                    "DELETE FROM op_logs WHERE created_at < ?", (cutoff,)
                ).rowcount
            if stats_retention_days > 0:
                cutoff = (date.today() - timedelta(days=stats_retention_days)).isoformat()
                removed["daily_stats"] = self.conn.execute(
                    "DELETE FROM daily_stats WHERE stat_date < ?", (cutoff,)
                ).rowcount
        return removed

    def vacuum(self) -> None:
        """回收空间。会短暂锁库，只在维护时调。"""
        with self._lock:
            self.conn.execute("PRAGMA optimize")
            self.conn.execute("VACUUM")

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

    # ------------------------------------------------------------------ 小工具
    def _scalar(self, sql: str, params: tuple) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]


def now_ts() -> int:
    return int(time.time())
