"""
Bayse Markets Telegram Bot
Auto-trades sports (every 30 min, 20% wallet) and crypto (every 5 min, 10% wallet).
"""
import os
import asyncio
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)

from bayse_client import BayseClient
from sports_scanner import find_sports_opportunity
from crypto_scanner import find_crypto_opportunity

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BayseBot")

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
BAYSE_PUBLIC_KEY = os.environ["BAYSE_PUBLIC_KEY"]
BAYSE_SECRET_KEY = os.environ["BAYSE_SECRET_KEY"]
ADMIN_CHAT_ID    = int(os.environ.get("ADMIN_CHAT_ID", "0"))

SPORTS_ALLOC     = 0.20
CRYPTO_ALLOC     = 0.10
MIN_GAP          = 0.10
SPORTS_INTERVAL  = 30 * 60
CRYPTO_INTERVAL  = 5  * 60
CURRENCY         = "NGN"

# ── Global state ───────────────────────────────────────────────────────────────
client = BayseClient(BAYSE_PUBLIC_KEY, BAYSE_SECRET_KEY, CURRENCY)
autotrade_enabled = False
trade_log: list[dict] = []


# ── Formatting ─────────────────────────────────────────────────────────────────
def fmt_price(p: float) -> str:
    return f"{int(round(p * 100))}¢"

def fmt_ngn(amount: float) -> str:
    return f"₦{amount:,.0f}"

def now_str() -> str:
    return datetime.now().strftime("%H:%M %d/%m")


# ── Notify admin ───────────────────────────────────────────────────────────────
async def notify(app, text: str):
    if ADMIN_CHAT_ID:
        try:
            await app.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"notify failed: {e}")


# ── Trade executor ─────────────────────────────────────────────────────────────
async def execute_trade(signal: dict, amount: float, app) -> bool:
    try:
        result = await client.place_order(
            event_id=signal["event_id"],
            market_id=signal["market_id"],
            side="BUY",
            outcome_id=signal["outcome_id"],
            amount=amount,
        )
        order  = result.get("order", {})
        status = order.get("status", "unknown")

        trade_log.append({
            "time":     now_str(),
            "title":    signal["title"][:40],
            "outcome":  signal["outcome_label"],
            "amount":   amount,
            "price":    order.get("price", signal["bayse_price"]),
            "status":   status,
            "category": signal["category"],
        })
        if len(trade_log) > 50:
            trade_log.pop(0)

        icon = "⚽" if signal["category"] == "sports" else "📈"
        if signal["category"] == "sports":
            detail = (
                f"Gap: {int(signal['gap']*100)}% "
                f"(Real: {int(signal['real_prob']*100)}% | "
                f"Bayse: {fmt_price(signal['bayse_price'])})"
            )
        else:
            detail = f"Trend: {signal['trend']} | Bayse: {fmt_price(signal['bayse_price'])}"

        await notify(app,
            f"{icon} *Auto-trade executed*\n\n"
            f"*{signal['title'][:50]}*\n"
            f"Outcome: *{signal['outcome_label']}* | Amount: *{fmt_ngn(amount)}*\n"
            f"{detail}\n"
            f"Status: `{status}` | {now_str()}"
        )
        log.info(f"Trade OK — {signal['title'][:35]} | {signal['outcome_label']} | ₦{amount:.0f}")
        return True

    except Exception as e:
        log.error(f"Trade failed: {e}")
        await notify(app, f"⚠️ Trade failed: {e}")
        return False


# ── Scan jobs ──────────────────────────────────────────────────────────────────
async def sports_scan_job(app):
    if not autotrade_enabled:
        return
    log.info("⚽ Sports scan…")
    try:
        balance = await client.available_balance()
        budget  = balance * SPORTS_ALLOC
        if budget < 100:
            log.info(f"Sports budget too low: ₦{budget:.0f}")
            return

        data   = await client.list_events(category="sports", size=50)
        events = data.get("events", [])
        traded = 0

        for event in events:
            if not autotrade_enabled:
                break
            signal = await find_sports_opportunity(event, min_gap=MIN_GAP)
            if signal:
                amount = max(100.0, budget / 5)
                await execute_trade(signal, amount, app)
                traded += 1
                if traded >= 5:
                    break
            await asyncio.sleep(1.5)

        log.info(f"⚽ Sports scan done — {traded} trade(s)")
    except Exception as e:
        log.error(f"Sports scan error: {e}")


async def crypto_scan_job(app):
    if not autotrade_enabled:
        return
    log.info("📈 Crypto scan…")
    try:
        balance = await client.available_balance()
        budget  = balance * CRYPTO_ALLOC
        if budget < 100:
            log.info(f"Crypto budget too low: ₦{budget:.0f}")
            return

        data   = await client.list_events(category="crypto", size=50)
        events = data.get("events", [])
        traded = 0

        for event in events:
            if not autotrade_enabled:
                break
            signal = await find_crypto_opportunity(event, client)
            if signal:
                amount = max(100.0, budget / 5)
                await execute_trade(signal, amount, app)
                traded += 1
                if traded >= 5:
                    break
            await asyncio.sleep(0.5)

        log.info(f"📈 Crypto scan done — {traded} trade(s)")
    except Exception as e:
        log.error(f"Crypto scan error: {e}")


async def _sports_loop(app):
    while True:
        await sports_scan_job(app)
        await asyncio.sleep(SPORTS_INTERVAL)


async def _crypto_loop(app):
    while True:
        await crypto_scan_job(app)
        await asyncio.sleep(CRYPTO_INTERVAL)


async def start_background_jobs(app):
    asyncio.create_task(_sports_loop(app))
    asyncio.create_task(_crypto_loop(app))


# ── Commands ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bayse Auto-Trader*\n\n"
        "Auto-trades sports & crypto prediction markets on Bayse.\n\n"
        "*Commands:*\n"
        "/autotrade — Toggle auto-trading ON/OFF\n"
        "/status — Bot status & settings\n"
        "/markets — Browse open markets\n"
        "/portfolio — Open positions\n"
        "/balance — Wallet balance\n"
        "/pnl — Profit & Loss\n"
        "/trades — Recent auto-trades\n"
        "/scan — Manual scan now\n"
        "/help — Show this menu",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_autotrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global autotrade_enabled
    autotrade_enabled = not autotrade_enabled
    icon  = "✅" if autotrade_enabled else "❌"
    state = "ON"  if autotrade_enabled else "OFF"
    note  = "_Bot is now scanning and trading automatically._" if autotrade_enabled \
            else "_All automatic trading stopped._"
    await update.message.reply_text(
        f"{icon} *Auto-trading: {state}*\n\n"
        f"⚽ Sports — 20% wallet, every 30 min\n"
        f"📈 Crypto — 10% wallet, every 5 min\n"
        f"📐 Min gap: {int(MIN_GAP*100)}% | Currency: {CURRENCY}\n\n"
        f"{note}",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    icon  = "✅" if autotrade_enabled else "❌"
    state = "ON"  if autotrade_enabled else "OFF"
    try:
        balance       = await client.available_balance()
        sports_budget = fmt_ngn(balance * SPORTS_ALLOC)
        crypto_budget = fmt_ngn(balance * CRYPTO_ALLOC)
        bal_str       = fmt_ngn(balance)
    except Exception:
        bal_str = sports_budget = crypto_budget = "N/A"

    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"Auto-trading: {icon} *{state}*\n\n"
        f"💰 Balance: *{bal_str}*\n"
        f"⚽ Sports budget: *{sports_budget}* (20%)\n"
        f"📈 Crypto budget: *{crypto_budget}* (10%)\n\n"
        f"⏱ Sports scan: every 30 min\n"
        f"⏱ Crypto scan: every 5 min\n"
        f"📐 Min gap: {int(MIN_GAP*100)}%\n"
        f"🔄 Trades this session: {len(trade_log)}",
        parse_mode="Markdown",
    )


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Checking balance…")
    try:
        data   = await client.get_wallet()
        assets = data.get("assets", [])
        if not assets:
            await update.message.reply_text("No wallet data found.")
            return
        lines = ["💰 *Wallet Balance*\n"]
        for a in assets:
            lines.append(f"• {a.get('currency')}: {float(a.get('available', 0)):,.2f} available")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Loading portfolio…")
    try:
        data      = await client.get_portfolio()
        positions = data.get("positions", [])
        if not positions:
            await update.message.reply_text("📂 No open positions.")
            return
        lines = ["📂 *Your Positions*\n"]
        for pos in positions[:10]:
            lines.append(
                f"• *{pos.get('eventTitle','?')[:35]}*\n"
                f"  {pos.get('outcome','?')} | "
                f"{pos.get('shares','?')} shares | "
                f"Value: ₦{float(pos.get('currentValue', 0)):,.0f}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching P&L…")
    try:
        data = await client.get_pnl()
        pnl  = data.get("pnl", data)
        await update.message.reply_text(
            f"📈 *Profit & Loss*\n\n"
            f"Realized:   ₦{float(pnl.get('realized', 0)):,.2f}\n"
            f"Unrealized: ₦{float(pnl.get('unrealized', 0)):,.2f}\n"
            f"Total:      ₦{float(pnl.get('total', 0)):,.2f}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not trade_log:
        await update.message.reply_text("No trades yet this session.")
        return
    lines = ["🗂 *Recent Auto-Trades*\n"]
    for t in reversed(trade_log[-10:]):
        icon = "⚽" if t["category"] == "sports" else "📈"
        lines.append(
            f"{icon} *{t['title']}*\n"
            f"  {t['outcome']} | ₦{t['amount']:,.0f} | "
            f"{fmt_price(t['price'])} | `{t['status']}` | {t['time']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_markets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching markets…")
    try:
        s_data = await client.list_events(category="sports", size=5)
        c_data = await client.list_events(category="crypto", size=5)
        events = s_data.get("events", []) + c_data.get("events", [])
        if not events:
            await update.message.reply_text("No open markets right now.")
            return
        keyboard = []
        for ev in events[:10]:
            market = ev["markets"][0] if ev.get("markets") else {}
            yes_p  = float(market.get("yesBuyPrice") or 0)
            icon   = "⚽" if ev.get("category") == "sports" else "📈"
            label  = f"{icon} {ev['title'][:30]}… YES:{fmt_price(yes_p)}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"ev:{ev['id']}")])
        await update.message.reply_text(
            "📊 *Open Markets* — tap for details:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 Manual scan started — I'll message you if trades are found."
    )
    app = ctx.application
    asyncio.create_task(sports_scan_job(app))
    asyncio.create_task(crypto_scan_job(app))


# ── Inline button handler ──────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("ev:"):
        event_id = data[3:]
        try:
            ev     = await client.get_event(event_id)
            event  = ev.get("event", ev)
            market = event.get("markets", [{}])[0]
            yes_p  = float(market.get("yesBuyPrice") or market.get("outcome1Price") or 0)
            no_p   = float(market.get("noBuyPrice")  or market.get("outcome2Price") or 0)
            text = (
                f"📋 *{event.get('title','')}*\n\n"
                f"Category: {event.get('category','').title()}\n"
                f"YES: *{fmt_price(yes_p)}* | NO: *{fmt_price(no_p)}*\n"
                f"Volume: ₦{float(event.get('totalVolume',0)):,.0f}\n"
                f"Closes: {str(event.get('closingDate','?'))[:10]}"
            )
            await query.edit_message_text(text, parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")


# ── Startup ────────────────────────────────────────────────────────────────────
async def on_startup(app):
    log.info("Bot online — starting background jobs.")
    await start_background_jobs(app)
    await notify(app,
        "🤖 *Bayse Bot is online!*\n\n"
        "Send /autotrade to start auto-trading.\n"
        "Send /status to see settings."
    )


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("autotrade", cmd_autotrade))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("trades",    cmd_trades))
    app.add_handler(CommandHandler("markets",   cmd_markets))
    app.add_handler(CommandHandler("scan",      cmd_scan))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
