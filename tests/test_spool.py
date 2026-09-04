from __future__ import annotations

import types

import pytest

from meshprint import spool as spoolmod
from meshprint.spool import Spool, resolve_queue


class FakeCups:
    """假 CUPS:lp 配 job id 進 not_completed;lpstat -W 列出未完成。"""

    def __init__(self, queues=("LQ310",)):
        self.queues = list(queues)
        self.next_job = 1
        self.not_completed = set()
        self.lp_fail = False
        self.lp_calls = []
        self.device_present = True
        self.printer_state = "3"       # 3=idle, 5=stopped(macOS 自動暫停)
        self.enable_calls = []
        self.uri = "usb://EPSON/LQ-310?serial=001"

    def run(self, cmd, capture_output=True, text=True, **kw):
        R = types.SimpleNamespace
        if cmd[0] == "lpoptions":
            out = "copies=1 device-uri={} printer-state={} printer-state-reasons=none".format(
                self.uri, self.printer_state)
            return R(returncode=0, stdout=out, stderr="")
        if cmd[0] == "lpinfo":
            out = "direct {}\n".format(self.uri) if self.device_present else ""
            return R(returncode=0, stdout=out, stderr="")
        if cmd[0] in ("cupsenable", "cupsaccept"):
            self.enable_calls.append(cmd[0])
            self.printer_state = "3"
            return R(returncode=0, stdout="", stderr="")
        if cmd[0] == "lp":
            self.lp_calls.append(cmd)
            if self.lp_fail:
                return R(returncode=1, stdout="", stderr="lp: boom")
            jid = "{}-{}".format(cmd[2], self.next_job)
            self.next_job += 1
            self.not_completed.add(jid)
            return R(returncode=0, stdout="request id is {} (1 file(s))\n".format(jid),
                     stderr="")
        if cmd[:2] == ["lpstat", "-W"]:
            out = "".join("{} pty 1024\n".format(j) for j in sorted(self.not_completed))
            return R(returncode=0, stdout=out, stderr="")
        if cmd[:2] == ["lpstat", "-e"]:
            return R(returncode=0, stdout="".join(q + "\n" for q in self.queues), stderr="")
        raise AssertionError("unexpected cmd: {}".format(cmd))


@pytest.fixture
def cups(monkeypatch):
    fake = FakeCups()
    monkeypatch.setattr(spoolmod.subprocess, "run", fake.run)
    return fake


def test_resolve_queue(cups, monkeypatch):
    assert resolve_queue("LQ310") == ("LQ310", "設定指定")
    assert resolve_queue("auto")[0] == "LQ310"
    cups.queues = ["A", "B"]
    q, why = resolve_queue("auto")
    assert q is None and "A, B" in why
    cups.queues = []
    assert resolve_queue("auto")[0] is None


def test_lifecycle_submit_print_complete(tmp_path, cups):
    sp = Spool(tmp_path, keep_done=200, max_pending=100)
    p1, d1 = sp.submit(b"JOB1", {"kind": "dm"})
    p2, d2 = sp.submit(b"JOB2", {"kind": "channel"})
    assert d1 == d2 == 0
    assert p1.exists() and p1.with_suffix(".json").exists()
    assert sp.pending_count() == 2

    stats = sp.tick("LQ310")
    assert stats.submitted == 2 and stats.completed == 0
    assert len(cups.lp_calls) == 2
    assert "-o" in cups.lp_calls[0] and "raw" in cups.lp_calls[0]
    assert sp.pending_count() == 2  # 已進 CUPS 但未完成 → 還在 spool

    stats = sp.tick("LQ310")
    assert stats.waiting == 2 and stats.submitted == 0  # 不重覆送印

    cups.not_completed.clear()  # 印表機印完了
    stats = sp.tick("LQ310")
    assert stats.completed == 2
    assert sp.pending_count() == 0
    assert sp.done_count() == 2


def test_lp_failure_keeps_pending(tmp_path, cups):
    sp = Spool(tmp_path)
    sp.submit(b"JOB", {})
    cups.lp_fail = True
    stats = sp.tick("LQ310")
    assert stats.failed == 1 and sp.pending_count() == 1
    cups.lp_fail = False  # 印表機恢復 → 補印(T-5)
    stats = sp.tick("LQ310")
    assert stats.submitted == 1


def test_storm_cap_drops_oldest(tmp_path, cups):
    sp = Spool(tmp_path, max_pending=3)
    dropped_total = 0
    for i in range(5):
        _, dropped = sp.submit(b"J%d" % i, {"i": i})
        dropped_total += dropped
    assert sp.pending_count() == 3
    assert dropped_total == 2
    # 留下的是最新三則
    idx = sorted(sp._load_meta(p).get("i") for p in sp._pending())
    assert idx == [2, 3, 4]
    # 警示票不受上限影響
    _, dropped = sp.submit(b"NOTICE", {"kind": "notice"}, enforce_cap=False)
    assert dropped == 0 and sp.pending_count() == 4


def test_printer_off_holds_jobs_out_of_cups(tmp_path, cups):
    """冷啟動保護:印表機關機時 job 留在 spool,不丟 CUPS(避免佇列被暫停)。"""
    sp = Spool(tmp_path)
    sp.submit(b"JOB", {})
    cups.device_present = False
    stats = sp.tick("LQ310")
    assert stats.submitted == 0 and stats.failed == 0 and stats.waiting == 1
    assert cups.lp_calls == []            # 完全沒碰 lp
    cups.device_present = True            # 印表機開機
    stats = sp.tick("LQ310")
    assert stats.submitted == 1


def test_paused_queue_auto_resumed(tmp_path, cups):
    """macOS 把佇列暫停(state=5)後,tick 會 cupsenable 自動恢復再送印。"""
    sp = Spool(tmp_path)
    sp.submit(b"JOB", {})
    cups.printer_state = "5"
    stats = sp.tick("LQ310")
    assert "cupsenable" in cups.enable_calls
    assert stats.submitted == 1


def test_expired_messages_skipped(tmp_path, cups):
    """保鮮期:超過 max_age_minutes 沒能送印的訊息直接略過(過夜關機不補印)。"""
    import os
    import time as _t
    sp = Spool(tmp_path, max_age_minutes=10)
    old, _ = sp.submit(b"OVERNIGHT", {"i": "old"})
    new, _ = sp.submit(b"FRESH", {"i": "new"})
    stale = _t.time() - 8 * 3600  # 模擬昨晚的訊息
    os.utime(old, (stale, stale))
    stats = sp.tick("LQ310")
    assert stats.expired == 1 and stats.submitted == 1
    assert not old.exists() and not old.with_suffix(".json").exists()
    metas = [sp._load_meta(p) for p in sp._pending()]
    assert [m.get("i") for m in metas] == ["new"]


def test_no_expiry_by_default(tmp_path, cups):
    import os
    import time as _t
    sp = Spool(tmp_path)  # max_age_minutes=0:照規格 §6.6 永久補印
    prn, _ = sp.submit(b"J", {})
    stale = _t.time() - 8 * 3600
    os.utime(prn, (stale, stale))
    stats = sp.tick("LQ310")
    assert stats.expired == 0 and stats.submitted == 1


def test_done_pruning(tmp_path, cups):
    sp = Spool(tmp_path, keep_done=2)
    for i in range(4):
        sp.submit(b"J", {})
    sp.tick("LQ310")
    cups.not_completed.clear()
    sp.tick("LQ310")
    assert sp.pending_count() == 0
    assert sp.done_count() == 2  # 只留最新 keep_done 份
