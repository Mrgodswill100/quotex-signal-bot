import os
import asyncio
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler
)

TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TD_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TD_BASE    = "https://api.twelvedata.com"

# ── Pairs ─────────────────────────────────────────────────────────────────────
FOREX_PAIRS = [
    ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF"],
    ["AUD/USD", "USD/CAD", "NZD/USD", "EUR/GBP"],
    ["EUR/JPY", "GBP/JPY", "EUR/AUD", "GBP/AUD"],
    ["AUD/JPY", "CAD/JPY", "CHF/JPY", "NZD/JPY"],
    ["EUR/CAD", "EUR/CHF", "GBP/CAD", "GBP/CHF"],
    ["AUD/CAD", "AUD/CHF", "AUD/NZD", "NZD/CAD"],
    ["USD/SGD", "USD/MXN", "USD/ZAR", "USD/NOK"],
]

DURATIONS = ["1 min", "5 min", "15 min", "30 min", "1 hour"]

# trade duration → 3 TFs to analyse (entry TF, confirmation TF, trend TF)
TIMEFRAMES = {
    "1 min":  [("1min","M1"),  ("5min","M5"),  ("15min","M15")],
    "5 min":  [("5min","M5"),  ("15min","M15"),("1h","H1")],
    "15 min": [("15min","M15"),("1h","H1"),    ("4h","H4")],
    "30 min": [("30min","M30"),("1h","H1"),    ("4h","H4")],
    "1 hour": [("1h","H1"),    ("4h","H4"),    ("1day","D1")],
}

# Minimum net confluence score to emit BUY/SELL (vs WAIT)
# Total max per TF = 13, 3 TFs = 39 max. We require >20% alignment minimum.
MIN_CONFLUENCE = 9

SELECT_PAIR, SELECT_DURATION = range(2)


# ── Data fetching ─────────────────────────────────────────────────────────────

async def fetch_candles(pair: str, interval: str, outputsize: int = 100) -> list | None:
    params = {
        "symbol": pair, "interval": interval,
        "outputsize": outputsize, "apikey": TD_API_KEY, "format": "JSON",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{TD_BASE}/time_series", params=params)
            data = r.json()
        if data.get("status") == "error" or "values" not in data:
            return None
        return data["values"]  # newest first
    except Exception:
        return None


# ── Indicator calculations ────────────────────────────────────────────────────

def get_closes(candles): return [float(c["close"]) for c in candles]
def get_highs(candles):  return [float(c["high"])  for c in candles]
def get_lows(candles):   return [float(c["low"])   for c in candles]


def calc_rsi(prices: list, period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    p = list(reversed(prices))
    gains, losses = [], []
    for i in range(1, period + 1):
        d = p[i] - p[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains) / period
    al = sum(losses) / period
    for i in range(period + 1, len(p)):
        d = p[i] - p[i-1]
        ag = (ag * (period-1) + max(d,0))  / period
        al = (al * (period-1) + max(-d,0)) / period
    if al == 0: return 100.0
    return round(100 - (100 / (1 + ag/al)), 2)


def calc_ema(prices: list, period: int) -> float | None:
    if len(prices) < period: return None
    p = list(reversed(prices))
    k = 2 / (period + 1)
    ema = sum(p[:period]) / period
    for x in p[period:]:
        ema = x * k + ema * (1 - k)
    return round(ema, 6)


def calc_macd(prices: list) -> tuple | None:
    if len(prices) < 35: return None
    p = list(reversed(prices))
    k12, k26, k9 = 2/13, 2/27, 2/10
    e12 = sum(p[:12]) / 12
    e26 = sum(p[:26]) / 26
    macd_series = []
    for i in range(26, len(p)):
        e12 = p[i]*k12 + e12*(1-k12)
        e26 = p[i]*k26 + e26*(1-k26)
        macd_series.append(e12 - e26)
    if len(macd_series) < 9: return None
    sig = sum(macd_series[:9]) / 9
    for v in macd_series[9:]:
        sig = v*k9 + sig*(1-k9)
    macd_line = macd_series[-1]
    hist = round(macd_line - sig, 6)
    # Check for recent crossover (last 3 bars)
    prev_macd = macd_series[-2] if len(macd_series) >= 2 else macd_line
    return round(macd_line, 6), round(sig, 6), hist, prev_macd


def calc_bollinger(prices: list, period: int = 20) -> dict | None:
    if len(prices) < period: return None
    window = [float(x) for x in prices[:period]]
    mid = sum(window) / period
    std = (sum((x - mid)**2 for x in window) / period) ** 0.5
    upper = mid + 2*std
    lower = mid - 2*std
    cur = prices[0]
    pct_b = (cur - lower) / (upper - lower) if upper != lower else 0.5
    # Bandwidth (volatility measure)
    bw = (upper - lower) / mid if mid != 0 else 0
    return {
        "upper": round(upper, 6), "mid": round(mid, 6),
        "lower": round(lower, 6), "pct_b": round(pct_b, 4),
        "bw": round(bw, 6), "cur": round(cur, 6),
    }


def calc_stochastic(candles: list, k_period: int = 14, d_period: int = 3) -> dict | None:
    if len(candles) < k_period + d_period: return None
    cl = get_closes(candles)
    hi = get_highs(candles)
    lo = get_lows(candles)
    k_vals = []
    for i in range(d_period + 1):
        whi = max(hi[i:i+k_period])
        wlo = min(lo[i:i+k_period])
        k_vals.append(100*(cl[i]-wlo)/(whi-wlo) if whi != wlo else 50.0)
    k = round(k_vals[0], 2)
    d = round(sum(k_vals[:d_period]) / d_period, 2)
    prev_k = k_vals[1]
    return {"k": k, "d": d, "prev_k": prev_k}


def calc_atr(candles: list, period: int = 14) -> float | None:
    """Average True Range — measures volatility."""
    if len(candles) < period + 1: return None
    cl = get_closes(candles)
    hi = get_highs(candles)
    lo = get_lows(candles)
    trs = []
    for i in range(period):
        tr = max(hi[i] - lo[i], abs(hi[i] - cl[i+1]), abs(lo[i] - cl[i+1]))
        trs.append(tr)
    return round(sum(trs) / period, 6)


# ── Intelligent scoring engine ────────────────────────────────────────────────
#
# Scoring philosophy:
#   • Each indicator votes with a weighted score
#   • Crossovers & extreme readings score higher than mild signals
#   • All 3 TFs must lean same direction for signal to fire
#   • Low volatility (squeeze) or conflicting signals → WAIT
#
# Max score per TF: ±13
# 3 TFs total: ±39
# MIN_CONFLUENCE = 9 → ~23% minimum — conservative but not too restrictive

def score_timeframe(candles: list) -> dict:
    score = 0
    details = {}
    cl = get_closes(candles)

    # ── RSI (max ±3) ─────────────────────────────────────────────────────────
    rsi = calc_rsi(cl)
    if rsi is not None:
        if rsi <= 20:    score += 3; lbl = f"Extremely oversold ({rsi}) 🔥"
        elif rsi <= 30:  score += 2; lbl = f"Oversold ({rsi})"
        elif rsi <= 42:  score += 1; lbl = f"Mild bullish ({rsi})"
        elif rsi >= 80:  score -= 3; lbl = f"Extremely overbought ({rsi}) 🔥"
        elif rsi >= 70:  score -= 2; lbl = f"Overbought ({rsi})"
        elif rsi >= 58:  score -= 1; lbl = f"Mild bearish ({rsi})"
        else:            lbl = f"Neutral ({rsi})"
        details["rsi"] = {"value": rsi, "label": lbl}

    # ── MACD (max ±3) ────────────────────────────────────────────────────────
    macd_res = calc_macd(cl)
    if macd_res:
        macd_line, sig_line, hist, prev_macd = macd_res
        # Fresh crossover in last bar = +1 bonus
        crossed_up   = macd_line > sig_line and prev_macd <= sig_line
        crossed_down = macd_line < sig_line and prev_macd >= sig_line
        if macd_line > sig_line:
            base = 3 if abs(hist) > 0.00005 else 1
            score += base + (1 if crossed_up else 0)
            lbl = ("Fresh bullish crossover ✅" if crossed_up
                   else ("Strong bullish momentum" if base == 3 else "Bullish"))
        else:
            base = 3 if abs(hist) > 0.00005 else 1
            score -= base + (1 if crossed_down else 0)
            lbl = ("Fresh bearish crossover ❌" if crossed_down
                   else ("Strong bearish momentum" if base == 3 else "Bearish"))
        details["macd"] = {"macd": macd_line, "signal": sig_line, "hist": hist, "label": lbl}

    # ── EMA 9/21/50 triple alignment (max ±3) ────────────────────────────────
    ema9  = calc_ema(cl, 9)
    ema21 = calc_ema(cl, 21)
    ema50 = calc_ema(cl, 50)
    if ema9 and ema21 and ema50:
        if ema9 > ema21 > ema50:
            score += 3; lbl = "Bull stack: 9>21>50 ✅"
        elif ema9 > ema21:
            score += 1; lbl = "Bullish (9>21)"
        elif ema9 < ema21 < ema50:
            score -= 3; lbl = "Bear stack: 9<21<50 ❌"
        elif ema9 < ema21:
            score -= 1; lbl = "Bearish (9<21)"
        else:
            lbl = "Mixed EMAs"
        details["ema"] = {"e9": ema9, "e21": ema21, "e50": ema50, "label": lbl}

    # ── Bollinger Bands (max ±2) ─────────────────────────────────────────────
    bb = calc_bollinger(cl)
    if bb:
        pb = bb["pct_b"]
        # Low bandwidth = squeeze = don't trade, penalise slightly
        if bb["bw"] < 0.001:
            lbl = "Squeeze — low volatility ⚠️"
            score -= 1
        elif pb <= 0.05:
            score += 2; lbl = f"Price at lower band — reversal zone 🔥"
        elif pb <= 0.2:
            score += 1; lbl = f"Near lower band"
        elif pb >= 0.95:
            score -= 2; lbl = f"Price at upper band — reversal zone 🔥"
        elif pb >= 0.8:
            score -= 1; lbl = f"Near upper band"
        else:
            lbl = f"Mid-band ({round(pb*100,1)}%)"
        details["bb"] = {"pct_b": pb, "bw": bb["bw"], "label": lbl}

    # ── Stochastic (max ±2) ──────────────────────────────────────────────────
    stoch = calc_stochastic(candles)
    if stoch:
        k, d, prev_k = stoch["k"], stoch["d"], stoch["prev_k"]
        crossed_up_s   = k > d and prev_k <= d
        crossed_down_s = k < d and prev_k >= d
        if k < 20 and d < 20:
            score += 2; lbl = f"Oversold K={k} D={d} 🔥"
        elif k < 20:
            score += 1; lbl = f"Oversold zone K={k}"
        elif k > 80 and d > 80:
            score -= 2; lbl = f"Overbought K={k} D={d} 🔥"
        elif k > 80:
            score -= 1; lbl = f"Overbought zone K={k}"
        elif crossed_up_s:
            score += 1; lbl = f"Bullish cross K={k} D={d}"
        elif crossed_down_s:
            score -= 1; lbl = f"Bearish cross K={k} D={d}"
        else:
            lbl = f"Neutral K={k} D={d}"
        details["stoch"] = {"k": k, "d": d, "label": lbl}

    return {"score": score, "details": details, "error": False}


async def analyse_pair(pair: str, duration: str) -> dict:
    tf_list = TIMEFRAMES[duration]
    tf_results = {}
    total_score = 0
    any_data = False

    # Fetch all TFs concurrently
    tasks = [fetch_candles(pair, interval, 100) for interval, _ in tf_list]
    candle_sets = await asyncio.gather(*tasks)

    for (interval, label), candles in zip(tf_list, candle_sets):
        if not candles or len(candles) < 50:
            tf_results[label] = {"error": True, "score": 0, "details": {}}
            continue
        any_data = True
        result = score_timeframe(candles)
        tf_results[label] = result
        total_score += result["score"]

    if not any_data:
        return {"error": True, "message": "Could not fetch market data. Check API key or try again."}

    # ── Confluence logic ──────────────────────────────────────────────────────
    # Extra check: all non-error TFs must agree in direction (same sign)
    # If they disagree → WAIT regardless of total score
    scored_tfs = [r for r in tf_results.values() if not r.get("error")]
    bulls = sum(1 for r in scored_tfs if r["score"] > 0)
    bears = sum(1 for r in scored_tfs if r["score"] < 0)
    n = len(scored_tfs)
    all_agree = (bulls == n) or (bears == n)

    # Compute confidence % from score magnitude
    max_possible = n * 14  # slightly above theoretical max for breathing room
    raw_pct = min(abs(total_score) / max_possible * 100, 95)
    strength = round(raw_pct)

    if total_score >= MIN_CONFLUENCE and all_agree:
        direction, emoji, sig_emoji = "BUY",  "🟢", "📈"
    elif total_score <= -MIN_CONFLUENCE and all_agree:
        direction, emoji, sig_emoji = "SELL", "🔴", "📉"
    else:
        direction, emoji, sig_emoji = "WAIT", "🟡", "⏳"
        strength = 0

    return {
        "error": False,
        "direction": direction,
        "emoji": emoji,
        "signal_emoji": sig_emoji,
        "strength": strength,
        "total_score": total_score,
        "all_agree": all_agree,
        "tf_results": tf_results,
        "tfs": [label for _, label in tf_list],
    }


# ── Message formatter ─────────────────────────────────────────────────────────

def format_signal(pair: str, duration: str, analysis: dict) -> str:
    now = datetime.utcnow().strftime("%H:%M UTC  %d %b %Y")
    tfs = analysis["tfs"]

    lines = [
        "╔══════════════════════════════╗",
        "║   📊  QUOTEX SIGNAL BOT      ║",
        "╚══════════════════════════════╝",
        "",
        f"💱  Pair:       *{pair}*",
        f"⏱   Duration:  *{duration}*",
        f"🕐  Time:       {now}",
        "",
        "━━━━  MULTI-TIMEFRAME ANALYSIS  ━━━━",
    ]

    for tf in tfs:
        r = analysis["tf_results"].get(tf, {})
        if r.get("error"):
            lines += ["", f"📌 *{tf}* — ⚠️ No data"]
            continue
        sc = r["score"]
        d  = r["details"]
        bias = "🟢 Bullish" if sc > 0 else ("🔴 Bearish" if sc < 0 else "🟡 Neutral")
        lines += ["", f"📌 *{tf}* — {bias}  (score {sc:+d})"]
        for key, icon in [("rsi","•"),("macd","•"),("ema","•"),("bb","•"),("stoch","•")]:
            if key in d:
                lines.append(f"  {icon} {key.upper():<5} → {d[key]['label']}")

    # Confluence bar
    pct = analysis["strength"]
    fill  = min(int(pct / 10), 10)
    bar   = "█" * fill + "░" * (10 - fill)

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]

    if analysis["direction"] == "WAIT":
        reason = "Timeframes disagree" if not analysis.get("all_agree") else "Confluence too weak"
        lines += [
            "🟡  *SIGNAL:  WAIT ⏳*",
            "",
            f"_Reason: {reason}._",
            "_Skip this setup — wait for cleaner conditions._",
        ]
    else:
        lines += [
            f"{analysis['emoji']}  *SIGNAL:  {analysis['direction']} {analysis['signal_emoji']}*",
            f"📶  Confidence:  *{pct}%*",
            f"📊  Confluence:  `[{bar}]`",
            f"✅  TF Agreement: {'All aligned' if analysis['all_agree'] else 'Partial'}",
            "",
            f"_Enter at open of next {duration} candle._",
        ]

    lines += [
        "",
        "⚠️ _For educational purposes only. Trade responsibly._",
    ]
    return "\n".join(lines)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📊 Generate Signal", callback_data="new_signal")]]
    await update.message.reply_text(
        "👋 Welcome to *Quotex Signal Bot*!\n\n"
        "🧠 *Intelligent multi-timeframe analysis* using:\n"
        "• RSI (14)  • MACD (12/26/9)\n"
        "• EMA 9/21/50 triple stack\n"
        "• Bollinger Bands  • Stochastic (14,3)\n\n"
        "📡 *Live market data* from Twelve Data\n"
        "✅ Signal only fires when *all timeframes agree*\n"
        "⏳ Otherwise it tells you to *WAIT*\n\n"
        "Tap below to get started 👇",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def new_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = []
    for row in FOREX_PAIRS:
        kb.append([InlineKeyboardButton(p, callback_data=f"pair_{p}") for p in row])
    await query.edit_message_text(
        "💱 *Select a Forex pair:*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return SELECT_PAIR


async def pair_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair = query.data.replace("pair_", "")
    ctx.user_data["pair"] = pair
    kb = [[InlineKeyboardButton(d, callback_data=f"dur_{d}")] for d in DURATIONS]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="new_signal")])
    await query.edit_message_text(
        f"✅ Pair: *{pair}*\n\n⏱ *Select trade duration:*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return SELECT_DURATION


async def duration_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    duration = query.data.replace("dur_", "")
    pair = ctx.user_data.get("pair", "EUR/USD")

    await query.edit_message_text(
        f"📡 Fetching live data for *{pair}*...\n"
        f"🧠 Analysing 3 timeframes with 5 indicators ⏳",
        parse_mode="Markdown",
    )

    analysis = await analyse_pair(pair, duration)

    if analysis.get("error"):
        await query.edit_message_text(
            f"⚠️ *Error:* {analysis['message']}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Retry", callback_data=f"dur_{duration}"),
                InlineKeyboardButton("🔙 Back",  callback_data="new_signal"),
            ]]),
        )
        return SELECT_DURATION

    msg = format_signal(pair, duration, analysis)
    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data=f"dur_{duration}"),
            InlineKeyboardButton("💱 New Pair", callback_data="new_signal"),
        ]]),
    )
    return SELECT_DURATION


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Quotex Signal Bot — How it works*\n\n"
        "1️⃣ Select a Forex pair\n"
        "2️⃣ Select your trade duration\n"
        "3️⃣ Bot fetches *live candles* from Twelve Data\n"
        "4️⃣ Calculates 5 indicators across 3 timeframes\n"
        "5️⃣ Only fires BUY/SELL when *all TFs agree*\n"
        "6️⃣ Otherwise returns *WAIT* to protect your account\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧠 *Indicators used:*\n"
        "• RSI (14) — overbought/oversold\n"
        "• MACD (12,26,9) — momentum + crossovers\n"
        "• EMA 9/21/50 — triple stack trend alignment\n"
        "• Bollinger Bands (20) — volatility + squeeze\n"
        "• Stochastic (14,3) — timing entries\n\n"
        "⚠️ _For educational purposes only._",
        parse_mode="Markdown",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    if not TD_API_KEY:
        print("⚠️  WARNING: TWELVE_DATA_API_KEY not set")

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(new_signal, pattern="^new_signal$")],
        states={
            SELECT_PAIR: [CallbackQueryHandler(pair_selected, pattern="^pair_")],
            SELECT_DURATION: [
                CallbackQueryHandler(duration_selected, pattern="^dur_"),
                CallbackQueryHandler(new_signal, pattern="^new_signal$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv)

    print("🤖 Quotex Signal Bot running with live data...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
      
