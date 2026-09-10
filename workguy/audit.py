"""哈希链审计模块。

设计精髓（对标 WorkBuddy Desktop 5.5.4 的审计账本）：
每条审计记录携带前一条的哈希（prev_hash -> hash），形成一条哈希链。
任意一条记录被篡改，都会导致其自身哈希失配、且后续所有记录的 prev_hash
链条断裂 —— verify_chain() 因此返回 False。这是防篡改的操作账本，不是普通日志。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable

from .config import AUDIT_GENESIS_HASH
from .types import AuditRecord


def _encode_field(v: str | None) -> str:
    """长度前缀编码，彻底消除分隔符歧义。

    - ``None`` 编码为 ``"N"``
    - 字符串 ``v`` 编码为 ``f"{len(v)}:{v}"``

    两种编码前缀不同（``N`` 不是数字），且各自自定界，因此：
    1. ``None`` 与 ``""`` 编码结果不同（``"N"`` vs ``"0:"``），不会歧义；
    2. 字段内含任意字符（含旧实现用作分隔符的 ``|``）都不会造成跨字段的
       拼接碰撞——不同 (字段值, 字段顺序) 必然得到不同的编码串。
    """
    if v is None:
        return "N"
    return f"{len(v)}:{v}"


def _compute_hash(
    sequence: int,
    timestamp: float,
    agent: str | None,
    category: str | None,
    event_type: str | None,
    decision: str | None,
    detail: dict,
    prev_hash: str | None,
) -> str:
    """对一条记录的字段做长度前缀编码后 sha256。

    detail 以 sort_keys=True 序列化，保证跨进程 / 跨平台的确定性。
    编码顺序固定为：sequence|timestamp|agent|category|event_type|decision|detail_json|prev_hash
    其中每个字段都经 ``_encode_field`` 长度前缀化，字段间不再依赖易歧义的
    分隔符。
    """
    detail_json = json.dumps(detail, sort_keys=True, ensure_ascii=False)
    parts = [
        str(sequence),
        repr(timestamp),
        agent,
        category,
        event_type,
        decision,
        detail_json,
        prev_hash,
    ]
    payload = "".join(_encode_field(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditAnchor:
    """链锚点。

    把它存到链之外（另一个文件、远端、或数据库另一张表），就能在不信任
    存储的前提下检出两类攻击：

    - 尾部截断：删掉链尾 N 条，剩下内部仍自洽，但 ``length`` 会变少；
    - 整链重写：从创世哈希重新算一整条链，内部自洽，但 ``last_hash`` 会变。

    length: 记录条数
    last_hash: 末条记录的哈希
    """

    length: int
    last_hash: str


class AuditLog:
    """防篡改哈希链审计账本。"""

    def __init__(
        self,
        genesis_hash: str = AUDIT_GENESIS_HASH,
        time_func: Callable[[], float] = time.time,
    ) -> None:
        self._genesis_hash = genesis_hash
        self._time_func = time_func
        self._records: list[AuditRecord] = []

    def append(
        self,
        agent: str,
        category: str,
        event_type: str,
        decision: str,
        detail: dict | None = None,
    ) -> AuditRecord:
        seq = len(self._records)
        prev_hash = self._genesis_hash if seq == 0 else self._records[-1].hash
        timestamp = self._time_func()
        record = AuditRecord(
            sequence=seq,
            timestamp=timestamp,
            agent=agent,
            category=category,
            event_type=event_type,
            decision=decision,
            detail=detail if detail is not None else {},
            prev_hash=prev_hash,
            hash=_compute_hash(
                seq, timestamp, agent, category, event_type, decision,
                detail if detail is not None else {}, prev_hash,
            ),
        )
        self._records.append(record)
        return record

    def records(self) -> list[AuditRecord]:
        """返回当前所有记录的副本。"""
        return list(self._records)

    def verify_chain(self, anchor: AuditAnchor | None = None) -> bool:
        """从 genesis 开始逐条重算哈希并校验 prev_hash 链接。

        任意一条记录的字段被改动（含其 prev_hash），重算出的 hash 与存储值
        不符，或其 prev_hash 不等于上一条的 hash，即返回 False。

        能力边界（务必知晓，不要夸大）：
        哈希链只能防止"改了中间但保留旧链"的篡改——因为中间改动会使其自身
        及后续所有 prev_hash 链接断裂。它**防不住整库重写**：创世哈希
        (genesis) 是硬编码常量，攻击者只要从它重新算一整条链，内部依然自洽，
        verify_chain() 会返回 True。要检出"整链重写"或"尾部截断"，必须持有
        一个存于链之外的 ``anchor``（见 ``anchor()``）。

        :param anchor: 若提供，除内部自洽外，还校验记录条数必须等于
            ``anchor.length`` 且末条哈希必须等于 ``anchor.last_hash``；
            任一不符即返回 False。不传时仅校验内部自洽（保持旧语义）。
        """
        expected_prev = self._genesis_hash
        for rec in self._records:
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
            if len(self._records) != anchor.length:
                return False
            if self.last_hash() != anchor.last_hash:
                return False
        return True

    def anchor(self) -> AuditAnchor:
        """生成当前链的锚点。

        调用方应把它存到链之外（独立文件 / 远端 / 另一张表）。后续用
        ``verify_chain(anchor)`` 即可检出尾部截断与整链重写。
        """
        return AuditAnchor(length=len(self._records), last_hash=self.last_hash())

    def last_hash(self) -> str:
        if not self._records:
            return self._genesis_hash
        return self._records[-1].hash

    @property
    def sequence(self) -> int:
        """下一条记录应有的 sequence 值（即当前记录数）。"""
        return len(self._records)

    def export_jsonl(self) -> str:
        """导出为 JSONL 字符串，每条记录一行。"""
        return "\n".join(
            json.dumps(rec.to_dict(), ensure_ascii=False) for rec in self._records
        )

    @classmethod
    def from_jsonl(cls, data: str) -> "AuditLog":
        """从 JSONL 字符串重建审计账本。"""
        log = cls()
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            record = AuditRecord(
                sequence=d["sequence"],
                timestamp=d["timestamp"],
                agent=d["agent"],
                category=d["category"],
                event_type=d["event_type"],
                decision=d["decision"],
                detail=d["detail"],
                prev_hash=d["prev_hash"],
                hash=d["hash"],
            )
            log._records.append(record)
        return log
