"""持久化层 Store 的测试套件。

每个测试使用临时数据库，互不影响；测完清理。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from workguy.audit import AUDIT_GENESIS_HASH, AuditAnchor, AuditLog, _compute_hash
from workguy.store import SCHEMA_VERSION, Store
from workguy.types import AuditRecord, Message, ToolCall


def _tmp_db() -> tuple[Store, str]:
    """创建一个指向临时文件的 Store，返回 (store, path)。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # 让 sqlite 自己建文件
    return Store(path), path


class TestSchemaAndMigrate(unittest.TestCase):
    def test_auto_migrate_on_construct(self):
        store, path = _tmp_db()
        self.assertEqual(store.version, SCHEMA_VERSION)
        store.close()
        os.remove(path)

    def test_migrate_idempotent(self):
        store, path = _tmp_db()
        self.assertEqual(store.version, SCHEMA_VERSION)
        # 重复调用不应报错
        store.migrate()
        store.migrate()
        self.assertEqual(store.version, SCHEMA_VERSION)
        store.close()
        os.remove(path)


class TestSessionsAndMessages(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _tmp_db()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_session_and_message_roundtrip(self):
        self.store.create_session("s1", "cli", "default")
        msg = Message(
            role="assistant",
            content="hello",
            tool_call_id="call_1",
            tokens=42,
        )
        self.store.save_message("s1", msg)
        loaded = self.store.load_messages("s1")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].role, "assistant")
        self.assertEqual(loaded[0].content, "hello")
        self.assertEqual(loaded[0].tool_call_id, "call_1")
        self.assertEqual(loaded[0].tokens, 42)

    def test_messages_preserved_order_by_seq(self):
        self.store.create_session("s2", "cli")
        texts = ["first", "second", "third", "fourth"]
        for t in texts:
            self.store.save_message("s2", Message(role="user", content=t))
        loaded = self.store.load_messages("s2")
        self.assertEqual([m.content for m in loaded], texts)

    def test_list_sessions(self):
        self.store.create_session("s3", "router", "deepseek-v4-flash")
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "s3")
        self.assertEqual(sessions[0]["agent"], "router")
        self.assertEqual(sessions[0]["model"], "deepseek-v4-flash")
        self.assertIn("created_at", sessions[0])


class TestAuditChain(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _tmp_db()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def _seed(self, n: int) -> list[AuditRecord]:
        log = AuditLog()
        recs = [log.append("cli", "tool", "bash", "allow", {"cmd": i}) for i in range(n)]
        for r in recs:
            self.store.append_audit(r)
        return recs

    def test_chain_verifies_true(self):
        self._seed(5)
        self.assertTrue(self.store.verify_audit_chain())

    def test_tamper_detected(self):
        recs = self._seed(4)
        # 直接改盘上某条记录的 decision
        raw = sqlite3.connect(self.path)
        seq = recs[1].sequence
        raw.execute(
            "UPDATE audit_chain SET decision=? WHERE sequence=?",
            ("deny", seq),
        )
        raw.commit()
        raw.close()
        # 重新打开
        self.store.close()
        store2 = Store(self.path)
        try:
            self.assertFalse(store2.verify_audit_chain())
        finally:
            store2.close()

    def test_broken_link_rejected(self):
        recs = self._seed(3)
        last = recs[-1]
        # 构造一条 prev_hash 与链头不符、但自身哈希合法的伪造记录
        bad = AuditRecord(
            sequence=last.sequence + 1,
            timestamp=time.time(),
            agent="cli",
            category="tool",
            event_type="bash",
            decision="allow",
            detail={"cmd": 999},
            prev_hash="0" * 64,  # 与真实链头不符
            hash=_compute_hash(
                last.sequence + 1,
                time.time(),
                "cli",
                "tool",
                "bash",
                "allow",
                {"cmd": 999},
                "0" * 64,
            ),
        )
        with self.assertRaises(ValueError):
            self.store.append_audit(bad)
        # 确认未落盘
        self.assertEqual(len(self.store.load_audit()), 3)


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _tmp_db()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_usage_accumulates_by_model(self):
        self.store.record_usage("s1", "default", 100, 50, 0.3)
        self.store.record_usage("s1", "default", 200, 100, 0.6)
        self.store.record_usage("s2", "lite", 1000, 500, 0.075)
        report = self.store.usage_report()
        self.assertEqual(report["default"]["input_tokens"], 300)
        self.assertEqual(report["default"]["output_tokens"], 150)
        self.assertAlmostEqual(report["default"]["credits"], 0.9)
        self.assertEqual(report["default"]["calls"], 2)
        self.assertEqual(report["lite"]["input_tokens"], 1000)
        self.assertEqual(report["lite"]["calls"], 1)


class TestExtensionState(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _tmp_db()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_set_get_overwrite(self):
        self.store.set_extension_state("skill", "pdf", True, {"v": 1})
        st = self.store.get_extension_state("skill", "pdf")
        self.assertIsNotNone(st)
        self.assertTrue(st["enabled"])
        self.assertEqual(st["meta"], {"v": 1})

        # 覆盖更新
        self.store.set_extension_state("skill", "pdf", False, {"v": 2})
        st2 = self.store.get_extension_state("skill", "pdf")
        self.assertFalse(st2["enabled"])
        self.assertEqual(st2["meta"], {"v": 2})

        # 未设置返回 None
        self.assertIsNone(self.store.get_extension_state("connector", "github"))


class TestJsonlInterop(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _tmp_db()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_export_restored_by_auditlog(self):
        log = AuditLog()
        recs = [log.append("cli", "tool", "bash", "allow", {"i": i}) for i in range(3)]
        for r in recs:
            self.store.append_audit(r)
        jsonl = self.store.export_audit_jsonl()
        restored = AuditLog.from_jsonl(jsonl)
        self.assertTrue(restored.verify_chain())
        self.assertEqual(len(restored.records()), 3)
        # detail 应原样还原
        self.assertEqual(restored.records()[1].detail, {"i": 1})


class TestStoreAnchor(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _tmp_db()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    def _seed(self, n: int) -> list[AuditRecord]:
        log = AuditLog()
        recs = [log.append("cli", "tool", "bash", "allow", {"cmd": i}) for i in range(n)]
        for r in recs:
            self.store.append_audit(r)
        return recs

    def test_anchor_captures_length_and_last_hash(self):
        self._seed(5)
        a = self.store.anchor()
        self.assertEqual(a.length, 5)
        recs = self.store.load_audit()
        self.assertEqual(a.last_hash, recs[-1].hash)

    def test_intact_chain_passes_with_anchor(self):
        self._seed(5)
        anchor = self.store.anchor()
        self.assertTrue(self.store.verify_audit_chain(anchor))

    def test_truncation_detected_with_anchor(self):
        self._seed(5)
        anchor = self.store.anchor()
        # 直接删掉盘上尾部 2 条（模拟证据被裁掉）
        raw = sqlite3.connect(self.path)
        raw.execute("DELETE FROM audit_chain WHERE sequence >= 3")
        raw.commit()
        raw.close()
        self.assertFalse(self.store.verify_audit_chain(anchor))

    def test_full_rewrite_detected_with_anchor(self):
        self._seed(5)
        anchor = self.store.anchor()
        # 清空后用不同内容重算一整条链（条数相同，末 hash 不同）
        self.store._conn.execute("DELETE FROM audit_chain")
        self.store._conn.commit()
        fresh = AuditLog()
        for i in range(5):
            self.store.append_audit(
                fresh.append("other", "x", "y", "deny", {"j": i})
            )
        self.assertTrue(self.store.verify_audit_chain())  # 内部自洽
        self.assertFalse(self.store.verify_audit_chain(anchor))


if __name__ == "__main__":
    unittest.main()
