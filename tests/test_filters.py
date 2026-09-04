from __future__ import annotations

from datetime import datetime, timezone

from meshprint.config import FilterConfig
from meshprint.filters import should_print
from meshprint.model import InboundMessage


def _msg(kind="channel", idx=0, name="蜜蜂", prefix="a1b2c3d4e5f6"):
    return InboundMessage(kind=kind, text="hi", rx_time=datetime.now(timezone.utc),
                          sender_name=name, sender_prefix=prefix, channel_idx=idx)


def test_dm_toggle():
    assert should_print(_msg(kind="dm"), FilterConfig())[0]
    assert not should_print(_msg(kind="dm"), FilterConfig(print_dm=False))[0]


def test_channel_list():
    fcfg = FilterConfig(channels=[0, 2])
    assert should_print(_msg(idx=0), fcfg)[0]
    assert not should_print(_msg(idx=1), fcfg)[0]
    assert should_print(_msg(idx=2), fcfg)[0]
    assert should_print(_msg(idx=7), FilterConfig(channels="all"))[0]


def test_ignore_senders_by_name_and_prefix():
    fcfg = FilterConfig(ignore_senders=["蜜蜂", "deadbe"])
    assert not should_print(_msg(name="蜜蜂"), fcfg)[0]
    assert not should_print(_msg(name="別人", prefix="deadbeef0000"), fcfg)[0]
    assert should_print(_msg(name="別人", prefix="a1b2c3d4e5f6"), fcfg)[0]
    # 名稱大小寫不敏感
    assert not should_print(_msg(name="Bee"), FilterConfig(ignore_senders=["bee"]))[0]
