"""Meshtastic MQTT 來源測試:PSK 展開、AES-CTR、信封解碼(全部離線)。"""
from __future__ import annotations

from datetime import timezone

from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2

from meshprint.config import Config, FilterConfig
from meshprint.filters import should_print
from meshprint.mqtt_source import DEFAULT_PSK, Decoder, decrypt_packet, expand_psk

TOPIC = "msh/TW/2/e/MediumFast/!deadbeef"


def test_expand_psk():
    assert expand_psk("AQ==") == DEFAULT_PSK                    # 0x01 = 預設鍵
    assert expand_psk("Ag==")[-1] == DEFAULT_PSK[-1] + 1        # 0x02 = 變體
    assert expand_psk("Ag==")[:-1] == DEFAULT_PSK[:-1]
    assert expand_psk("AA==") == b""                            # 0x00 = 不加密
    raw16 = bytes(range(16))
    import base64
    assert expand_psk(base64.b64encode(raw16).decode()) == raw16
    assert expand_psk(base64.b64encode(b"12345").decode()) is None  # 長度無效
    assert expand_psk("not-base64!!") is None


def _make_env(text="哈囉 台灣鏈網", channel="MediumFast", from_num=0x11223344,
              pid=1000, key=DEFAULT_PSK, portnum=portnums_pb2.TEXT_MESSAGE_APP,
              payload=None, rx_time=1767000000, hop_start=3, hop_limit=1,
              rx_snr=-7.25):
    data = mesh_pb2.Data(portnum=portnum,
                         payload=text.encode() if payload is None else payload)
    pkt = mesh_pb2.MeshPacket(**{"from": from_num}, id=pid, rx_time=rx_time,
                              hop_start=hop_start, hop_limit=hop_limit, rx_snr=rx_snr)
    pkt.encrypted = decrypt_packet(key, from_num, pid, data.SerializeToString())
    env = mqtt_pb2.ServiceEnvelope(packet=pkt, channel_id=channel,
                                   gateway_id="!deadbeef")
    return env.SerializeToString()


def _decoder():
    cfg = Config()
    return Decoder(cfg.mqtt, timezone.utc)


def test_default_channels_include_signaltest():
    d = _decoder()
    assert "SignalTest" in d.keys      # 台灣主力聊天頻道(業主確認)
    assert "MediumFast" in d.keys
    assert d.display["SignalTest"] == "MQTT TW/SignalTest"


def test_aes256_signaltest_roundtrip():
    d = _decoder()
    key = d.keys["SignalTest"]
    assert len(key) == 32  # 社群頻道用 AES-256
    env = _make_env(channel="SignalTest", key=key, pid=9000, text="訊號測試 5-9")
    msg = d.handle("msh/TW/2/e/SignalTest/!x", env)
    assert msg is not None
    assert msg.text == "訊號測試 5-9"
    assert msg.header_left() == "MQTT TW/SignalTest"


def test_text_message_decoded():
    d = _decoder()
    msg = d.handle(TOPIC, _make_env())
    assert msg is not None
    assert msg.kind == "channel" and msg.channel_idx is None
    assert msg.header_left() == "MQTT TW/MediumFast"
    assert msg.text == "哈囉 台灣鏈網"
    assert msg.sender_prefix == "223344"
    assert int(msg.msg_time.timestamp()) == 1767000000
    assert msg.extra["path_len"] == 2 and msg.extra["SNR"] == -7.25
    assert msg.extra_note() == "hops 2 · SNR -7.2"


def test_zero_hop_shows_direct():
    d = _decoder()
    msg = d.handle(TOPIC, _make_env(hop_start=3, hop_limit=3, pid=1001))
    assert msg.extra["path_len"] == -1
    assert msg.extra_note().startswith("直收")


def test_gateway_duplicates_dropped():
    d = _decoder()
    env = _make_env(pid=2000)
    assert d.handle(TOPIC, env) is not None
    assert d.handle(TOPIC, env) is None  # 另一台閘道器轉發同一包


def test_nodeinfo_builds_name_cache():
    d = _decoder()
    user = mesh_pb2.User(id="!11223344", long_name="北投中繼站", short_name="BT")
    info = _make_env(portnum=portnums_pb2.NODEINFO_APP,
                     payload=user.SerializeToString(), pid=3000)
    assert d.handle(TOPIC, info) is None
    msg = d.handle(TOPIC, _make_env(pid=3001))
    assert msg.sender_name == "北投中繼站"
    assert msg.sender_line() == "北投中繼站 <223344>"


def test_unconfigured_channel_skipped():
    d = _decoder()
    assert d.handle("msh/TW/2/e/MeshHK/!x", _make_env(channel="MeshHK", pid=4000)) is None


def test_wrong_key_skipped():
    d = _decoder()
    bad_key = bytes(range(16))
    assert d.handle(TOPIC, _make_env(key=bad_key, pid=5000)) is None


def test_non_envelope_topics_skipped():
    d = _decoder()
    env = _make_env(pid=6000)
    assert d.handle("msh/TW/2/json/MediumFast/!x", env) is None
    assert d.handle("msh/TW/2/map/!x", env) is None
    assert d.handle("msh/TW/2/stat/!x", env) is None
    # 子區域 topic 照收
    assert d.handle("msh/TW/north/2/e/MediumFast/!x", _make_env(pid=6001)) is not None


def test_position_packets_ignored():
    d = _decoder()
    env = _make_env(portnum=portnums_pb2.POSITION_APP, payload=b"\x0d\x01\x02",
                    pid=7000)
    assert d.handle(TOPIC, env) is None


def test_mqtt_message_passes_channel_idx_filter():
    d = _decoder()
    msg = d.handle(TOPIC, _make_env(pid=8000))
    # [filter] channels 的編號過濾不應擋掉 idx=None 的 MQTT 訊息
    assert should_print(msg, FilterConfig(channels=[0]))[0]
