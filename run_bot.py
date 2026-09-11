"""
run_bot.py
-----------
تنها اسکریپتی که GitHub Actions هر ~۱۵ دقیقه اجرا می‌کند. کارهایی که انجام
می‌دهد:

  1. یک‌بار get_updates می‌زند و آپدیت‌های جدید را می‌گیرد.
  2. اگر پیام از کانال منبع بود -> مود/ویدیوی جدید را ذخیره می‌کند.
  3. اگر پیام از پیوی خودِ شما (owner_guid) بود:
       - اگر دستور /bugs یا دکمه «بعدی» بود -> ۵ باگ بعدی را دسته‌بندی‌شده نشان می‌دهد.
       - هر چیز دیگری -> یک گزارش وضعیت کلی برمی‌گرداند.
  4. اگر به ساعتِ پست‌گذاریِ برنامه‌ریزی‌شده رسیده باشیم (و این ساعت قبلاً
     پست نشده) -> برای هر کانال مقصد مود/ویدیو می‌گذارد، و یک پیام خلاصه
     وضعیت به پیوی شما می‌فرستد.
  5. اگر هر جای کد خطا بدهد، به‌جای کرش خام، خطا ثبت می‌شود و فقط یک پیام
     خلاصه (نه سیل پیام) با فاصله حداقل ۱۰ دقیقه به پیوی شما می‌رود.
"""

import os
import sys

from rubka import Robot
from rubka.button import InlineBuilder

import bot_core as core


def handle_source_channel_message(state, msg):
    file_info = msg.get("file")
    caption = msg.get("text") or ""

    if file_info and file_info.get("file_type") == "Image":
        parsed = core.parse_caption(caption)
        state["pending_photo"] = {"file_id": file_info.get("file_id"), **parsed}

    elif file_info and file_info.get("file_type") == "Video":
        parsed = core.parse_caption(caption)
        state["videos"].append({
            "id": __import__("uuid").uuid4().hex[:8],
            "video_file_id": file_info.get("file_id"),
            "title": parsed["title"] or caption.strip() or "ویدیو جدید",
        })
        state["pending_photo"] = None

    elif file_info and file_info.get("file_type") in ("File", "Music", "Voice", "Gif"):
        pending = state.get("pending_photo")
        if pending:
            state["mods"].append({
                "id": __import__("uuid").uuid4().hex[:8],
                "photo_file_id": pending["file_id"],
                "file_file_id": file_info.get("file_id"),
                "title": pending["title"],
                "hashtags": pending["hashtags"],
                "description": pending["description"],
                "version": pending["version"],
            })
            state["pending_photo"] = None


def bugs_keyboard(has_more, next_offset):
    if not has_more:
        return None
    builder = InlineBuilder()
    return builder.row(
        builder.button_simple(f"bugs_next_{next_offset}", "پیام بعدی ▶️")
    ).build()


def handle_owner_message(bot, config, state, msg):
    text = (msg.get("text") or "").strip()
    button_id = (msg.get("aux_data") or {}).get("button_id", "")

    if button_id.startswith("bugs_next_"):
        offset = int(button_id.replace("bugs_next_", "") or 0)
        page_text, has_more = core.build_bugs_page(state, offset)
        bot.send_message(
            chat_id=config["owner_guid"], text=page_text,
            inline_keypad=bugs_keyboard(has_more, offset + core.ERRORS_PER_PAGE),
        )
        return

    if text in ("/bugs", "باگ", "باگ‌ها", "bugs"):
        page_text, has_more = core.build_bugs_page(state, 0)
        bot.send_message(
            chat_id=config["owner_guid"], text=page_text,
            inline_keypad=bugs_keyboard(has_more, core.ERRORS_PER_PAGE),
        )
        return

    n_mods = len(state.get("mods", []))
    n_videos = len(state.get("videos", []))
    n_errors = len(state.get("errors", []))
    now = core.tehran_now().strftime("%Y-%m-%d %H:%M")
    status = (
        f"✅ ربات فعال است.\n"
        f"🕰 ساعت تهران: {now}\n"
        f"🎮 مودهای ذخیره‌شده: {n_mods}\n"
        f"🎬 ویدیوهای ذخیره‌شده: {n_videos}\n"
        f"🐞 تعداد کل باگ‌های ثبت‌شده: {n_errors}\n"
        f"برای دیدن گزارش باگ‌ها: /bugs"
    )
    bot.send_message(chat_id=config["owner_guid"], text=status)


def run_posting_schedule(bot, config, state):
    now = core.tehran_now()
    today = now.strftime("%Y-%m-%d")
    hour = now.hour

    posted = state.setdefault("posted_hours_today", {"date": today, "hours": []})
    if posted.get("date") != today:
        posted["date"] = today
        posted["hours"] = []

    start, end = config["schedule"]["start_hour_tehran"], config["schedule"]["end_hour_tehran"]
    if not (start <= hour <= end) or hour in posted["hours"]:
        return

    video_slot = core.is_video_slot(hour, config)
    posted_summary = []

    for channel in config["destination_channels"]:
        guid = channel["guid"]
        try:
            if video_slot:
                used = state["used_videos_per_channel"].setdefault(guid, [])
                item, used = core.pick_item(state["videos"], used)
                state["used_videos_per_channel"][guid] = used
                if item is None:
                    continue
                core.send_video(bot, channel, item)
                posted_summary.append(f"🎬 {channel['name']}: ویدیو «{item['title']}»")
            else:
                used = state["used_mods_per_channel"].setdefault(guid, [])
                item, used = core.pick_item(state["mods"], used)
                state["used_mods_per_channel"][guid] = used
                if item is None:
                    continue
                core.send_mod(bot, channel, item)
                posted_summary.append(f"🎮 {channel['name']}: مود «{item.get('title')}»")
        except Exception as e:
            core.log_error(state, f"ارسال به {channel['name']}", e)

    posted["hours"].append(hour)

    if posted_summary:
        core.notify_owner(bot, config, f"📤 گزارش پست ساعت {hour}:00\n" + "\n".join(posted_summary))
    else:
        core.notify_owner(bot, config, f"ℹ️ ساعت {hour}:00 چیزی برای پست‌کردن (مود/ویدیوی تکراری‌نشده) موجود نبود.")


def main():
    config = core.load_config()
    state = core.load_state()
    token = os.environ.get("RUBIKA_BOT_TOKEN") or config.get("bot_token")
    bot = Robot(token=token)

    try:
        resp = bot.get_updates(offset_id=state.get("last_offset_id"), limit=50)
        updates = resp.get("data", {}).get("updates", []) if isinstance(resp, dict) else []
    except Exception as e:
        core.log_error(state, "دریافت آپدیت‌ها", e)
        updates = []

    new_offset = state.get("last_offset_id")
    for update in updates:
        new_offset = update.get("offset_id", new_offset)
        msg = update.get("new_message") or update.get("updated_message")
        if not msg:
            continue
        chat_id = msg.get("chat_id") or update.get("chat_id")

        try:
            if chat_id == config.get("source_channel_guid"):
                handle_source_channel_message(state, msg)
            elif chat_id == config.get("owner_guid"):
                handle_owner_message(bot, config, state, msg)
        except Exception as e:
            core.log_error(state, "پردازش پیام", e)

    state["last_offset_id"] = new_offset

    try:
        run_posting_schedule(bot, config, state)
    except Exception as e:
        core.log_error(state, "زمان‌بند پست‌گذاری", e)

    try:
        core.maybe_notify_new_errors(bot, config, state)
    except Exception:
        pass

    core.save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as fatal:
        print(f"خطای کلی و غیرمنتظره: {fatal}", file=sys.stderr)
        raise
