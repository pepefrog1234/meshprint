"""MeshCore 節點連線模組(規格 §6.1):USB serial 節點 → 事件 → InboundMessage。

工作原理
--------
節點端不自己實作序列協定,直接用官方 `meshcore` 函式庫(asyncio、事件驅動)。
本模組只做兩件事:

1. 外層監督迴圈 run():
   - 找裝置:設定 port="auto" 時掃描 /dev/cu.usbmodem*(macOS)、/dev/ttyACM*、
     /dev/ttyUSB*(Linux);沒有就每 10 秒再掃,直到裝置出現。
   - 連線:MeshCore.create_serial() 會開序列埠並送 appstart(相當於「敲門」);
     節點沒回應(不是 companion serial 韌體、或是別的 USB 裝置)就回 None,
     5 秒後重試。
   - 進入工作階段 _session();結束(斷線)後清理、回到迴圈頂端重新找裝置——
     所以拔線再插回、節點重開機都會自動接回來。
   函式庫本身有 auto_reconnect:短暫斷線它自己會重連 3 次並自動補送 appstart
   (韌體要求每次連線都要 appstart);連 3 次都失敗才發 DISCONNECTED 事件,
   由本模組的外層迴圈接手「整個重來」。兩層合作:小抖動函式庫處理、
   大故障本模組處理。

2. 單一工作階段 _session():
   - 開啟 auto_update_contacts:節點收到別人的廣告(advert)、新聯絡人時,
     函式庫會用 lastmod 增量抓聯絡人表,寄件者名稱才查得到。
   - 訂閱三個事件:CONTACT_MSG_RECV(私訊)、CHANNEL_MSG_RECV(頻道訊息)、
     DISCONNECTED(結束工作階段)。
   - start_auto_message_fetching():節點韌體有暫存佇列,離線期間收到的訊息
     會存在節點裡;開啟後函式庫收到 MESSAGES_WAITING 就自動一則一則撈出來
     (啟動時也立刻撈一次),因此程式重啟不會漏掉節點暫存的訊息。
   - 等到 stop(使用者按 Ctrl-C)或 gone(斷線)其中一個發生就退出。

payload → InboundMessage 的對應(contact_message / channel_message)是純函式,
tests/test_node.py 用假的 MeshCore 物件離線測試。

頻道訊息的寄件者:MeshCore 頻道訊息在協定層沒有寄件者欄位(只有私訊有
pubkey_prefix);生態慣例是發送端把「名字: 內容」寫在文字開頭,
所以 split_channel_sender() 用啟發式把名字拆出來。
"""
from __future__ import annotations

import asyncio
import glob
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from .model import InboundMessage

log = logging.getLogger(__name__)

PORT_GLOBS = ["/dev/cu.usbmodem*", "/dev/ttyACM*", "/dev/ttyUSB*"]
SCAN_INTERVAL = 10   # §6.1:裝置消失時每 10 秒掃描
CONNECT_RETRY = 5    # 節點無回應時的重試間隔(秒)
APPSTART_TIMEOUT = 5.0   # 敲門(appstart)等回應的上限(秒)


def _tzinfo(name: str):
    """設定的時區;無效就退回 UTC(不讓一個打錯的時區名弄掛整個程式)。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def split_channel_sender(text: str) -> Tuple[str, str]:
    """頻道訊息無協定層寄件者;MeshCore 慣例是文字開頭「名字: 內容」。

    啟發式:冒號前 ≤ 32 字元(adv_name 上限)、無換行、不含 URL 樣式才視為名字。
    回傳 (名字或空字串, 內容)。
    """
    head, sep, rest = text.partition(": ")
    if sep and 0 < len(head) <= 32 and "\n" not in head and "://" not in head:
        return head, rest
    return "", text


def _common_extra(payload: dict) -> dict:
    """兩種訊息共用的選配欄位:path_len(經過幾跳,255 = 直收)、SNR(v3 韌體才有)、txt_type。"""
    extra = {}
    if "path_len" in payload:
        extra["path_len"] = payload["path_len"]
    if "SNR" in payload:
        extra["SNR"] = payload["SNR"]
    if "txt_type" in payload:
        extra["txt_type"] = payload["txt_type"]
    return extra


def _msg_time(payload: dict, tz) -> Optional[datetime]:
    """發送端時戳(epoch 秒)→ datetime;值怪異就當作沒有。"""
    ts = payload.get("sender_timestamp")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz)
    except (OverflowError, OSError, ValueError):
        return None  # 發送端時戳本就不可信任(§6.2)


def contact_message(payload: dict, resolve_name: Callable[[str], str], tz) -> InboundMessage:
    """私訊 payload → InboundMessage。寄件者由 pubkey_prefix 透過聯絡人表查名稱。"""
    prefix = payload.get("pubkey_prefix", "") or ""
    return InboundMessage(
        kind="dm",
        text=payload.get("text", "") or "",
        rx_time=datetime.now(tz),
        sender_name=resolve_name(prefix) or "",
        sender_prefix=prefix,
        msg_time=_msg_time(payload, tz),
        extra=_common_extra(payload),
    )


def channel_message(payload: dict, channel_name: str, tz) -> InboundMessage:
    """頻道訊息 payload → InboundMessage。寄件者從文字開頭的「名字: 」拆出。"""
    sender, body = split_channel_sender(payload.get("text", "") or "")
    return InboundMessage(
        kind="channel",
        text=body,
        rx_time=datetime.now(tz),
        sender_name=sender,
        channel_idx=payload.get("channel_idx"),
        channel_name=channel_name or "",
        msg_time=_msg_time(payload, tz),
        extra=_common_extra(payload),
    )


class NodeClient:
    """一台 MeshCore 節點的監督者:找裝置、連線、收事件、斷線重來。"""

    def __init__(self, cfg, on_message: Callable[[InboundMessage], None]):
        self.cfg = cfg
        self.on_message = on_message       # 收到訊息就呼叫(daemon 傳入的是 inbox.put_nowait)
        self.tz = _tzinfo(cfg.render.timezone)
        self.status = "尚未連線"            # 給 log/狀態顯示用的人類可讀狀態
        self._channel_names = {}           # channel_idx → 名稱快取(每次工作階段重建)

    # ---- 外層監督迴圈 ----

    async def run(self, stop: asyncio.Event) -> None:
        """一直跑到 stop 被設為止:找裝置 → 連線 → 工作階段 → 斷線 → 重來。"""
        from meshcore import MeshCore

        announced_waiting = False
        while not stop.is_set():
            port = self._pick_port()
            if port is None:
                if not announced_waiting:   # 「等待裝置」只提示一次,免得洗版
                    log.info("等待 MeshCore 節點裝置(掃描 %s;每 %d 秒)…",
                             " ".join(PORT_GLOBS), SCAN_INTERVAL)
                    announced_waiting = True
                self.status = "等待裝置"
                await self._sleep(stop, SCAN_INTERVAL)
                continue
            announced_waiting = False
            self.status = "連線中 {}".format(port)
            mc = None
            try:
                # auto_reconnect:短暫斷線由函式庫重連 3 次(重連後自動補 appstart)
                mc = await MeshCore.create_serial(
                    port, self.cfg.node.baud,
                    auto_reconnect=True, max_reconnect_attempts=3,
                    default_timeout=APPSTART_TIMEOUT)
            except Exception as e:
                log.warning("開啟 %s 失敗:%s", port, e)
            if mc is None:
                log.warning("%s 無回應(不是 companion serial 韌體?)%d 秒後重試",
                            port, CONNECT_RETRY)
                self.status = "節點無回應"
                await self._sleep(stop, CONNECT_RETRY)
                continue
            try:
                await self._session(mc, stop)
            except Exception:
                log.exception("節點工作階段異常,重新連線")
            finally:
                try:
                    await mc.disconnect()
                except Exception:
                    pass
            self._channel_names.clear()
            self.status = "已斷線"
            await self._sleep(stop, 1)

    def _pick_port(self) -> Optional[str]:
        """設定指定的埠(存在才回)或自動掃描到的第一個候選裝置。"""
        configured = self.cfg.node.port
        if configured != "auto":
            import os.path
            return configured if os.path.exists(configured) else None
        hits = []
        for pattern in PORT_GLOBS:
            hits.extend(sorted(glob.glob(pattern)))
        if not hits:
            return None
        if len(hits) > 1:
            log.warning("找到多個候選裝置 %s,先試 %s;多裝置環境請在設定檔指定 port",
                        hits, hits[0])
        return hits[0]

    # ---- 單一連線工作階段 ----

    async def _session(self, mc, stop: asyncio.Event) -> None:
        """已連上的節點:抓聯絡人、訂閱事件、開自動撈訊息,直到 stop 或斷線。"""
        from meshcore import EventType

        name = (mc.self_info or {}).get("name", "?")   # appstart 回應裡有節點自己的名字
        log.info("節點已連線:%s(%s)", name, self.status.replace("連線中 ", ""))
        self.status = "已連線:{}".format(name)

        mc.auto_update_contacts = True  # 廣告/新聯絡人時由函式庫增量刷新聯絡人表
        try:
            await mc.ensure_contacts()
        except Exception as e:
            log.warning("抓取聯絡人表失敗(名稱解析先用空表):%s", e)

        gone = asyncio.Event()

        def on_disconnected(event) -> None:
            # 函式庫重連 3 次都失敗(或手動斷線)才會發這個事件
            log.warning("節點連線中斷:%s", event.payload)
            gone.set()

        async def on_contact(event) -> None:
            self._emit(contact_message(event.payload, self._resolver(mc), self.tz))

        async def on_channel(event) -> None:
            idx = event.payload.get("channel_idx")
            chname = await self._channel_name(mc, idx)
            self._emit(channel_message(event.payload, chname, self.tz))

        subs = [
            mc.subscribe(EventType.DISCONNECTED, on_disconnected),
            mc.subscribe(EventType.CONTACT_MSG_RECV, on_contact),
            mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel),
        ]
        try:
            await mc.start_auto_message_fetching()  # 含立即撈一次節點暫存的訊息
            await self._until_any(stop, gone)
        finally:
            for s in subs:
                try:
                    mc.unsubscribe(s)
                except Exception:
                    pass

    def _resolver(self, mc) -> Callable[[str], str]:
        """回一個「pubkey 前綴 → adv_name」的查詢函式(查函式庫快取的聯絡人表)。"""
        def resolve(prefix: str) -> str:
            contact = mc.get_contact_by_key_prefix(prefix)
            return (contact or {}).get("adv_name", "")
        return resolve

    async def _channel_name(self, mc, idx) -> str:
        """向節點問頻道名稱(get_channel),每個 idx 只問一次、結果快取到工作階段結束。"""
        if idx is None:
            return ""
        if idx in self._channel_names:
            return self._channel_names[idx]
        from meshcore import EventType
        name = ""
        try:
            res = await mc.commands.get_channel(int(idx))
            if res is not None and res.type == EventType.CHANNEL_INFO:
                name = (res.payload or {}).get("channel_name", "") or ""
        except Exception as e:
            log.debug("查詢頻道 %s 名稱失敗:%s", idx, e)
        self._channel_names[idx] = name
        return name

    def _emit(self, msg: InboundMessage) -> None:
        """把訊息交給上層;上層炸了只記 log,不能讓事件處理器死掉。"""
        try:
            self.on_message(msg)
        except Exception:
            log.exception("on_message 處理失敗")

    # ---- 小工具 ----

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        """可被 stop 打斷的 sleep:Ctrl-C 時不用等完整個間隔。"""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def _until_any(*events: asyncio.Event) -> None:
        """等任一事件發生;其餘等待任務一律取消,避免殘留。"""
        tasks = [asyncio.create_task(e.wait()) for e in events]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
