#!/usr/bin/env python3
"""
Telegram expense bot (polling) + PostgreSQL storage + scheduled daily report.

Environment variables (set these in Railway -> Variables):
- TELEGRAM_TOKEN         (required) your bot token
- DATABASE_URL           (Railway provides automatically)
- MONTHLY_BUDGET         (optional, default 300)
- REMINDER_CHAT_ID       (optional) chat id to send daily report to
- REMINDER_TIME          (optional) "HH:MM" (24h) local time to send daily report
- TIMEZONE               (optional) timezone string, default "UTC" or "Asia/Dubai"
- ALLOWED_USER_IDS       (optional) comma-separated Telegram user ids allowed to use bot
"""
import os
import time
import logging
from datetime import datetime, date
import requests
import psycopg2
import psycopg2.extras
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from decimal import Decimal

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------- Config from env ----------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    logging.error("TELEGRAM_TOKEN not set")
    raise SystemExit("TELEGRAM_TOKEN not set")

URL = f"https://api.telegram.org/bot{TOKEN}"
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway sets this
MONTHLY_BUDGET_ENV = float(os.getenv("MONTHLY_BUDGET", "300"))
REMINDER_CHAT_ID = os.getenv("REMINDER_CHAT_ID", "").strip() or None
REMINDER_TIME = os.getenv("REMINDER_TIME", "").strip() or None  # "21:00"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Dubai")
ALLOWED_USER_IDS = [s.strip() for s in os.getenv("ALLOWED_USER_IDS", "").split(",") if s.strip()]

tz = pytz.timezone(TIMEZONE)

# ---------- Database helpers ----------
def db_conn():
    """Return a new psycopg2 connection (caller must close)."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    """Create tables if they don't exist and ensure budget setting exists."""
    sql_create_expenses = """
    CREATE TABLE IF NOT EXISTS expenses (
      id SERIAL PRIMARY KEY,
      user_id BIGINT,
      username TEXT,
      amount NUMERIC NOT NULL,
      category TEXT,
      note TEXT,
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    sql_create_settings = """
    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT
    );
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_create_expenses)
            cur.execute(sql_create_settings)
            # ensure budget exists in settings (if absent, use env)
            cur.execute(
                "INSERT INTO settings (key, value) SELECT 'budget', %s WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key='budget')",
                (str(MONTHLY_BUDGET_ENV),),
            )
        conn.commit()
    logging.info("Database initialized.")

def get_user_today_total(user_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SUM(amount) FROM expenses WHERE user_id=%s AND created_at::date = CURRENT_DATE",
                (user_id,),
            )
            r = cur.fetchone()
            return float(r[0]) if r and r[0] is not None else 0.0

# ---------- DB operations ----------
def add_expense_db(user_id, username, amount, category, note):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO expenses (user_id, username, amount, category, note) VALUES (%s,%s,%s,%s,%s)",
                (user_id, username, Decimal(amount), category, note),
            )
        conn.commit()

def get_all_expenses():
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM expenses ORDER BY created_at DESC")
            return cur.fetchall()

def get_month_totals():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(amount)::numeric FROM expenses WHERE to_char(created_at, 'YYYY-MM') = to_char(now(), 'YYYY-MM')")
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0

def get_today_total():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(amount) FROM expenses WHERE created_at::date = CURRENT_DATE")
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0

def get_by_user_month():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(username, user_id::text) AS user,
                       SUM(amount) as total
                FROM expenses
                WHERE to_char(created_at, 'YYYY-MM') = to_char(now(), 'YYYY-MM')
                GROUP BY COALESCE(username, user_id::text)
                ORDER BY total DESC
            """)
            return cur.fetchall()



def get_user_month_total(user_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SUM(amount) FROM expenses WHERE user_id=%s AND to_char(created_at,'YYYY-MM') = to_char(now(),'YYYY-MM')",
                (user_id,),
            )
            r = cur.fetchone()
            return float(r[0]) if r and r[0] is not None else 0.0

def delete_last_user_expense(user_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM expenses
                WHERE id = (
                  SELECT id FROM expenses
                  WHERE user_id=%s AND to_char(created_at,'YYYY-MM') = to_char(now(),'YYYY-MM')
                  ORDER BY created_at DESC
                  LIMIT 1
                )
                RETURNING id;
                """,
                (user_id,),
            )
            r = cur.fetchone()
        conn.commit()
        return bool(r and r[0])

def get_setting(key):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            r = cur.fetchone()
            return r[0] if r else None

def set_setting(key, value):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key,value) VALUES (%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(value)),
            )
        conn.commit()

def get_budget():
    val = get_setting("budget")
    if val:
        try:
            return float(val)
        except:
            return MONTHLY_BUDGET_ENV
    return MONTHLY_BUDGET_ENV

# ---------- Telegram helpers ----------
def send_message(chat_id, text):
    try:
        resp = requests.post(URL + "/sendMessage", json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()
    except Exception as e:
        logging.exception("send_message failed: %s", e)

def send_markdown(chat_id, text):
    try:
        resp = requests.post(
            URL + "/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )
        resp.raise_for_status()
    except Exception as e:
        logging.exception("send_message failed: %s", e)

def fetch_updates(offset=None, timeout=60):
    try:
        params = {"timeout": timeout}
        if offset:
            params["offset"] = offset
        r = requests.get(URL + "/getUpdates", params=params, timeout=timeout + 10)
        return r.json()
    except Exception as e:
        logging.exception("fetch_updates failed: %s", e)
        return {}

# ---------- Business logic / stats ----------
def days_in_month(dt=None):
    if dt is None:
        dt = datetime.now(tz)
    import calendar
    return calendar.monthrange(dt.year, dt.month)[1]

def compute_forecast_and_stats():
    total_month = get_month_totals()
    total_today = get_today_total()
    now = datetime.now(tz)
    days_passed = now.day
    dim = days_in_month(now)
    avg_daily = total_month / max(days_passed, 1)
    predicted = avg_daily * dim
    remaining = max(get_budget() - total_month, 0.0)
    days_left = dim - now.day
    return {
        "total_month": round(total_month, 2),
        "total_today": round(total_today, 2),
        "avg_daily": round(avg_daily, 2),
        "predicted": round(predicted, 2),
        "remaining": round(remaining, 2),
        "days_left": days_left,
        "days_in_month": dim,
        "will_exceed": predicted > get_budget(),
    }

# ---------- Command parsing ----------
def is_allowed(user_id):
    if not ALLOWED_USER_IDS:
        return True
    return str(user_id) in ALLOWED_USER_IDS

def parse_expense_text(text):
    t = text.strip()
    if t.startswith("/spent"):
        t = t[len("/spent"):].strip()
    if t.lower().startswith("add "):
        t = t.split(" ", 1)[1].strip()
    parts = t.split()
    if not parts:
        return None
    try:
        amt = parts[0].replace(",", "")
        amt_val = float(amt)
        category = parts[1] if len(parts) > 1 else ""
        note = " ".join(parts[2:]) if len(parts) > 2 else ""
        return (amt_val, category, note)
    except (ValueError, IndexError):
        return None

# ---------- Scheduled daily report ----------
def send_daily_report_job():
    if not REMINDER_CHAT_ID or not REMINDER_TIME:
        logging.info("Daily reminder not configured; skipping daily report.")
        return
    stats = compute_forecast_and_stats()
    lines = [
        "⏰ Daily Budget Summary",
        f"Today: {stats['total_today']} AED",
        f"This month: {stats['total_month']} AED",
        f"Days left: {stats['days_left']} / {stats['days_in_month']}",
        f"Budget: {get_budget()} AED, Remaining: {stats['remaining']} AED",
        f"Predicted end: {stats['predicted']} AED {'⚠️ Over budget' if stats['will_exceed'] else '✅ On track'}",
    ]
    send_markdown(REMINDER_CHAT_ID, "\n".join(lines))
    logging.info("Daily reminder sent to %s", REMINDER_CHAT_ID)

def schedule_daily_job():
    if not REMINDER_TIME or not REMINDER_CHAT_ID:
        logging.info("No REMINDER_TIME or REMINDER_CHAT_ID set; skipping scheduling.")
        return
    try:
        hh, mm = map(int, REMINDER_TIME.split(":"))
    except:
        logging.error("Invalid REMINDER_TIME format. Use HH:MM (24h).")
        return
    sched = BackgroundScheduler(timezone=tz)
    sched.start()
    sched.add_job(send_daily_report_job, "cron", hour=hh, minute=mm)
    logging.info("Scheduled daily job at %02d:%02d %s", hh, mm, TIMEZONE)

# ---------- Main polling loop ----------
def main():
    init_db()
    schedule_daily_job()
    logging.info("Bot started (polling).")
    offset = None
    while True:
        updates = fetch_updates(offset=offset, timeout=60)
        if not updates or "result" not in updates:
            time.sleep(1)
            continue
        for up in updates["result"]:
            offset = up["update_id"] + 1
            try:
                if "message" not in up:
                    continue
                msg = up["message"]
                chat_id = msg["chat"]["id"]
                user_id = msg["from"]["id"]
                username = msg["from"].get("first_name") or msg["from"].get("username") or str(user_id)
                text = msg.get("text", "").strip()
                logging.info("Received from %s (%s): %s", username, user_id, text)

                if not is_allowed(user_id):
                    send_message(chat_id, "🚫 You are not allowed to use this bot.")
                    continue

                if text.startswith("/"):
                    parts = text.split()
                    cmd = parts[0].lower()
                    args = parts[1:]
                    if cmd == "/start":
                        send_message(chat_id, "👋 Hi! Send expenses like: `50 food lunch` or `/spent 50 food lunch`. Use /summary for stats.")
                    elif cmd == "/spent":
                        parsed = parse_expense_text(text)
                        if not parsed:
                            send_message(chat_id, "Usage: /spent <amount> [category] [note]")
                        else:
                            amt, cat, note = parsed
                            add_expense_db(user_id, username, amt, cat, note)
                            send_message(chat_id, f"✅ Logged {amt} AED ({cat})")
                    elif cmd in ["/daily", "/today"]:
                        send_message(chat_id, f"Your spending today: {get_user_today_total(user_id)} AED")
                    elif cmd in ["/monthly", "/total"]:
                        send_message(chat_id, f"This month's total: {get_month_totals()} AED")
                    elif cmd == "/predict":
                        stats = compute_forecast_and_stats()
                        send_message(chat_id, f"Forecast: {stats['predicted']} AED. {'⚠️ You will exceed budget!' if stats['will_exceed'] else '✅ On track'}")
                    elif cmd == "/summary":
                        s = compute_forecast_and_stats()
                        by_user = get_by_user_month()
                        lines = [
                            f"📊 Summary - {datetime.now(tz).strftime('%Y-%m')}",
                            f"Today: {s['total_today']} AED",
                            f"Month: {s['total_month']} AED",
                            f"Remaining: {s['remaining']} AED (Budget {get_budget()} AED)",
                            f"Days left: {s['days_left']}",
                            f"Forecast: {s['predicted']} AED {'⚠️' if s['will_exceed'] else ''}",
                            "",
                            "🔎 By user:",
                        ]
                        for u in by_user:
                            lines.append(f"- {u['user']}: {u['total']} AED")
                        send_markdown(chat_id, "\n".join(lines))
                    elif cmd == "/me":
                        total = get_user_month_total(user_id)
                        send_message(chat_id, f"You spent {total} AED this month.")
                    elif cmd == "/undo":
                        ok = delete_last_user_expense(user_id)
                        send_message(chat_id, "✅ Last expense removed." if ok else "Nothing to undo.")
                    elif cmd == "/setbudget":
                        if not args:
                            send_message(chat_id, "Usage: /setbudget <amount>")
                        else:
                            try:
                                new_b = float(args[0])
                                set_setting("budget", new_b)
                                send_message(chat_id, f"✅ Budget updated to {new_b} AED")
                            except Exception:
                                send_message(chat_id, "Invalid amount.")
                    elif cmd == "/budget":
                        send_message(chat_id, f"Budget: {get_budget()} AED")
                    elif cmd == "/balance":
                        s = compute_forecast_and_stats()
                        send_message(chat_id, f"Remaining this month: {s['remaining']} AED")
                    elif cmd == "/daysleft":
                        s = compute_forecast_and_stats()
                        send_message(chat_id, f"Days left: {s['days_left']} of {s['days_in_month']}")
                    elif cmd == "/whoami":
                        send_message(chat_id, f"Your ID: {user_id}\nChat ID: {chat_id}")
                    else:
                        send_message(chat_id, "Unknown command. Use /summary, /spent, /daily, /predict.")
                    continue

                parsed = parse_expense_text(text)
                if parsed:
                    amt, cat, note = parsed
                    try:
                        add_expense_db(user_id, username, amt, cat, note)
                        send_message(chat_id, f"✅ Logged {amt} AED ({cat})")
                    except Exception:
                        logging.exception("DB insert failed")
                        send_message(chat_id, "❌ Failed to save expense.")
                else:
                    send_message(chat_id, "I didn't understand. Send `/spent 50 food note` or just `50 food note`.")
            except Exception:
                logging.exception("Error processing update")
        time.sleep(0.5)

if __name__ == "__main__":
    main()
