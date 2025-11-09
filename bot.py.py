#!/usr/bin/env python3
"""
bot.py — Telegram bot with automatic inline "完整账单" button under every summary.
Set TOKEN in environment before running.

Usage:
  export TOKEN="123456:ABC..."   (Linux/macOS)
  python3 bot.py
"""

import os
import sys
import re
import io
import sqlite3
import datetime
import logging
import pytz
import ast
import operator as op

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler

# ====== CONFIG ======
TOKEN = os.environ.get("TOKEN", "8270868449:AAGbRTkqWfDhZqpt_3e_sYT1G0MwzBVZ8-w")
DB_PATH = "tx.db"
LAST_N = 5

# built-in admins — बदलना हो तो यहाँ कर लो
ADMINS = {6603524612, 7773526534, 8157411319}
authorized_users = set(ADMINS)

# per-chat caches
exchange_rates = {}
fee_rates = {}

# timezone
IST = pytz.timezone("Asia/Kolkata")

# logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== safe arithmetic evaluator ======
_ALLOWED_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}

def safe_eval_arith(expr: str) -> float:
    if not re.match(r'^[0-9\.\+\-\*/\(\) \t]+$', expr):
        raise ValueError("Invalid characters in expression")
    def _eval(node):
        if isinstance(node, ast.Num):
            return node.n
        if hasattr(ast, "Constant") and isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Invalid constant")
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            opfunc = _ALLOWED_OPS.get(type(node.op))
            if opfunc is None:
                raise ValueError("Operator not allowed")
            return opfunc(left, right)
        if isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            opfunc = _ALLOWED_OPS.get(type(node.op))
            if opfunc is None:
                raise ValueError("Unary operator not allowed")
            return opfunc(operand)
        raise ValueError("Expression not allowed")
    parsed = ast.parse(expr, mode='eval')
    return float(_eval(parsed.body))

# ====== DB helpers ======
def _db_connect():
    con = sqlite3.connect(DB_PATH, timeout=10, detect_types=sqlite3.PARSE_DECLTYPES)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = _db_connect()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user TEXT,
        type TEXT,
        amount_inr REAL,
        amount_usd REAL,
        time_iso TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settings (
        chat_id INTEGER PRIMARY KEY,
        exchange_rate REAL,
        fee_rate REAL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    )""")
    for a in ADMINS:
        cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (int(a),))
    cur.execute("SELECT user_id FROM admins")
    for r in cur.fetchall():
        try:
            authorized_users.add(int(r[0]))
        except:
            pass
    cur.execute("SELECT chat_id, exchange_rate, fee_rate FROM settings")
    for (cid, er, fr) in cur.fetchall():
        try:
            if er is not None:
                exchange_rates[int(cid)] = float(er)
            if fr is not None:
                fee_rates[int(cid)] = float(fr)
        except:
            pass
    con.commit()
    con.close()
    logger.info("DB initialized at %s", DB_PATH)

def persist_setting(chat_id, exchange_rate=None, fee_rate=None):
    con = _db_connect(); cur = con.cursor()
    cur.execute("SELECT 1 FROM settings WHERE chat_id=?", (chat_id,))
    exists = cur.fetchone()
    if exists:
        if exchange_rate is not None:
            cur.execute("UPDATE settings SET exchange_rate=? WHERE chat_id=?", (exchange_rate, chat_id))
        if fee_rate is not None:
            cur.execute("UPDATE settings SET fee_rate=? WHERE chat_id=?", (fee_rate, chat_id))
    else:
        cur.execute("INSERT INTO.settings(chat_id, exchange_rate, fee_rate) VALUES (?,?,?)".replace('.', ''),
                    (chat_id, exchange_rate if exchange_rate is not None else 106.0, fee_rate if fee_rate is not None else 0.0))
        # note: above replace('.') is a tiny safety to avoid accidental dot in SQL string in some editors
    # fallback correct implementation if previous line odd:
    try:
        con.commit()
    except:
        # normal insert/update handled earlier - ensure commit
        con.commit()
    con.close()

def persist_admin(user_id):
    con = _db_connect(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (int(user_id),))
    con.commit(); con.close()

def remove_admin_persist(user_id):
    con = _db_connect(); cur = con.cursor()
    cur.execute("DELETE FROM admins WHERE user_id=?", (int(user_id),))
    con.commit(); con.close()

def add_tx_db(chat_id, user, type_, amount_inr, amount_usd):
    con = _db_connect(); cur = con.cursor()
    cur.execute(
        "INSERT INTO transactions (chat_id,user,type,amount_inr,amount_usd,time_iso) VALUES (?,?,?,?,?,?)",
        (chat_id, user, type_, float(amount_inr), float(amount_usd), datetime.datetime.utcnow().isoformat())
    )
    con.commit(); con.close()

def get_transactions_between(chat_id, from_dt_utc, to_dt_utc):
    con = _db_connect(); cur = con.cursor()
    cur.execute("""SELECT time_iso, amount_inr, amount_usd, user, type
                   FROM transactions
                   WHERE chat_id=? AND time_iso BETWEEN ? AND ?
                   ORDER BY id ASC""",
                (chat_id, from_dt_utc.isoformat(), to_dt_utc.isoformat()))
    rows = cur.fetchall(); con.close()
    return rows

# ====== helpers ======
def is_authorized(user_id):
    return int(user_id) in authorized_users

def get_exchange_rate(chat_id):
    if chat_id in exchange_rates:
        return float(exchange_rates[chat_id])
    try:
        con = _db_connect(); cur = con.cursor()
        cur.execute("SELECT exchange_rate FROM settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone(); con.close()
        if row and row[0] is not None:
            exchange_rates[chat_id] = float(row[0]); return float(row[0])
    except:
        pass
    return 106.0

def set_exchange_rate(chat_id, rate):
    exchange_rates[chat_id] = float(rate); persist_setting(chat_id, exchange_rate=float(rate))

def get_fee_rate(chat_id):
    if chat_id in fee_rates:
        return float(fee_rates[chat_id])
    try:
        con = _db_connect(); cur = con.cursor()
        cur.execute("SELECT fee_rate FROM settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone(); con.close()
        if row and row[0] is not None:
            fee_rates[chat_id] = float(row[0]); return float(row[0])
    except:
        pass
    return 0.0

def set_fee_rate(chat_id, fee):
    fee_rates[chat_id] = float(fee); persist_setting(chat_id, fee_rate=float(fee))

def add_admin(user_id):
    authorized_users.add(int(user_id)); persist_admin(user_id)

def remove_admin(user_id):
    try:
        authorized_users.discard(int(user_id))
        remove_admin_persist(user_id)
    except:
        pass

# ====== day bounds ======
def _ist_bounds_for_today():
    now_ist = datetime.datetime.now(IST)
    today_ist = now_ist.date()
    ist_from = IST.localize(datetime.datetime.combine(today_ist, datetime.time(hour=8, minute=30)))
    ist_to = ist_from + datetime.timedelta(days=1)
    from_utc = ist_from.astimezone(pytz.utc)
    to_utc = ist_to.astimezone(pytz.utc)
    return from_utc, to_utc

# ====== formatting helpers (no thousands commas anywhere) ======
def fmt_inr_plain(x):
    x = float(x)
    if abs(x - int(x)) < 0.005:
        return f"{int(x)}"
    return f"{x:.2f}"

def fmt_usd(x):
    return f"{float(x):.2f}U"

# ====== build clean (normal text) message ======
def build_compact_message(chat_id):
    rate = get_exchange_rate(chat_id)
    fee = get_fee_rate(chat_id)

    from_dt, to_dt = _ist_bounds_for_today()
    rows = get_transactions_between(chat_id, from_dt, to_dt)
    incomes_all = [r for r in rows if r[4] == "income"]
    payouts_all = [r for r in rows if r[4] == "payout"]

    inc_count = len(incomes_all)
    pay_count = len(payouts_all)

    show_n = LAST_N if LAST_N > 0 else 5
    incomes_show = incomes_all[-show_n:] if incomes_all else []
    payouts_show = payouts_all[-show_n:] if payouts_all else []

    def fmt_time(tiso):
        try:
            dt = datetime.datetime.fromisoformat(tiso)
            dt = dt.replace(tzinfo=pytz.utc).astimezone(IST)
            return dt.strftime("%H:%M:%S")
        except:
            return tiso

    income_lines = []
    for r in incomes_show:
        t = fmt_time(r[0])
        inr = fmt_inr_plain(r[1])
        usd = fmt_usd(r[2])
        user = r[3]
        income_lines.append(f"{t}  {inr} / {rate} = {usd}  {user}")
    if not income_lines:
        income_lines = ["None"]

    payout_lines = []
    for r in payouts_show:
        t = fmt_time(r[0])
        usd = fmt_usd(r[2])
        inr = fmt_inr_plain(r[1])
        user = r[3]
        payout_lines.append(f"{t}  {usd} ({inr})  {user}")
    if not payout_lines:
        payout_lines = ["None"]

    total_income_inr = sum(float(r[1]) for r in incomes_all)
    total_income_usd = sum(float(r[2]) for r in incomes_all)
    total_payout_inr = sum(float(r[1]) for r in payouts_all)
    total_payout_usd = sum(float(r[2]) for r in payouts_all)
    not_yet_inr = total_income_inr - total_payout_inr
    not_yet_usd = total_income_usd - total_payout_usd

    parts = []
    parts.append(f"Today's Income ({inc_count})")
    parts.extend(income_lines)
    parts.append("")
    parts.append(f"Today's Issued ({pay_count})")
    parts.extend(payout_lines)
    parts.append("")
    parts.append(f"Total Income : {fmt_inr_plain(total_income_inr)} | {fmt_usd(total_income_usd)}")
    parts.append(f"Exchange Rate : {rate}")
    parts.append(f"Fee Rate : {fee}%")
    parts.append("")
    parts.append(f"Should be issued : {fmt_inr_plain(total_payout_inr)} | {fmt_usd(total_payout_usd)}")
    parts.append(f"Already issued : {fmt_inr_plain(total_income_inr)} | {fmt_usd(total_income_usd)}")
    parts.append(f"Not yet issued : {fmt_inr_plain(not_yet_inr)} | {fmt_usd(not_yet_usd)}")
    parts.append("")
    return "\n".join(parts)

# ====== helper: send summary + inline button (Chinese-style label) ======
def send_summary_with_button(update: Update, context: CallbackContext, chat_id: int):
    text = build_compact_message(chat_id)
    # Chinese-style button label like screenshot: "🌐 完整账单"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 完整账单", callback_data="VIEWFULL")]])
    update.message.reply_text(text, reply_markup=kb)

# ====== commands ======
def start(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Bot ready. Use +<expr> for income (e.g. +100*1.07), T<usd> for payout (e.g. T34.59). /summary /setrate /setfee /addadmin /deladmin")

def summary_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ You are not authorized.")
    chat_id = update.effective_chat.id
    send_summary_with_button(update, context, chat_id)

def viewfull_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ You are not authorized.")
    chat_id = update.effective_chat.id
    from_dt, to_dt = _ist_bounds_for_today()
    rows = get_transactions_between(chat_id, from_dt, to_dt)
    lines = []
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(r[0]).replace(tzinfo=pytz.utc).astimezone(IST)
            timestr = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            timestr = r[0]
        lines.append(f"{timestr} | {r[4]} | INR {fmt_inr_plain(r[1])} | USD {fmt_usd(r[2])} | {r[3]}")
    full_text = "\n".join(lines) or "No transactions for today."
    bio = io.BytesIO(); bio.write(full_text.encode("utf-8")); bio.seek(0)
    filename = f"report_{chat_id}_{datetime.date.today().isoformat()}.txt"
    update.message.reply_document(document=bio, filename=filename)
    bio.close()

# ---- callback for the button (send file & edit message) ----
def viewfull_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user = query.from_user
    chat = query.message.chat
    chat_id = chat.id

    # acknowledge quickly (keeps the button from showing 'loading' forever)
    try:
        query.answer(text="Sending report...")
    except:
        try:
            query.answer()
        except:
            pass

    # authorization check
    if not is_authorized(user.id):
        # remove button but keep the summary text visible
        try:
            query.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        try:
            query.message.reply_text("You are not authorized to download this report.")
        except:
            pass
        return

    # Build the full report text (same as /viewfull)
    from_dt, to_dt = _ist_bounds_for_today()
    rows = get_transactions_between(chat_id, from_dt, to_dt)
    lines = []
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(r[0]).replace(tzinfo=pytz.utc).astimezone(IST)
            timestr = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            timestr = r[0]
        lines.append(f"{timestr} | {r[4]} | INR {fmt_inr_plain(r[1])} | USD {fmt_usd(r[2])} | {r[3]}")
    full_text = "\n".join(lines) or "No transactions for today."

    bio = io.BytesIO()
    bio.write(full_text.encode("utf-8"))
    bio.seek(0)
    filename = f"report_{chat_id}_{datetime.date.today().isoformat()}.txt"

    sent = False
    try:
        # try to send into the same chat first
        context.bot.send_document(chat_id=chat_id, document=bio, filename=filename)
        sent = True
    except Exception as e:
        logger.warning("Failed to send report to chat %s: %s", chat_id, e)
        try:
            bio.seek(0)
            context.bot.send_document(chat_id=user.id, document=bio, filename=filename)
            sent = True
        except Exception as e2:
            logger.exception("Failed to send report to user %s: %s", user.id, e2)

    bio.close()

    # remove the inline button only; keep the summary text intact
    try:
        query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # optionally notify the clicking admin (small ephemeral toast already done by query.answer)
    # if you prefer a visible confirmation in chat, uncomment next lines:
    # if sent:
    #     try:
    #         context.bot.send_message(chat_id=chat_id, text="Report has been sent ✅")
    #     except:
    #         pass


# ====== admin & helper commands ======
def whoami_cmd(update: Update, context: CallbackContext):
    uid = update.effective_user.id; cid = update.effective_chat.id; uname = update.effective_user.first_name
    update.message.reply_text(f"Your user_id: {uid}\nchat_id: {cid}\nname: {uname}")

def clear_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ You are not authorized.")
    chat_id = update.effective_chat.id
    con = _db_connect(); cur = con.cursor()
    cur.execute("DELETE FROM transactions WHERE chat_id=?", (chat_id,))
    con.commit(); con.close()
    return update.message.reply_text("✅ All transactions cleared for this chat.")

def dbpeek_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ You are not authorized.")
    chat_id = update.effective_chat.id
    con = _db_connect(); cur = con.cursor()
    cur.execute("SELECT time_iso, amount_inr, amount_usd, user, type FROM transactions WHERE chat_id=? ORDER BY id DESC LIMIT 50", (chat_id,))
    rows = cur.fetchall(); con.close()
    if not rows:
        return update.message.reply_text("No transactions found for this chat_id.")
    text = "Last transactions for this chat:\n"
    for r in rows:
        text += f"{r[0]} | {r[4]} | INR={fmt_inr_plain(r[1])} | USD={fmt_usd(r[2])} | {r[3]}\n"
    update.message.reply_text(text)

def setrate_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ Not authorized.")
    chat_id = update.effective_chat.id
    if not context.args:
        return update.message.reply_text(f"Current rate: {get_exchange_rate(chat_id)}")
    try:
        rate = float(context.args[0]); set_exchange_rate(chat_id, rate)
        update.message.reply_text(f"✅ Exchange rate set to {rate}")
    except:
        update.message.reply_text("⚠️ Invalid rate. Use: /setrate 106.5")

def getrate_cmd(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    update.message.reply_text(f"Exchange rate: {get_exchange_rate(chat_id)}")

def setfee_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ Not authorized.")
    chat_id = update.effective_chat.id
    if not context.args:
        return update.message.reply_text(f"Current fee: {get_fee_rate(chat_id)}%")
    try:
        fee = float(context.args[0]); set_fee_rate(chat_id, fee)
        update.message.reply_text(f"✅ Fee set to {fee}%")
    except:
        update.message.reply_text("⚠️ Invalid fee. Use: /setfee 1.5")

def addadmin_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ Not authorized.")
    try:
        uid = int(context.args[0]); add_admin(uid)
        update.message.reply_text(f"✅ Added admin {uid}")
    except:
        update.message.reply_text("⚠️ Usage: /addadmin <user_id>")

def deladmin_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ Not authorized.")
    try:
        uid = int(context.args[0])
        if uid in ADMINS:
            return update.message.reply_text("❌ Cannot remove built-in admin.")
        remove_admin(uid); update.message.reply_text(f"✅ Removed admin {uid}")
    except:
        update.message.reply_text("⚠️ Usage: /deladmin <user_id>")

# ====== daily reset ======
def daily_reset(context: CallbackContext):
    con = _db_connect(); cur = con.cursor()
    cur.execute("SELECT DISTINCT chat_id FROM transactions")
    chat_ids = {r[0] for r in cur.fetchall()}; con.close()
    for chat_id in chat_ids:
        try:
            con2 = _db_connect(); cur2 = con2.cursor()
            cur2.execute("DELETE FROM transactions WHERE chat_id=?", (chat_id,))
            con2.commit(); con2.close()
            try:
                context.bot.send_message(chat_id, "Good morning — begun new day. Please send today's UPI/IMPS amounts here.")
            except Exception as e:
                logger.warning("Couldn't send reset message to %s: %s", chat_id, e)
        except Exception as e:
            logger.exception("Error clearing transactions for chat %s: %s", chat_id, e)

# ====== text handler (with +0 special-case) ======
def text_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw_text = update.message.text or ""
    text = raw_text.strip()
    user = update.effective_user.first_name or "user"

    if not is_authorized(user_id):
        return

    rate = get_exchange_rate(chat_id)

    # Special-case exact "+0": do NOT record, but reply + summary+button
    if text == "+0":
        update.message.reply_text
        send_summary_with_button(update, context, chat_id)
        return

    # Income '+' with arithmetic
    if text.startswith("+"):
        expr = text[1:].strip()
        try:
            amount = safe_eval_arith(expr)
            usd = amount / rate
            add_tx_db(chat_id, user, "income", amount, usd)
            send_summary_with_button(update, context, chat_id)
            return
        except Exception:
            return update.message.reply_text("⚠️ Invalid income format. Use +100 or +100*1.07 etc.")

    # Negative income -number
    if re.fullmatch(r'-\s*\d+(\.\d+)?', text):
        num = re.sub(r'[^\d\.\-]', '', text)
        try:
            amount = float(num)
            inr = -abs(amount)
            usd = inr / rate
            add_tx_db(chat_id, user, "income", inr, usd)
            send_summary_with_button(update, context, chat_id)
            return
        except:
            return update.message.reply_text("⚠️ Invalid negative income format.")

    # Payout: T<number> (USD)
    m = re.fullmatch(r'T(-?\d+(\.\d+)?)([Uu])?', text)
    if m:
        num_str = m.group(1)
        try:
            amt = float(num_str)
            usd_amt = float(amt)
            inr = usd_amt * rate
            add_tx_db(chat_id, user, "payout", inr, usd_amt)
            send_summary_with_button(update, context, chat_id)
            return
        except:
            return update.message.reply_text("⚠️ Invalid payout format. Use T100")

    return

# ====== main ======
def main():
    init_db()
    if not TOKEN:
        logger.error("No TOKEN found. Set TOKEN environment variable before running.")
        sys.exit(1)

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("summary", summary_cmd))
    dp.add_handler(CommandHandler("viewfull", viewfull_cmd))
    dp.add_handler(CallbackQueryHandler(viewfull_callback, pattern=r'^VIEWFULL$'))
    dp.add_handler(CommandHandler("whoami", whoami_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))
    dp.add_handler(CommandHandler("dbpeek", dbpeek_cmd))

    dp.add_handler(CommandHandler(["setrate", "rate"], setrate_cmd))
    dp.add_handler(CommandHandler("getrate", getrate_cmd))
    dp.add_handler(CommandHandler("setfee", setfee_cmd))
    dp.add_handler(CommandHandler("addadmin", addadmin_cmd))
    dp.add_handler(CommandHandler("deladmin", deladmin_cmd))

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_handler))

    job_queue = updater.job_queue
    reset_time = datetime.time(hour=8, minute=45, tzinfo=IST)
    job_queue.run_daily(daily_reset, time=reset_time)
    logger.info("Scheduled daily reset at 08:45 IST")

    print("Bot started...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
