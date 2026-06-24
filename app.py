import os, sys, asyncio, threading, time
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters
)
from iqoptionapi.stable_api import IQ_Option

# ── ENV VARS ──────────────────────────────────────────────────────────────────
TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
IQ_EMAIL   = os.environ.get("IQ_EMAIL", "")
IQ_PASSWORD= os.environ.get("IQ_PASSWORD", "")

# ── ASSETS ────────────────────────────────────────────────────────────────────
ASSETS = {
    "US500 OTC":  "USSC-OTC",
    "EURUSD OTC": "EURUSD-OTC",
    "GBPUSD OTC": "GBPUSD-OTC",
    "Gold OTC":   "XAUUSD-OTC",
}

# Timeframes in seconds for IQ Option API
TIMEFRAMES = [5, 10, 15, 30]  # seconds

TRADE_DURATION = 60  # 1 minute in seconds

# Stochastic settings
STOCH_K   = 13
STOCH_D   = 3
STOCH_OB  = 95   # near 100 overbought
STOCH_OS  = 5    # near 1 oversold
EMA_PERIOD = 9
CANDLES_NEEDED = 100

# Conversation states
SELECT_ASSET, ENTER_AMOUNT, ENTER_TRADES = range(3)

# ── IQ OPTION CONNECTION ──────────────────────────────────────────────────────

def connect_iq():
    print(f"Attempting IQ Option connection with email: {IQ_EMAIL[:4]}***")
    try:
        api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
        check, reason = api.connect()
        print(f"IQ Connect result: check={check}, reason={reason}")
        if not check:
            print(f"❌ IQ Option connection failed: {reason}")
            return None
        api.change_balance("PRACTICE")
        balance = api.get_balance()
        print(f"✅ Connected to IQ Option DEMO | Balance: {balance}")
        return api
    except Exception as e:
        print(f"❌ IQ Option exception: {type(e).__name__}: {e}")
        return None

# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_stochastic(candles, k_period=13, d_period=3):
    """Calculate Stochastic Oscillator - returns (K, D, prev_K)"""
    if len(candles) < k_period + d_period + 1:
        return None
    
    closes = [c["close"] for c in candles]
    highs  = [c["max"]   for c in candles]
    lows   = [c["min"]   for c in candles]
    
    k_values = []
    for i in range(d_period + 1):
        window_h = max(highs[i:i + k_period])
        window_l = min(lows[i:i + k_period])
        if window_h == window_l:
            k_values.append(50.0)
        else:
            k = 100 * (closes[i] - window_l) / (window_h - window_l)
            k_values.append(k)
    
    k_current  = k_values[0]
    d_current  = sum(k_values[:d_period]) / d_period
    k_previous = k_values[1]
    
    return round(k_current, 2), round(d_current, 2), round(k_previous, 2)


def calc_ema(closes, period=9):
    """Calculate EMA"""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)


def check_ema_crossover(candles, period=9):
    """
    Check 9 EMA crossover on latest candles.
    Returns: 'bull' if EMA crossed up, 'bear' if crossed down, None if no cross
    """
    closes = [c["close"] for c in candles]
    if len(closes) < period + 2:
        return None

    # We need current and previous EMA vs price relationship
    # Crossover: price crosses above/below EMA
    current_close  = closes[0]
    previous_close = closes[1]
    
    # Calculate EMA on slightly shifted data to get prev EMA
    ema_now  = calc_ema(closes, period)
    ema_prev = calc_ema(closes[1:], period)
    
    if ema_now is None or ema_prev is None:
        return None
    
    # Bull crossover: price was below EMA, now above
    if previous_close < ema_prev and current_close > ema_now:
        return "bull"
    # Bear crossover: price was above EMA, now below
    elif previous_close > ema_prev and current_close < ema_now:
        return "bear"
    
    return None


# ── SIGNAL LOGIC ─────────────────────────────────────────────────────────────

async def get_candles_iq(api, asset_name, tf_seconds, count=100):
    """Fetch candles from IQ Option"""
    try:
        end_time = time.time()
        candles = api.get_candles(asset_name, tf_seconds, count, end_time)
        if candles:
            # Sort newest first
            return sorted(candles, key=lambda x: x["from"], reverse=True)
        return None
    except Exception as e:
        print(f"Error fetching candles {asset_name} {tf_seconds}s: {e}")
        return None


async def analyse_signal(api, asset_name):
    """
    Strategy:
    1. Stochastic (13,3,3) K line must touch level 1 or 100
       on at least 3 out of 4 timeframes (5s, 10s, 15s, 30s)
    2. Confirm with 9 MA crossover on 5s chart
    Returns: 'BUY', 'SELL', or 'WAIT'
    """
    # Fetch candles for all 4 timeframes concurrently
    tasks = [get_candles_iq(api, asset_name, tf, CANDLES_NEEDED) for tf in TIMEFRAMES]
    results = await asyncio.gather(*tasks)

    stoch_results = {}
    for tf, candles in zip(TIMEFRAMES, results):
        if not candles or len(candles) < STOCH_K + STOCH_D + 1:
            stoch_results[tf] = None
            continue
        st = calc_stochastic(candles, STOCH_K, STOCH_D)
        stoch_results[tf] = st  # (K, D, prev_K)

    # Count TFs that touched level 1 (oversold) or 100 (overbought)
    # Touched = current K or previous K reached the level
    oversold_count   = 0
    overbought_count = 0

    for tf in TIMEFRAMES:
        st = stoch_results.get(tf)
        if st is None:
            continue
        k, d, prev_k = st

        # Touched level 1 — K at or below 2 now or on previous candle
        if k <= 2 or prev_k <= 2:
            oversold_count += 1

        # Touched level 100 — K at or above 98 now or on previous candle
        if k >= 98 or prev_k >= 98:
            overbought_count += 1

    # Need at least 3 out of 4 TFs
    if oversold_count >= 3:
        direction = "BUY"
    elif overbought_count >= 3:
        direction = "SELL"
    else:
        return "WAIT", f"Only {max(oversold_count, overbought_count)}/4 TFs confluent — need 3+", stoch_results

    # Step 2: Confirm with 9 MA crossover on 5s chart
    candles_5s = results[0]  # TIMEFRAMES[0] = 5s
    if candles_5s:
        cross = check_ema_crossover(candles_5s, EMA_PERIOD)
        if direction == "BUY" and cross != "bull":
            return "WAIT", "Waiting for 9 MA bullish crossover on 5s", stoch_results
        if direction == "SELL" and cross != "bear":
            return "WAIT", "Waiting for 9 MA bearish crossover on 5s", stoch_results

    return direction, "All conditions met ✅", stoch_results


# ── TRADE EXECUTION ───────────────────────────────────────────────────────────

async def place_trade(api, asset_name, direction, amount):
    """Place a binary options trade on IQ Option demo"""
    try:
        action = "call" if direction == "BUY" else "put"
        success, trade_id = api.buy(amount, asset_name, action, TRADE_DURATION)
        if success:
            return True, trade_id
        return False, None
    except Exception as e:
        print(f"Trade error: {e}")
        return False, None


async def get_trade_result(api, trade_id):
    """Wait for trade to close and get result"""
    await asyncio.sleep(TRADE_DURATION + 5)  # Wait for expiry + buffer
    try:
        result = api.check_win_v4(trade_id)
        return result  # Returns profit amount (positive = win, negative = loss)
    except Exception as e:
        print(f"Result check error: {e}")
        return None


# ── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 Start Trading", callback_data="start_trading")],
        [InlineKeyboardButton("💰 Check Balance",  callback_data="check_balance")],
        [InlineKeyboardButton("⛔ Stop Bot",        callback_data="stop_bot")],
    ]
    await update.message.reply_text(
        "👋 Welcome to *Chima Dtrader AI* 🤖\n\n"
        "🏦 Account: *DEMO*\n"
        "📈 Strategy: Stochastic Confluence + 9 EMA Cross\n"
        "⏱ Timeframes: 5s · 10s · 15s · 30s\n"
        "⏰ Trade Duration: *1 Minute*\n\n"
        "Tap below to begin 👇",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def start_trading(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    kb = [
        [InlineKeyboardButton("US500 OTC",  callback_data="asset_US500 OTC")],
        [InlineKeyboardButton("EURUSD OTC", callback_data="asset_EURUSD OTC")],
        [InlineKeyboardButton("GBPUSD OTC", callback_data="asset_GBPUSD OTC")],
        [InlineKeyboardButton("Gold OTC",   callback_data="asset_Gold OTC")],
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
        "💵 *Enter trade amount in $:*\n"
        "_(e.g. type 50)_",
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
        await update.message.reply_text("⚠️ Please enter a valid number (e.g. 50):")
        return ENTER_AMOUNT
    
    ctx.user_data["amount"] = amount
    await update.message.reply_text(
        f"✅ Amount: *${amount}*\n\n"
        "🔢 *How many trades should the bot take?*\n"
        "_(e.g. type 5)_",
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
    
    ctx.user_data["max_trades"]    = max_trades
    ctx.user_data["trades_done"]   = 0
    ctx.user_data["total_profit"]  = 0.0
    ctx.user_data["is_trading"]    = True
    ctx.user_data["chat_id"]       = update.effective_chat.id

    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "║  📊 CHIMA DTRADER AI ║\n"
        "╚══════════════════════╝\n\n"
        f"✅ *Session Started!*\n\n"
        f"📊 Asset: *{asset}*\n"
        f"💵 Amount: *${amount}*\n"
        f"🔢 Max Trades: *{max_trades}*\n"
        f"🏦 Account: *DEMO*\n\n"
        "🔍 Scanning for signals...",
        parse_mode="Markdown"
    )
    
    # Start scanning in background
    asyncio.create_task(scan_and_trade(update, ctx))
    return ConversationHandler.END


async def scan_and_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Background task: scan for signals and trade"""
    chat_id    = ctx.user_data.get("chat_id")
    asset      = ctx.user_data.get("asset")
    amount     = ctx.user_data.get("amount")
    max_trades = ctx.user_data.get("max_trades")
    asset_name = ASSETS.get(asset)
    bot        = ctx.application.bot

    # Connect to IQ Option
    api = connect_iq()
    if not api:
        await bot.send_message(chat_id,
            "❌ Failed to connect to IQ Option. Check credentials.")
        return

    trades_done  = 0
    total_profit = 0.0

    while trades_done < max_trades and ctx.user_data.get("is_trading", True):
        try:
            direction, reason, stoch_data = await analyse_signal(api, asset_name)
            
            if direction == "WAIT":
                await asyncio.sleep(5)  # Check every 5 seconds
                continue
            
            # Get current price and balance
            price   = api.get_candles(asset_name, 5, 1, time.time())
            balance = api.get_balance()
            entry_price = price[0]["close"] if price else 0.0
            entry_time  = datetime.utcnow().strftime("%H:%M:%S")
            
            # ── MESSAGE 1: Entry Alert ──────────────────────────────────────
            direction_emoji = "📈" if direction == "BUY" else "📉"
            stoch_5s  = stoch_data.get(5,  (0,0,0))
            stoch_10s = stoch_data.get(10, (0,0,0))
            stoch_15s = stoch_data.get(15, (0,0,0))
            stoch_30s = stoch_data.get(30, (0,0,0))

            await bot.send_message(chat_id,
                f"🚨 *TRADE ENTRY*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Asset: *{asset}*\n"
                f"🎯 Direction: *{direction}* {direction_emoji}\n"
                f"💰 Entry Price: `{entry_price}`\n"
                f"💵 Amount: *${amount}*\n"
                f"⏰ Duration: *1 Minute*\n"
                f"🕐 Time: `{entry_time} UTC`\n\n"
                f"📉 *Stochastic Readings:*\n"
                f"  5s  → K: `{stoch_5s[0]}`\n"
                f"  10s → K: `{stoch_10s[0]}`\n"
                f"  15s → K: `{stoch_15s[0]}`\n"
                f"  30s → K: `{stoch_30s[0]}`\n\n"
                f"🔢 Trade: *{trades_done + 1}/{max_trades}*\n"
                f"🏦 Balance: *${balance:.2f}*",
                parse_mode="Markdown"
            )

            # Place the trade
            success, trade_id = await place_trade(api, asset_name, direction, amount)

            if not success:
                await bot.send_message(chat_id,
                    "⚠️ Trade placement failed. Scanning again...")
                await asyncio.sleep(10)
                continue

            trades_done += 1

            # ── Wait and get result ─────────────────────────────────────────
            await bot.send_message(chat_id,
                f"⏳ Trade placed! Waiting 1 minute for result...")

            profit = await get_trade_result(api, trade_id)
            new_balance = api.get_balance()

            if profit is None:
                await bot.send_message(chat_id, "⚠️ Could not retrieve trade result.")
                continue

            total_profit += profit
            win  = profit > 0
            outcome_emoji = "🟢 WIN" if win else "🔴 LOSS"
            profit_str    = f"+${profit:.2f}" if win else f"-${abs(profit):.2f}"

            # Get close price
            close_candle = api.get_candles(asset_name, 5, 1, time.time())
            close_price  = close_candle[0]["close"] if close_candle else 0.0

            # ── MESSAGE 2: Result ───────────────────────────────────────────
            await bot.send_message(chat_id,
                f"{'✅' if win else '❌'} *TRADE RESULT*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Asset: *{asset}*\n"
                f"🎯 Direction: *{direction}* {direction_emoji}\n"
                f"💰 Entry Price: `{entry_price}`\n"
                f"🏁 Close Price: `{close_price}`\n"
                f"💵 Amount: *${amount}*\n"
                f"📊 Outcome: *{outcome_emoji}*\n"
                f"💸 Profit: *{profit_str}*\n"
                f"🏦 Balance: *${new_balance:.2f}*\n\n"
                f"🔢 Trades: *{trades_done}/{max_trades}*\n"
                f"📈 Session P&L: *{'+' if total_profit>=0 else ''}{total_profit:.2f}*",
                parse_mode="Markdown"
            )

            # Small pause between trades
            if trades_done < max_trades:
                await asyncio.sleep(10)

        except Exception as e:
            print(f"Scan/trade error: {e}")
            await asyncio.sleep(10)

    # ── SESSION COMPLETE ────────────────────────────────────────────────────
    ctx.user_data["is_trading"] = False
    final_balance = api.get_balance()
    pnl_emoji = "📈" if total_profit >= 0 else "📉"

    await bot.send_message(chat_id,
        f"🛑 *TRADING SESSION COMPLETE*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Asset: *{asset}*\n"
        f"🔢 Trades Taken: *{trades_done}/{max_trades}*\n"
        f"💸 Total P&L: *{'+' if total_profit>=0 else ''}{total_profit:.2f}* {pnl_emoji}\n"
        f"🏦 Final Balance: *${final_balance:.2f}*\n\n"
        "Tap /start to begin a new session.",
        parse_mode="Markdown"
    )


async def check_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("⏳ Checking balance...")
    try:
        api = connect_iq()
        if api:
            balance = api.get_balance()
            await q.edit_message_text(
                f"🏦 *Demo Account Balance*\n\n"
                f"💵 Balance: *${balance:.2f}*\n\n"
                "Tap /start to go back.",
                parse_mode="Markdown"
            )
        else:
            await q.edit_message_text("❌ Could not connect to IQ Option.")
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")


async def stop_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["is_trading"] = False
    await q.edit_message_text(
        "⛔ *Bot Stopped*\n\n"
        "All trading has been halted.\n"
        "Tap /start to begin again.",
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
    print("=== CHIMA DTRADER AI STARTING ===")
    print(f"TOKEN:    {'SET' if TOKEN else 'MISSING!'}")
    print(f"IQ EMAIL: {'SET' if IQ_EMAIL else 'MISSING!'}")
    print(f"IQ PASS:  {'SET' if IQ_PASSWORD else 'MISSING!'}")

    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN missing")
        sys.exit(1)

    bot_app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_trading, pattern="^start_trading$")],
        states={
            SELECT_ASSET:  [CallbackQueryHandler(asset_selected, pattern="^asset_")],
            ENTER_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_entered)],
            ENTER_TRADES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, trades_entered)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("stop",  stop_cmd))
    bot_app.add_handler(CallbackQueryHandler(check_balance, pattern="^check_balance$"))
    bot_app.add_handler(CallbackQueryHandler(stop_bot,      pattern="^stop_bot$"))
    bot_app.add_handler(conv)

    print("🤖 Bot polling started!")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def index():
        return "Chima Dtrader AI is running."

    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True
    ).start()
    main()
