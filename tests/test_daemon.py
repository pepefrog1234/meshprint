"""handle_message 整合測試:過濾 → 去重 → 渲染 → spool(免硬體)。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meshprint import config, fonts
from meshprint.config import Config
from meshprint.daemon import Ctx, handle_message
from meshprint.dedup import DedupLRU
from meshprint.model import InboundMessage
from meshprint.render import Renderer
from meshprint.spool import Spool


@pytest.fixture(scope="module")
def chain():
    try:
        return fonts.resolve(config.RenderConfig())
    except RuntimeError:
        pytest.skip("系統無可用 CJK 字型")


@pytest.fixture
def ctx(tmp_path, chain):
    cfg = Config()
    cfg.spool.dir = str(tmp_path)
    return Ctx(cfg=cfg, rend=Renderer(cfg, chain),
               spool=Spool(tmp_path, keep_done=cfg.spool.keep_done,
                           max_pending=cfg.spool.max_pending),
               dedup=DedupLRU(256, tmp_path / "dedup.json"))


def _msg(text="測試訊息", ts=1767000000):
    return InboundMessage(kind="channel", text=text,
                          rx_time=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
                          sender_name="蜜蜂", channel_idx=0, channel_name="公共頻道",
                          msg_time=datetime.fromtimestamp(ts, timezone.utc))


def test_spooled_with_meta(ctx):
    assert handle_message(_msg(), ctx) == "spooled"
    assert ctx.spool.pending_count() == 1
    prn = ctx.spool._pending()[0]
    meta = ctx.spool._load_meta(prn)
    assert meta["kind"] == "channel" and meta["channel"] == "#0 公共頻道"
    assert prn.read_bytes().startswith(b"\x1b@\x1bU\x01")  # 自足 ESC/P2 job


def test_duplicate_dropped(ctx):
    assert handle_message(_msg(), ctx) == "spooled"
    assert handle_message(_msg(), ctx) == "duplicate"
    assert ctx.spool.pending_count() == 1


def test_filtered(ctx):
    ctx.cfg.filter.print_dm = False
    dm = InboundMessage(kind="dm", text="hi",
                        rx_time=datetime.now(timezone.utc), sender_prefix="aabbcc")
    assert handle_message(dm, ctx) == "filtered:dm-off"
    assert ctx.spool.pending_count() == 0


def test_storm_emits_notice(ctx):
    ctx.spool.max_pending = 2
    for i in range(3):
        assert handle_message(_msg(text="訊息 {}".format(i), ts=1767000000 + i), ctx) == "spooled"
    # 2 則真訊息 + 1 張風暴警示票
    metas = [ctx.spool._load_meta(p) for p in ctx.spool._pending()]
    kinds = [m.get("kind") for m in metas]
    assert kinds.count("notice") == 1
    assert len(metas) == 3
