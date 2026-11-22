# LINE-Antigravity-CLI-Bot
# LINE-Antigravity-CLI-Bot

把 LINE 聊天室接上 Google Antigravity CLI（`agy`）的個人 AI 機器人。

不呼叫付費 API，而是在本機以 headless 模式執行 `agy`，直接用 Google AI Pro 訂閱額度驅動 Gemini 模型。

```
LINE ──webhook──> cloudflared ──> line_agy_bot.py ──> agy -p（AI Pro 額度）
  ^                                     │
  └──────────── 回覆文字 / 語音 ─────────┘
```

## 功能

- **私訊與群組**：以使用者／群組白名單控管存取
- **群組三種模式**：被叫才回（`trigger`）、附上聊天上下文（`context`）、自動判斷插話（`auto`）
- **控制刷屏**：`auto` 模式可設定回應機率，提到 bot 名字一定會回
- **圖片辨識**：可直接傳圖，或「回覆」某張圖再提問
- **語音回覆**：Gemini TTS（可指定語氣），失敗自動退回 edge-tts 或文字
- **自訂角色**：`persona.txt` 設定個性，每個聊天室各自保留對話上下文
- **零相依**：只用 Python 標準庫（3.9+），搭配 cloudflared 即可上線

## 需求

- Python 3.9+
- [Antigravity CLI](https://antigravity.google/)（`agy`）＋ Google AI Pro / Ultra 訂閱
- LINE Messaging API channel
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)（或其他能把 webhook 開到外網的工具）
- 選用：ffmpeg（語音）、`pip install edge-tts`（備援語音）、Gemini API key（Gemini TTS）

## 快速開始

```bash
git clone https://github.com/milkpipi/LINE-Antigravity-CLI-Bot.git
cd LINE-Antigravity-CLI-Bot
cp .env.example .env                 # 填入 LINE 的 secret / token
cp persona.example.txt persona.txt   # 選用：自訂角色
python line_agy_bot.py
cloudflared tunnel --url http://127.0.0.1:8080
```

把 cloudflared 給的網址加上 `/callback` 填到 LINE Console 的 Webhook URL。詳細步驟見下方。

> ⚠️ **使用須知**：個人訂閱額度僅限個人使用。Google 表示 AI Pro 額度不適用於多人共用的中繼服務，用於群組時帳號被限制的風險請自行評估。本專案與 Google、LINE 無關。

## 安裝步驟

### 1. 安裝並登入 agy

```bash
# Linux / macOS
curl -fsSL https://antigravity.google/cli/install.sh | bash
# Windows PowerShell
irm https://antigravity.google/cli/install.ps1 | iex
```

先跑一次互動式登入（headless 模式只會用已快取的憑證）：

```bash
agy auth login        # 或直接執行 agy 走登入流程
agy models            # 查可用模型，挑一個 Gemini 的 slug 填進 .env 的 AGY_MODEL
agy -p "用一句話自我介紹" --output-format json   # 確認能正常回應
```

### 2. 建立 LINE Messaging API channel

1. LINE Developers Console → 建立 Messaging API channel。
2. Basic settings 複製 **Channel secret**；Messaging API 分頁發行 **Channel access token (long-lived)**。
3. LINE Official Account Manager → 回應設定：**關閉「自動回應訊息」**、**開啟 Webhook**。
4. 要在群組使用的話，在 Messaging API 設定**開啟「允許加入群組或多人聊天室」**。

### 3. 設定與啟動

```bash
cp .env.example .env      # Windows: copy .env.example .env
# 填入 LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN、AGY_MODEL
python3 line_agy_bot.py   # Windows: py line_agy_bot.py
```

### 4. 把 webhook 開到外網

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

把它給的 `https://xxxx.trycloudflare.com` 加上 `/callback`，填到 LINE Console 的 Webhook URL，按 Verify。
（trycloudflare 網址每次重開會變；要固定的話改用 Cloudflare named tunnel 綁自己的網域，或 ngrok 固定網域。）

### 5. 綁定你自己

1. 用手機加 bot 好友、傳任意訊息 → bot 回你的 userId（設定模式，不會呼叫 agy）。
2. 填進 `.env` 的 `ALLOWED_USER_IDS`，重啟程式。之後只有你傳的私訊會交給 agy。

### 6. 啟用私人群組

1. 把 bot 拉進群組，由你在群組裡傳 `/id` → bot 回傳 groupId（其他人傳不會有反應）。
2. 填進 `.env` 的 `ALLOWED_GROUP_IDS`，重啟程式。
3. 白名單是「群組」層級：群組啟用後，裡面每個成員都能用，不必個別加入 `ALLOWED_USER_IDS`。
   用 `.env` 的 `GROUP_MODE` 選擇行為：

   | 模式 | 怎麼觸發 | 上下文 | agy 呼叫次數 |
   |---|---|---|---|
   | `trigger`（預設） | @bot 或 `!ai`、`/ai` 開頭 | 只有那一則 | 被叫幾次就幾次 |
   | `context` | 同上 | 附上這段期間大家聊的內容 | 同上 |
   | `auto` | 自動；大家安靜 `GROUP_AUTO_DELAY` 秒後整批送出 | 整批新訊息 | 每段聊天一次，模型覺得不用回就保持安靜 |

   `auto` 模式只有 `AUTO_REPLY_CHANCE`（預設 20%）的機率會拿去問模型，避免刷屏；沒抽中的訊息會留下來當上下文。
   @bot、觸發詞，或訊息中提到 `BOT_NAMES` 裡的名字，一律會回應。
   `auto` 模式下 @bot 或觸發詞仍會立即回應。模型判斷不需回應時會輸出 `[SKIP]`，bot 就不發訊息，但那次呼叫仍算額度。
4. 每個群組共用一段對話，訊息會自動標上發言者名字，所以 bot 分得出是誰在問；傳 `/new` 可重開群組對話。

> 額度提醒：Google 論壇官方回覆表示，個人 AI Pro 額度在「多人共用的中繼」情境下不適用；群組使用有帳號被限制的可能，請自行評估。

## 圖片

bot 收到圖片會下載到 agy 工作目錄的 `images/`，再請 agy 用它的檔案檢視工具看圖（Gemini 本身支援看圖）。

| 情境 | 行為 |
|---|---|
| 私訊只傳圖 | 等 `IMAGE_WAIT` 秒沒補文字，就直接針對圖片回應 |
| 私訊傳圖後馬上打字（例如「這是什麼」） | 圖＋文字合併成一題 |
| 群組 `trigger` 模式 | 傳圖不會觸發；之後有人 `!ai` / @bot 提問時，自動附上最近還沒問過的圖 |
| 群組 `context` / `auto` 模式 | 圖片和文字一起進上下文，`auto` 會自己判斷要不要回應 |
| 任何地方「回覆」某張圖再提問 | 只看被回覆的那張圖 |

> 這部分只用模擬環境測過流程。agy 在 headless 模式看圖目前有回報不穩的 issue（google-antigravity/antigravity-cli#1278，官方無法重現）。如果實測回覆「看不到圖片」，把 log 貼給我再調整。

## 語音回覆

需要 **ffmpeg**（Windows：`winget install Gyan.FFmpeg`，裝完重開終端機），用來把音檔轉成 LINE 的 m4a。

| 引擎 | 自然度 | 需要 |
|---|---|---|
| `gemini`（預設） | 最自然，可用文字指定語氣，能演出 `<laugh>`、`<sigh>` 等 | AI Studio 免費 API key（`GEMINI_API_KEY`） |
| `edge`（備援） | 標準播報腔 | `pip install edge-tts` |
| `http` | 看你接的模型（例如本機聲音複製） | 自己架的 TTS 伺服器（`VOICE_HTTP_URL`） |

- `VOICE_MODE=model`（預設）：有人說「用講的」「念給我聽」「想聽你的聲音」，或模型覺得用說的比較有趣時，回覆會變成語音。
- 主引擎失敗（例如額度用完）會依 `VOICE_FALLBACK` 改用其他引擎，都失敗就回文字。
- 語音檔由 bot 自己的 `/media/` 提供，網址會從 LINE 的 webhook 自動偵測；檔案 24 小時後自動刪除。
- 換聲音：改 `GEMINI_TTS_VOICE`（可以在 AI Studio 先試聽），語氣改 `GEMINI_TTS_STYLE`。

## 使用

| 訊息 | 作用 |
|---|---|
| 一般文字（私訊）／`!ai 問題`、`@bot 問題`（群組） | 交給 agy，同一聊天室會接續上下文 |
| `/new` | 開新對話（bot 被重新加進群組時也會自動重開） |
| `/id` | 顯示 userId／groupId |
| `/help` | 指令說明 |

## 角色設定

編輯 `persona.txt` 即可改角色（可多行）。改完重啟 bot，並在各聊天室傳 `/new` 讓新設定生效；舊對話會沿用開頭時的設定。

## 常駐執行

- Linux：寫一個 systemd service 執行 `python3 /path/line_agy_bot.py`（`User=` 要設成登入過 agy 的那個使用者，否則抓不到憑證）。
- Windows：工作排程器 →「登入時」執行 `py C:\path\line_agy_bot.py`，cloudflared 也同樣設一個。

## 運作細節

- 每則訊息跑一次 `agy -p ... --output-format json`，用回傳的 `conversation_id` 加上 `--conversation` 接續上下文；對話失效時會自動開新對話重試。
- 單一工作執行緒依序處理，不會同時開多個 agy。
- 沒有加 `--dangerously-skip-permissions`：需要授權的工具在 headless 下會被自動拒絕；另外預設帶 `--sandbox`，工作目錄是一個空資料夾。
- 先用 reply token 回覆（不吃 push 額度）；agy 超過約 50 秒或 reply 失敗才改用 push（會算進每月免費訊息數）。
- 私訊等待時會顯示「輸入中」動畫（LINE 只支援一對一聊天，群組沒有）。
- 對話紀錄的 ID 存在 `state.json`；log 直接印在終端機。

## 疑難排解

| 狀況 | 處理 |
|---|---|
| `authentication required` | 用執行 bot 的同一個使用者帳號跑一次互動式 `agy` 登入 |
| 回覆說模型錯誤 | `AGY_MODEL` 的 slug 打錯；headless 不會自動改用其他模型，用 `agy models` 重查 |
| `--sandbox` 相關錯誤 | `.env` 設 `AGY_SANDBOX=0` |
| Verify 出現 502 Bad Gateway | bot 沒在跑，或 cloudflared 把 localhost 解析成 IPv6；tunnel 改用 `http://127.0.0.1:8080` |
| Webhook Verify 失敗 | 確認 bot 有在跑、tunnel 網址結尾有 `/callback`、Channel secret 正確 |
| 群組一直回 `[SKIP]` 或太常不出聲 | 傳 `/new` 重開群組對話；想讓它更愛插話，可在 `.env` 用 `AUTO_NOTE=` 改寫插話規則 |
| 回覆很慢 | 調低 `AGY_EFFORT` 或換比較快的模型 |

## 授權

[MIT License](LICENSE)
