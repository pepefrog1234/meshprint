"""設定檔(規格 §6.7):TOML 載入、預設值、基本驗證。

工作原理
--------
- 每個 [區段] 對應一個 dataclass,dataclass 的欄位預設值就是程式預設值。
  設定檔不存在時整個用預設值(所以第一次跑不需要任何設定檔)。
- load() 讀 ~/.config/meshprint/config.toml(或 --config 指定的路徑),
  把檔案裡有寫的欄位覆蓋到 dataclass 上;未知的區段/欄位只記警告、不中斷
  (打錯字不會讓程式起不來,但 log 會提醒)。
- 讀完做幾項「保護性修正」:內文字級不得低於 24 px(點陣機物理極限)、
  [filter] channels 與 [mqtt] channels 的型別不對就退回預設。
- TOML 解析用 3.11 內建的 tomllib;3.10 退回相容套件 tomli。
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("~/.config/meshprint/config.toml")

MIN_BODY_PX = 24  # §6.4:180 dpi 下漢字低於 24×24 點會糊,硬下限


@dataclass
class NodeConfig:
    """[node] MeshCore 節點來源。"""
    enabled: bool = True
    transport: str = "serial"     # v1 僅 serial
    port: str = "auto"            # "auto" 掃描 /dev/cu.usbmodem* 等;或指定裝置路徑
    baud: int = 115200


def _default_mqtt_channels():
    # 臺灣鏈網官方頻道組(PSK 解自社群公開發布的頻道 QR,2026-01 版):
    # SignalTest 為最熱絡聊天頻道(業主確認),MeshTW 社群頻道、Emergency! 緊急頻道;
    # MediumFast 主頻道多為遙測/位置(非 TEXT 一律丟棄)、LongFast 為全球預設
    return [{"name": "SignalTest", "psk": "y1HciVgpl5Hzh05KJUe/umWUH8XhG3UjR1rvZHfUHFU="},
            {"name": "MeshTW", "psk": "isDhHrNpJPlGX3GBJBX6kjuK7KQNp4Z0M7OTDpnX5N4="},
            {"name": "Emergency!", "psk": "y2jnf86fTpf/4AFAf+mCwbzRoxpCV0P90dqJo0+w/SY="},
            {"name": "MediumFast", "psk": "AQ=="},
            {"name": "LongFast", "psk": "AQ=="}]


@dataclass
class MqttConfig:
    """[mqtt] Meshtastic MQTT 來源(規格 §12)。"""
    enabled: bool = False
    host: str = "mqtt.meshtastic.org"   # 官方公共 broker(帳密為官方公開值)
    port: int = 1883
    username: str = "meshdev"
    password: str = "large4cats"
    tls: bool = False
    root: str = "msh/TW"                # 臺灣鏈網 root topic;訂閱 root/#
    label: str = "MQTT"                 # 票頭來源標示,例如「MQTT TW/SignalTest」
    channels: List[dict] = field(default_factory=_default_mqtt_channels)  # [[mqtt.channels]] name/psk


@dataclass
class PrinterConfig:
    """[printer] 印表機與紙張幾何。"""
    queue: str = "auto"           # CUPS 佇列名;"auto" = 系統唯一的那個
    width_dots: int = 1440        # 可印寬度:8 吋 × 180 dpi(業主實機縮成 1332 = 7.4 吋)
    left_margin_dots: int = 24    # 左右對稱邊界(點)
    feed_after_lines: int = 4     # 每張票印完走紙幾行(1 行 = 1/6 吋)
    form_feed: bool = False       # 連續報表紙不換頁;A4 單張才需要


@dataclass
class RenderConfig:
    """[render] 字型與版面。"""
    font: str = "~/Library/Fonts/NotoSansCJKtc-Regular.otf"
    font_index: int = 0           # .ttc 集合檔的 face 索引
    fallback_fonts: List[str] = field(default_factory=list)  # "路徑" 或 "路徑#索引"
    body_px: int = 28             # 內文字級(點),不得低於 MIN_BODY_PX
    header_px: int = 24           # 標題/寄件者列字級
    max_body_lines: int = 40      # 內文超過就截斷並印「…(截斷)」
    timezone: str = "Asia/Taipei"
    time_format: str = "%Y-%m-%d %H:%M:%S"


@dataclass
class FilterConfig:
    """[filter] 要印哪些訊息。"""
    print_dm: bool = True
    channels: Any = "all"         # "all" 或 [0, 1, ...](只約束 MeshCore 頻道編號)
    ignore_senders: List[str] = field(default_factory=list)  # 名稱(不分大小寫)或 hex 前綴


@dataclass
class SpoolConfig:
    """[spool] 磁碟 spool 行為。"""
    dir: str = "~/.meshprint"     # 狀態根目錄(spool/、done/、dedup.json)
    keep_done: int = 200          # §6.6:done/ 保留份數
    max_pending: int = 100        # §7:訊息風暴上限,超過丟最舊
    retry_seconds: int = 30       # §6.6:背景重試間隔
    max_age_minutes: int = 0      # >0:超過 N 分鐘沒能送印就略過(0 = 永不過期,照 §6.6 補印)


@dataclass
class LogConfig:
    """[log](M4 服務化時使用)。"""
    level: str = "info"
    dir: str = "~/.meshprint/log"


@dataclass
class Config:
    node: NodeConfig = field(default_factory=NodeConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    printer: PrinterConfig = field(default_factory=PrinterConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    spool: SpoolConfig = field(default_factory=SpoolConfig)
    log: LogConfig = field(default_factory=LogConfig)


def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib
    with path.open("rb") as f:
        return tomllib.load(f)


def load(path=None) -> Config:
    """讀設定檔(不存在則全預設)並做保護性修正;--config 指定的檔案不存在才報錯。"""
    cfg = Config()
    p = Path(path).expanduser() if path else DEFAULT_PATH.expanduser()
    if p.exists():
        _apply(cfg, _load_toml(p), p)
    elif path:
        raise FileNotFoundError("設定檔不存在:{}".format(p))
    if cfg.render.body_px < MIN_BODY_PX:
        log.warning("body_px=%d 低於下限 %d(§6.4 點陣機物理極限),強制調升",
                    cfg.render.body_px, MIN_BODY_PX)
        cfg.render.body_px = MIN_BODY_PX
    ch = cfg.filter.channels
    if ch != "all":
        if isinstance(ch, list):
            try:
                cfg.filter.channels = [int(c) for c in ch]
            except (TypeError, ValueError):
                log.warning("[filter] channels=%r 無法解析,改為 \"all\"", ch)
                cfg.filter.channels = "all"
        else:
            log.warning("[filter] channels=%r 應為 \"all\" 或整數陣列,改為 \"all\"", ch)
            cfg.filter.channels = "all"
    if not isinstance(cfg.mqtt.channels, list):
        log.warning("[mqtt] channels 應為陣列([[mqtt.channels]] 表),改用預設值")
        cfg.mqtt.channels = _default_mqtt_channels()
    return cfg


def _apply(cfg: Config, data: dict, src: Path) -> None:
    """把 TOML 的 {區段: {欄位: 值}} 覆蓋到對應 dataclass;未知的只警告。"""
    for section, values in data.items():
        target = getattr(cfg, section, None)
        if target is None or not dataclasses.is_dataclass(target) or not isinstance(values, dict):
            log.warning("%s:未知設定區段 [%s],忽略", src, section)
            continue
        names = {f.name for f in dataclasses.fields(target)}
        for key, value in values.items():
            if key not in names:
                log.warning("%s:[%s] 未知欄位 %s,忽略", src, section, key)
                continue
            setattr(target, key, value)
