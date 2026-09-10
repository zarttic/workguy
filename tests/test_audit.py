"""哈希链审计模块的测试。

通过 TDD 验证防篡改审计账本的核心机制：
- 哈希链从 genesis 开始逐条链接
- 任意一条被篡改都会让 verify_chain 返回 False
- JSONL 往返保真
"""

from __future__ import annotations

import dataclasses
import unittest

from workguy.audit import AuditAnchor, AuditLog, _compute_hash
from workguy.config import AUDIT_GENESIS_HASH
from workguy.types import AuditRecord


class TestAuditAppend(unittest.TestCase):
    def test_first_record_sequence_zero_and_genesis_prev(self):
        log = AuditLog()
        rec = log.append("cli", "tool", "exec", "allow", {"x": 1})
        self.assertEqual(rec.sequence, 0)
        self.assertEqual(rec.prev_hash, AUDIT_GENESIS_HASH)
        self.assertEqual(log.sequence, 1)

    def test_sequence_increments(self):
        log = AuditLog()
        r0 = log.append("a", "c", "e", "allow")
        r1 = log.append("a", "c", "e", "deny")
        r2 = log.append("a", "c", "e", "allow")
        self.assertEqual(r0.sequence, 0)
        self.assertEqual(r1.sequence, 1)
        self.assertEqual(r2.sequence, 2)
        self.assertEqual(log.sequence, 3)

    def test_hash_is_64_hex(self):
        log = AuditLog()
        rec = log.append("cli", "tool", "exec", "allow", {"k": "v"})
        self.assertEqual(len(rec.hash), 64)
        # 能解析回 int 且不抛异常，即说明是合法十六进制串
        self.assertTrue(all(c in "0123456789abcdef" for c in rec.hash))

    def test_detail_none_stored_as_empty_dict(self):
        log = AuditLog()
        rec = log.append("cli", "tool", "exec", "allow")
        self.assertEqual(rec.detail, {})


class TestAuditChain(unittest.TestCase):
    def test_chain_intact_after_n_appends(self):
        log = AuditLog()
        for i in range(20):
            log.append("agent", "cat", "evt", "allow", {"i": i})
        self.assertTrue(log.verify_chain())

    def test_empty_log_verify_true(self):
        log = AuditLog()
        self.assertTrue(log.verify_chain())
        self.assertEqual(log.records(), [])

    def test_last_hash_equals_final_record_hash(self):
        log = AuditLog()
        r = log.append("a", "c", "e", "allow")
        self.assertEqual(log.last_hash(), r.hash)


class TestAuditTamperDetection(unittest.TestCase):
    def _build(self, n: int) -> AuditLog:
        log = AuditLog()
        for i in range(n):
            log.append("agent", "cat", f"evt{i}", "allow", {"i": i})
        return log

    def test_tamper_decision_breaks_chain(self):
        log = self._build(5)
        self.assertTrue(log.verify_chain())
        idx = 2
        original = log._records[idx]
        tampered = dataclasses.replace(original, decision="deny")
        log._records[idx] = tampered
        self.assertFalse(log.verify_chain())

    def test_tamper_detail_breaks_chain(self):
        log = self._build(5)
        self.assertTrue(log.verify_chain())
        idx = 1
        original = log._records[idx]
        tampered = dataclasses.replace(original, detail={"i": 999})
        log._records[idx] = tampered
        self.assertFalse(log.verify_chain())

    def test_tamper_prev_hash_breaks_chain(self):
        log = self._build(4)
        self.assertTrue(log.verify_chain())
        idx = 3
        original = log._records[idx]
        tampered = dataclasses.replace(original, prev_hash="0" * 63 + "1")
        log._records[idx] = tampered
        self.assertFalse(log.verify_chain())

    def test_tamper_propagates_to_all_subsequent(self):
        # 篡改第一条后，其自身及后续所有都应校验失败
        log = self._build(6)
        first = log._records[0]
        log._records[0] = dataclasses.replace(first, decision="executed")
        self.assertFalse(log.verify_chain())


class TestAuditJsonlRoundTrip(unittest.TestCase):
    def test_roundtrip_preserves_chain(self):
        log = AuditLog()
        log.append("cli", "tool", "exec", "allow", {"path": "/a", "n": 3})
        log.append("router", "route", "select", "deny", {"model": "lite"})
        log.append("cli", "permission", "check", "require_approval", {"tool": "Bash"})
        data = log.export_jsonl()
        restored = AuditLog.from_jsonl(data)
        self.assertEqual(len(restored.records()), 3)
        self.assertTrue(restored.verify_chain())
        # 内容保真
        for a, b in zip(log.records(), restored.records()):
            self.assertEqual(a.to_dict(), b.to_dict())

    def test_roundtrip_empty(self):
        log = AuditLog()
        restored = AuditLog.from_jsonl(log.export_jsonl())
        self.assertEqual(restored.records(), [])
        self.assertTrue(restored.verify_chain())

    def test_roundtrip_detail_key_order_deterministic(self):
        # detail 乱序写入，往返后 key 顺序不影响哈希一致性
        log = AuditLog()
        log.append("a", "c", "e", "allow", {"z": 1, "a": 2, "m": 3})
        restored = AuditLog.from_jsonl(log.export_jsonl())
        self.assertTrue(restored.verify_chain())


class TestAuditLengthPrefixEncoding(unittest.TestCase):
    """长度前缀编码：消除字段间分隔符歧义，且不破坏链。"""

    def test_chain_works_after_length_prefix(self):
        log = AuditLog()
        for i in range(12):
            log.append("agent", "cat", "evt", "allow", {"i": i})
        self.assertTrue(log.verify_chain())

    def test_ambiguous_fields_do_not_collide(self):
        # 旧实现下这会碰撞：agent="a|b", category="" 与 agent="a", category="b"
        # 用 "|" 拼出来都是 "a|b|"。长度前缀编码后必须不同。
        base = dict(
            sequence=0,
            timestamp=1.0,
            event_type="e",
            decision="allow",
            detail={},
            prev_hash="0" * 64,
        )
        h1 = _compute_hash(
            base["sequence"], base["timestamp"],
            "a|b", "", base["event_type"], base["decision"],
            base["detail"], base["prev_hash"],
        )
        h2 = _compute_hash(
            base["sequence"], base["timestamp"],
            "a", "b", base["event_type"], base["decision"],
            base["detail"], base["prev_hash"],
        )
        self.assertNotEqual(h1, h2)

    def test_none_and_empty_string_differ(self):
        # None 与 "" 必须编码成不同的哈希
        base = dict(
            sequence=0,
            timestamp=1.0,
            event_type="e",
            decision="allow",
            detail={},
            prev_hash="0" * 64,
        )
        h_none = _compute_hash(
            base["sequence"], base["timestamp"],
            None, "", base["event_type"], base["decision"],
            base["detail"], base["prev_hash"],
        )
        h_empty = _compute_hash(
            base["sequence"], base["timestamp"],
            "", "", base["event_type"], base["decision"],
            base["detail"], base["prev_hash"],
        )
        self.assertNotEqual(h_none, h_empty)


class TestAuditAnchor(unittest.TestCase):
    """锚点机制：检出尾部截断与整链重写。"""

    def _build(self, n: int) -> AuditLog:
        log = AuditLog()
        for i in range(n):
            log.append("agent", "cat", f"evt{i}", "allow", {"i": i})
        return log

    def test_anchor_captures_length_and_last_hash(self):
        log = self._build(5)
        a = log.anchor()
        self.assertEqual(a.length, 5)
        self.assertEqual(a.last_hash, log.last_hash())

    def test_no_anchor_truncation_still_true(self):
        # 设计边界：没有锚点时，尾删在内部仍自洽，verify_chain 返回 True
        log = self._build(5)
        self.assertTrue(log.verify_chain())
        log._records = log._records[:-2]
        self.assertEqual(len(log._records), 3)
        self.assertTrue(log.verify_chain())

    def test_truncation_detected_with_anchor(self):
        log = self._build(5)
        anchor = log.anchor()
        # 删掉尾部 2 条（模拟攻击者"删证据"）
        log._records = log._records[:-2]
        self.assertEqual(len(log._records), 3)
        self.assertFalse(log.verify_chain(anchor))

    def test_full_rewrite_detected_with_anchor(self):
        log = self._build(5)
        anchor = log.anchor()
        # 攻击者从创世哈希重算一条全新链（条数相同，但内容不同 -> 末 hash 变）
        fresh = AuditLog()
        for i in range(5):
            fresh.append("other", "x", "y", "deny", {"j": i})
        # 内部自洽，但末 hash 与锚点不符
        self.assertTrue(fresh.verify_chain())
        self.assertFalse(fresh.verify_chain(anchor))

    def test_anchor_matches_intact_chain(self):
        log = self._build(5)
        anchor = log.anchor()
        self.assertTrue(log.verify_chain(anchor))


if __name__ == "__main__":
    unittest.main()
