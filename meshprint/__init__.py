"""MeshPrint:MeshCore / Meshtastic 網狀網路訊息 → Epson 點陣印表機 自動列印。

模組地圖(資料流由上而下):
    node.py         MeshCore USB 節點來源     ─┐
    mqtt_source.py  Meshtastic MQTT 來源      ─┴→ model.InboundMessage
    filters.py      要不要印([filter])
    dedup.py        去重(LRU,持久化)
    render.py       版面 + CJK 點陣化(fonts.py 提供字型鏈)→ 1-bit 影像
    escp2.py        影像 → ESC/P2 24-pin 圖形指令 bytes
    spool.py        磁碟 spool → lp -o raw → 追蹤 CUPS 印完
    daemon.py       以 asyncio 把上面串成常駐程式
    config.py       設定檔;cli.py 命令列進入點
"""
__version__ = "0.1.0"
