import os
import re
import html
import time
import sqlite3
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ChatMemberHandler,
)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_USER_ID = 190503955

SPAM_MESSAGE_LIMIT = 8
SPAM_WINDOW_SECONDS = 3
SPAM_COOLDOWN_SECONDS = 5

DB_FILE = "bot_data.db"


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)


db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    joined_at TEXT,
    last_seen TEXT
)
""")


db.execute("""
CREATE TABLE IF NOT EXISTS blocked_users (
    user_id INTEGER PRIMARY KEY,
    blocked_at TEXT
)
""")


db.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    direction TEXT,
    text TEXT,
    created_at TEXT
)
""")


db.execute("""
CREATE TABLE IF NOT EXISTS admin_channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT
)
""")


db.commit()


user_message_times = defaultdict(deque)
user_blocked_until = {}

reply_targets = {}
broadcast_mode = set()

button_states = {}
pending_banner = {}


BAD_WORDS = {
    "کص ننت",
    "کصت ننت",
    "کص کش",
    "کصکش",
    "مادر جنده",
    "مادرجنده",
    "مادر قحبه",
    "مادرقحبه",
    "مادر کصته",
    "مادرکصته",
    "ننه جنده",
    "ننه قحبه",
    "ننه سگ",
    "ننه سگه",
    "ننه کصه",
    "خواهر جنده",
    "خواهرجنده",
    "جنده",
    "قحبه",
    "مادرت",
    "ننت",
    "خواهرت",
    "mother fucker",
    "motherfucker",
    "son of a bitch",
    "son of bitch",
}


def contains_profanity(text: str) -> bool:
    if not text:
        return False

    text = text.lower()
    normalized = re.sub(r"[\u200c\u200d]", " ", text)

    for word in BAD_WORDS:
        if word in normalized:
            return True

    return False


def now():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def save_user(user):
    if not user:
        return

    timestamp = now()

    db.execute("""
    INSERT INTO users (
        user_id,
        first_name,
        last_name,
        username,
        joined_at,
        last_seen
    )
    VALUES (?, ?, ?, ?, ?, ?)

    ON CONFLICT(user_id) DO UPDATE SET
        first_name=excluded.first_name,
        last_name=excluded.last_name,
        username=excluded.username,
        last_seen=excluded.last_seen
    """, (
        user.id,
        user.first_name or "",
        user.last_name or "",
        user.username or "",
        timestamp,
        timestamp
    ))

    db.commit()


def is_blocked(user_id):
    row = db.execute(
        "SELECT 1 FROM blocked_users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    return row is not None


def block_user(user_id):
    db.execute(
        "INSERT OR IGNORE INTO blocked_users(user_id, blocked_at) VALUES (?, ?)",
        (user_id, now())
    )
    db.commit()


def unblock_user(user_id):
    db.execute(
        "DELETE FROM blocked_users WHERE user_id=?",
        (user_id,)
    )
    db.commit()


def save_message(user_id, direction, text):
    db.execute("""
    INSERT INTO messages (
        user_id,
        direction,
        text,
        created_at
    )
    VALUES (?, ?, ?, ?)
    """, (
        user_id,
        direction,
        text or "",
        now()
    ))

    db.commit()


def save_channel(chat_id, title):
    db.execute("""
    INSERT INTO admin_channels (
        chat_id,
        title,
        added_at
    )
    VALUES (?, ?, ?)

    ON CONFLICT(chat_id) DO UPDATE SET
        title=excluded.title
    """, (
        chat_id,
        title,
        now()
    ))

    db.commit()


def get_channels():
    return db.execute("""
    SELECT chat_id, title
    FROM admin_channels
    ORDER BY added_at DESC
    """).fetchall()


def user_display_name(user):
    name = " ".join(
        x for x in [user.first_name, user.last_name]
        if x
    ).strip()

    return name or "کاربر"


def user_mention(user):
    name = html.escape(
        user_display_name(user)
    )

    return (
        f'<a href="tg://user?id={user.id}">'
        f'{name}'
        f'</a>'
    )


def user_profile_button(user_id):
    return InlineKeyboardButton(
        "پروفایل کاربر",
        url=f"tg://user?id={user_id}"
    )


def user_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "ارسال پیام",
                callback_data="user_send"
            ),
            InlineKeyboardButton(
                "راهنما",
                callback_data="user_help"
            )
        ]
    ])


def user_reply_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "پاسخ",
                callback_data="user_reply"
            )
        ]
    ])


def admin_message_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "پاسخ به کاربر",
                callback_data=f"reply:{user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "بلاک کردن",
                callback_data=f"block:{user_id}"
            ),
            InlineKeyboardButton(
                "آنبلاک",
                callback_data=f"unblock:{user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "حذف چت",
                callback_data=f"deletechat:{user_id}"
            ),
            InlineKeyboardButton(
                "بکاپ چت",
                callback_data=f"backup:{user_id}"
            )
        ],
        [
            user_profile_button(user_id)
        ]
    ])


def get_message_text(message):
    if message.text:
        return message.text

    if message.caption:
        return message.caption

    return ""


def message_has_supported_content(message):
    return any([
        message.text,
        message.photo,
        message.video,
        message.animation,
        message.document,
        message.audio,
        message.voice,
        message.video_note,
        message.sticker,
    ])


def check_rate_limit(user_id):
    current = time.monotonic()

    if user_id in user_blocked_until:

        if current < user_blocked_until[user_id]:
            return False

        del user_blocked_until[user_id]

    times = user_message_times[user_id]

    while times and current - times[0] > SPAM_WINDOW_SECONDS:
        times.popleft()

    if len(times) >= SPAM_MESSAGE_LIMIT:

        user_blocked_until[user_id] = (
            current + SPAM_COOLDOWN_SECONDS
        )

        times.clear()

        return False

    times.append(current)

    return True


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not user:
        return

    if update.effective_chat.type != "private":
        return

    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "شما بلاک هستید."
        )
        return

    await update.message.reply_text(
        "سلام 👋\n\n"
        "پیامت رو ارسال کن تا برای مدیریت ارسال بشه.\n\n"
        "متن، عکس، ویدئو، GIF، استیکر، فایل، ویس و صدا قابل ارسال است.",
        reply_markup=user_keyboard()
    )


# =========================================================
# SEND USER MESSAGE TO ADMIN
# =========================================================

async def send_message_to_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user
):
    message = update.message

    mention = user_mention(user)
    received_time = now()

    header = (
        "پیام جدید از کاربر\n\n"
        f"{mention}\n"
        f"زمان دریافت: {received_time}"
    )

    if message.text:

        admin_text = (
            f"{header}\n\n"
            f"{html.escape(message.text)}"
        )

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=admin_text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_message_keyboard(user.id)
        )

        return

    caption = message.caption or ""

    media_header = header

    if caption:
        media_header += (
            "\n\n"
            "کپشن:\n"
            f"{html.escape(caption)}"
        )

    await context.bot.send_message(
        chat_id=ADMIN_USER_ID,
        text=media_header,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_message_keyboard(user.id)
    )

    await context.bot.copy_message(
        chat_id=ADMIN_USER_ID,
        from_chat_id=message.chat_id,
        message_id=message.message_id
    )


# =========================================================
# USER MESSAGE
# =========================================================

async def handle_user_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    if update.effective_chat.type != "private":
        return

    save_user(user)

    if is_blocked(user.id):

        try:
            await message.delete()
        except Exception:
            pass

        return

    if not check_rate_limit(user.id):

        await message.reply_text(
            f"لطفاً {SPAM_COOLDOWN_SECONDS} ثانیه دیگر دوباره تلاش کنید."
        )

        return

    if not message_has_supported_content(message):

        await message.reply_text(
            "این نوع پیام پشتیبانی نمی‌شود."
        )

        return

    text = get_message_text(message)

    if text and contains_profanity(text):

        await message.reply_text(
            "پیام شما به دلیل توهین ارسال نشد."
        )

        return

    save_message(
        user.id,
        "user",
        text
    )

    try:

        await send_message_to_admin(
            update,
            context,
            user
        )

        await message.reply_text(
            "پیام شما دریافت شد. منتظر پاسخ باشید."
        )

    except Exception as e:

        logger.exception(
            "Could not send user message to admin: %s",
            e
        )

        await message.reply_text(
            "خطا در ارسال پیام. لطفاً دوباره تلاش کنید."
        )


# =========================================================
# USER BUTTONS
# =========================================================

async def user_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if is_blocked(user_id):
        return

    if query.data == "user_send":

        context.user_data[
            "waiting_for_user_message"
        ] = True

        await query.message.reply_text(
            "پیامت رو ارسال کن.\n"
            "متن، عکس، ویدئو، GIF، استیکر، فایل، ویس و صدا قابل ارسال است."
        )

    elif query.data == "user_reply":

        context.user_data[
            "waiting_for_user_message"
        ] = True

        await query.message.reply_text(
            "پاسخت رو ارسال کن."
        )

    elif query.data == "user_help":

        await query.message.reply_text(
            "راهنما\n\n"
            "پیامت رو ارسال کن تا برای مدیریت ارسال بشه."
        )


# =========================================================
# BOT ADDED TO CHANNEL / GROUP
# =========================================================

async def my_chat_member_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_member = update.my_chat_member

    if not chat_member:
        return

    chat = chat_member.chat
    new_status = chat_member.new_chat_member.status

    if chat.type in ["channel", "supergroup"]:

        if new_status in ["administrator", "creator"]:

            save_channel(
                chat.id,
                chat.title or "بدون عنوان"
            )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if query.from_user.id != ADMIN_USER_ID:

        await query.answer(
            "دسترسی ندارید.",
            show_alert=True
        )

        return

    await query.answer()

    data = query.data

    try:

        action, raw_user_id = data.split(
            ":",
            1
        )

        user_id = int(raw_user_id)

    except Exception:
        return

    if action == "reply":

        reply_targets[ADMIN_USER_ID] = user_id

        await query.message.reply_text(
            "پاسخ به کاربر را ارسال کنید.\n"
            "متن، عکس، ویدئو، GIF، استیکر، فایل، ویس و صدا قابل ارسال است."
        )

    elif action == "block":

        block_user(user_id)

        await query.message.reply_text(
            "کاربر بلاک شد."
        )

        try:

            await context.bot.send_message(
                chat_id=user_id,
                text="شما بلاک شدید."
            )

        except Exception:
            pass

    elif action == "unblock":

        unblock_user(user_id)

        await query.message.reply_text(
            "کاربر آنبلاک شد."
        )

    elif action == "deletechat":

        db.execute(
            "DELETE FROM messages WHERE user_id=?",
            (user_id,)
        )

        db.commit()

        await query.message.reply_text(
            "اطلاعات ذخیره‌شده چت حذف شد."
        )

    elif action == "backup":

        rows = db.execute("""
        SELECT direction, text, created_at
        FROM messages
        WHERE user_id=?
        ORDER BY id ASC
        """, (user_id,)).fetchall()

        if not rows:

            await query.message.reply_text(
                "برای این کاربر چتی ذخیره نشده."
            )

            return

        filename = f"chat_{user_id}.txt"

        with open(
            filename,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                f"Backup for user {user_id}\n"
            )

            f.write(
                "=" * 50 + "\n\n"
            )

            for direction, text, created_at in rows:

                sender = (
                    "USER"
                    if direction == "user"
                    else "ADMIN"
                )

                f.write(
                    f"[{created_at}] {sender}:\n"
                    f"{text}\n\n"
                )

        try:

            with open(
                filename,
                "rb"
            ) as file:

                await context.bot.send_document(
                    chat_id=ADMIN_USER_ID,
                    document=file,
                    caption=f"بکاپ چت کاربر {user_id}"
                )

        finally:

            try:
                os.remove(filename)
            except Exception:
                pass


# =========================================================
# ADMIN HELP
# =========================================================

def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "لیست کاربران",
                callback_data="panel:users"
            )
        ],
        [
            InlineKeyboardButton(
                "لیست مسدود شده",
                callback_data="panel:blocked"
            )
        ],
        [
            InlineKeyboardButton(
                "ارسال همگانی",
                callback_data="panel:broadcast"
            )
        ]
    ])


async def admin_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    await update.message.reply_text(
        "پنل مدیریت\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=admin_panel_keyboard()
    )


# =========================================================
# ADMIN PANEL CALLBACK
# =========================================================

async def panel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if query.from_user.id != ADMIN_USER_ID:

        await query.answer(
            "دسترسی ندارید.",
            show_alert=True
        )

        return

    await query.answer()

    action = query.data.split(
        ":",
        1
    )[1]

    if action == "blocked":

        rows = db.execute("""
        SELECT user_id, blocked_at
        FROM blocked_users
        ORDER BY blocked_at DESC
        """).fetchall()

        if not rows:

            await query.message.reply_text(
                "هیچ کاربری بلاک نیست."
            )

            return

        text = "کاربران مسدود شده\n\n"

        for user_id, blocked_at in rows:

            text += (
                f"{user_id}\n"
                f"{blocked_at}\n\n"
            )

        await query.message.reply_text(text)

    elif action == "users":

        row = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()

        count = row[0]

        await query.message.reply_text(
            f"تعداد کاربران ثبت‌شده: {count}"
        )

    elif action == "broadcast":

        broadcast_mode.add(
            ADMIN_USER_ID
        )

        await query.message.reply_text(
            "متن پیام همگانی را ارسال کنید.\n\n"
            "برای لغو، /cancel را بفرستید."
        )


# =========================================================
# ADMIN REPLY
# =========================================================

async def send_admin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_user_id: int
):
    message = update.message

    if message.text:

        await context.bot.send_message(
            chat_id=target_user_id,
            text=message.text,
            reply_markup=user_reply_keyboard()
        )

        save_message(
            target_user_id,
            "admin",
            message.text
        )

        return

    await context.bot.copy_message(
        chat_id=target_user_id,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
        reply_markup=user_reply_keyboard()
    )

    save_message(
        target_user_id,
        "admin",
        get_message_text(message)
    )


# =========================================================
# ADMIN MESSAGE
# =========================================================

async def handle_admin_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    if user.id != ADMIN_USER_ID:
        return

    if update.effective_chat.type != "private":
        return

    text = message.text or ""

    # -----------------------------
    # CANCEL
    # -----------------------------

    if text == "/cancel":

        broadcast_mode.discard(
            ADMIN_USER_ID
        )

        reply_targets.pop(
            ADMIN_USER_ID,
            None
        )

        button_states.pop(
            ADMIN_USER_ID,
            None
        )

        pending_banner.pop(
            ADMIN_USER_ID,
            None
        )

        await message.reply_text(
            "لغو شد."
        )

        return

    # -----------------------------
    # BROADCAST
    # -----------------------------

    if ADMIN_USER_ID in broadcast_mode:

        broadcast_mode.discard(
            ADMIN_USER_ID
        )

        if not message.text:

            await message.reply_text(
                "ارسال همگانی فعلاً فقط برای پیام متنی فعال است."
            )

            return

        users = db.execute(
            "SELECT user_id FROM users"
        ).fetchall()

        sent = 0

        for (user_id,) in users:

            if is_blocked(user_id):
                continue

            try:

                await context.bot.send_message(
                    chat_id=user_id,
                    text=message.text
                )

                sent += 1

            except Exception as e:

                logger.warning(
                    "Broadcast failed for %s: %s",
                    user_id,
                    e
                )

        await message.reply_text(
            f"ارسال همگانی انجام شد.\n"
            f"تعداد ارسال موفق: {sent}"
        )

        return

    # -----------------------------
    # BUTTON
    # -----------------------------

    if text == "/button":

        button_states[ADMIN_USER_ID] = {
            "step": "text"
        }

        await message.reply_text(
            "متن بنر را ارسال کنید."
        )

        return

    # -----------------------------
    # BUTTON FLOW
    # -----------------------------

    if ADMIN_USER_ID in button_states:

        await handle_button_flow(
            update,
            context
        )

        return

    # -----------------------------
    # HELP
    # -----------------------------

    if text.lower() == "help":

        await admin_help(
            update,
            context
        )

        return

    # -----------------------------
    # REPLY TO USER
    # -----------------------------

    if ADMIN_USER_ID in reply_targets:

        target_user_id = reply_targets[
            ADMIN_USER_ID
        ]

        if is_blocked(target_user_id):

            await message.reply_text(
                "این کاربر بلاک است."
            )

            reply_targets.pop(
                ADMIN_USER_ID,
                None
            )

            return

        if not message_has_supported_content(message):

            await message.reply_text(
                "این نوع پیام پشتیبانی نمی‌شود."
            )

            return

        try:

            await send_admin_reply(
                update,
                context,
                target_user_id
            )

            await message.reply_text(
                "پاسخ ارسال شد."
            )

            reply_targets.pop(
                ADMIN_USER_ID,
                None
            )

        except Exception as e:

            logger.exception(
                "Reply failed: %s",
                e
            )

            await message.reply_text(
                "ارسال پاسخ انجام نشد."
            )

        return

    await message.reply_text(
        "برای مدیریت، help را ارسال کنید."
    )


# =========================================================
# BUTTON CREATOR
# =========================================================

async def handle_button_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id
    message = update.message

    if user_id not in button_states:

        button_states[user_id] = {
            "step": "text"
        }

        await message.reply_text(
            "متن بنر را ارسال کنید."
        )

        return

    state = button_states[user_id]

    if state["step"] == "text":

        if not message.text:

            await message.reply_text(
                "متن بنر باید متنی باشد."
            )

            return

        state["text"] = message.text
        state["step"] = "buttons"
        state["buttons"] = []

        await message.reply_text(
            "دکمه‌ها را ارسال کنید.\n\n"
            "فرمت:\n"
            "متن دکمه - لینک\n\n"
            "برای دکمه کنار هم با | جدا کنید:\n"
            "متن1 - لینک1 | متن2 - لینک2\n\n"
            "برای پایان end بفرستید."
        )

    elif state["step"] == "buttons":

        if not message.text:

            await message.reply_text(
                "لطفاً متن دکمه‌ها را ارسال کنید."
            )

            return

        if message.text.lower() == "end":

            pending_banner[user_id] = {
                "text": state["text"],
                "buttons": state["buttons"]
            }

            del button_states[user_id]

            await message.reply_text(
                "بنر آماده شد."
            )

            await show_channels(
                update,
                context
            )

            return

        parts = message.text.split("|")
        row = []

        for part in parts:

            part = part.strip()

            if " - " in part:

                btn_text, btn_url = part.split(
                    " - ",
                    1
                )

            elif "-" in part:

                btn_text, btn_url = part.split(
                    "-",
                    1
                )

            else:
                continue

            btn_text = btn_text.strip()
            btn_url = btn_url.strip()

            if not btn_text or not btn_url:
                continue

            row.append(
                InlineKeyboardButton(
                    btn_text,
                    url=btn_url
                )
            )

        if row:

            state["buttons"].append(row)

            await message.reply_text(
                f"دکمه اضافه شد. ({len(row)} دکمه)\n\n"
                "ادامه بده یا end بزن."
            )

        else:

            await message.reply_text(
                "فرمت دکمه صحیح نیست."
            )


# =========================================================
# CHANNELS
# =========================================================

async def show_channels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    rows = get_channels()

    if not rows:

        await update.message.reply_text(
            "ربات در هیچ کانالی ادمین نیست.\n\n"
            "ربات را در کانال مورد نظر ادمین کنید.\n"
            "سپس /addchannel [chat_id] [title] را بزنید."
        )

        return

    keyboard = []

    for chat_id, title in rows:

        keyboard.append([
            InlineKeyboardButton(
                title,
                callback_data=f"send_banner:{chat_id}"
            )
        ])

    await update.message.reply_text(
        "کانال‌هایی که ربات ادمین است:\n\n"
        "روی کانال بزنید تا بنر ارسال شود.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# ADD CHANNEL
# =========================================================

async def add_channel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if not context.args:

        await update.message.reply_text(
            "استفاده: /addchannel [chat_id] [title]"
        )

        return

    try:

        chat_id = int(
            context.args[0]
        )

        title = (
            " ".join(context.args[1:])
            if len(context.args) > 1
            else "کانال"
        )

    except ValueError:

        await update.message.reply_text(
            "chat_id نامعتبر است."
        )

        return

    save_channel(
        chat_id,
        title
    )

    await update.message.reply_text(
        f"کانال {title} با آیدی {chat_id} ذخیره شد."
    )


# =========================================================
# SEND BANNER
# =========================================================

async def send_banner_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    if query.from_user.id != ADMIN_USER_ID:
        return

    try:

        chat_id = int(
            query.data.split(":")[1]
        )

    except Exception:
        return

    banner = pending_banner.get(
        ADMIN_USER_ID
    )

    if not banner:

        await query.message.reply_text(
            "بنری یافت نشد. دوباره /button بزنید."
        )

        return

    keyboard = []

    for row in banner["buttons"]:
        keyboard.append(row)

    try:

        await context.bot.send_message(
            chat_id=chat_id,
            text=banner["text"],
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        await query.message.reply_text(
            "بنر ارسال شد."
        )

        del pending_banner[
            ADMIN_USER_ID
        ]

    except Exception as e:

        await query.message.reply_text(
            f"خطا در ارسال بنر: {str(e)}"
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.exception(
        "Unhandled error:",
        exc_info=context.error
    )


# =========================================================
# HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain"
        )

        self.end_headers()

        self.wfile.write(
            b"OK"
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_health_server(port):

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    server.serve_forever()


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set."
        )

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    health_thread = threading.Thread(
        target=start_health_server,
        args=(port,),
        daemon=True
    )

    health_thread.start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # /addchannel
    application.add_handler(
        CommandHandler(
            "addchannel",
            add_channel_command
        )
    )

    # دکمه‌های کاربر
    application.add_handler(
        CallbackQueryHandler(
            user_button,
            pattern=r"^(user_send|user_reply|user_help)$"
        )
    )

    # دکمه‌های مدیریت کاربر
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^(reply|block|unblock|deletechat|backup):"
        )
    )

    # پنل مدیریت
    application.add_handler(
        CallbackQueryHandler(
            panel_callback,
            pattern=r"^panel:"
        )
    )

    # ارسال بنر
    application.add_handler(
        CallbackQueryHandler(
            send_banner_callback,
            pattern=r"^send_banner:"
        )
    )

    # تشخیص ادمین شدن ربات در کانال/سوپرگروه
    application.add_handler(
        ChatMemberHandler(
            my_chat_member_handler,
            ChatMemberHandler.MY_CHAT_MEMBER
        )
    )

    # تمام پیام‌های ادمین
    application.add_handler(
        MessageHandler(
            filters.ALL
            & ~filters.COMMAND
            & filters.User(
                user_id=ADMIN_USER_ID
            ),
            handle_admin_message
        )
    )

    # تمام پیام‌های کاربران
    application.add_handler(
        MessageHandler(
            filters.ALL
            & ~filters.COMMAND
            & ~filters.User(
                user_id=ADMIN_USER_ID
            ),
            handle_user_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        f"Bot is running on port {port}..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
