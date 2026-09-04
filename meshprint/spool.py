"""磁碟 spool(規格 §6.6):訊息落地 → 送 CUPS → 追蹤印完 → 歸檔。

工作原理
--------
CUPS 只是「送資料到印表機的管子」,本 spool 才是訊息的真正主人:
每則訊息編碼成 ESC/P2 之後先寫到磁碟,確認印表機真的印完才算數。
這樣印表機離線、缺紙、程式重啟都不會弄丟訊息。

檔案佈局(根目錄預設 ~/.meshprint):
    spool/<UTC時間>-<序號>.prn   ESC/P2 原始 bytes(一則訊息 = 一個自足 job)
    spool/<同名>.json            中繼資料:來源、寄件者、job_id、送出時間…
    done/…                        已確認印完的成對檔案(保留最新 keep_done 份)

一則訊息的狀態機:
    待送(json 無 job_id)──lp 成功──▶ 已送(json 有 job_id)──CUPS 印完──▶ done/
      │                                   │
      ├─ 超過 max_pending 則:最舊的被丟棄(訊息風暴,§7)
      └─ 超過 max_age_minutes 未能送印:略過(業主設定;離線過夜不補印)

tick()(每 30 秒一次,收到新訊息也立刻一次)每回合做的事,依序:
1. 逾時略過(max_age_minutes > 0 時);
2. 若有待送訊息:查佇列狀態(lpoptions)與 USB 裝置在不在(lpinfo)——
   印表機不在線就整回合不送(避免失敗 job 讓 macOS 把佇列暫停),
   佇列已被暫停(printer-state=5)就先 cupsenable 救回;
3. 逐一處理:已送的 → 查 job 是否已從「未完成」清單消失 → 消失就歸檔;
   待送的 → lp -o raw 送出,記下 job id。lp 失敗就停止本回合(佇列有問題,
   別再灌),留待下回合重試;
4. 修剪 done/ 只留最新 keep_done 份。

「印完」的判定:job id 從 `lpstat -W not-completed -o <佇列>` 消失。
這比規格寫的「lp 回傳成功」嚴格,能撐過 macOS 拔線後自動暫停佇列的情境(T-5)。

與 CUPS 對話的原則:只信「不隨語系變化的 token」——佇列名(lpstat -e)、
job id、lpoptions 的 key=value、URI——因為 macOS 的 lpstat 說明文字會在地化,
連 LANG=C 都壓不住,解析文字會炸。
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

STALE_SECONDS = 600        # job 卡在 CUPS 超過此秒數 → 告警(佇列可能暫停)
OFFLINE_WARN_EVERY = 300   # 印表機離線的提醒間隔(秒)


def resolve_queue(name: str) -> Tuple[Optional[str], str]:
    """回傳 (佇列名或 None, 說明)。lpstat -e 只輸出名稱,不受語系影響。

    "auto":系統剛好只有一個列印目的地就用它;零個或多個都回 None 並說明原因。
    """
    if name != "auto":
        return name, "設定指定"
    try:
        res = subprocess.run(["lpstat", "-e"], capture_output=True, text=True)
    except FileNotFoundError:
        return None, "找不到 lpstat;此系統似乎沒有 CUPS"
    names = [l.strip() for l in res.stdout.splitlines() if l.strip()]
    if res.returncode != 0 or not names:
        return None, "找不到任何 CUPS 印表機佇列"
    if len(names) == 1:
        return names[0], "自動偵測"
    return None, "有多個列印目的地({}),請在設定檔 [printer] queue 指定其一".format(", ".join(names))


@dataclass
class TickStats:
    """一回合 tick 的統計(給 log 與測試看)。"""
    submitted: int = 0   # 本回合送進 CUPS 的
    completed: int = 0   # 本回合確認印完、歸檔的
    waiting: int = 0     # 還在等(CUPS 印製中,或印表機離線暫不送)
    failed: int = 0      # lp 失敗
    expired: int = 0     # 逾時略過

    def __str__(self) -> str:
        s = "送出 {}、完成 {}、等待 {}、失敗 {}".format(
            self.submitted, self.completed, self.waiting, self.failed)
        if self.expired:
            s += "、逾時略過 {}".format(self.expired)
        return s


class Spool:
    def __init__(self, root: Path, keep_done: int = 200, max_pending: int = 100,
                 max_age_minutes: int = 0):
        self.root = Path(root).expanduser()
        self.spool_dir = self.root / "spool"
        self.done_dir = self.root / "done"
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.done_dir.mkdir(parents=True, exist_ok=True)
        self.keep_done = keep_done
        self.max_pending = max_pending
        self.max_age_minutes = max_age_minutes  # >0:逾時未送印即略過(0 = 永不過期)
        self._seq = itertools.count()           # 同一秒內多則訊息的排序用序號
        self._lock = threading.Lock()           # submit 與 tick 可能在不同執行緒,互斥
        self._last_offline_warn = 0.0

    # ---- 提交 ----

    def submit(self, data: bytes, meta: dict, enforce_cap: bool = True) -> Tuple[Path, int]:
        """寫入 spool(prn + json 中繼資料);回傳 (路徑, 因風暴丟棄的則數)。

        檔名用 UTC 時間 + 序號,字串排序就是時間順序,tick 依此順序送印。
        先寫暫存檔再 os.replace 原子改名:程式若在寫到一半時崩潰,
        不會留下「半個 .prn」被誤送印。
        enforce_cap=False 供系統警示票使用,避免警示票自己把真訊息擠出上限。
        """
        with self._lock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.spool_dir / "{}-{:04d}.prn".format(stamp, next(self._seq))
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._save_meta(path, dict(meta))
            dropped = self._enforce_cap() if enforce_cap else 0
        if dropped:
            log.warning("訊息風暴:spool 超過 %d 則,已丟棄最舊 %d 則(§7)",
                        self.max_pending, dropped)
        return path, dropped

    def _enforce_cap(self) -> int:
        """訊息風暴保護(§7):待送數超過 max_pending 就從最舊的開始丟。"""
        pending = self._pending()
        dropped = 0
        # 只丟還沒進 CUPS 的(已送出的反正會印,丟檔案也止不住)
        droppable = [p for p in pending if not self._load_meta(p).get("job_id")]
        while len(pending) > self.max_pending and droppable:
            victim = droppable.pop(0)
            pending.remove(victim)
            self._unlink_pair(victim)
            dropped += 1
        return dropped

    # ---- 列印回合 ----

    def tick(self, queue: str) -> TickStats:
        """送出未送的、確認已送的;每 30 秒與每次 submit 後各跑一次。

        冷啟動保護:印表機(USB)不在線時不把 job 丟進 CUPS——失敗的 job 會讓
        macOS 把佇列暫停且不自動恢復;留在本 spool,偵測到開機再送。
        若佇列已被暫停(printer-state=5),先 cupsenable 自動恢復。
        """
        stats = TickStats()
        with self._lock:
            not_done = self._cups_not_completed(queue)   # CUPS 還沒印完的 job id 集合
            pending = self._pending()
            stats.expired = self._expire(pending)
            submit_ok = True
            if any(not self._load_meta(p).get("job_id") for p in pending):
                # 只有真的有東西要送時才查裝置狀態(兩個外部指令,約 0.2 秒)
                info = self._queue_info(queue)
                present = self._device_present(info.get("device-uri", ""))
                if present is False:
                    submit_ok = False
                    now = time.monotonic()
                    if now - self._last_offline_warn > OFFLINE_WARN_EVERY:
                        log.warning("印表機未上線(USB 未偵測到),%d 則留在 spool 等待開機",
                                    sum(1 for p in pending
                                        if not self._load_meta(p).get("job_id")))
                        self._last_offline_warn = now
                elif info.get("printer-state") == "5":
                    self._enable_queue(queue)
            for prn in pending:
                meta = self._load_meta(prn)
                job_id = meta.get("job_id")
                if job_id:
                    # 已送進 CUPS:看它印完了沒
                    if not_done is None:
                        stats.waiting += 1  # lpstat 掛了:狀態未知,不動
                    elif job_id in not_done:
                        stats.waiting += 1
                        self._maybe_warn_stale(prn, meta)
                    else:
                        self._finish(prn)   # 從「未完成」清單消失 = 印完 → 歸檔
                        stats.completed += 1
                    continue
                if not submit_ok:
                    stats.waiting += 1
                    continue
                ok, new_id, err = self._lp(prn, queue, meta)
                if not ok:
                    stats.failed += 1
                    log.warning("lp 失敗(%s 留在 spool,稍後重試):%s", prn.name, err)
                    break  # 佇列有問題,同回合別再灌
                stats.submitted += 1
                if new_id:
                    meta["job_id"] = new_id
                    meta["submitted_at"] = datetime.now(timezone.utc).isoformat()
                    self._save_meta(prn, meta)
                else:
                    # 解析不到 job id 就無從追蹤,視同完成(僅記 log)
                    log.info("lp 成功但無法解析 job id,%s 視同完成", prn.name)
                    self._finish(prn)
                    stats.completed += 1
            self._prune_done()
        return stats

    def _expire(self, pending: list) -> int:
        """max_age_minutes > 0 時,把逾時仍未送進 CUPS 的訊息直接略過(不印)。

        印表機在線時訊息幾秒內就送印,不會逾時;只有離線期間(關機過夜等)
        累積的才會過期——業主要求:早上開機不要補印睡覺期間錯過的訊息。
        年齡以 .prn 檔的 mtime 計(= 進 spool 的時間 ≈ 接收時間)。
        """
        if self.max_age_minutes <= 0:
            return 0
        cutoff = time.time() - self.max_age_minutes * 60
        expired = [p for p in pending
                   if not self._load_meta(p).get("job_id")
                   and p.stat().st_mtime < cutoff]
        for prn in expired:
            self._unlink_pair(prn)
            pending.remove(prn)
        if expired:
            log.info("略過 %d 則逾時訊息(在 spool 超過 %d 分鐘未能送印,"
                     "通常是印表機關機期間累積)", len(expired), self.max_age_minutes)
        return len(expired)

    # ---- 與 CUPS 對話(只解析不受語系影響的 token)----

    def _queue_info(self, queue: str) -> dict:
        """lpoptions -p 輸出 key=value(不受系統語系影響)。

        會用到的 key:device-uri(usb://EPSON/LQ-310?serial=…)、
        printer-state(3 = 閒置、4 = 列印中、5 = 已停止/暫停)。
        """
        try:
            res = subprocess.run(["lpoptions", "-p", queue],
                                 capture_output=True, text=True)
        except FileNotFoundError:
            return {}
        if res.returncode != 0:
            return {}
        info = {}
        for token in res.stdout.split():
            if "=" in token:
                key, _, value = token.partition("=")
                info[key] = value
        return info

    def _device_present(self, device_uri: str):
        """USB 印表機是否在線;非 USB(網路佇列等)回 None 不做檢查。

        用 `lpinfo --include-schemes usb -v` 只列 USB 後端(0.1 秒;不限定 scheme
        會去掃網路,要 15 秒)。比對時去掉 ?serial=… 之後的部分,避免格式差異。
        """
        if not device_uri.startswith("usb://"):
            return None
        base = device_uri.split("?", 1)[0]
        try:
            res = subprocess.run(["lpinfo", "--include-schemes", "usb", "-v"],
                                 capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        if res.returncode != 0:
            return None
        return any(base in line for line in res.stdout.splitlines())

    def _enable_queue(self, queue: str) -> None:
        """佇列被暫停/拒收時恢復(對已啟用的佇列執行是無害的 no-op)。"""
        for cmd in (["cupsenable", queue], ["cupsaccept", queue]):
            try:
                subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError:
                return
        log.info("佇列 %s 曾被系統暫停,已自動恢復(cupsenable)", queue)

    def _lp(self, prn: Path, queue: str, meta: dict):
        """送印:lp -d <佇列> -o raw(raw = 繞過驅動 filter,bytes 原樣送到印表機)。

        回傳 (成功?, job id 或 None, 錯誤訊息)。job id 用「佇列名-數字」的
        pattern 從 stdout 抓,不管前面的說明文字是什麼語言。
        """
        title = meta.get("title") or "meshprint {}".format(prn.stem)
        try:
            res = subprocess.run(
                ["lp", "-d", queue, "-o", "raw", "-t", title, str(prn)],
                capture_output=True, text=True)
        except FileNotFoundError:
            return False, None, "找不到 lp"
        if res.returncode != 0:
            return False, None, (res.stderr or res.stdout).strip()
        m = re.search(r"\b({}-\d+)\b".format(re.escape(queue)), res.stdout)
        return True, (m.group(1) if m else None), ""

    def _cups_not_completed(self, queue: str):
        """CUPS 裡還沒印完的 job id 集合(每行第一個 token);查不到回 None。"""
        try:
            res = subprocess.run(["lpstat", "-W", "not-completed", "-o", queue],
                                 capture_output=True, text=True)
        except FileNotFoundError:
            return None
        if res.returncode != 0:
            return None
        ids = set()
        for line in res.stdout.splitlines():
            parts = line.split()
            if parts:
                ids.add(parts[0])
        return ids

    def _maybe_warn_stale(self, prn: Path, meta: dict) -> None:
        """已送進 CUPS 卻卡很久的 job,提醒一次(通常是佇列被暫停或缺紙)。"""
        submitted = meta.get("submitted_at")
        if not submitted or meta.get("stale_warned"):
            return
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(submitted)).total_seconds()
        except ValueError:
            return
        if age > STALE_SECONDS:
            log.warning("job %s 已卡在 CUPS %d 秒——佇列可能暫停"
                        "(缺紙/離線後 macOS 會自動暫停;可用 cupsenable <佇列> 恢復)",
                        meta.get("job_id"), int(age))
            meta["stale_warned"] = True
            self._save_meta(prn, meta)

    # ---- 檔案雜務 ----

    def _pending(self):
        """spool 內所有 .prn,依檔名(= 時間)排序。"""
        return sorted(self.spool_dir.glob("*.prn"))

    def pending_count(self) -> int:
        return len(self._pending())

    def done_count(self) -> int:
        return len(list(self.done_dir.glob("*.prn")))

    def _meta_path(self, prn: Path) -> Path:
        return prn.with_suffix(".json")

    def _load_meta(self, prn: Path) -> dict:
        try:
            return json.loads(self._meta_path(prn).read_text("utf-8"))
        except Exception:
            return {}

    def _save_meta(self, prn: Path, meta: dict) -> None:
        """寫中繼資料(同樣先寫暫存檔再原子改名)。"""
        mp = self._meta_path(prn)
        tmp = mp.with_suffix(".jtmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, mp)

    def _finish(self, prn: Path) -> None:
        """印完:成對移到 done/。"""
        for p in (prn, self._meta_path(prn)):
            if p.exists():
                os.replace(p, self.done_dir / p.name)

    def _unlink_pair(self, prn: Path) -> None:
        for p in (prn, self._meta_path(prn)):
            try:
                p.unlink()
            except OSError:
                pass

    def _prune_done(self) -> None:
        """done/ 只保留最新 keep_done 份(依檔名時間序,刪最舊的)。"""
        done = sorted(self.done_dir.glob("*.prn"))
        for prn in done[:max(0, len(done) - self.keep_done)]:
            self._unlink_pair(self.done_dir / prn.name)
