# bot.py — final, fixed: negative incomes included, count accurate, show only LAST_N items, strict CAPS T payouts
# NOTE: This file includes the token directly for testing. Revoke/regenerate after testing.
import os
import sys
import re
import io
import sqlite3
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ensure current directory is first on import path (helps imghdr shim on PA)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ====== CONFIG ======
TOKEN = "8436563657:AAFUs0u8kBXzQGS3C3H12Ejes8AEcXeRp5o"   # <-- testing token (revoke after)
BASE_URL = os.environ.get("BASE_URL", "")  # optional web panel url
LAST_N = int(os.environ.get("LAST_N", "5"))  # how many recent items to show in compact view
DB_PATH = os.environ.get("DB_PATH", "tx.db")
ADMINS = {6603524612, 7773526534}
authorized_users = set(ADMINS)

# per-chat maps
exchange_rates = {}
fee_rates = {}

# ====== DB helpers ======
def init_db():
    con = sqlite3.connect(DB_PATH)
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
    con.commit()
    con.close()

def add_tx_db(chat_id, user, type_, amount_inr, amount_usd):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO transactions (chat_id,user,type,amount_inr,amount_usd,time_iso) VALUES (?,?,?,?,?,?)",
        (chat_id, user, type_, amount_inr, amount_usd, datetime.datetime.now().isoformat())
    )
    con.commit()
    con.close()

def get_transactions_between(chat_id, from_dt, to_dt):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""SELECT time_iso, amount_inr, amount_usd, user, type
                   FROM transactions
                   WHERE chat_id=? AND time_iso BETWEEN ? AND ?
                   ORDER BY id ASC""",
                (chat_id, from_dt.isoformat(), to_dt.isoformat()))
    rows = cur.fetchall()
    con.close()
    return rows

def get_latest_transactions(chat_id, limit=5):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT time_iso, amount_inr, amount_usd, user, type FROM transactions WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = cur.fetchall()
    con.close()
    return list(reversed(rows))

# ====== helpers ======
def is_authorized(user_id): return user_id in authorized_users
def get_exchange_rate(chat_id): return exchange_rates.get(chat_id, 106)
def get_fee_rate(chat_id): return fee_rates.get(chat_id, 0.0)

def eval_arith(expr):
    # safe-ish evaluator: allow digits, spaces and basic ops
    if not re.match(r"^[0-9\.\+\-\*\/\(\) ]+$", expr):
        raise ValueError("Invalid characters")
    # eval only arithmetic expression
    return float(eval(expr))

# ====== message builders ======
def build_compact_message(chat_id):
    """
    Shows:
      - header "Today's Income (TOTAL_COUNT)" but only list LAST_N items (most recent)
      - Today's Issued (same: count and show LAST_N)
      - Totals and Already/Should/Not yet lines
      - 'Use /viewfull' hint
    """
    rate = get_exchange_rate(chat_id)
    fee = get_fee_rate(chat_id)

    # day range 06:00 -> next day 06:00
    today = datetime.date.today()
    from_dt = datetime.datetime.combine(today, datetime.time(hour=6))
    to_dt = from_dt + datetime.timedelta(days=1)

    rows = get_transactions_between(chat_id, from_dt, to_dt)
    incomes_all = [r for r in rows if r[4] == "income"]
    payouts_all = [r for r in rows if r[4] == "payout"]

    # totals and counts (count should reflect ALL incomes/payouts for day)
    income_count = len(incomes_all)
    payout_count = len(payouts_all)

    # prepare the show lists (only last N most recent)
    show_n = LAST_N if LAST_N > 0 else 5
    incomes_show = incomes_all[-show_n:] if incomes_all else []
    payouts_show = payouts_all[-show_n:] if payouts_all else []

    def fmt_time(tiso):
        try:
            return datetime.datetime.fromisoformat(tiso).strftime("%H:%M:%S")
        except:
            return tiso

    income_lines = "\n".join(
        [f"{fmt_time(r[0])}   {int(r[1]) if float(r[1]).is_integer() else r[1]} / {rate} = {r[2]:.2f}U   {r[3]}" for r in incomes_show]
    ) or "None"

    payout_lines = "\n".join(
        [f"{fmt_time(r[0])}   {r[2]:.2f}U ({int(r[1]) if float(r[1]).is_integer() else r[1]})   {r[3]}" for r in payouts_show]
    ) or "None"

    # totals across all incomes/payouts (for the day)
    total_income_inr = sum(r[1] for r in incomes_all)
    total_income_usd = sum(r[2] for r in incomes_all)
    total_payout_inr = sum(r[1] for r in payouts_all)
    total_payout_usd = sum(r[2] for r in payouts_all)
    not_yet_inr = total_income_inr - total_payout_inr
    not_yet_usd = total_income_usd - total_payout_usd

    parts = []
    parts.append(f"Today's Income ({income_count})")
    parts.append(income_lines)
    parts.append("")
    parts.append(f"Today's Issued ({payout_count})")
    parts.append(payout_lines)
    parts.append("")
    parts.append(f"Total Income : {int(total_income_inr)}")
    parts.append(f"Exchange Rate : {rate}")
    parts.append(f"Fee Rate : {int(fee)}%")
    parts.append("")
    parts.append(f"Already issued : {int(total_income_inr)} | {total_income_usd:.2f}U")
    parts.append(f"Should be issued : {int(total_payout_inr)} | {total_payout_usd:.2f}U")
    parts.append(f"Not yet issued : {int(not_yet_inr)} | {not_yet_usd:.2f}U")
    parts.append("")
    parts.append("Use /viewfull to download full report")
    return "\n".join(parts)

def build_full_text_report(chat_id):
    rate = get_exchange_rate(chat_id)
    fee = get_fee_rate(chat_id)

    today = datetime.date.today()
    from_dt = datetime.datetime.combine(today, datetime.time(hour=6))
    to_dt = from_dt + datetime.timedelta(days=1)

    rows = get_transactions_between(chat_id, from_dt, to_dt)
    incomes = [r for r in rows if r[4] == "income"]
    payouts = [r for r in rows if r[4] == "payout"]

    def fmt_time(tiso):
        try:
            return datetime.datetime.fromisoformat(tiso).strftime("%H:%M:%S")
        except:
            return tiso

    lines = []
    lines.append(f"Full report for chat_id={chat_id} date={today.isoformat()}")
    lines.append("="*40)
    lines.append("")
    lines.append(f"Today's Income ({len(incomes)})")
    if incomes:
        for r in incomes:
            lines.append(f"{fmt_time(r[0])}   {int(r[1]) if float(r[1]).is_integer() else r[1]} / {rate} = {r[2]:.2f}U   {r[3]}")
    else:
        lines.append("None")
    lines.append("")
    lines.append(f"Today's Issued ({len(payouts)})")
    if payouts:
        for r in payouts:
            lines.append(f"{fmt_time(r[0])}   {r[2]:.2f}U ({int(r[1]) if float(r[1]).is_integer() else r[1]})   {r[3]}")
    else:
        lines.append("None")
    lines.append("")
    total_income_inr = sum(r[1] for r in incomes)
    total_income_usd = sum(r[2] for r in incomes)
    total_payout_inr = sum(r[1] for r in payouts)
    total_payout_usd = sum(r[2] for r in payouts)
    not_yet_inr = total_income_inr - total_payout_inr
    not_yet_usd = total_income_usd - total_payout_usd
    lines.append(f"Total Income : {int(total_income_inr)}")
    lines.append(f"Exchange Rate : {rate}")
    lines.append(f"Fee Rate : {int(fee)}%")
    lines.append("")
    lines.append(f"Already issued : {int(total_income_inr)} | {total_income_usd:.2f}U")
    lines.append(f"Should be issued : {int(total_payout_inr)} | {total_payout_usd:.2f}U")
    lines.append(f"Not yet issued : {int(not_yet_inr)} | {not_yet_usd:.2f}U")
    lines.append("")
    lines.append("All transactions (chronological):")
    lines.append("")
    for r in rows:
        lines.append(f"{r[0]} | type={r[4]} | INR={r[1]} | USD={r[2]:.2f} | user={r[3]}")
    return "\n".join(lines)

# ====== commands ======
def start(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Bot ready. Use + / - / T / T- for transactions. /summary for quick view. /viewfull to download full report.")

def summary_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    text = build_compact_message(chat_id)

    keyboard = None
    if BASE_URL:
        today = datetime.date.today()
        url = f"{BASE_URL}/report?chat_id={chat_id}&date={today.isoformat()}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View full report (web)", url=url)]])

    update.message.reply_text(text, parse_mode=None, reply_markup=keyboard)

def viewfull_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    full_text = build_full_text_report(chat_id)

    bio = io.BytesIO()
    bio.write(full_text.encode("utf-8"))
    bio.seek(0)
    filename = f"report_{chat_id}_{datetime.date.today().isoformat()}.txt"
    update.message.reply_document(document=bio, filename=filename)
    bio.close()

# ====== admin helpers ======
def whoami_cmd(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    uname = update.effective_user.first_name
    update.message.reply_text(f"Your user_id: {uid}\nchat_id: {cid}\nname: {uname}")

def clear_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return update.message.reply_text("❌ You are not authorized.")
    chat_id = update.effective_chat.id
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM transactions WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()
    return update.message.reply_text("Today's bill has been cleared and recording can be restarted")

def dbpeek_cmd(update: Update, context: CallbackContext):
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    limit = 20
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT time_iso, amount_inr, amount_usd, user, type FROM transactions WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    rows = cur.fetchall()
    con.close()
    if not rows:
        return update.message.reply_text("No transactions found for this chat_id.")
    text = "Last transactions for this chat:\n"
    for r in reversed(rows):
        text += f"{r[0]} | {r[4]} | INR={r[1]} | USD={r[2]:.2f} | {r[3]}\n"
    update.message.reply_text(text)

# ====== text handler (transactions + admin) ======
def text_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw_text = update.message.text or ""
    text = raw_text.strip()
    user = update.effective_user.first_name or "user"

    if not is_authorized(user_id):
        return

    # plain clear
    if text.lower() == "clear" or text.lower().startswith("clearing bills"):
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("DELETE FROM transactions WHERE chat_id=?", (chat_id,))
        con.commit()
        con.close()
        return update.message.reply_text("Today's bill has been cleared and recording can be restarted")

    # add/del operator
    if text.lower().startswith("add "):
        try:
            new_id = int(text.split()[1])
            authorized_users.add(new_id)
            return update.message.reply_text(f"✅ Added operator with ID: {new_id}")
        except:
            return update.message.reply_text("⚠️ Example: add 123456789")
    if text.lower().startswith("del "):
        try:
            remove_id = int(text.split()[1])
            authorized_users.discard(remove_id)
            return update.message.reply_text(f"❌ Removed operator with ID: {remove_id}")
        except:
            return update.message.reply_text("⚠️ Example: del 123456789")

    # exchange rate
    if text.lower().startswith("exchange"):
        match = re.search(r"(-?\d+(\.\d+)?)", text)
        if match:
            exchange_rates[chat_id] = float(match.group(1))
            return update.message.reply_text(f"Exchange rate set: {exchange_rates[chat_id]}")
        else:
            return update.message.reply_text("⚠️ Example: exchange 106")

    # fee rate
    if text.lower().startswith("fee"):
        match = re.search(r"(\d+(\.\d+)?)", text)
        if match:
            fee_rates[chat_id] = float(match.group(1))
            return update.message.reply_text(f"Fee rate set: {fee_rates[chat_id]}%")
        else:
            return update.message.reply_text("⚠️ Example: fee 2")

    rate = get_exchange_rate(chat_id)

    # Income (+expr)
    if text.startswith("+"):
        expr = text[1:].strip()
        try:
            amount = eval_arith(expr)
            usd = amount / rate
            add_tx_db(chat_id, user, "income", amount, usd)
            return update.message.reply_text(build_compact_message(chat_id), parse_mode=None)
        except Exception:
            return update.message.reply_text("⚠️ Invalid income format.")

    # Negative Income (-expr)  --> ensure formats like -100 or -100.5 accepted
    if re.fullmatch(r'-\s*\d+(\.\d+)?', text):
        # parse numeric part
        num = re.sub(r'[^\d\.\-]', '', text)
        try:
            amount = float(num)
            # store as negative INR and negative USD
            inr = -abs(amount) if amount > 0 else amount
            usd = inr / rate
            add_tx_db(chat_id, user, "income", inr, usd)
            return update.message.reply_text(build_compact_message(chat_id), parse_mode=None)
        except Exception:
            return update.message.reply_text("⚠️ Invalid negative income format.")

    # ====== Strict payout parsing (only CAPITAL T accepted) ======
    # Accept only: T<number>U or T-<number>U (U or u allowed, decimal ok), e.g. T100U, T10.25u, T-5U
    t = text.strip()
    m = re.fullmatch(r'T(-?\d+(\.\d+)?)([Uu])', t)
    if m:
        num_str = m.group(1)
        try:
            amt = float(num_str)
            if num_str.startswith("-"):
                usd_amt = -abs(amt)
                inr = usd_amt * rate
                add_tx_db(chat_id, user, "payout", inr, usd_amt)
            else:
                usd_amt = amt
                inr = usd_amt * rate
                add_tx_db(chat_id, user, "payout", inr, usd_amt)
            return update.message.reply_text(build_compact_message(chat_id), parse_mode=None)
        except Exception:
            return update.message.reply_text("⚠️ Invalid payout format.")
    # if starts with 'T' but invalid pattern -> ignore to avoid spam
    if t and t[0] == 'T':
        return

    # any other text ignored

# ====== main ======
def main():
    init_db()
    if not TOKEN or TOKEN == "" or TOKEN == "PUT_YOUR_TOKEN_HERE":
        print("❌ ERROR: Set BOT_TOKEN env var or edit bot.py to include your bot token.")
        sys.exit(1)

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # core commands
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("summary", summary_cmd))
    dp.add_handler(CommandHandler("viewfull", viewfull_cmd))

    # debug/admin commands
    dp.add_handler(CommandHandler("whoami", whoami_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))
    dp.add_handler(CommandHandler("dbpeek", dbpeek_cmd))

    # text handler for transactions and plain-text clear
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_handler))

    print("Bot started...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

