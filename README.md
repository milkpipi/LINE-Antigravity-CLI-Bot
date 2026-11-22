# LINE-Antigravity-CLI-Bot
LINE × Antigravity CLI Bot  把 LINE 聊天室接上 Google Antigravity CLI的個人 AI 機器人。它不呼叫付費 API，而是在本機以 headless 模式執行 agy，直接用訂閱額度驅動 Gemini 模型。 私訊與群組都能用，以使用者和群組白名單控管存取 群組有三種模式：被叫才回、附上聊天上下文、自動判斷要不要插話；可設定回應機率，提到 bot 名字一定會回 支援圖片辨識，也可以「回覆」指定圖片來提問 語音回覆：Gemini TTS 可指定語氣，失敗時自動退回 edge-tts 或文字 用 persona.txt 自訂角色，每個聊天室各自保留對話上下文 只用 Python 標準庫，搭配 cloudflared 就能上線
