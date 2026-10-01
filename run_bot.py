"""
run_bot.py
-----------
تنها فایل منطق ربات (به‌جای bot_core.py که فقط هسته/API است). همه‌چیز
اینجاست: خوندن کانال منبع، تحویل فایل به کاربر، دستورهای مالک/پنل،
پخش همگانی، پست فوری، و زمان‌بند پست‌گذاری خودکار.

قبلاً این منطق بین دو فایل mod_archiver.py و channel_admin.py پخش شده
بود؛ چون هر دو فقط bot_core و همدیگه رو import می‌کردن و منطق مستقلی
نداشتن که واقعاً جدا نگه داشتنش سودی داشته باشه، اینجا با هم یکی شدن تا
نگه‌داری ساده‌تر بشه و importهای متقابل (channel_admin <-> mod_archiver)
از بین بره.

هر بار اجرا می‌شود (توسط زنگ‌زن بیرونی cron-job.org، یا دستی از Actions):
  1. اگر ورودی force_post_channel داده شده باشد -> فوراً یک مود پست می‌کند.
  2. getUpdates می‌زند، کاربرهای جدید را ثبت می‌کند.
  3. دستورها: /myidver001، #عدد یا /عدد (ثبت درخواست فایل)، /file (تحویل)،
     /start، /help، /ticket، و برای مالک: پنل، رمز پخش همگانی، /bugs و غیره.
  4. کانال منبع: عکس مود باید تگ #مود داشته باشد؛ ویدیو باید تگ #ویدئو داشته باشد.
  5. زمان‌بندی پست‌گذاری طبق schedule/plan هر کانال در config.json.
  6. پیام‌های دوره‌ای (راه‌اندازی/گزارش) با فاصلهٔ حداقلی، تا سیل نشوند.
"""

import os
import sys

import bot_core as core


INFO_REPLY_COOLDOWN_MINUTES = 3
STATUS_REPLY_COOLDOWN_MINUTES = 3
NUMBER_REQUEST_COOLDOWN_MINUTES = 10


# ===========================================================================
# بخش «آرشیو»: خوندن کانال منبع، تحویل فایل به کاربر
# ===========================================================================
def handle_source_channel_message(state, msg, chat_id):
    """یک پیام از کانال منبع (یا فوروارد/آپلود مستقیم مالک) را پردازش
    می‌کند. سه حالت ممکن:
      - عکس با تگ #مود  -> کاورِ در انتظارِ یک مود جدید
      - ویدیو/فایل با تگ #ویدئو -> مستقیماً به آرشیو ویدیو اضافه می‌شود
      - هر فایل دیگر -> فایل واقعیِ مودی که شماره‌اش را دارد (یا شمارهٔ
        عکسِ در انتظار را قرض می‌گیرد)
    اگر فایل قبل از عکسش برسد (ترتیب معکوس در فوروارد/آپلود دستی)، به
    جای گم‌شدن در state["orphan_files"] نگه داشته می‌شود تا وقتی عکس با
    همان شماره رسید، جفت شوند.
    """
    file_info = msg.get("file")
    caption = msg.get("text") or ""
    message_id = msg.get("message_id")

    if file_info and file_info.get("file_id") and message_id:
        core.remember_recent_file(state, message_id, file_info)
    if not file_info and message_id:
        cached = core.recall_recent_file(state, message_id)
        if cached:
            file_info = cached
            print(f"DEBUG: فایل از حافظهٔ موقت بازیابی شد (پیام ادیت‌شده) -> {cached.get('file_type')}")

    if not file_info or not file_info.get("file_id"):
        return

    kind = core.detect_file_kind(file_info)
    print(f"DEBUG: پیام کانال منبع -> kind={kind!r} caption={caption[:60]!r} | file_info: {file_info}")

    mod_parsed = core.parse_mod_caption(caption)
    video_parsed = core.parse_video_caption(caption)

    if mod_parsed is not None:
        _handle_mod_cover(state, file_info, message_id, chat_id, mod_parsed)
        return

    if video_parsed is not None or kind == "Video":
        title = (video_parsed or {}).get("title") or caption.strip() or "ویدیو جدید"
        state.setdefault("videos", []).append({
            "id": _uuid_short(),
            "video_file_id": file_info.get("file_id"),
            "file_type": "Video",
            "file_name": file_info.get("file_name") or file_info.get("name"),
            "message_id": message_id,
            "source_channel_guid": chat_id,
            "title": title,
        })
        print(f"DEBUG: ویدیوی جدید ثبت شد -> {title!r}")
        return

    _handle_mod_file(state, file_info, message_id, chat_id, caption)


def _uuid_short():
    import uuid
    return uuid.uuid4().hex[:8]


def _handle_mod_cover(state, file_info, message_id, chat_id, mod_parsed):
    number = mod_parsed.get("number")
    photo = {
        "file_id": file_info.get("file_id"),
        "file_name": file_info.get("file_name") or file_info.get("name"),
        "message_id": message_id,
        "source_channel_guid": chat_id,
        **mod_parsed,
    }

    if not number:
        # بدون شماره، نمی‌تونیم به فایلش وصلش کنیم؛ فقط در انتظار می‌مونه
        # (اگه فایل بعدی هشتگ شماره داشته باشه، اون شماره رو قرض می‌گیره).
        state["pending_photo"] = photo
        print("DEBUG: عکس مود بدون شمارهٔ #عدد؛ در انتظار فایل بعدی ماند")
        return

    state["pending_photo"] = photo

    # اگه فایل این شماره قبلاً (قبل از عکس) رسیده بود، همین الان مود کامل می‌شه.
    existing_file = state.get("files_by_number", {}).get(str(number))
    orphan = state.get("orphan_files", {}).pop(str(number), None)
    file_entry = existing_file or orphan
    if orphan and not existing_file:
        state.setdefault("files_by_number", {})[str(number)] = orphan

    if file_entry:
        _finalize_mod(state, photo, number)


def _handle_mod_file(state, file_info, message_id, chat_id, caption):
    number = core.extract_number(caption)
    pending = state.get("pending_photo")

    if not number and pending and pending.get("number"):
        number = pending.get("number")

    if not number:
        print("DEBUG: فایل بدون هشتگ شماره (#عدد) و بدون عکسِ در انتظار، نادیده گرفته شد")
        return

    original_name = (
        file_info.get("file_name") or file_info.get("name")
        or file_info.get("original_name") or file_info.get("title")
    )
    manual_ext = core.extract_extension_line(caption) or (
        pending.get("extension") if pending and pending.get("number") == number else ""
    )

    file_entry = {
        "file_id": file_info.get("file_id"),
        "file_type": core.normalize_send_file_type(file_info.get("file_type"), original_name),
        "file_name": original_name,
        "manual_extension": manual_ext,
        "message_id": message_id,
        "source_channel_guid": chat_id,
        "title": (pending.get("title") if pending and pending.get("number") == number else None)
                 or f"فایل شماره {number}",
    }
    state.setdefault("files_by_number", {})[number] = file_entry

    if pending and pending.get("number") == number:
        _finalize_mod(state, pending, number)
    elif pending and pending.get("number") and pending.get("number") != number:
        print(f"DEBUG: شمارهٔ فایل (#{number}) با شمارهٔ عکسِ در انتظار "
              f"(#{pending.get('number')}) یکی نیست؛ به‌صورت جدا (بدون عکس فعلاً) ذخیره شد")
        # فایلی که به عکسِ در انتظارِ فعلی مربوط نیست؛ شاید عکسش دیرتر برسه.
        state.setdefault("orphan_files", {})[number] = file_entry
    else:
        # عکسِ در انتظار نداریم؛ شاید مودی با همین شماره از قبل ثبت شده،
        # یا عکسش دیرتر می‌رسه (فایل زودتر از عکس فرستاده شده).
        existing_mod = next((m for m in state.get("mods", []) if m.get("number") == number), None)
        if not existing_mod:
            state.setdefault("orphan_files", {})[number] = file_entry
            print(f"DEBUG: فایل #{number} بدون عکس همراه؛ منتظر عکس با همین شماره ماند")


def _finalize_mod(state, photo, number):
    """وقتی هم عکس و هم فایل یک شماره آماده شدن، رکورد مود رو می‌سازه
    (یا اگه از قبل بود، به‌روزش می‌کنه) و pending_photo رو خالی می‌کنه."""
    existing = next((m for m in state.get("mods", []) if m.get("number") == number), None)
    payload = {
        "photo_file_id": photo["file_id"],
        "photo_file_name": photo.get("file_name"),
        "photo_message_id": photo.get("message_id"),
        "source_channel_guid": photo.get("source_channel_guid"),
        "title": photo.get("title") or (existing or {}).get("title") or f"مود شماره {number}",
        "description": photo.get("description", (existing or {}).get("description", "")),
        "version": photo.get("version", (existing or {}).get("version", "")),
        "number": number,
    }
    if existing:
        existing.update(payload)
        print(f"DEBUG: مود #{number} به‌روزرسانی شد -> {payload['title']!r}")
    else:
        payload["id"] = _uuid_short()
        state.setdefault("mods", []).append(payload)
        print(f"DEBUG: مود کامل جدید ثبت شد -> #{number} {payload['title']!r}")

    if state.get("pending_photo", {}) and state["pending_photo"].get("number") == number:
        state["pending_photo"] = None


def handle_start_command(token, config, state, chat_id):
    welcome = config.get(
        "start_text",
        "👋 سلام و خوش اومدید!\n\n"
        "برای دریافت هر مود، شماره‌ای که زیر همون پست توی کانال نوشته شده "
        "رو برام بفرستید (مثلاً 9 یا #9)، بعد /file رو بزنید تا فایلش براتون بیاد.\n\n"
        "🎫 اگه با پشتیبانی کاری داشتید، با /ticket می‌تونید برام پیام بذارید."
    )
    channels = config.get("required_join_channels", [])
    if channels:
        welcome += "\n\n" + core.build_join_prompt(channels)
    core.send_message(token, chat_id, welcome)


def handle_number_request(token, config, state, chat_id, raw_text):
    text = core.clean_command_text(raw_text)
    if not text.lstrip("#/").isdigit() or not text:
        return False
    number = str(int(text.lstrip("#/")))  # صفر ابتدایی حذف می‌شه

    entry = state.get("files_by_number", {}).get(number)
    if not entry:
        core.send_message(token, chat_id, f"فایلی با شمارهٔ {number} پیدا نشد.")
        return True

    if not core.should_send_now(state, f"numreq:{chat_id}:{number}", NUMBER_REQUEST_COOLDOWN_MINUTES):
        print(f"DEBUG: درخواست تکراری #{number} از {chat_id} — نادیده گرفته شد.")
        return True

    state.setdefault("pending_file_requests", {})[chat_id] = {
        "number": number,
        "requested_at": core.tehran_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    core.send_message(token, chat_id, core.build_join_prompt(config.get("required_join_channels", [])))
    return True


def handle_file_command(token, config, state, chat_id):
    """فایل مود آخرین شماره‌ای که کاربر درخواست کرده را تحویل می‌دهد.
    این مسیر عضویت را چک نمی‌کند."""
    if not chat_id:
        return True

    pending = state.setdefault("pending_file_requests", {}).get(chat_id)
    if not pending:
        core.send_message(token, chat_id, "⚠️ اول شمارهٔ مود را بفرستید؛ سپس برای دریافت آن /file را بزنید.")
        return True

    number = str(pending.get("number", "")).strip()
    entry = state.get("files_by_number", {}).get(number)
    state["pending_file_requests"].pop(chat_id, None)  # درخواست یک‌بارمصرف

    if not entry or not entry.get("file_id"):
        core.send_message(token, chat_id, f"فایل مود شمارهٔ {number} دیگر در انبار ربات پیدا نشد.")
        return True

    try:
        core.send_file(
            token, chat_id, entry["file_id"], f"#{number}",
            file_type=entry.get("file_type") or "File",
            file_name=core.file_send_name(number, entry),
            source_chat_id=entry.get("source_channel_guid"),
            source_message_id=entry.get("message_id"),
        )
    except core.SendUncertainError as e:
        core.log_error(state, "تحویل فایل (نامطمئن)", e)
    except Exception as e:
        core.log_error(state, "تحویل فایل", e)
        core.send_message(token, chat_id, "⚠️ در ارسال فایل مشکلی پیش اومد؛ لطفاً دوباره امتحان کنید یا /ticket بزنید.")
    return True


def run_resync(token, config, state):
    """از ابتدای بافر روبیکا (نه last_offset_id فعلی) دوباره می‌خونه و از
    کانال منبع، مود/ویدیوهای جامونده رو پیدا می‌کنه."""
    offset = None
    total_updates = 0
    mods_before = len(state.get("mods", []))
    videos_before = len(state.get("videos", []))
    source_guid = config.get("source_channel_guid")

    for _ in range(30):
        try:
            resp = core.get_updates(token, offset_id=offset, limit=50)
        except Exception as e:
            core.log_error(state, "دستور /update", e)
            break
        batch = resp.get("updates", []) if isinstance(resp, dict) else []
        next_off = resp.get("next_offset_id") if isinstance(resp, dict) else None
        total_updates += len(batch)

        for update in batch:
            msg = update.get("new_message") or update.get("updated_message") or update
            if not isinstance(msg, dict):
                continue
            chat_id = core.extract_chat_id(update, msg)
            if chat_id == source_guid:
                try:
                    handle_source_channel_message(state, msg, chat_id)
                except Exception as e:
                    core.log_error(state, "پردازش /update", e)

        if not batch or not next_off or next_off == offset:
            offset = next_off or core.extract_fallback_offset(batch) or offset
            break
        offset = next_off

    if offset:
        state["last_offset_id"] = offset

    return total_updates, len(state.get("mods", [])) - mods_before, len(state.get("videos", [])) - videos_before


# ===========================================================================
# بخش «ادمین»: دستورهای مالک، پنل، پخش، پست فوری، زمان‌بند
# ===========================================================================
def handle_ticket_flow(token, config, state, chat_id, text):
    awaiting = state.setdefault("awaiting_ticket", [])

    if chat_id in awaiting:
        awaiting.remove(chat_id)
        now = core.tehran_now().strftime("%Y-%m-%d %H:%M")
        core.notify_owner(
            token, config,
            f"🎫 تیکت جدید\nGUID فرستنده: {chat_id}\nزمان: {now}\nمتن: {text}"
        )
        core.send_message(token, chat_id, config.get("texts", {}).get(
            "ticket_sent", "تیکت شما ارسال شد. با تشکر 🙏"))
        return True

    if text == "/ticket":
        if chat_id not in awaiting:
            awaiting.append(chat_id)
        core.send_message(token, chat_id, config.get("texts", {}).get(
            "ticket_prompt", "لطفاً متن تیکت خودتون رو بنویسید."))
        return True

    return False


def handle_owner_message(token, config, state, text, msg=None):
    owner = config["owner_guid"]
    panel_keyword = core.clean_command_text(config.get("panel_keyword", "پنل"))
    broadcast_secret = config.get("broadcast_secret", "")
    clean_text = core.clean_command_text(text)

    if state.get("awaiting_broadcast"):
        state["awaiting_broadcast"] = False
        sent, failed = core.broadcast_to_users(token, state, text)
        core.send_message(token, owner, f"📣 پیام برای {sent} کاربر ارسال شد. (ناموفق: {failed})")
        return

    if broadcast_secret and text.strip() == broadcast_secret:
        state["awaiting_broadcast"] = True
        core.send_message(token, owner, "پیام خودت رو بفرست تا برای همهٔ کاربرها ارسالش کنم.")
        return

    if state.get("awaiting_channelcast"):
        state["awaiting_channelcast"] = False
        sent, failed = core.broadcast_to_channels(token, config, text)
        core.send_message(token, owner, f"📡 پیام در {sent} کانال پست شد. (ناموفق: {failed})")
        return

    if clean_text == "/channelcast":
        state["awaiting_channelcast"] = True
        core.send_message(token, owner, "پیام خودت رو بفرست تا توی همهٔ کانال‌های فعال پست کنم.")
        return

    if clean_text == panel_keyword:
        state["awaiting_panel_selection"] = True
        core.send_message(token, owner, core.build_panel_list(config))
        return

    if state.get("awaiting_panel_selection") and clean_text.isdigit():
        state["awaiting_panel_selection"] = False
        core.send_message(token, owner, core.build_channel_detail(config, state, int(clean_text)))
        return

    if clean_text.startswith("/postmod"):
        target = clean_text.split(maxsplit=1)[1].strip() if len(clean_text.split(maxsplit=1)) > 1 else ""
        run_force_post_typed(token, config, state, target, "mod")
        return

    if clean_text.startswith("/postvideo"):
        target = clean_text.split(maxsplit=1)[1].strip() if len(clean_text.split(maxsplit=1)) > 1 else ""
        run_force_post_typed(token, config, state, target, "video")
        return

    if clean_text.startswith("/post "):
        target = clean_text.split(maxsplit=1)[1].strip() if len(clean_text.split(maxsplit=1)) > 1 else ""
        run_force_post_typed(token, config, state, target, "mod")
        return

    if clean_text.startswith("/block"):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            core.send_message(token, owner, "فرمت درست: /block <chat_id> <روز یا permanent> <دلیل>")
        else:
            target_id, days_part = parts[1], parts[2]
            reason = parts[3] if len(parts) > 3 else ""
            days = None if days_part.lower() == "permanent" else days_part
            try:
                info = core.block_user(state, target_id, reason, days)
                core.send_message(token, owner, f"⛔ {target_id} مسدود شد.\nتا: {info['until'] or 'دائمی'}")
            except Exception as e:
                core.send_message(token, owner, f"خطا در مسدودسازی: {e}")
        return

    if clean_text.startswith("/unblock"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            core.send_message(token, owner, "فرمت درست: /unblock <chat_id>")
        else:
            ok = core.unblock_user(state, parts[1])
            core.send_message(token, owner, "✅ رفع مسدودیت شد." if ok else "این آیدی مسدود نبود.")
        return

    if clean_text == "/blocked":
        core.send_message(token, owner, core.build_blocked_list(state))
        return

    if clean_text == "/update":
        total, new_mods, new_videos = run_resync(token, config, state)
        core.send_message(
            token, owner,
            f"🔄 بازخوانی کامل شد.\nکل آپدیت‌های بررسی‌شده: {total}\n"
            f"مود جدید پیدا شد: {new_mods}\nویدیوی جدید پیدا شد: {new_videos}"
        )
        return

    if clean_text.startswith("/reply"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            core.send_message(token, owner, "فرمت درست: /reply <GUID> <متن پاسخ>\n(GUID رو از همون پیام تیکتی که برات فرستادم بردار)")
        else:
            target_guid, reply_text = parts[1], parts[2]
            try:
                core.send_message(token, target_guid, f"📩 پاسخ پشتیبانی:\n{reply_text}")
                core.send_message(token, owner, "✅ پاسخ ارسال شد.")
            except Exception as e:
                core.send_message(token, owner, f"❌ ارسال ناموفق بود: {e}")
        return

    if clean_text == "/bugs":
        core.send_message(token, owner, core.build_bugs_page(state))
        return

    if clean_text == "/clearbugs":
        n = len(state.get("errors", []))
        state["errors"] = []
        state["bug_page_offset"] = 0
        core.send_message(token, owner, f"🧹 لیست باگ‌ها پاک شد ({n} مورد حذف شد).")
        return

    if clean_text == "/resetall":
        core.send_message(
            token, owner,
            "⚠️ این کار همهٔ مودها، ویدیوها، فایل‌های شماره‌گذاری‌شده و "
            "تاریخچهٔ پست‌های هر کانال رو کامل پاک می‌کنه (غیرقابل بازگشت).\n"
            "برای تأیید، دقیقاً بفرست: /resetall confirm"
        )
        return

    if clean_text == "/resetall confirm":
        n_mods, n_videos = len(state.get("mods", [])), len(state.get("videos", []))
        n_files = len(state.get("files_by_number", {}))
        state.update({
            "mods": [], "videos": [], "files_by_number": {}, "orphan_files": {},
            "pending_photo": None, "pending_file_requests": {},
            "used_mods_per_channel": {}, "used_videos_per_channel": {}, "mod_progress": {},
            "posted_slots_today": {"date": None, "mod": {}, "video": {}},
        })
        core.send_message(
            token, owner,
            f"🗑 پاک‌سازی کامل انجام شد.\nمودها: {n_mods} | ویدیوها: {n_videos} | فایل‌های شماره‌دار: {n_files}\n"
            f"از این لحظه به بعد آرشیو خالیه — منتظر پست جدید از کانال منبع می‌مونه."
        )
        return

    if not core.should_send_now(state, f"ownerreply:{clean_text}", STATUS_REPLY_COOLDOWN_MINUTES):
        print(f"DEBUG: همین متن به مالک به‌تازگی پاسخ داده شده بود؛ دوباره پاسخ داده نشد: {text!r}")
        return

    n_mods, n_videos = len(state.get("mods", [])), len(state.get("videos", []))
    n_errors, n_users = len(state.get("errors", [])), len(state.get("known_users", []))
    now = core.tehran_now().strftime("%Y-%m-%d %H:%M")
    status = (
        f"✅ ربات فعال است.\n🕰 ساعت تهران: {now}\n👥 تعداد کاربران: {n_users}\n"
        f"🎮 مودهای ذخیره‌شده: {n_mods}\n🎬 ویدیوهای ذخیره‌شده: {n_videos}\n"
        f"🐞 تعداد کل باگ‌های ثبت‌شده: {n_errors}\n"
        f"برای دیدن گزارش باگ‌ها: /bugs\nبرای پاک کردن لیست باگ‌ها: /clearbugs\n"
        f"برای پاک‌سازی کامل آرشیو مود/ویدیو: /resetall\nبرای پنل کانال‌ها: {config.get('panel_keyword', 'پنل')}\n"
        f"برای پاسخ به تیکت: /reply <GUID> <متن>\nبرای راهنما: /help"
    )
    core.send_message(token, owner, status)


def run_posting_schedule(token, config, state):
    now = core.tehran_now()
    today = now.strftime("%Y-%m-%d")
    minute_of_day = now.hour * 60 + now.minute

    start_h = config["schedule"]["start_hour_tehran"]
    end_h = config["schedule"]["end_hour_tehran"]
    start_min, end_min = start_h * 60, end_h * 60
    if not (start_min <= minute_of_day <= end_min):
        return

    posted = state.setdefault("posted_slots_today", {"date": today, "mod": {}, "video": {}})
    if posted.get("date") != today:
        posted["date"] = today
        posted["mod"] = {}
        posted["video"] = {}

    TOLERANCE_MIN = 15
    elapsed = minute_of_day - start_min
    posted_summary = []

    for channel in config["destination_channels"]:
        if not channel.get("enabled", True):
            continue
        guid = channel["guid"]
        label = core.channel_label(channel)
        mod_h, video_h = core.resolve_channel_plan(config, channel)
        mod_interval_min = max(1, round(mod_h * 60))
        video_interval_min = max(1, round(video_h * 60))

        mod_slot = elapsed // mod_interval_min
        video_slot = elapsed // video_interval_min
        due_mod = (elapsed % mod_interval_min) < TOLERANCE_MIN
        due_video = (elapsed % video_interval_min) < TOLERANCE_MIN

        mod_key, video_key = f"{guid}:{mod_slot}", f"{guid}:{video_slot}"

        if due_mod and not posted["mod"].get(mod_key):
    # قبل از تلاش علامت بزن تا در حالت حلقه‌ای سریع، پشت‌سرهم
    # retry نشه و کل API و مودهای آرشیو هدر نره.
    posted["mod"][mod_key] = True
    try:
        item = core.post_next_mod(token, channel, state)
        if item is not None:
            posted_summary.append(f"🎮 {label}: مود «{item.get('title')}»")
    except Exception as e:
        core.log_error(state, f"ارسال مود به {label}", e)

if due_video and not posted["video"].get(video_key):
    posted["video"][video_key] = True
    try:
        vitem = core.post_next_video(token, channel, state)
        if vitem is not None:
            posted_summary.append(f"🎬 {label}: ویدیو «{vitem['title']}»")
    except Exception as e:
        core.log_error(state, f"ارسال ویدیو به {label}", e)

    if posted_summary:
        core.notify_owner(token, config, f"📤 گزارش پست {now.strftime('%H:%M')}\n" + "\n".join(posted_summary))


def resolve_post_targets(config, target):
    target = (target or "").strip()
    all_channels = config["destination_channels"]
    if not target:
        return None, "فرمت درست: <شماره کانال یا all>"
    if target.lower() == "all":
        return [c for c in all_channels if c.get("enabled", True)], None
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(all_channels):
            return [all_channels[idx]], None
        return None, f"شمارهٔ کانال {target} معتبر نیست."
    return None, "مقدار باید عدد یا all باشه."


def run_force_post_typed(token, config, state, target, kind):
    targets, err = resolve_post_targets(config, target)
    if err:
        core.notify_owner(token, config, f"⚠️ پست فوری: {err}")
        return

    kind_fa = "مود" if kind == "mod" else "ویدیو"
    summary = []
    for channel in targets:
        label = core.channel_label(channel)
        try:
            if kind == "mod":
                item = core.post_next_mod(token, channel, state)
                if item is not None:
                    summary.append(f"🎮 {label}: مود «{item.get('title')}»")
            else:
                item = core.post_next_video(token, channel, state)
                if item is not None:
                    summary.append(f"🎬 {label}: ویدیو «{item['title']}»")
        except Exception as e:
            core.log_error(state, f"پست فوری {kind_fa} برای {label}", e)

    if summary:
        core.notify_owner(token, config, "🚀 پست فوری انجام شد:\n" + "\n".join(summary))
    else:
        core.notify_owner(
            token, config,
            f"ℹ️ پست فوری: هیچ {kind_fa}یِ آماده‌ای برای این کانال(ها) موجود نبود.\n"
            f"(مطمئن شو حداقل یک {kind_fa} کامل در state.json ثبت شده — با /bugs یا پیام وضعیت چک کن.)"
        )


def run_force_post(token, config, state, target):
    """برای سازگاری با ورودی force_post_channel در Actions (فقط مود)."""
    if not (target or "").strip():
        return
    run_force_post_typed(token, config, state, target, "mod")


# ===========================================================================
# main
# ===========================================================================
def main():
    config = core.load_config()
    state = core.load_state()
    token = os.environ.get("RUBIKA_BOT_TOKEN") or config.get("bot_token")
    if not token:
        print("DEBUG: توکن پیدا نشد")
        return

    owner_guid = config.get("owner_guid")
    source_guid = config.get("source_channel_guid")

    core.track_channel_activation(state, config)
    core.prune_known_users(state, config)
    n_users_before = len(state.get("known_users", []))

    print(f"DEBUG: وضعیت لود شده -> last_offset_id={state.get('last_offset_id')!r} "
          f"mods={len(state.get('mods', []))} videos={len(state.get('videos', []))} "
          f"users={n_users_before} errors={len(state.get('errors', []))}")

    if os.environ.get("FLUSH_UPDATES", "").strip().lower() in ("yes", "true", "1"):
        flushed = 0
        offset = state.get("last_offset_id")
        for _ in range(30):
            try:
                resp = core.get_updates(token, offset_id=offset, limit=50)
            except Exception as e:
                core.log_error(state, "پاک‌سازی انبار", e)
                break
            batch = resp.get("updates", []) if isinstance(resp, dict) else []
            next_off = resp.get("next_offset_id") if isinstance(resp, dict) else None
            flushed += len(batch)
            if not batch or not next_off or next_off == offset:
                offset = next_off or core.extract_fallback_offset(batch) or offset
                break
            offset = next_off
        if offset:
            state["last_offset_id"] = offset
        core.notify_owner(token, config, f"🧹 پاک‌سازی انجام شد. {flushed} پیام قدیمی بدون پاسخ دور ریخته شد.")
        core.save_state(state)
        print(f"DEBUG: flush کامل شد -> {flushed} پیام, offset نهایی={offset!r}")
        return

    if os.environ.get("TRIGGER_TYPE") == "workflow_dispatch" and core.should_send_now(state, "heartbeat", 8):
        try:
            me = core.get_me(token)
            bot_name = (me.get("bot") or {}).get("title") or "ربات"
            now_str = core.tehran_now().strftime("%Y-%m-%d %H:%M")
            n_active_channels = len([c for c in config.get("destination_channels", []) if c.get("enabled", True)])
            core.notify_owner(
                token, config,
                f"🟢 {bot_name} راه‌اندازی شد و وصل است.\n🕰 ساعت تهران: {now_str}\n"
                f"👥 تعداد کاربران: {n_users_before}\n📡 تعداد کانال‌های فعال: {n_active_channels}\n"
                f"🎮 تعداد مودها: {len(state.get('mods', []))}\n🎬 تعداد ویدیوها: {len(state.get('videos', []))}"
            )
        except Exception as e:
            core.log_error(state, "تست اتصال (getMe)", e)

    force_target = os.environ.get("FORCE_POST_CHANNEL", "")
    if force_target:
        try:
            run_force_post(token, config, state, force_target)
        except Exception as e:
            core.log_error(state, "پست فوری", e)

    try:
        resp = core.get_updates(token, offset_id=state.get("last_offset_id"), limit=50)
        updates = resp.get("updates", []) if isinstance(resp, dict) else []
        next_offset = resp.get("next_offset_id") if isinstance(resp, dict) else None
    except Exception as e:
        core.log_error(state, "دریافت آپدیت‌ها", e)
        updates = []
        next_offset = None

    print(f"DEBUG: تعداد آپدیت‌های دریافتی: {len(updates)} | next_offset={next_offset!r}")

    for update in updates:
        print(f"DEBUG: RAW update کامل: {update}")
        msg = update.get("new_message") or update.get("updated_message") or update
        if not isinstance(msg, dict):
            continue

        chat_id = core.extract_chat_id(update, msg)
        update_identity = core.get_update_identity(update, msg)
        if core.was_processed(state, update_identity):
            print(f"DEBUG: آپدیت تکراری رد شد -> {update_identity}")
            continue

        raw_text = msg.get("text") or ""
        text = core.clean_command_text(raw_text)
        print(f"DEBUG: پیام -> chat_id={chat_id} | text={raw_text!r}")

        try:
            if chat_id and chat_id != owner_guid:
                block_info = core.is_blocked(state, chat_id)
                if block_info:
                    core.send_message(token, chat_id, core.build_blocked_message(block_info))
                    core.mark_processed(state, update_identity)
                    continue

            is_new_user = core.track_known_user(state, config, chat_id)
            owner_needs_keypad = chat_id == owner_guid and not state.get("owner_keypad_set")
            if is_new_user or owner_needs_keypad:
                try:
                    core.set_chat_keypad(token, chat_id, ["/help", "/ticket"])
                    if owner_needs_keypad:
                        state["owner_keypad_set"] = True
                except Exception as e:
                    core.log_error(state, "تنظیم کیبورد ثابت", e)

            if core.is_user_guid(chat_id) and chat_id not in (owner_guid, source_guid):
                if core.check_spam(state, chat_id) and core.should_send_now(
                        state, f"spamreport:{chat_id}", core.SPAM_REPORT_COOLDOWN_MINUTES):
                    core.notify_owner(
                        token, config,
                        f"🚨 فعالیت مشکوک/اسپم\nGUID فرد: {chat_id}\n"
                        f"بیش از {core.SPAM_THRESHOLD} پیام در {core.SPAM_WINDOW_MINUTES} دقیقهٔ اخیر."
                    )

            if chat_id and chat_id == source_guid:
                print(f"DEBUG: RAW پیام کانال منبع (کامل): {msg}")

            if text == "/myidver001" and chat_id:
                core.send_message(token, chat_id, f"GUID این چت:\n{chat_id}")

            elif text == "/start" and chat_id and chat_id not in (owner_guid, source_guid):
                if core.should_send_now(state, f"start:{chat_id}", INFO_REPLY_COOLDOWN_MINUTES):
                    handle_start_command(token, config, state, chat_id)

            elif text == "/help" and chat_id:
                if core.should_send_now(state, f"help:{chat_id}", INFO_REPLY_COOLDOWN_MINUTES):
                    help_text = config.get(
                        "help_text", "برای دریافت فایل مود، شمارهٔ زیر پست رو با # یا / به من بفرستید (مثلاً #1).")
                    core.send_message(token, chat_id, help_text)

            elif chat_id and handle_ticket_flow(token, config, state, chat_id, raw_text):
                pass

            elif text == "/file" and chat_id:
                handle_file_command(token, config, state, chat_id)

            elif not msg.get("file") and handle_number_request(token, config, state, chat_id, raw_text):
                pass

            elif chat_id and msg.get("file") and chat_id in (source_guid, owner_guid):
                # پست کانال منبع، یا فایل/عکسی که مستقیم (یا فوروارد) به
                # پیوی خودِ ربات فرستاده شده — هر دو با یک منطق پردازش می‌شن.
                handle_source_channel_message(state, msg, chat_id)

            elif chat_id and chat_id == owner_guid:
                handle_owner_message(token, config, state, raw_text, msg)

        except Exception as e:
            core.log_error(state, "پردازش پیام", e)
        finally:
            core.mark_processed(state, update_identity)

    if next_offset:
        state["last_offset_id"] = next_offset
    else:
        print("DEBUG: next_offset_id خالی بود؛ offset قبلی حفظ شد و dedupe از تکرار جلوگیری می‌کند.")

    try:
        run_posting_schedule(token, config, state)
    except Exception as e:
        core.log_error(state, "زمان‌بند پست‌گذاری", e)

    try:
        core.maybe_notify_new_errors(token, config, state)
    except Exception:
        pass

    core.trim_stored_content(state)
    core.prune_sent_log(state)

    print(f"DEBUG: وضعیت قبل از ذخیره -> last_offset_id={state.get('last_offset_id')!r} "
          f"mods={len(state.get('mods', []))} videos={len(state.get('videos', []))} "
          f"users={len(state.get('known_users', []))}")

    core.save_state(state)


def main_loop():
    """اجرای پیوسته برای Termux/سرور: هر چند ثانیه یک‌بار main() را
    صدا می‌زند تا:
      - پیام‌های کاربران تقریباً آنی جواب بگیرند (تأخیر = POLL_INTERVAL_SECONDS)
      - زمان‌بند پست‌گذاری هم مداوم چک شود و سر ساعت پست شود.
    """
    import time as _time
    interval = max(2, int(os.environ.get("POLL_INTERVAL_SECONDS", "5")))
    print(f"DEBUG: LOOP_MODE فعال است — هر {interval} ثانیه یک چرخه اجرا می‌شود. (Ctrl+C برای توقف)")
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\nDEBUG: با Ctrl+C متوقف شد.")
            break
        except Exception as e:
            # خطا در یک چرخه کل حلقه رو نمی‌کشه
            print(f"DEBUG: خطا در یک چرخه (ادامه می‌دهم): {e}", file=sys.stderr)
        _time.sleep(interval)


if __name__ == "__main__":
    try:
        if os.environ.get("LOOP_MODE", "").strip().lower() in ("1", "yes", "true", "on"):
            main_loop()
        else:
            main()
    except Exception as fatal:
        print(f"خطای کلی و غیرمنتظره: {fatal}", file=sys.stderr)
        raise
