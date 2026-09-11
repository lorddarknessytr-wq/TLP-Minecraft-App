"""
bot_core.py
------------
توابع مشترکی که run_bot.py و get_my_guid.py استفاده می‌کنند:
- خواندن/نوشتن فایل‌های JSON
- محاسبه ساعت تهران
- استخراج هشتگ/توضیحات/ورژن از کپشن
- ساخت و ارسال پیام‌های مود/ویدیو
- ارسال گزارش وضعیت و باگ به پیوی مالک ربات (با جلوگیری از سیل پیام)
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

MAX_ERRORS_STORED = 200
ERRORS_PER_PAGE = 5
ERROR_NOTIFY_COOLDOWN_MINUTES = 10


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    return load_json(CONFIG_PATH)


def load_state():
    return load_json(STATE_PATH)


def save_state(state):
    save_json(STATE_PATH, state)


def tehran_now():
    return datetime.now(timezone.utc) + TEHRAN_OFFSET


# ---------------------------------------------------------------------------
# پارس کپشن پست‌های کانال منبع
# ---------------------------------------------------------------------------
def parse_caption(caption: str):
    caption = caption or ""
    hashtags = re.findall(r"#\S+", caption)

    desc_match = re.search(r"(?:توضیحات|توضیح)\s*[:：]\s*(.+)", caption)
    description = desc_match.group(1).strip() if desc_match else ""

    version_match = re.search(r"(?:ورژن|نسخه)\s*[:：]\s*(\S+)", caption)
    version = version_match.group(1).strip() if version_match else ""

    title = ""
    for line in caption.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"(?:توضیحات|توضیح|ورژن|نسخه)\s*[:：]", line):
            continue
        title = line
        break

    return {"title": title, "hashtags": hashtags, "description": description, "version": version}


# ---------------------------------------------------------------------------
# ارسال مود / ویدیو به یک کانال مقصد
# ---------------------------------------------------------------------------
def send_mod(bot, channel, mod):
    lines = []
    if mod.get("title"):
        lines.append(mod["title"])
    if mod.get("hashtags"):
        lines.append(" ".join(mod["hashtags"]))
    if mod.get("description"):
        lines.append(f"📝 توضیحات: {mod['description']}")
    if mod.get("version"):
        lines.append(f"🔢 ورژن: {mod['version']}")
    lines.append(f"🔗 کانال: {channel['channel_link']}")
    if channel.get("mod_photo_extra_text"):
        lines.append(channel["mod_photo_extra_text"])

    bot.send_image(chat_id=channel["guid"], file_id=mod["photo_file_id"], text="\n".join(lines))
    bot.send_document(chat_id=channel["guid"], file_id=mod["file_file_id"], text=channel.get("mod_file_caption", ""))


def send_video(bot, channel, video):
    lines = [video.get("title", "ویدیو جدید")]
    if channel.get("video_extra_text"):
        lines.append(channel["video_extra_text"])
    caption = "\n".join(lines)

    send_video_fn = getattr(bot, "send_video", None)
    if callable(send_video_fn):
        send_video_fn(chat_id=channel["guid"], file_id=video["video_file_id"], text=caption)
    else:
        bot.send_document(chat_id=channel["guid"], file_id=video["video_file_id"], text=caption)


def is_video_slot(hour: int, config: dict) -> bool:
    start = config["schedule"]["start_hour_tehran"]
    every_n = config["schedule"]["video_every_n_slots"]
    return (hour - start) % every_n == 0


def pick_item(items, used_ids):
    if not items:
        return None, used_ids
    available = [i for i in items if i["id"] not in used_ids]
    if not available:
        used_ids = []
        available = items
    chosen = __import__("random").choice(available)
    return chosen, used_ids + [chosen["id"]]


# ---------------------------------------------------------------------------
# پیام به مالک ربات (وضعیت / باگ) — با جلوگیری از سیل پیام
# ---------------------------------------------------------------------------
def notify_owner(bot, config, text):
    owner = config.get("owner_guid")
    if not owner or owner.startswith("c0xYOUR"):
        return  # owner_guid هنوز تنظیم نشده
    try:
        bot.send_message(chat_id=owner, text=text)
    except Exception:
        pass  # اگر خودِ ارسال گزارش هم خطا داد، دیگر چیزی برای گزارش‌کردن نداریم


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
    return err


def maybe_notify_new_errors(bot, config, state):
    """فقط یک پیام خلاصه می‌فرستد (نه یکی‌یکی)، و حداکثر هر N دقیقه یک‌بار."""
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

    notify_owner(
        bot, config,
        f"⚠️ {len(unnotified)} خطای جدید ثبت شد.\n"
        f"برای دیدن جزئیات (دسته‌بندی‌شده، ۵ تا ۵ تا)، به من پیام بده: /bugs"
    )
    for e in unnotified:
        e["notified"] = True
    state["last_error_notify_time"] = now.strftime("%Y-%m-%d %H:%M")


def build_bugs_page(state, offset=0):
    """۵ خطای بعدی را دسته‌بندی‌شده متن‌بندی می‌کند و اینکه آیا صفحه بعدی هست یا نه."""
    errors = list(reversed(state.get("errors", [])))  # جدیدترین اول
    page = errors[offset: offset + ERRORS_PER_PAGE]
    has_more = len(errors) > offset + ERRORS_PER_PAGE

    if not page:
        return "🎉 هیچ باگی ثبت نشده.", False

    by_category = {}
    for e in page:
        by_category.setdefault(e["category"], []).append(e)

    lines = [f"🐞 گزارش باگ‌ها ({offset + 1}-{offset + len(page)} از {len(errors)})"]
    for cat, items in by_category.items():
        lines.append(f"\n📌 دسته: {cat}")
        for e in items:
            lines.append(f"• [{e['time']}] {e['message']}")

    return "\n".join(lines), has_more
