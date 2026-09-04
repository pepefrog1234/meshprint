"""命令列介面(規格 §6.8):preview / test / calibrate / status / run。

各子指令走的管線深度不同,方便分段除錯:
- preview   :訊息 → 版面 → PNG(免硬體;--prn 可同時輸出 ESC/P2 bytes)
- test      :訊息 → 版面 → ESC/P2 → lp 直接送印(不經節點、不經 spool 的煙霧測試)
- calibrate :校正頁 → 送印(--preview 改輸出 PNG)
- status    :印出設定檔、字型鏈、佇列、MQTT、節點裝置、spool 狀態
- run       :常駐(daemon.py)——真正的「收訊息 → 自動列印」

preview/test 用 _fake_message() 從命令列參數組一則假訊息,
與真實來源產生的 InboundMessage 走完全相同的版面與編碼程式。
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import __version__, config, escp2, fonts
from .model import InboundMessage
from .render import Renderer

log = logging.getLogger("meshprint")


def _msg_opts(p) -> None:
    """preview / test 共用的「假訊息」參數。"""
    p.add_argument("--dm", action="store_true", help="以私訊(DM)版面呈現")
    p.add_argument("--channel", type=int, default=0, metavar="IDX", help="頻道編號(預設 0)")
    p.add_argument("--channel-name", default="公共頻道", metavar="NAME")
    p.add_argument("--sender", default="測試 Test", metavar="NAME")
    p.add_argument("--prefix", default="a1b2c3", metavar="HEX", help="寄件者 pubkey 前綴")
    p.add_argument("--hops", type=int, default=None, metavar="N")
    p.add_argument("--time", default=None, metavar="ISO", help="接收時間(預設現在)")


def _fake_message(args, cfg) -> InboundMessage:
    """把命令列參數組成一則 InboundMessage(時間預設「現在」,設定的時區)。"""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.render.timezone)
    if args.time:
        dt = datetime.fromisoformat(args.time)
        dt = dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
    else:
        dt = datetime.now(tz)
    extra = {} if args.hops is None else {"path_len": args.hops}
    if args.dm:
        return InboundMessage(kind="dm", text=args.text, rx_time=dt,
                              sender_name=args.sender, sender_prefix=args.prefix, extra=extra)
    return InboundMessage(kind="channel", text=args.text, rx_time=dt,
                          sender_name=args.sender, sender_prefix=args.prefix,
                          channel_idx=args.channel, channel_name=args.channel_name,
                          extra=extra)


def _renderer(cfg) -> Renderer:
    return Renderer(cfg, fonts.resolve(cfg.render))


def _resolve_queue(name: str) -> str:
    """佇列解析失敗就直接結束程式並說明原因(CLI 用;daemon 有自己的容錯版)。"""
    from .spool import resolve_queue
    queue, why = resolve_queue(name)
    if queue is None:
        raise SystemExit(why + ";請接上印表機,或在設定檔 [printer] queue 指定。")
    return queue


def _send_raw(data: bytes, queue: str, title: str) -> int:
    """把 ESC/P2 bytes 寫成暫存檔,`lp -o raw` 送印(繞過 CUPS 驅動 filter)。"""
    with tempfile.NamedTemporaryFile(suffix=".prn", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        res = subprocess.run(["lp", "-d", queue, "-o", "raw", "-t", title, path],
                             capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("找不到 lp;此系統似乎沒有 CUPS。")
    finally:
        try:
            os.unlink(path)   # lp 已把檔案複製進 CUPS spool,暫存檔可刪
        except OSError:
            pass
    if res.returncode != 0:
        print("lp 失敗:{}".format((res.stderr or res.stdout).strip()), file=sys.stderr)
        return 1
    print("已送印 {} bytes → 佇列 {}:{}".format(len(data), queue, res.stdout.strip()))
    return 0


def cmd_preview(args, cfg) -> int:
    rend = _renderer(cfg)
    img = rend.ticket(_fake_message(args, cfg))
    img.save(args.out, dpi=(180, 180))   # 帶 dpi 資訊,看圖軟體會以實際尺寸顯示
    print("已輸出 {}({}×{} dots @180dpi,紙上約 {:.0f} mm 長)".format(
        args.out, img.width, img.height, img.height / 180 * 25.4))
    if args.prn:
        data = escp2.encode_job(img, cfg.printer)
        Path(args.prn).write_bytes(data)
        print("ESC/P2:{} bytes → {}".format(len(data), args.prn))
    return 0


def cmd_test(args, cfg) -> int:
    rend = _renderer(cfg)
    img = rend.ticket(_fake_message(args, cfg))
    data = escp2.encode_job(img, cfg.printer)
    if args.keep:
        Path(args.keep).write_bytes(data)
        print("ESC/P2:{} bytes → {}".format(len(data), args.keep))
    return _send_raw(data, _resolve_queue(cfg.printer.queue), "meshprint test")


def cmd_calibrate(args, cfg) -> int:
    rend = _renderer(cfg)
    img = rend.calibration()
    if args.preview:
        img.save(args.preview, dpi=(180, 180))
        print("已輸出 {}({}×{} dots)".format(args.preview, img.width, img.height))
        return 0
    data = escp2.encode_job(img, cfg.printer)
    return _send_raw(data, _resolve_queue(cfg.printer.queue), "meshprint calibrate")


def cmd_status(args, cfg) -> int:
    """不碰硬體的狀態總覽(不啟動來源,節點只看裝置檔在不在)。"""
    import glob as globmod

    from .node import PORT_GLOBS
    from .spool import Spool, resolve_queue

    print("meshprint {}".format(__version__))
    p = Path(args.config).expanduser() if args.config else config.DEFAULT_PATH.expanduser()
    print("設定檔:{}{}".format(p, "" if p.exists() else "(不存在,使用預設值)"))
    try:
        chain = fonts.resolve(cfg.render)
        print("字型鏈:{}".format(" → ".join(f.display for f in chain.faces)))
    except RuntimeError as e:
        print("字型:{}".format(e))
    queue, why = resolve_queue(cfg.printer.queue)
    print("佇列:{}".format(queue if queue else why))
    m = cfg.mqtt
    if m.enabled:
        chans = ", ".join(str(c.get("name", "?")) if isinstance(c, dict) else str(c)
                          for c in m.channels)
        print("MQTT:{}:{} {} 頻道[{}]".format(m.host, m.port, m.root, chans))
    else:
        print("MQTT:未啟用(設定檔 [mqtt] enabled = true 開啟)")
    if not cfg.node.enabled:
        print("節點裝置:已停用([node] enabled = false)")
    elif cfg.node.port == "auto":
        hits = [h for pat in PORT_GLOBS for h in sorted(globmod.glob(pat))]
        print("節點裝置:{}".format(", ".join(hits) if hits else "未偵測到(掃描 {})".format(
            " ".join(PORT_GLOBS))))
    else:
        exists = Path(cfg.node.port).exists()
        print("節點裝置:{}{}".format(cfg.node.port, "" if exists else "(不存在)"))
    sp = Spool(Path(cfg.spool.dir).expanduser(),
               keep_done=cfg.spool.keep_done, max_pending=cfg.spool.max_pending)
    print("spool:待印 {} 則、done 留存 {} 則({})".format(
        sp.pending_count(), sp.done_count(), sp.root))
    print("常駐:以 meshprint run 啟動(連線狀態見其 log)")
    return 0


def cmd_run(args, cfg) -> int:
    """常駐模式:先確認 meshcore 套件在(它要求 Python ≥ 3.10),再交給 daemon。"""
    try:
        import meshcore  # noqa: F401
    except ImportError:
        print("缺少 meshcore 套件:請在 Python ≥3.10 環境執行 pip install -e . 後再試。",
              file=sys.stderr)
        return 2
    import asyncio

    from . import daemon
    return asyncio.run(daemon.run_daemon(cfg))


def main(argv=None) -> int:
    """進入點(pyproject 的 console script 與 `python -m meshprint` 都到這裡)。"""
    ap = argparse.ArgumentParser(prog="meshprint",
                                 description="MeshCore 訊息 → Epson 點陣印表機 自動列印")
    ap.add_argument("--config", metavar="PATH",
                    help="設定檔路徑(預設 ~/.config/meshprint/config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version="meshprint {}".format(__version__))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preview", help="渲染版面 PNG(免硬體)")
    p.add_argument("text")
    p.add_argument("-o", "--out", default="preview.png", metavar="PNG")
    p.add_argument("--prn", metavar="PATH", help="同時輸出 ESC/P2 bytes")
    _msg_opts(p)
    p.set_defaults(func=cmd_preview)

    t = sub.add_parser("test", help="把文字走完整管線直接送印(硬體煙霧測試)")
    t.add_argument("text")
    t.add_argument("--keep", metavar="PATH", help="另存 ESC/P2 bytes")
    _msg_opts(t)
    t.set_defaults(func=cmd_test)

    c = sub.add_parser("calibrate", help="列印寬度標尺+全字級樣張")
    c.add_argument("--preview", metavar="PNG", help="改為輸出 PNG,不送印")
    c.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("status", help="顯示佇列/字型/spool 狀態")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="常駐模式(M3)")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    return args.func(args, cfg)
