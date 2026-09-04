# MeshPrint

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Tests](https://img.shields.io/badge/tests-56%20passed-brightgreen.svg)

**網狀網路的電傳打字機(teleprinter)**:把 MeshCore 節點與 Meshtastic 台灣 MQTT 頻道
收到的訊息,即時印到 Epson LQ-310 點陣印表機的連續報表紙上——離線、可留存、
斷電後紀錄仍在紙上,就像傳統 RTTY / 電報收報機。

適合:業餘無線電與 LoRa mesh 玩家、社群基地台/中繼站、災害應變據點——任何想要
「訊息一到就有一張紙,不靠螢幕、不靠雲端」的場合。

完整技術規格見 [SPEC.md](SPEC.md)(v1.0 已凍結;§12 為 v1.1 增補的 MQTT 來源)。

---

## 目錄

1. [背景知識](#背景知識)
2. [功能總覽](#功能總覽)
3. [系統架構](#系統架構)
4. [工作原理](#工作原理)
5. [硬體與環境需求](#硬體與環境需求)
6. [安裝](#安裝)
7. [設定印表機(CUPS 佇列)](#設定印表機cups-佇列)
8. [快速開始](#快速開始)
9. [命令列用法](#命令列用法)
10. [設定檔](#設定檔)
11. [Meshtastic MQTT 頻道](#meshtastic-mqtt-頻道)
12. [日常運作行為](#日常運作行為)
13. [疑難排解](#疑難排解)
14. [測試與開發](#測試與開發)
15. [專案狀態與里程碑](#專案狀態與里程碑)
16. [與規格的已知差異](#與規格的已知差異)
17. [授權與致謝](#授權與致謝)

---

## 背景知識

**MeshCore 與 Meshtastic** 都是用 LoRa 無線電組成的去中心化文字通訊網路:
節點之間互相轉發,不需要基地台或網際網路,適合登山、災害、社群自建通訊。

- **MeshCore**:節點刷 *companion radio* 韌體後,可用 USB serial 接電腦,
  由電腦端程式(本專案用官方 `meshcore` Python 函式庫)收發訊息。本專案把它當
  「收報機」——只收不發。
- **Meshtastic**:台灣社群「臺灣鏈網」的閘道器節點會把空中收到的封包上傳到公共
  MQTT broker;本專案訂閱 broker、用社群頻道金鑰解密,不需要自己有 Meshtastic 節點
  就能把台灣的聊天頻道印出來。

**為什麼是點陣印表機?** 點陣機配連續報表紙可以無人值守長時間輸出,一張票一張票
往下滾,斷電後紙還在;而且 Epson LQ-310 這種 24 針機型至今仍在產、耗材便宜。
缺點是**國際版沒有中文字庫**——所以本專案完全不用印表機的文字模式,而是把整張票
在電腦上畫成黑白點陣圖,再用 ESC/P2 的 24-pin 圖形指令「一根針一根針」地打出來。
字型、排版、缺字處理全由軟體掌控,繁中/日/韓/英數都能印。

**為什麼走 CUPS raw?** macOS / Linux 的列印系統 CUPS 平常會把文件經過驅動程式
「翻譯」成印表機語言;本專案自己產生印表機語言(ESC/P2 bytes),所以用 `lp -o raw`
叫 CUPS 原封不動轉送,驅動程式選什麼都無所謂。

---

## 功能總覽

- **兩種訊息來源,可同時或單獨啟用**
  - MeshCore 節點(USB serial):私訊 + 頻道訊息,拔線/重開自動重連。
  - Meshtastic MQTT:臺灣鏈網 SignalTest、MeshTW、Emergency! 等頻道,內建金鑰,純唯讀。
- **單據式版面**:每則訊息一張「票」——來源/時間、寄件者/hops/SNR、分隔線、內文自動換行。
- **軟體點陣化**:繁中/日/韓/英數全部由電腦渲染;缺字沿字型鏈備援,Emoji 印「□」。
- **可靠性**:磁碟 spool、追蹤 CUPS 真的印完才歸檔、印表機關機不丟訊息、開機自動補印
  (或依保鮮期略過)、佇列被 macOS 暫停自動救回、去重持久化、訊息風暴保護、渲染失敗
  ASCII 降級。
- **過濾**:私訊開關、頻道清單、忽略名單(名稱或節點前綴)。
- **免硬體開發**:PNG 預覽、golden bytes 測試、假 CUPS / 假 MeshCore 物件——56 項測試
  全部不需要印表機或節點。
- **可攜**:macOS 為主要目標,程式碼保持 Linux(含 Raspberry Pi)相容。

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
                                             │       lp -o raw → CUPS 佇列
                                             │       lpstat 確認印完 → done/
                                             ▼
                                       Epson LQ-310(USB,raw 佇列)
```

| 模組 | 職責 | 可離線測試 |
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
發送端時戳、hops/SNR 等附加資訊)。之後的每一級都只認這個物件,不知道訊息從哪來——
所以要加第三種來源(例如 APRS、Meshtastic 本地節點)只要多寫一個來源模組。

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
  圖形帶、走紙 `feed_after_lines` 行、(選配)換頁。一張兩行的票約 20 KB。

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
| 印表機 | Epson LQ-310(或任何 ESC/P2 相容 24-pin 機型),USB 連接,連續報表紙(9.5 吋寬、撕邊後 8.5 吋) |
| 節點(選配) | 刷 `companion_radio_usb` 韌體的 MeshCore 相容板(Heltec V3、T-Beam、RAK 等),USB 接上出現 `/dev/cu.usbmodem*`(macOS)或 `/dev/ttyACM*`(Linux) |
| 網路(選配) | 要印 Meshtastic MQTT 頻道才需要;只接節點可完全離線 |
| 作業系統 | macOS(主要目標)或 Linux(含 Raspberry Pi OS),需有 CUPS |
| Python | ≥ 3.10(`meshcore` 套件硬性要求;3.10 會自動用 `tomli` 讀 TOML) |
| 相依套件 | Pillow、fonttools、meshcore、paho-mqtt、meshtastic、cryptography(`pip install` 自動安裝) |
| 字型 | 建議 [Noto Sans CJK TC](https://github.com/notofonts/noto-cjk/tree/main/Sans)(單一字型涵蓋繁中/日/韓/英數);找不到時自動改用系統字型鏈 |

---

## 安裝

### 通用步驟(macOS / Linux)

```bash
git clone https://github.com/pepefrog1234/meshprint.git
cd meshprint
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`python3` 必須 ≥ 3.10(`python3 --version` 檢查)。之後所有指令用 `.venv/bin/meshprint`;
或 `source .venv/bin/activate` 後直接打 `meshprint`。

### 字型

把 `NotoSansCJKtc-Regular.otf` 放到 `~/Library/Fonts/`(macOS)或在設定檔
`[render] font` 指定路徑。沒裝也能跑:程式會依序尋找系統內的繁中字型
(PingFang、Heiti TC、Linux 的 Noto 套件路徑…)並在 log 提醒改用了哪一個。
Linux 可 `apt install fonts-noto-cjk`,再設 `font = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"`
(集合檔要用 `font_index` 指定繁中 face,或交給自動探索)。

### 沒有 Python 3.10+ 的機器

例如 macOS 系統內建只有 3.9。用 [uv](https://docs.astral.sh/uv/) 抓一個獨立的 CPython,
不需要管理員權限、不動系統:

```bash
pip3 install --user uv
~/Library/Python/3.9/bin/uv venv .venv --python 3.12
~/Library/Python/3.9/bin/uv pip install --python .venv/bin/python -e ".[dev]"
```

(`~/Library/Python/3.9/bin` 是 macOS 系統 pip 的 `--user` 安裝位置;Linux 通常是
`~/.local/bin/uv`。)

### 檢查安裝

```bash
.venv/bin/meshprint status
.venv/bin/meshprint preview "哈囉 世界" -o out.png
```

`status` 會列出設定檔、字型鏈、CUPS 佇列、MQTT、節點裝置、spool 狀態;
`preview` 免硬體產生一張票的 PNG,打開看看版面。

---

## 設定印表機(CUPS 佇列)

接上 USB、開機後:

```bash
lpstat -e
```

有列出佇列就直接用(程式 `queue = "auto"` 會自動抓系統唯一的那個)。
**macOS 通常不會自動幫 LQ-310 這種老 USB 印表機建佇列**,列出來是空的就手動建一次
(永久有效,重開機、關印表機都不會消失):

```bash
lpinfo --include-schemes usb -v          # 找到 usb://EPSON/LQ-310?serial=…
lpadmin -p LQ310 -E -v "usb://EPSON/LQ-310?serial=你的序號"
```

不指定驅動(`-m`)建出來的是 raw 佇列,正是本專案要的;`-m everywhere` 對這種
非 IPP 印表機會失敗,屬正常。Linux 同樣指令(可能需要 `sudo`,或把使用者加入 `lpadmin` 群組)。
有多台印表機時在設定檔 `[printer] queue` 明確指定。

---

## 快速開始

以下指令在專案資料夾內執行(先 `cd` 進去);或把 `.venv/bin/meshprint` 換成完整路徑
(例如 `~/Documents/print/.venv/bin/meshprint`)就能在任何位置執行。

1. **校正**:印校正頁,量標尺(吋距 25.4 mm、總誤差 < 1 mm)、看全寬實線左右有沒有被裁、
   看四種字級是否清晰:

   ```bash
   .venv/bin/meshprint calibrate
   ```

   字壓到報表紙右側虛線 → 在設定檔縮小 `[printer] width_dots`(例如 1332 = 7.4 吋)。
2. **印一張真票**驗證完整管線:

   ```bash
   .venv/bin/meshprint test "LQ-310 上線!繁中かな한글 123"
   ```

3. **開常駐**:

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

位置 `~/.config/meshprint/config.toml`;不存在時全部用預設值(第一次跑不需要任何設定檔)。
只需要寫想覆蓋的欄位。一份實機用的範例:

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
port = "auto"             # 或 "/dev/cu.usbmodem14401"、"/dev/ttyACM0"
baud = 115200

[mqtt]                    # Meshtastic MQTT 來源(見下一節)
enabled = false
host = "mqtt.meshtastic.org"
port = 1883
username = "meshdev"      # Meshtastic 官方公開帳密
password = "large4cats"
tls = false
root = "msh/TW"
label = "MQTT"            # 票頭來源標示
# [[mqtt.channels]] 見下一節;不寫就用內建的臺灣鏈網頻道組

[printer]
queue = "auto"            # CUPS 佇列名;"auto" = 系統唯一的那個
width_dots = 1440         # 可印寬度(點;1 點 = 1/180 吋;LQ-310 上限 1440)
left_margin_dots = 24     # 左右對稱邊界
feed_after_lines = 4      # 每張票後走紙幾行(1 行 = 1/6 吋)
form_feed = false         # 連續紙不換頁;A4 單張才設 true

[render]
font = "~/Library/Fonts/NotoSansCJKtc-Regular.otf"
font_index = 0            # .ttc 集合檔的 face 索引
fallback_fonts = []       # 備援字型:"路徑" 或 "路徑#索引"
body_px = 28              # 內文字級(下限 24)
header_px = 24            # 標題/寄件者列字級
max_body_lines = 40       # 內文超過就截斷
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

## Meshtastic MQTT 頻道

臺灣鏈網(Meshtastic Taiwan Community)使用官方公共 broker `mqtt.meshtastic.org`,
root topic `msh/TW`。程式訂閱 `msh/TW/#`(含子區域),從 ServiceEnvelope 解出 MeshPacket,
用頻道金鑰 AES-CTR 解密,只把 TEXT 訊息印出來;NODEINFO 封包用來累積節點名稱,
位置/遙測等一律丟棄。

內建頻道組(金鑰解自社群公開發布的頻道 QR,2026-01 版):

| 頻道 | 金鑰 | 角色 |
|---|---|---|
| **SignalTest** | 社群 AES-256 | 臺灣最熱絡的聊天頻道 |
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

**其他地區的社群**:把 `root` 改成該地區的 root topic(例如 `msh/US`、`msh/EU_868`),
頻道與金鑰換成當地的即可;頻道 QR / `meshtastic.org/e/#…` 連結裡的 ChannelSet protobuf
含有每個頻道的 PSK,用 `meshtastic` Python 套件的 `apponly_pb2.ChannelSet` 解開就能取得。

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
| `zsh: no such file or directory: .venv/bin/meshprint` | 不在專案資料夾。先 `cd` 進去,或用完整路徑 |
| `lpstat -e` 沒顯示東西 | 佇列從沒建過(macOS 不會自動幫老 USB 印表機建)。先 `lpinfo --include-schemes usb -v` 確認看得到 `usb://EPSON/LQ-310`,再照上面 `lpadmin` 建一次 |
| 字偏右、印到報表紙虛線 | 可印寬度太寬。縮小 `[printer] width_dots`(1332 = 7.4 吋);整體要右移就加大 `left_margin_dots`;或把印表機後方齒孔牽引器整組往右挪 |
| 開機後不印、`status` 顯示待印 > 0 | 看 `run` 的 log:「印表機未上線」= USB 沒偵測到;「佇列可能暫停」= 手動 `cupsenable <佇列>` |
| SignalTest 明明有人聊卻沒印 | 社群換了金鑰;重解新版頻道 QR 更新 `psk` |
| 韓文/罕用字印成「□」 | 字型鏈全缺字。裝 Noto Sans CJK TC,或在 `[render] fallback_fonts` 加字型 |
| 節點插著但一直「等待裝置」 | 檢查 `ls /dev/cu.usbmodem*`(Linux `/dev/ttyACM*`);多個裝置時在 `[node] port` 指定;確認刷的是 `companion_radio_usb` 而非 BLE 版 |
| `meshprint run` 說缺少 meshcore | Python 版本 < 3.10;見「沒有 Python 3.10+ 的機器」 |
| 改設定沒反應 | 設定檔啟動時讀取,`Ctrl-C` 重開 `run` |
| 字太細/筆畫斷 | 點陣機不吃細筆畫;把字級調大(`body_px`)、換 Medium 字重的字型 |

---

## 測試與開發

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

### 加一個新的訊息來源

1. 新模組實作 `async def run(self, stop: asyncio.Event)`:連線、收訊息、斷線重連,
   每收到一則就呼叫建構時傳入的 `on_message(InboundMessage)`,看到 `stop` 就收尾。
2. 把 payload 對應成 `InboundMessage`(`kind`、`text`、`rx_time` 必填;有頻道編號就填
   `channel_idx`,沒有就填 `channel_name` 讓票頭只印名稱;`extra["path_len"]`/`["SNR"]`
   會自動印在票的右上)。對應邏輯寫成純函式,方便離線測試。
3. 在 `daemon.run_daemon()` 的 `producers` 依設定開關加入;`config.py` 加對應的設定區段。
過濾、去重、版面、spool 全部不用動。

### 改版面

`render.py` 的 `Renderer.ticket()`:高度公式與繪製順序一一對應,改了一邊記得改另一邊;
用 `meshprint preview` 免耗紙看結果。分隔線厚度、間距在檔案頂部的常數。

### 回報問題

開 [issue](https://github.com/pepefrog1234/meshprint/issues) 時請附:`meshprint status`
輸出、`meshprint -v run` 的 log 片段、印表機型號與紙張;版面問題附 `preview` 的 PNG。
送 PR 前請確認 `pytest` 全過。

---

## 專案狀態與里程碑

| 里程碑 | 狀態 |
|---|---|
| M1 編碼器 + 版面 + `preview`/`test` CLI | ✅ 2026-08-27,T-1 golden bytes 通過 |
| M2 實機列印與校正 | ✅ 2026-08-28,LQ-310 校正完成 |
| M3 MeshCore 整合、過濾、去重、spool | ✅ 2026-08-27 程式完成;T-4/T-5 端對端待節點 |
| v1.1 Meshtastic MQTT 台灣頻道來源(SPEC §12) | ✅ 2026-08-28,實連 broker 驗證解密 |
| 冷啟動保護、保鮮期、忽略名單 | ✅ 2026-08-28 ~ 29 |
| M4 launchd / systemd 服務化(開機自動跑、當掉自動重拉、log 寫檔) | ⏳ |
| v2 構想(SPEC §10) | 網路印表伺服器 9100 埠輸出、iOS 直印、Meshtastic 本地節點、QR code 附印、每日頁首 |

---

## 與規格的已知差異

- **排版單位**:§6.3 說以「格」排版;實作以字型實際 advance 寬度量測換行(CJK 字型下
  等同格制,西文不會硬塞半格疊字;裝 Noto Sans **Mono** CJK TC 即嚴格等寬)。不做標點禁則。
- **spool 完成判定**:§6.6 寫「`lp` 回傳成功」;實作追蹤 CUPS job 從 not-completed 消失
  才算完成,並加上印表機離線不送、佇列暫停自動恢復、保鮮期(`max_age_minutes`)。
- **佇列偵測**:用 `lpstat -e` 而非 `-p`(macOS 說明文字會在地化)。
- **Python 版本**:規格 ≥ 3.10;`tomllib` 3.11 才內建,3.10 以 `tomli` 相容。
- **MQTT 來源**:規格 §10 列為 v2,提前以 §12 增補實作。

---

## 授權與致謝

本專案以 [MIT License](LICENSE) 釋出。

- [MeshCore](https://meshcore.co.uk/) 與官方 Python 函式庫 [meshcore_py](https://github.com/meshcore-dev/meshcore_py)
- [Meshtastic](https://meshtastic.org/) 與 [meshtastic-python](https://github.com/meshtastic/python)(protobuf 定義)
- [臺灣鏈網 Meshtastic Taiwan Community](https://meshtw.github.io/) 公開的頻道組與 MQTT 設定
- [Noto Sans CJK](https://github.com/notofonts/noto-cjk)(SIL Open Font License)
- Pillow、fonttools、paho-mqtt、cryptography

規格書由業主與 Claude 共同擬定,程式碼由 Claude 實作、業主實機驗證。
