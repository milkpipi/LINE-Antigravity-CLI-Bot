#!/usr/bin/env python3
"""
LINE 私訊 bot：把訊息交給 Antigravity CLI (agy) 處理，使用 Google AI Pro 訂閱額度。

設計重點
- 只用 Python 標準庫，不需要 pip install。
- 私訊：只回應白名單裡的 LINE userId。
- 群組：只在白名單群組（ALLOWED_GROUP_IDS）運作，群組內所有成員都能使用。
  GROUP_MODE 決定行為：
    trigger  只有 @bot 或觸發詞開頭才回應（只看那一則）
    context  一樣要 @bot 或觸發詞才回應，但會把期間的群組聊天一起當上下文
    auto     自動讀所有訊息，安靜 GROUP_AUTO_DELAY 秒後整批交給 agy，由模型決定要不要插話
  每個群組一段共用對話，訊息前面會標示發言者名字。
- 語音：模型在回覆開頭加 [VOICE] 時，用 edge-tts 轉成語音，由本程式的 /media/ 提供給 LINE 下載。
- 圖片：下載到 agy 工作目錄的 images/，在提示裡附上路徑，請 agy 用檔案檢視工具看圖。
  私訊傳圖後幾秒內再傳文字會合併成一題；群組可「回覆」某張圖並 @bot / 觸發詞來問。
- 單一工作執行緒依序處理，不會同時開多個 agy，也沒有輪詢迴圈。
- 每個聊天室（私訊或群組）記住 agy 的 conversation_id，用 --conversation 接續上下文；傳「/new」重開對話。
- 不加 --dangerously-skip-permissions：agy 在 headless 模式下遇到需要授權的工具會自動拒絕。
- 先用 reply token 回覆（不吃 push 額度），逾時或失敗才改用 push。
"""

import base64
import hashlib
import hmac
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 設定 ----
def read_text_any(path: Path) -> str:
    """讀文字檔：容忍 Windows 記事本的 UTF-8 BOM，必要時退回 Big5/CP950。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp950"):
        try:
            text = data.decode(enc)
            if enc != "utf-16" or data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return text
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def find_file(name: str):
    """找設定檔；Windows 檔名常被記事本多加 .txt（副檔名又被隱藏），一併找。"""
    for cand in (BASE_DIR / name, BASE_DIR / (name + ".txt")):
        if cand.is_file():
            return cand
    return None


DOTENV_INFO = ""


def load_dotenv() -> None:
    """讀取 KEY=VALUE 格式的 .env（不覆蓋已存在的環境變數）。"""
    global DOTENV_INFO
    path = find_file(".env")
    if not path:
        DOTENV_INFO = f"找不到 .env（應放在 {BASE_DIR}）"
        return
    keys = []
    for raw in read_text_any(path).splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in os.environ and os.environ[key] != value:
            keys.append(key + "(被系統環境變數覆蓋)")
        else:
            keys.append(key)
        os.environ.setdefault(key, value)
    DOTENV_INFO = f"已讀取 {path.name}：" + ", ".join(keys)


load_dotenv()

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ALLOWED_USER_IDS = {
    u.strip() for u in os.environ.get("ALLOWED_USER_IDS", "").split(",") if u.strip()
}
ALLOWED_GROUP_IDS = {
    g.strip() for g in os.environ.get("ALLOWED_GROUP_IDS", "").split(",") if g.strip()
}
GROUP_TRIGGERS = [
    t.strip() for t in os.environ.get("GROUP_TRIGGERS", "/ai,!ai").split(",") if t.strip()
]
GROUP_MODE = os.environ.get("GROUP_MODE", "trigger").strip().lower()
if GROUP_MODE not in ("trigger", "context", "auto"):
    GROUP_MODE = "trigger"
GROUP_AUTO_DELAY = float(os.environ.get("GROUP_AUTO_DELAY", "10"))
def _parse_chance(raw: str) -> float:
    raw = raw.strip().rstrip("%")
    try:
        v = float(raw)
    except ValueError:
        return 0.2
    if v > 1:  # 允許寫 20 或 20%
        v /= 100
    return min(max(v, 0.0), 1.0)


AUTO_REPLY_CHANCE = _parse_chance(os.environ.get("AUTO_REPLY_CHANCE", "0.2"))
BOT_NAMES = [n.strip() for n in os.environ.get("BOT_NAMES", "").split(",") if n.strip()]
GROUP_CONTEXT_MAX = int(os.environ.get("GROUP_CONTEXT_MAX", "40"))
SKIP_TOKEN = "[SKIP]"
IMAGE_WAIT = float(os.environ.get("IMAGE_WAIT", "8"))              # 私訊：傳圖後等幾秒看有沒有補文字
IMAGE_ATTACH_WINDOW = float(os.environ.get("IMAGE_ATTACH_WINDOW", "300"))  # 圖片可被後續提問引用的秒數
IMAGE_KEEP_HOURS = float(os.environ.get("IMAGE_KEEP_HOURS", "24"))
LINE_DATA_API_BASE = os.environ.get("LINE_DATA_API_BASE", "https://api-data.line.me").rstrip("/")
VOICE_MODE = os.environ.get("VOICE_MODE", "model").strip().lower()  # off / model / always
if VOICE_MODE not in ("off", "model", "always"):
    VOICE_MODE = "model"
VOICE_NAME = os.environ.get("VOICE_NAME", "zh-TW-HsiaoChenNeural").strip()
VOICE_RATE = os.environ.get("VOICE_RATE", "+0%").strip()
VOICE_PITCH = os.environ.get("VOICE_PITCH", "+0Hz").strip()
VOICE_WITH_TEXT = os.environ.get("VOICE_WITH_TEXT", "0").strip() in ("1", "true", "yes")
VOICE_MAX_CHARS = int(os.environ.get("VOICE_MAX_CHARS", "200"))
EDGE_TTS_BIN = os.environ.get("EDGE_TTS_BIN", "").strip()
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg") or ""
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
VOICE_TAG = "[VOICE]"
# 語音引擎：gemini（Gemini TTS，最自然，需免費 API key）/ edge（edge-tts）/ http（本機 TTS 伺服器）
VOICE_ENGINE = os.environ.get("VOICE_ENGINE", "gemini").strip().lower()
VOICE_FALLBACK = [e.strip() for e in os.environ.get("VOICE_FALLBACK", "edge").split(",") if e.strip()]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_API_BASE = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com").rstrip("/")
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts").strip()
GEMINI_TTS_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Leda").strip()
GEMINI_TTS_STYLE = os.environ.get(
    "GEMINI_TTS_STYLE",
    "台灣年輕女生用 LINE 傳語音給朋友，台灣腔國語，自然口語、輕鬆隨性，語速正常偏快",
).strip()
VOICE_HTTP_URL = os.environ.get("VOICE_HTTP_URL", "").strip()  # 例：http://127.0.0.1:9880/tts?text={text}&...
PORT = int(os.environ.get("PORT", "8080"))
LINE_API_BASE = os.environ.get("LINE_API_BASE", "https://api.line.me").rstrip("/")

AGY_BIN = os.environ.get("AGY_BIN") or shutil.which("agy") or "agy"
AGY_MODEL = os.environ.get("AGY_MODEL", "").strip()
AGY_EFFORT = os.environ.get("AGY_EFFORT", "").strip()
AGY_SANDBOX = os.environ.get("AGY_SANDBOX", "1").strip() not in ("0", "false", "no", "")
AGY_TIMEOUT = os.environ.get("AGY_TIMEOUT", "5m").strip()
AGY_WORKDIR = Path(os.environ.get("AGY_WORKDIR") or (BASE_DIR / "agy_workdir")).resolve()
STATE_FILE = Path(os.environ.get("STATE_FILE") or (BASE_DIR / "state.json")).resolve()

# 新對話第一則訊息前面附上的指示（agy 沒有獨立的 system prompt 參數）
# 優先讀 PERSONA_FILE（預設 persona.txt，可多行），沒有檔案才用 PERSONA 環境變數／內建預設
_persona_file = (Path(os.environ["PERSONA_FILE"]) if os.environ.get("PERSONA_FILE")
                 else find_file("persona.txt"))
_persona_from_file = (
    read_text_any(_persona_file).strip() if _persona_file and _persona_file.is_file() else ""
)
PERSONA_SOURCE = (f"{_persona_file.name}" if _persona_from_file
                  else ("PERSONA 環境變數" if os.environ.get("PERSONA") else "內建預設"))
_FORMAT_RULE = "（技術提醒：LINE 不支援 Markdown，請用純文字回覆，不要用 **粗體**、# 標題或表格。）"
PERSONA = (_persona_from_file + "\n" + _FORMAT_RULE) if _persona_from_file else os.environ.get(
    "PERSONA",
    "你現在是透過 LINE 聊天回覆的個人助理。請使用繁體中文（台灣用語），"
    "回答精簡、直接；LINE 不支援 Markdown，請用純文字，不要用 **粗體**、# 標題或表格。"
    "除非我明確要求，否則不要讀寫檔案或執行指令。",
)
GROUP_NOTE = os.environ.get(
    "GROUP_NOTE",
    "這是一個 LINE 私人群組，裡面有幾位朋友；每則訊息開頭會標示「發言者名字：」，"
    "回覆時不需要重複名字標記。",
)
IMAGE_NOTE = (
    "（訊息中的「[圖片：路徑]」是傳到聊天室的圖片檔，請用你的檔案檢視工具打開該路徑看過圖片再回答，"
    "不要對使用者提到檔案路徑。）"
)
AUTO_NOTE = os.environ.get("AUTO_NOTE") or (
    "群組裡的新訊息會一批一批轉給你。你是群組成員之一，像朋友一樣自然地參與聊天："
    "有人提問、在討論你能幫上忙或能接話的事、提到你的名字、分享圖片或有趣的事時，就正常回覆。"
    f"只有在對話明顯跟你無關、或你插話會很突兀時，才只輸出 {SKIP_TOKEN} 這幾個字（不加任何符號或其他文字）。"
    f"注意：{SKIP_TOKEN} 只用在這種自動轉來的訊息；有人直接叫你時一定要正常回覆。"
)
VOICE_TAGS_NOTE = (
    "語音內容裡可以用 <laugh>（笑）、<sigh>（嘆氣）、<short pause>（停頓）這類標記讓語氣更自然，適量就好。"
    if VOICE_ENGINE == "gemini" else ""
)
VOICE_NOTE = (
    f"你可以用語音回覆：當有人要你用講的、念給他聽、想聽你的聲音，或你覺得用說的更有趣時，"
    f"在回覆最前面加上 {VOICE_TAG}，後面接要念出來的內容。語音內容要口語、像在講話，"
    f"不要網址、表情符號或條列，盡量在 {VOICE_MAX_CHARS // 2} 字以內。其他時候正常用文字回覆。"
    + VOICE_TAGS_NOTE
)
VOICE_KEYWORDS = re.compile(r"語音|用講的|用說的|講給|說給|念給|唸給|念出來|唸出來|聽你|你的聲音|發語音|錄音")
FORCE_REPLY_NOTE = f"（有人直接叫你，請正常回覆，不可以輸出 {SKIP_TOKEN}）"

REPLY_TOKEN_SAFE_SECONDS = 50  # 超過這個時間就不冒險用 reply token，直接 push
LINE_TEXT_LIMIT = 4900         # LINE 單則文字上限 5000 字元，留一點緩衝
LINE_MAX_MESSAGES = 5          # 一次 reply/push 最多 5 則


def log(*args) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *args, flush=True)


# ---------------------------------------------------------------- 狀態 ----
_state_lock = threading.Lock()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def get_conversation(chat_id: str):
    with _state_lock:
        return load_state().get(chat_id, {}).get("conversation_id")


def set_conversation(chat_id: str, conversation_id):
    with _state_lock:
        state = load_state()
        if conversation_id:
            state.setdefault(chat_id, {})["conversation_id"] = conversation_id
        else:
            state.pop(chat_id, None)
        save_state(state)


# ------------------------------------------------------------- LINE API ----
def verify_signature(body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def line_post(path: str, payload: dict) -> tuple:
    req = urllib.request.Request(
        LINE_API_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ACCESS_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, str(e)


_name_cache = {}


def get_display_name(group_id: str, user_id: str) -> str:
    """取得群組成員的顯示名稱（有快取），失敗就回傳簡短的 userId。"""
    key = (group_id, user_id)
    if key in _name_cache:
        return _name_cache[key]
    name = "成員" + user_id[-4:]
    req = urllib.request.Request(
        f"{LINE_API_BASE}/v2/bot/group/{group_id}/member/{user_id}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            name = json.loads(resp.read().decode("utf-8")).get("displayName") or name
    except (urllib.error.URLError, json.JSONDecodeError, ValueError):
        pass
    _name_cache[key] = name
    return name


def split_text(text: str) -> list:
    text = text.strip() or "（沒有回應內容）"
    chunks = []
    while text:
        if len(text) <= LINE_TEXT_LIMIT:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, LINE_TEXT_LIMIT)
        if cut < LINE_TEXT_LIMIT // 2:
            cut = LINE_TEXT_LIMIT
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if len(chunks) > LINE_MAX_MESSAGES:
        chunks = chunks[:LINE_MAX_MESSAGES]
        chunks[-1] = chunks[-1][: LINE_TEXT_LIMIT - 20] + "\n…（內容過長，已截斷）"
    return chunks


def send_text(to: str, reply_token, text: str, received_at: float) -> None:
    send_messages(to, reply_token, [{"type": "text", "text": c} for c in split_text(text)],
                  received_at)


def send_messages(to: str, reply_token, messages: list, received_at: float) -> None:
    if reply_token and time.time() - received_at < REPLY_TOKEN_SAFE_SECONDS:
        status, body = line_post(
            "/v2/bot/message/reply", {"replyToken": reply_token, "messages": messages}
        )
        if status == 200:
            return
        log(f"reply 失敗（{status}），改用 push：{body[:200]}")
    status, body = line_post("/v2/bot/message/push", {"to": to, "messages": messages})
    if status != 200:
        log(f"push 失敗（{status}）：{body[:300]}")


# ---------------------------------------------------------------- 語音 ----
MEDIA_DIR = AGY_WORKDIR / "media"
_public_base = {"url": PUBLIC_BASE_URL}
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_MEDIA_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(m4a|mp3)$")


def public_base_url():
    return _public_base["url"]


def note_public_host(headers) -> None:
    """從 webhook 請求的 Host 自動得知 tunnel 的公開網址（trycloudflare 網址會變也沒關係）。"""
    if PUBLIC_BASE_URL:
        return
    host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "").split(",")[0].strip()
    if host and not host.startswith(("localhost", "127.", "0.0.0.0", "[::1]")):
        url = f"https://{host}"
        if _public_base["url"] != url:
            _public_base["url"] = url
            log(f"偵測到公開網址：{url}（語音檔會從這裡提供）")


def tts_command():
    if EDGE_TTS_BIN:
        return [EDGE_TTS_BIN]
    exe = shutil.which("edge-tts")
    return [exe] if exe else [sys.executable, "-m", "edge_tts"]


def _cleanup_media() -> None:
    cutoff = time.time() - IMAGE_KEEP_HOURS * 3600
    for f in MEDIA_DIR.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


_AUDIO_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z _-]{0,30}>")


def synth_edge(text: str, base: Path):
    out = base.with_suffix(".mp3")
    cmd = tts_command() + [f"--voice={VOICE_NAME}", f"--rate={VOICE_RATE}", f"--pitch={VOICE_PITCH}",
                           f"--text={text}", f"--write-media={out}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"edge-tts 執行失敗：{e!r}（是否已 pip install edge-tts？）")
        return None
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        log("edge-tts 失敗：" + proc.stderr.decode("utf-8", "replace")[-300:])
        return None
    return out


def synth_gemini(text: str, base: Path):
    if not GEMINI_API_KEY:
        log("VOICE_ENGINE=gemini 但沒設 GEMINI_API_KEY")
        return None
    if "3.8" in GEMINI_TTS_MODEL:
        part = {"text": text}
        if GEMINI_TTS_STYLE:
            part["speech_metadata"] = {"style": GEMINI_TTS_STYLE}
        voice_cfg = {"voice": GEMINI_TTS_VOICE}
    else:  # 舊版模型：用自然語言指定語氣
        part = {"text": f"用這種方式說（{GEMINI_TTS_STYLE}）：{text}" if GEMINI_TTS_STYLE else text}
        voice_cfg = {"prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}}
    payload = {
        "contents": [{"role": "user", "parts": [part]}],
        "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": voice_cfg}},
    }
    req = urllib.request.Request(
        f"{GEMINI_API_BASE}/v1beta/models/{GEMINI_TTS_MODEL}:generateContent",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"Gemini TTS 失敗（{e.code}）：{e.read().decode('utf-8', 'replace')[:300]}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        log(f"Gemini TTS 失敗：{e!r}")
        return None
    try:
        parts = result["candidates"][0]["content"]["parts"]
        inline = next(p["inlineData"] for p in parts if "inlineData" in p)
        data = base64.b64decode(inline["data"])
    except (KeyError, IndexError, StopIteration, ValueError):
        log("Gemini TTS 回應裡沒有音訊：" + json.dumps(result, ensure_ascii=False)[:300])
        return None
    out = base.with_suffix(".wav")
    if data[:4] == b"RIFF":
        out.write_bytes(data)
    else:  # 原始 PCM（16-bit、單聲道），從 mimeType 取取樣率，預設 24 kHz
        m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(m.group(1)) if m else 24000)
            w.writeframes(data)
    return out


def synth_http(text: str, base: Path):
    if not VOICE_HTTP_URL or "{text}" not in VOICE_HTTP_URL:
        log("VOICE_ENGINE=http 需要設定含 {text} 的 VOICE_HTTP_URL")
        return None
    url = VOICE_HTTP_URL.replace("{text}", urllib.parse.quote(text))
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"本機 TTS 伺服器失敗：{e!r}")
        return None
    ext = ".mp3" if "mpeg" in ctype or data[:3] == b"ID3" else ".m4a" if "mp4" in ctype else ".wav"
    out = base.with_suffix(ext)
    out.write_bytes(data)
    return out


def audio_duration_ms(path: Path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe and FFMPEG_BIN:
        cand = Path(FFMPEG_BIN).with_name("ffprobe" + Path(FFMPEG_BIN).suffix)
        ffprobe = str(cand) if cand.is_file() else None
    if ffprobe:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, stdin=subprocess.DEVNULL)
        try:
            return max(1000, int(float(r.stdout.decode().strip()) * 1000))
        except ValueError:
            pass
    if path.suffix == ".wav":
        with wave.open(str(path), "rb") as w:
            return max(1000, int(w.getnframes() / w.getframerate() * 1000))
    return max(1000, int(path.stat().st_size * 8 / 48000 * 1000))  # edge-tts 48 kbps mp3


def to_line_audio(src: Path):
    """轉成 LINE 可播放的 m4a；沒 ffmpeg 時只有 mp3 能直接送。"""
    if src.suffix == ".m4a":
        return src
    if FFMPEG_BIN:
        dst = src.with_suffix(".m4a")
        conv = subprocess.run([FFMPEG_BIN, "-y", "-loglevel", "error", "-i", str(src),
                               "-c:a", "aac", "-b:a", "96k", str(dst)],
                              capture_output=True, stdin=subprocess.DEVNULL)
        if conv.returncode == 0 and dst.is_file():
            return dst
        log("ffmpeg 轉 m4a 失敗：" + conv.stderr.decode("utf-8", "replace")[-200:])
    if src.suffix == ".mp3":
        return src
    log(f"{src.suffix} 需要 ffmpeg 轉檔才能傳到 LINE（請安裝 ffmpeg）")
    return None


SYNTHS = {"gemini": synth_gemini, "edge": synth_edge, "http": synth_http}


def make_voice(text: str):
    """文字轉語音，回傳 (公開網址, 長度毫秒)；全部引擎都失敗回傳 None。"""
    base_url = public_base_url()
    if not base_url:
        log("還不知道公開網址（尚未收到 webhook，也沒設 PUBLIC_BASE_URL），改用文字回覆")
        return None
    speak = _EMOJI_RE.sub("", text).strip()
    if not speak:
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_media()
    engines = [VOICE_ENGINE] + [e for e in VOICE_FALLBACK if e != VOICE_ENGINE]
    for engine in engines:
        synth = SYNTHS.get(engine)
        if not synth:
            continue
        # 只有 Gemini 會把 <laugh> 之類的標記演出來，其他引擎先拿掉，免得被念出來
        engine_text = speak if engine == "gemini" else _AUDIO_TAG_RE.sub("", speak).strip()
        name = uuid.uuid4().hex
        raw = synth(engine_text, MEDIA_DIR / name)
        if not raw:
            continue
        out = to_line_audio(raw)
        if raw != out:
            raw.unlink(missing_ok=True)
        if not out:
            continue
        duration_ms = audio_duration_ms(out)
        log(f"已產生語音（{engine}）{out.name}，約 {duration_ms/1000:.1f} 秒")
        return f"{base_url}/media/{out.name}", duration_ms
    return None


def split_voice_tag(answer: str):
    """回傳 (是否要語音, 去掉標記的文字)。"""
    m = re.match(r"^\s*[`*_「『\(（]*\[\s*VOICE\s*\][`*_」』\)）]*\s*", answer, flags=re.I)
    if m:
        return True, answer[m.end():].strip()
    return False, re.sub(r"\[\s*VOICE\s*\]", "", answer, flags=re.I).strip()


def send_reply(to: str, reply_token, answer: str, received_at: float) -> None:
    want_voice, text = split_voice_tag(answer)
    if VOICE_MODE == "always":
        want_voice = True
    if VOICE_MODE == "off" or not want_voice or not text:
        send_text(to, reply_token, text or answer, received_at)
        return
    if len(text) > VOICE_MAX_CHARS:
        log(f"內容 {len(text)} 字超過 VOICE_MAX_CHARS，改用文字")
        send_text(to, reply_token, text, received_at)
        return
    voice = make_voice(text)
    plain = _AUDIO_TAG_RE.sub("", text).strip()
    if not voice:
        send_text(to, reply_token, plain, received_at)
        return
    url, duration_ms = voice
    messages = [{"type": "audio", "originalContentUrl": url, "duration": duration_ms}]
    if VOICE_WITH_TEXT:
        messages += [{"type": "text", "text": c} for c in split_text(plain)][:4]
    send_messages(to, reply_token, messages, received_at)


def show_loading(user_id: str) -> None:
    """一對一聊天中顯示「輸入中」動畫（最多 60 秒），失敗不影響主流程。"""
    line_post("/v2/bot/chat/loading/start", {"chatId": user_id, "loadingSeconds": 60})


# ------------------------------------------------------------------ agy ----
def run_agy(prompt: str, conversation_id):
    """回傳 (回覆文字, 新的 conversation_id 或 None)。"""
    cmd = [AGY_BIN]
    if AGY_MODEL:
        cmd += ["--model", AGY_MODEL]
    if AGY_EFFORT:
        cmd += ["--effort", AGY_EFFORT]
    if AGY_SANDBOX:
        cmd += ["--sandbox"]
    if AGY_TIMEOUT:
        cmd += ["--print-timeout", AGY_TIMEOUT]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    cmd += ["--output-format", "json", "-p", prompt]

    AGY_WORKDIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGY_WORKDIR),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15 * 60,
        )
    except FileNotFoundError:
        return f"找不到 agy 執行檔（{AGY_BIN}），請確認已安裝或在 .env 設定 AGY_BIN。", None
    except subprocess.TimeoutExpired:
        return "agy 執行超過 15 分鐘，已中止。", None

    stdout = proc.stdout.decode("utf-8", "replace").strip()
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    if stderr:
        log("agy stderr:", stderr[:500])

    envelope = None
    for line in reversed(stdout.splitlines()):  # JSON envelope 在最後一行
        line = line.strip()
        if line.startswith("{"):
            try:
                envelope = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if envelope is None:
        detail = stderr or stdout or f"exit code {proc.returncode}"
        return f"agy 沒有回傳可解析的結果：\n{detail[:800]}", None

    status = envelope.get("status")
    if status != "SUCCESS":
        err = envelope.get("error") or status or "未知錯誤"
        hint = ""
        if "auth" in str(err).lower():
            hint = "\n（請在這台電腦先執行一次互動式 agy 完成登入）"
        return f"agy 執行失敗：{err}{hint}", None

    usage = envelope.get("usage") or {}
    log(f"agy ok, {envelope.get('duration_seconds')}s, tokens={usage.get('total_tokens')}")
    return envelope.get("response") or "", envelope.get("conversation_id") or None


# --------------------------------------------------------------- 工作佇列 ----
jobs: "queue.Queue[dict]" = queue.Queue()

BARE_COMMANDS = ("/new", "/reset", "/id", "/help", "/?")

HELP_TEXT = (
    "可用指令：\n"
    "（群組裡要 @bot 或用 " + " / ".join(GROUP_TRIGGERS) + " 開頭才會回應）\n"
    "/new　開始新對話（清除上下文）\n"
    "/id　顯示你的 LINE userId\n"
    "/help　顯示這段說明\n"
    "其他訊息都會交給 agy 回答。"
)


# ---------------------------------------------------------------- 圖片 ----
IMAGE_DIR = AGY_WORKDIR / "images"
_img_lock = threading.Lock()
_images_by_id = {}   # LINE messageId -> ImageRef（給「回覆圖片」用）
_pending_images = {}  # chat_id -> {"items": [(path, user_id, t)], "timer", "reply_token", "received_at"}


def cleanup_images() -> None:
    cutoff = time.time() - IMAGE_KEEP_HOURS * 3600
    for f in IMAGE_DIR.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


class ImageRef:
    """一張正在背景下載的圖片；組 prompt 時才等它下載完，避免「圖還沒下載完文字就先處理」。"""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.path = None
        self.done = threading.Event()

    def resolve(self, timeout: float = 30):
        self.done.wait(timeout)
        return self.path


def _download_image(ref: ImageRef) -> None:
    try:
        req = urllib.request.Request(
            f"{LINE_DATA_API_BASE}/v2/bot/message/{ref.message_id}/content",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            data = resp.read()
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(ctype, ".jpg")
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        cleanup_images()
        path = IMAGE_DIR / f"{ref.message_id}{ext}"
        path.write_bytes(data)
        ref.path = str(path)
        log(f"已存圖片 {path.name}（{len(data)//1024} KB）")
    except (urllib.error.URLError, OSError) as e:
        log(f"下載圖片失敗 {ref.message_id}: {e}")
    finally:
        ref.done.set()


def start_image_download(message_id: str) -> ImageRef:
    ref = ImageRef(message_id)
    with _img_lock:
        _images_by_id[message_id] = ref
        if len(_images_by_id) > 500:
            for k in list(_images_by_id)[:-500]:
                _images_by_id.pop(k, None)
    threading.Thread(target=_download_image, args=(ref,), daemon=True).start()
    return ref


def quoted_image(message: dict):
    qid = message.get("quotedMessageId")
    if not qid:
        return None
    with _img_lock:
        return _images_by_id.get(qid)


def add_pending_image(chat_id, user_id, path, reply_token, on_timeout=None) -> None:
    with _img_lock:
        entry = _pending_images.setdefault(chat_id, {"items": []})
        entry["items"].append((path, user_id, time.time()))  # path 是 ImageRef
        entry["reply_token"], entry["received_at"] = reply_token, time.time()
        if entry.get("timer"):
            entry["timer"].cancel()
            entry["timer"] = None
        if on_timeout:
            timer = threading.Timer(IMAGE_WAIT, on_timeout, args=(chat_id,))
            timer.daemon = True
            entry["timer"] = timer
            timer.start()


def take_pending_images(chat_id):
    """取出這個聊天室最近（IMAGE_ATTACH_WINDOW 內）還沒被問過的圖片。"""
    with _img_lock:
        entry = _pending_images.pop(chat_id, None)
    if not entry:
        return [], None, 0.0
    if entry.get("timer"):
        entry["timer"].cancel()
    now = time.time()
    items = [it for it in entry["items"] if now - it[2] <= IMAGE_ATTACH_WINDOW]
    return items, entry.get("reply_token"), entry.get("received_at", 0.0)


def flush_private_images(chat_id: str) -> None:
    """私訊傳圖後 IMAGE_WAIT 秒內沒有補文字：直接就圖片回應。"""
    items, reply_token, received_at = take_pending_images(chat_id)
    if items:
        jobs.put({
            "chat_id": chat_id, "user_id": chat_id, "is_group": False, "text": "",
            "images": [it[0] for it in items], "history": [],
            "reply_token": reply_token, "received_at": received_at,
        })


def image_tags(refs) -> str:
    tags = []
    for ref in refs:
        path = ref.resolve()
        tags.append(f"[圖片：{path}]" if path else "（有一張圖片下載失敗）")
    return " ".join(tags)


# 群組訊息暫存：{group_id: {"msgs": [(user_id, text)], "reply_token", "received_at", "timer"}}
_group_lock = threading.Lock()
_group_buf = {}


def take_group_buffer(group_id: str):
    """取出並清空某群組暫存的訊息，同時取消尚未觸發的自動計時器。"""
    with _group_lock:
        entry = _group_buf.pop(group_id, None)
    if not entry:
        return [], None, 0.0
    if entry.get("timer"):
        entry["timer"].cancel()
    return entry["msgs"], entry.get("reply_token"), entry.get("received_at", 0.0)


def buffer_group_message(group_id: str, user_id: str, text, reply_token, ts=None) -> None:
    with _group_lock:
        entry = _group_buf.setdefault(group_id, {"msgs": []})
        entry["msgs"].append((user_id, text[:500] if isinstance(text, str) else text,
                              ts or time.time() * 1000, False))
        entry["msgs"].sort(key=lambda m: m[2])  # 依 LINE 訊息時間排序（webhook 可能同時到）
        del entry["msgs"][:-GROUP_CONTEXT_MAX]
        entry["reply_token"], entry["received_at"] = reply_token, time.time()
        if GROUP_MODE == "auto":
            if entry.get("timer"):
                entry["timer"].cancel()
            timer = threading.Timer(GROUP_AUTO_DELAY, flush_auto_batch, args=(group_id,))
            timer.daemon = True
            entry["timer"] = timer
            timer.start()


def flush_auto_batch(group_id: str) -> None:
    with _group_lock:
        entry = _group_buf.get(group_id)
        if not entry or not entry.get("timer"):
            return
        entry["timer"] = None
        new = [m for m in entry["msgs"] if not m[3]]
        named = bool(BOT_NAMES) and any(
            isinstance(t, str) and any(n in t for n in BOT_NAMES) for _, t, *_ in new
        )
        if not named and random.random() >= AUTO_REPLY_CHANCE:
            # 沒抽中：不呼叫 agy，訊息留著當之後的上下文
            entry["msgs"] = [(u, t, ts, True) for u, t, ts, *_ in entry["msgs"]]
            log(f"群組 {group_id}：這段沒抽中（機率 {AUTO_REPLY_CHANCE:.0%}），保留為上下文")
            return
    msgs, reply_token, received_at = take_group_buffer(group_id)
    if msgs:
        jobs.put({
            "chat_id": group_id, "user_id": msgs[-1][0], "is_group": True,
            "text": "", "history": msgs, "auto": True, "named": named,
            "reply_token": reply_token, "received_at": received_at,
        })


def format_history(group_id: str, msgs) -> str:
    return "\n".join(
        f"{get_display_name(group_id, uid)}："
        + (t if isinstance(t, str) else image_tags([t]))
        for uid, t, *_ in msgs
    )


def handle_job(job: dict) -> None:
    chat_id, user_id = job["chat_id"], job["user_id"]
    is_group = job["is_group"]
    text = job["text"].strip()
    reply_token, received_at = job["reply_token"], job["received_at"]
    history = job.get("history") or []
    images = job.get("images") or []
    if job.get("take_pending"):
        # 輪到這題時才收集剛傳的圖片（圖片和文字的 webhook 可能幾乎同時到）
        images = [it[0] for it in take_pending_images(chat_id)[0]] + images

    if job.get("auto"):
        older = [m for m in history if len(m) > 3 and m[3]]
        newer = [m for m in history if not (len(m) > 3 and m[3])]
        body = AUTO_NOTE
        if older:
            body += "\n\n（稍早的群組對話，你沒有回應過，供參考）\n" + format_history(chat_id, older)
        body += "\n\n（群組新訊息）\n" + format_history(chat_id, newer or history)
        if job.get("named"):
            body = "（有人在訊息裡提到你的名字，請自然地回應，不可以輸出 [SKIP]）\n" + body
        run_and_reply(job, body, allow_skip=not job.get("named"))
        return

    cmd = text.lower() if not images else ""
    if cmd in ("/new", "/reset"):
        set_conversation(chat_id, None)
        if is_group:
            take_group_buffer(chat_id)
        send_text(chat_id, reply_token, "已開始新對話。", received_at)
        return
    if cmd == "/id":
        info = f"你的 userId：{user_id}"
        if is_group:
            info += f"\n這個群組的 groupId：{chat_id}"
        send_text(chat_id, reply_token, info, received_at)
        return
    if cmd in ("/help", "/?"):
        send_text(chat_id, reply_token, HELP_TEXT, received_at)
        return
    if not text and not images:
        send_text(chat_id, reply_token, "要問什麼呢？", received_at)
        return

    content = text
    if images:
        content = (text + "\n" if text else "（傳了圖片）") + image_tags(images)
    if not is_group:
        show_loading(chat_id)  # 輸入中動畫只支援一對一聊天
        body = content
    else:
        body = f"{get_display_name(chat_id, user_id)}：{content}"
        if history:
            body = ("（在這之前群組裡的對話，供你參考）\n" + format_history(chat_id, history)
                    + "\n\n（現在有人在問你）\n" + body)
        if GROUP_MODE == "auto":
            body = FORCE_REPLY_NOTE + "\n" + body
    run_and_reply(job, body)


def is_skip(answer: str) -> bool:
    """回覆是空的，或只有 [SKIP]（容忍模型多包引號、粗體、句號等符號）。"""
    core = re.sub(r"[\s`'\"「」『』*_.。!！:：()（）\[\]]", "", answer).upper()
    return core in ("", "SKIP")


def clean_answer(answer: str) -> str:
    return re.sub(r"\[\s*SKIP\s*\]", "", answer, flags=re.I).strip()


def run_and_reply(job: dict, body: str, allow_skip: bool = False) -> None:
    chat_id, is_group = job["chat_id"], job["is_group"]
    persona = PERSONA
    if is_group:
        persona += "\n" + GROUP_NOTE
        if GROUP_MODE == "auto":
            persona += "\n" + AUTO_NOTE

    if VOICE_MODE == "model":
        persona += "\n" + VOICE_NOTE
        if VOICE_KEYWORDS.search(body.split("（群組新訊息）")[-1]):
            body += f"\n（對方想聽語音，請在回覆最前面加上 {VOICE_TAG}，並用口語回答）"
    if "[圖片：" in body:
        body += "\n" + IMAGE_NOTE
    conversation_id = get_conversation(chat_id)
    prompt = body if conversation_id else f"{persona}\n\n---\n{body}"
    answer, new_cid = run_agy(prompt, conversation_id)

    # 舊對話可能已失效：清掉後用新對話重試一次
    if conversation_id and new_cid is None:
        log("接續對話失敗，改開新對話重試")
        answer, new_cid = run_agy(f"{persona}\n\n---\n{body}", None)

    if new_cid:
        set_conversation(chat_id, new_cid)
    if is_skip(answer):
        if allow_skip:
            log(f"群組 {chat_id}：模型判斷不需回應")
            return
        # 被直接叫到卻回 [SKIP]：明確要求它回覆，再試一次
        log("被點名卻回了 [SKIP]，重試一次")
        answer, retry_cid = run_agy(
            FORCE_REPLY_NOTE + "\n剛剛那則訊息是直接問你的，請重新回答。", new_cid or conversation_id
        )
        if retry_cid:
            set_conversation(chat_id, retry_cid)
        if is_skip(answer):
            answer = "我剛剛恍神了，可以再問我一次嗎？（若一直這樣，請傳 /new 重開對話）"
    send_reply(chat_id, job["reply_token"], clean_answer(answer), job["received_at"])


def worker() -> None:
    while True:
        job = jobs.get()
        try:
            handle_job(job)
        except Exception as e:  # 單一訊息出錯不讓整個 bot 掛掉
            log("處理訊息時發生例外：", repr(e))
            try:
                send_text(job["chat_id"], None, f"bot 內部錯誤：{e!r}", 0)
            except Exception:
                pass
        finally:
            jobs.task_done()


# ------------------------------------------------------------- Webhook ----
class Handler(BaseHTTPRequestHandler):
    server_version = "line-agy-bot"

    def log_message(self, fmt, *args):  # 安靜一點，只記我們自己的 log
        pass

    def _send(self, code: int, body: str = "OK") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/media/"):
            self.serve_media(path[len("/media/"):])
            return
        self._send(200, "line-agy-bot is running")

    def serve_media(self, name: str) -> None:
        if not _MEDIA_NAME_RE.match(name):
            self._send(404, "Not Found")
            return
        f = MEDIA_DIR / name
        if not f.is_file():
            self._send(404, "Not Found")
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mp4" if name.endswith(".m4a") else "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?")[0] != "/callback":
            self._send(404, "Not Found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if not verify_signature(body, self.headers.get("X-Line-Signature", "")):
            log("簽章驗證失敗，已拒絕")
            self._send(400, "Bad signature")
            return
        self._send(200)  # 先回 200 給 LINE，實際處理丟到背景
        note_public_host(self.headers)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return
        for event in payload.get("events", []):
            self.dispatch(event)

    def dispatch(self, event: dict) -> None:
        source = event.get("source") or {}
        stype = source.get("type")
        user_id = source.get("userId", "")
        reply_token = event.get("replyToken")

        if stype == "group":
            group_id = source.get("groupId", "")
            if event.get("type") == "join":
                log(f"bot 被加入群組 {group_id}，重開這個群組的對話")
                set_conversation(group_id, None)
                take_group_buffer(group_id)
            if event.get("type") != "message":
                return
            self.dispatch_group(event, group_id, user_id, reply_token)
            return
        if stype != "user" or event.get("type") != "message":
            return  # 多人聊天室（room）與其他事件忽略

        message = event.get("message") or {}
        if user_id not in ALLOWED_USER_IDS:
            if not ALLOWED_USER_IDS:
                # 設定模式：還沒設白名單時，只回傳 userId，不呼叫 agy
                log(f"設定模式：收到 userId {user_id}")
                send_text(
                    user_id, reply_token,
                    f"設定模式：你的 userId 是\n{user_id}\n"
                    "請把它填進 .env 的 ALLOWED_USER_IDS 後重新啟動 bot。",
                    time.time(),
                )
            else:
                log(f"忽略非白名單使用者 {user_id}")
            return
        mtype = message.get("type")
        if mtype == "image":
            ref = start_image_download(message.get("id", ""))
            add_pending_image(user_id, user_id, ref, reply_token, on_timeout=flush_private_images)
            return
        if mtype != "text":
            send_text(user_id, reply_token, "目前只支援文字和圖片。", time.time())
            return
        q = quoted_image(message)
        self.enqueue(user_id, user_id, False, message.get("text", ""), reply_token,
                     images=[q] if q else [], take_pending=not q)

    def dispatch_group(self, event, group_id, user_id, reply_token) -> None:
        message = event.get("message") or {}
        mtype = message.get("type")
        if mtype == "image":
            if group_id not in ALLOWED_GROUP_IDS:
                return
            ref = start_image_download(message.get("id", ""))
            if GROUP_MODE == "trigger":
                add_pending_image(group_id, user_id, ref, reply_token)  # 等有人叫 bot 時一起附上
            else:
                buffer_group_message(group_id, user_id, ref, reply_token, event.get("timestamp"))
            return
        if mtype != "text":
            return
        text = message.get("text", "")

        if group_id not in ALLOWED_GROUP_IDS:
            # 讓擁有者在群組裡用 /id 取得 groupId，其他情況一律不出聲
            if text.strip().lower() == "/id" and user_id in ALLOWED_USER_IDS:
                send_text(group_id, reply_token,
                          f"這個群組的 groupId：\n{group_id}\n"
                          "填進 .env 的 ALLOWED_GROUP_IDS 後重啟即可啟用。", time.time())
            else:
                log(f"忽略非白名單群組 {group_id}")
            return

        prompt = self.extract_group_prompt(message, text)
        if prompt is None and text.strip().lower() in BARE_COMMANDS:
            prompt = text.strip()  # 指令不用加觸發詞
        if prompt is None:
            # 沒被呼叫：trigger 模式直接忽略；其他模式先記下來當上下文
            if GROUP_MODE != "trigger" and not text.lstrip().startswith("/"):
                buffer_group_message(group_id, user_id, text, reply_token, event.get("timestamp"))
            return
        history = []
        if GROUP_MODE != "trigger":
            history, _, _ = take_group_buffer(group_id)
        q = quoted_image(message)  # 明確回覆某張圖時只看那張
        self.enqueue(group_id, user_id, True, prompt, reply_token, history,
                     images=[q] if q else [], take_pending=(GROUP_MODE == "trigger" and not q))

    @staticmethod
    def extract_group_prompt(message: dict, text: str):
        """有 @bot 或觸發詞開頭才回傳要問的內容，否則回傳 None。"""
        mentionees = (message.get("mention") or {}).get("mentionees") or []
        self_mentions = [m for m in mentionees if m.get("isSelf")]
        if self_mentions:
            # 依位置從後往前刪掉 @bot 文字
            for m in sorted(self_mentions, key=lambda m: m.get("index", 0), reverse=True):
                i, n = m.get("index", 0), m.get("length", 0)
                text = text[:i] + text[i + n:]
            return text.strip()
        stripped = text.lstrip()
        for trig in GROUP_TRIGGERS:
            if stripped.lower().startswith(trig.lower()):
                return stripped[len(trig):].strip()
        return None

    @staticmethod
    def enqueue(chat_id, user_id, is_group, text, reply_token, history=None, images=None,
                take_pending=False) -> None:
        jobs.put({
            "take_pending": take_pending,
            "history": history or [],
            "images": images or [],
            "chat_id": chat_id,
            "user_id": user_id,
            "is_group": is_group,
            "text": text,
            "reply_token": reply_token,
            "received_at": time.time(),
        })


def main() -> None:
    missing = [k for k, v in (("LINE_CHANNEL_SECRET", CHANNEL_SECRET),
                              ("LINE_CHANNEL_ACCESS_TOKEN", ACCESS_TOKEN)) if not v]
    if missing:
        sys.exit(f"缺少設定：{', '.join(missing)}（請參考 .env.example）")

    threading.Thread(target=worker, daemon=True).start()
    log(DOTENV_INFO)
    log(f"角色設定來源: {PERSONA_SOURCE}（{PERSONA[:30].strip().replace(chr(10), ' ')}…）")
    log(f"agy: {AGY_BIN} | model: {AGY_MODEL or '(帳號預設)'} | sandbox: {AGY_SANDBOX}")
    log(f"工作目錄: {AGY_WORKDIR}")
    voice_desc = {"gemini": f"Gemini {GEMINI_TTS_MODEL} / {GEMINI_TTS_VOICE}"
                  + ("" if GEMINI_API_KEY else "（缺 GEMINI_API_KEY）"),
                  "edge": f"edge-tts / {VOICE_NAME}", "http": f"本機 {VOICE_HTTP_URL[:40]}"}
    log(f"語音: {VOICE_MODE} | 引擎: {voice_desc.get(VOICE_ENGINE, VOICE_ENGINE)}"
        f" | 備援: {','.join(VOICE_FALLBACK) or '無'} | 轉檔: {'ffmpeg' if FFMPEG_BIN else '無 ffmpeg'}"
        + (f" | 公開網址: {PUBLIC_BASE_URL}" if PUBLIC_BASE_URL else " | 公開網址: 收到第一個 webhook 時自動偵測"))
    log("使用者白名單: " + (", ".join(sorted(ALLOWED_USER_IDS)) or "（未設定，設定模式）"))
    log("群組白名單: " + (", ".join(sorted(ALLOWED_GROUP_IDS)) or "（未設定，群組停用）")
        + f" | 觸發詞: {', '.join(GROUP_TRIGGERS)} 或 @bot | 模式: {GROUP_MODE}"
        + (f"（安靜 {GROUP_AUTO_DELAY:g} 秒後送出）" if GROUP_MODE == "auto" else ""))
    log(f"Webhook 監聽 http://0.0.0.0:{PORT}/callback")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
