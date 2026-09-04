# MeshPrint 規格書 v1.0
## MeshCore 訊息 → Epson 點陣印表機 自動列印系統
- 文件狀態:**v1.0 已凍結**(2026-08-27 業主回覆 §11 全部確認;回覆內容記錄於 §11)
- 日期:2026-08-27(v0.1 草案同日凍結)
- 目標讀者:業主(Pty)與實作者(Claude)
---
## 1. 專案概述
當連接於電腦的 MeshCore 節點(companion radio 韌體)收到網狀網路訊息時,電腦端常駐程式自動將訊息以固定單據版面列印到點陣印表機的紙上,形成一份即時、離線、可留存的紙本訊息紀錄。
設計動機與定位:點陣機搭配連續報表紙可長時間無人值守輸出,斷電後紀錄仍在紙上,適合作為 mesh 網路的「電傳打字機(teleprinter)」——類似傳統 RTTY/電報收報機的角色。
核心限制與因應:目標印表機(Epson LQ-310)為國際版機型,**無內建中文字庫**,僅有 ESC/P2 西文字型。因此本系統不使用印表機文字模式印 CJK,而是在電腦端將整則訊息(含中日韓文字)點陣化後,以 ESC/P2 的 24-pin 點陣圖形指令輸出。這使字型、排版、缺字處理完全由軟體端掌控,印表機退化為「一次擊出 24 個直向點」的輸出裝置。
## 2. 系統架構
```
                115200 8N1                                                USB (CUPS raw)
┌──────────────┐  USB serial   ┌───────────────── meshprintd ──────────────┐   ┌──────────┐
│ MeshCore 節點 │──────────────►│  ① 連線模組 (meshcore lib, asyncio)        │──►│ LQ-310   │
│ companion_   │               │  ② 過濾/去重                                │   │ 點陣印表機│
│ radio_usb    │               │  ③ 版面引擎(單據 layout)                   │   └──────────┘
└──────────────┘               │  ④ CJK 點陣化(Pillow + Noto CJK, 1-bit)   │
                               │  ⑤ ESC/P2 編碼器(ESC * 圖形帶)            │
                               │  ⑥ 磁碟 spool → lp -d <queue> -o raw       │
                               └────────────────────────────────────────────┘
```
模組間以單向資料流串接;③④⑤為純函式(輸入訊息物件、輸出 bytes),可完全脫離硬體測試。
## 3. 範圍
### 3.1 v1 包含
- 節點連線:USB serial(companion radio 韌體),斷線自動重連。
- 訊息來源:直接訊息(DM)與頻道訊息(channel),可依設定過濾。
- 列印:單據式版面、CJK 圖形模式輸出、每則訊息即到即印。
- 可靠性:印表機離線時訊息落地磁碟 spool,恢復後補印;程式重啟不遺失。
- 操作:設定檔(TOML)、CLI(常駐模式/測試列印/預覽輸出 PNG)。
### 3.2 v1 不包含(列入 §10 未來擴充)
- BLE / TCP 連線節點、回覆訊息(本系統唯讀)、GUI、Meshtastic 支援、iOS 端、網路印表伺服器(9100 埠)輸出、印表機內建西文字型混排加速。
## 4. 執行環境與相依
| 項目 | 規格 |
|---|---|
| 作業系統 | macOS 26(Tahoe)為主要目標;程式碼保持 Linux 相容(Raspberry Pi 部署為潛在場景) |
| 語言 | Python ≥ 3.10 |
| 相依套件 | `meshcore`(官方 companion radio 函式庫,asyncio/事件驅動)、`Pillow`(點陣化)、`fonttools`(cmap 缺字偵測)、`tomllib`(內建) |
| 字型 | Noto Sans CJK TC(OTF;涵蓋繁中/日/韓 + 拉丁字母,單一字型即可全包);路徑可設定 |
| 列印路徑 | CUPS raw job:`lp -d <佇列> -o raw`(USB 佇列由 macOS 插入時自動建立,raw 模式繞過驅動 filter 鏈) |
技術選型說明:MeshCore 端不自行實作序列協定,直接使用官方 `meshcore` 函式庫——它已提供 serial 連線、事件訂閱(`CONTACT_MSG_RECV` / `CHANNEL_MSG_RECV`)、聯絡人查詢與自動抓取訊息(auto message fetching),協定隨韌體演進由上游維護,本專案只做「訊息 → 紙」這段。
## 5. 硬體需求與前置設定
1. **MeshCore 節點**:刷 `companion_radio_usb` 韌體(注意:companion 韌體一次只編譯一種介面,BLE 版無法走 serial),USB 接上 Mac 後出現 `/dev/cu.usbmodem*`。
2. **印表機**:Epson LQ-310(或任何 ESC/P2 相容 24-pin 機型),USB 連接,裝連續報表紙(80 行機,可印寬度 8 吋)。
3. **CUPS 佇列**:插上 USB 後以 `lpstat -p` 確認佇列名稱。若 macOS 未自動建立,手動:`lpadmin -p LQ310 -E -v usb://EPSON/LQ-310 -m everywhere` 失敗時改用通用 24-Pin 驅動——佇列用哪個驅動不影響本系統,因為送 raw 時 filter 不會執行。
4. **首次校正**:執行 `meshprint calibrate` 印出寬度標尺與測試訊息,確認左右邊界與走紙量(§8 T-2)。
## 6. 功能規格
### 6.1 MeshCore 連線模組
- 啟動流程:開啟 serial → `send_appstart` 取得自身資訊 → `get_contacts()` 快取聯絡人表 → `start_auto_message_fetching()` → 訂閱事件。
- 訂閱事件:`CONTACT_MSG_RECV`、`CHANNEL_MSG_RECV`(主要);`CONNECTED`/`DISCONNECTED`(連線狀態);`NEW_CONTACT`、`ADVERTISEMENT`(觸發聯絡人表刷新,節流 ≥ 60 秒一次)。
- 斷線處理:使用函式庫 `auto_reconnect`(指數退避);重連成功後重新執行啟動流程。序列裝置整個消失(拔線)時,每 10 秒掃描 `/dev/cu.usbmodem*` 等待裝置回來。
- 埠選擇:設定 `port = "auto"` 時掃描候選裝置並以 device query 驗證是 MeshCore 節點;多裝置環境須在設定檔指定。
### 6.2 訊息資料模型
內部統一為 `InboundMessage`:
| 欄位 | 來源 | 說明 |
|---|---|---|
| `kind` | 事件型別 | `dm` 或 `channel` |
| `sender_name` | `pubkey_prefix` → 聯絡人表 `adv_name` | 查無則顯示 `<{prefix 前 6 碼 hex}>` |
| `channel` | `channel_idx`(+ `get_channel` 查名稱) | DM 為空 |
| `text` | payload `text` | UTF-8 |
| `msg_time` | payload timestamp | 發送端時戳(不可信任,僅參考) |
| `rx_time` | 本機收到事件時間 | 版面主要時間,時區預設 Asia/Taipei |
| `extra` | payload 其餘欄位(path_len、SNR 等,若韌體提供) | 選配顯示 |
去重:維護最近 256 則的 `(kind, sender/channel, msg_time, sha1(text)[:8])` LRU,重複者丟棄並記 log。
### 6.3 列印版面(單據 layout)
每則訊息一張「票」,由上而下:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ← 粗分隔線(直接畫線,非字元)
#0 公共頻道                2026-08-27 14:32:05   ← 標題列:來源 + 接收時間
蜜蜂 Bee <a1b2c3>                      hops 2   ← 寄件者列(DM 時第一列改為「私訊」)
──────────────────────────────────  ← 細分隔線
今晚 20:00 網路例會,7.100 MHz LSB,
歡迎各位加入測試。                              ← 內文,自動換行
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
(走紙 feed_after_lines,預設 4 行)
```
- 版面以「格」為單位排版:全形字佔 2 格、半形佔 1 格;換行於格界斷開,不做斷詞(v1)。
- 分隔線由點陣化引擎直接繪製水平線,不依賴「─」字元,避免字型線段接縫。
- 標題/寄件者列字級小於內文;所有字級可設定。
- 內文超長(> `max_body_lines`,預設 40 行)時截斷並印 `…(截斷)`。
### 6.4 CJK 點陣化引擎
- 畫布:`Pillow` mode `"1"`(1 bit),寬 = `width_dots`(預設 1440 = 8 吋 × 180 dpi),高度依內容;**反鋸齒固定關閉**(mode "1" 走 FreeType mono 渲染),字重建議 Regular/Medium,過細筆畫在撞針輸出會斷。
- 字級下限:主文預設 28 px(≈ 11 pt @180 dpi);**不得低於 24 px**——180 dpi 下漢字低於 24×24 點,筆畫密的字(體、鬱、變)會糊掉,此為點陣機物理極限。
- 缺字處理:以 `fonttools` 預載各字型 cmap,逐字元沿 fallback 鏈(主字型 → 選配備援字型)找 glyph,全部缺字時以「□」替代並記 log。Emoji 在 v1 一律替代為「□」。
- 中日混排(選配):payload 無語言資訊,v1 全部以 TC 字型渲染;設定檔可加日文備援字型,但 Han unification 異體字形問題留待 v2。
### 6.5 ESC/P2 編碼器
輸入 1-bit 影像,輸出 bytes:
1. 影像高度補齊為 24 的倍數(補白)。
2. 逐帶(band,24 列)切割;每帶逐欄打包 3 bytes(bit7 = 最上針,黑點 = 1)。
3. 全白帶直接以 `ESC J 24` 跳過,不送圖形資料(空行加速)。
4. 每帶輸出:`ESC * 39 nL nH` + 欄資料 + `CR` + `ESC J 24`。欄數 n = nL + 256×nH,上限 1440。
5. 帶內左右全白邊界裁切,配合 `ESC $` 絕對定位起印,減少資料量與印字頭空跑。
工作(job)層級:
| 位置 | 指令 |
|---|---|
| Job 開頭 | `ESC @`(初始化)、`ESC U 1`(單向列印——圖形垂直對齊必要) |
| 每則訊息之間 | `ESC J`×n 或 LF 走紙(`feed_after_lines`) |
| Job 結尾 | 依設定 `form_feed` 決定是否 `FF`;預設不 FF(連續紙滾動輸出) |
資料量與速度預估(待 M2 實測校準):一帶滿寬 ≈ 4.3 KB;一則 5 行訊息 ≈ 8–12 帶 ≈ 40–50 KB;瓶頸在印字頭機械速度而非 USB,估每則 10–25 秒。列印中新到訊息在佇列排隊,依序輸出。
### 6.6 列印輸出與磁碟 spool
- 流程:編碼完成 → 寫入 `spool/{ISO時間}-{序號}.prn`(附同名 `.json` 中繼資料)→ `lp -d <queue> -o raw <file>` → 回傳成功且 `lpstat` 無錯誤狀態 → 移入 `done/`(保留最近 N 份,預設 200)。
- `lp` 失敗或佇列 paused:留在 spool,背景每 30 秒重試,並在終端/log 告警。程式啟動時先清 spool 積壓(依檔名時間序)。
- 佇列名 `auto`:取 `lpstat -p` 中唯一啟用的印表機;多於一台則要求設定檔明確指定。
### 6.7 設定檔(`~/.config/meshprint/config.toml`)
```toml
[node]
transport = "serial"       # v1 僅 serial
port = "auto"              # 或 "/dev/cu.usbmodem14401"
baud = 115200
[printer]
queue = "auto"             # 或 CUPS 佇列名
width_dots = 1440          # 8 吋 × 180 dpi
left_margin_dots = 24
feed_after_lines = 4
form_feed = false
[render]
font = "~/Library/Fonts/NotoSansCJKtc-Regular.otf"
fallback_fonts = []
body_px = 28
header_px = 24
[filter]
print_dm = true
channels = "all"           # "all" 或 [0, 1]
ignore_senders = []        # adv_name 或 pubkey prefix
[log]
level = "info"
dir = "~/.meshprint/log"
```
### 6.8 CLI
| 指令 | 行為 |
|---|---|
| `meshprint run` | 常駐模式(前景;launchd 服務化見 M4) |
| `meshprint test "文字"` | 不經節點,直接把文字走完整管線列印(硬體煙霧測試) |
| `meshprint preview "文字" -o out.png` | 只跑 ③④,輸出版面 PNG 預覽(**免硬體**驗證版面) |
| `meshprint calibrate` | 列印寬度標尺 + 全字級樣張 |
| `meshprint status` | 顯示節點連線、佇列、spool 積壓數 |
## 7. 可靠性與錯誤處理
| 情境 | 行為 |
|---|---|
| 節點斷線/拔除 | 自動重連 + 裝置掃描;恢復後由韌體暫存訊息經 auto-fetch 補收(受節點端佇列容量限制,超出即遺失——屬協定限制,文件註明) |
| 印表機離線/缺紙 | spool 落地,恢復後依序補印 |
| 程式崩潰/重啟 | spool 未完成項重印;去重 LRU 持久化避免重覆列印 |
| 渲染失敗(字型遺失等) | 該則以純 ASCII 降級版面(印表機事件記錄用),不中斷服務 |
| 訊息風暴 | 佇列上限(預設 100 則)後丟最舊並印一行警示 |
## 8. 驗收測試
- **T-1 編碼器單元測試**:固定輸入影像 → 比對 golden bytes;帶切割、空帶跳過、邊界裁切各案例。
- **T-2 校正頁**:`calibrate` 實印,量測 8 吋標尺誤差 < 1 mm,無左右截斷(對應先前 24-pin 驅動「右側缺 1/4」的已知雷點)。
- **T-3 CJK 樣張**:繁中/日文假名/韓文/英數混排、Big5 外罕用字、Emoji(應為 □)、40 行長文截斷。
- **T-4 端對端**:另一節點發 DM 與頻道訊息各一 → 30 秒內出紙,欄位正確。
- **T-5 韌性**:列印中拔印表機 USB → 恢復後補印;拔節點 → 恢復後續收。
- **T-6 疲勞**:連續 100 則(腳本灌入),無遺漏、無重覆、順序正確。
## 9. 開發里程碑
| 里程碑 | 交付物 | 驗證方式 |
|---|---|---|
| M1 | `escp2.py` + `render.py` + `preview`/`test` CLI | **免硬體**:PNG 預覽 + golden bytes(Claude 端即可完成並自測) |
| M2 | 實機列印與校正(`calibrate`) | 業主在 LQ-310 上跑 T-2/T-3,回饋修參 |
| M3 | MeshCore 整合(事件 → 列印)、過濾、去重、spool | T-4/T-5 |
| M4 | launchd 服務化、log、文件 | T-6 + 一週試運轉 |
## 10. 未來擴充(v2+)
- 輸出改走網路印表伺服器 TCP 9100(bytes 完全不變,只換傳輸層)→ 進而支援 **iOS App 直接對印表機出單**(點陣機無 AirPrint,socket 直送是唯一路)。
- Meshtastic 來源並存(同一版面引擎,多一個來源模組)。
- 純 ASCII 訊息改用印表機內建西文字型文字模式(速度大增),CJK 才切圖形——混排輸出。
- QR code 附印(訊息原文/座標連結)、每日頁首(日期大字)、BLE/TCP 節點。
## 11. 待確認事項(凍結 v1.0 前需業主回覆)
1. **實作語言**:本規格以 Python 為準(理由:官方 `meshcore` 函式庫、Pillow 點陣化、實作者可在無硬體環境直接自測 M1)。若堅持 Swift 原生(CoreText 渲染),③④⑤架構不變,但 MeshCore 協定層需自行移植——請確認走 Python。
2. **印表機**:確定購入 LQ-310?紙張用連續報表紙(建議)或 A4 單張?
3. **列印範圍**:DM + 全部頻道都印,或指定頻道?
4. **節點韌體**:手上節點是否已可刷 `companion_radio_usb`(哪塊板子?)。
5. **時間顯示**:預設 Asia/Taipei、`YYYY-MM-DD HH:MM:SS`,OK?

> **業主回覆(2026-08-27,據此凍結 v1.0)**
> 1. ✅ 用 Python。
> 2. ✅ LQ-310 已購,2026-08-28 到貨。紙張未指明——依本規格建議採連續報表紙(`form_feed = false` 預設不變;若改 A4 單張再調整)。
> 3. ✅ 全部頻道都印(含 DM;即預設值 `print_dm = true`、`channels = "all"`)。
> 4. ✅ 節點採可刷 `companion_radio_usb` 的相容板。
> 5. ✅ 照預設(Asia/Taipei、`YYYY-MM-DD HH:MM:SS`)。
---
## 12. v1.1 增補:Meshtastic MQTT 來源(2026-08-28 業主需求)
把 §10 的「Meshtastic 來源並存」提前實作,但走 **MQTT** 而非本地節點:訂閱臺灣鏈網
(Meshtastic Taiwan Community)使用的公共 broker,把台灣聊天頻道即時印出。與
MeshCore 節點共用 ②–⑥ 整條管線,可同時開或各自單獨開(`[node]`/`[mqtt]` 各有 `enabled`)。

- **連線**(實測查證):broker = 官方 `mqtt.meshtastic.org:1883`(公開帳密
  meshdev/large4cats;`mqtt.meshtastic.tw` 不存在),root topic = `msh/TW`(臺灣鏈網
  手冊指定)。訂閱 `msh/TW/#`,含子區域 topic。**純唯讀,永不 publish。**
- **解碼**:ServiceEnvelope(protobuf)→ 依 `channel_id` 過濾設定頻道 → AES-CTR 解密
  (nonce = packet_id u64 LE + from u32 LE + 4×0;1-byte PSK 依韌體規則展開為預設鍵變體)
  → `Data`:TEXT_MESSAGE_APP 印出;NODEINFO_APP 餵節點名稱快取;其餘(位置/遙測)丟棄。
  PKI 私訊無金鑰可解,一律略過。
- **預設頻道**(金鑰解自臺灣鏈網公開發布的頻道組 QR,2026-01 版;QR 內容即
  meshtastic.org/e/# 連結之 ChannelSet protobuf):
  | 頻道 | PSK | 說明 |
  |---|---|---|
  | `SignalTest` | AES-256(社群金鑰) | **臺灣最熱絡聊天頻道(業主確認,預設主印)** |
  | `MeshTW` | AES-256(社群金鑰) | 社群頻道 |
  | `Emergency!` | AES-256(社群金鑰) | 緊急頻道 |
  | `MediumFast` | `AQ==` 預設鍵 | 主頻道(多為遙測/位置,非 TEXT 丟棄) |
  | `LongFast` | `AQ==` 預設鍵 | 全球預設頻道 |

  其他頻道/自訂 PSK 由設定檔 `[[mqtt.channels]]` 增列。
- **去重**:同一包會被多個閘道器重複轉發 → 來源端以 `(from, packet_id)` LRU 1024 去重;
  管線原有去重照常適用。
- **版面**:同一張票;標題列印 `MQTT TW/<頻道>`(此來源無頻道編號,不印 `#n`);
  寄件者 = NODEINFO 累積的 long_name(未知則以節點 ID 末 6 碼 hex 顯示);
  hops 取 `hop_start − hop_limit`(0 → 「直收」)、SNR 取閘道器回報值。
- **過濾**:`[filter] channels`(頻道編號)只約束 MeshCore 來源;MQTT 頻道由 `[mqtt]`
  自選。`ignore_senders` 兩邊都適用。
- **設定**(新增 `[mqtt]` 區段,預設 `enabled = false`):
```toml
[mqtt]
enabled = true
host = "mqtt.meshtastic.org"
port = 1883
username = "meshdev"          # 官方公開帳密
password = "large4cats"
root = "msh/TW"
label = "MQTT"                # 票頭來源標示
[[mqtt.channels]]
name = "MediumFast"
psk = "AQ=="
[[mqtt.channels]]
name = "LongFast"
psk = "AQ=="
```
- **驗證**(2026-08-28 實測):離線單元測試(PSK 展開/AES-128 與 AES-256 roundtrip/
  信封解碼/閘道器重複去重/NODEINFO 名稱快取);實連 broker 驗證——MediumFast 以預設
  金鑰大量解密成功(147/151),SignalTest/MeshTW 以社群 AES-256 金鑰解密成功,
  證實金鑰與 nonce 佈局正確。SignalTest 初測曾誤以預設金鑰解不開,循社群頻道 QR
  取得正確金鑰後解決。
---
附錄 A:本系統使用之 ESC/P2 指令
| 指令 | Bytes(hex) | 用途 |
|---|---|---|
| ESC @ | `1B 40` | 印表機初始化 |
| ESC U n | `1B 55 01` | 單向列印(圖形對齊) |
| ESC * m nL nH d… | `1B 2A 27 nL nH …` | 24-pin 點陣圖,m=39(180×180 dpi),每欄 3 bytes |
| ESC $ nL nH | `1B 24 nL nH` | 絕對水平定位(1/60 吋單位) |
| ESC J n | `1B 4A n` | 走紙 n/180 吋(帶距 n=24) |
| CR / LF / FF | `0D` / `0A` / `0C` | 歸位/換行/換頁 |
