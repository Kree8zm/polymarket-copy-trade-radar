"""
pm_telegram_bot.py — Telegram bot wrapper for the Polymarket copy-trading profiler.

Commands:
  /start   — show inline menu
  /menu    — show inline menu
  /scan    — run a fresh wallet scan and send top 3 + report
  /top     — show cached top 3 from the last scan
  /report  — send the full HTML report
  /status  — show DB stats
  /help    — show this message

Run:
  python3 pm_telegram_bot.py
"""

import asyncio
import os
import sys
import json
import subprocess
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# Paths
BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("PM_DB_PATH", BASE_DIR / "copyable_wallets.db"))
HTML_REPORT = Path(os.environ.get("PM_HTML_REPORT", BASE_DIR / "copyable_wallets.html"))
JSON_RANK = BASE_DIR / "pm_intel_rank.json"

# Your bot token from @BotFather
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Your chat/user IDs allowed to use this bot (optional, for security)
ALLOWED_USERS = set(int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if x.strip())

# Public HTTPS URL for the Mini App
MINI_APP_URL = os.environ.get(
    "PM_MINI_APP_URL",
    "https://urban-waddle-r74w597jww9v3pqg9-8888.app.github.dev/copyable_wallets.html",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

def load_top_n(n=3):
    if not JSON_RANK.exists():
        return []
    try:
        data = json.loads(JSON_RANK.read_text())
        return data.get("ranked", [])[:n]
    except Exception:
        return []

def format_wallet_line(wallet, rank):
    name = wallet.get("name") or wallet.get("address", "???")
    score = wallet.get("score", 0)
    pnl = wallet.get("realized_pnl", 0)
    decay = wallet.get("edge_decay", 0)
    strategy = wallet.get("strategy_score", 1.0)
    edge = wallet.get("edge_verdict", "?")
    return (
        f"#{rank} {name}\n"
        f"   score={score:.2f}  realized_pnl=${pnl:,.0f}  edge_decay={decay:.2f}\n"
        f"   strategy={strategy:.2f}x  verdict={edge}"
    )

def get_main_menu_keyboard():
    cache_busting_url = f"{MINI_APP_URL}?v={int(time.time())}"
    keyboard = [
        [
            InlineKeyboardButton("🔄 Run Scan", callback_data="cmd_scan"),
            InlineKeyboardButton("📈 Top 3", callback_data="cmd_top"),
        ],
        [
            InlineKeyboardButton("📄 Download HTML", callback_data="cmd_report"),
            InlineKeyboardButton("⚙️ Status", callback_data="cmd_status"),
        ],
        [
            InlineKeyboardButton(
                "🌐 Open Interactive Dashboard",
                web_app=WebAppInfo(url=cache_busting_url),
            )
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------------------------
# Core logic helpers
# ---------------------------------------------------------------------------

SCAN_LOCK = asyncio.Lock()

async def reply_top3(chat, user_id: int):
    if not is_allowed(user_id):
        await chat.send_message("Not authorized.")
        return
    wallets = load_top_n(3)
    if not wallets:
        await chat.send_message("No scan results yet. Use /scan first.")
        return
    lines = ["Top 3 copyable wallets:"]
    for i, w in enumerate(wallets, 1):
        lines.append(format_wallet_line(w, i))
    await chat.reply_text("\n\n".join(lines))

async def reply_report(chat, user_id: int):
    if not is_allowed(user_id):
        await chat.send_message("Not authorized.")
        return
    if not HTML_REPORT.exists():
        await chat.send_message("No report yet. Run /scan first.")
        return
    await chat.reply_document(document=open(HTML_REPORT, "rb"), filename="copyable_wallets.html")

async def reply_status(chat, user_id: int):
    if not is_allowed(user_id):
        await chat.send_message("Not authorized.")
        return
    if not DB_PATH.exists():
        await chat.send_message("DB not created yet. Run /scan first.")
        return
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM wallets")
    count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM wallet_metrics_history")
    trades = cur.fetchone()[0]
    conn.close()
    await chat.reply_text(f"DB status:\nwallets={count}\nhistory_rows={trades}")

# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

async def run_scan_async(message, user_id: int):
    if not is_allowed(user_id):
        await message.reply_text("Not authorized.")
        return

    if SCAN_LOCK.locked():
        await message.reply_text("⏳ Scan already in progress. I'll ping you when it's done.")
        return

    async with SCAN_LOCK:
        status_msg = await message.reply_text("⏳ *Scan started in background...*", parse_mode="Markdown")

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(BASE_DIR / "pm_wallet_profiler.py"),
                "--auto-n", "50",
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                await status_msg.edit_text("❌ *Scan timed out after 10 minutes.*", parse_mode="Markdown")
                return

            if proc.returncode != 0:
                err = stderr.decode("utf-8", "replace")[:800]
                await status_msg.edit_text(f"❌ *Scan failed:* `{err}`", parse_mode="Markdown")
                return

            wallets = load_top_n(3)
            if wallets:
                lines = ["✅ *Scan complete. Top 3:*"]
                for i, w in enumerate(wallets, 1):
                    lines.append(format_wallet_line(w, i))
                await message.reply_text("\n\n".join(lines), parse_mode="Markdown")

            if HTML_REPORT.exists():
                await message.reply_document(
                    document=open(HTML_REPORT, "rb"),
                    filename="copyable_wallets.html",
                    caption="Full report attached.",
                )
            else:
                await message.reply_text("Scan finished but no HTML report was generated.")
        except Exception as e:
            await status_msg.edit_text(f"⚠️ *Error during scan:* `{e}`", parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 <b>Polymarket Scanner Dashboard</b>\nSelect an option below to trigger actions:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Polymarket Copy-Trading Scanner\n\n"
        "/start  — show interactive menu\n"
        "/menu   — show interactive menu\n"
        "/scan   — run a fresh scan (takes a few minutes)\n"
        "/top    — show top 3 wallets from last scan\n"
        "/report — send the full HTML report\n"
        "/status — show database stats\n"
        "/help   — this message"
    )

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_top3(update.effective_chat, update.effective_user.id)

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_report(update.effective_chat, update.effective_user.id)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_status(update.effective_chat, update.effective_user.id)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(run_scan_async(update.effective_message, update.effective_user.id))

# ---------------------------------------------------------------------------
# Button callback handler
# ---------------------------------------------------------------------------

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cmd_scan":
        asyncio.create_task(run_scan_async(query.message, query.from_user.id))
    elif query.data == "cmd_top":
        await reply_top3(query.message.chat, query.from_user.id)
    elif query.data == "cmd_report":
        await reply_report(query.message.chat, query.from_user.id)
    elif query.data == "cmd_status":
        await reply_status(query.message.chat, query.from_user.id)

# ---------------------------------------------------------------------------
# Health-check server for worker / background-service deployments
# ---------------------------------------------------------------------------

HEALTH_PORT = int(os.environ.get("PM_HEALTH_PORT", "8080"))

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def start_health_server(port=HEALTH_PORT):
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[health] ping server on :{port}/ping", file=sys.stderr)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start_health_server()
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN env var.", file=sys.stderr)
        sys.exit(1)
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", start_command))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(button_callback_handler))

    print("Bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
