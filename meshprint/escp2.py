"""ESC/P2 編碼器(規格 §6.5):把 1-bit 黑白影像變成 24-pin 點陣印表機的圖形指令。

工作原理
--------
LQ-310 是國際版機型、沒有中文字庫,所以本系統完全不用印表機的「文字模式」,
而是先把整張票在電腦端畫成黑白點陣圖(見 render.py),再用 ESC/P2 的
「位元影像」指令 ESC * 把每一個點交給印字頭去擊打。印表機在這裡退化成
「一次擊出 24 個直向點」的輸出裝置,字型、排版全由軟體掌控。

1. 帶(band):印字頭一次能擊出直向 24 根針,所以影像每 24 列切成一帶。
   印完一帶就用 ESC J 走紙 24/180 吋(剛好一帶的高度),再印下一帶,直到底。
   影像高度不是 24 的倍數時,底部補白湊齊。
2. 欄(column):每一帶內的資料是「逐欄」由左到右送的。一欄 = 直向 24 個點
   = 3 個 byte:byte0 的 bit7 是最上面那根針、byte2 的 bit0 是最下面那根針,
   bit = 1 代表「擊針(印黑點)」。
3. 指令 ESC * m nL nH d1 d2 …:m = 39 表示 24-pin、水平 180 dpi(與垂直
   180 dpi 相同,點才是正方形、字才不變形);nL + 256 × nH 是這帶要送幾欄,
   8 吋紙最多 1440 欄。
4. 省資料、省時間(§6.5):
   - 整帶全白 → 不送圖形資料,只 ESC J 走紙(空行加速);
   - 帶內左右兩端全白的欄裁掉,先用 ESC $(絕對水平定位,單位 1/60 吋 = 3 個點)
     把印字頭移到第一個黑點附近再開始送,印字頭不必空跑整行。
5. 一則訊息 = 一個「自足」的列印工作(job):開頭 ESC @ 初始化 + ESC U 1 單向列印
   (雙向列印時左右行程有機械誤差,圖形帶與帶之間會對不齊而出現鋸齒),
   結尾依設定走紙數行、決定要不要換頁(FF)。因此每個 spool 檔都能單獨送印,
   順序或重印都不會互相影響(§6.6)。

本模組是純函式:輸入 PIL 影像、輸出 bytes,完全不碰硬體。
tests/test_escp2.py 用手算的 golden bytes 與「隨機影像 → 編碼 → 獨立解碼器
→ 比對」的 roundtrip 驗證位元順序與裁切邏輯。

Pillow mode "1" 影像的位元定義:白 = 1、黑 = 0;tobytes() 每列補齊到 byte 邊界,
每個 byte 的 bit7 是最左邊的像素。編碼時要「反過來」看:黑(bit 0)才擊針。
"""
from __future__ import annotations

from PIL import Image

ESC = b"\x1b"
MAX_COLUMNS = 1440       # 8 吋 × 180 dpi,ESC * m=39 一帶最多能送的欄數
BAND_HEIGHT = 24         # 24 根針 = 一帶 24 列
LINE_FEED_180 = 30       # 一「行」= 1/6 吋 = 30/180 吋(feed_after_lines 的單位)

INIT = ESC + b"@"                 # ESC @:印表機重設為出廠狀態
UNIDIRECTIONAL = ESC + b"U\x01"   # ESC U 1:單向列印,圖形垂直對齊必要
FORM_FEED = b"\x0c"               # FF:換頁(連續紙預設不用)
BAND_FEED = ESC + b"J" + bytes([BAND_HEIGHT])  # ESC J 24:走紙 24/180 吋 = 一帶


def encode_page(img: Image.Image) -> bytes:
    """逐帶(24 列)編碼整張影像。白 = 不擊針;左右全白處裁切並以 ESC $ 定位。

    回傳的 bytes 只含圖形帶與走紙,不含 job 開頭/結尾(那是 encode_job 的事)。
    """
    if img.mode != "1":
        img = img.convert("1")
    w, h = img.size
    if w > MAX_COLUMNS:
        raise ValueError("影像寬 {} 超過 ESC/P2 上限 {}".format(w, MAX_COLUMNS))
    if h % BAND_HEIGHT:
        # 高度補齊為 24 的倍數:多出來的列填白(1 = 白),最後一帶才有完整 24 列可打包
        padded = Image.new("1", (w, h + BAND_HEIGHT - h % BAND_HEIGHT), 1)
        padded.paste(img, (0, 0))
        img = padded
        h = img.size[1]
    raw = img.tobytes()          # 每列 stride 個 byte,MSB 在左
    stride = (w + 7) // 8
    out = bytearray()
    for band in range(h // BAND_HEIGHT):
        cols = _pack_band(raw, stride, w, band)     # 這一帶的 3w 個 byte(逐欄)
        first, last = _ink_span(cols, w)             # 第一/最後一個有黑點的欄
        if first is None:
            out += BAND_FEED          # 全白帶:只走紙,不送資料(§6.5 空行加速)
            continue
        # ESC $ 的單位是 1/60 吋 = 3 個點,所以起印位置只能對到 3 的倍數;
        # 多出來的 0~2 欄空白直接包在資料裡送,位置才會精確。
        pos60 = first // 3
        start = pos60 * 3
        n = last + 1 - start          # 實際要送的欄數(含最多 2 欄前導空白)
        out += ESC + b"$" + bytes([pos60 & 0xFF, pos60 >> 8])   # 絕對水平定位
        out += ESC + b"*\x27" + bytes([n & 0xFF, n >> 8])       # m=39(0x27),n 欄
        out += cols[start * 3:(last + 1) * 3]                    # 每欄 3 bytes
        out += b"\r" + BAND_FEED      # 歸位,再走紙一帶
    return bytes(out)


def _pack_band(raw: bytes, stride: int, w: int, band: int) -> bytearray:
    """把影像的第 band 帶(24 列)重新排列成「逐欄 3 bytes」的針序資料。

    影像是「逐列」存的(一列一列、每列由左到右),印字頭要的是「逐欄」
    (每欄由上到下 24 個點),所以這裡做一次轉置:
    - 影像列 r(0~23)落在欄資料的第 r>>3 個 byte(0~2),
    - 在該 byte 內的位置是 bit (7 - r%8):r=0 是 bit7(最上針),r=7 是 bit0;
    - 影像像素 bit 為 0 代表黑 → 該針要擊(設 1)。
    """
    cols = bytearray(3 * w)
    base = band * BAND_HEIGHT
    for r in range(BAND_HEIGHT):
        off = (base + r) * stride     # 這一列在 raw 裡的起始 byte
        t = r >> 3                    # 落在欄資料的第幾個 byte(0/1/2)
        mask = 0x80 >> (r & 7)        # 落在該 byte 的哪一個 bit
        for x in range(w):
            # raw[off + x//8] 的 bit (7 - x%8) 就是像素 (x, r);0 = 黑
            if not (raw[off + (x >> 3)] >> (7 - (x & 7))) & 1:
                cols[x * 3 + t] |= mask
    return cols


def _ink_span(cols: bytearray, w: int):
    """回傳這一帶第一個與最後一個「有任何黑點」的欄索引;全白則 (None, None)。"""
    first = last = None
    for x in range(w):
        i = x * 3
        if cols[i] or cols[i + 1] or cols[i + 2]:
            if first is None:
                first = x
            last = x
    return first, last


def feed_lines(lines: int) -> bytes:
    """走紙 lines 行(每行 1/6 吋)。ESC J 的參數最大 255,超過就拆成多段。"""
    total = lines * LINE_FEED_180
    out = bytearray()
    while total > 0:
        step = min(total, 255)
        out += ESC + b"J" + bytes([step])
        total -= step
    return bytes(out)


def encode_job(img: Image.Image, printer_cfg) -> bytes:
    """單則訊息 = 一個自足的 raw job(§6.6:每個 spool 檔可獨立送印)。

    順序:初始化 → 單向列印 → 圖形帶 → 訊息間走紙 → (選配)換頁。
    """
    parts = [INIT, UNIDIRECTIONAL, encode_page(img)]
    if printer_cfg.feed_after_lines:
        parts.append(feed_lines(printer_cfg.feed_after_lines))
    if printer_cfg.form_feed:
        parts.append(FORM_FEED)
    return b"".join(parts)
