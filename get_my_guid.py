"""
get_my_guid.py
---------------
برای پیدا کردن GUID پیوی خودتان یا GUID کانال منبع، این اسکریپت را (از تب
Actions در گیت‌هاب، workflow با نام "Find GUIDs") دستی اجرا کنید.

قبلش:
  - یک پیام دلخواه (مثلاً "سلام") به پیوی ربات بفرستید تا owner_guid دیده شود.
  - مطمئن شوید ربات در کانال منبع ادمین است و کانال منبع اخیراً پستی گذاشته
    (یا یک پست تستی در کانال منبع بگذارید) تا source_channel_guid دیده شود.

خروجی را در لاگ اجرای Actions (قابل مشاهده از گوشی) می‌بینید.
"""

import os
from rubka import Robot


def main():
    token = os.environ.get("RUBIKA_BOT_TOKEN")
    bot = Robot(token=token)

    resp = bot.get_updates(limit=100)
    updates = resp.get("data", {}).get("updates", []) if isinstance(resp, dict) else []

    seen = {}
    for u in updates:
        msg = u.get("new_message") or u.get("updated_message") or {}
        chat_id = msg.get("chat_id") or u.get("chat_id")
        if not chat_id:
            continue
        preview = (msg.get("text") or "").strip()[:40]
        seen.setdefault(chat_id, {"count": 0, "sample_text": preview})
        seen[chat_id]["count"] += 1

    print("=== GUID های دیده‌شده در آپدیت‌های اخیر ===")
    if not seen:
        print("هیچ آپدیتی دیده نشد. یک پیام به پیوی ربات بفرستید یا در کانال منبع پستی بگذارید و دوباره اجرا کنید.")
        return

    for chat_id, info in seen.items():
        print(f"{chat_id}   |   تعداد پیام: {info['count']}   |   نمونه متن: {info['sample_text']}")

    print("\nراهنما:")
    print("- اگر همین الان به ربات پیام دادید، GUID حساب خودتان همینجاست -> بگذارید در owner_guid")
    print("- اگر کانال منبع پستی گذاشته، GUID آن همینجاست -> بگذارید در source_channel_guid")
    print("- برای هر کانال مقصد هم همین کار را انجام دهید (یک پیام تستی در آن کانال بگذارید)")


if __name__ == "__main__":
    main()
