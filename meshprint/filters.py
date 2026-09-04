"""過濾(規格 §6.7 [filter]):決定一則訊息要不要印。

規則依序:
1. 私訊:print_dm = false 就不印;
2. 頻道編號:channels 不是 "all" 時,MeshCore 頻道訊息的 channel_idx 不在清單就不印。
   MQTT 來源沒有頻道編號(idx = None),不受此規則約束——它自己在 [mqtt] channels 選頻道;
3. 忽略名單 ignore_senders:每個項目同時試兩種比對——
   - 與寄件者顯示名稱完全相同(不分大小寫);
   - 是 hex 字串時,與寄件者前綴(pubkey 前綴 / 節點 ID 末碼)做「開頭比對」,
     所以寫票上 <> 內看到的 6 碼就能擋。
回傳 (要不要印, 原因代碼);原因會出現在 log,方便查為什麼沒印。
"""
from __future__ import annotations

from typing import Tuple

from .model import InboundMessage


def _is_hexish(s: str) -> bool:
    """看起來像 hex 前綴(至少 2 碼、全是 0-9a-f)才拿去比對前綴,避免名字誤中。"""
    return len(s) >= 2 and all(c in "0123456789abcdef" for c in s)


def should_print(msg: InboundMessage, fcfg) -> Tuple[bool, str]:
    if msg.kind == "dm":
        if not fcfg.print_dm:
            return False, "dm-off"
    else:
        channels = fcfg.channels
        # 頻道編號過濾只對有 idx 的來源(MeshCore 節點)生效;
        # MQTT 來源(idx=None)自己在 [mqtt] channels 選頻道
        if channels != "all" and msg.channel_idx is not None:
            allowed = {int(c) for c in channels}
            if msg.channel_idx not in allowed:
                return False, "channel-{}-filtered".format(msg.channel_idx)
    for entry in fcfg.ignore_senders:
        low = str(entry).strip().lower()
        if not low:
            continue
        if msg.sender_name and msg.sender_name.lower() == low:
            return False, "ignored-name"
        if msg.sender_prefix and _is_hexish(low) and msg.sender_prefix.lower().startswith(low):
            return False, "ignored-prefix"
    return True, "ok"
