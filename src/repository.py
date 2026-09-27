"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .domain import Conflict, NotFound


ENDED_STATES = ("closed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handovers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    from_user TEXT NOT NULL,
                    from_org TEXT NOT NULL DEFAULT '',
                    to_user TEXT NOT NULL,
                    to_org TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_handover_pending
                    ON handovers(record_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_handover_record ON handovers(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_handover_user
                    ON handovers(from_user, to_user);
                """
            )
            # 兼容已存在的数据库：补齐席位与机构列
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            migrations = [
                ("organization", "ALTER TABLE records ADD COLUMN organization TEXT NOT NULL DEFAULT ''"),
                ("owner_id", "ALTER TABLE records ADD COLUMN owner_id TEXT"),
                ("owner_org", "ALTER TABLE records ADD COLUMN owner_org TEXT"),
                ("claimed_at", "ALTER TABLE records ADD COLUMN claimed_at TEXT"),
            ]
            for name, statement in migrations:
                if name not in columns:
                    connection.execute(statement)

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _handover_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, organization: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,organization) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now, organization),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "organization": organization}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def claim(self, record_id: int, user_id: str, user_org: str) -> Dict[str, Any]:
        """原子认领：仅当没有负责人且任务未结束时抢占成功。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, owner_id FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            cursor = connection.execute(
                "UPDATE records SET owner_id=?, owner_org=?, claimed_at=COALESCE(claimed_at, ?), updated_by=?, updated_at=? "
                "WHERE id=? AND owner_id IS NULL AND state NOT IN ('closed','cancelled')",
                (user_id, user_org, now, user_id, now, record_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                if row["state"] in ENDED_STATES:
                    raise Conflict("任务已结束，无法认领")
                raise Conflict("任务刚被其他调度员认领，同一时刻一单只归一人")
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "claimed", user_id, version, json.dumps({"owner": user_id, "organization": user_org}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def create_handover(self, record_id: int, from_user: str, from_org: str, to_user: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone() is None:
                connection.rollback()
                raise NotFound("记录不存在")
            try:
                cursor = connection.execute(
                    "INSERT INTO handovers(record_id,from_user,from_org,to_user,status,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, from_user, from_org, to_user, "pending", now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该任务已有待确认的交接，不能重复发起") from exc
            handover_id = int(cursor.lastrowid)
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "handover_started", from_user, version, json.dumps({"from_user": from_user, "to_user": to_user}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
            connection.commit()
        return self._handover_row(row)

    def confirm_handover(self, handover_id: int, record_id: int, to_user: str, to_org: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            handover = connection.execute(
                "UPDATE handovers SET status='confirmed', to_org=?, decided_at=? "
                "WHERE id=? AND record_id=? AND status='pending' AND to_user=?",
                (to_org, now, handover_id, record_id, to_user),
            )
            if handover.rowcount == 0:
                connection.rollback()
                raise Conflict("交接已被处理或取消，请刷新后重试")
            cursor = connection.execute(
                "UPDATE records SET owner_id=?, owner_org=?, claimed_at=?, updated_by=?, updated_at=? WHERE id=?",
                (to_user, to_org, now, to_user, now, record_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "handover_confirmed", to_user, version, json.dumps({"to_user": to_user, "organization": to_org}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def cancel_handover(self, record_id: int, user_id: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE handovers SET status='cancelled', decided_at=? "
                "WHERE record_id=? AND status='pending' AND from_user=?",
                (now, record_id, user_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise Conflict("没有待你确认取消的交接")
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "handover_cancelled", user_id, version, json.dumps({"by": user_id}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()

    def invalidate_pending_handover(self, record_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE handovers SET status='cancelled', decided_at=? WHERE record_id=? AND status='pending'",
                (_now(), record_id),
            )

    def pending_handover(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM handovers WHERE record_id=? AND status='pending' ORDER BY id DESC",
                (record_id,),
            ).fetchone()
        return self._handover_row(row) if row is not None else None

    def pending_handover_map(self, record_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        ids = list(record_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM handovers WHERE status='pending' AND record_id IN (%s)" % placeholders,
                ids,
            ).fetchall()
        return {int(row["record_id"]): self._handover_row(row) for row in rows}

    def list_pending_handovers(self, user_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM handovers WHERE status='pending' AND (from_user=? OR to_user=?) ORDER BY id",
                (user_id, user_id),
            ).fetchall()
        return [self._handover_row(row) for row in rows]

    def list_handovers(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM handovers WHERE record_id=? ORDER BY id",
                (record_id,),
            ).fetchall()
        return [self._handover_row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
