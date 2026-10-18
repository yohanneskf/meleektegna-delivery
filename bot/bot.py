"""
መልእክተኛው delivery - Telegram bot

* /start shows a persistent "Order Now" keyboard button.
* The Telegram MENU BUTTON (next to the message box) is set to open the same
  order form, so students can order without ever sending /start.
* Orders arrive either
    - from the menu button  -> the form POSTs to this server's /order endpoint
      (verified with Telegram's signed initData), or
    - from the keyboard button -> Telegram's web_app_data message.
  Both end up as the same message for the admin / riders, plus a confirmation
  for the student.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
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

# Which website may call /order (CORS). Defaults to the host of WEBAPP_URL.
_u = urllib.parse.urlparse(WEBAPP_URL)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN") or f"{_u.scheme}://{_u.netloc}"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("meleektegna")

ORDER_BUTTON = "🛵 Order Now · ትዕዛዝ ይስጡ"
MENU_BUTTON_TEXT = "Order Now"

CATEGORY_ICON = {"Food": "🍽️", "Medicine": "💊", "Jar Water": "💧", "Other": "🛒"}

# =============================================================================
# PRICES (in Birr). The bot decides the prices; the form's numbers are ignored.
# Change them here AND in webapp/index.html so both show the same thing.
# =============================================================================
JAR_LITERS = 20
JAR_PRICE = 250
DELIVERY_FEES = {"bole": 50, "geda": 60}      # any other place -> you contact the student
FEE_CATEGORIES = {"Food", "Medicine"}         # categories that get a delivery fee
EXTRA_ITEM_FEE = 20                           # Food only: each additional food adds this
                                              # (3 foods from Geda = 60 + 20 + 20 = 100)
                                              # Medicine = one fixed fee, however many
MAX_ITEMS = 10


def e(value) -> str:
    """HTML-escape user text."""
    return html.escape(str(value))


# ----------------------------------------------------------------------------
# Order parsing / formatting (shared by both ways an order can arrive)
# ----------------------------------------------------------------------------
def parse_order(d: dict) -> dict:
    """Validate the order JSON sent by the Mini App. Raises ValueError if bad."""
    try:
        category = str(d["category"])
        if category not in CATEGORY_ICON:
            raise ValueError("unknown category")
        o = {
            "category": category,
            "name": str(d["name"]).strip()[:80],
            "phone": str(d["phone"]).strip()[:20],
            "block": str(d["block"]).strip()[:20],
            "dorm": str(d["dorm"]).strip()[:20],
        }
        if not all(o[k] for k in ("name", "phone", "block", "dorm")):
            raise ValueError("empty field")

        if category == "Jar Water":
            count = int(d["jar"]["count"])
            if not 1 <= count <= 100:
                raise ValueError("bad jar count")
            # only 20 L jars, at our price (ignore any size/price sent by the form)
            o.update(liters=JAR_LITERS, price=JAR_PRICE, count=count, total=JAR_PRICE * count)
        else:
            pickup = str(d["pickup"]).strip()[:80]
            if not pickup:
                raise ValueError("no pickup place")
            o["pickup"] = pickup

            # Fee: only when the student tapped Bole / Geda (not the "Other" box)
            choice = str(d.get("pickupChoice") or "").strip().lower()
            key = None if choice == "other" else pickup.lower()
            each = DELIVERY_FEES.get(key) if (key and category in FEE_CATEGORIES) else None

            if category in ("Food", "Medicine"):
                raw = d.get("items")
                if not raw and d.get("item"):  # older cached form: one item only
                    raw = [{"name": d["item"], "quantity": d.get("quantity", 1)}]
                items = []
                for it in list(raw or [])[:MAX_ITEMS]:
                    nm, qty = str(it["name"]).strip()[:100], int(it["quantity"])
                    if not nm or not 1 <= qty <= 99:
                        raise ValueError("bad item")
                    items.append({"name": nm, "quantity": qty, "place": str(it.get("place") or "").strip()[:100]})
                if not items:
                    raise ValueError("no items")

                if category == "Food":
                    # ONE place for the whole order. (An older cached form put a place on every
                    # food: accept it only if they are all the same place.)
                    places = {m["place"].lower(): m["place"] for m in items if m["place"]}
                    place = str(d.get("place") or "").strip()[:100]
                    if not place and len(places) == 1:
                        place = next(iter(places.values()))
                    if not place:
                        raise ValueError("food needs a place")
                    if any(k != place.lower() for k in places):
                        raise ValueError("foods must all come from one place")
                    o["place"] = place
                    o["items"] = [{"name": m["name"], "quantity": m["quantity"]} for m in items]
                    # base fee for the first food + EXTRA_ITEM_FEE for every additional one
                    o["portions"] = sum(m["quantity"] for m in items)
                    fee = each + EXTRA_ITEM_FEE * (o["portions"] - 1) if each else None
                else:
                    o["items"] = [{"name": m["name"], "quantity": m["quantity"]} for m in items]
                    fee = each  # Medicine: ONE fixed fee, however many medicines
            else:
                # "Other" essentials: one item, one place, one quantity
                o.update(
                    place=str(d["place"]).strip()[:100],
                    item=str(d["item"]).strip()[:200],
                    quantity=int(d["quantity"]),
                )
                if not (o["place"] and o["item"] and 1 <= o["quantity"] <= 999):
                    raise ValueError("bad order values")
                fee = each

            o["fee_each"], o["fee"] = each, fee
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed order: {exc!r}") from exc
    return o


def render_order(o: dict, user_id: int, full_name: str, username: str | None):
    """Return (order_no, admin_text, student_text)."""
    order_no = datetime.now().strftime("%d%H%M%S")
    handle = f"@{username}" if username else "—"
    category = o["category"]
    icon = CATEGORY_ICON.get(category, "📦")

    if category == "Jar Water":
        details = (
            f"{icon} <b>Type:</b> Jar Water\n"
            f"🪣 <b>Size:</b> {o['liters']} Liter\n"
            f"🔢 <b>Jars:</b> {o['count']}\n"
            f"💰 <b>Price:</b> {o['price']} Birr each → <b>{o['total']} Birr</b>"
        )
        student_summary = (
            f"💧 Jar Water {o['liters']}L × {o['count']}\n💰 Total: {o['total']} Birr"
        )
    else:
        fee, each = o.get("fee"), o.get("fee_each")
        if fee and category == "Food" and o["portions"] > 1:
            fee_text = f"{each} + {o['portions'] - 1} × {EXTRA_ITEM_FEE} = <b>{fee} Birr</b>"
        elif fee:
            fee_text = f"<b>{fee} Birr</b>"
            if category == "Medicine" and len(o["items"]) > 1:
                fee_text += " (one fee for all medicines)"
        else:
            fee_text = None
        admin_fee = (
            f"🚚 <b>Delivery fee:</b> {fee_text}"
            if fee_text else "🚚 <b>Delivery fee:</b> ⚠️ not set – contact the student"
        )
        student_fee = (
            "🚚 Delivery fee: " + fee_text.replace("<b>", "").replace("</b>", "")
            if fee_text else "🚚 Delivery fee: we'll call you to confirm it"
        )

        if category == "Medicine":
            what = "💊 <b>Medicines:</b>\n" + "\n".join(
                f"   {i}. {e(m['name'])} × {m['quantity']}" for i, m in enumerate(o["items"], 1)
            )
            student_what = "\n".join(f"💊 {e(m['name'])} × {m['quantity']}" for m in o["items"])
            student_where = f"📍 {e(o['pickup'])}"
        elif category == "Food":
            what = f"🏪 <b>Place:</b> {e(o['place'])}\n🍽️ <b>Foods:</b>\n" + "\n".join(
                f"   {i}. {e(m['name'])} × {m['quantity']}" for i, m in enumerate(o["items"], 1)
            )
            student_what = "\n".join(f"🍽️ {e(m['name'])} × {m['quantity']}" for m in o["items"])
            student_where = f"📍 {e(o['pickup'])} · {e(o['place'])}"
        else:
            what = (
                f"🏪 <b>Place:</b> {e(o['place'])}\n"
                f"📝 <b>Item:</b> {e(o['item'])}\n"
                f"🔢 <b>Quantity:</b> {o['quantity']}"
            )
            student_what = f"{icon} {e(o['item'])} × {o['quantity']}"
            student_where = f"📍 {e(o['pickup'])} · {e(o['place'])}"

        details = (
            f"{icon} <b>Type:</b> {e(category)}\n"
            f"📍 <b>Pick up from:</b> {e(o['pickup'])}\n"
            f"{what}\n"
            f"{admin_fee}"
        )
        student_summary = f"{student_what}\n{student_where}\n{student_fee}"

    admin_text = (
        f"🆕 <b>New Order #{order_no}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{details}\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {e(o['name'])}\n"
        f"🏠 <b>Block:</b> {e(o['block'])}   <b>Dorm:</b> {e(o['dorm'])}\n"
        f"📞 <b>Phone:</b> {e(o['phone'])}\n"
        f"💬 <b>Telegram:</b> {e(full_name)} ({e(handle)})"
    )
    student_text = (
        f"✅ <b>ትዕዛዝዎ ደርሶናል! Order received!</b>\n"
        f"Order #{order_no}\n\n"
        f"{student_summary}\n"
        f"🏠 Block {e(o['block'])}, Dorm {e(o['dorm'])}\n\n"
        f"We'll call you on {e(o['phone'])} shortly. 🛵"
    )
    return order_no, admin_text, student_text


def take_button(order_no: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ I'll take it", callback_data=f"take:{order_no}:{user_id}")]]
    )


def order_keyboard() -> ReplyKeyboardMarkup:
    # Persistent keyboard button: stays visible under the chat after /start.
    return ReplyKeyboardMarkup(
        [[KeyboardButton(ORDER_BUTTON, web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ----------------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 እንኳን ደህና መጡ! Welcome to <b>መልእክተኛው delivery</b> – ASTU Student Delivery Service.\n\n"
        "Food 🍽️ · Medicine 💊 · Jar Water 💧 · Other essentials 🛒\n\n"
        "Tap <b>Order Now</b> below 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=order_keyboard(),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Use this inside your riders' group to find the ADMIN_CHAT_ID."""
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Order that arrives through the keyboard button (web_app_data)."""
    msg = update.effective_message
    user = update.effective_user
    try:
        o = parse_order(json.loads(msg.web_app_data.data))
    except ValueError:
        log.exception("Bad order payload: %r", msg.web_app_data.data)
        await msg.reply_text("❌ Sorry, something went wrong with your order. Please try again.")
        return

    order_no, admin_text, student_text = render_order(
        o, user.id, user.full_name, user.username
    )
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_text,
        parse_mode=ParseMode.HTML,
        reply_markup=take_button(order_no, user.id),
    )
    await msg.reply_text(
        student_text, parse_mode=ParseMode.HTML, reply_markup=order_keyboard()
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


async def post_init(app: Application):
    """Runs once at startup: make 'Order Now' the permanent menu button."""
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
        log.info("Menu button set to the order form")
    except Exception:
        log.exception("Could not set the menu button")


# ----------------------------------------------------------------------------
# Small web server: health page (for UptimeRobot) + POST /order (menu button)
# ----------------------------------------------------------------------------
def tg_api(method: str, payload: dict) -> dict:
    """Call the Telegram Bot API directly (used by the web server thread)."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def validate_init_data(init_data: str, max_age: int = 86400) -> dict:
    """Check Telegram's signature on initData and return the user dict."""
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise ValueError("no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("bad signature")
    if time.time() - int(pairs.get("auth_date", "0")) > max_age:
        raise ValueError("expired")
    return json.loads(pairs["user"])


def send_order_via_api(o: dict, user: dict):
    full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    order_no, admin_text, student_text = render_order(
        o, user["id"], full_name or "—", user.get("username")
    )
    tg_api("sendMessage", {
        "chat_id": ADMIN_CHAT_ID,
        "text": admin_text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ I'll take it", "callback_data": f"take:{order_no}:{user['id']}"}
        ]]},
    })
    try:
        tg_api("sendMessage", {
            "chat_id": user["id"], "text": student_text, "parse_mode": "HTML"
        })
    except Exception:
        log.warning("Could not send confirmation to student %s", user["id"])


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")

    def _json(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    # health page
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Meleektegna delivery bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path.split("?")[0] != "/order":
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 20000:
                return self._json(413, {"ok": False, "error": "bad size"})
            body = json.loads(self.rfile.read(length))
            user = validate_init_data(body["initData"])
            order = parse_order(body["order"])
        except (ValueError, KeyError, TypeError):
            log.warning("Rejected /order request", exc_info=True)
            return self._json(400, {"ok": False, "error": "invalid request"})
        try:
            send_order_via_api(order, user)
        except Exception:
            log.exception("Could not deliver order to admin")
            return self._json(502, {"ok": False, "error": "could not deliver"})
        self._json(200, {"ok": True})

    def log_message(self, *args):
        pass  # keep the logs clean


def start_web_server():
    port = os.environ.get("PORT")  # Render sets this automatically
    if not port:
        return  # running locally, no need
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web server listening on port %s", port)


def main():
    start_web_server()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    app.add_handler(CallbackQueryHandler(rider_took, pattern=r"^take:"))
    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()