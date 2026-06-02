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
from typing import Dict, List, Set, Optional

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

# Site 1 (original – login + scrape)
SITE1_BASE_URL = os.getenv("SITE1_BASE_URL", "http://54.38.92.155/ints")
SITE1_USERNAME = os.getenv("SITE1_USERNAME", "thanhxuan")
SITE1_PASSWORD = os.getenv("SITE1_PASSWORD", "thanhxuan")
SITE1_CHECK_INTERVAL = int(os.getenv("SITE1_CHECK_INTERVAL", "5"))

# Site 2 (new API)
SITE2_API_URL = os.getenv("SITE2_API_URL", "http://147.135.212.197/crapi/had/viewstats")
SITE2_API_TOKEN = os.getenv("SITE2_API_TOKEN", "")
SITE2_CHECK_INTERVAL = int(os.getenv("SITE2_CHECK_INTERVAL", "18"))

# Site 3 (new login + scrape)
SITE3_BASE_URL = os.getenv("SITE3_BASE_URL", "https://nexor-iprn.com")
SITE3_USERNAME = os.getenv("SITE3_USERNAME", "")
SITE3_PASSWORD = os.getenv("SITE3_PASSWORD", "")
SITE3_CHECK_INTERVAL = int(os.getenv("SITE3_CHECK_INTERVAL", "10"))

# Site 4 (new – CSRF token required, will be extracted dynamically)
SITE4_BASE_URL = os.getenv("SITE4_BASE_URL", "http://168.119.13.175/ints")
SITE4_USERNAME = os.getenv("SITE4_USERNAME", "")
SITE4_PASSWORD = os.getenv("SITE4_PASSWORD", "")
SITE4_CHECK_INTERVAL = int(os.getenv("SITE4_CHECK_INTERVAL", "10"))

# Shared settings
INTERNAL_RETRIES = 3
RETRY_BACKOFF = 15
MAX_BACKOFF = 60

# JSON data files
MAIN_BUTTONS_FILE = "main_buttons.json"
SUB_BUTTONS_FILE = "sub_buttons.json"
POOLS_FILE = "pools.json"
ASSIGNED_FILE = "assigned.json"
USERS_FILE = "users.json"

# SQLite database
DB_FILE = "wallet.db"
# ------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("sms_otp_bot")

# Sessions for login‑based sites
session1 = requests.Session()
session1.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE1_BASE_URL}/agent/SMSCDRReports",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "close",
    "Cache-Control": "no-cache",
})

session3 = requests.Session()
session3.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE3_BASE_URL}/agent/SMSCDRReports",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "close",
    "Cache-Control": "no-cache",
})

session4 = requests.Session()
session4.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE4_BASE_URL}/agent/SMSCDRStats",  # correct page
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "close",
    "Cache-Control": "no-cache",
})

last_get_number: Dict[int, float] = {}

# ----------------------------------------------------------------------
# JSON & SQLite helpers (unchanged)
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
# Helper to build inline keyboard with 2 buttons per row
# ----------------------------------------------------------------------
def build_menu_buttons(buttons: List[InlineKeyboardButton],
                       header_buttons: List[InlineKeyboardButton] = None,
                       footer_buttons: List[InlineKeyboardButton] = None) -> InlineKeyboardMarkup:
    menu = []
    if header_buttons:
        menu.append(header_buttons)
    for i in range(0, len(buttons), 2):
        row = buttons[i:i+2]
        menu.append(row)
    if footer_buttons:
        menu.append(footer_buttons)
    return InlineKeyboardMarkup(menu)

# ----------------------------------------------------------------------
# Site login (generic)
# ----------------------------------------------------------------------
def site_login(session, base_url, username, password, retries=3) -> bool:
    login_url = f"{base_url}/login"
    signin_url = f"{base_url}/signin"
    for attempt in range(1, retries + 1):
        logger.info(f"Login attempt {attempt}/{retries} for {base_url}")
        try:
            resp = session.get(login_url, timeout=30)
        except Exception as e:
            logger.error(f"Login page request failed for {base_url}: {e}")
            time.sleep(2)
            continue
        match = re.search(r"What is (\d+)\s*\+\s*(\d+)\s*=\s*\?\s*:", resp.text)
        if not match:
            logger.error(f"CAPTCHA question not found for {base_url}.")
            time.sleep(2)
            continue
        a, b = int(match.group(1)), int(match.group(2))
        answer = a + b
        logger.info(f"{base_url} CAPTCHA solved: {a} + {b} = {answer}")
        data = {"username": username, "password": password, "capt": str(answer)}
        try:
            resp = session.post(signin_url, data=data, allow_redirects=True, timeout=30)
        except Exception as e:
            logger.error(f"Login POST failed for {base_url}: {e}")
            time.sleep(2)
            continue
        if "Dashboard" in resp.text or "/agent/" in resp.url:
            logger.info(f"✅ Login successful for {base_url}.")
            try:
                session.get(f"{base_url}/agent/", timeout=15)
            except Exception:
                pass
            return True
        else:
            logger.error(f"Login failed for {base_url}.")
            time.sleep(2)
    logger.critical(f"All login attempts exhausted for {base_url}.")
    return False

# ----------------------------------------------------------------------
# Site 1 data fetcher (unchanged)
# ----------------------------------------------------------------------
def fetch_data_sync_site1(session) -> Optional[list]:
    base_url = SITE1_BASE_URL
    today = datetime.now()
    fdate1 = (today - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    fdate2 = (today + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    data_url = f"{base_url}/agent/res/data_smscdr.php"
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
            resp = session.get(data_url, params=params, timeout=30)
        except Exception as e:
            logger.warning(f"Data request attempt {attempt+1} for Site1 failed: {e}")
            time.sleep(2)
            continue
        if "login" in resp.url.lower():
            logger.warning("Session expired for Site1 – re‑login needed.")
            return None
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for Site1")
            time.sleep(2)
            continue
        try:
            json_data = resp.json()
        except Exception:
            logger.error(f"JSON decode failed for Site1. First 300 chars: {resp.text[:300]}")
            if "login" in resp.text.lower() and "password" in resp.text.lower():
                logger.warning("Response is login page.")
                return None
            time.sleep(2)
            continue
        rows = json_data.get("aaData")
        if rows is None:
            logger.info("No 'aaData' in response from Site1.")
            return []
        return rows
    logger.error("Data fetch failed after all retries for Site1.")
    return None

async def fetch_data_async_site1(session) -> Optional[list]:
    return await asyncio.to_thread(fetch_data_sync_site1, session)

# ----------------------------------------------------------------------
# Site 2 API fetcher (unchanged)
# ----------------------------------------------------------------------
def fetch_data_sync_site2_api() -> Optional[list]:
    token = SITE2_API_TOKEN
    if not token:
        logger.error("SITE2_API_TOKEN is not set. Cannot fetch Site2 data.")
        return None

    today = datetime.now()
    dt1 = (today - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    dt2 = (today + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")

    params = {
        "token": token,
        "dt1": dt1,
        "dt2": dt2,
        "records": 200
    }

    for attempt in range(1, INTERNAL_RETRIES + 1):
        try:
            resp = requests.get(SITE2_API_URL, params=params, timeout=30)
        except Exception as e:
            logger.warning(f"Site2 API request attempt {attempt} failed: {e}")
            time.sleep(2)
            continue

        if resp.status_code == 200:
            if "Error, you've accessed this site too many times" in resp.text:
                wait_match = re.search(r"Try again in (\d+) seconds?\.", resp.text)
                wait_seconds = int(wait_match.group(1)) if wait_match else 3
                logger.warning(f"Rate limit hit. Waiting {wait_seconds + 2}s before retry.")
                time.sleep(wait_seconds + 2)
                continue
            try:
                json_data = resp.json()
            except Exception:
                logger.error(f"JSON decode failed for Site2 API. Response: {resp.text[:300]}")
                time.sleep(2)
                continue

            rows = json_data.get("data") or json_data.get("aaData") or json_data
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                logger.error(f"Unexpected API response format: {type(rows)}")
                return None

            normalised = []
            number_keys = [
                "number", "Number", "phone", "Phone", "msisdn", "MSISDN",
                "destination", "Destination", "to", "To", "num", "Num"
            ]
            for row in rows:
                if isinstance(row, list):
                    normalised.append(row)
                elif isinstance(row, dict):
                    num_val = ""
                    for key in number_keys:
                        if key in row and row[key]:
                            num_val = str(row[key]).strip()
                            break
                    if not num_val:
                        continue
                    normalised.append([
                        row.get("date") or row.get("Date") or "",
                        row.get("range") or row.get("Range") or row.get("srange") or "",
                        num_val,
                        row.get("cli") or row.get("CLI") or row.get("sender") or "",
                        row.get("client") or row.get("Client") or "",
                        row.get("sms") or row.get("SMS") or row.get("message") or "",
                        row.get("currency") or row.get("Currency") or "",
                        row.get("my_payout") or row.get("MyPayout") or row.get("payout") or "",
                        row.get("client_payout") or row.get("ClientPayout") or ""
                    ])
                else:
                    continue
            return normalised
        else:
            logger.warning(f"Site2 API HTTP {resp.status_code}")
            time.sleep(2)
            continue

    logger.error("All API attempts failed for Site2.")
    return None

async def fetch_data_async_site2_api() -> Optional[list]:
    return await asyncio.to_thread(fetch_data_sync_site2_api)

# ----------------------------------------------------------------------
# Site 3 data fetcher (unchanged)
# ----------------------------------------------------------------------
def fetch_data_sync_site3(session) -> Optional[list]:
    base_url = SITE3_BASE_URL
    today = datetime.now()
    fdate1 = (today - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    fdate2 = (today + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    data_url = f"{base_url}/agent/res/data_smscdr.php"
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
            resp = session.get(data_url, params=params, timeout=30)
        except Exception as e:
            logger.warning(f"Data request attempt {attempt+1} for Site3 failed: {e}")
            time.sleep(2)
            continue
        if "login" in resp.url.lower():
            logger.warning("Session expired for Site3 – re‑login needed.")
            return None
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for Site3")
            time.sleep(2)
            continue
        try:
            json_data = resp.json()
        except Exception:
            logger.error(f"JSON decode failed for Site3. First 300 chars: {resp.text[:300]}")
            if "login" in resp.text.lower() and "password" in resp.text.lower():
                logger.warning("Response is login page.")
                return None
            time.sleep(2)
            continue
        rows = json_data.get("aaData")
        if rows is None:
            logger.info("No 'aaData' in response from Site3.")
            return []
        return rows
    logger.error("Data fetch failed after all retries for Site3.")
    return None

async def fetch_data_async_site3(session) -> Optional[list]:
    return await asyncio.to_thread(fetch_data_sync_site3, session)

# ----------------------------------------------------------------------
# Site 4 helpers: extract fresh csstr + fetch data with exact AJAX URL
# ----------------------------------------------------------------------
def get_site4_data_url(session, base_url) -> Optional[str]:
    """
    Load the SMSCDRStats page and extract the sAjaxSource URL (which
    already includes the csstr parameter).
    """
    stats_url = f"{base_url}/agent/SMSCDRStats"
    try:
        resp = session.get(stats_url, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Failed to load stats page, HTTP {resp.status_code}")
            return None
        # Find the sAjaxSource URL in the JavaScript
        match = re.search(r'"sAjaxSource":\s*"([^"]+)"', resp.text)
        if match:
            url = match.group(1)
            # The URL is relative to the page, make it absolute
            if url.startswith("res/"):
                full_url = f"{base_url}/agent/{url}"
            else:
                full_url = f"{base_url}/agent/{url}"
            logger.info(f"Extracted data URL for Site4: {full_url}")
            return full_url
        else:
            logger.error("sAjaxSource not found in stats page.")
            return None
    except Exception as e:
        logger.error(f"Error getting data URL: {e}")
        return None

def fetch_data_sync_site4_from_url(session, data_url) -> Optional[list]:
    """
    Fetch the data_smscdr.php using the exact URL from the page.
    No extra parameters added.
    """
    for attempt in range(INTERNAL_RETRIES):
        try:
            # Set Referer to the stats page to mimic browser
            session.headers["Referer"] = f"{SITE4_BASE_URL}/agent/SMSCDRStats"
            resp = session.get(data_url, timeout=30)
        except Exception as e:
            logger.warning(f"Data request attempt {attempt+1} for Site4 failed: {e}")
            time.sleep(2)
            continue
        if "login" in resp.url.lower():
            logger.warning("Session expired for Site4 – re‑login needed.")
            return None
        if resp.status_code == 403:
            logger.warning(f"HTTP 403 for Site4. URL: {data_url}")
            time.sleep(2)
            continue
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for Site4")
            time.sleep(2)
            continue
        try:
            json_data = resp.json()
        except Exception:
            logger.error(f"JSON decode failed for Site4. Response: {resp.text[:300]}")
            time.sleep(2)
            continue
        rows = json_data.get("aaData")
        if rows is None:
            logger.info("No 'aaData' in Site4 response.")
            return []
        return rows
    logger.error("Data fetch failed after all retries for Site4.")
    return None

async def fetch_data_async_site4(session, data_url) -> Optional[list]:
    return await asyncio.to_thread(fetch_data_sync_site4_from_url, session, data_url)

# ----------------------------------------------------------------------
# OTP extraction & seen pairs (unchanged)
# ----------------------------------------------------------------------
def extract_otp(sms_text: str) -> Optional[str]:
    if not isinstance(sms_text, str):
        return None
    match = re.search(r"#\s*((?:\d+\s*)+?)\s*is\s+your", sms_text)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match2 = re.search(r"#\s*(\d[\d\s]+)", sms_text)
    if match2:
        return re.sub(r"\s+", "", match2.group(1))
    return None

def load_seen_pairs(filename) -> Set[str]:
    if not os.path.exists(filename):
        return set()
    with open(filename, 'r') as f:
        return set(line.strip() for line in f if "|" in line)

def save_seen_pair(filename, number: str, otp: str):
    with open(filename, 'a') as f:
        f.write(f"{number}|{otp}\n")

def normalise_number(num: str) -> str:
    return num.strip().lstrip('+')

# ----------------------------------------------------------------------
# Formatting & sending (unchanged)
# ----------------------------------------------------------------------
def mask_number(num: str) -> str:
    if not num or not num.strip():
        return "Unknown"
    num = num.strip()
    if not num.startswith("+"):
        num = "+" + num
    if len(num) <= 7:
        return num[:3] + "***"
    return num[:4] + "*" * (len(num) - 7) + num[-3:]

async def send_otp_to_group(bot: Bot, row: list, otp: str, site_label: str = ""):
    number = str(row[2]).strip()
    cli = str(row[3]).strip() if len(row) > 3 else ""
    sms = str(row[5]).strip() if len(row) > 5 else ""
    masked = mask_number(number)
    prefix = f"[{site_label}] " if site_label else ""
    text = (
        f"{prefix}✅ New message received!\n\n"
        f"🏢 CLI : {cli}\n"
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

async def send_otp_to_user(bot: Bot, user_id: int, row: list, otp: str,
                           old_balance: float, new_balance: float, site_label: str = ""):
    number = str(row[2]).strip()
    sms = str(row[5]).strip() if len(row) > 5 else ""
    if not number.startswith("+"):
        number = "+" + number
    prefix = f"[{site_label}] " if site_label else ""
    text = (
        f"{prefix}📩 <b>Message Received!</b>\n\n"
        f"📞 Number : <code>{number}</code>\n\n"
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
# Monitors (unchanged for Site1-3, only Site4 rewritten)
# ----------------------------------------------------------------------
async def monitor_site1(application: Application):
    session = session1
    base_url = SITE1_BASE_URL
    username = SITE1_USERNAME
    password = SITE1_PASSWORD
    seen_file = "seen_pairs_site1.txt"
    label = "Site1"
    check_interval = SITE1_CHECK_INTERVAL
    bot = application.bot

    if not site_login(session, base_url, username, password):
        logger.critical(f"Initial login failed for {label}.")

    seen_pairs = load_seen_pairs(seen_file)
    rows = await fetch_data_async_site1(session)
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
                save_seen_pair(seen_file, number, otp)
        logger.info(f"[{label}] Initialized with {len(seen_pairs)} known OTP pairs.")
    else:
        logger.warning(f"[{label}] Initial data fetch returned no rows.")

    consecutive_failures = 0
    while True:
        rows = await fetch_data_async_site1(session)
        if rows is None:
            logger.warning(f"[{label}] Data fetch failed. Re‑login required.")
            if site_login(session, base_url, username, password):
                logger.info(f"[{label}] Re‑login succeeded. Retrying fetch...")
                rows = await fetch_data_async_site1(session)
                if rows is not None:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            else:
                consecutive_failures += 1
            if rows is None:
                backoff = min(RETRY_BACKOFF * consecutive_failures, MAX_BACKOFF)
                logger.info(f"[{label}] Waiting {backoff}s before next attempt.")
                await asyncio.sleep(backoff)
                continue
        else:
            consecutive_failures = 0

        assigned = load_assigned()
        normalised_assigned = {normalise_number(k): v for k, v in assigned.items()}
        per_otp = float(get_setting("per_otp_bdt", "0.30"))
        new_otp_count = 0
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
            save_seen_pair(seen_file, number, otp)
            new_otp_count += 1
            tasks = [send_otp_to_group(bot, row, otp, site_label="")]
            user_id = normalised_assigned.get(normalise_number(number))
            if user_id:
                old_balance = get_user_balance(user_id)
                credit_user(user_id, per_otp)
                new_balance = get_user_balance(user_id)
                tasks.append(send_otp_to_user(bot, user_id, row, otp, old_balance, new_balance, site_label=""))
            await asyncio.gather(*tasks)
        if new_otp_count > 0:
            logger.info(f"[{label}] 📨 {new_otp_count} new OTP(s) processed.")
        await asyncio.sleep(check_interval)

async def monitor_site2(application: Application):
    seen_file = "seen_pairs_site2.txt"
    label = "Site2"
    check_interval = SITE2_CHECK_INTERVAL
    bot = application.bot

    seen_pairs = load_seen_pairs(seen_file)
    rows = await fetch_data_async_site2_api()
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
                save_seen_pair(seen_file, number, otp)
        logger.info(f"[{label}] Initialized with {len(seen_pairs)} known OTP pairs (API).")
    else:
        logger.warning(f"[{label}] Initial API fetch returned no rows. Will keep trying.")

    consecutive_failures = 0
    while True:
        rows = await fetch_data_async_site2_api()
        if rows is None:
            consecutive_failures += 1
            backoff = min(RETRY_BACKOFF * consecutive_failures, MAX_BACKOFF)
            logger.warning(f"[{label}] API fetch failed. Consecutive: {consecutive_failures}. Waiting {backoff}s.")
            await asyncio.sleep(backoff)
            continue
        else:
            consecutive_failures = 0

        assigned = load_assigned()
        normalised_assigned = {normalise_number(k): v for k, v in assigned.items()}
        per_otp = float(get_setting("per_otp_bdt", "0.30"))
        new_otp_count = 0
        for row in rows:
            if len(row) < 9: continue
            sms_text = str(row[5])
            if "#" not in sms_text: continue
            otp = extract_otp(sms_text)
            if not otp: continue
            number = str(row[2]).strip()
            if not number:
                continue
            pair = f"{number}|{otp}"
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            save_seen_pair(seen_file, number, otp)
            new_otp_count += 1
            tasks = [send_otp_to_group(bot, row, otp, site_label="")]
            user_id = normalised_assigned.get(normalise_number(number))
            if user_id:
                old_balance = get_user_balance(user_id)
                credit_user(user_id, per_otp)
                new_balance = get_user_balance(user_id)
                tasks.append(send_otp_to_user(bot, user_id, row, otp, old_balance, new_balance, site_label=""))
            await asyncio.gather(*tasks)
        if new_otp_count > 0:
            logger.info(f"[{label}] 📨 {new_otp_count} new OTP(s) processed.")
        await asyncio.sleep(check_interval)

async def monitor_site3(application: Application):
    session = session3
    base_url = SITE3_BASE_URL
    username = SITE3_USERNAME
    password = SITE3_PASSWORD
    seen_file = "seen_pairs_site3.txt"
    label = "Site3"
    check_interval = SITE3_CHECK_INTERVAL
    bot = application.bot

    if not site_login(session, base_url, username, password):
        logger.critical(f"Initial login failed for {label}.")

    seen_pairs = load_seen_pairs(seen_file)
    rows = await fetch_data_async_site3(session)
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
                save_seen_pair(seen_file, number, otp)
        logger.info(f"[{label}] Initialized with {len(seen_pairs)} known OTP pairs.")
    else:
        logger.warning(f"[{label}] Initial data fetch returned no rows.")

    consecutive_failures = 0
    while True:
        rows = await fetch_data_async_site3(session)
        if rows is None:
            logger.warning(f"[{label}] Data fetch failed. Re‑login required.")
            if site_login(session, base_url, username, password):
                logger.info(f"[{label}] Re‑login succeeded. Retrying fetch...")
                rows = await fetch_data_async_site3(session)
                if rows is not None:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            else:
                consecutive_failures += 1
            if rows is None:
                backoff = min(RETRY_BACKOFF * consecutive_failures, MAX_BACKOFF)
                logger.info(f"[{label}] Waiting {backoff}s before next attempt.")
                await asyncio.sleep(backoff)
                continue
        else:
            consecutive_failures = 0

        assigned = load_assigned()
        normalised_assigned = {normalise_number(k): v for k, v in assigned.items()}
        per_otp = float(get_setting("per_otp_bdt", "0.30"))
        new_otp_count = 0
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
            save_seen_pair(seen_file, number, otp)
            new_otp_count += 1
            tasks = [send_otp_to_group(bot, row, otp, site_label="Site3")]
            user_id = normalised_assigned.get(normalise_number(number))
            if user_id:
                old_balance = get_user_balance(user_id)
                credit_user(user_id, per_otp)
                new_balance = get_user_balance(user_id)
                tasks.append(send_otp_to_user(bot, user_id, row, otp, old_balance, new_balance, site_label="Site3"))
            await asyncio.gather(*tasks)
        if new_otp_count > 0:
            logger.info(f"[{label}] 📨 {new_otp_count} new OTP(s) processed.")
        await asyncio.sleep(check_interval)

async def monitor_site4(application: Application):
    session = session4
    base_url = SITE4_BASE_URL
    username = SITE4_USERNAME
    password = SITE4_PASSWORD
    seen_file = "seen_pairs_site4.txt"
    label = "Site4"
    check_interval = SITE4_CHECK_INTERVAL
    bot = application.bot
    data_url = None

    if not site_login(session, base_url, username, password):
        logger.critical(f"Initial login failed for {label}.")
    else:
        data_url = get_site4_data_url(session, base_url)

    seen_pairs = load_seen_pairs(seen_file)
    if data_url:
        rows = await fetch_data_async_site4(session, data_url)
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
                    save_seen_pair(seen_file, number, otp)
            logger.info(f"[{label}] Initialized with {len(seen_pairs)} known OTP pairs.")
    else:
        logger.warning(f"[{label}] Could not get initial data URL. Will retry.")

    consecutive_failures = 0
    while True:
        if not data_url:
            logger.info(f"[{label}] Data URL missing, attempting re‑login.")
            if site_login(session, base_url, username, password):
                data_url = get_site4_data_url(session, base_url)
                if data_url:
                    consecutive_failures = 0
                    logger.info(f"[{label}] Re‑login and URL extraction successful.")
                else:
                    consecutive_failures += 1
            else:
                consecutive_failures += 1
            if not data_url:
                backoff = min(RETRY_BACKOFF * consecutive_failures, MAX_BACKOFF)
                logger.info(f"[{label}] Still no data URL. Waiting {backoff}s.")
                await asyncio.sleep(backoff)
                continue

        rows = await fetch_data_async_site4(session, data_url)

        if rows is None:
            logger.warning(f"[{label}] Data fetch failed. Invalidating data URL.")
            data_url = None  # force refresh on next loop
            consecutive_failures += 1
            backoff = min(RETRY_BACKOFF * consecutive_failures, MAX_BACKOFF)
            logger.info(f"[{label}] Waiting {backoff}s before next attempt.")
            await asyncio.sleep(backoff)
            continue
        else:
            consecutive_failures = 0

        assigned = load_assigned()
        normalised_assigned = {normalise_number(k): v for k, v in assigned.items()}
        per_otp = float(get_setting("per_otp_bdt", "0.30"))
        new_otp_count = 0
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
            save_seen_pair(seen_file, number, otp)
            new_otp_count += 1
            tasks = [send_otp_to_group(bot, row, otp, site_label="Site4")]
            user_id = normalised_assigned.get(normalise_number(number))
            if user_id:
                old_balance = get_user_balance(user_id)
                credit_user(user_id, per_otp)
                new_balance = get_user_balance(user_id)
                tasks.append(send_otp_to_user(bot, user_id, row, otp, old_balance, new_balance, site_label="Site4"))
            await asyncio.gather(*tasks)
        if new_otp_count > 0:
            logger.info(f"[{label}] 📨 {new_otp_count} new OTP(s) processed.")
        await asyncio.sleep(check_interval)

# ----------------------------------------------------------------------
# Rate limiting (unchanged)
# ----------------------------------------------------------------------
def check_get_number_rate_limit(user_id):
    now = time.time()
    last = last_get_number.get(user_id, 0)
    if now - last < 10:
        return False, 10 - int(now - last)
    last_get_number[user_id] = now
    return True, 0

# ----------------------------------------------------------------------
# Telegram handlers (unchanged)
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

async def get_number_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are temporarily banned for 5 minutes due to flooding.")
        return
    mains = load_main_buttons()
    if not mains:
        await update.message.reply_text("No main buttons available.")
        return
    buttons = [InlineKeyboardButton(name, callback_data=f"get_main:{name}") for name in mains]
    keyboard = build_menu_buttons(buttons)
    await update.message.reply_text("Choose a service:", reply_markup=keyboard)

async def get_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    main_name = query.data.split(":",1)[1]
    subs = load_sub_buttons().get(main_name, [])
    if not subs:
        pool_key = main_name
        pools = load_pools()
        numbers = pools.get(pool_key, [])
        if not numbers:
            await query.edit_message_text("No numbers available for this service.")
            return
        assigned_number = numbers.pop(0)
        pools[pool_key] = numbers
        save_pools(pools)
        assigned = load_assigned()
        assigned[assigned_number] = query.from_user.id
        save_assigned(assigned)
        context.user_data["last_main"] = main_name
        context.user_data["last_sub"] = None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Copy Number", copy_text=CopyTextButton(text=assigned_number))],
            [InlineKeyboardButton("Change Number", callback_data=f"change_number:{main_name}:"),
             InlineKeyboardButton("OTP Group", url="https://t.me/otpservers")]
        ])
        await query.edit_message_text(
            f"New 𝗡𝘂𝗺𝗯𝗲𝗿 𝗔𝘀𝘀𝗶𝗴𝗻𝗲𝗱!\n\n{assigned_number}\n\nWaiting for OTP ...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        return

    buttons = [InlineKeyboardButton(sub, callback_data=f"get_sub:{main_name}:{sub}") for sub in subs]
    keyboard = build_menu_buttons(buttons)
    await query.edit_message_text(f"Select a sub‑category for {main_name}:", reply_markup=keyboard)

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
    parts = query.data.split(":",2)
    main_name = parts[1]
    sub_name = parts[2] if len(parts) > 2 else None
    if sub_name:
        await assign_number_and_display(query, main_name, sub_name, user_id, context)
    else:
        pool_key = main_name
        pools = load_pools()
        numbers = pools.get(pool_key, [])
        if not numbers:
            await query.edit_message_text("No numbers available for this service.")
            return
        assigned_number = numbers.pop(0)
        pools[pool_key] = numbers
        save_pools(pools)
        assigned = load_assigned()
        assigned[assigned_number] = user_id
        save_assigned(assigned)
        context.user_data["last_main"] = main_name
        context.user_data["last_sub"] = None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Copy Number", copy_text=CopyTextButton(text=assigned_number))],
            [InlineKeyboardButton("Change Number", callback_data=f"change_number:{main_name}:"),
             InlineKeyboardButton("OTP Group", url="https://t.me/otpservers")]
        ])
        await query.edit_message_text(
            f"New 𝗡𝘂𝗺𝗯𝗲𝗿 𝗔𝘀𝘀𝗶𝗴𝗻𝗲𝗱!\n\n{assigned_number}\n\nWaiting for OTP ...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        return

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
    text = f"New 𝗡𝘂𝗺𝗯𝗲𝗿 𝗔𝘀𝘀𝗶𝗴𝗻𝗲𝗱!\n\n{assigned_number}\n\nWaiting for OTP ..."
    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

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
            pools.pop(main_name, None)
            save_pools(pools)
        await query.edit_message_text(f"Main button '{main_name}' and its sub buttons removed.")
    else:
        await query.edit_message_text("Not found.")
    return ConversationHandler.END

UPLOAD_MAIN_SELECT, UPLOAD_SUB_OPTION, UPLOAD_FILE = range(100, 103)
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
    await update.message.reply_text("Select main button for upload:", reply_markup=InlineKeyboardMarkup(keyboard))
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
    if subs:
        buttons = [[InlineKeyboardButton("Upload to main directly", callback_data=f"upload_direct_main:{main_name}")]]
        for sub in subs:
            buttons.append([InlineKeyboardButton(f"Sub: {sub}", callback_data=f"upload_sub:{main_name}:{sub}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="cancel_upload")])
        keyboard = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(f"Where to upload numbers for '{main_name}'?", reply_markup=keyboard)
        return UPLOAD_SUB_OPTION
    else:
        context.user_data["upload_sub"] = None
        await query.edit_message_text(f"Send a .txt file with numbers (one per line) for '{main_name}'.")
        return UPLOAD_FILE

async def upload_sub_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_upload":
        await query.edit_message_text("Cancelled.")
        return await back_to_profile(update, context)
    data = query.data
    if data.startswith("upload_direct_main:"):
        main_name = data.split(":",2)[1]
        context.user_data["upload_main"] = main_name
        context.user_data["upload_sub"] = None
        await query.edit_message_text(f"Send a .txt file with numbers (one per line) for '{main_name}'.")
        return UPLOAD_FILE
    elif data.startswith("upload_sub:"):
        _, main_name, sub_name = data.split(":",2)
        context.user_data["upload_main"] = main_name
        context.user_data["upload_sub"] = sub_name
        await query.edit_message_text(f"Send a .txt file with numbers for {main_name} / {sub_name}.")
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
    sub_name = context.user_data.get("upload_sub")
    pool_key = f"{main_name}_{sub_name}" if sub_name else main_name
    pools = load_pools()
    if pool_key not in pools:
        pools[pool_key] = []
    pools[pool_key].extend(numbers)
    save_pools(pools)
    try:
        desc = f"{main_name} / {sub_name}" if sub_name else main_name
        await update.message.bot.send_message(GROUP_CHAT_ID, f"{desc}‑এ {len(numbers)} টি নাম্বার যোগ করা হয়েছে।")
    except Exception as e:
        logger.error(f"Broadcast upload notification failed: {e}")
    await update.message.reply_text(f"Added {len(numbers)} numbers to {desc}.", reply_markup=ReplyKeyboardMarkup(admin_profile_kb(), resize_keyboard=True))
    return ConversationHandler.END

BROADCAST_RECEIVE, BROADCAST_CONFIRM = range(2)
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    await update.message.reply_text("Send the content you want to broadcast (text, photo, video, file).", reply_markup=ReplyKeyboardMarkup([["⬅️ Back"]], resize_keyboard=True))
    return BROADCAST_RECEIVE

async def broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text == "⬅️ Back":
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

def admin_profile_kb():
    return [
        ["💰 Balance", "📋 Pending"],
        ["✅ Approved", "✏️ Edit"],
        ["📢 Broadcast", "Upload"],
        ["Add/Remove Main Button"],
        ["⬅️ Back"]
    ]

async def back_to_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    kb = admin_profile_kb() if is_admin(user_id) else [["💰 Balance", "📋 Withdraw History"], ["⬅️ Back"]]
    await update.message.reply_text("👤 Profile Menu", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await back_to_profile(update, context)

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
    elif text == "Add/Remove Main Button" and is_admin(user_id):
        return await add_remove_main(update, context)
    elif text == "📢 Broadcast" and is_admin(user_id):
        return await broadcast_start(update, context)
    else:
        return PROFILE_SELECT

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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^Get Number$"), get_number_start))
    application.add_handler(CallbackQueryHandler(get_main_callback, pattern="^get_main:"))
    application.add_handler(CallbackQueryHandler(get_sub_callback, pattern="^get_sub:"))
    application.add_handler(CallbackQueryHandler(change_number_callback, pattern="^change_number:"))
    application.add_handler(CallbackQueryHandler(admin_complete_callback, pattern="^admin_complete_"))

    fake_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Fake Name$"), fake_name_start)],
        states={FAKE_GENDER: [CallbackQueryHandler(fake_gender_select, pattern="^fake_")]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(fake_conv)

    get2fa_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Get 2FA$"), get2fa_start)],
        states={GET2FA_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, get2fa_generate)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(get2fa_conv)

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

    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Broadcast$"), broadcast_start)],
        states={
            BROADCAST_RECEIVE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_receive)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_confirm, pattern="^broadcast_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(broadcast_conv)

    profile_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👤 My Profile$"), profile_start)],
        states={
            PROFILE_SELECT: [
                MessageHandler(filters.Regex("^(💰 Balance|📋 Pending|✅ Approved|📋 Withdraw History|✏️ Edit|Upload|📢 Broadcast|Add/Remove Main Button|⬅️ Back)$"), profile_select),
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
            UPLOAD_SUB_OPTION: [
                CallbackQueryHandler(upload_sub_option_callback, pattern="^(upload_direct_main:|upload_sub:|cancel_upload$)")
            ],
            UPLOAD_FILE: [
                MessageHandler(filters.Document.ALL, upload_file_receive)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(profile_conv)

    async def post_init(app: Application):
        asyncio.create_task(monitor_site1(app))
        asyncio.create_task(monitor_site2(app))
        asyncio.create_task(monitor_site3(app))
        asyncio.create_task(monitor_site4(app))   # ← fixed Site4

    application.post_init = post_init

    logger.info("Bot started with quad monitoring (Site1, Site2 API, Site3, Site4 fixed). Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()