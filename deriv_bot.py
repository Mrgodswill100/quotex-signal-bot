import os, sys, asyncio, json, time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters
)
import websockets

# ── ENV VARS ──────────────────────────────────────────────────────────────────
TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DERIV_APP_ID  = os.environ.get("DERIV_APP_ID", "36544")        # ✅ FIXED: correct public app id
DERIV_API_URL = "wss://ws.derivws.com/websockets/v3?app_id=" + DERIV_APP_ID  # ✅ FIXED: correct URL
DERIV_TOKEN   = os.environ.get("DERIV_API_TOKEN", "")

# ── ASSETS ────────────────────────────────────────────────────────────────────
# ✅ FIXED: All assets now have correct Deriv symbol names
ASSETS = {
    "Volatility 100 (1s)": "1HZ100V",
    "Volatility 75 (1s)":  "1HZ75V",
    "Volatility 50 (1s)":  "1HZ50V",
    "Volatility 25 (1s)":  "1HZ25V",
}

# ── STRATEGY SETTINGS ─────────────────────────────────────────────────────────
TIMEFRAMES     = [5, 10, 15]   # seconds
TRADE_DURATION = 60            # 1 minute
CANDLES_NEEDED = 100
STOCH_K        = 13
STOCH_D        = 3
EMA_PERIOD     = 9

# Conversation states
SELECT_ASSET, ENTER_AMOUNT, ENTER_TRADES = range(3)


# ── DERIV WEBSOCKET HELPERS ───────────────────────────────────────────────────

async def deriv_send(ws, payload: dict) -> dict:
    req_id = int(time.time() * 1000) % 99999
    payload["req_id"] = req_id
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(raw)
        if data.get("req_id") == req_id or data.get("msg_type") in (
            "authorize", "balance", "buy", "proposal_open_contract"
        ):
            if "error" in data:
                raise Exception(f"Deriv error: {data['error']['message']}")
            return data


async def connect_deriv():
    """Connect and authorize with Deriv API."""
    try:
        ws = await asyncio.wait_for(
            websockets.connect(DERIV_API_URL), timeout=15
        )
        resp = await deriv_send(ws, {"authorize": DERIV_TOKEN})
        account  = resp.get("authorize", {})
        balance  = account.get("balance", 0)
        currency = account.get("currency", "USD")
        print(f"✅ Deriv connected | Balance: {balance} {currency}")
        return ws, balance, currency
    except Exception as e:
        print(f"❌ Deriv connection failed: {e}")
        return None, 0, "USD"


async def get_balance(ws) -> float:
    try:
        resp = await deriv_send(ws, {"balance": 1, "account": "current"})
        return float(resp.get("balance", {}).get("balance", 0))
    except:
        return 0.0


async def get_candles(ws, symbol: str, granularity: int, count: int = 100):
    try:
        resp = await deriv_send(ws, {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "end": "latest",
            "count": count,
            "adjust_start_time": 1
        })
        raw_candles = resp.get("candles", [])
        if not raw_candles:
            return None
        candles = [
            {
                "open":  float(c["open"]),
                "max":   float(c["high"]),
                "min":   float(c["low"]),
                "close": float(c["close"]),
                "from":  c["epoch"]
            }
            for c in raw_candles
        ]
        return sorted(candles, key=lambda x: x["from"], reverse=True)
    except Exception as e:
        print(f"get_candles error ({symbol} {granularity}s): {e}")
        return None


async def place_trade(ws, symbol: str, direction: str, amount: float):
    try:
        contract_type = "CALL" if direction == "BUY" else "PUT"
        proposal_resp = await deriv_send(ws, {
            "proposal": 1,
            "amount": str(amount),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": TRADE_DURATION,
            "duration_unit": "s",
            "symbol": symbol
        })
        proposal_id = proposal_resp.get("proposal", {}).get("id")
        if not proposal_id:
            return False, None
        buy_resp    = await deriv_send(ws, {"buy": proposal_id, "price": amount})
        contract_id = buy_resp.get("buy", {}).get("contract_id")
        return (True, contract_id) if contract_id else (False, None)
    except Exception as e:
        print(f"place_trade error: {e}")
        return False, None


async def get_trade_result(ws, contract_id: int) -> float:
    await asyncio.sleep(TRADE_DURATION + 5)
    try:
        resp = await deriv_send(ws, {
            "profit_table": 1,
            "contract_id": contract_id,
            "description": 1
        })
        contracts = resp.get("profit_table", {}).get("transactions", [])
        if contracts:
            return float(contracts[0].get("profit", 0))
        resp2 = await deriv_send(ws, {
            "proposal_open_contract": 1,
            "contract_id": contract_id,
        })
        poc = resp2.get("proposal_open_contract", {})
        return float(poc.get("profit", 0))
    except Exception as e:
        print(f"get_trade_result error: {e}")
        return 0.0


# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_stochastic(candles, k_period=13, d_period=3):
    if len(candles) < k_period + d_period + 1:
        return None
    closes = [c["close"] for c in candles]
    highs  = [c["max"]   for c in candles]
    lows   = [c["min"]   for c in candles]
    k_values = []
    for i in range(d_period + 1):
        wh = max(highs[i:i + k_period])
        wl = min(lows[i:i + k_period])
        k_values.append(50.0 if wh == wl else 100 * (closes[i] - wl) / (wh - wl))
    return round(k_values[0], 2), round(sum(k_values[:d_period]) / d_period, 2), round(k_values[1], 2)


def calc_ema(closes, period=9):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)


def check_ema_crossover(candles, period=9):
    closes = [c["close"] for c in candles]
    if len(closes) < period + 2:
        return None
    current_close  = closes[0]
    previous_close = closes[1]
    ema_now  = calc_ema(closes, period)
    ema_prev = calc_ema(closes[1:], period)
    if ema_now is None or ema_prev is None:
        return None
    if previous_close < ema_prev and current_close > ema_now:
        return "bull"
    elif previous_close > ema_prev and current_close < ema_now:
        return "bear"
    return None


# ── SIGNAL LOGIC ─────────────────────────────────────────────────────────────

async def analyse_signal(ws, symbol: str):
    tasks   = [get_candles(ws, symbol, tf, CANDLES_NEEDED) for tf in TIMEFRAMES]
    results = await asyncio.gather(*tasks)

    stoch_results    = {}
    oversold_count   = 0
    overbought_count = 0

    for tf, candles in zip(TIMEFRAMES, results):
        if not candles or len(candles) < STOCH_K + STOCH_D + 1:
            stoch_results[tf] = None
            continue
        st = calc_stochastic(candles, STOCH_K, STOCH_D)
        stoch_results[tf] = st
        if st:
            k, d, prev_k = st
            if k <= 2 or prev_k <= 2:
                oversold_count += 1
            if k >= 98 or prev_k >= 98:
                overbought_count += 1

    if oversold_count >= 2:
        direction = "BUY"
    elif overbought_count >= 2:
        direction = "SELL"
    else:
        return "WAIT", f"Only {max(oversold_count, overbought_count)}/3 TFs confluent — need 2+", stoch_results

    # EMA confirmation on 5s
    candles_5s = results[0]
    if candles_5s:
        cross = check_ema_crossover(candles_5s, EMA_PERIOD)
        if direction == "BUY" and cross != "bull":
            return "WAIT", "Waiting for 9 EMA bullish crossover on 5s", stoch_results
        if direction == "SELL" and cross != "bear":
            return "WAIT", "Waiting for 9 EMA bearish crossover on 5s", stoch_results

    return direction, "All conditions met ✅", stoch_results


# ── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 Start Trading", callback_data="start_trading")],
        [InlineKeyboardButton("💰 Check Balance",  callback_data="check_balance")],
        [InlineKeyboardButton("⛔ Stop Bot",        callback_data="stop_bot")],
    ]
    await update.message.reply_text(
        "👋 Welcome to *Chima Dtrader AI* 🤖\n\n"
        "🏦 Platform: *Deriv.com*\n"
        "🏦 Account: *DEMO*\n"
        "📈 Strategy: Stochastic Confluence + 9 EMA Cross\n"
        "⏱ Timeframes: 5s · 10s · 15s\n"
        "⏰ Trade Duration: *1 Minute*\n\n"
        "Tap below to begin 👇",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def start_trading(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # ✅ FIXED: Asset buttons now match ASSETS dict
    kb = [
        [InlineKeyboardButton("Volatility 100 (1s)", callback_data="asset_Volatility 100 (1s)")],
        [InlineKeyboardButton("Volatility 75 (1s)",  callback_data="asset_Volatility 75 (1s)")],
        [InlineKeyboardButton("Volatility 50 (1s)",  callback_data="asset_Volatility 50 (1s)")],
        [InlineKeyboardButton("Volatility 25 (1s)",  callback_data="asset_Volatility 25 (1s)")],
    ]
    await q.edit_message_text(
        "📊 *Select Asset to Trade:*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SELECT_ASSET


async def asset_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    asset = q.data.replace("asset_", "")
    ctx.user_data["asset"] = asset
    await q.edit_message_text(
        f"✅ Asset: *{asset}*\n\n"
        "💵 *Enter trade amount in $:*\n_(e.g. type 10)_",
        parse_mode="Markdown"
    )
    return ENTER_AMOUNT


async def amount_entered(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount = float(text)
        if amount < 1:
            await update.message.reply_text("⚠️ Minimum amount is $1. Enter again:")
            return ENTER_AMOUNT
    except:
        await update.message.reply_text("⚠️ Please enter a valid number (e.g. 10):")
        return ENTER_AMOUNT
    ctx.user_data["amount"] = amount
    await update.message.reply_text(
        f"✅ Amount: *${amount}*\n\n"
        "🔢 *How many trades should the bot take?*\n_(e.g. type 5)_",
        parse_mode="Markdown"
    )
    return ENTER_TRADES


async def trades_entered(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        max_trades = int(text)
        if max_trades < 1:
            await update.message.reply_text("⚠️ Minimum is 1 trade. Enter again:")
            return ENTER_TRADES
    except:
        await update.message.reply_text("⚠️ Please enter a whole number (e.g. 5):")
        return ENTER_TRADES

    asset  = ctx.user_data.get("asset")
    amount = ctx.user_data.get("amount")
    ctx.user_data["max_trades"] = max_trades
    ctx.user_data["is_trading"] = True
    ctx.user_data["chat_id"]    = update.effective_chat.id

    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "║  📊 CHIMA DTRADER AI ║\n"
        "╚══════════════════════╝\n\n"
        f"✅ *Session Started!*\n\n"
        f"🏦 Platform: *Deriv.com DEMO*\n"
        f"📊 Asset: *{asset}*\n"
        f"💵 Amount: *${amount}*\n"
        f"🔢 Max Trades: *{max_trades}*\n"
        f"⏰ Duration: *1 Minute*\n\n"
        "🔍 Scanning for signals...",
        parse_mode="Markdown"
    )
    asyncio.create_task(scan_and_trade(update, ctx))
    return ConversationHandler.END


# ── SCAN & TRADE LOOP ─────────────────────────────────────────────────────────

async def scan_and_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id    = ctx.user_data.get("chat_id")
    asset      = ctx.user_data.get("asset")
    amount     = ctx.user_data.get("amount")
    max_trades = ctx.user_data.get("max_trades")
    symbol     = ASSETS.get(asset)
    bot        = ctx.application.bot

    if not symbol:
        await bot.send_message(chat_id, "❌ Unknown asset selected. Please restart.")
        return

    ws, balance, currency = await connect_deriv()
    if not ws:
        await bot.send_message(chat_id,
            "❌ Could not connect to Deriv.\n\n"
            "Please check:\n"
            "1. DERIV_API_TOKEN is set correctly in Render\n"
            "2. Your token has *Trade* permission\n"
            "3. Token was created on *Demo* account\n\n"
            "Tap /start to try again.")
        return

    trades_done  = 0
    total_profit = 0.0

    while trades_done < max_trades and ctx.user_data.get("is_trading", True):
        try:
            direction, reason, stoch_data = await analyse_signal(ws, symbol)

            if direction == "WAIT":
                await asyncio.sleep(1)
                continue

            latest      = await get_candles(ws, symbol, 5, 1)
            entry_price = latest[0]["close"] if latest else 0.0
            balance     = await get_balance(ws)
            entry_time  = datetime.utcnow().strftime("%H:%M:%S")

            stoch_5s  = stoch_data.get(5,  (0, 0, 0)) or (0, 0, 0)
            stoch_10s = stoch_data.get(10, (0, 0, 0)) or (0, 0, 0)
            stoch_15s = stoch_data.get(15, (0, 0, 0)) or (0, 0, 0)
            dir_emoji = "📈" if direction == "BUY" else "📉"

            await bot.send_message(chat_id,
                f"🚨 *TRADE ENTRY*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 Platform: *Deriv DEMO*\n"
                f"📊 Asset: *{asset}*\n"
                f"🎯 Direction: *{direction}* {dir_emoji}\n"
                f"💰 Entry Price: `{entry_price}`\n"
                f"💵 Amount: *${amount}*\n"
                f"⏰ Duration: *1 Minute*\n"
                f"🕐 Time: `{entry_time} UTC`\n\n"
                f"📉 *Stochastic Readings:*\n"
                f"  5s  → K: `{stoch_5s[0]}`\n"
                f"  10s → K: `{stoch_10s[0]}`\n"
                f"  15s → K: `{stoch_15s[0]}`\n\n"
                f"🔢 Trade: *{trades_done + 1}/{max_trades}*\n"
                f"🏦 Balance: *${balance:.2f}*",
                parse_mode="Markdown"
            )

            success, contract_id = await place_trade(ws, symbol, direction, amount)

            if not success:
                await bot.send_message(chat_id, "⚠️ Trade placement failed. Scanning again...")
                await asyncio.sleep(10)
                continue

            trades_done += 1
            await bot.send_message(chat_id,
                "⏳ *Trade placed on Deriv!* Waiting 1 minute for result...",
                parse_mode="Markdown"
            )

            profit      = await get_trade_result(ws, contract_id)
            new_balance = await get_balance(ws)
            total_profit += profit

            win         = profit > 0
            outcome     = "🟢 WIN" if win else "🔴 LOSS"
            profit_str  = f"+${profit:.2f}" if win else f"-${abs(profit):.2f}"

            latest2     = await get_candles(ws, symbol, 5, 1)
            close_price = latest2[0]["close"] if latest2 else 0.0

            await bot.send_message(chat_id,
                f"{'✅' if win else '❌'} *TRADE RESULT*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Asset: *{asset}*\n"
                f"🎯 Direction: *{direction}* {dir_emoji}\n"
                f"💰 Entry: `{entry_price}`\n"
                f"🏁 Close: `{close_price}`\n"
                f"💵 Amount: *${amount}*\n"
                f"📊 Outcome: *{outcome}*\n"
                f"💸 Profit: *{profit_str}*\n"
                f"🏦 Balance: *${new_balance:.2f}*\n\n"
                f"🔢 Trades: *{trades_done}/{max_trades}*\n"
                f"📈 Session P&L: *{'+' if total_profit >= 0 else ''}{total_profit:.2f}*",
                parse_mode="Markdown"
            )

            if trades_done < max_trades:
                await asyncio.sleep(10)

        except Exception as e:
            print(f"Scan/trade error: {e}")
            await asyncio.sleep(10)

    ctx.user_data["is_trading"] = False
    try:
        final_balance = await get_balance(ws)
        await ws.close()
    except:
        final_balance = 0.0

    pnl_emoji = "📈" if total_profit >= 0 else "📉"
    await bot.send_message(chat_id,
        f"🛑 *SESSION COMPLETE*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 Platform: *Deriv DEMO*\n"
        f"📊 Asset: *{asset}*\n"
        f"🔢 Trades: *{trades_done}/{max_trades}*\n"
        f"💸 Total P&L: *{'+' if total_profit >= 0 else ''}{total_profit:.2f}* {pnl_emoji}\n"
        f"🏦 Final Balance: *${final_balance:.2f}*\n\n"
        "Tap /start to begin a new session.",
        parse_mode="Markdown"
    )


# ── BALANCE & STOP ────────────────────────────────────────────────────────────

async def check_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("⏳ Checking Deriv balance...")
    try:
        ws, balance, currency = await connect_deriv()
        if ws:
            await ws.close()
            await q.edit_message_text(
                f"🏦 *Deriv Demo Account Balance*\n\n"
                f"💵 Balance: *${balance:.2f} {currency}*\n\n"
                "Tap /start to go back.",
                parse_mode="Markdown"
            )
        else:
            await q.edit_message_text(
                "❌ Could not connect to Deriv.\n"
                "Check your DERIV_API_TOKEN in Render settings."
            )
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")


async def stop_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["is_trading"] = False
    await q.edit_message_text(
        "⛔ *Bot Stopped*\n\nAll trading halted.\nTap /start to begin again.",
        parse_mode="Markdown"
    )


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["is_trading"] = False
    await update.message.reply_text(
        "⛔ *Bot Stopped*\n\nTap /start to begin again.",
        parse_mode="Markdown"
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=== CHIMA DTRADER AI (DERIV) STARTING ===")
    print(f"TELEGRAM TOKEN : {'SET' if TOKEN else 'MISSING!'}")
    print(f"DERIV APP ID   : {DERIV_APP_ID}")
    print(f"DERIV API TOKEN: {'SET' if DERIV_TOKEN else 'MISSING!'}")
    print(f"DERIV URL      : {DERIV_API_URL}")

    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN missing")
        sys.exit(1)
    if not DERIV_TOKEN:
        print("ERROR: DERIV_API_TOKEN missing — set it in Render environment variables")
        sys.exit(1)

    bot_app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_trading, pattern="^start_trading$")],
        states={
            SELECT_ASSET: [CallbackQueryHandler(asset_selected, pattern="^asset_")],
            ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_entered)],
            ENTER_TRADES: [MessageHandler(filters.TEXT & ~filters.COMMAND, trades_entered)],
        },
        fallbacks=[CommandHandler("stop", stop_cmd)],
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("stop",  stop_cmd))
    bot_app.add_handler(conv)
    bot_app.add_handler(CallbackQueryHandler(check_balance, pattern="^check_balance$"))
    bot_app.add_handler(CallbackQueryHandler(stop_bot,      pattern="^stop_bot$"))

    print("✅ Bot is running...")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
