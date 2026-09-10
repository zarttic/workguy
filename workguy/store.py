"""持久化层：将审计链与会话落到 SQLite。

零第三方依赖，仅使用标准库 sqlite3。对标 WorkBuddy Desktop 的 workbuddy.db
（Drizzle 风格、带 deleted_at 软删除），这里做精简版。迁移用
``PRAGMA user_version`` 做版本号，是零依赖下替代 Alembic 的标准做法。

关键不变量：
- WAL 模式：兼顾并发与崩溃安全。
- 审计链落盘原子性：``append_audit`` 在单个事务内写入，并校验新记录的
  ``prev_hash`` 等于库中最后一条的 ``hash``；不匹配即回滚并抛异常。
- ``details`` / ``meta`` 字段以 JSON 序列化，读取时还原为 dict。
- 哈希算法与 ``workguy.audit._compute_hash`` 保持一致，保证与内存版互操作。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .audit import AUDIT_GENESIS_HASH, AuditAnchor, _compute_hash
from .config import AUDIT_GENESIS_HASH as _GENESIS  # 一致性别名
from .types import AuditRecord, Message

SCHEMA_VERSION = 1

# 建表语句：全程 IF NOT EXISTS，保证 migrate() 幂等。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent      TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    role         TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT,
    tokens       INTEGER NOT NULL DEFAULT 0,
    seq          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_chain (
    sequence   INTEGER PRIMARY KEY,
    timestamp  REAL NOT NULL,
    agent      TEXT NOT NULL,
    category   TEXT NOT NULL,
    event_type TEXT NOT NULL,
    decision   TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    credits       REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS extension_state (
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    meta_json  TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, name)
);
"""

# 防止误用（审计创世哈希两处来源应一致）
assert _GENESIS == AUDIT_GENESIS_HASH


class Store:
    """SQLite 持久化内核。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn = sqlite3.connect(self._path)
        # WAL：并发读写 + 崩溃安全
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self.migrate()

    # -- 迁移 -------------------------------------------------------------

    @property
    def version(self) -> int:
        """读取 PRAGMA user_version。"""
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def migrate(self) -> None:
        """幂等迁移：把库升到 SCHEMA_VERSION。

        建表用 IF NOT EXISTS，可重复调用不出错。executescript 自带提交，
        故此处不包在 ``with self._conn:`` 中以规避事务冲突。
        """
        self._conn.executescript(_SCHEMA_SQL)
        # PRAGMA 不支持参数绑定；SCHEMA_VERSION 为固定整数，格式化安全
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- 会话与消息 -------------------------------------------------------

    def create_session(self, session_id: str, agent: str, model: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, agent, model) "
                "VALUES (?, ?, ?)",
                (session_id, agent, model),
            )

    def save_message(self, session_id: str, msg: Message) -> None:
        with self._conn:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id=?",
                (session_id,),
            )
            seq = cur.fetchone()[0]
            self._conn.execute(
                "INSERT INTO messages "
                "(session_id, role, content, tool_call_id, tokens, seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, msg.role, msg.content, msg.tool_call_id, msg.tokens, seq),
            )

    def load_messages(self, session_id: str) -> list[Message]:
        rows = self._conn.execute(
            "SELECT role, content, tool_call_id, tokens FROM messages "
            "WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
        return [
            Message(
                role=r["role"],
                content=r["content"],
                tool_call_id=r["tool_call_id"],
                tokens=r["tokens"],
            )
            for r in rows
        ]

    def list_sessions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT session_id, agent, model, created_at FROM sessions "
            "WHERE deleted_at IS NULL ORDER BY created_at ASC"
        ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "agent": r["agent"],
                "model": r["model"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # -- 审计链 -----------------------------------------------------------

    def append_audit(self, rec: AuditRecord) -> None:
        """原子写入一条审计记录，并校验哈希链连续性。

        在单个事务内：若新记录的 ``prev_hash`` 不等于库中最后一条的 ``hash``
        （空库时为创世哈希），回滚并抛 ``ValueError``，防止断链/乱序写入。
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT hash FROM audit_chain ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            last_hash = row["hash"] if row else AUDIT_GENESIS_HASH

            # 1) 自身哈希完整性
            computed_self = _compute_hash(
                rec.sequence,
                rec.timestamp,
                rec.agent,
                rec.category,
                rec.event_type,
                rec.decision,
                rec.detail,
                rec.prev_hash,
            )
            if computed_self != rec.hash:
                raise ValueError("audit record hash mismatch")

            # 2) 哈希链连续性（防断链 / 乱序）
            if rec.prev_hash != last_hash:
                raise ValueError("audit chain broken: prev_hash does not link")

            self._conn.execute(
                "INSERT INTO audit_chain "
                "(sequence, timestamp, agent, category, event_type, decision, "
                " detail_json, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.sequence,
                    rec.timestamp,
                    rec.agent,
                    rec.category,
                    rec.event_type,
                    rec.decision,
                    json.dumps(rec.detail, sort_keys=True, ensure_ascii=False),
                    rec.prev_hash,
                    rec.hash,
                ),
            )

    def load_audit(self) -> list[AuditRecord]:
        rows = self._conn.execute(
            "SELECT sequence, timestamp, agent, category, event_type, decision, "
            "detail_json, prev_hash, hash FROM audit_chain ORDER BY sequence ASC"
        ).fetchall()
        return [
            AuditRecord(
                sequence=r["sequence"],
                timestamp=r["timestamp"],
                agent=r["agent"],
                category=r["category"],
                event_type=r["event_type"],
                decision=r["decision"],
                detail=json.loads(r["detail_json"]),
                prev_hash=r["prev_hash"],
                hash=r["hash"],
            )
            for r in rows
        ]

    def verify_audit_chain(self, anchor: AuditAnchor | None = None) -> bool:
        """从盘上重算整条链，语义与内存版 ``AuditLog.verify_chain`` 一致。

        :param anchor: 若提供，额外校验记录条数与末条哈希是否匹配锚点，
            可检出尾部截断与整链重写（含重新落盘的伪造链）。不传时仅校验
            内部自洽（保持旧语义）。
        """
        expected_prev = AUDIT_GENESIS_HASH
        recs = self.load_audit()
        for rec in recs:
            computed = _compute_hash(
                rec.sequence,
                rec.timestamp,
                rec.agent,
                rec.category,
                rec.event_type,
                rec.decision,
                rec.detail,
                rec.prev_hash,
            )
            if computed != rec.hash:
                return False
            if rec.prev_hash != expected_prev:
                return False
            expected_prev = rec.hash
        if anchor is not None:
            last = recs[-1].hash if recs else AUDIT_GENESIS_HASH
            if len(recs) != anchor.length:
                return False
            if last != anchor.last_hash:
                return False
        return True

    def anchor(self) -> AuditAnchor:
        """生成盘上审计链的锚点（语义同 ``AuditLog.anchor``）。

        调用方应把它存到本表之外，后续用 ``verify_audit_chain(anchor)``
        检出截断 / 重写。
        """
        recs = self.load_audit()
        last_hash = recs[-1].hash if recs else AUDIT_GENESIS_HASH
        return AuditAnchor(length=len(recs), last_hash=last_hash)

    def export_audit_jsonl(self) -> str:
        """导出为 JSONL，可被 ``AuditLog.from_jsonl`` 还原（互操作）。"""
        return "\n".join(
            json.dumps(rec.to_dict(), ensure_ascii=False) for rec in self.load_audit()
        )

    # -- 用量计费 ---------------------------------------------------------

    def record_usage(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        credits: float,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO usage "
                "(session_id, model, input_tokens, output_tokens, credits) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, model, input_tokens, output_tokens, credits),
            )

    def usage_report(self) -> dict[str, dict]:
        """按 model 汇总用量与 credits。"""
        rows = self._conn.execute(
            "SELECT model, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(credits) AS credits, "
            "COUNT(*) AS calls "
            "FROM usage GROUP BY model"
        ).fetchall()
        return {
            r["model"]: {
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "credits": r["credits"],
                "calls": r["calls"],
            }
            for r in rows
        }

    # -- 扩展状态 ---------------------------------------------------------

    def set_extension_state(
        self,
        kind: str,
        name: str,
        enabled: bool,
        meta: dict | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO extension_state (kind, name, enabled, meta_json, updated_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(kind, name) DO UPDATE SET "
                "enabled=excluded.enabled, meta_json=excluded.meta_json, "
                "updated_at=CURRENT_TIMESTAMP",
                (
                    kind,
                    name,
                    1 if enabled else 0,
                    json.dumps(meta if meta is not None else {}, ensure_ascii=False),
                ),
            )

    def get_extension_state(self, kind: str, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT kind, name, enabled, meta_json, updated_at "
            "FROM extension_state WHERE kind=? AND name=?",
            (kind, name),
        ).fetchone()
        if row is None:
            return None
        return {
            "kind": row["kind"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "meta": json.loads(row["meta_json"]),
            "updated_at": row["updated_at"],
        }
