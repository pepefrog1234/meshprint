"""單據版面引擎(規格 §6.3)+ CJK 點陣化(§6.4):把一則訊息畫成黑白點陣圖。

工作原理
--------
每則訊息一張「票」,由上而下:

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   粗分隔線(直接畫矩形,不是「─」字元)
    #0 公共頻道              2026-08-27 14:32:05   標題列:來源(左)+ 接收時間(右)
    蜜蜂 Bee <a1b2c3>                    hops 2   寄件者列:名稱+前綴(左)+ hops/SNR(右)
    ──────────────────────────────────   細分隔線
    今晚 20:00 網路例會,7.100 MHz LSB,
    歡迎各位加入測試。                              內文:自動換行、超過 max_body_lines 截斷
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

幾何(單位都是「點」,1 點 = 1/180 吋):
- 畫布寬 = width_dots(預設 1440 = 8 吋),高度依內容決定;
- 內容區 = [left_margin_dots, width_dots - left_margin_dots - EDGE_SAFETY),
  分隔線與文字都畫在這個範圍內;左右邊界是對稱的;
- 行高 = 字型的 ascent + descent(取自 FreeType 度量),保證任何字形都不會被
  上下裁到;字都以「基線」(anchor="ls")對齊,混用備援字型時基線才一致。

點陣化:
- 畫布用 Pillow 的 mode "1"(每像素 1 bit),並把 draw.fontmode 設成 "1",
  強制 FreeType 走「單色」渲染、關閉反鋸齒——撞針只有「打/不打」兩種狀態,
  灰階邊緣在點陣機上只會變成髒點(§6.4)。
- 字級下限 24 px:180 dpi 下漢字少於 24×24 點,筆畫密的字(體、鬱、變)會糊成
  一團,這是點陣機的物理極限(config.py 會強制拉高)。

排版單位:
- 規格說以「格」排版(全形 2 格、半形 1 格)。實作上改用字型的實際 advance
  寬度逐字元量測、逐字元繪製(不用 Pillow 一次畫整行):CJK 字型下全形 = 1 em、
  半形 ≈ 0.5 em,行為等同格制;而比例字寬的西文不會被硬塞進半格造成疊字。
  換行只在字元邊界斷,不做斷詞、不做標點禁則(v1 規格如此)。
- 缺字處理交給 fonts.FontChain:逐字元沿字型鏈找 glyph,全缺者印「□」。

另外提供:
- notice():單行系統警示票(例如訊息風暴丟棄通知);
- calibration():校正頁(8 吋標尺 + 全字級樣張),給實機量邊界用;
- ascii_ticket():渲染失敗時的降級版面,不依賴任何字型檔(§7)。
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from typing import List

from PIL import Image, ImageDraw

from .fonts import FontChain
from .model import InboundMessage

log = logging.getLogger(__name__)

RULE_THICK = 3       # 粗分隔線厚度(點)≈ 0.4 mm
RULE_THIN = 1        # 細分隔線厚度(點)
GAP = 6              # 分隔線與文字的垂直間距(點)
EDGE_SAFETY = 8      # 右緣保險:字形可能略超出 advance,避免被 1440 上限裁到
TRUNCATED_MARK = "…(截斷)"


def sanitize(text: str) -> str:
    """把任意 UTF-8 文字整理成「可以安全畫出來」的字串。

    - NFC 正規化:同一個字的組合/預組形式統一,cmap 查表才對得上;
    - 換行統一成 \\n、Tab 換成 4 個空格;
    - 丟掉零寬連接符、變體選擇子、膚色修飾等「看不見但會干擾寬度計算」的碼位;
    - Emoji(U+1F000 以上的圖形碼位)一律換成「□」(§6.4 v1:點陣機印不出彩色 emoji);
    - 其他控制字元(Cc/Cf)直接刪除。
    """
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for ch in text:
        cp = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append("    ")
        elif cp == 0x200D or 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF:
            continue  # ZWJ / 變體選擇子 / 膚色修飾:丟棄
        elif 0x1F000 <= cp <= 0x1FBFF:
            out.append("□")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def wrap(chain: FontChain, px: int, text: str, max_w: float) -> List[str]:
    """逐字元量測寬度換行,不做斷詞(§6.3 v1);全形字自然佔約兩格。

    演算法:每個段落(以 \\n 分隔)從頭累加每個字元的 advance 寬度,
    加上下一個字會超過 max_w 就在這裡斷行。空段落保留成一個空行。
    量測用的寬度與 _draw_text 實際繪製的推進量完全相同(同一個快取),
    所以量出來的行寬就是畫出來的行寬,不會意外溢出右緣。
    """
    lines: List[str] = []
    for para in text.split("\n"):
        cur: List[str] = []
        cur_w = 0.0
        for ch in para:
            w = chain.advance(px, ch)
            if cur and cur_w + w > max_w + 0.001:
                lines.append("".join(cur))
                cur = [ch]
                cur_w = w
            else:
                cur.append(ch)
                cur_w += w
        lines.append("".join(cur))
    return lines


def ellipsize(chain: FontChain, px: int, s: str, max_w: float) -> str:
    """字串塞不進 max_w 時從尾端截掉並補「…」(用在標題/寄件者這種單行欄位)。"""
    if chain.measure(px, s) <= max_w:
        return s
    ell = "…"
    budget = max_w - chain.advance(px, ell)
    out: List[str] = []
    cur = 0.0
    for ch in s:
        w = chain.advance(px, ch)
        if cur + w > budget:
            break
        out.append(ch)
        cur += w
    return "".join(out) + ell


class Renderer:
    """把訊息畫成票的引擎;持有設定與字型鏈(字型與寬度快取都在 chain 裡)。"""

    def __init__(self, cfg, chain: FontChain):
        self.cfg = cfg
        self.chain = chain

    # ---- 幾何:所有座標都是「點」(1/180 吋)----

    @property
    def width(self) -> int:
        return self.cfg.printer.width_dots

    @property
    def x0(self) -> int:
        """內容區左緣。"""
        return self.cfg.printer.left_margin_dots

    @property
    def x1(self) -> int:
        """內容區右緣(不含):左右對稱邊界,再扣一點右緣保險。"""
        return self.width - self.cfg.printer.left_margin_dots - EDGE_SAFETY

    @property
    def content_w(self) -> int:
        return self.x1 - self.x0

    def _metrics(self, px: int):
        """主字型在 px 字級的 (ascent, descent);行高與基線位置都由此推算。"""
        return self.chain.primary.pil(px).getmetrics()

    def _line_h(self, px: int) -> int:
        a, d = self._metrics(px)
        return a + d

    def _ascent(self, px: int) -> int:
        return self._metrics(px)[0]

    # ---- 繪製原語 ----

    def _draw_text(self, draw, x: float, baseline: int, px: int, s: str) -> float:
        """從 x 開始、沿基線逐字元畫出 s;回傳畫完後的 x。

        逐字元畫(而非整行一次畫)是刻意的:每個字可能來自字型鏈的不同字型
        (主字型缺字時換備援字型),而且每個字的推進量要與 wrap() 的量測一致。
        anchor="ls" = 以「左、基線」為錨點,不同字型混排時基線才會對齊。
        """
        cx = float(x)
        for ch in s:
            face, actual = self.chain.face_or_replacement(ch)
            draw.text((round(cx), baseline), actual, font=face.pil(px), fill=0, anchor="ls")
            cx += self.chain.advance(px, ch)
        return cx

    def _row(self, draw, y: int, px: int, left: str, right: str) -> int:
        """畫一列「左欄 + 右對齊欄」(標題列、寄件者列);回傳下一列的 y。

        右欄先量寬、靠右畫;左欄可用寬度 = 內容寬 - 右欄寬 - 兩個空格的間隙,
        塞不下就截尾補「…」,確保兩欄永遠不會重疊。
        """
        left = sanitize(left).replace("\n", " ")
        right = sanitize(right).replace("\n", " ")
        baseline = y + self._ascent(px)
        right_w = self.chain.measure(px, right) if right else 0.0
        if right:
            self._draw_text(draw, max(self.x0, self.x1 - right_w), baseline, px, right)
        gap = self.chain.advance(px, " ") * 2
        left_max = self.content_w - (right_w + gap if right else 0.0)
        self._draw_text(draw, self.x0, baseline, px,
                        ellipsize(self.chain, px, left, left_max))
        return y + self._line_h(px)

    def _rule(self, draw, y: int, thickness: int) -> int:
        """畫一條橫跨內容區的分隔線(實心矩形);回傳線下方的 y。"""
        draw.rectangle([self.x0, y, self.x1 - 1, y + thickness - 1], fill=0)
        return y + thickness

    def _fmt_time(self, dt: datetime) -> str:
        """把接收時間轉成設定的時區與格式(預設 Asia/Taipei、YYYY-MM-DD HH:MM:SS)。"""
        r = self.cfg.render
        try:
            from zoneinfo import ZoneInfo
            if dt.tzinfo is not None:
                dt = dt.astimezone(ZoneInfo(r.timezone))
        except Exception as e:
            log.warning("時區 %s 無效,以原時間顯示:%s", r.timezone, e)
        return dt.strftime(r.time_format)

    def _body_lines(self, text: str) -> List[str]:
        """內文:清理 → 換行 → 超過 max_body_lines 就截斷並補一行「…(截斷)」。"""
        r = self.cfg.render
        lines = wrap(self.chain, r.body_px, sanitize(text), self.content_w)
        if len(lines) > r.max_body_lines:
            lines = lines[:r.max_body_lines] + [TRUNCATED_MARK]
        return lines

    # ---- 版面 ----

    def ticket(self, msg: InboundMessage) -> Image.Image:
        """一則訊息一張「票」(§6.3)。回傳 1-bit 影像,寬 = width_dots。

        流程:先算內文行數 → 由各區塊高度精確算出總高度 → 開一張全白畫布
        → 由上而下依序畫:粗線、標題列、寄件者列、細線、內文各行、粗線。
        高度公式與繪製順序必須一一對應,否則最後一條線會畫到畫布外。
        """
        r = self.cfg.render
        body = self._body_lines(msg.text)
        lh_h = self._line_h(r.header_px)
        lh_b = self._line_h(r.body_px)
        height = (RULE_THICK + GAP + lh_h * 2 + 3 + RULE_THIN + GAP
                  + lh_b * len(body) + GAP + RULE_THICK)
        img = Image.new("1", (self.width, height), 1)   # 1 = 白底
        draw = ImageDraw.Draw(img)
        draw.fontmode = "1"  # 強制關閉反鋸齒:撞針輸出必須是純 1-bit(§6.4)
        y = self._rule(draw, 0, RULE_THICK)
        y += GAP
        y = self._row(draw, y, r.header_px, msg.header_left(), self._fmt_time(msg.rx_time))
        y = self._row(draw, y, r.header_px, msg.sender_line(), msg.extra_note())
        y += 3
        y = self._rule(draw, y, RULE_THIN)
        y += GAP
        for line in body:
            baseline = y + self._ascent(r.body_px)
            self._draw_text(draw, self.x0, baseline, r.body_px, line)
            y += lh_b
        y += GAP
        self._rule(draw, y, RULE_THICK)
        return img

    def notice(self, text: str) -> Image.Image:
        """單行系統警示票(細線夾一行字),例如訊息風暴丟棄通知(§7)。"""
        px = self.cfg.render.header_px
        lh = self._line_h(px)
        img = Image.new("1", (self.width, RULE_THIN + 4 + lh + 4 + RULE_THIN), 1)
        draw = ImageDraw.Draw(img)
        draw.fontmode = "1"
        y = self._rule(draw, 0, RULE_THIN) + 4
        self._draw_text(draw, self.x0, y + self._ascent(px), px,
                        ellipsize(self.chain, px, sanitize(text).replace("\n", " "),
                                  self.content_w))
        self._rule(draw, y + lh + 4, RULE_THIN)
        return img

    def calibration(self) -> Image.Image:
        """§6.8 校正頁:寬度標尺 + 全字級樣張(T-2 對應右緣裁切雷點)。

        內容由上而下:標題、一條「刻意畫滿 0 ~ width-1」的全寬實線(檢查紙的
        左右緣有沒有被裁)、每 1/2 吋一個刻度的標尺(每吋長刻度並標數字,
        量吋距是否為 25.4 mm 即可驗證 dpi 與走紙比例)、四種字級的樣張、說明。
        先畫在一張很高的畫布上,最後依實際用到的高度裁切,省得手算高度。
        """
        r = self.cfg.render
        w = self.width
        hpx = r.header_px
        sizes = sorted({hpx, r.body_px, 32, 40})
        sample = "永體鬱變 繁體中文 かなカナ 한글 AaGgWw iIl1 0123456789"
        img = Image.new("1", (w, 2000), 1)
        draw = ImageDraw.Draw(img)
        draw.fontmode = "1"
        y = 0
        baseline = y + self._ascent(hpx)
        self._draw_text(draw, self.x0, baseline, hpx,
                        "MeshPrint 校正頁 · width_dots={}({:.1f}\")".format(w, w / 180))
        y += self._line_h(hpx) + 4
        # 全寬實線:第 0 點與第 w-1 點都要印得到(左右緣裁切檢查)
        draw.rectangle([0, y, w - 1, y + 2], fill=0)
        y += 3 + 8
        axis = y + 24
        draw.line([0, axis, w - 1, axis], fill=0)
        for x in range(0, w, 90):                    # 90 點 = 1/2 吋
            t = 24 if x % 180 == 0 else 12           # 整吋長刻度、半吋短刻度
            draw.line([x, axis - t, x, axis], fill=0)
        draw.line([w - 1, axis - 24, w - 1, axis], fill=0)   # 右緣最後一點也標出來
        y = axis + 2
        baseline = y + self._ascent(hpx)
        for inch in range(0, w // 180 + 1):
            label = str(inch)
            lw = self.chain.measure(hpx, label)
            x = min(max(inch * 180 - lw / 2, 0), w - lw)   # 數字置中於刻度,但不出界
            self._draw_text(draw, x, baseline, hpx, label)
        y += self._line_h(hpx) + 8
        for px in sizes:
            baseline = y + self._ascent(px)
            self._draw_text(draw, self.x0, baseline, px, "{}px {}".format(px, sample))
            y += self._line_h(px) + 4
        baseline = y + self._ascent(hpx)
        self._draw_text(draw, self.x0, baseline, hpx,
                        "上方實線應觸及左右可印極限;吋標間距應為 25.4 mm")
        y += self._line_h(hpx)
        return img.crop((0, 0, w, y + 2))


def ascii_ticket(msg: InboundMessage, cfg) -> Image.Image:
    """§7 渲染失敗降級版面:不依賴任何字型檔(PIL 內建點陣字),非 ASCII 以 ? 代。

    只有在正常渲染丟出例外(字型檔壞掉、被移走…)時才會用到:寧可印一張
    只有英數的醜票留下紀錄,也不要讓服務中斷或訊息無聲消失。
    """
    from PIL import ImageFont
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:  # 舊版 Pillow 無 size 參數
        font = ImageFont.load_default()

    def degrade(s: str) -> str:
        return s.encode("ascii", "replace").decode("ascii")

    width = cfg.printer.width_dots
    x0 = cfg.printer.left_margin_dots
    lines = ["[meshprint: render degraded]",
             degrade("{} | {}".format(msg.header_left(),
                                      msg.rx_time.strftime(cfg.render.time_format))),
             degrade(msg.sender_line())]
    body = msg.text.splitlines() or [""]
    lines += [degrade(l) for l in body[:cfg.render.max_body_lines]]
    lh = 26
    img = Image.new("1", (width, 3 + 8 + lh * len(lines) + 8 + 3), 1)
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"
    draw.rectangle([x0, 0, width - x0 - 1, 2], fill=0)
    y = 3 + 8
    for line in lines:
        draw.text((x0, y), line, font=font, fill=0)
        y += lh
    draw.rectangle([x0, y + 5, width - x0 - 1, y + 7], fill=0)
    return img
