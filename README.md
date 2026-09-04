# MeshPrint

**網狀網路的電傳打字機**:把 MeshCore 節點與 Meshtastic 台灣 MQTT 頻道收到的訊息,
即時印到 Epson LQ-310 點陣印表機的連續報表紙上——離線、可留存、斷電紀錄還在紙上,
就像傳統 RTTY / 電報收報機。

完整規格見 [SPEC.md](SPEC.md)(v1.0 已凍結;§12 為 v1.1 增補的 MQTT 來源)。

---

## 目錄

1. [功能總覽](#功能總覽)
2. [系統架構](#系統架構)
3. [工作原理](#工作原理)
4. [硬體與環境需求](#硬體與環境需求)
5. [安裝](#安裝)
6. [快速開始(印表機到貨 runbook)](#快速開始印表機到貨-runbook)
7. [命令列用法](#命令列用法)
8. [設定檔](#設定檔)
9. [Meshtastic MQTT 台灣頻道](#meshtastic-mqtt-台灣頻道)
10. [日常運作行為](#日常運作行為)
11. [疑難排解](#疑難排解)
12. [測試](#測試)
13. [專案狀態與里程碑](#專案狀態與里程碑)
14. [與規格的已知差異](#與規格的已知差異)

---

## 功能總覽

- **兩種訊息來源,可同時或單獨啟用**
  - MeshCore 節點(USB serial、companion radio 韌體):私訊 + 頻道訊息,拔線自動重連。
  - Meshtastic MQTT(臺灣鏈網公共 broker):SignalTest、MeshTW、Emergency! 等台灣頻道,
    自帶解密金鑰,純唯讀。
- **單據式版面**:每則訊息一張「票」——來源/時間、寄件者/hops/SNR、分隔線、內文自動換行。
- **繁中/日/韓/英數全靠軟體點陣化**:印表機沒有中文字庫,整張票在電腦端畫成 1-bit
  點陣圖,以 ESC/P2 24-pin 圖形模式輸出;缺字沿字型鏈備援,Emoji 印「□」。
- **可靠性**:磁碟 spool、追蹤 CUPS 真的印完才歸檔、印表機關機不丟訊息、開機自動補印
  (或依保鮮期略過)、佇列被 macOS 暫停自動救回、去重持久化、訊息風暴保護、渲染失敗
  ASCII 降級。
- **過濾**:私訊開關、頻道清單、忽略名單(名稱或節點前綴)。
- **免硬體開發**:PNG 預覽、golden bytes 測試、假 CUPS / 假 MeshCore 物件——56 項測試
  全部不需要印表機或節點。

---

## 系統架構

```
 MeshCore 節點 ──USB serial──▶ node.py ──────┐
 (companion_radio_usb)                       │
                                             ├─▶ InboundMessage(model.py)
 mqtt.meshtastic.org ──MQTT──▶ mqtt_source.py┘        │
 msh/TW/#  (protobuf + AES-CTR 解密)                   ▼
                                       filters.py  要不要印?
                                             │
                                       dedup.py    看過了嗎?(LRU 256,持久化)
                                             │
                                       render.py   版面 + CJK 點陣化 ──▶ 1-bit 影像
                                       (fonts.py 字型鏈)                  │
                                       escp2.py    影像 → ESC/P2 bytes    │
                                             │                            │
                                       spool.py    落地 ~/.meshprint/spool/*.prn
                                             │       lp -o raw → CUPS 佇列 LQ310
                                             │       lpstat 確認印完 → done/
                                             ▼
                                       Epson LQ-310(USB,raw 佇列)
```

| 模組 | 職責 | 純函式/可離線測試 |
|---|---|---|
| `meshprint/model.py` | 統一訊息物件 `InboundMessage` 與票頭字串 | ✔ |
| `meshprint/node.py` | MeshCore 連線監督、事件 → 訊息、頻道寄件者拆解 | 對應部分 ✔ |
| `meshprint/mqtt_source.py` | MQTT 訂閱、ServiceEnvelope 解析、AES-CTR 解密、節點名稱快取 | `Decoder` ✔ |
| `meshprint/filters.py` | `[filter]` 規則 | ✔ |
| `meshprint/dedup.py` | 訊息指紋 LRU,寫 `dedup.json` | ✔ |
| `meshprint/fonts.py` | 字型鏈:cmap 缺字偵測、備援、寬度快取 | ✔ |
| `meshprint/render.py` | 票 / 警示票 / 校正頁 / ASCII 降級版面 | ✔ |
| `meshprint/escp2.py` | 1-bit 影像 → ESC/P2 圖形帶 | ✔(golden bytes) |
| `meshprint/spool.py` | 磁碟 spool 狀態機、與 CUPS 對話 | ✔(假 CUPS) |
| `meshprint/daemon.py` | asyncio 串接:來源 → 佇列 → 消費者 → spool 巡邏 | `handle_message` ✔ |
| `meshprint/config.py` | TOML 設定與預設值 | ✔ |
| `meshprint/cli.py` | `meshprint` 命令列 | — |

每個模組檔案頂部都有「工作原理」的詳細繁中註解,想深入直接看原始碼。

---

## 工作原理

### 1. 訊息統一模型
所有來源先轉成 `InboundMessage`(種類、內文、接收時間、寄件者名稱/前綴、頻道、
發送端時戳、hops/SNR 等附加資訊)。之後的每一級都只認這個物件,不知道訊息從哪來。

### 2. 過濾與去重
- `filters.should_print()`:私訊開關 → 頻道編號清單(只約束 MeshCore 頻道)→ 忽略名單。
- `dedup.DedupLRU`:指紋 = `種類 | 寄件者或頻道 | 發送端時戳 | sha1(內文)[:8]`,
  最近 256 則 LRU,每次更新寫 `~/.meshprint/dedup.json`,重啟不重印。
  MQTT 來源另外在更前面用封包 `(from, id)` 擋掉多台閘道器的重複轉發。

### 3. 版面與點陣化(render.py + fonts.py)
- 畫布 = Pillow mode `"1"`(1 bit/像素),寬 = `width_dots`(1 點 = 1/180 吋),
  `draw.fontmode = "1"` 強制單色渲染、關閉反鋸齒——撞針只有打/不打,灰階只會變髒點。
- 逐字元量測、逐字元繪製:每個字先問字型鏈「誰有這個字」,主字型缺就用備援,
  全缺印「□」;量測與繪製共用同一組寬度快取,所以算出來的行寬就是畫出來的行寬。
- 行高 = 字型 ascent + descent,基線對齊,混用字型不會上下裁切。
- 內文字級硬下限 24 px:180 dpi 下漢字低於 24×24 點,筆畫密的字會糊。

### 4. ESC/P2 編碼(escp2.py)
- 影像每 24 列切成一「帶」;帶內逐欄打包 3 bytes(byte0 bit7 = 最上針,1 = 擊針)。
- `ESC * 39 nL nH` 送圖形(m=39:24 針、180×180 dpi),`CR` 歸位,`ESC J 24` 走紙一帶。
- 全白帶只走紙不送資料;每帶左右全白裁掉並用 `ESC $`(1/60 吋單位)定位起印,
  減少資料量與印字頭空跑。
- 每則訊息是自足 job:`ESC @` 初始化、`ESC U 1` 單向列印(圖形帶才對得齊)、
  圖形帶、走紙 `feed_after_lines` 行、(選配)換頁。

### 5. spool 與 CUPS(spool.py)
- 每則訊息 = `spool/<UTC時間>-<序號>.prn` + 同名 `.json` 中繼資料;先寫暫存檔再原子改名。
- 狀態機:待送 → (`lp -o raw`,記下 job id)已送 → (job id 從
  `lpstat -W not-completed` 消失)→ `done/`(留最新 200 份)。
- 每 30 秒巡邏一次,收到新訊息也立刻跑一次(即到即印)。
- 只解析不受語系影響的 token(佇列名、job id、`lpoptions` 的 key=value、URI),
  因為 macOS 的 lpstat 說明文字會在地化。

### 6. 常駐程式(daemon.py)
一個 asyncio 迴圈:來源任務把訊息丟進 `asyncio.Queue`,消費者取出後在執行緒池做
過濾/去重/渲染/編碼/寫 spool,再觸發一次 spool 巡邏;定時任務每 30 秒巡邏。
`Ctrl-C` / `SIGTERM` 設 stop 事件,所有任務收尾,spool 裡未印的留在磁碟。

---

## 硬體與環境需求

| 項目 | 需求 |
|---|---|
| 印表機 | Epson LQ-310(或任何 ESC/P2 相容 24-pin 機型),USB,連續報表紙 |
| 節點(選配) | 刷 `companion_radio_usb` 韌體的 MeshCore 相容板,USB 接上出現 `/dev/cu.usbmodem*` |
| 作業系統 | macOS(主要目標);程式碼保持 Linux 相容 |
| Python | ≥ 3.10(`meshcore` 套件硬性要求) |
| 相依套件 | Pillow、fonttools、meshcore、paho-mqtt、meshtastic、cryptography(pyproject 自動安裝) |
| 字型 | Noto Sans CJK TC(已裝在 `~/Library/Fonts/NotoSansCJKtc-Regular.otf`);找不到時自動改用系統字型鏈 |
| CUPS 佇列 | `LQ310`(raw 佇列;本系統全部走 `-o raw`,驅動無關) |

---

## 安裝

**本機環境已就緒,不用安裝任何東西**——專案 `.venv` 內建 CPython 3.12 與全部依賴,
所有指令用 `.venv/bin/meshprint` 執行即可。

背景:系統 Python 只有 3.9,而 `meshcore` 要求 ≥ 3.10,因此 `.venv` 是用
[uv](https://docs.astral.sh/uv/) 佈建的 standalone CPython 3.12。只有在**重建環境**時
才需要 uv(已裝在 `~/Library/Python/3.9/bin/uv`,不在 PATH 內,用完整路徑呼叫):

```bash
~/Library/Python/3.9/bin/uv venv .venv --python 3.12
```

```bash
~/Library/Python/3.9/bin/uv pip install --python .venv/bin/python -e ".[dev]"
```

其他機器(已有 Python ≥ 3.10):`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`。

---

## 快速開始(印表機到貨 runbook)

以下指令都在專案資料夾內執行(先 `cd ~/Documents/print`);或把 `.venv/bin/meshprint`
換成完整路徑 `~/Documents/print/.venv/bin/meshprint`,就能在任何位置執行。

1. 接上 LQ-310 USB、裝連續報表紙、開機。
2. 確認 CUPS 佇列:

   ```bash
   lpstat -e
   ```

   應列出 `LQ310`。若是空的(macOS 不會自動幫這種老 USB 印表機建佇列),手動建一次
   即永久有效:

   ```bash
   lpadmin -p LQ310 -E -v "usb://EPSON/LQ-310?serial=001012607140937330"
   ```

   (序號以 `lpinfo --include-schemes usb -v` 查到的為準。)
3. 印校正頁,量標尺(吋距 25.4 mm、總誤差 < 1 mm)、看全寬實線左右有沒有被裁、看四種字級是否清晰:

   ```bash
   .venv/bin/meshprint calibrate
   ```

   字壓到紙的右側虛線 → 在設定檔縮小 `[printer] width_dots`(本機已設 1332 = 7.4 吋)。
4. 印一張真票驗證完整管線:

   ```bash
   .venv/bin/meshprint test "LQ-310 上線!繁中かな한글 123"
   ```

5. 開常駐:

   ```bash
   .venv/bin/meshprint run
   ```

   前景執行,`Ctrl-C` 結束。節點隨時插上 USB 就會自動偵測;MQTT 依設定檔啟用。
   **改了設定檔或更新程式後要重啟 `run` 才生效。**

---

## 命令列用法

| 指令 | 行為 |
|---|---|
| `meshprint run` | 常駐:收訊息 → 自動列印(節點 + MQTT 依設定) |
| `meshprint status` | 設定檔、字型鏈、佇列、MQTT、節點裝置、spool 待印/留存數 |
| `meshprint preview "文字" -o out.png` | 免硬體渲染票的 PNG;`--dm`、`--sender`、`--hops`、`--time` 可模擬欄位;`--prn 檔` 同時輸出 ESC/P2 bytes |
| `meshprint test "文字"` | 不經節點、不經 spool,直接送印(煙霧測試);`--keep 檔` 另存 bytes |
| `meshprint calibrate` | 印校正頁;`--preview c.png` 改輸出 PNG |
| `meshprint --config 路徑 …` | 指定設定檔;`-v` 顯示 debug log;`--version` |

---

## 設定檔

位置 `~/.config/meshprint/config.toml`;不存在時全部用預設值。只需要寫想覆蓋的欄位。
**本機目前的設定**(業主實機校正後):

```toml
[printer]
width_dots = 1332        # 7.4 吋(預設 1440 = 8.0 吋會壓到報表紙右側虛線)
left_margin_dots = 24

[mqtt]
enabled = true           # 印 Meshtastic 台灣頻道

[filter]
ignore_senders = ["9e8780", "a70eac"]   # 忽略這些節點(票上 <> 內的前綴或顯示名稱)

[spool]
max_age_minutes = 10     # 超過 10 分鐘沒能送印的訊息略過(睡覺關印表機,早上不補印)
```

完整欄位與預設值:

```toml
[node]                    # MeshCore 節點來源
enabled = true
transport = "serial"
port = "auto"             # 或 "/dev/cu.usbmodem14401"
baud = 115200

[mqtt]                    # Meshtastic MQTT 來源(見下一節)
enabled = false
host = "mqtt.meshtastic.org"
port = 1883
username = "meshdev"      # 官方公開帳密
password = "large4cats"
tls = false
root = "msh/TW"
label = "MQTT"            # 票頭來源標示
# [[mqtt.channels]] 見下一節;不寫就用內建的臺灣鏈網頻道組

[printer]
queue = "auto"            # CUPS 佇列名;"auto" = 系統唯一的那個
width_dots = 1440         # 可印寬度(點;1 點 = 1/180 吋)
left_margin_dots = 24     # 左右對稱邊界
feed_after_lines = 4      # 每張票後走紙幾行(1 行 = 1/6 吋)
form_feed = false         # 連續紙不換頁

[render]
font = "~/Library/Fonts/NotoSansCJKtc-Regular.otf"
font_index = 0            # .ttc 集合檔的 face 索引
fallback_fonts = []       # 備援字型:"路徑" 或 "路徑#索引"
body_px = 28              # 內文字級(下限 24)
header_px = 24            # 標題/寄件者列字級
max_body_lines = 40
timezone = "Asia/Taipei"
time_format = "%Y-%m-%d %H:%M:%S"

[filter]
print_dm = true
channels = "all"          # 或 [0, 1](只約束 MeshCore 頻道編號)
ignore_senders = []       # 顯示名稱(不分大小寫)或 hex 前綴(開頭比對)

[spool]
dir = "~/.meshprint"      # spool/、done/、dedup.json 的根目錄
keep_done = 200           # done/ 保留份數
max_pending = 100         # 訊息風暴上限,超過丟最舊
retry_seconds = 30        # 巡邏/重試間隔
max_age_minutes = 0       # >0:逾時未送印即略過;0 = 永久補印
```

---

## Meshtastic MQTT 台灣頻道

臺灣鏈網(Meshtastic Taiwan Community)使用官方公共 broker `mqtt.meshtastic.org`,
root topic `msh/TW`(`mqtt.meshtastic.tw` 這個網域不存在)。程式訂閱 `msh/TW/#`,
從 ServiceEnvelope 解出 MeshPacket,用頻道金鑰 AES-CTR 解密,只把 TEXT 訊息印出來。

內建頻道組(金鑰解自社群公開發布的頻道 QR,2026-01 版):

| 頻道 | 金鑰 | 角色 |
|---|---|---|
| **SignalTest** | 社群 AES-256 | 臺灣最熱絡的聊天頻道(主印) |
| MeshTW | 社群 AES-256 | 社群頻道 |
| Emergency! | 社群 AES-256 | 緊急頻道 |
| MediumFast | 預設鍵 `AQ==` | 主頻道,幾乎都是遙測/位置(非 TEXT 一律丟棄) |
| LongFast | 預設鍵 `AQ==` | 全球預設頻道 |

只想印特定頻道、或要加其他頻道/自訂金鑰,在設定檔列出即可(寫了就取代內建清單):

```toml
[[mqtt.channels]]
name = "SignalTest"
psk = "y1HciVgpl5Hzh05KJUe/umWUH8XhG3UjR1rvZHfUHFU="

[[mqtt.channels]]
name = "某頻道"
psk = "AQ=="              # 1 byte = 預設鍵變體;16/32 bytes base64 = AES-128/256
```

行為要點:純唯讀(永不 publish);多閘道器重複轉發以封包 id 去重;寄件者名稱由
NODEINFO 封包累積(冷啟動初期可能只看到節點 ID 末 6 碼);PKI 私訊無法解密一律略過;
票頭顯示 `MQTT TW/<頻道>`,右上為 hops 與閘道器 SNR。
**社群若更換金鑰(rekey),該頻道會突然解不開**——症狀是頻道明明有人聊、log 卻安靜;
拿新版頻道 QR 重新解出 PSK 填入即可。

---

## 日常運作行為

- **印表機隨開隨關**:關機期間訊息留在本機 spool、不丟進 CUPS(避免 macOS 因失敗
  job 把佇列暫停);開機後 30 秒內自動處理。`max_age_minutes = 10` 時,離線超過
  10 分鐘的訊息直接略過——睡覺關機,早上不會噴一長串。
- **佇列被 macOS 暫停**(printer-state=5)會自動 `cupsenable` 恢復;已送進 CUPS 卻卡
  10 分鐘的 job 會在 log 提醒。
- **CUPS 佇列是永久的**:`lpadmin` 建過一次,重開機、拔線、關印表機都不會消失。
- **去重**:節點重連補收、程式重啟、閘道器重複轉發都不會印第二次。
- **訊息風暴**:spool 超過 100 則丟最舊,並印一張單行警示票。
- **渲染失敗**(字型檔壞掉等)改印純 ASCII 降級版面,服務不中斷。
- **兩個來源的開關**:

  | `[node] enabled` | `[mqtt] enabled` | `run` 的行為 |
  |---|---|---|
  | true | false | 只收 MeshCore 節點(預設) |
  | true | true | 兩邊同時收,同一台印表機依序出紙 |
  | false | true | 只收 MQTT,不需接節點 |
  | false | false | 啟動即報錯結束 |

---

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `zsh: no such file or directory: .venv/bin/meshprint` | 不在專案資料夾。先 `cd ~/Documents/print`,或用完整路徑 `~/Documents/print/.venv/bin/meshprint` |
| `lpstat -e` 沒顯示東西 | 佇列從沒建過(macOS 不會自動幫老 USB 印表機建)。先 `lpinfo --include-schemes usb -v` 確認看得到 `usb://EPSON/LQ-310`,再照 runbook 第 2 步 `lpadmin` 建一次 |
| 字偏右、印到報表紙虛線 | 可印寬度太寬。縮小 `[printer] width_dots`(1332 = 7.4 吋);整體要右移就加大 `left_margin_dots`;或把印表機後方齒孔牽引器整組往右挪 |
| 開機後不印、`status` 顯示待印 > 0 | 看 `run` 的 log:「印表機未上線」= USB 沒偵測到;「佇列可能暫停」= 手動 `cupsenable LQ310` |
| SignalTest 明明有人聊卻沒印 | 社群換了金鑰;重解新版頻道 QR 更新 `psk` |
| 韓文/罕用字印成「□」 | 字型鏈全缺字。裝 Noto Sans CJK TC(已裝)或在 `[render] fallback_fonts` 加字型 |
| 改設定沒反應 | 設定檔啟動時讀取,`Ctrl-C` 重開 `run` |
| 找不到 `uv` | 一般使用不需要;重建環境時用 `~/Library/Python/3.9/bin/uv` |

---

## 測試

```bash
.venv/bin/python -m pytest
```

56 項測試全部免硬體:
- `test_escp2.py`:手算 golden bytes、補白、裁切/`ESC $` 定位、寬度上限、隨機影像 → 編碼 → 獨立解碼器 roundtrip。
- `test_render.py`:文字清理、換行不溢出且不掉字、全形寬度、缺字替代、截斷、票/校正頁幾何。
- `test_filters.py`、`test_dedup.py`:規則與 LRU/持久化。
- `test_node.py`:payload 對應、頻道寄件者啟發式、用假 MeshCore 物件驗證事件訂閱與斷線結束。
- `test_mqtt.py`:PSK 展開、AES-128/256 roundtrip、信封解碼、閘道器去重、NODEINFO 名稱快取、topic 篩選。
- `test_spool.py`:假 CUPS——送印/完成/歸檔、lp 失敗重試、風暴上限、印表機離線不送、佇列暫停自動恢復、保鮮期、done 修剪。
- `test_daemon.py`:`handle_message` 整合(過濾 → 去重 → 渲染 → spool → 風暴警示票)。

---

## 專案狀態與里程碑

| 里程碑 | 狀態 |
|---|---|
| M1 編碼器 + 版面 + `preview`/`test` CLI | ✅ 2026-08-27,T-1 golden bytes 通過 |
| M2 實機列印與校正 | ✅ 2026-08-28,LQ-310 校正完成(width_dots 1332) |
| M3 MeshCore 整合、過濾、去重、spool | ✅ 2026-08-27 程式完成;T-4/T-5 端對端待節點 |
| v1.1 Meshtastic MQTT 台灣頻道來源(SPEC §12) | ✅ 2026-08-28,實連 broker 驗證解密 |
| 冷啟動保護、保鮮期、忽略名單 | ✅ 2026-08-28 ~ 29(業主日常需求) |
| M4 launchd 服務化(開機自動跑、當掉自動重拉、log 寫檔) | ⏳ |

---

## 與規格的已知差異

- **排版單位**:§6.3 說以「格」排版;實作以字型實際 advance 寬度量測換行(CJK 字型下
  等同格制,西文不會硬塞半格疊字;裝 Noto Sans **Mono** CJK TC 即嚴格等寬)。不做標點禁則。
- **spool 完成判定**:§6.6 寫「`lp` 回傳成功」;實作追蹤 CUPS job 從 not-completed 消失
  才算完成,並加上印表機離線不送、佇列暫停自動恢復、保鮮期(`max_age_minutes`)。
- **佇列偵測**:用 `lpstat -e` 而非 `-p`(macOS 說明文字會在地化)。
- **Python 版本**:規格 ≥ 3.10;`tomllib` 3.11 才內建,3.10 以 `tomli` 相容。
- **MQTT 來源**:規格 §10 列為 v2,提前以 §12 增補實作。
