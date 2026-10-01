"""
bot_core.py
------------
هستهٔ ربات: ارتباط با API روبیکا (فقط requests)، پارس کپشن، ارسال
پیام/فایل/مود/ویدیو، مدیریت state و ابزارهای کمکی.

منطق دستورها، آرشیو کانال منبع و زمان‌بند پست‌گذاری در run_bot.py است.

نکات مهم این نسخه (رفع باگ‌ها):
- نوع فایل (عکس/ویدیو/فایل) دیگه فقط به فیلد file_type وابسته نیست؛ چون
  API روبیکا برای فایل‌های دریافتی معمولاً فقط file_id/file_name/size
  می‌ده. تشخیص از روی پسوند نام فایل هم انجام می‌شه. همین باعث می‌شد
  عکس به‌جای فایل (یا فایل به‌جای عکس) ارسال بشه.
- api_call حالا فیلد status پاسخ روبیکا رو چک می‌کنه؛ قبلاً یک ارسال
  ناموفق (با HTTP 200) موفق حساب می‌شد یا برعکس، و زنجیرهٔ fallback باعث
  ارسال چندباره می‌شد.
- برای متدهای ارسال، بعد از timeout دیگه کورکورانه دوباره ارسال نمی‌شه
  (ممکنه پیام رسیده باشه) تا پست تکراری ایجاد نشه.
- پیشرفت ارسال مود (کاور/فایل) ذخیره می‌شه؛ اگه فایل شکست بخوره، دفعهٔ
  بعد کاور دوباره فرستاده نمی‌شه.
- فقط مودهایی که هم کاور و هم فایل دارن انتخاب می‌شن.
"""

import copy
import hashlib
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

TEHRAN_OFFSET = timedelta(hours=3, minutes=30)
MAX_ERRORS_STORED = 200
ERRORS_PER_PAGE = 5
ERROR_NOTIFY_COOLDOWN_MINUTES = 10
REQUEST_TIMEOUT = 20
UPLOAD_TIMEOUT = 120
MAX_PROCESSED_IDS = 1000
MAX_STORED_MODS = 800
MAX_STORED_VIDEOS = 800
MAX_USED_IDS = 1500
MAX_MESSAGE_LEN = 4000
MAX_POST_ATTEMPTS = 3


class RubikaAPIError(RuntimeError):
    """پاسخ خطا از سرور روبیکا (ارسال قطعاً انجام نشده)."""


class SendUncertainError(RuntimeError):
    """قطع ارتباط/timeout حین ارسال؛ ممکنه پیام رسیده باشه، پس دوباره
    نمی‌فرستیمش تا پست تکراری نشه."""


# ---------------------------------------------------------------------------
# فایل‌های JSON و state
# ---------------------------------------------------------------------------
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """ذخیرهٔ اتمیک: اول در فایل موقت، بعد جایگزینی؛ تا قطع‌شدن وسط نوشتن
    state.json رو خراب نکنه."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    return load_json(CONFIG_PATH)


DEFAULT_STATE = {
    "last_offset_id": None,
    "mods": [],
    "videos": [],
    "files_by_number": {},
    "pending_photo": None,
    "orphan_files": {},
    "recent_files": {},
    "used_mods_per_channel": {},
    "used_videos_per_channel": {},
    "mod_progress": {},
    "channel_activated": {},
    "known_users": [],
    "blocked_users": {},
    "awaiting_ticket": [],
    "awaiting_broadcast": False,
    "awaiting_channelcast": False,
    "awaiting_panel_selection": False,
    "sent_log": {},
    "errors": [],
    "last_error_notify_time": None,
    "bug_page_offset": 0,
    "pending_file_requests": {},
    "processed_updates": [],
    "activity_log": {},
    "owner_keypad_set": False,
    "posted_slots_today": {"date": None, "mod": {}, "video": {}},
}
LEGACY_STATE_KEYS = ("posted_hours_today", "awaiting_newguid")


def normalize_state(state):
    """کلیدهای جاافتاده رو می‌سازه و نوع‌های اشتباه رو درست می‌کنه تا با یک
    state قدیمی/ناقص KeyError نخوریم."""
    if not isinstance(state, dict):
        state = {}
    for key, default in DEFAULT_STATE.items():
        cur = state.get(key)
        if key not in state:
            state[key] = copy.deepcopy(default)
        elif isinstance(default, (list, dict)) and not isinstance(cur, type(default)):
            state[key] = copy.deepcopy(default)
    for key in LEGACY_STATE_KEYS:
        state.pop(key, None)
    posted = state["posted_slots_today"]
    for sub in ("mod", "video"):
        if not isinstance(posted.get(sub), dict):
            posted[sub] = {}
    posted.setdefault("date", None)
    return state


def load_state():
    if not STATE_PATH.exists():
        return normalize_state({})
    try:
        return normalize_state(load_json(STATE_PATH))
    except (json.JSONDecodeError, OSError) as e:
        # نگه‌داشتن نسخهٔ خراب برای بررسی، و شروع با state خالی
        try:
            os.replace(STATE_PATH, str(STATE_PATH) + ".broken")
        except OSError:
            pass
        print(f"DEBUG: state.json خراب بود ({e}); با state خالی شروع شد.")
        return normalize_state({})


def save_state(state):
    save_json(STATE_PATH, state)


def tehran_now():
    return datetime.now(timezone.utc) + TEHRAN_OFFSET


# ---------------------------------------------------------------------------
# ابزارهای متن
# ---------------------------------------------------------------------------
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(s):
    return str(s or "").translate(_DIGIT_MAP)


def clean_command_text(text):
    """متن رو برای تشخیص دستور/شماره تمیز می‌کنه: حذف نیم‌فاصله و
    کاراکترهای نامرئی، تبدیل رقم فارسی/عربی به انگلیسی. (برای ارسال متن
    به دیگران از متن خام استفاده کن، نه این.)"""
    return normalize_digits(_INVISIBLE_RE.sub("", str(text or ""))).strip()


def channel_label(ch):
    return ch.get("name") or ch.get("channel_link") or ch.get("guid") or "بدون‌نام"


# ---------------------------------------------------------------------------
# ارتباط خام با Rubika Bot API
# ---------------------------------------------------------------------------
def _is_send_method(method):
    return method.startswith("send") or method == "forwardMessage"


def api_call(token, method, payload=None, retries=3, backoff_seconds=2):
    """
    فراخوانی متد API روبیکا.
    - خطاهای موقت سرور (429/502/503/504) و قطعی‌های شبکه برای متدهای
      «خواندنی» با فاصله دوباره امتحان می‌شن.
    - برای متدهای «ارسال» (sendMessage/sendFile/forwardMessage) فقط وقتی
      دوباره امتحان می‌کنیم که مطمئنیم درخواست اصلاً به سرور نرسیده
      (ConnectTimeout) یا سرور صراحتاً 429/503 گفته؛ در غیر این‌صورت
      SendUncertainError می‌دیم تا پیام دوبار نره.
    - فیلد status پاسخ چک می‌شه؛ غیر از OK یعنی خطا.
    """
    url = f"https://botapi.rubika.ir/v3/{token}/{method}"
    is_send = _is_send_method(method)
    retry_codes = (429, 503) if is_send else (429, 502, 503, 504)
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload or {}, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.ConnectTimeout as e:
            last_exc = e
            time.sleep(backoff_seconds * attempt)
            continue
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if is_send:
                raise SendUncertainError(f"{method}: قطع ارتباط حین ارسال ({e})") from e
            last_exc = e
            time.sleep(backoff_seconds * attempt)
            continue

        if resp.status_code in retry_codes:
            last_exc = RubikaAPIError(f"{method}: HTTP {resp.status_code} موقت (تلاش {attempt}/{retries})")
            time.sleep(backoff_seconds * attempt)
            continue

        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code >= 400:
            detail = ""
            if isinstance(body, dict):
                detail = f"{body.get('status', '')} {body.get('status_det', '') or body.get('dev_message', '')}".strip()
            if not detail:
                detail = resp.text[:200]
            if is_send and resp.status_code >= 500:
                raise SendUncertainError(f"{method}: HTTP {resp.status_code} {detail}")
            raise RubikaAPIError(f"{method}: HTTP {resp.status_code} {detail}")

        if isinstance(body, dict):
            status = body.get("status")
            if status is not None and str(status).upper() != "OK":
                detail = body.get("status_det") or body.get("dev_message") or ""
                raise RubikaAPIError(f"{method}: {status} {detail}".strip())
            return body.get("data", body)
        return body

    raise last_exc or RuntimeError(f"فراخوانی {method} بدون دلیل مشخص شکست خورد")


def get_me(token):
    return api_call(token, "getMe")


def get_updates(token, offset_id=None, limit=50):
    payload = {"limit": limit}
    if offset_id:
        payload["offset_id"] = offset_id
    return api_call(token, "getUpdates", payload)


def get_file(token, file_id):
    return api_call(token, "getFile", {"file_id": file_id})


def request_send_file(token, file_type):
    return api_call(token, "requestSendFile", {"type": file_type or "File"})


def get_chat(token, chat_id):
    return api_call(token, "getChat", {"chat_id": chat_id})


def forward_message(token, from_chat_id, message_id, to_chat_id):
    return api_call(token, "forwardMessage", {
        "from_chat_id": from_chat_id, "message_id": message_id, "to_chat_id": to_chat_id,
    })


def set_chat_keypad(token, chat_id, buttons):
    """کیبورد ثابت پایین صفحه. buttons لیستی از متن دکمه‌هاست."""
    rows = [{"buttons": [{"id": str(i), "type": "Simple", "button_text": t}]} for i, t in enumerate(buttons)]
    payload = {
        "chat_id": chat_id,
        "chat_keypad_type": "New",
        "chat_keypad": {"rows": rows, "resize_keyboard": True, "one_time_keyboard": False},
    }
    return api_call(token, "editChatKeypad", payload)


# ---------------------------------------------------------------------------
# فرمت‌دهی متن (metadata)
# ---------------------------------------------------------------------------
FORMAT_TYPES = {
    "bold": "Bold", "italic": "Italic", "underline": "Underline",
    "strike": "Strike", "spoiler": "Spoiler", "mono": "Mono",
    "pre": "Pre", "quote": "Quote",
}


def _utf16_len(s):
    """طول رشته بر حسب واحدهای UTF-16 (ایموجی دو واحد حساب می‌شه)."""
    return len(s.encode("utf-16-le")) // 2


def build_text_with_metadata(parts):
    """parts: لیست (متن, نوع‌فرمت یا None) → (متن نهایی, metadata)."""
    text = ""
    metadata = []
    pos16 = 0
    for chunk, fmt in parts:
        if not chunk:
            continue
        text += chunk
        length16 = _utf16_len(chunk)
        if fmt:
            metadata.append({"type": FORMAT_TYPES[fmt], "from_index": pos16, "length": length16})
        pos16 += length16
    return text, metadata


_CUSTOM_QUOTE_RE = re.compile(r"/Quote(.*?)/Quote", re.IGNORECASE | re.DOTALL)


def parse_custom_markup(text):
    """هر بخش بین دو تا /Quote رو به یک بلوکِ نقل‌قولِ روی خط خودش تبدیل
    می‌کنه.

    رفع باگ: متادیتای نوع Quote در روبیکا فقط وقتی که بازهٔ نقل‌قول از
    *ابتدای یک خط* شروع بشه به‌صورت بصری اعمال می‌شه؛ اگه چیزی (even یک
    فاصلهٔ خالی) قبلش روی همون خط باشه، سرور درخواست رو رد نمی‌کنه (پس
    خطایی هم دیده نمی‌شه) ولی ظاهر باکس نقل‌قول اصلاً نمایش داده نمی‌شه.
    چون متن‌های config.json معمولاً یک فاصلهٔ اضافه قبل/بعد از خودِ کلمهٔ
    «/Quote» دارن (کپی‌پیست)، قبلاً همون یک فاصله باعث می‌شد from_index
    از صفر/ابتدای خط جابه‌جا بشه و نقل‌قول اصلاً دیده نشه — با اینکه
    کلمهٔ «/Quote» خودش درست حذف می‌شد و ظاهراً «مشکلی» به چشم نمی‌اومد.
    الان: فاصله‌های خام دور خودِ نشانه حذف می‌شن، و قبل/بعدِ هر نقل‌قول
    (اگه از قبل با \n تموم نشده باشه) یک خط جدید اضافه می‌شه.
    """
    if not text:
        return []

    raw = []  # (chunk, is_quote)
    last = 0
    for m in _CUSTOM_QUOTE_RE.finditer(text):
        raw.append((text[last:m.start()], False))
        raw.append((m.group(1) or "", True))
        last = m.end()
    raw.append((text[last:], False))

    parts = []
    for chunk, is_quote in raw:
        if is_quote:
            body = chunk.strip(" \t\r\n")
            if not body:
                continue
            if parts and not parts[-1][0].endswith("\n"):
                parts.append(("\n", None))
            parts.append((body, "quote"))
            parts.append(("\n", None))  # محتوای بعدی هم از خط جدید شروع بشه
        else:
            core_text = chunk.strip(" \t")
            if not core_text:
                continue
            parts.append((core_text, None))

    while parts and parts[-1] == ("\n", None):
        parts.pop()

    return parts


def send_message(token, chat_id, text, metadata=None):
    text = str(text)
    if len(text) > MAX_MESSAGE_LEN:
        # پیام خیلی بلند: تکه‌تکه (بدون metadata، چون افست‌ها بهم می‌ریزن)
        chunks, cur = [], ""
        for line in text.split("\n"):
            while len(line) > MAX_MESSAGE_LEN:
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(line[:MAX_MESSAGE_LEN])
                line = line[MAX_MESSAGE_LEN:]
            if len(cur) + len(line) + 1 > MAX_MESSAGE_LEN:
                chunks.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        result = None
        for chunk in chunks:
            result = api_call(token, "sendMessage", {"chat_id": chat_id, "text": chunk})
        return result

    payload = {"chat_id": chat_id, "text": text}
    if metadata:
        try:
            return api_call(token, "sendMessage", {**payload, "metadata": {"meta_data_parts": metadata}})
        except RubikaAPIError as e:
            print(f"DEBUG: sendMessage با metadata رد شد ({e}); بدون metadata دوباره می‌فرستم")
    return api_call(token, "sendMessage", payload)


# ---------------------------------------------------------------------------
# تشخیص نوع فایل (عکس / ویدیو / فایل)
# ---------------------------------------------------------------------------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp", ".m4v", ".ts"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac"}

_RAW_TYPE_MAP = {
    "image": "Image", "photo": "Image", "picture": "Image",
    "video": "Video", "videomessage": "Video",
    "gif": "Gif",
    "music": "Music", "audio": "Music",
    "voice": "Voice",
    "file": "File", "document": "File", "application": "File",
}


def file_extension(file_name):
    """پسوند با نقطه (مثلاً '.apk')؛ اگه نداشت/نامعتبر بود رشتهٔ خالی."""
    name = str(file_name or "").strip()
    if "." not in name:
        return ""
    ext = "." + name.rsplit(".", 1)[-1]
    if len(ext) > 10 or " " in ext:
        return ""
    return ext.lower()


def kind_from_name(file_name):
    ext = file_extension(file_name)
    if ext in IMAGE_EXTS:
        return "Image"
    if ext in VIDEO_EXTS:
        return "Video"
    if ext in AUDIO_EXTS:
        return "Music"
    return None


def detect_file_kind(file_info):
    """نوع فایل رو برمی‌گردونه: Image / Video / Gif / Music / Voice / File
    یا None اگه نتونست تشخیص بده. اول file_type صریح، بعد پسوند نام فایل."""
    if not isinstance(file_info, dict):
        return None
    raw = str(file_info.get("file_type") or file_info.get("type") or "").strip().lower()
    if raw in _RAW_TYPE_MAP:
        return _RAW_TYPE_MAP[raw]
    name = (file_info.get("file_name") or file_info.get("name")
            or file_info.get("original_name") or file_info.get("title"))
    return kind_from_name(name)


def normalize_send_file_type(file_type, file_name=None):
    """نوع معتبر برای requestSendFile: File / Image / Video / Voice / Music / Gif.
    اگه نوع ناشناخته بود، از پسوند نام فایل حدس می‌زنه؛ در نهایت File."""
    raw = str(file_type or "").strip().lower()
    if raw in _RAW_TYPE_MAP:
        return _RAW_TYPE_MAP[raw]
    return kind_from_name(file_name) or "File"


def safe_file_name(file_name, fallback="file"):
    name = str(file_name or "").strip()
    if not name:
        return fallback
    return name.replace("/", "_").replace("\\", "_")[:180]


# ---------------------------------------------------------------------------
# ارسال فایل
# ---------------------------------------------------------------------------
def _request_with_retry(method, url, retries=3, backoff_seconds=3, **kwargs):
    """درخواست خام (دانلود/آپلود) با تلاش مجدد روی قطعی شبکه."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            print(f"DEBUG: درخواست {method} ناموفق (تلاش {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise last_exc


def reupload_file(token, file_id, file_type, file_name="file"):
    """دانلود فایل با file_id قدیمی و آپلود دوباره برای گرفتن file_id تازه.
    نوع آپلود = نوع واقعی فایل (Image برای عکس)، تا عکس به‌صورت فایل
    و فایل به‌صورت عکس ارسال نشه."""
    file_info = get_file(token, file_id)
    download_url = (file_info or {}).get("download_url")
    if not download_url:
        raise RuntimeError("getFile آدرس دانلود برنگردوند.")
    r = _request_with_retry("GET", download_url, timeout=UPLOAD_TIMEOUT)
    upload_req = request_send_file(token, normalize_send_file_type(file_type, file_name))
    upload_url = (upload_req or {}).get("upload_url")
    if not upload_url:
        raise RuntimeError("requestSendFile آدرس آپلود برنگردوند.")
    up = _request_with_retry("POST", upload_url, timeout=UPLOAD_TIMEOUT, files={"file": (file_name, r.content)})
    try:
        result = up.json()
    except ValueError:
        raise RuntimeError(f"آپلود مجدد جواب JSON نداد: {up.text[:200]}")
    new_file_id = None
    if isinstance(result, dict):
        new_file_id = result.get("file_id") or (result.get("data") or {}).get("file_id")
    if not new_file_id:
        raise RuntimeError(f"آپلود مجدد جواب معتبر نداد: {result}")
    return new_file_id


def send_file(token, chat_id, file_id, text="", file_type=None, file_name="file",
              source_chat_id=None, source_message_id=None, metadata=None):
    """
    ارسال فایل (فقط یک‌بار به مقصد می‌رسه):
      ۱) sendFile مستقیم با file_id (اگه metadata رد شد، بدون metadata)
      ۲) اگه file_id برای این چت معتبر نبود: دانلود + آپلود مجدد + sendFile
      ۳) آخرین راه: forwardMessage از کانال منبع (کپشن اصلی همون‌جا حفظ
         می‌شه، نه کپشن سفارشی ما)
    اگه حین ارسال قطع ارتباط/timeout پیش بیاد (SendUncertainError)، هیچ
    تلاش دیگه‌ای نمی‌کنیم چون ممکنه فایل رسیده باشه.
    """
    send_type = normalize_send_file_type(file_type, file_name)
    safe_name = safe_file_name(file_name, "file")

    def _send(fid, use_metadata):
        payload = {"chat_id": chat_id, "file_id": fid, "text": text or ""}
        if use_metadata and metadata:
            payload["metadata"] = {"meta_data_parts": metadata}
        return api_call(token, "sendFile", payload)

    def _send_with_meta_fallback(fid):
        if metadata:
            try:
                return _send(fid, True)
            except RubikaAPIError as e:
                print(f"DEBUG: sendFile با metadata رد شد ({e}); بدون metadata امتحان می‌کنم")
        return _send(fid, False)

    errors = []

    try:
        result = _send_with_meta_fallback(file_id)
        print(f"DEBUG: sendFile موفق (مستقیم) -> chat_id={chat_id}")
        return result
    except SendUncertainError:
        raise
    except Exception as e:
        errors.append(f"مستقیم: {e}")
        print(f"DEBUG: sendFile مستقیم ناموفق: {e}")

    try:
        new_file_id = reupload_file(token, file_id, send_type, safe_name)
        result = _send_with_meta_fallback(new_file_id)
        print(f"DEBUG: sendFile بعد از آپلود مجدد موفق -> chat_id={chat_id}")
        return result
    except SendUncertainError:
        raise
    except Exception as e:
        errors.append(f"آپلود مجدد: {e}")
        print(f"DEBUG: آپلود مجدد ناموفق: {e}")

    if source_chat_id and source_message_id:
        try:
            result = forward_message(token, source_chat_id, source_message_id, chat_id)
            print(f"DEBUG: forwardMessage موفق -> chat_id={chat_id}")
            return result
        except SendUncertainError:
            raise
        except Exception as e:
            errors.append(f"forward: {e}")
            print(f"DEBUG: forwardMessage ناموفق: {e}")

    raise RuntimeError("ارسال فایل شکست خورد؛ " + " | ".join(errors))


# ---------------------------------------------------------------------------
# شناسایی چت‌ها (کاربر / کانال / گروه) و آپدیت‌ها
# ---------------------------------------------------------------------------
def is_channel_guid(guid):
    """GUID کانال‌های روبیکا با c0 شروع می‌شه (کاربر b0، گروه g0)."""
    return str(guid or "").startswith("c0")


def is_group_guid(guid):
    return str(guid or "").startswith("g0")


def is_user_guid(guid):
    g = str(guid or "")
    return bool(g) and not g.startswith(("c0", "g0"))


def known_channel_guids(config):
    guids = {config.get("source_channel_guid")}
    for ch in config.get("destination_channels", []):
        guids.add(ch.get("guid"))
    for ch in config.get("required_join_channels", []):
        guids.add(ch.get("guid"))
    guids.discard(None)
    guids.discard("")
    return guids


def extract_chat_id(update, msg):
    """chat_id در آپدیت‌های روبیکا کنار خودِ update می‌آد، نه داخل message؛
    قبلاً فقط msg.chat_id خونده می‌شد و برای پیام‌های کانال None می‌شد."""
    chat_id = update.get("chat_id") if isinstance(update, dict) else None
    if not chat_id and isinstance(msg, dict):
        chat_id = msg.get("chat_id")
        if not chat_id and str(msg.get("sender_type", "")).lower() == "user":
            chat_id = msg.get("sender_id")
    return chat_id


def get_update_identity(update, msg=None):
    """شناسهٔ یکتای آپدیت برای جلوگیری از پردازش تکراری. پیام ادیت‌شده
    (همون message_id با متن/فایل جدید) باید دوباره پردازش بشه، برای همین
    خلاصهٔ محتوا هم داخل شناسه هست."""
    if isinstance(update, dict):
        for key in ("update_id", "id"):
            if update.get(key) is not None:
                return f"u:{update[key]}"
    msg = msg if isinstance(msg, dict) else {}
    mid = msg.get("message_id") or msg.get("id")
    if not mid:
        return None
    utype = "e" if (update.get("updated_message") or msg.get("is_edited")) else "n"
    chat = extract_chat_id(update, msg) or ""
    body = f"{msg.get('text', '')}|{(msg.get('file') or {}).get('file_id', '')}"
    digest = hashlib.md5(body.encode("utf-8")).hexdigest()[:8]
    return f"{utype}:{chat}:{mid}:{digest}"


def was_processed(state, identity):
    return bool(identity and identity in state.setdefault("processed_updates", []))


def mark_processed(state, identity):
    if not identity:
        return
    items = state.setdefault("processed_updates", [])
    if identity not in items:
        items.append(identity)
    if len(items) > MAX_PROCESSED_IDS:
        del items[:-MAX_PROCESSED_IDS]


def extract_fallback_offset(updates):
    if not updates:
        return None
    last = updates[-1]
    msg = last.get("new_message") or last.get("updated_message") or {}
    return last.get("update_id") or last.get("id") or msg.get("message_id")


def track_known_user(state, config, chat_id):
    if not is_user_guid(chat_id) or chat_id in known_channel_guids(config):
        return False
    if chat_id == config.get("owner_guid"):
        return False
    users = state.setdefault("known_users", [])
    if chat_id not in users:
        users.append(chat_id)
        return True
    return False


def prune_known_users(state, config):
    """ورودی‌هایی که در واقع کانال/گروه یا مالک‌اند رو از known_users پاک
    می‌کنه (تشخیص با پیشوند GUID، پس کانال‌های ثبت‌نشده در config هم
    کاربر حساب نمی‌شن)."""
    channels = known_channel_guids(config)
    owner = config.get("owner_guid")
    state["known_users"] = [
        u for u in state.get("known_users", [])
        if is_user_guid(u) and u not in channels and u != owner
    ]


# ---------------------------------------------------------------------------
# حافظهٔ موقت فایل‌های اخیر (برای آپدیتِ ادیت پیام که فایل همراهش نیست)
# ---------------------------------------------------------------------------
RECENT_FILES_TTL_MINUTES = 240


def remember_recent_file(state, message_id, file_info):
    if not message_id or not isinstance(file_info, dict):
        return
    cache = state.setdefault("recent_files", {})
    cache[str(message_id)] = {
        "file_id": file_info.get("file_id"),
        "file_name": file_info.get("file_name") or file_info.get("name"),
        "file_type": file_info.get("file_type") or file_info.get("type"),
        "seen": tehran_now().strftime("%Y-%m-%d %H:%M"),
    }
    cutoff = tehran_now().replace(tzinfo=None) - timedelta(minutes=RECENT_FILES_TTL_MINUTES)
    for k in list(cache.keys()):
        try:
            if datetime.strptime(cache[k]["seen"], "%Y-%m-%d %H:%M") < cutoff:
                del cache[k]
        except Exception:
            del cache[k]


def recall_recent_file(state, message_id):
    if not message_id:
        return None
    cached = state.get("recent_files", {}).get(str(message_id))
    if not cached or not cached.get("file_id"):
        return None
    return {k: v for k, v in cached.items() if k != "seen" and v}


# ---------------------------------------------------------------------------
# پارس کپشن — موقعیتی و دقیق
# ---------------------------------------------------------------------------
def _content_lines(caption):
    lines = [l.strip() for l in (caption or "").splitlines() if l.strip()]
    return [l for l in lines if not l.startswith("#")]


_LABEL_RE = re.compile(r"^\s*(عنوان|توضیحات|توضیح|ورژن|نسخه)\s*[:：]\s*")


def _strip_label(line):
    return _LABEL_RE.sub("", line).strip()


_MOD_TAG_RE = re.compile(r"(?<![\w#])#مود(?!\w)")
_VIDEO_TAG_RE = re.compile(r"(?<![\w#])#(?:ویدئو|ویدیو|ویديو|ويدئو|ويديو)(?!\w)")
_EXTENSION_RE = re.compile(r"^\s*(?:پسوند|فرمت|extension|ext)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"#\s?(\d+)(?!\d|\.\d)")


def has_mod_tag(caption):
    return bool(_MOD_TAG_RE.search(_INVISIBLE_RE.sub("", caption or "")))


def has_video_tag(caption):
    return bool(_VIDEO_TAG_RE.search(_INVISIBLE_RE.sub("", caption or "")))


def normalize_extension(value):
    """'mcaddon' یا '.mcaddon' یا 'MCPACK' → '.mcaddon'"""
    value = str(value or "").strip().lstrip(".")
    if not value or " " in value or len(value) > 12:
        return ""
    return "." + value.lower()


def extract_number(caption):
    """شمارهٔ بعد از # (با پشتیبانی از رقم فارسی). صفرهای ابتدایی حذف
    می‌شن تا #01 و #1 یکی حساب بشن."""
    m = _NUMBER_RE.search(normalize_digits(_INVISIBLE_RE.sub("", caption or "")))
    return str(int(m.group(1))) if m else None


def extract_extension_line(caption):
    for line in _content_lines(caption or ""):
        m = _EXTENSION_RE.match(line)
        if m:
            return normalize_extension(m.group(1))
    return ""


def parse_mod_caption(caption):
    """اگه تگ #مود نباشه None؛ وگرنه عنوان/توضیح/ورژن/پسوند/شماره."""
    caption = caption or ""
    if not has_mod_tag(caption):
        return None

    number = extract_number(caption)
    lines = _content_lines(caption)
    if not lines:
        return {"title": "", "description": "", "version": "", "extension": "", "number": number}

    title = _strip_label(lines[0])
    body = lines[1:]
    version_index = None
    version_value = ""
    extension_index = None
    extension_value = ""

    version_re = re.compile(r"^\s*(?:ورژن|نسخه)\s*[:：]?\s*(.+?)\s*$")
    plain_version_re = re.compile(r"^\s*v?\d+(?:\.\d+){1,4}\+?(?:[-+][\w.-]+)?\s*$", re.I)

    for idx, line in enumerate(body):
        ext_m = _EXTENSION_RE.match(line)
        if ext_m:
            extension_index = idx
            extension_value = normalize_extension(ext_m.group(1))
            continue
        if version_index is not None:
            continue
        m = version_re.match(line)
        if m:
            version_index = idx
            version_value = m.group(1).strip()
            continue
        if plain_version_re.match(line):
            version_index = idx
            version_value = line.strip()

    skip = {i for i in (version_index, extension_index) if i is not None}
    description_lines = [l for i, l in enumerate(body) if i not in skip]
    description = "\n".join(_strip_label(x) for x in description_lines if _strip_label(x)).strip()

    return {
        "title": title,
        "description": description,
        "version": version_value,
        "extension": extension_value,
        "number": number,
    }


def parse_video_caption(caption):
    """اگه تگ #ویدئو/#ویدیو نباشه None؛ وگرنه {'title': ...}."""
    caption = caption or ""
    if not has_video_tag(caption):
        return None
    lines = _content_lines(caption)
    title = _strip_label(lines[0]) if lines else ""
    return {"title": title}


# ---------------------------------------------------------------------------
# ارسال مود / ویدیو به یک کانال مقصد
# ---------------------------------------------------------------------------
def file_send_name(number, entry):
    ext = (entry or {}).get("manual_extension") or file_extension((entry or {}).get("file_name"))
    return f"{number}{ext}"


def send_mod_cover(token, channel, mod, state):
    """پست عکس مود با کپشن (عنوان، توضیح، ورژن، متن‌های سفارشی)."""
    direct = channel.get("send_file_directly", False)
    parts = []

    if mod.get("title"):
        parts.append((mod["title"], "bold"))
        parts.append(("\n\n", None))

    if mod.get("description"):
        parts.append((f"⚙️- {mod['description']}", "quote"))
        parts.append(("\n", None))

    if not direct and mod.get("number"):
        bot_username = channel.get("bot_username") or "@TLP_AdminBot"
        parts.append((
            f"برای دریافت فایل، عدد {mod['number']} یا #{mod['number']} رو برای ربات ({bot_username}) بفرستید.",
            "quote",
        ))

    if mod.get("version"):
        parts.append(("\n\n", None))
        parts.append((f"💾- ورژن: {mod['version']}", None))

    if channel.get("channel_link"):
        parts.append(("\n\n", None))
        parts.append((f" {channel['channel_link']}", None))

    if channel.get("mod_photo_extra_text"):
        parts.append(("\n\n", None))
        parts.extend(parse_custom_markup(channel["mod_photo_extra_text"]))

    text, metadata = build_text_with_metadata(parts)

    # کاور همیشه «تصویر» است، مستقل از اینکه file_type ذخیره شده یا نه.
    cover_name = safe_file_name(mod.get("photo_file_name"), "cover.jpg")
    if kind_from_name(cover_name) != "Image":
        cover_name = "cover.jpg"

    return send_file(
        token, channel["guid"], mod["photo_file_id"], text, metadata=metadata,
        file_type="Image", file_name=cover_name,
        source_chat_id=mod.get("source_channel_guid"), source_message_id=mod.get("photo_message_id"),
    )


def send_mod_file(token, channel, mod, state):
    """پست فایل مود (فقط وقتی send_file_directly روشنه) با کپشن سفارشی."""
    number = mod.get("number")
    entry = state.get("files_by_number", {}).get(str(number)) if number else None
    if not entry or not entry.get("file_id"):
        raise RuntimeError(f"فایل مود شمارهٔ {number} در آرشیو پیدا نشد.")
    caption_text, caption_meta = build_text_with_metadata(
        parse_custom_markup(channel.get("mod_file_caption", ""))
    )
    return send_file(
        token, channel["guid"], entry["file_id"], caption_text, metadata=caption_meta,
        file_type=entry.get("file_type") or "File",
        file_name=file_send_name(number, entry),
        source_chat_id=entry.get("source_channel_guid"), source_message_id=entry.get("message_id"),
    )


def send_video(token, channel, video):
    """پست ویدیو در کانال. True اگه ارسال شد، False اگه برای این کانال خاموشه."""
    if not channel.get("videos_enabled", True):
        print(f"DEBUG: ارسال ویدیو برای {channel_label(channel)} خاموشه (videos_enabled=false)")
        return False

    parts = []
    if video.get("title"):
        parts.append((video["title"], None))

    if channel.get("channel_link"):
        if parts:
            parts.append(("\n\n", None))
        parts.append((channel["channel_link"], "quote"))

    if channel.get("video_extra_text"):
        if parts:
            parts.append(("\n\n", None))
        parts.extend(parse_custom_markup(channel["video_extra_text"]))

    text, metadata = build_text_with_metadata(parts)
    send_file(
        token, channel["guid"], video["video_file_id"], text, metadata=metadata,
        file_type="Video", file_name=safe_file_name(video.get("file_name"), "video.mp4"),
        source_chat_id=video.get("source_channel_guid"), source_message_id=video.get("message_id"),
    )
    return True


def pick_item(items, used_ids):
    if not items:
        return None, used_ids
    available = [i for i in items if i["id"] not in used_ids]
    if not available:
        used_ids = []
        available = items
    chosen = random.choice(available)
    return chosen, (used_ids + [chosen["id"]])[-MAX_USED_IDS:]


def usable_mods(state):
    """فقط مودهایی که هم عکس کاور و هم فایل دارن؛ قبلاً مود بدون فایل هم
    انتخاب می‌شد و فقط عکس پست می‌شد."""
    files = state.get("files_by_number", {})
    return [
        m for m in state.get("mods", [])
        if m.get("photo_file_id") and m.get("number")
        and (files.get(str(m["number"])) or {}).get("file_id")
    ]


def _run_stage(state, prog, stage, fn, label):
    """یک مرحلهٔ ارسال (cover/file) رو یک‌بار اجرا می‌کنه و در prog ثبت
    می‌کنه. اگه وضعیتش نامطمئن بود (timeout)، فرض می‌کنیم رسیده و
    دوباره نمی‌فرستیم."""
    if prog.get(stage):
        return
    try:
        fn()
    except SendUncertainError as e:
        log_error(state, f"ارسال نامطمئن ({label})", e)
    prog[stage] = True


def post_next_mod(token, channel, state):
    """مود بعدیِ استفاده‌نشده رو (کاور + در صورت نیاز فایل) در کانال پست
    می‌کنه. پیشرفت هر کانال ذخیره می‌شه تا اگه مرحلهٔ فایل شکست خورد،
    دفعهٔ بعد کاور دوباره ارسال نشه. None اگه مودی برای ارسال نبود."""
    guid = channel["guid"]
    label = channel_label(channel)
    progress_all = state.setdefault("mod_progress", {})
    mods = usable_mods(state)
    used = state.setdefault("used_mods_per_channel", {}).setdefault(guid, [])

    prog = progress_all.get(guid)
    item = None
    if prog:
        item = next((m for m in mods if m["id"] == prog.get("mod_id")), None)
        if item is None:
            progress_all.pop(guid, None)
            prog = None

    if item is None:
        item, candidate_used = pick_item(mods, used)
        if item is None:
            return None
        prog = {"mod_id": item["id"], "cover": False, "file": False,
                "attempts": 0, "candidate_used": candidate_used}
        progress_all[guid] = prog

    prog["attempts"] = prog.get("attempts", 0) + 1
    candidate_used = prog.get("candidate_used") or (used + [item["id"]])
    direct = channel.get("send_file_directly", False)

    try:
        _run_stage(state, prog, "cover", lambda: send_mod_cover(token, channel, item, state), f"کاور {label}")
        if direct:
            _run_stage(state, prog, "file", lambda: send_mod_file(token, channel, item, state), f"فایل {label}")
    except Exception:
        if prog["attempts"] >= MAX_POST_ATTEMPTS:
            # بعد از چند بار شکست، این مود رو رد می‌کنیم تا کانال گیر نکنه
            state["used_mods_per_channel"][guid] = candidate_used
            progress_all.pop(guid, None)
            log_error(state, f"رد شدن مود برای {label}", f"مود «{item.get('title')}» بعد از {prog['attempts']} تلاش رد شد.")
        raise

    state["used_mods_per_channel"][guid] = candidate_used
    progress_all.pop(guid, None)
    return item


def post_next_video(token, channel, state):
    """ویدیوی بعدی رو پست می‌کنه. None اگه ویدیویی نبود یا خاموشه."""
    if not channel.get("videos_enabled", True):
        return None
    guid = channel["guid"]
    used = state.setdefault("used_videos_per_channel", {}).setdefault(guid, [])
    video, candidate_used = pick_item(state.get("videos", []), used)
    if video is None:
        return None
    try:
        send_video(token, channel, video)
    except SendUncertainError as e:
        log_error(state, f"ارسال نامطمئن (ویدیو {channel_label(channel)})", e)
    state["used_videos_per_channel"][guid] = candidate_used
    return video


# ---------------------------------------------------------------------------
# جلوگیری از سیل پیام‌های دوره‌ای (دفترچهٔ ارسال سبک)
# ---------------------------------------------------------------------------
def prune_sent_log(state, max_age_hours=1):
    log = state.get("sent_log", {})
    if not log:
        return
    cutoff = tehran_now().replace(tzinfo=None) - timedelta(hours=max_age_hours)
    for k in list(log.keys()):
        try:
            if datetime.strptime(log[k], "%Y-%m-%d %H:%M") < cutoff:
                del log[k]
        except Exception:
            del log[k]


def should_send_now(state, key, min_interval_minutes):
    """True اگه برای این key در min_interval_minutes اخیر چیزی ثبت نشده
    (و زمان الان رو ثبت می‌کنه)."""
    log = state.setdefault("sent_log", {})
    now = tehran_now().replace(tzinfo=None)
    last = log.get(key)
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
            if (now - last_dt) < timedelta(minutes=min_interval_minutes):
                return False
        except Exception:
            pass
    log[key] = now.strftime("%Y-%m-%d %H:%M")
    prune_sent_log(state)
    return True


# ---------------------------------------------------------------------------
# پخش پیام
# ---------------------------------------------------------------------------
def broadcast_to_channels(token, config, text):
    sent, failed = 0, 0
    for ch in config.get("destination_channels", []):
        if not ch.get("enabled", True):
            continue
        try:
            send_message(token, ch["guid"], text)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"DEBUG: channelcast failed for {channel_label(ch)}: {e}")
    return sent, failed


def broadcast_to_users(token, state, text):
    sent, failed = 0, 0
    for uid in list(state.get("known_users", [])):
        if not is_user_guid(uid):
            continue
        try:
            send_message(token, uid, text)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"DEBUG: broadcast failed for {uid}: {e}")
        time.sleep(0.2)
    return sent, failed


# ---------------------------------------------------------------------------
# مسدودسازی کاربران
# ---------------------------------------------------------------------------
def is_blocked(state, chat_id):
    blocked = state.get("blocked_users", {})
    info = blocked.get(chat_id)
    if not info:
        return None
    until = info.get("until")
    if until:
        try:
            until_dt = datetime.strptime(until, "%Y-%m-%d %H:%M")
            if tehran_now().replace(tzinfo=None) >= until_dt:
                del blocked[chat_id]
                return None
        except Exception:
            pass
    return info


def block_user(state, chat_id, reason, days=None):
    blocked_at = tehran_now().strftime("%Y-%m-%d %H:%M")
    until = None
    if days:
        until = (tehran_now().replace(tzinfo=None) + timedelta(days=float(days))).strftime("%Y-%m-%d %H:%M")
    state.setdefault("blocked_users", {})[chat_id] = {
        "reason": reason or "بدون دلیل ذکرشده",
        "blocked_at": blocked_at,
        "until": until,
    }
    return state["blocked_users"][chat_id]


def unblock_user(state, chat_id):
    return state.get("blocked_users", {}).pop(chat_id, None) is not None


def build_blocked_list(state):
    blocked = state.get("blocked_users", {})
    if not blocked:
        return "🚫 هیچ کاربری مسدود نیست."
    lines = [f"🚫 کاربران مسدود ({len(blocked)}):", ""]
    for chat_id, info in blocked.items():
        until = info.get("until") or "دائمی"
        lines.append(f"• {chat_id}\n  دلیل: {info.get('reason')}\n  از: {info.get('blocked_at')} — تا: {until}")
    return "\n".join(lines)


def build_blocked_message(info):
    until = info.get("until") or "دائمی (تا اطلاع ثانوی)"
    return (
        f"⛔ شما توسط مالک ربات مسدود شده‌اید.\n"
        f"دلیل: {info.get('reason')}\n"
        f"تاریخ مسدودیت: {info.get('blocked_at')}\n"
        f"پایان مسدودیت: {until}"
    )


# ---------------------------------------------------------------------------
# پیام به مالک، خطاها و باگ‌ها
# ---------------------------------------------------------------------------
def notify_owner(token, config, text):
    owner = config.get("owner_guid")
    if not owner or owner.startswith("PUT_YOUR"):
        print("DEBUG: notify_owner skipped — owner_guid تنظیم نشده")
        return
    try:
        send_message(token, owner, text)
        print(f"DEBUG: notify_owner ارسال شد به {owner}")
    except Exception as e:
        print(f"DEBUG: notify_owner failed: {e}")


def log_error(state, category, message):
    err = {
        "id": str(uuid.uuid4())[:6],
        "time": tehran_now().strftime("%Y-%m-%d %H:%M"),
        "category": category,
        "message": str(message)[:300],
        "notified": False,
    }
    state.setdefault("errors", []).append(err)
    state["errors"] = state["errors"][-MAX_ERRORS_STORED:]
    print(f"DEBUG ERROR [{category}]: {message}")
    return err


def maybe_notify_new_errors(token, config, state):
    unnotified = [e for e in state.get("errors", []) if not e.get("notified")]
    if not unnotified:
        return
    last_time = state.get("last_error_notify_time")
    now = tehran_now()
    if last_time:
        try:
            last_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M")
            if (now.replace(tzinfo=None) - last_dt) < timedelta(minutes=ERROR_NOTIFY_COOLDOWN_MINUTES):
                return
        except Exception:
            pass
    notify_owner(token, config, f"⚠️ {len(unnotified)} خطای جدید ثبت شد.\nبرای دیدن جزئیات: /bugs")
    for e in unnotified:
        e["notified"] = True
    state["last_error_notify_time"] = now.strftime("%Y-%m-%d %H:%M")


def build_bugs_page(state):
    errors = list(reversed(state.get("errors", [])))
    if not errors:
        return "🎉 هیچ باگی ثبت نشده."
    offset = state.get("bug_page_offset", 0)
    if offset >= len(errors):
        offset = 0
    page = errors[offset: offset + ERRORS_PER_PAGE]
    next_offset = offset + ERRORS_PER_PAGE
    state["bug_page_offset"] = next_offset if next_offset < len(errors) else 0

    by_category = {}
    for e in page:
        by_category.setdefault(e["category"], []).append(e)

    lines = [f"🐞 گزارش باگ‌ها ({offset + 1}-{offset + len(page)} از {len(errors)})"]
    for cat, items in by_category.items():
        lines.append(f"\n📌 دسته: {cat}")
        for e in items:
            lines.append(f"• [{e['time']}] {e['message']}")

    if state["bug_page_offset"] == 0:
        lines.append("\n(به انتهای لیست رسیدید؛ دوباره /bugs بفرستید تا از اول شروع بشه)")
    else:
        lines.append("\nبرای دیدن بعدی، دوباره بنویسید: /bugs")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# پنل مدیریت
# ---------------------------------------------------------------------------
def build_panel_list(config):
    channels = config.get("destination_channels", [])
    if not channels:
        return "هیچ کانال مقصدی در config.json ثبت نشده."
    lines = ["🎛 پنل مدیریت کانال‌ها", ""]
    for i, ch in enumerate(channels, start=1):
        status = "✅ فعال" if ch.get("enabled", True) else "⛔ غیرفعال"
        lines.append(f"{i}. {channel_label(ch)} — {status}")
    lines.append("\nبرای دیدن جزئیات، عدد همون کانال رو بفرستید.")
    return "\n".join(lines)


def build_channel_detail(config, state, index):
    channels = config.get("destination_channels", [])
    if index < 1 or index > len(channels):
        return "همچین شماره‌ای در لیست نیست."
    ch = channels[index - 1]
    guid = ch["guid"]
    posts_count = len(state.get("used_mods_per_channel", {}).get(guid, []))
    activated = state.get("channel_activated", {}).get(guid, "هنوز فعالیتی ثبت نشده")
    status = "✅ فعال" if ch.get("enabled", True) else "⛔ غیرفعال"
    delivery = "مستقیم در کانال" if ch.get("send_file_directly") else "از طریق ربات (پیوی)"
    return (
        f"📊 {channel_label(ch)}\n"
        f"GUID: {guid}\n"
        f"وضعیت: {status}\n"
        f"روش تحویل فایل: {delivery}\n"
        f"تعداد پست‌های ارسالی: {posts_count}\n"
        f"فعال از: {activated}"
    )


def trim_stored_content(state):
    """جلوگیری از سنگین‌شدن state.json."""
    for key, limit in (("mods", MAX_STORED_MODS), ("videos", MAX_STORED_VIDEOS)):
        items = state.get(key, [])
        if len(items) > limit:
            state[key] = items[-limit:]
    for key in ("used_mods_per_channel", "used_videos_per_channel"):
        for guid, ids in list(state.get(key, {}).items()):
            if len(ids) > MAX_USED_IDS:
                state[key][guid] = ids[-MAX_USED_IDS:]


def track_channel_activation(state, config):
    activated = state.setdefault("channel_activated", {})
    for ch in config.get("destination_channels", []):
        guid = ch["guid"]
        if guid not in activated:
            activated[guid] = tehran_now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# متن عضویت اجباری (فقط اطلاع‌رسانی؛ /file عضویت رو چک نمی‌کنه)
# ---------------------------------------------------------------------------
def build_join_prompt(channels):
    lines = ["📣 برای استفاده از ربات باید در کانال‌های زیر عضو شوید:", ""]
    if channels:
        for ch in channels:
            lines.append(str(ch.get("link") or ch.get("guid") or "").strip())
    else:
        lines.append("هیچ کانالی تنظیم نشده است.")
    lines.extend([
        "",
        "✨ پس از عضویت در کانال‌های بالا، برای دریافت فایل مود /file را ارسال کنید.",
        "⚠️ اگر قبلاً عضو هستید، فقط /file را بزنید.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# تشخیص اسپم/فعالیت بیش‌ازحد یک کاربر
# ---------------------------------------------------------------------------
SPAM_WINDOW_MINUTES = 5
SPAM_THRESHOLD = 15
SPAM_REPORT_COOLDOWN_MINUTES = 30


def check_spam(state, chat_id):
    now = tehran_now().replace(tzinfo=None)
    log = state.setdefault("activity_log", {})
    entries = log.get(chat_id, [])
    entries.append(now.strftime("%Y-%m-%d %H:%M:%S"))
    cutoff = now - timedelta(minutes=SPAM_WINDOW_MINUTES)
    fresh = []
    for t in entries:
        try:
            if datetime.strptime(t, "%Y-%m-%d %H:%M:%S") >= cutoff:
                fresh.append(t)
        except Exception:
            pass
    log[chat_id] = fresh
    if len(log) > 1000:
        state["activity_log"] = {k: v for k, v in log.items() if v}
    return len(fresh) >= SPAM_THRESHOLD


# ---------------------------------------------------------------------------
# پلن‌های زمان‌بندی پست‌گذاری
# ---------------------------------------------------------------------------
DEFAULT_PLANS = {
    "0": {"mod_interval_hours": 2, "video_interval_hours": 4.5},
    "1": {"mod_interval_hours": 1, "video_interval_hours": 3},
    "2": {"mod_interval_hours": 0.5, "video_interval_hours": 2},
}


def resolve_channel_plan(config, channel):
    plans = config.get("schedule", {}).get("plans", DEFAULT_PLANS)
    plan_key = str(channel.get("plan", config.get("schedule", {}).get("default_plan", 1)))
    plan = plans.get(plan_key, DEFAULT_PLANS["1"])
    mod_h = channel.get("mod_interval_hours", plan.get("mod_interval_hours", 1))
    video_h = channel.get("video_interval_hours", plan.get("video_interval_hours", 3))
    return float(mod_h), float(video_h)
