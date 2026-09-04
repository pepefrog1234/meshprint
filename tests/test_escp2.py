"""T-1 編碼器單元測試:golden bytes + 隨機影像 roundtrip。"""
from __future__ import annotations

import random

import pytest
from PIL import Image

from meshprint import escp2
from meshprint.config import PrinterConfig


def blank(w, h):
    return Image.new("1", (w, h), 1)


def test_pil_bit_semantics():
    # 前提檢查:mode "1" tobytes() 白=bit1、黑=bit0(編碼器依賴此行為)
    assert Image.new("1", (8, 1), 1).tobytes() == b"\xff"
    assert Image.new("1", (8, 1), 0).tobytes() == b"\x00"


def test_golden_basic_band():
    img = blank(3, 24)
    img.putpixel((0, 0), 0)
    img.putpixel((2, 23), 0)
    expected = (b"\x1b$\x00\x00"
                + b"\x1b*\x27\x03\x00"
                + bytes([0x80, 0, 0, 0, 0, 0, 0, 0, 0x01])
                + b"\r\x1bJ\x18")
    assert escp2.encode_page(img) == expected


def test_golden_blank_band_skips_graphics():
    assert escp2.encode_page(blank(10, 24)) == b"\x1bJ\x18"


def test_golden_height_padding():
    img = blank(1, 30)  # 補齊為 48 → 兩帶
    img.putpixel((0, 29), 0)  # 第二帶 r=5 → byte0 bit 0x04
    expected = (b"\x1bJ\x18"
                + b"\x1b$\x00\x00"
                + b"\x1b*\x27\x01\x00"
                + b"\x04\x00\x00"
                + b"\r\x1bJ\x18")
    assert escp2.encode_page(img) == expected


def test_golden_left_trim_and_abs_position():
    img = blank(1440, 24)
    img.putpixel((100, 0), 0)  # pos60 = 33,資料自 x=99 起(含 1 欄殘餘空白)
    expected = (b"\x1b$\x21\x00"
                + b"\x1b*\x27\x02\x00"
                + b"\x00\x00\x00\x80\x00\x00"
                + b"\r\x1bJ\x18")
    assert escp2.encode_page(img) == expected


def test_width_limit():
    with pytest.raises(ValueError):
        escp2.encode_page(blank(1441, 24))


def test_feed_lines():
    assert escp2.feed_lines(4) == b"\x1bJ\x78"          # 4 行 = 120/180"
    assert escp2.feed_lines(9) == b"\x1bJ\xff\x1bJ\x0f"  # 270 → 255 + 15


def test_encode_job_wrapping():
    cfg = PrinterConfig()
    assert escp2.encode_job(blank(3, 24), cfg) == b"\x1b@\x1bU\x01\x1bJ\x18\x1bJ\x78"
    cfg.form_feed = True
    assert escp2.encode_job(blank(3, 24), cfg).endswith(b"\x0c")
    cfg.form_feed = False
    cfg.feed_after_lines = 0
    assert escp2.encode_job(blank(3, 24), cfg) == b"\x1b@\x1bU\x01\x1bJ\x18"


def _decode_page(data: bytes, width: int):
    """獨立實作的解碼器(依印表機規格方向):回傳黑點集合與總高。"""
    black = set()
    x = y = i = 0
    while i < len(data):
        if data[i:i + 2] == b"\x1b$":
            x = (data[i + 2] + 256 * data[i + 3]) * 3
            i += 4
        elif data[i:i + 3] == b"\x1b*\x27":
            n = data[i + 3] + 256 * data[i + 4]
            i += 5
            for c in range(n):
                for t in range(3):
                    byte = data[i + c * 3 + t]
                    for b in range(8):
                        if byte & (0x80 >> b):
                            black.add((x + c, y + t * 8 + b))
            i += 3 * n
            x += n
        elif data[i:i + 2] == b"\x1bJ":
            y += data[i + 2]
            i += 3
        elif data[i] == 0x0D:
            x = 0
            i += 1
        else:
            raise AssertionError("未知位元組 @%d: %r" % (i, data[i:i + 4]))
    return black, y


def test_roundtrip_random_image():
    rng = random.Random(1234)
    w, h = 200, 70  # 高度非 24 倍數 → 測補白
    img = blank(w, h)
    for _ in range(500):
        img.putpixel((rng.randrange(w), rng.randrange(h)), 0)
    black, height = _decode_page(escp2.encode_page(img), w)
    padded_h = 72
    assert height == padded_h
    expected = {(x, y) for x in range(w) for y in range(h) if img.getpixel((x, y)) == 0}
    assert black == expected
