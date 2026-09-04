"""訊息資料模型(規格 §6.2):所有來源的訊息都先轉成同一個 InboundMessage。

工作原理
--------
MeshCore 節點的私訊/頻道訊息、Meshtastic MQTT 的頻道訊息,格式各不相同;
各來源模組(node.py、mqtt_source.py)負責把自己的 payload 對應成這個統一物件,
之後的過濾、去重、版面、spool 就都只認 InboundMessage,不用知道訊息從哪來。

版面(render.py)只透過三個方法取字串:
- header_left():票的第一列左邊——「私訊」或「#編號 頻道名」或(無編號時)頻道名;
- sender_line():第二列左邊——「名稱 <前綴>」/「名稱」/「<前綴>」/「(未具名)」;
- extra_note():第二列右邊——hops、SNR 等選配資訊,沒有就空字串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class InboundMessage:
    kind: str                       # "dm" | "channel"
    text: str                       # 內文(UTF-8;版面會再做 sanitize)
    rx_time: datetime               # 本機接收時間(版面主要時間,aware datetime)
    sender_name: str = ""           # 聯絡人 adv_name / NODEINFO 名稱;查無則空字串
    sender_prefix: str = ""         # MeshCore:pubkey 前綴 hex;MQTT:節點 ID 末 6 碼(顯示前 6 碼)
    channel_idx: Optional[int] = None   # MeshCore 頻道編號;MQTT 來源為 None
    channel_name: str = ""          # 頻道名稱(MQTT 來源會帶「MQTT TW/…」前綴)
    msg_time: Optional[datetime] = None  # 發送端時戳(不可信任,僅去重與參考用)
    extra: dict = field(default_factory=dict)  # path_len、SNR 等選配欄位

    def header_left(self) -> str:
        if self.kind == "dm":
            return "私訊"
        if self.channel_idx is None:  # 無頻道編號的來源(如 MQTT):只印名稱
            return self.channel_name.strip() or "頻道"
        return "#{} {}".format(self.channel_idx, self.channel_name.strip()).rstrip()

    def sender_line(self) -> str:
        prefix = self.sender_prefix[:6]   # §6.2:前綴只顯示前 6 碼 hex
        if self.sender_name and prefix:
            return "{} <{}>".format(self.sender_name, prefix)
        if self.sender_name:
            return self.sender_name
        if prefix:
            return "<{}>".format(prefix)
        return "(未具名)"

    def extra_note(self) -> str:
        parts = []
        hops = self.extra.get("path_len")
        if hops is not None and hops != "":
            # MeshCore 用 255 表示直收;MQTT 來源把 0 hop 轉成 -1,同樣顯示「直收」
            parts.append("直收" if hops in (255, -1) else "hops {}".format(hops))
        snr = self.extra.get("SNR")
        if isinstance(snr, (int, float)):
            parts.append("SNR {:+.1f}".format(snr))
        return " · ".join(parts)
