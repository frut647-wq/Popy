import os
import time
import logging
import sqlite3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("8656231038:AAH5V-bSv_kPGPGfHNPfx5ckUA6rHpRGMq4", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "carfactory.db")

START_COINS = 1000

# ------------------------------------------------------------------
# کاتالوگ ماشین‌ها/کارخونه‌ها به تفکیک دسته
# seconds رو می‌تونی برای تست کمتر کنی
# ------------------------------------------------------------------
CAR_CATALOG = {
    # دسته ایرانی
    "پراید":   {"category": "ایرانی", "emoji": "🚗", "factory_cost": 500,   "seconds": 20 * 60,  "sell": 800},
    "206":     {"category": "ایرانی", "emoji": "🚗", "factory_cost": 800,   "seconds": 30 * 60,  "sell": 1300},
    "سمند":    {"category": "ایرانی", "emoji": "🚙", "factory_cost": 1200,  "seconds": 40 * 60,  "sell": 1900},

    # دسته خارجی
    "تویوتا_کمری":   {"category": "خارجی", "emoji": "🚘", "factory_cost": 5000,  "seconds": 3 * 3600,  "sell": 8000},
    "هیوندای_سوناتا": {"category": "خارجی", "emoji": "🚘", "factory_cost": 6500,  "seconds": 4 * 3600,  "sell": 10500},
    "بی_ام_و":       {"category": "خارجی", "emoji": "🏎️", "factory_cost": 9000,  "seconds": 5 * 3600,  "sell": 14500},

    # دسته کمیاب
    "فراری":       {"category": "کمیاب", "emoji": "🏎️", "factory_cost": 30000, "seconds": 5 * 3600,  "sell": 50000},
    "لامبورگینی":  {"category": "کمیاب", "emoji": "🏎️", "factory_cost": 42000, "seconds": 6 * 3600,  "sell": 70000},

    # دسته هواپیما (سطح آخر)
    "بوئینگ_737":  {"category": "هواپیما", "emoji": "✈️", "factory_cost": 150000, "seconds": 10 * 3600, "sell": 250000},
}

CATEGORIES = ["ایرانی", "خارجی", "کمیاب", "هواپیما"]
# برای رفتن سراغ هر دسته باید حداقل یه کارخونه از دسته قبلی داشته باشی
CATEGORY_REQUIREMENT = {
    "ایرانی": None,
    "خارجی": "ایرانی",
    "کمیاب": "خارجی",
    "هواپیما": "کمیاب",
}

UPGRADE_BASE_COST_RATIO = 0.5   # هزینه ارتقا = factory_cost * سطح فعلی * این ضریب
SPEED_BONUS_PER_LEVEL = 0.15    # هر سطح ۱۵٪ سریع‌تر
SELL_BONUS_PER_LEVEL = 0.20     # هر سطح ۲۰٪ گرون‌تر فروخته میشه


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            coins INTEGER DEFAULT 1000
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS factories (
            user_id INTEGER,
            car_key TEXT,
            level INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, car_key)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS production_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            car_key TEXT,
            ready_at REAL,
            collected INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def ensure_player(user_id: int, username: str):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM players WHERE user_id=?", (user_id,))
    if not c.fetchone():
        c.execute(
            "INSERT INTO players (user_id, username, coins) VALUES (?, ?, ?)",
            (user_id, username or str(user_id), START_COINS),
        )
    else:
        c.execute("UPDATE players SET username=? WHERE user_id=?", (username or str(user_id), user_id))
    conn.commit()
    conn.close()


def get_player(user_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_owned_factories(user_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT car_key, level FROM factories WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return {r["car_key"]: r["level"] for r in rows}


def owns_category(user_id: int, category: str) -> bool:
    owned = get_owned_factories(user_id)
    return any(CAR_CATALOG[k]["category"] == category for k in owned)


def effective_seconds(car_key: str, level: int) -> int:
    base = CAR_CATALOG[car_key]["seconds"]
    return int(base / (1 + SPEED_BONUS_PER_LEVEL * (level - 1)))


def effective_sell_price(car_key: str, level: int) -> int:
    base = CAR_CATALOG[car_key]["sell"]
    return int(base * (1 + SELL_BONUS_PER_LEVEL * (level - 1)))


async def safe_answer(query, **kwargs):
    try:
        await query.answer(**kwargs)
    except Exception:
        pass


def fmt_countdown(seconds_left: float) -> str:
    seconds_left = max(0, int(seconds_left))
    h = seconds_left // 3600
    m = (seconds_left % 3600) // 60
    s = seconds_left % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ------------------------------------------------------------------
# پنل شاپ (خرید کارخونه جدید)
# ------------------------------------------------------------------
def build_shop_categories_keyboard():
    buttons = [[InlineKeyboardButton(cat, callback_data=f"shopcat:{cat}")] for cat in CATEGORIES]
    return InlineKeyboardMarkup(buttons)


def build_shop_category_view(user_id: int, category: str):
    req = CATEGORY_REQUIREMENT[category]
    if req and not owns_category(user_id, req):
        return f"🔒 برای باز شدن دسته «{category}» باید اول حداقل یه کارخونه دسته «{req}» داشته باشی.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data="shop:back")]]
        )
    owned = get_owned_factories(user_id)
    lines = [f"🏭 کارخونه‌های دسته «{category}»", ""]
    buttons = []
    for key, info in CAR_CATALOG.items():
        if info["category"] != category:
            continue
        label = key.replace("_", " ")
        if key in owned:
            lines.append(f"✅ {info['emoji']} {label} — قبلاً خریدی (سطح {owned[key]})")
        else:
            hours = info["seconds"] / 3600
            lines.append(
                f"{info['emoji']} {label} — هزینه {info['factory_cost']} سکه | زمان ساخت ~{hours:.1f} ساعت | فروش {info['sell']}"
            )
            buttons.append([InlineKeyboardButton(f"خرید کارخونه {label}", callback_data=f"buyfactory:{key}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="shop:back")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ------------------------------------------------------------------
# پنل کارخونه‌های خودم (تولید / دریافت و فروش / ارتقا)
# ------------------------------------------------------------------
def build_my_factories_view(user_id: int):
    owned = get_owned_factories(user_id)
    if not owned:
        return "هنوز هیچ کارخونه‌ای نخریدی. برو تو «شاپ» یکی بخر.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛒 برو شاپ", callback_data="shop:open")]]
        )

    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM production_jobs WHERE user_id=? AND collected=0", (user_id,))
    active_jobs = c.fetchall()
    conn.close()
    active_by_car = {j["car_key"]: j for j in active_jobs}

    now = time.time()
    lines = ["🏭 کارخونه‌های من", ""]
    buttons = []

    for key, level in owned.items():
        info = CAR_CATALOG[key]
        label = key.replace("_", " ")
        job = active_by_car.get(key)
        sell_price = effective_sell_price(key, level)
        if job:
            left = job["ready_at"] - now
            if left <= 0:
                lines.append(f"✅ {info['emoji']} {label} (سطح {level}) آماده‌ست! فروش: {sell_price} سکه")
                buttons.append([InlineKeyboardButton(f"دریافت و فروش {label}", callback_data=f"collect:{job['id']}")])
            else:
                lines.append(f"⏰ {fmt_countdown(left)} <-- {info['emoji']} {label} (سطح {level})")
        else:
            lines.append(f"{info['emoji']} {label} (سطح {level}) — آماده شروع تولید")
            buttons.append([InlineKeyboardButton(f"شروع تولید {label}", callback_data=f"produce:{key}")])
        upgrade_cost = int(info["factory_cost"] * level * UPGRADE_BASE_COST_RATIO)
        buttons.append([InlineKeyboardButton(f"⬆️ ارتقای {label} ({upgrade_cost} سکه)", callback_data=f"upgrade:{key}")])

    buttons.append([InlineKeyboardButton("🛒 برو شاپ", callback_data="shop:open")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ------------------------------------------------------------------
# دستورات متنی
# ------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_player(user.id, user.username or user.first_name)
    await update.effective_message.reply_text(
        f"🚗 به بازی کارخونه ماشین‌سازی خوش اومدی!\n\n"
        f"شما در ابتدا {START_COINS} سکه دارید.\n"
        "دستورات:\n"
        "شاپ - خرید کارخونه جدید\n"
        "کارخونه - تولید، دریافت و فروش، ارتقا\n"
        "موجودی - سکه فعلیت\n"
        "راهنما - توضیح کامل بازی"
    )


async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_player(user.id, user.username or user.first_name)
    await update.effective_message.reply_text(
        "🛒 شاپ — کدوم دسته کارخونه رو می‌خوای ببینی؟", reply_markup=build_shop_categories_keyboard()
    )


async def factory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_player(user.id, user.username or user.first_name)
    text, kb = build_my_factories_view(user.id)
    await update.effective_message.reply_text(text, reply_markup=kb)


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_player(user.id, user.username or user.first_name)
    player = get_player(user.id)
    await update.effective_message.reply_text(f"💰 موجودی شما: {player['coins']} سکه")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 راهنمای بازی کارخونه ماشین‌سازی\n\n"
        "🔸 اول بازی چیکار کنم؟\n"
        f"➖ شما در ابتدا {START_COINS} سکه دارید. با دستور «شاپ» یه کارخونه ایرانی (مثلاً پراید) می‌خری.\n\n"
        "🔸 چطوری ماشین بسازم؟\n"
        "➖ بعد از خرید کارخونه، با دستور «کارخونه» واردش میشی و دکمه «شروع تولید» رو می‌زنی. هر ماشین یه زمان مشخص لازم داره (روی دکمه نوشته شده).\n\n"
        "🔸 ماشین ساخته شد، حالا چیکار کنم؟\n"
        "➖ وقتی تایمر تموم شد، دوباره وارد «کارخونه» شو و دکمه «دریافت و فروش» رو بزن. ماشین خودکار فروخته میشه و سکه‌اش میاد تو حسابت.\n\n"
        "🔸 چرا نمی‌تونم کارخونه خارجی/کمیاب بخرم؟\n"
        "➖ هر دسته قفله تا یه کارخونه از دسته قبلی داشته باشی:\n"
        "ایرانی 🔓 آزاد ← خارجی (نیاز به ۱ کارخونه ایرانی) ← کمیاب (نیاز به ۱ کارخونه خارجی) ← هواپیما (نیاز به ۱ کارخونه کمیاب)\n\n"
        "🔸 ارتقا چه فایده‌ای داره؟\n"
        "➖ هر بار که یه کارخونه رو ارتقا میدی، هم سریع‌تر ماشین می‌سازه هم گرون‌تر می‌فروشتش. هزینه ارتقا هر بار بیشتر میشه.\n\n"
        "🔸 میشه هم‌زمان چند کارخونه داشته باشم؟\n"
        "➖ بله، هر کارخونه که بخری جدا و مستقل کار می‌کنه؛ می‌تونی هم‌زمان چندتا تولید فعال داشته باشی.\n\n"
        "دستورات: شاپ | کارخونه | موجودی | راهنما"
    )
    await update.effective_message.reply_text(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text == "شاپ":
        await shop_cmd(update, context)
    elif text == "کارخونه":
        await factory_cmd(update, context)
    elif text == "موجودی":
        await balance_cmd(update, context)
    elif text == "راهنما":
        await help_cmd(update, context)


# ------------------------------------------------------------------
# دکمه‌ها
# ------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    user = query.from_user
    ensure_player(user.id, user.username or user.first_name)
    data = query.data

    if data == "shop:open":
        await query.edit_message_text("🛒 شاپ — کدوم دسته کارخونه رو می‌خوای ببینی؟", reply_markup=build_shop_categories_keyboard())
        return

    if data == "shop:back":
        await query.edit_message_text("🛒 شاپ — کدوم دسته کارخونه رو می‌خوای ببینی؟", reply_markup=build_shop_categories_keyboard())
        return

    if data.startswith("shopcat:"):
        category = data.split(":", 1)[1]
        text, kb = build_shop_category_view(user.id, category)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("buyfactory:"):
        car_key = data.split(":", 1)[1]
        info = CAR_CATALOG[car_key]
        req = CATEGORY_REQUIREMENT[info["category"]]
        if req and not owns_category(user.id, req):
            await query.answer(f"اول باید یه کارخونه دسته {req} داشته باشی.", show_alert=True)
            return
        owned = get_owned_factories(user.id)
        if car_key in owned:
            await query.answer("این کارخونه رو قبلاً خریدی.", show_alert=True)
            return
        player = get_player(user.id)
        if player["coins"] < info["factory_cost"]:
            await query.answer(f"سکه کافی نداری. هزینه: {info['factory_cost']}", show_alert=True)
            return
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE players SET coins = coins - ? WHERE user_id=?", (info["factory_cost"], user.id))
        c.execute("INSERT INTO factories (user_id, car_key, level) VALUES (?, ?, 1)", (user.id, car_key))
        conn.commit()
        conn.close()
        text, kb = build_shop_category_view(user.id, info["category"])
        await query.edit_message_text(text + "\n\n✅ کارخونه خریداری شد!", reply_markup=kb)
        return

    if data.startswith("produce:"):
        car_key = data.split(":", 1)[1]
        owned = get_owned_factories(user.id)
        if car_key not in owned:
            await query.answer("این کارخونه رو نداری.", show_alert=True)
            return
        conn = db()
        c = conn.cursor()
        c.execute("SELECT * FROM production_jobs WHERE user_id=? AND car_key=? AND collected=0", (user.id, car_key))
        if c.fetchone():
            conn.close()
            await query.answer("این کارخونه الان داره تولید می‌کنه.", show_alert=True)
            return
        seconds = effective_seconds(car_key, owned[car_key])
        c.execute(
            "INSERT INTO production_jobs (user_id, car_key, ready_at, collected) VALUES (?, ?, ?, 0)",
            (user.id, car_key, time.time() + seconds),
        )
        conn.commit()
        conn.close()
        text, kb = build_my_factories_view(user.id)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("collect:"):
        job_id = int(data.split(":", 1)[1])
        conn = db()
        c = conn.cursor()
        c.execute("SELECT * FROM production_jobs WHERE id=? AND user_id=?", (job_id, user.id))
        job = c.fetchone()
        if not job or job["collected"] or job["ready_at"] > time.time():
            conn.close()
            await query.answer("هنوز آماده نیست یا قبلاً دریافت شده.", show_alert=True)
            return
        owned = get_owned_factories(user.id)
        level = owned.get(job["car_key"], 1)
        sell_price = effective_sell_price(job["car_key"], level)
        c.execute("UPDATE production_jobs SET collected=1 WHERE id=?", (job_id,))
        c.execute("UPDATE players SET coins = coins + ? WHERE user_id=?", (sell_price, user.id))
        conn.commit()
        conn.close()
        text, kb = build_my_factories_view(user.id)
        await query.edit_message_text(
            text + f"\n\n✅ {job['car_key'].replace('_',' ')} ساخته و فروخته شد! {sell_price} سکه گرفتی.",
            reply_markup=kb,
        )
        return

    if data.startswith("upgrade:"):
        car_key = data.split(":", 1)[1]
        owned = get_owned_factories(user.id)
        if car_key not in owned:
            await query.answer("این کارخونه رو نداری.", show_alert=True)
            return
        level = owned[car_key]
        info = CAR_CATALOG[car_key]
        cost = int(info["factory_cost"] * level * UPGRADE_BASE_COST_RATIO)
        player = get_player(user.id)
        if player["coins"] < cost:
            await query.answer(f"سکه کافی نداری. هزینه ارتقا: {cost}", show_alert=True)
            return
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE players SET coins = coins - ? WHERE user_id=?", (cost, user.id))
        c.execute("UPDATE factories SET level = level + 1 WHERE user_id=? AND car_key=?", (user.id, car_key))
        conn.commit()
        conn.close()
        text, kb = build_my_factories_view(user.id)
        await query.edit_message_text(text + f"\n\n✅ کارخونه {car_key.replace('_',' ')} به سطح {level + 1} ارتقا پیدا کرد.", reply_markup=kb)
        return


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده. اونو تو Railway Variables بذار.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.Regex("^(شاپ|کارخونه|موجودی|راهنما)$"), handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("Car factory bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
