import os
import re
import html
import time
import sqlite3
import logging
import psutil
import asyncio
import io
import contextlib
import traceback
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = 190503955

SPAM_MESSAGE_LIMIT = 8
SPAM_WINDOW_SECONDS = 5
SPAM_COOLDOWN_SECONDS = 5

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "bot_data.db"
db = sqlite3.connect(DB_FILE, check_same_thread=False)
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
db.commit()

user_message_times = defaultdict(deque)
reply_targets = {}
broadcast_mode = set()

BAD_WORDS = {
    "کص ننت", "کصت ننت", "کص کش", "کصکش", "مادر جنده", "مادرجنده", "مادر قحبه", "مادرقحبه",
    "مادر کصته", "مادرکصته", "ننه جنده", "ننه قحبه", "ننه سگ", "ننه سگه", "ننه کصه",
    "خواهر جنده", "خواهرجنده", "جنده", "قحبه", "مادرت", "ننت", "خواهرت",
    "mother fucker", "motherfucker", "son of a bitch", "son of bitch",
}

def contains_profanity(text: str) -> bool:
    text = text.lower()
    normalized = re.sub(r"[\u200c\u200d]", " ", text)
    for word in BAD_WORDS:
        if word in normalized:
            return True
    return False

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def save_user(user):
    if not user:
        return
    timestamp = now()
    db.execute("""
    INSERT INTO users (user_id, first_name, last_name, username, joined_at, last_seen)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        first_name=excluded.first_name,
        last_name=excluded.last_name,
        username=excluded.username,
        last_seen=excluded.last_seen
    """, (user.id, user.first_name or "", user.last_name or "", user.username or "", timestamp, timestamp))
    db.commit()

def is_blocked(user_id):
    row = db.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def block_user(user_id):
    db.execute("INSERT OR IGNORE INTO blocked_users(user_id, blocked_at) VALUES (?, ?)", (user_id, now()))
    db.commit()

def unblock_user(user_id):
    db.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
    db.commit()

def save_message(user_id, direction, text):
    db.execute("INSERT INTO messages(user_id, direction, text, created_at) VALUES (?, ?, ?, ?)", (user_id, direction, text or "", now()))
    db.commit()

def user_display_name(user):
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    return name or "کاربر"

def user_mention(user):
    name = html.escape(user_display_name(user))
    return f'<a href="tg://user?id={user.id}">{name}</a>'

def user_profile_button(user_id):
    return InlineKeyboardButton("پروفایل کاربر", url=f"tg://user?id={user_id}")

def user_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ارسال پیام", callback_data="user_send"), InlineKeyboardButton("راهنما", callback_data="user_help")]
    ])

def user_reply_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("پاسخ", callback_data="user_reply")]
    ])

def admin_message_keyboard(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("پاسخ به کاربر", callback_data=f"reply:{user_id}")],
        [InlineKeyboardButton("بلاک کردن", callback_data=f"block:{user_id}"), InlineKeyboardButton("آنبلاک", callback_data=f"unblock:{user_id}")],
        [InlineKeyboardButton("حذف چت", callback_data=f"deletechat:{user_id}"), InlineKeyboardButton("بکاپ چت", callback_data=f"backup:{user_id}")],
        [user_profile_button(user_id)]
    ])

log_buffer = deque(maxlen=100)

class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_buffer.append(msg)
        except Exception:
            pass

buffer_handler = BufferHandler()
buffer_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(buffer_handler)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    save_user(user)
    if is_blocked(user.id):
        await update.message.reply_text("شما بلاک شده‌اید.")
        return
    await update.message.reply_text(f"سلام {html.escape(user_display_name(user))}\n\nپیامت رو برای پشتیبانی ارسال کن.", reply_markup=user_keyboard())

async def user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if is_blocked(user_id):
        return
    if query.data == "user_send":
        context.user_data["waiting_for_user_message"] = True
        await query.message.reply_text("پیامت رو ارسال کن")
    elif query.data == "user_reply":
        context.user_data["waiting_for_user_message"] = True
        await query.message.reply_text("پاسخت رو ارسال کن")
    elif query.data == "user_help":
        await query.message.reply_text("راهنما\n\nپیامت رو ارسال کن تا برای مدیریت ارسال بشه.")

def check_rate_limit(user_id):
    current = time.monotonic()
    times = user_message_times[user_id]
    while times and current - times[0] > SPAM_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= SPAM_MESSAGE_LIMIT:
        return False
    times.append(current)
    return True

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    if not user or not message:
        return
    save_user(user)
    if is_blocked(user.id):
        try:
            await message.delete()
        except Exception:
            pass
        return
    if not check_rate_limit(user.id):
        await message.reply_text("لطفاً 5 ثانیه دیگر دوباره تلاش کنید.")
        return
    text = message.text or ""
    if contains_profanity(text):
        await message.reply_text("پیام شما به دلیل توهین ارسال نشد.")
        return
    save_message(user.id, "user", text)
    mention = user_mention(user)
    received_time = now()
    admin_text = f"پیام جدید از کاربر\n\n{mention}\nزمان دریافت: {received_time}\n\n{html.escape(text)}"
    try:
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=admin_text, parse_mode=ParseMode.HTML, reply_markup=admin_message_keyboard(user.id))
        await update.message.reply_text("پیام شما دریافت شد. منتظر پاسخ باشید.")
    except Exception as e:
        logger.exception("Could not send user message to admin: %s", e)
        await update.message.reply_text("خطا در ارسال پیام. لطفاً دوباره تلاش کنید.")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("دسترسی ندارید.", show_alert=True)
        return
    await query.answer()
    data = query.data
    try:
        action, raw_user_id = data.split(":", 1)
        user_id = int(raw_user_id)
    except Exception:
        return
    if action == "reply":
        reply_targets[ADMIN_USER_ID] = user_id
        await query.message.reply_text("پاسخ به کاربر را ارسال کنید.")
    elif action == "block":
        block_user(user_id)
        await query.message.reply_text("کاربر بلاک شد.")
        try:
            await context.bot.send_message(chat_id=user_id, text="شما بلاک شدید.")
        except Exception:
            pass
    elif action == "unblock":
        unblock_user(user_id)
        await query.message.reply_text("کاربر آنبلاک شد.")
    elif action == "deletechat":
        db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        db.commit()
        await query.message.reply_text("اطلاعات ذخیره‌شده چت حذف شد.")
    elif action == "backup":
        rows = db.execute("SELECT direction, text, created_at FROM messages WHERE user_id=? ORDER BY id ASC", (user_id,)).fetchall()
        if not rows:
            await query.message.reply_text("برای این کاربر چتی ذخیره نشده.")
            return
        filename = f"chat_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"Backup for user {user_id}\n")
            f.write("=" * 50 + "\n\n")
            for direction, text, created_at in rows:
                sender = "USER" if direction == "user" else "ADMIN"
                f.write(f"[{created_at}] {sender}:\n{text}\n\n")
        try:
            with open(filename, "rb") as file:
                await context.bot.send_document(chat_id=ADMIN_USER_ID, document=file, caption=f"بکاپ چت کاربر {user_id}")
        finally:
            try:
                os.remove(filename)
            except Exception:
                pass

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    start_time = time.perf_counter()
    sent_message = await update.message.reply_text("محاسبه پینگ...")
    end_time = time.perf_counter()
    ping_ms = (end_time - start_time) * 1000
    await sent_message.delete()
    await update.message.reply_text(f"پینگ: <code>{ping_ms:.2f}ms</code>", parse_mode=ParseMode.HTML)

async def cpu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_count = psutil.cpu_count()
    try:
        cpu_freq = psutil.cpu_freq()
        freq_text = f"{cpu_freq.current:.0f}MHz" if cpu_freq else "نامشخص"
    except:
        freq_text = "نامشخص"
    memory = psutil.virtual_memory()
    memory_percent = memory.percent
    disk = psutil.disk_usage('/')
    disk_percent = disk.percent
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot_time
    uptime_hours = uptime.total_seconds() / 3600
    text = f"وضعیت سرور\n\nCPU: <code>{cpu_percent}%</code>\nهسته‌ها: <code>{cpu_count}</code>\nفرکانس: <code>{freq_text}</code>\n\nRAM: <code>{memory_percent}%</code>\nدیسک: <code>{disk_percent}%</code>\n\nآپتایم: <code>{uptime_hours:.1f} ساعت</code>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if not log_buffer:
        await update.message.reply_text("لاگی ثبت نشده.")
        return
    lines = list(log_buffer)[-10:]
    log_text = "\n".join(lines)
    await update.message.reply_text(f"```\n{log_text}\n```", parse_mode=ParseMode.MARKDOWN_V2)

async def remote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("روی پیام حاوی کد ریپلای کن و /remote بزن.")
        return
    replied = update.message.reply_to_message
    code = replied.text or ""
    if not code:
        await update.message.reply_text("پیام ریپلای شده متن ندارد.")
        return
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\n?", "", code)
        code = re.sub(r"\n?```$", "", code)
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec_globals = {
                'bot': context.bot,
                'update': update,
                'context': context,
                'db': db,
                'ADMIN_USER_ID': ADMIN_USER_ID,
                'psutil': psutil,
                'time': time,
                'datetime': datetime,
                'asyncio': asyncio,
                'os': os,
                'html': html,
                're': re,
                'logger': logger,
                'log_buffer': log_buffer,
            }
            start_time = time.perf_counter()
            exec(code, exec_globals)
            end_time = time.perf_counter()
        output = stdout.getvalue()
        error_output = stderr.getvalue()
        execution_time = (end_time - start_time) * 1000
        if output:
            result_text = f"خروجی:\n```\n{output[:3000]}\n```\nزمان اجرا: {execution_time:.2f}ms"
        elif error_output:
            result_text = f"خطا:\n```\n{error_output[:3000]}\n```"
        else:
            result_text = f"کد اجرا شد.\nزمان اجرا: {execution_time:.2f}ms"
    except Exception as e:
        error_details = traceback.format_exc()
        result_text = f"خطا در اجرا:\n```\n{str(e)}\n\n{error_details[:2000]}\n```"
    await update.message.reply_text(result_text)

async def remote_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if update.message.document:
        document = update.message.document
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
        code = file_bytes.decode('utf-8')
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exec_globals = {
                    'bot': context.bot,
                    'update': update,
                    'context': context,
                    'db': db,
                    'ADMIN_USER_ID': ADMIN_USER_ID,
                    'psutil': psutil,
                    'time': time,
                    'datetime': datetime,
                    'asyncio': asyncio,
                    'os': os,
                    'html': html,
                    're': re,
                }
                exec(code, exec_globals)
            output = stdout.getvalue()
            if output:
                result_text = f"خروجی:\n```\n{output[:3000]}\n```"
            else:
                result_text = "فایل اجرا شد."
        except Exception as e:
            result_text = f"خطا:\n```\n{str(e)}\n```"
        await update.message.reply_text(result_text)

def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("کاربران مسدود شده", callback_data="panel:blocked")],
        [InlineKeyboardButton("ارسال همگانی", callback_data="panel:broadcast")],
        [InlineKeyboardButton("لیست کاربران", callback_data="panel:users")],
        [InlineKeyboardButton("پینگ", callback_data="panel:ping")],
        [InlineKeyboardButton("CPU", callback_data="panel:cpu")],
        [InlineKeyboardButton("لاگ", callback_data="panel:log")],
        [InlineKeyboardButton("اجرای کد", callback_data="panel:remote")],
    ])

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text("پنل مدیریت\n\nیکی از گزینه‌ها را انتخاب کنید:", parse_mode=ParseMode.HTML, reply_markup=admin_panel_keyboard())

async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("دسترسی ندارید.", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "blocked":
        rows = db.execute("SELECT user_id, blocked_at FROM blocked_users ORDER BY blocked_at DESC").fetchall()
        if not rows:
            await query.message.reply_text("هیچ کاربری بلاک نیست.")
            return
        text = "کاربران مسدود شده\n\n"
        for user_id, blocked_at in rows:
            text += f"{user_id}\n{blocked_at}\n\n"
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)
    elif action == "users":
        row = db.execute("SELECT COUNT(*) FROM users").fetchone()
        count = row[0]
        await query.message.reply_text(f"تعداد کاربران ثبت‌شده: <b>{count}</b>", parse_mode=ParseMode.HTML)
    elif action == "broadcast":
        broadcast_mode.add(ADMIN_USER_ID)
        await query.message.reply_text("متن پیام همگانی را ارسال کنید.\n\nبرای لغو، /cancel را بفرستید.")
    elif action == "ping":
        start_time = time.perf_counter()
        sent_message = await query.message.reply_text("محاسبه پینگ...")
        end_time = time.perf_counter()
        ping_ms = (end_time - start_time) * 1000
        await sent_message.delete()
        await query.message.reply_text(f"پینگ: <code>{ping_ms:.2f}ms</code>", parse_mode=ParseMode.HTML)
    elif action == "cpu":
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        try:
            cpu_freq = psutil.cpu_freq()
            freq_text = f"{cpu_freq.current:.0f}MHz" if cpu_freq else "نامشخص"
        except:
            freq_text = "نامشخص"
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        uptime_hours = uptime.total_seconds() / 3600
        text = f"وضعیت سرور\n\nCPU: <code>{cpu_percent}%</code>\nهسته‌ها: <code>{cpu_count}</code>\nفرکانس: <code>{freq_text}</code>\n\nRAM: <code>{memory_percent}%</code>\nدیسک: <code>{disk_percent}%</code>\n\nآپتایم: <code>{uptime_hours:.1f} ساعت</code>"
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)
    elif action == "log":
        if not log_buffer:
            await query.message.reply_text("لاگی ثبت نشده.")
            return
        lines = list(log_buffer)[-10:]
        log_text = "\n".join(lines)
        await query.message.reply_text(f"```\n{log_text}\n```", parse_mode=ParseMode.MARKDOWN_V2)
    elif action == "remote":
        await query.message.reply_text("کد پایتون را بفرست و روی آن /remote را ریپلای کن.")

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    text = update.message.text or ""
    if text.lower() == "help":
        await admin_help(update, context)
        return
    if text == "/cancel":
        broadcast_mode.discard(ADMIN_USER_ID)
        reply_targets.pop(ADMIN_USER_ID, None)
        await update.message.reply_text("لغو شد.")
        return
    if ADMIN_USER_ID in broadcast_mode:
        broadcast_mode.discard(ADMIN_USER_ID)
        users = db.execute("SELECT user_id FROM users").fetchall()
        sent = 0
        for (user_id,) in users:
            if is_blocked(user_id):
                continue
            try:
                await context.bot.send_message(chat_id=user_id, text=text)
                sent += 1
            except Exception as e:
                logger.warning("Broadcast failed for %s: %s", user_id, e)
        await update.message.reply_text(f"ارسال همگانی انجام شد.\nتعداد ارسال موفق: {sent}")
        return
    if ADMIN_USER_ID in reply_targets:
        target_user_id = reply_targets[ADMIN_USER_ID]
        if is_blocked(target_user_id):
            await update.message.reply_text("این کاربر بلاک است.")
            reply_targets.pop(ADMIN_USER_ID, None)
            return
        try:
            await context.bot.send_message(chat_id=target_user_id, text=f"پاسخ شما:\n\n{html.escape(text)}", parse_mode=ParseMode.HTML, reply_markup=user_reply_keyboard())
            save_message(target_user_id, "admin", text)
            await update.message.reply_text("پاسخ ارسال شد.")
            reply_targets.pop(ADMIN_USER_ID, None)
        except Exception as e:
            logger.exception("Reply failed: %s", e)
            await update.message.reply_text("ارسال پاسخ انجام نشد.")
        return
    await update.message.reply_text("برای مدیریت، help را ارسال کنید.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error:", exc_info=context.error)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def main():
    port = int(os.environ.get("PORT", "10000"))

    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("cpu", cpu_command))
    application.add_handler(CommandHandler("log", log_command))
    application.add_handler(CommandHandler("remote", remote_command))

    application.add_handler(CallbackQueryHandler(user_button, pattern=r"^(user_send|user_reply|user_help)$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(reply|block|unblock|deletechat|backup):"))
    application.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel:"))

    application.add_handler(MessageHandler(filters.Document.FileExtension("py") & filters.User(user_id=ADMIN_USER_ID), remote_file_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(user_id=ADMIN_USER_ID), handle_admin_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.User(user_id=ADMIN_USER_ID), handle_user_message))

    application.add_error_handler(error_handler)

    print(f"Bot is running on port {port}...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
