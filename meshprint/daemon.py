"""常駐程式(規格 M3):把各來源的訊息串成「過濾 → 去重 → 渲染 → 編碼 → spool → CUPS」。

工作原理
--------
整個程式是一個 asyncio 事件迴圈,裡面同時跑幾個任務(task):

    ┌ 來源(producer)─────────────┐
    │ NodeClient.run()  MeshCore 節點 │──┐
    │ MqttSource.run()  Meshtastic MQTT│──┤ inbox.put_nowait(InboundMessage)
    └────────────────────────────────┘  ▼
                                  inbox(asyncio.Queue)
                                        │
                                        ▼
    consumer():一次取一則 → handle_message()(在執行緒池裡跑,渲染是 CPU 工作)
              → 有入 spool 就立刻 tick_once()(即到即印)
    ticker():每 retry_seconds(30 秒)tick_once() 一次(補印、確認印完、重試)
    tick_once():解析佇列名 → Spool.tick()(在執行緒池裡跑,裡面會叫 lp/lpstat)

- 來源與消費者之間只有一個 Queue,來源永遠不會被慢速的印表機卡住;
  真正的積壓發生在磁碟 spool(有上限與保鮮期保護)。
- tick 用 asyncio.Lock 串行化:消費者觸發的「立刻印」與 30 秒定時的「巡邏」
  不會同時對 CUPS 動手。
- SIGINT / SIGTERM 只做一件事:設 stop 事件;所有任務看到 stop 就收尾,
  spool 裡還沒印的留在磁碟,下次啟動接著印(或依保鮮期略過)。

handle_message() 是同步純邏輯,tests/test_daemon.py 直接用假的 Ctx 測試。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from . import escp2, fonts
from . import spool as spoolmod
from .dedup import DedupLRU
from .filters import should_print
from .model import InboundMessage
from .node import NodeClient
from .render import Renderer, ascii_ticket

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    """handle_message 需要的所有依賴,打包成一個物件方便傳遞與測試替換。"""
    cfg: object
    rend: Renderer
    spool: spoolmod.Spool
    dedup: DedupLRU


def _brief(msg: InboundMessage) -> str:
    """log 用的一行摘要(不含內文,避免把聊天內容灌進 log)。"""
    return "{}|{}|{}字".format(msg.header_left(), msg.sender_line(), len(msg.text))


def _meta(msg: InboundMessage) -> dict:
    """寫進 spool .json 的中繼資料;title 會變成 CUPS 裡的 job 名稱。"""
    return {
        "kind": msg.kind,
        "channel": msg.header_left(),
        "sender": msg.sender_line(),
        "rx_time": msg.rx_time.isoformat(),
        "chars": len(msg.text),
        "title": "meshprint {}".format(msg.header_left()),
    }


def handle_message(msg: InboundMessage, ctx: Ctx) -> str:
    """一則訊息走到 spool 為止;回傳處置結果(供 log 與測試)。

    順序:過濾([filter] 設定)→ 去重(LRU,持久化)→ 渲染成票(失敗改
    ASCII 降級版面)→ 編碼成 ESC/P2 → 寫入 spool。若這次寫入觸發了風暴丟棄,
    再補一張單行警示票(不受上限限制),讓紙上看得出有訊息被丟。
    """
    ok, reason = should_print(msg, ctx.cfg.filter)
    if not ok:
        log.info("略過(%s):%s", reason, _brief(msg))
        return "filtered:" + reason
    if ctx.dedup.seen_or_add(DedupLRU.key_for(msg)):
        log.info("重覆訊息,丟棄:%s", _brief(msg))
        return "duplicate"
    try:
        img = ctx.rend.ticket(msg)
    except Exception:
        log.exception("渲染失敗,改用 ASCII 降級版面(§7)")
        try:
            img = ascii_ticket(msg, ctx.cfg)
        except Exception:
            log.exception("ASCII 降級也失敗,這則只留 log:%s", _brief(msg))
            return "render-error"
    data = escp2.encode_job(img, ctx.cfg.printer)
    _, dropped = ctx.spool.submit(data, _meta(msg))
    if dropped:
        try:
            warn = ctx.rend.notice("訊息風暴:已丟棄最舊 {} 則".format(dropped))
            ctx.spool.submit(escp2.encode_job(warn, ctx.cfg.printer),
                             {"kind": "notice", "title": "meshprint notice"},
                             enforce_cap=False)
        except Exception:
            log.exception("風暴警示票渲染失敗")
    log.info("已入 spool:%s", _brief(msg))
    return "spooled"


async def run_daemon(cfg) -> int:
    """`meshprint run` 的主體:建好所有元件,啟動任務,等 stop,收尾。"""
    rend = Renderer(cfg, fonts.resolve(cfg.render))
    root = Path(cfg.spool.dir).expanduser()
    sp = spoolmod.Spool(root, keep_done=cfg.spool.keep_done,
                        max_pending=cfg.spool.max_pending,
                        max_age_minutes=cfg.spool.max_age_minutes)
    ctx = Ctx(cfg, rend, sp, DedupLRU(256, root / "dedup.json"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass   # 某些平台(Windows)不支援,退回預設行為

    inbox: asyncio.Queue = asyncio.Queue()
    producers = []
    if cfg.node.enabled:
        node = NodeClient(cfg, on_message=inbox.put_nowait)
        producers.append(node.run(stop))
    else:
        log.info("MeshCore 節點來源已停用([node] enabled = false)")
    if cfg.mqtt.enabled:
        from .mqtt_source import MqttSource   # 需要 paho/meshtastic/cryptography,用到才載入
        producers.append(MqttSource(cfg, inbox.put_nowait).run(stop))
    if not producers:
        log.error("沒有任何訊息來源([node] 與 [mqtt] 皆停用),結束")
        return 2

    tick_lock = asyncio.Lock()
    last_queue_warn = [0.0]   # 用 list 包一層,讓內層函式能改它(closure 寫入)

    async def tick_once() -> None:
        """跑一回合 spool.tick;佇列不存在時只在有待印訊息且每 5 分鐘提醒一次。"""
        async with tick_lock:
            queue, why = spoolmod.resolve_queue(cfg.printer.queue)
            if queue is None:
                now = time.monotonic()
                if sp.pending_count() and now - last_queue_warn[0] > 300:
                    log.warning("spool 待印 %d 則但目前無佇列:%s", sp.pending_count(), why)
                    last_queue_warn[0] = now
                return
            stats = await asyncio.to_thread(sp.tick, queue)   # 會呼叫外部指令,別擋住迴圈
            if stats.submitted or stats.completed or stats.failed or stats.expired:
                log.info("spool:%s(佇列 %s)", stats, queue)

    async def consumer() -> None:
        """從 inbox 取訊息 → handle_message(執行緒池)→ 有入 spool 就立刻印。"""
        while True:
            msg = await inbox.get()
            result = await asyncio.to_thread(handle_message, msg, ctx)
            if result == "spooled":
                await tick_once()  # 即到即印,不等 30 秒輪詢

    async def ticker() -> None:
        """定時巡邏:補印、確認印完、重試;啟動時先跑一次以清積壓。"""
        while not stop.is_set():
            await tick_once()
            await NodeClient._sleep(stop, cfg.spool.retry_seconds)

    backlog = sp.pending_count()
    if backlog:
        log.info("啟動:spool 積壓 %d 則,將依序補印(§6.6)", backlog)
    log.info("meshprintd 啟動(Ctrl-C 結束)")

    tasks = [asyncio.create_task(p) for p in producers]
    tasks.append(asyncio.create_task(ticker()))
    consumer_task = asyncio.create_task(consumer())
    await stop.wait()
    log.info("收到停止訊號,關閉中…")
    consumer_task.cancel()   # consumer 是無限迴圈,要主動取消;其餘任務看到 stop 會自己結束
    for t in tasks + [consumer_task]:
        try:
            await t
        except asyncio.CancelledError:
            pass
    log.info("已結束;spool 待印 %d 則(下次啟動會補印)", sp.pending_count())
    return 0
