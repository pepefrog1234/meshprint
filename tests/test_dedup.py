from __future__ import annotations

from datetime import datetime, timezone

from meshprint.dedup import DedupLRU
from meshprint.model import InboundMessage


def _msg(text="hi", ts=1767000000):
    return InboundMessage(kind="dm", text=text,
                          rx_time=datetime.now(timezone.utc),
                          sender_prefix="a1b2c3d4e5f6",
                          msg_time=datetime.fromtimestamp(ts, timezone.utc))


def test_duplicate_detection():
    d = DedupLRU(capacity=8)
    k = DedupLRU.key_for(_msg())
    assert not d.seen_or_add(k)
    assert d.seen_or_add(k)
    # 不同文字/時間 → 不同 key
    assert DedupLRU.key_for(_msg(text="yo")) != k
    assert DedupLRU.key_for(_msg(ts=1767000001)) != k


def test_capacity_eviction():
    d = DedupLRU(capacity=3)
    keys = [DedupLRU.key_for(_msg(text=str(i))) for i in range(5)]
    for k in keys:
        d.seen_or_add(k)
    assert len(d) == 3
    assert not d.seen_or_add(keys[0])  # 已被擠出 → 視為新訊息
    assert d.seen_or_add(keys[4])      # 最新的還在


def test_persistence(tmp_path):
    path = tmp_path / "dedup.json"
    d1 = DedupLRU(capacity=8, path=path)
    k = DedupLRU.key_for(_msg())
    d1.seen_or_add(k)
    d2 = DedupLRU(capacity=8, path=path)
    assert d2.seen_or_add(k)  # 重啟後仍記得(§7)
