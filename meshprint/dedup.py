"""去重(規格 §6.2 / §7):同一則訊息只印一次,程式重啟也不重印。

工作原理
--------
同一則訊息可能重複到達:節點重連後 auto-fetch 再撈一次、程式重啟後節點暫存
再送一次、MQTT 不同閘道器各轉發一次(那層在 mqtt_source 先以 packet id 擋掉)。
這裡用「訊息內容的指紋」當鍵:

    kind | 寄件者前綴(私訊)或 ch<編號>:<寄件者>(頻道) | 發送端時戳 | sha1(內文)[:8]

指紋相同 → 視為同一則。用 OrderedDict 實作 LRU:只記最近 256 則,舊的自動擠掉。
每次新增都把整份鍵清單寫到 dedup.json(先寫暫存檔再原子改名),
下次啟動先讀回來,所以重啟不會把剛印過的再印一次。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .model import InboundMessage

log = logging.getLogger(__name__)


class DedupLRU:
    def __init__(self, capacity: int = 256, path: Optional[Path] = None):
        self.capacity = capacity
        self.path = Path(path).expanduser() if path else None   # None = 只在記憶體,不持久化
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        if self.path and self.path.exists():
            try:
                for key in json.loads(self.path.read_text("utf-8")):
                    self._seen[str(key)] = None
            except Exception as e:
                log.warning("讀取去重狀態失敗,重新開始:%s", e)

    @staticmethod
    def key_for(msg: InboundMessage) -> str:
        """訊息指紋(見模組說明)。"""
        # 規格 §6.2:(kind, sender/channel, msg_time, sha1(text)[:8])
        if msg.kind == "dm":
            ident = msg.sender_prefix
        else:
            ident = "ch{}:{}".format(msg.channel_idx, msg.sender_name)
        t = int(msg.msg_time.timestamp()) if msg.msg_time else 0
        digest = hashlib.sha1(msg.text.encode("utf-8")).hexdigest()[:8]
        return "|".join([msg.kind, ident, str(t), digest])

    def seen_or_add(self, key: str) -> bool:
        """已見過 → True(呼叫端丟棄);否則記下並回 False。"""
        if key in self._seen:
            self._seen.move_to_end(key)      # 重新標成「最近用過」
            return True
        self._seen[key] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)   # 擠掉最舊的
        self._persist()
        return False

    def __len__(self) -> int:
        return len(self._seen)

    def _persist(self) -> None:
        """整份鍵清單寫檔(256 筆很小);失敗只警告,不影響列印。"""
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self._seen)), "utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("寫入去重狀態失敗:%s", e)
