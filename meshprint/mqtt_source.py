"""Meshtastic MQTT 來源(規格 §12,v1.1):訂閱社群 broker,把台灣頻道的聊天訊息印上紙。

工作原理
--------
Meshtastic 網路裡有些節點(閘道器)會把收到的封包原封不動上傳到 MQTT broker,
主題(topic)長這樣:

    msh/TW/2/e/<頻道名>/!<閘道器ID>        ← 加密信封(我們要的)
    msh/TW/2/json/… 、 /2/map/… 、 /2/stat/…   ← JSON/地圖/統計(略過)
    msh/TW/<子區域>/2/e/…                   ← 有些社群再分區,一樣收

我們訂閱 `msh/TW/#`(root 底下全部),流程:

1. ServiceEnvelope(protobuf):裡面有 channel_id(頻道名)、gateway_id 與
   MeshPacket。channel_id 不在設定的頻道清單 → 直接丟。
2. 去重:同一個空中封包會被好幾台閘道器各上傳一次,MeshPacket 的
   (from, id) 是唯一鍵,用 LRU 集合記最近 1024 個,重複的丟掉。
3. 解密:MeshPacket 的 payload 通常是 `encrypted`(頻道金鑰加密的 bytes)。
   Meshtastic 用 AES-CTR,nonce 固定 16 bytes = packet_id(8 bytes 小端,
   高 4 bytes 為 0)+ from 節點編號(4 bytes 小端)+ 4 bytes 0。
   金鑰(PSK)用 base64 寫在設定裡:1 byte 的短 PSK 是「預設金鑰的變體」
   (韌體規則:defaultpsk 最後一 byte 加上 n-1;`AQ==` = 0x01 就是原版預設鍵),
   16/32 bytes 則直接當 AES-128/256 金鑰。解不開(金鑰不對)就當作不是給我們的。
4. 解出來的 `Data` 有 portnum:
   - TEXT_MESSAGE_APP(1):聊天文字 → 轉成 InboundMessage 交給列印管線;
   - NODEINFO_APP(4):節點自報家門(User:id、long_name、short_name)→
     存進「節點編號 → 名稱」快取,之後該節點的訊息就印得出名字;
   - 其他(位置、遙測、traceroute…)一律丟棄。
5. 私訊(PKI 頻道)用收件者的公鑰加密,我們沒有私鑰,無法解密,一律略過。

本模組**純唯讀**:只 subscribe,永遠不 publish。

執行緒模型:paho-mqtt 的網路迴圈跑在它自己的執行緒(loop_start),
on_message 回呼在那個執行緒;我們用 loop.call_soon_threadsafe 把
(topic, payload) 丟回 asyncio 主迴圈再解碼,解碼與後續處理都在主迴圈,
不必加鎖。斷線由 paho 自動重連(reconnect_delay_set),重連後在 on_connect
裡重新訂閱。

Decoder 是純邏輯(不碰網路),tests/test_mqtt.py 自己組 protobuf、
用同一個 CTR 函式加密後餵進去測。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import struct
from collections import OrderedDict
from datetime import datetime
from typing import Callable, Optional

from .model import InboundMessage
from .node import _tzinfo

log = logging.getLogger(__name__)

# 韌體 defaultpsk:1-byte PSK 的基底金鑰,最後一 byte 依值遞增
DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
SEEN_CAP = 1024  # 來源端以 (from, packet_id) 去重——同一包會被多個閘道器轉發


def expand_psk(b64: str) -> Optional[bytes]:
    """Meshtastic 頻道金鑰:1 byte = 預設鍵變體(0 = 不加密)、16/32 bytes = AES-128/256。
    回傳 b"" 表示不加密,None 表示無效。"""
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if len(raw) == 0:
        return b""
    if len(raw) == 1:
        if raw[0] == 0:
            return b""
        key = bytearray(DEFAULT_PSK)
        key[-1] = (key[-1] + raw[0] - 1) & 0xFF   # 0x01 → 原版預設鍵,0x02 → 最後一 byte +1…
        return bytes(key)
    if len(raw) in (16, 32):
        return raw
    return None


def decrypt_packet(key: bytes, from_num: int, packet_id: int, data: bytes) -> bytes:
    """AES-CTR;nonce = packet_id(u64 LE)+ from(u32 LE)+ 4×0(韌體 CryptoEngine 佈局)。
    CTR 對稱,同函式亦可加密(測試用)。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    nonce = struct.pack("<QI", packet_id & 0xFFFFFFFFFFFFFFFF,
                        from_num & 0xFFFFFFFF) + b"\x00\x00\x00\x00"
    c = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    return c.update(data) + c.finalize()


class Decoder:
    """topic + payload → InboundMessage 的純邏輯(可完全離線測試)。"""

    def __init__(self, mqtt_cfg, tz):
        self.tz = tz
        root = mqtt_cfg.root.strip("/")
        region = root.split("/", 1)[1] if "/" in root else root   # "msh/TW" → "TW"
        self.keys = {}       # 頻道名 → 展開後的金鑰(b"" = 不加密)
        self.display = {}    # 頻道名 → 票頭顯示字串,例如 "MQTT TW/SignalTest"
        for ch in mqtt_cfg.channels:
            if not isinstance(ch, dict):
                log.warning("[mqtt] channels 項目應為 {name=..., psk=...},略過:%r", ch)
                continue
            name = str(ch.get("name", "")).strip()
            if not name:
                continue
            key = expand_psk(str(ch.get("psk", "AQ==")))
            if key is None:
                log.warning("MQTT 頻道 %s 的 PSK 無效,略過", name)
                continue
            self.keys[name] = key
            self.display[name] = "{} {}/{}".format(mqtt_cfg.label, region, name).strip()
        self.names = {}                     # node number -> long_name(由 NODEINFO 累積)
        self._seen = OrderedDict()          # (from, packet_id) LRU:閘道器重複轉發去重

    def handle(self, topic: str, payload: bytes) -> Optional[InboundMessage]:
        """一則 MQTT 訊息 → InboundMessage(是聊天文字時)或 None(其餘一切)。"""
        parts = topic.split("/")
        if len(parts) < 4 or parts[-3] != "e":   # 只要加密信封主題,跳過 json/map/stat
            return None
        from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2

        env = mqtt_pb2.ServiceEnvelope()
        try:
            env.ParseFromString(payload)
        except Exception:
            return None
        if env.channel_id not in self.keys:
            return None
        pkt = env.packet
        from_num = getattr(pkt, "from")     # "from" 是 Python 保留字,只能這樣取
        if pkt.id:
            seen_key = (from_num, pkt.id)
            if seen_key in self._seen:
                self._seen.move_to_end(seen_key)
                return None
            self._seen[seen_key] = None
            while len(self._seen) > SEEN_CAP:
                self._seen.popitem(last=False)
        data = self._payload(pkt, self.keys[env.channel_id])
        if data is None:
            return None
        if data.portnum == portnums_pb2.NODEINFO_APP:
            # 節點自報家門:記下名字,這則本身不印
            try:
                user = mesh_pb2.User()
                user.ParseFromString(data.payload)
                if user.long_name:
                    self.names[from_num] = user.long_name
            except Exception:
                pass
            return None
        if data.portnum != portnums_pb2.TEXT_MESSAGE_APP:
            return None
        extra = {}
        if pkt.hop_start:
            # hop_start = 出發時的 hop 額度,hop_limit = 抵達閘道器時剩下的;差 = 經過幾跳
            hops = max(0, pkt.hop_start - pkt.hop_limit)
            extra["path_len"] = hops if hops > 0 else -1   # 0 hop = 直收
        if pkt.rx_snr:
            extra["SNR"] = pkt.rx_snr          # 閘道器收到時的 SNR
        msg_time = None
        if pkt.rx_time:
            try:
                msg_time = datetime.fromtimestamp(pkt.rx_time, self.tz)
            except (OverflowError, OSError, ValueError):
                pass
        return InboundMessage(
            kind="channel",
            text=data.payload.decode("utf-8", "replace"),
            rx_time=datetime.now(self.tz),
            sender_name=self.names.get(from_num, ""),
            sender_prefix="{:08x}".format(from_num & 0xFFFFFFFF)[-6:],   # 節點 ID 末 6 碼 hex
            channel_idx=None,               # MQTT 頻道沒有編號,票頭只印名稱
            channel_name=self.display[env.channel_id],
            msg_time=msg_time,
            extra=extra,
        )

    def _payload(self, pkt, key):
        """取出 MeshPacket 的 Data:已解碼的直接用;加密的用頻道金鑰解。"""
        from meshtastic.protobuf import mesh_pb2

        which = pkt.WhichOneof("payload_variant")
        if which == "decoded":
            return pkt.decoded
        if which != "encrypted" or not key:
            return None
        try:
            data = mesh_pb2.Data()
            data.ParseFromString(
                decrypt_packet(key, getattr(pkt, "from"), pkt.id, pkt.encrypted))
            return data
        except Exception:
            return None  # 金鑰不符或封包損毀:略過


class MqttSource:
    """MQTT 訂閱端:連線/重連交給 paho,收到的封包丟回 asyncio 迴圈解碼。"""

    def __init__(self, cfg, on_message: Callable[[InboundMessage], None]):
        self.cfg = cfg.mqtt
        self.on_message = on_message
        self.decoder = Decoder(cfg.mqtt, _tzinfo(cfg.render.timezone))

    async def run(self, stop: asyncio.Event) -> None:
        """連上 broker、訂閱、一直收到 stop 為止;斷線由 paho 自動重連。"""
        import paho.mqtt.client as mqtt

        loop = asyncio.get_running_loop()
        topic = self.cfg.root.strip("/") + "/#"
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="meshprint-" + secrets.token_hex(4))   # 隨機 id 避免撞名被踢
        if self.cfg.username:
            client.username_pw_set(self.cfg.username, self.cfg.password)
        if self.cfg.tls:
            client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=60)   # 重連退避 1 → 60 秒

        def on_connect(c, userdata, flags, reason_code, properties=None):
            log.info("MQTT 已連線 %s:%s,訂閱 %s(頻道:%s)",
                     self.cfg.host, self.cfg.port, topic,
                     ", ".join(self.decoder.keys) or "(未設定)")
            c.subscribe(topic)  # 放在 on_connect:重連後自動重訂閱

        def on_disconnect(c, userdata, flags, reason_code, properties=None):
            if not stop.is_set():
                log.warning("MQTT 斷線(%s),自動重連中…", reason_code)

        def on_message(c, userdata, m):
            # 這裡在 paho 的執行緒;把工作丟回 asyncio 主迴圈做
            loop.call_soon_threadsafe(self._handle, m.topic, m.payload)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)   # 非阻塞,失敗也會自動重試
        client.loop_start()
        try:
            await stop.wait()
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass

    def _handle(self, topic: str, payload: bytes) -> None:
        """(主迴圈)解碼一則 MQTT 訊息;是聊天文字就交給上層。"""
        try:
            msg = self.decoder.handle(topic, payload)
        except Exception:
            log.exception("MQTT 封包處理失敗(topic=%s)", topic)
            return
        if msg is not None:
            log.info("MQTT 收到:%s|%s|%d字", msg.channel_name, msg.sender_line(),
                     len(msg.text))
            try:
                self.on_message(msg)
            except Exception:
                log.exception("on_message 處理失敗")
