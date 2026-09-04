"""字型鏈(規格 §6.4):載入字型、以 cmap 判斷缺字、沿備援鏈逐字元找 glyph。

工作原理
--------
1. 字型鏈 = 依序排列的多個字型「面」(Face)。畫每一個字元時,從主字型開始
   問「你有這個碼位的 glyph 嗎?」,第一個有的就用它;全部都沒有就印「□」
   並記一次 log。這樣繁中主字型缺的韓文、罕用字可以由後面的字型補。
2. 「有沒有這個字」不是靠畫畫看(FreeType 會畫出空白框,分不出來),而是用
   fonttools 預先讀出字型的 cmap 表(碼位 → glyph 的對照表)做成 set,
   查表 O(1)。.ttc 集合檔要指定第幾個 face 的 cmap。
3. 每個 Face 對每個字級快取一個 Pillow FreeTypeFont 物件(載入很慢),
   FontChain 再快取「每個字元選到哪個 face」與「每個 (字級, 字元) 的推進寬度」,
   所以一則票渲染時幾乎只是查表。
4. 寬度一律以 mode="1"(單色)量測,與實際單色渲染的 hinting 結果一致。
5. 設定檔的主字型不存在時,自動探索系統內可用的繁中字型(Noto → PingFang →
   Heiti TC …),並附加韓文/日文/萬用備援,讓程式在沒裝 Noto 的機器也能跑,
   但會在 log 提醒改用了哪個字型。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PIL import ImageFont

log = logging.getLogger(__name__)

REPLACEMENT = "□"  # §6.4 缺字替代字元

# 設定的主字型不存在時,依序嘗試的系統字型(macOS / Linux);第一個存在的當主字型
_AUTO_PRIMARY = [
    "~/Library/Fonts/NotoSansMonoCJKtc-Regular.otf",
    "~/Library/Fonts/NotoSansCJKtc-Regular.otf",
    "/Library/Fonts/NotoSansCJKtc-Regular.otf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-TC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
# 自動探索模式下附加的補洞字型(韓文、日文、最後防線);存在的都加進鏈尾
_AUTO_FALLBACKS = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


@dataclass(frozen=True)
class FaceSpec:
    """一個字型面的位置:檔案路徑 + (.ttc 集合檔內的)face 索引。"""
    path: str
    index: int = 0

    @staticmethod
    def parse(entry: str) -> "FaceSpec":
        """設定檔寫法:「路徑」或「路徑#索引」。"""
        path, sep, idx = entry.partition("#")
        return FaceSpec(path, int(idx) if sep else 0)


class Face:
    """已載入的單一字型面:cmap 碼位集合 + 各字級的 Pillow 字型物件快取。"""

    def __init__(self, spec: FaceSpec):
        self.spec = spec
        self.path = str(Path(spec.path).expanduser())
        self.display = "{}#{}".format(Path(self.path).name, spec.index)
        self._pil: Dict[int, ImageFont.FreeTypeFont] = {}
        self._codepoints = self._load_cmap()

    def _load_cmap(self) -> Set[int]:
        """用 fonttools 讀 cmap(lazy 模式只解析需要的表,16 MB 的 Noto 也很快)。"""
        from fontTools.ttLib import TTFont

        kwargs = {"lazy": True}
        if self.path.lower().endswith((".ttc", ".otc")):
            kwargs["fontNumber"] = self.spec.index
        tt = TTFont(self.path, **kwargs)
        try:
            return set(tt.getBestCmap())
        finally:
            tt.close()

    def pil(self, px: int) -> ImageFont.FreeTypeFont:
        """px 字級的 Pillow 字型物件(載入一次後快取)。"""
        f = self._pil.get(px)
        if f is None:
            f = ImageFont.truetype(self.path, px, index=self.spec.index)
            self._pil[px] = f
        return f

    def has(self, ch: str) -> bool:
        return ord(ch) in self._codepoints


class FontChain:
    """字型備援鏈:選字型、缺字替代、推進寬度快取。"""

    def __init__(self, faces: List[Face]):
        if not faces:
            raise RuntimeError("無法載入任何字型")
        self.faces = faces
        self._pick: Dict[str, Optional[Face]] = {}          # 字元 → 選到的 face(None = 全缺)
        self._adv: Dict[Tuple[int, str], float] = {}        # (字級, 字元) → 推進寬度
        self._warned: Set[str] = set()                      # 已記過 log 的缺字

    @property
    def primary(self) -> Face:
        """主字型:行高、基線等版面度量一律以它為準。"""
        return self.faces[0]

    def pick(self, ch: str) -> Optional[Face]:
        """沿鏈找第一個有這個字的 face;找不到回 None。結果快取。"""
        if ch in self._pick:
            return self._pick[ch]
        face = next((f for f in self.faces if f.has(ch)), None)
        self._pick[ch] = face
        return face

    def face_or_replacement(self, ch: str) -> Tuple[Face, str]:
        """回傳 (要用的 face, 實際要畫的字元):缺字時改畫「□」(連 □ 都沒有就畫 ?)。"""
        face = self.pick(ch)
        if face is not None:
            return face, ch
        if ch not in self._warned:
            self._warned.add(ch)
            log.info("缺字 U+%04X %r → %s", ord(ch), ch, REPLACEMENT)
        face = self.pick(REPLACEMENT)
        if face is not None:
            return face, REPLACEMENT
        return self.primary, "?"

    def advance(self, px: int, ch: str) -> float:
        """字元在 px 字級的推進寬度(以單色模式量,與渲染一致);快取。"""
        key = (px, ch)
        got = self._adv.get(key)
        if got is None:
            face, actual = self.face_or_replacement(ch)
            got = face.pil(px).getlength(actual, mode="1")
            self._adv[key] = got
        return got

    def measure(self, px: int, s: str) -> float:
        """整個字串的寬度 = 各字元推進量總和(與逐字元繪製完全一致)。"""
        return sum(self.advance(px, ch) for ch in s)


def resolve(render_cfg) -> FontChain:
    """依 [render] 設定建立字型鏈:主字型(或自動探索)+ 設定的備援字型。"""
    specs: List[FaceSpec] = []
    primary = Path(render_cfg.font).expanduser()
    if primary.exists():
        specs.append(FaceSpec(str(primary), render_cfg.font_index))
    else:
        found = _discover()
        if not found:
            raise RuntimeError(
                "找不到可用字型:{} 不存在,系統亦無可自動備援的 CJK 字型。"
                "請安裝 Noto Sans CJK TC 並在設定檔 [render] font 指定路徑。".format(primary))
        log.warning("設定字型 %s 不存在,自動改用 %s", primary, found[0].path)
        specs.extend(found)
    for entry in render_cfg.fallback_fonts:
        spec = FaceSpec.parse(entry)
        if Path(spec.path).expanduser().exists():
            specs.append(spec)
        else:
            log.warning("備援字型不存在,略過:%s", entry)
    faces: List[Face] = []
    for s in specs:
        try:
            faces.append(Face(s))
        except Exception as e:
            log.warning("載入字型失敗,略過 %s:%s", s.path, e)
    return FontChain(faces)


def _discover() -> List[FaceSpec]:
    """自動探索:第一個存在的候選當主字型(.ttc 要先找出繁中 face),再附加補洞字型。"""
    primary = None
    for cand in _AUTO_PRIMARY:
        p = Path(cand).expanduser()
        if not p.exists():
            continue
        if p.suffix.lower() in (".ttc", ".otc"):
            idx = _find_tc_face(p)
            if idx is None:
                continue
            primary = FaceSpec(str(p), idx)
        else:
            primary = FaceSpec(str(p))
        break
    if primary is None:
        return []
    out = [primary]
    for cand in _AUTO_FALLBACKS:
        p = Path(cand).expanduser()
        if p.exists():
            out.append(FaceSpec(str(p), 0))
    return out


def _find_tc_face(path: Path):
    """在 .ttc 集合檔中找繁中(TC)face;偏好 Regular,其次 Medium。

    .ttc 一個檔案包多個字型(例如 STHeiti Medium.ttc = Heiti TC + Heiti SC),
    用每個 face 的 name 表全名(nameID 4)判斷是不是繁中版。
    """
    from fontTools.ttLib import TTCollection

    try:
        coll = TTCollection(str(path), lazy=True)
    except Exception as e:
        log.warning("讀取 %s 失敗:%s", path, e)
        return None
    best = None
    best_score = -1
    for i, tt in enumerate(coll.fonts):
        try:
            name = tt["name"].getDebugName(4) or ""
        except Exception:
            continue
        low = name.lower()
        if " tc" not in low and "traditional" not in low:
            continue
        score = 0
        if "regular" in low:
            score = 2
        elif "medium" in low:
            score = 1
        if score > best_score:
            best, best_score = i, score
    return best
