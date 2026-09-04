from __future__ import annotations

import asyncio
from datetime import timezone

from meshcore import EventType
from meshcore.events import Event

from meshprint.config import Config
from meshprint.model import InboundMessage
from meshprint.node import (NodeClient, channel_message, contact_message,
                            split_channel_sender)

UTC = timezone.utc


def test_split_channel_sender():
    assert split_channel_sender("Bee: hello") == ("Bee", "hello")
    assert split_channel_sender("a: b: c") == ("a", "b: c")
    assert split_channel_sender("no colon here") == ("", "no colon here")
    assert split_channel_sender("http://x: y") == ("", "http://x: y")
    assert split_channel_sender("x" * 40 + ": y") == ("", "x" * 40 + ": y")
    assert split_channel_sender("a\nb: c") == ("", "a\nb: c")
    assert split_channel_sender(": naked") == ("", ": naked")


def test_contact_message_mapping():
    payload = {"pubkey_prefix": "a1b2c3d4e5f6", "text": "hi",
               "sender_timestamp": 1767000000, "path_len": 2, "SNR": 8.25,
               "txt_type": 0}
    msg = contact_message(payload, lambda p: "蜜蜂" if p.startswith("a1b2") else "", UTC)
    assert msg.kind == "dm"
    assert msg.sender_name == "蜜蜂"
    assert msg.sender_prefix == "a1b2c3d4e5f6"
    assert msg.sender_line() == "蜜蜂 <a1b2c3>"
    assert int(msg.msg_time.timestamp()) == 1767000000
    assert msg.extra_note() == "hops 2 · SNR +8.2"


def test_contact_message_unknown_sender_and_direct():
    msg = contact_message({"pubkey_prefix": "deadbeef0000", "text": "x",
                           "path_len": 255}, lambda p: "", UTC)
    assert msg.sender_line() == "<deadbe>"
    assert msg.extra_note() == "直收"


def test_channel_message_mapping():
    payload = {"channel_idx": 0, "text": "蜜蜂 Bee: 今晚例會",
               "sender_timestamp": 1767000000, "path_len": 1}
    msg = channel_message(payload, "公共頻道", UTC)
    assert msg.kind == "channel"
    assert msg.header_left() == "#0 公共頻道"
    assert msg.sender_name == "蜜蜂 Bee"
    assert msg.text == "今晚例會"
    msg2 = channel_message({"channel_idx": 3, "text": "無名訊息"}, "", UTC)
    assert msg2.sender_line() == "(未具名)"
    assert msg2.header_left() == "#3"


class FakeMC:
    """最小 MeshCore 替身:收 subscribe、可手動 fire 事件。"""

    def __init__(self):
        self.self_info = {"name": "測試節點"}
        self.auto_update_contacts = False
        self.subs = []
        self.fetch_started = False
        self.commands = self

    def subscribe(self, event_type, callback, attribute_filters=None):
        self.subs.append((event_type, callback))
        return (event_type, callback)

    def unsubscribe(self, sub):
        if sub in self.subs:
            self.subs.remove(sub)

    async def ensure_contacts(self):
        return True

    async def start_auto_message_fetching(self):
        self.fetch_started = True

    def get_contact_by_key_prefix(self, prefix):
        return {"adv_name": "蜜蜂", "public_key": "a1b2c3" + "0" * 58}

    async def get_channel(self, idx):
        return Event(EventType.CHANNEL_INFO, {"channel_idx": idx, "channel_name": "公共頻道"})

    async def fire(self, event_type, payload):
        for etype, cb in list(self.subs):
            if etype == event_type:
                res = cb(Event(event_type, payload))
                if asyncio.iscoroutine(res):
                    await res


def test_session_wiring():
    got = []

    async def main():
        node = NodeClient(Config(), on_message=got.append)
        mc = FakeMC()
        stop = asyncio.Event()
        task = asyncio.create_task(node._session(mc, stop))
        await asyncio.sleep(0.01)  # 等訂閱與 auto-fetch 啟動
        assert mc.fetch_started
        assert mc.auto_update_contacts
        await mc.fire(EventType.CONTACT_MSG_RECV,
                      {"pubkey_prefix": "a1b2c3d4e5f6", "text": "dm 內容",
                       "sender_timestamp": 1767000000, "path_len": 255})
        await mc.fire(EventType.CHANNEL_MSG_RECV,
                      {"channel_idx": 0, "text": "蜜蜂 Bee: 頻道內容",
                       "sender_timestamp": 1767000001, "path_len": 2})
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(main())
    assert len(got) == 2
    dm, ch = got
    assert dm.kind == "dm" and dm.sender_name == "蜜蜂" and dm.text == "dm 內容"
    assert ch.kind == "channel" and ch.channel_name == "公共頻道"
    assert ch.sender_name == "蜜蜂 Bee" and ch.text == "頻道內容"


def test_session_ends_on_disconnect():
    async def main():
        node = NodeClient(Config(), on_message=lambda m: None)
        mc = FakeMC()
        stop = asyncio.Event()
        task = asyncio.create_task(node._session(mc, stop))
        await asyncio.sleep(0.01)
        await mc.fire(EventType.DISCONNECTED, {"reason": "reconnect_failed"})
        await asyncio.wait_for(task, 5)  # 不用 stop 也要自行結束

    asyncio.run(main())
