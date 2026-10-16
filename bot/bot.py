"""
መልእክተኛው delivery - Telegram bot
Opens the order Mini App, receives the order, forwards it to the admin /
riders' group, and confirms to the student.
"""
import html
import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]            # https://your-site.vercel.app
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])  # your id or the riders' group id

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("meleektegna")

ORDER_BUTTON = "🛵 Order Now · ትዕዛዝ ይስጡ"

CATEGORY_ICON = {"Food": "🍽️", "Medicine": "💊", "Jar Water": "💧", "Other": "🛒"}


def e(value) -> str:
    """HTML-escape user text."""
    return html.escape(str(value))


def order_keyboard() -> ReplyKeyboardMarkup:
    # sendData() from the Mini App only works when opened from a *keyboard* button.
    return ReplyKeyboardMarkup(
        [[KeyboardButton(ORDER_BUTTON, web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 እንኳን ደህና መጡ! Welcome to <b>መልእክተኛው delivery</b> – ASTU Student Delivery Service.\n\n"
        "Food 🍽️ · Medicine 💊 · Jar Water 💧 · Other essentials 🛒\n\n"
        "Tap the button below to place your order 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=order_keyboard(),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Use this inside your riders' group to find the ADMIN_CHAT_ID."""
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user

    try:
        d = json.loads(msg.web_app_data.data)
        category = d["category"]
        name, phone = d["name"], d["phone"]
        block, dorm = d["block"], d["dorm"]
        is_jar = category == "Jar Water"
        if is_jar:
            jar = d["jar"]
            liters, price = int(jar["liters"]), int(jar["price"])
            count, total = int(jar["count"]), int(jar["total"])
        else:
            pickup, place, item = d["pickup"], d["place"], d["item"]
            quantity = int(d["quantity"])
    except (ValueError, KeyError, TypeError):
        log.exception("Bad order payload: %r", msg.web_app_data.data)
        await msg.reply_text("❌ Sorry, something went wrong with your order. Please try again.")
        return

    order_no = datetime.now().strftime("%d%H%M%S")
    username = f"@{user.username}" if user.username else "—"
    icon = CATEGORY_ICON.get(category, "📦")

    # ---------- details block (differs for jar vs. other categories) ----------
    if is_jar:
        details = (
            f"{icon} <b>Type:</b> Jar Water\n"
            f"🪣 <b>Size:</b> {liters} Liter\n"
            f"🔢 <b>Jars:</b> {count}\n"
            f"💰 <b>Price:</b> {price} Birr each → <b>{total} Birr</b>"
        )
        student_summary = f"💧 Jar Water {liters}L × {count}\n💰 Total: {total} Birr"
    else:
        details = (
            f"{icon} <b>Type:</b> {e(category)}\n"
            f"📍 <b>Pick up from:</b> {e(pickup)}\n"
            f"🏪 <b>Place:</b> {e(place)}\n"
            f"📝 <b>Item:</b> {e(item)}\n"
            f"🔢 <b>Quantity:</b> {quantity}"
        )
        student_summary = f"{icon} {e(item)} × {quantity}\n📍 {e(pickup)} · {e(place)}"

    # 1) message to admin / riders
    admin_text = (
        f"🆕 <b>New Order #{order_no}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{details}\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {e(name)}\n"
        f"🏠 <b>Block:</b> {e(block)}   <b>Dorm:</b> {e(dorm)}\n"
        f"📞 <b>Phone:</b> {e(phone)}\n"
        f"💬 <b>Telegram:</b> {e(user.full_name)} ({e(username)})"
    )
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ I'll take it", callback_data=f"take:{order_no}:{user.id}")]]
    )
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_text,
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )

    # 2) confirmation to the student
    await msg.reply_text(
        f"✅ <b>ትዕዛዝዎ ደርሶናል! Order received!</b>\n"
        f"Order #{order_no}\n\n"
        f"{student_summary}\n"
        f"🏠 Block {e(block)}, Dorm {e(dorm)}\n\n"
        f"We'll call you on {e(phone)} shortly. 🛵",
        parse_mode=ParseMode.HTML,
        reply_markup=order_keyboard(),
    )


async def rider_took(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A rider presses 'I'll take it' in the chat."""
    q = update.callback_query
    _, order_no, student_id = q.data.split(":")
    rider = q.from_user
    await q.answer("Order assigned to you ✅")

    new_text = q.message.text_html + f"\n\n🛵 <b>Taken by:</b> {e(rider.full_name)}"
    await q.edit_message_text(new_text, parse_mode=ParseMode.HTML)

    try:
        await context.bot.send_message(
            chat_id=int(student_id),
            text=f"🛵 Your order #{order_no} is on the way! "
                 f"Our rider <b>{e(rider.first_name)}</b> will contact you.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.warning("Could not notify student %s", student_id)


class HealthHandler(BaseHTTPRequestHandler):
    """Tiny web page so Render (web service) sees an open port and
    UptimeRobot can ping it to keep the bot awake."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Meleektegna delivery bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # keep the logs clean


def start_health_server():
    port = os.environ.get("PORT")  # Render sets this automatically
    if not port:
        return  # running locally, no need
    server = HTTPServer(("0.0.0.0", int(port)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


def main():
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    app.add_handler(CallbackQueryHandler(rider_took, pattern=r"^take:"))
    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()