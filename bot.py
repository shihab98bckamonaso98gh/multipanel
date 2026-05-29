import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set

import pyotp
import requests
from dotenv import load_dotenv
from faker import Faker
from telegram import (
    Bot,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ---------- Configuration ----------
TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
BOT_USERNAME = os.getenv("BOT_USERNAME")
USERNAME = "thanhxuan"
PASSWORD = "thanhxuan"
BASE_URL = "http://54.38.92.155/ints"
LOGIN_URL = f"{BASE_URL}/login"
SIGNIN_URL = f"{BASE_URL}/signin"
PAGE_URL = f"{BASE_URL}/agent/SMSCDRStats"
DATA_URL = f"{BASE_URL}/agent/res/data_smscdr.php"
SEEN_PAIRS_FILE = "seen_pairs.txt"
CHECK_INTERVAL = 5
INTERNAL_RETRIES = 3
RETRY_BACKOFF = 15

# JSON data files
MAIN_BUTTONS_FILE = "main_buttons.json"
SUB_BUTTONS_FILE = "sub_buttons.json"
POOLS_FILE = "pools.json"
ASSIGNED_FILE = "assigned.json"
USERS_FILE = "users.json"

# SQLite database for wallet/withdraw and bans
DB_FILE = "wallet.db"
# ------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("sms_otp_bot")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "close",
    "Cache-Control": "no-cache",
})

last_get_number: Dict[int, float] = {}

# ----------------------------------------------------------------------
# Robust JSON loading
# ----------------------------------------------------------------------
def load_json(filename, default):
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r') as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"Corrupted JSON file {filename}. Resetting to default.")
        os.remove(filename)
        return default

def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_main_buttons() -> List[str]:
    return load_json(MAIN_BUTTONS_FILE, ["Facebook", "Instagram"])

def save_main_buttons(buttons: List[str]):
    save_json(MAIN_BUTTONS_FILE, buttons)

def load_sub_buttons() -> Dict[str, List[str]]:
    return load_json(SUB_BUTTONS_FILE, {"Facebook": ["Peru"], "Instagram": ["India"]})

def save_sub_buttons(data: Dict[str, List[str]]):
    save_json(SUB_BUTTONS_FILE, data)

def load_pools() -> Dict[str, List[str]]:
    return load_json(POOLS_FILE, {})

def save_pools(data: Dict[str, List[str]]):
    save_json(POOLS_FILE, data)

def load_assigned() -> Dict[str, int]:
    return load_json(ASSIGNED_FILE, {})

def save_assigned(data: Dict[str, int]):
    save_json(ASSIGNED_FILE, data)

def load_users() -> Set[int]:
    return set(load_json(USERS_FILE, []))

def save_users(users: Set[int]):
    save_json(USERS_FILE, list(users))

# ---------- SQLite helpers ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance_bdt REAL DEFAULT 0.0,
                    bkash TEXT,
                    rocket TEXT,
                    binance TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount_bdt REAL,
                    method TEXT,
                    wallet_detail TEXT,
                    status TEXT DEFAULT 'pending',
                    request_time TEXT,
                    completed_time TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
                    user_id INTEGER PRIMARY KEY,
                    until REAL
                )''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal_bdt', '20.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('per_otp_bdt', '0.30')")
    conn.commit()
    conn.close()

init_db()

def is_banned(user_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT until FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row[0] > time.time())

def ban_user(user_id, minutes=5):
    until = time.time() + minutes * 60
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO banned_users (user_id, until) VALUES (?, ?)", (user_id, until))
    conn.commit()
    conn.close()

def ensure_user_exists(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_user_balance(user_id):
    ensure_user_exists(user_id)
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT balance_bdt FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else 0.0

def credit_user(user_id, amount_bdt):
    ensure_user_exists(user_id)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE users SET balance_bdt = balance_bdt + ? WHERE user_id=?", (amount_bdt, user_id))
    conn.commit()
    conn.close()

def deduct_user(user_id, amount_bdt):
    ensure_user_exists(user_id)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE users SET balance_bdt = balance_bdt - ? WHERE user_id=?", (amount_bdt, user_id))
    conn.commit()
    conn.close()

def get_user_wallet(user_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT bkash, rocket, binance FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return {'bkash': row[0], 'rocket': row[1], 'binance': row[2]} if row else {'bkash': None, 'rocket': None, 'binance': None}

def set_wallet_detail(user_id, field, value):
    ensure_user_exists(user_id)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()

def create_withdrawal(user_id, amount_bdt, method, wallet_detail):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    balance = conn.execute("SELECT balance_bdt FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    if balance < amount_bdt:
        conn.close()
        return False, "Insufficient balance."
    conn.execute("UPDATE users SET balance_bdt = balance_bdt - ? WHERE user_id=?", (amount_bdt, user_id))
    conn.execute("INSERT INTO withdraw_requests (user_id, amount_bdt, method, wallet_detail, status, request_time) VALUES (?,?,?,?,'pending',?)",
                 (user_id, amount_bdt, method, wallet_detail, now))
    conn.commit()
    conn.close()
    return True, None

def get_pending_requests():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT id, user_id, amount_bdt, method, wallet_detail, request_time FROM withdraw_requests WHERE status='pending' ORDER BY request_time").fetchall()
    conn.close()
    return [{'id': r[0], 'user_id': r[1], 'amount_bdt': r[2], 'method': r[3], 'wallet_detail': r[4], 'time': r[5]} for r in rows]

def complete_withdrawal(request_id, admin_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT id, user_id, amount_bdt, method, wallet_detail FROM withdraw_requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
    if not row:
        conn.close()
        return None
    user_id = row[1]
    amount = row[2]
    method = row[3]
    wallet = row[4]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE withdraw_requests SET status='completed', completed_time=? WHERE id=?", (now, request_id))
    conn.commit()
    conn.close()

    ex_rate = 125.0
    if method == 'binance':
        amount_display = f"${amount/ex_rate:.4f}"
        wallet_label = "Binance UID"
    else:
        amount_display = f"{amount:.2f} BDT"
        wallet_label = f"{method.capitalize()} Number" if method != 'mobile' else "Mobile Number"

    msg = (
        f"🎉 <b>Withdrawal Approved</b>\n\n"
        f"💵 <b>Amount:</b> {amount_display}\n"
        f"🏦 <b>Method:</b> {method}\n"
        f"📞 <b>{wallet_label}:</b> {wallet}\n"
        f"✅ <b>Status:</b> Complete\n\n"
        f"We appreciate your trust! Share your experience or reach support below."
    )
    return user_id, msg

def get_withdrawal_history(user_id=None):
    conn = sqlite3.connect(DB_FILE)
    if user_id is None:
        rows = conn.execute("SELECT id, user_id, amount_bdt, method, wallet_detail, request_time, completed_time FROM withdraw_requests WHERE status='completed' ORDER BY completed_time DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute("SELECT id, amount_bdt, method, wallet_detail, request_time, completed_time FROM withdraw_requests WHERE user_id=? AND status='completed' ORDER BY completed_time DESC", (user_id,)).fetchall()
    conn.close()
    return [{'id': r[0], 'user_id': r[1] if len(r)>6 else user_id, 'amount_bdt': r[2], 'method': r[3], 'wallet': r[4], 'request_time': r[5], 'completed_time': r[6]} for r in rows]

def get_setting(key, default=None):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def is_admin(user_id):
    return user_id == ADMIN_CHAT_ID

# ----------------------------------------------------------------------
# Site login & data fetching
# ----------------------------------------------------------------------
def login() -> bool:
    try:
        resp = session.get(LOGIN_URL, timeout=30)
    except Exception as e:
        logger.error(f"Login page request failed: {e}")
        return False
    match = re.search(r"What is (\d+)\s*\+\s*(\d+)\s*=\s*\?\s*:", resp.text)
    if not match:
        logger.error("CAPTCHA question not found.")
        return False
    a, b = int(match.group(1)), int(match.group(2))
    answer = a + b
    logger.info(f"CAPTCHA: {a} + {b} = {answer}")
    data = {"username": USERNAME, "password": PASSWORD, "capt": str(answer)}
    try:
        resp = session.post(SIGNIN_URL, data=data, allow_redirects=True, timeout=30)
    except Exception as e:
        logger.error(f"Login POST failed: {e}")
        return False
    if "Dashboard" in resp.text or "/agent/" in resp.url:
        logger.info("Login successful.")
        return True
    else:
        logger.error("Login failed.")
        return False

def fetch_data_sync() -> list | None:
    today = datetime.now()
    fdate1 = (today - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    fdate2 = (today + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    params = {
        "fdate1": fdate1, "fdate2": fdate2, "frange": "", "fclient": "",
        "fnum": "", "fcli": "", "fgdate": "", "fgmonth": "", "fgrange": "",
        "fgclient": "", "fgnumber": "", "fgcli": "", "fg": "0",
        "sEcho": "1", "iDisplayStart": "0", "iDisplayLength": "-1",
        "iColumns": "9", "sColumns": "",
        **{f"mDataProp_{i}": str(i) for i in range(9)},
    }
    for attempt in range(INTERNAL_RETRIES):
        try:
            resp = session.get(DATA_URL, params=params, timeout=30)
        except Exception as e:
            logger.warning(f"Data request attempt {attempt+1} failed: {e}")
            time.sleep(2)
            continue
        if "login" in resp.url.lower():
            logger.warning("Session expired.")
            return None
        if resp.status_code != 200:
            logger.warning(f"Status {resp.status_code}")
            time.sleep(2)
            continue
        try:
            json_data = resp.json()
        except Exception:
            logger.error(f"JSON decode failed. First 300 chars: {resp.text[:300]}")
            if "login" in resp.text.lower() and "password" in resp.text.lower():
                logger.warning("Response is login page.")
                return None
            time.sleep(2)
            continue
        rows = json_data.get("aaData")
        if rows is None:
            logger.info("No 'aaData' in response.")
            return []
        return rows
    logger.error("Data fetch failed after all retries.")
    return None

async def fetch_data_async() -> list | None:
    return await asyncio.to_thread(fetch_data_sync)

# ----------------------------------------------------------------------
# OTP extraction & seen pairs
# ----------------------------------------------------------------------
def extract_otp(sms_text: str) -> str | None:
    if not isinstance(sms_text, str):
        return None
    match = re.search(r"#\s*((?:\d+\s*)+?)\s*is\s+your", sms_text)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match2 = re.search(r"#\s*(\d[\d\s]+)", sms_text)
    if match2:
        return re.sub(r"\s+", "", match2.group(1))
    return None

def load_seen_pairs() -> Set[str]:
    if not os.path.exists(SEEN_PAIRS_FILE):
        return set()
    with open(SEEN_PAIRS_FILE, 'r') as f:
        return set(line.strip() for line in f if "|" in line)

def save_seen_pair(number: str, otp: str):
    with open(SEEN_PAIRS_FILE, 'a') as f:
        f.write(f"{number}|{otp}\n")

# ----------------------------------------------------------------------
# Formatting & sending
# ----------------------------------------------------------------------
def mask_number(num: str) -> str:
    if not num.startswith("+"):
        num = "+" + num
    if len(num) <= 7:
        return num[:3] + "***"
    return num[:4] + "*" * (len(num) - 7) + num[-3:]

async def send_otp_to_group(bot: Bot, row: list, otp: str):
    number = str(row[2]).strip()
    sms = str(row[5]).strip()
    masked = mask_number(number)
    text = (
        f"✅ New message received!\n"
        f"📞 Number: {masked}\n\n"
        f"🔑 OTP: {otp}\n\n"
        f"💬 Message:\n{sms}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Get Number", url=f"https://t.me/{BOT_USERNAME}?start=start")]
    ])
    try:
        await bot.send_message(GROUP_CHAT_ID, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Failed to send to group: {e}")

async def send_otp_to_user(bot: Bot, user_id: int, row: list, otp: str, old_balance: float, new_balance: float):
    number = str(row[2]).strip()
    sms = str(row[5]).strip()
    text = (
        "📩 <b>Message Received!</b>\n\n"
        f"📞 Number : <code>+{number}</code>\n\n"
        f"🔑 OTP Code: <code>{otp}</code>\n\n"
        f"💬 Full Message:\n<code>{sms}</code>\n\n"
        f"💰 Balance : {old_balance:.2f} BDT ---> {new_balance:.2f} BDT"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"OTP: {otp}", copy_text=CopyTextButton(text=otp))]
    ])
    try:
        await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send to user {user_id}: {e}")

# ----------------------------------------------------------------------
# Background OTP monitor
# ----------------------------------------------------------------------
async def monitoring_loop(application: Application):
    bot = application.bot
    if not login():
        logger.critical("Initial login failed.")
        return
    seen_pairs = load_seen_pairs()
    rows = await fetch_data_async()
    if rows:
        for row in rows:
            if len(row) < 9: continue
            sms_text = str(row[5])
            if "#" not in sms_text: continue
            otp = extract_otp(sms_text)
            if not otp: continue
            number = str(row[2]).strip()
            pair = f"{number}|{otp}"
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                save_seen_pair(number, otp)
        logger.info(f"Initialized with {len(seen_pairs)} seen pairs.")
    else:
        logger.warning("Initial data fetch failed.")

    consecutive_failures = 0
    while True:
        rows = await fetch_data_async()
        if rows is None:
            logger.warning(f"Data fetch failed. Consecutive failures: {consecutive_failures+1}")
            if consecutive_failures == 0:
                logger.info("Attempting re-login...")
                if login():
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            else:
                consecutive_failures += 1
            backoff = RETRY_BACKOFF * min(consecutive_failures, 3)
            logger.info(f"Waiting {backoff}s before retry.")
            await asyncio.sleep(backoff)
            continue
        consecutive_failures = 0

        assigned = load_assigned()
        per_otp = float(get_setting("per_otp_bdt", "0.30"))
        for row in rows:
            if len(row) < 9: continue
            sms_text = str(row[5])
            if "#" not in sms_text: continue
            otp = extract_otp(sms_text)
            if not otp: continue
            number = str(row[2]).strip()
            pair = f"{number}|{otp}"
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            save_seen_pair(number, otp)

            tasks = [send_otp_to_group(bot, row, otp)]
            user_id = assigned.get(number)
            if user_id:
                old_balance = get_user_balance(user_id)
                credit_user(user_id, per_otp)
                new_balance = get_user_balance(user_id)
                tasks.append(send_otp_to_user(bot, user_id, row, otp, old_balance, new_balance))
            await asyncio.gather(*tasks)

        await asyncio.sleep(CHECK_INTERVAL)

# ----------------------------------------------------------------------
# Rate limiting for Get Number
# ----------------------------------------------------------------------
def check_get_number_rate_limit(user_id):
    now = time.time()
    last = last_get_number.get(user_id, 0)
    if now - last < 10:
        return False, 10 - int(now - last)
    last_get_number[user_id] = now
    return True, 0

# ----------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users = load_users()
    users.add(user.id)
    save_users(users)
    keyboard = [
        ["Get Number", "Fake Name"],
        ["Get 2FA", "👤 My Profile"]
    ]
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

# ── Get Number flow ──
async def get_number_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are temporarily banned for 5 minutes due to flooding.")
        return
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons available.")
        return
    keyboard = [[InlineKeyboardButton(name, callback_data=f"get_main:{name}")] for name in mains]
    await update.message.reply_text("Choose a service:", reply_markup=InlineKeyboardMarkup(keyboard))

async def get_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    main_name = query.data.split(":",1)[1]
    subs = load_sub_buttons().get(main_name, [])
    if not subs:
        await query.edit_message_text("No sub-categories available.")
        return
    keyboard = [[InlineKeyboardButton(sub, callback_data=f"get_sub:{main_name}:{sub}")] for sub in subs]
    await query.edit_message_text(f"Select a sub-category for {main_name}:", reply_markup=InlineKeyboardMarkup(keyboard))

async def assign_number_and_display(query_or_update, main_name, sub_name, user_id, context=None):
    pool_key = f"{main_name}_{sub_name}"
    pools = load_pools()
    numbers = pools.get(pool_key, [])
    if not numbers:
        if hasattr(query_or_update, 'edit_message_text'):
            await query_or_update.edit_message_text("No numbers available in this category.")
        else:
            await query_or_update.message.reply_text("No numbers available in this category.")
        return
    assigned_number = numbers.pop(0)
    pools[pool_key] = numbers
    save_pools(pools)
    assigned = load_assigned()
    assigned[assigned_number] = user_id
    save_assigned(assigned)

    if context:
        context.user_data["last_main"] = main_name
        context.user_data["last_sub"] = sub_name

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy Number", copy_text=CopyTextButton(text=assigned_number))],
        [InlineKeyboardButton("Change Number", callback_data=f"change_number:{main_name}:{sub_name}"),
         InlineKeyboardButton("OTP Group", url="https://t.me/otpservers")]
    ])
    text = f"Your assigned number:\n`{assigned_number}`"
    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def get_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    allowed, wait = check_get_number_rate_limit(user_id)
    if not allowed:
        await query.edit_message_text(f"⏳ Please wait {wait} seconds before requesting another number.")
        return
    _, main_name, sub_name = query.data.split(":",2)
    await assign_number_and_display(query, main_name, sub_name, user_id, context)

async def change_number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if is_banned(user_id):
        await query.edit_message_text("🚫 You are banned.")
        return
    allowed, wait = check_get_number_rate_limit(user_id)
    if not allowed:
        await query.edit_message_text(f"⏳ Wait {wait}s.")
        return
    _, main_name, sub_name = query.data.split(":",2)
    await assign_number_and_display(query, main_name, sub_name, user_id, context)

# ── Fake Name flow ──
FAKE_GENDER = 1

async def fake_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 Banned.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👨 Male", callback_data="fake_male"),
         InlineKeyboardButton("👩 Female", callback_data="fake_female")]
    ])
    await update.message.reply_text("Select gender:", reply_markup=keyboard)
    return FAKE_GENDER

async def fake_gender_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gender = query.data.split("_")[1]
    fake = Faker()
    if gender == 'male':
        first = fake.first_name_male()
        last = fake.last_name()
    else:
        first = fake.first_name_female()
        last = fake.last_name()
    full_name = f"{first} {last}"
    username = f"{first.lower()}{last.lower()}{random.randint(10,99)}"

    chars = string.ascii_letters + string.digits + "!@#$%^&*()_+-="
    random_part = ''.join(random.choices(chars, k=random.randint(8,10)))
    tz = timezone(timedelta(hours=6))
    day = datetime.now(tz).day
    password = f"{random_part}{day}"

    text = (
        f"{'👨' if gender=='male' else '👩'} <b>Generated Identity:</b>\n\n"
        f"<b>Name:</b> {full_name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>Password:</b> <code>{password}</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy Name", copy_text=CopyTextButton(text=full_name)),
         InlineKeyboardButton("Copy Username", copy_text=CopyTextButton(text=username))],
        [InlineKeyboardButton("Copy Password", copy_text=CopyTextButton(text=password))],
        [InlineKeyboardButton("Change Details", callback_data=f"fake_{gender}")]
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return FAKE_GENDER

# ── Get 2FA flow (with copy button) ──
GET2FA_SECRET = 1

async def get2fa_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 Banned.")
        return
    await update.message.reply_text(
        "📲 <b>Paste your 2FA Secret Key</b>\n\n"
        "<i>Example: JBSWY3DPEHPK3PXP</i>",
        parse_mode=ParseMode.HTML
    )
    return GET2FA_SECRET

async def get2fa_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    secret_raw = update.message.text.strip()
    secret_clean = re.sub(r'\s+', '', secret_raw).upper()
    if not re.fullmatch(r'[A-Z2-7]+', secret_clean):
        await update.message.reply_text(
            "❌ Invalid secret key. Only characters A-Z and 2-7 are allowed after removing spaces.\n"
            "Please try again or /cancel."
        )
        return GET2FA_SECRET
    try:
        totp = pyotp.TOTP(secret_clean)
        code = totp.now()
        remaining = 30 - (int(time.time()) % 30)
        msg = f"🔐 <b>2FA Code:</b> <code>{code}</code>\n⏱ Valid for {remaining} seconds"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"OTP: {code}", copy_text=CopyTextButton(text=code))]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"TOTP generation error: {e}")
        await update.message.reply_text("❌ Error generating code. Check your secret.")
    return ConversationHandler.END

# ── Admin: Add/Remove Main Button ──
ADD_MAIN, REMOVE_MAIN_SELECT = range(2)

async def add_remove_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    keyboard = [["Add Main Button", "Remove Main Button"], ["⬅️ Back"]]
    await update.message.reply_text("Choose action:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return ADD_MAIN

async def add_main_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    await update.message.reply_text("Send the name of the new main button:", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
    return ADD_MAIN

async def add_main_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    name = update.message.text.strip()
    mains = load_main_buttons()
    if name in mains:
        await update.message.reply_text("Already exists.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    else:
        mains.append(name)
        save_main_buttons(mains)
        sub_buttons = load_sub_buttons()
        if name not in sub_buttons:
            sub_buttons[name] = []
            save_sub_buttons(sub_buttons)
        await update.message.reply_text(f"Main button '{name}' added.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return ConversationHandler.END

async def remove_main_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons to remove.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(m, callback_data=f"remove_main:{m}")] for m in mains]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel")])
    await update.message.reply_text("Select main button to remove:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REMOVE_MAIN_SELECT

async def remove_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    main_name = query.data.split(":",1)[1]
    mains = load_main_buttons()
    if main_name in mains:
        mains.remove(main_name)
        save_main_buttons(mains)
        sub_buttons = load_sub_buttons()
        if main_name in sub_buttons:
            subs = sub_buttons.pop(main_name)
            save_sub_buttons(sub_buttons)
            pools = load_pools()
            for sub in subs:
                pools.pop(f"{main_name}_{sub}", None)
            save_pools(pools)
        await query.edit_message_text(f"Main button '{main_name}' and its sub buttons removed.")
    else:
        await query.edit_message_text("Not found.")
    return ConversationHandler.END

# ── Admin: Add/Remove Sub Button ──
ADD_SUB_MAIN_SELECT, ADD_SUB_NAME, REMOVE_SUB_MAIN_SELECT, REMOVE_SUB_SELECT = range(4)

async def add_remove_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    keyboard = [["Add Sub Button", "Remove Sub Button"], ["⬅️ Back"]]
    await update.message.reply_text("Choose action:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return ADD_SUB_MAIN_SELECT

async def add_sub_main_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(m, callback_data=f"add_sub_main:{m}")] for m in mains]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel")])
    await update.message.reply_text("Select main button:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_SUB_NAME

async def add_sub_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    context.user_data["add_sub_main"] = query.data.split(":",1)[1]
    await query.edit_message_text("Send the name of the new sub button:")
    return ADD_SUB_NAME

async def add_sub_name_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    main_name = context.user_data["add_sub_main"]
    sub_name = update.message.text.strip()
    sub_buttons = load_sub_buttons()
    if main_name not in sub_buttons:
        sub_buttons[main_name] = []
    if sub_name in sub_buttons[main_name]:
        await update.message.reply_text("Already exists.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    else:
        sub_buttons[main_name].append(sub_name)
        save_sub_buttons(sub_buttons)
        await update.message.reply_text(f"Sub button '{sub_name}' added under '{main_name}'.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return ConversationHandler.END

async def remove_sub_main_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(m, callback_data=f"remove_sub_main:{m}")] for m in mains]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel")])
    await update.message.reply_text("Select main button:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REMOVE_SUB_SELECT

async def remove_sub_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    main_name = query.data.split(":",1)[1]
    subs = load_sub_buttons().get(main_name, [])
    if not subs:
        await query.edit_message_text(f"No sub buttons under '{main_name}'.")
        return ConversationHandler.END
    context.user_data["remove_sub_main"] = main_name
    keyboard = [[InlineKeyboardButton(s, callback_data=f"remove_sub:{main_name}:{s}")] for s in subs]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel")])
    await query.edit_message_text("Select sub button to remove:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REMOVE_SUB_SELECT

async def remove_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    _, main_name, sub_name = query.data.split(":",2)
    sub_buttons = load_sub_buttons()
    if main_name in sub_buttons and sub_name in sub_buttons[main_name]:
        sub_buttons[main_name].remove(sub_name)
        save_sub_buttons(sub_buttons)
        pool_key = f"{main_name}_{sub_name}"
        pools = load_pools()
        pools.pop(pool_key, None)
        save_pools(pools)
        await query.edit_message_text(f"Sub button '{sub_name}' removed.")
    else:
        await query.edit_message_text("Not found.")
    return ConversationHandler.END

# ── Admin: Upload numbers (integrated inside profile) ──
UPLOAD_MAIN_SELECT, UPLOAD_SUB_SELECT, UPLOAD_FILE = range(100, 103)

async def upload_from_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(m, callback_data=f"upload_main:{m}")] for m in mains]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel_upload")])
    await update.message.reply_text("Select main button:", reply_markup=InlineKeyboardMarkup(keyboard))
    return UPLOAD_MAIN_SELECT

async def upload_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_upload":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    main_name = query.data.split(":",1)[1]
    context.user_data["upload_main"] = main_name
    subs = load_sub_buttons().get(main_name, [])
    if not subs:
        await query.edit_message_text(f"No sub buttons under '{main_name}'.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(s, callback_data=f"upload_sub:{main_name}:{s}")] for s in subs]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel_upload")])
    await query.edit_message_text("Select sub button:", reply_markup=InlineKeyboardMarkup(keyboard))
    return UPLOAD_SUB_SELECT

async def upload_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_upload":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    _, main_name, sub_name = query.data.split(":",2)
    context.user_data["upload_main"] = main_name
    context.user_data["upload_sub"] = sub_name
    await query.edit_message_text(f"Send a .txt file with numbers (one per line) for {main_name} / {sub_name}.")
    return UPLOAD_FILE

async def upload_file_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Please send a .txt file.")
        return UPLOAD_FILE
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Only .txt files accepted.")
        return UPLOAD_FILE
    file = await doc.get_file()
    content = (await file.download_as_bytearray()).decode("utf-8")
    numbers = [line.strip() for line in content.splitlines() if line.strip()]
    main_name = context.user_data["upload_main"]
    sub_name = context.user_data["upload_sub"]
    pool_key = f"{main_name}_{sub_name}"
    pools = load_pools()
    if pool_key not in pools:
        pools[pool_key] = []
    pools[pool_key].extend(numbers)
    save_pools(pools)

    try:
        await update.message.bot.send_message(GROUP_CHAT_ID, f"{main_name}র {sub_name} নাম্বার যুক্ত করা হয়েছে।")
    except Exception as e:
        logger.error(f"Broadcast upload notification failed: {e}")

    await update.message.reply_text(f"Added {len(numbers)} numbers to {main_name} / {sub_name}.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return ConversationHandler.END

# ── Admin: Broadcast ──
BROADCAST_RECEIVE, BROADCAST_CONFIRM = range(2)

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    await update.message.reply_text("Send the content you want to broadcast (text, photo, video, file).", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
    return BROADCAST_RECEIVE

async def broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⬅️ Back":
        return await back_to_profile(update, context)
    context.user_data["broadcast_msg"] = update.message
    keyboard = [
        [InlineKeyboardButton("Yes, send to all", callback_data="broadcast_confirm")],
        [InlineKeyboardButton("⬅️ Back", callback_data="broadcast_cancel")]
    ]
    await update.message.reply_text("Confirm broadcast?", reply_markup=InlineKeyboardMarkup(keyboard))
    return BROADCAST_CONFIRM

async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "broadcast_cancel":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)

    users = load_users()
    msg = context.user_data["broadcast_msg"]
    bot = context.bot
    success = 0
    for uid in users:
        try:
            await msg.copy(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Broadcast fail to {uid}: {e}")
    await query.edit_message_text(f"Broadcast finished. Sent to {success}/{len(users)} users.")
    return ConversationHandler.END

# ── Helper: admin profile keyboard ──
def admin_profile_kb():
    return [
        ["💰 Balance", "📋 Pending"],
        ["✅ Approved", "✏️ Edit"],
        ["📢 Broadcast", "Upload"],
        ["Add/Remove Main Button", "Add/Remove Sub Button"],
        ["⬅️ Back"]
    ]

async def back_to_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("👤 Profile Menu", reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["💰 Balance", "📋 Withdraw History"], ["⬅️ Back"]], resize_keyboard=True))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await back_to_profile(update, context)

# ── Profile menu ──
PROFILE_SELECT, SET_WALLET_METHOD, SET_WALLET_VALUE, WITHDRAW_METHOD, WITHDRAW_AMOUNT, EDIT_MENU, EDIT_PRICE, EDIT_RATE = range(8)

async def profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("👤 Profile Menu", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    else:
        kb = [["💰 Balance", "📋 Withdraw History"], ["⬅️ Back"]]
        await update.message.reply_text("👤 Profile Menu", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return PROFILE_SELECT

async def profile_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if text == "⬅️ Back":
        await start(update, context)
        return ConversationHandler.END
    elif text == "💰 Balance":
        balance = get_user_balance(user_id)
        wallet = get_user_wallet(user_id)
        min_bdt = float(get_setting("min_withdrawal_bdt", "20.0"))
        usd = balance / 125.0
        min_usd = min_bdt / 125.0
        msg = (
            f"⚠️ Double‑check your wallet! Wrong details = no refund.\n\n"
            f"🤑 Balance: {balance:.2f} BDT / ${usd:.4f}\n\n"
            f"🌍 Bkash: {wallet['bkash'] or 'Not Set'}\n"
            f"🌍 Rocket: {wallet['rocket'] or 'Not Set'}\n"
            f"🌍 Binance: {wallet['binance'] or 'Not Set'}\n\n"
            f"💳 Minimum Withdrawal: {min_bdt} BDT / ${min_usd:.2f}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Set Wallet", callback_data="profile_set_wallet"),
             InlineKeyboardButton("Withdraw", callback_data="profile_withdraw")]
        ])
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    elif text == "📋 Pending" and is_admin(user_id):
        pending = get_pending_requests()
        if not pending:
            await update.message.reply_text("No pending withdrawal requests.")
        else:
            lines = []
            kb_buttons = []
            for p in pending:
                lines.append(f"🔹 ID: {p['id']} | User: {p['user_id']}\n   💵 {p['amount_bdt']} BDT via {p['method']} ({p['wallet_detail']})\n   🕒 {p['time']}")
                kb_buttons.append([InlineKeyboardButton(f"✅ Complete #{p['id']}", callback_data=f"admin_complete_{p['id']}")])
            await update.message.reply_text("📋 <b>Pending Withdrawals:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_buttons))
        return PROFILE_SELECT
    elif text == "✅ Approved" and is_admin(user_id):
        history = get_withdrawal_history(user_id=None)
        if not history:
            await update.message.reply_text("No approved withdrawals yet.")
        else:
            lines = [f"🔹 ID: {h['id']} | User: {h['user_id']}\n   💵 {h['amount_bdt']} BDT via {h['method']} ({h['wallet']})\n   📅 {h['completed_time']}" for h in history]
            await update.message.reply_text("✅ <b>Approved Withdrawals:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    elif text == "📋 Withdraw History" and not is_admin(user_id):
        history = get_withdrawal_history(user_id=user_id)
        if not history:
            await update.message.reply_text("No completed withdrawals yet.")
        else:
            lines = [f"🔹 ID: {h['id']}\n   💵 {h['amount_bdt']} BDT via {h['method']} ({h['wallet']})\n   📅 {h['completed_time']}" for h in history]
            await update.message.reply_text("📋 <b>Your Withdraw History:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML)
        return PROFILE_SELECT
    elif text == "✏️ Edit" and is_admin(user_id):
        kb = [["Withdraw price", "Rate"], ["⬅️ Back"]]
        await update.message.reply_text("Edit Menu", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return EDIT_MENU
    elif text == "Upload" and is_admin(user_id):
        return await upload_from_profile(update, context)
    else:
        return PROFILE_SELECT

# ── Set Wallet flow ──
async def profile_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "profile_set_wallet":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Bkash", callback_data="wallet_bkash"),
             InlineKeyboardButton("Rocket", callback_data="wallet_rocket"),
             InlineKeyboardButton("Binance", callback_data="wallet_binance")]
        ])
        await query.edit_message_text("Select wallet to set:", reply_markup=keyboard)
        return SET_WALLET_METHOD
    elif data == "profile_withdraw":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Bkash", callback_data="withdraw_method_bkash"),
             InlineKeyboardButton("Rocket", callback_data="withdraw_method_rocket"),
             InlineKeyboardButton("Binance", callback_data="withdraw_method_binance"),
             InlineKeyboardButton("Mobile Recharge", callback_data="withdraw_method_mobile")]
        ])
        await query.edit_message_text("Select withdrawal method:", reply_markup=keyboard)
        return WITHDRAW_METHOD

async def wallet_method_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.split("_")[1]
    context.user_data["wallet_method"] = method
    prompt = "Enter your Binance UID:" if method == "binance" else f"Enter your {method.capitalize()} number:"
    await query.edit_message_text(prompt)
    return SET_WALLET_VALUE

async def wallet_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    value = update.message.text.strip()
    method = context.user_data["wallet_method"]
    if method in ("bkash", "rocket") and not re.fullmatch(r"\d{7,15}", value):
        await update.message.reply_text("Invalid phone number. Must be 7-15 digits. Try again or /cancel.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return SET_WALLET_VALUE
    elif method == "binance" and not re.fullmatch(r"\d{6,}", value):
        await update.message.reply_text("Invalid Binance UID. Must be numeric. Try again or /cancel.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return SET_WALLET_VALUE
    set_wallet_detail(user_id, method, value)
    await update.message.reply_text(f"{method.capitalize()} wallet set to: {value}", reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["💰 Balance", "📋 Withdraw History"], ["⬅️ Back"]], resize_keyboard=True))
    return ConversationHandler.END

async def withdraw_method_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.replace("withdraw_method_", "")
    context.user_data["withdraw_method"] = method
    wallet = get_user_wallet(query.from_user.id)
    if method in ("bkash", "rocket", "binance"):
        detail = wallet.get(method)
    else:
        detail = wallet.get("bkash")
    if not detail:
        await query.edit_message_text(f"Your {method} wallet is not set. Use 'Set Wallet' first.")
        return ConversationHandler.END
    context.user_data["withdraw_wallet_detail"] = detail
    balance = get_user_balance(query.from_user.id)
    min_bdt = float(get_setting("min_withdrawal_bdt", "20.0"))
    usd = balance / 125.0
    min_usd = min_bdt / 125.0
    msg = (
        f"💰 Current Balance: {balance:.2f} BDT / ${usd:.4f}\n"
        f"💳 Minimum Withdrawal: {min_bdt} BDT / ${min_usd:.2f}\n\n"
        f"Enter amount in BDT to withdraw:"
    )
    await query.edit_message_text(msg)
    return WITHDRAW_AMOUNT

async def withdraw_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Invalid number. Try again or /cancel.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return WITHDRAW_AMOUNT
    min_bdt = float(get_setting("min_withdrawal_bdt", "20.0"))
    if amount < min_bdt:
        await update.message.reply_text(f"Minimum withdrawal is {min_bdt} BDT.", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return WITHDRAW_AMOUNT
    success, err = create_withdrawal(user_id, amount, context.user_data["withdraw_method"], context.user_data["withdraw_wallet_detail"])
    if success:
        await update.message.reply_text("✅ Withdrawal request submitted. Processing...", reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["💰 Balance", "📋 Withdraw History"], ["⬅️ Back"]], resize_keyboard=True))
    else:
        await update.message.reply_text(f"❌ {err}", reply_markup=ReplyKeyboardMarkup(admin_profile_kb() if is_admin(user_id) else [["💰 Balance", "📋 Withdraw History"], ["⬅️ Back"]], resize_keyboard=True))
    return ConversationHandler.END

async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Withdraw price":
        cur_min = get_setting("min_withdrawal_bdt", "20.0")
        await update.message.reply_text(f"Current minimum withdrawal: {cur_min} BDT\nEnter new minimum amount in BDT:", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return EDIT_PRICE
    elif text == "Rate":
        cur_rate = get_setting("per_otp_bdt", "0.30")
        await update.message.reply_text(f"Current OTP earning rate: {cur_rate} BDT per OTP\nEnter new rate in BDT:", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
        return EDIT_RATE
    elif text == "⬅️ Back":
        return await profile_start(update, context)
    else:
        return EDIT_MENU

async def edit_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Back":
        return await profile_start(update, context)
    try:
        new_min = float(text)
        if new_min <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid amount. Positive number only.")
        return EDIT_PRICE
    set_setting("min_withdrawal_bdt", new_min)
    await update.message.reply_text(f"Minimum withdrawal updated to {new_min} BDT.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return ConversationHandler.END

async def edit_rate_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Back":
        return await profile_start(update, context)
    try:
        new_rate = float(text)
        if new_rate <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid rate. Positive number only.")
        return EDIT_RATE
    set_setting("per_otp_bdt", new_rate)
    await update.message.reply_text(f"OTP earning rate updated to {new_rate} BDT.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return ConversationHandler.END

async def admin_complete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    req_id = int(query.data.split("_")[-1])
    result = complete_withdrawal(req_id, query.from_user.id)
    if result is None:
        await query.edit_message_text("Request not found or already processed.")
        return
    user_id, msg = result
    await context.bot.send_message(user_id, msg, parse_mode=ParseMode.HTML)
    await query.edit_message_text(f"✅ Withdrawal #{req_id} approved and user notified.")
    return

# ----------------------------------------------------------------------
# Build the Application
# ----------------------------------------------------------------------
def main():
    application = Application.builder().token(TOKEN).build()

    # Command handler
    application.add_handler(CommandHandler("start", start))

    # Standalone keyboard button handlers
    application.add_handler(MessageHandler(filters.Regex("^Get Number$"), get_number_start))
    application.add_handler(MessageHandler(filters.Regex("^Fake Name$"), fake_name_start))
    application.add_handler(MessageHandler(filters.Regex("^Get 2FA$"), get2fa_start))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(get_main_callback, pattern="^get_main:"))
    application.add_handler(CallbackQueryHandler(get_sub_callback, pattern="^get_sub:"))
    application.add_handler(CallbackQueryHandler(change_number_callback, pattern="^change_number:"))
    application.add_handler(CallbackQueryHandler(admin_complete_callback, pattern="^admin_complete_"))

    # Fake Name conversation
    fake_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Fake Name$"), fake_name_start)],
        states={FAKE_GENDER: [CallbackQueryHandler(fake_gender_select, pattern="^fake_")]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(fake_conv)

    # Get 2FA conversation
    get2fa_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Get 2FA$"), get2fa_start)],
        states={GET2FA_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, get2fa_generate)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(get2fa_conv)

    # Admin Add/Remove Main Button (inside profile)
    add_remove_main_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Add/Remove Main Button$"), add_remove_main)],
        states={
            ADD_MAIN: [
                MessageHandler(filters.Regex("^Add Main Button$"), add_main_prompt),
                MessageHandler(filters.Regex("^Remove Main Button$"), remove_main_select),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_main_receive),
            ],
            REMOVE_MAIN_SELECT: [CallbackQueryHandler(remove_main_callback, pattern="^remove_main:|^cancel$")]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(add_remove_main_conv)

    # Admin Add/Remove Sub Button (inside profile)
    add_remove_sub_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Add/Remove Sub Button$"), add_remove_sub)],
        states={
            ADD_SUB_MAIN_SELECT: [
                MessageHandler(filters.Regex("^Add Sub Button$"), add_sub_main_select),
                MessageHandler(filters.Regex("^Remove Sub Button$"), remove_sub_main_select),
            ],
            ADD_SUB_NAME: [
                CallbackQueryHandler(add_sub_main_callback, pattern="^add_sub_main:|^cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_name_receive),
            ],
            REMOVE_SUB_SELECT: [
                CallbackQueryHandler(remove_sub_main_callback, pattern="^remove_sub_main:|^cancel$"),
                CallbackQueryHandler(remove_sub_callback, pattern="^remove_sub:|^cancel$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(add_remove_sub_conv)

    # Admin Broadcast (inside profile)
    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Broadcast$"), broadcast_start)],
        states={
            BROADCAST_RECEIVE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_receive)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_confirm, pattern="^broadcast_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(broadcast_conv)

    # Profile conversation (includes upload states)
    profile_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👤 My Profile$"), profile_start)],
        states={
            PROFILE_SELECT: [
                MessageHandler(filters.Regex("^(💰 Balance|📋 Pending|✅ Approved|📋 Withdraw History|✏️ Edit|Upload|⬅️ Back)$"), profile_select),
                CallbackQueryHandler(profile_callback_handler, pattern="^(profile_set_wallet|profile_withdraw)$"),
            ],
            SET_WALLET_METHOD: [CallbackQueryHandler(wallet_method_select, pattern="^wallet_(bkash|rocket|binance)$")],
            SET_WALLET_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_value_received),
                CommandHandler("cancel", cancel)
            ],
            WITHDRAW_METHOD: [CallbackQueryHandler(withdraw_method_select, pattern="^withdraw_method_(bkash|rocket|binance|mobile)$")],
            WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received),
                CommandHandler("cancel", cancel)
            ],
            EDIT_MENU: [MessageHandler(filters.Regex("^(Withdraw price|Rate|⬅️ Back)$"), edit_menu)],
            EDIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_price_received),
                CommandHandler("cancel", cancel)
            ],
            EDIT_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_rate_received),
                CommandHandler("cancel", cancel)
            ],
            UPLOAD_MAIN_SELECT: [
                CallbackQueryHandler(upload_main_callback, pattern="^upload_main:|^cancel_upload$")
            ],
            UPLOAD_SUB_SELECT: [
                CallbackQueryHandler(upload_sub_callback, pattern="^upload_sub:|^cancel_upload$")
            ],
            UPLOAD_FILE: [
                MessageHandler(filters.Document.ALL, upload_file_receive),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: upload_file_receive(u, c))  # fallback for non-file
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(profile_conv)

    # Background monitoring
    async def post_init(app: Application):
        app.create_task(monitoring_loop(app))
    application.post_init = post_init

    logger.info("Bot started. Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()