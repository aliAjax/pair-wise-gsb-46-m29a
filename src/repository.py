"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


FINISHED_STATES = ("closed", "cancelled")
HANDOVER_PENDING = "pending"
HANDOVER_ACCEPTED = "accepted"
HANDOVER_DECLINED = "declined"
HANDOVER_REVOKED = "revoked"


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
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_handovers_record ON handovers(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_handovers_pending ON handovers(to_user, status);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            for name, ddl in (
                ("organization", "TEXT NOT NULL DEFAULT ''"),
                ("owner_id", "TEXT"),
                ("owner_org", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    connection.execute("ALTER TABLE records ADD COLUMN %s %s" % (name, ddl))

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _hrow(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, organization: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,organization,owner_id,owner_org,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), organization, None, "", actor_id, actor_id, now, now),
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

    def list_tasks(self, organization: Optional[str] = None, scope: str = "open", owner_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """未结束任务视图：open=全部未结束，unclaimed=本机构待认领，mine=我负责的。"""
        limit = max(1, min(int(limit), 500))
        clauses = ["state NOT IN ('closed','cancelled')"]
        params: List[Any] = []
        if organization is not None:
            clauses.append("organization=?")
            params.append(organization)
        if scope == "unclaimed":
            clauses.append("owner_id IS NULL")
        elif scope == "mine":
            clauses.append("owner_id=?")
            params.append(owner_id)
        params.append(limit)
        sql = "SELECT * FROM records WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(clauses)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

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

    # ---- 席位认领 ----

    def try_claim(self, record_id: int, user_id: str, org: str) -> Dict[str, Any]:
        """原子认领：仅当负责人仍为空时成功，防止两人同时认领同一条单。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version, owner_id FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if row["owner_id"]:
                connection.rollback()
                raise Conflict("任务刚刚已被其他调度员认领，请刷新列表")
            cursor = connection.execute(
                "UPDATE records SET owner_id=?, owner_org=?, updated_by=?, updated_at=? WHERE id=? AND owner_id IS NULL",
                (user_id, org, user_id, now, record_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise Conflict("任务刚刚已被其他调度员认领，请刷新列表")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "claim", user_id, int(row["version"]), json.dumps({"owner_id": user_id, "owner_org": org}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 换班交接 ----

    def get_handover(self, handover_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
        if row is None:
            raise NotFound("交接单不存在")
        return self._hrow(row)

    def find_pending_handover(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM handovers WHERE record_id=? AND status=? ORDER BY id DESC LIMIT 1",
                (record_id, HANDOVER_PENDING),
            ).fetchone()
        return self._hrow(row) if row else None

    def insert_handover(self, record_id: int, from_user: str, from_org: str, to_user: str, to_org: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT id FROM handovers WHERE record_id=? AND status=?",
                (record_id, HANDOVER_PENDING),
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("该任务已有待确认的交接，请等待接班人确认或先撤销")
            cursor = connection.execute(
                "INSERT INTO handovers(record_id,from_user,from_org,to_user,to_org,note,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, from_user, from_org, to_user, to_org, note, HANDOVER_PENDING, now),
            )
            handover_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "handover_start", from_user, self._version(connection, record_id),
                 json.dumps({"from": from_user, "to": to_user, "note": note}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
            connection.commit()
        return self._hrow(row)

    def resolve_handover(
        self,
        handover_id: int,
        actor_id: str,
        new_status: str,
        transfer: bool,
        audit_action: str,
        details: Dict[str, Any],
    ) -> tuple:
        """确认/拒绝/撤销交接在同一事务内完成；transfer=True时才真正移交负责人。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            handover = connection.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
            if handover is None:
                connection.rollback()
                raise NotFound("交接单不存在")
            if handover["status"] != HANDOVER_PENDING:
                connection.rollback()
                raise Conflict("交接已%s，不能重复处理" % self._handover_status_label(handover["status"]))
            row = connection.execute("SELECT version FROM records WHERE id=?", (handover["record_id"],)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(row["version"])
            if transfer:
                connection.execute(
                    "UPDATE records SET owner_id=?, owner_org=?, version=?, updated_by=?, updated_at=? WHERE id=?",
                    (handover["to_user"], handover["to_org"], version + 1, actor_id, now, handover["record_id"]),
                )
                version += 1
            connection.execute(
                "UPDATE handovers SET status=?, decided_at=? WHERE id=?",
                (new_status, now, handover_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (handover["record_id"], audit_action, actor_id, version,
                 json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            record = connection.execute("SELECT * FROM records WHERE id=?", (handover["record_id"],)).fetchone()
            updated = connection.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
            connection.commit()
        return self._row(record), self._hrow(updated)

    @staticmethod
    def _version(connection: sqlite3.Connection, record_id: int) -> int:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        return int(row["version"]) if row else 0

    @staticmethod
    def _handover_status_label(status: str) -> str:
        return {HANDOVER_ACCEPTED: "确认", HANDOVER_DECLINED: "拒绝", HANDOVER_REVOKED: "撤销"}.get(status, "处理")

    def list_handovers(
        self,
        record_id: Optional[int] = None,
        user: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        if user and direction == "incoming":
            clauses.append("to_user=?")
            params.append(user)
        elif user and direction == "outgoing":
            clauses.append("from_user=?")
            params.append(user)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM handovers"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._hrow(row) for row in rows]

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
