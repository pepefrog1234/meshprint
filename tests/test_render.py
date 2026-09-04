"""版面/點陣化測試(需系統存在任一 CJK 字型,否則跳過)。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meshprint import config, fonts
from meshprint.config import Config
from meshprint.model import InboundMessage
from meshprint.render import TRUNCATED_MARK, Renderer, ellipsize, sanitize, wrap


@pytest.fixture(scope="module")
def chain():
    try:
        return fonts.resolve(config.RenderConfig())
    except RuntimeError:
        pytest.skip("系統無可用 CJK 字型")


@pytest.fixture(scope="module")
def rend(chain):
    return Renderer(Config(), chain)


def _msg(text, **kw):
    kw.setdefault("kind", "channel")
    kw.setdefault("channel_idx", 0)
    kw.setdefault("channel_name", "公共頻道")
    kw.setdefault("sender_name", "蜜蜂 Bee")
    kw.setdefault("sender_prefix", "a1b2c3")
    return InboundMessage(text=text, rx_time=datetime(2026, 8, 27, 6, 32, 5, tzinfo=timezone.utc), **kw)


def test_sanitize():
    assert sanitize("a\r\nb\rc") == "a\nb\nc"
    assert sanitize("x\ty") == "x    y"
    assert sanitize("hi\U0001f600") == "hi\u25a1"  # Emoji -> □
    assert sanitize("a\u200db") == "ab"  # ZWJ 丟棄
    assert sanitize("e\u0301") == "\u00e9"  # NFC 正規化
    assert sanitize("a\x00\x1fb") == "ab"


def test_wrap_no_overflow_and_lossless(chain):
    text = "MeshCore 網狀網路 mixed 測試字串,abcDEF012。" * 12
    lines = wrap(chain, 28, text, 600)
    assert "".join(lines) == text
    for line in lines:
        assert chain.measure(28, line) <= 600.001
    assert len(lines) > 1


def test_wrap_keeps_blank_lines(chain):
    assert wrap(chain, 28, "a\n\nb", 600) == ["a", "", "b"]


def test_cjk_fullwidth(chain):
    a = chain.advance(28, "永")
    assert a == chain.advance(28, "體")
    assert a > chain.advance(28, "A")


def test_ellipsize(chain):
    s = "這是一個非常長的頻道名稱測試字串" * 3
    out = ellipsize(chain, 24, s, 300)
    assert out.endswith("…")
    assert chain.measure(24, out) <= 300.001
    assert ellipsize(chain, 24, "短", 300) == "短"


def test_missing_glyph_replacement(chain):
    face, actual = chain.face_or_replacement("\U0010fffd")
    assert actual == "□"


def test_ticket_geometry(rend):
    img = rend.ticket(_msg("今晚 20:00 網路例會,7.100 MHz LSB,\n歡迎各位加入測試。"))
    assert img.mode == "1"
    assert img.width == rend.width
    assert img.getextrema()[0] == 0  # 有黑點
    assert img.height > 80


def test_ticket_truncation(rend):
    lines = rend._body_lines("測\n" * 60)
    assert len(lines) == rend.cfg.render.max_body_lines + 1
    assert lines[-1] == TRUNCATED_MARK


def test_dm_header(rend):
    m = _msg("hi", kind="dm", channel_idx=None, channel_name="")
    assert m.header_left() == "私訊"
    img = rend.ticket(m)
    assert img.getextrema()[0] == 0


def test_calibration_page(rend):
    img = rend.calibration()
    assert img.mode == "1"
    assert img.width == rend.width
    assert img.getextrema()[0] == 0
    assert img.height < 2000
