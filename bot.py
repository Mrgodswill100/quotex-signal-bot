import os
import asyncio
import json
import urllib.request
import urllib.parse
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler
)

TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TD_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TD_BASE    = "https://api.twelvedata.com"

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

TIMEFRAMES = {
    "1 min":  [("1min","M1"),  ("5min","M5"),  ("15min","M15")],
    "5 min":  [("5min","M5"),  ("15min","M15"),("1h","H1")],
    "15 min": [("15min","M15"),("1h","H1"),    ("4h","H4")],
    "30 min": [("30min","M30"),("1h","H1"),    ("4h","H4")],
    "1 hour": [("1h","H1"),    ("4h","H4"),    ("1day","D1")],
}

# Lowered for more signals (was 9)
MIN_CONFLUENCE = 6
# Allow 2 out of 3 TFs to agree (was all 3)
MIN_TF_AGREE_RATIO = 0.67

SELECT_PAIR, SELECT_DURATION = range(2)


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_candles_sync(pair, interval, outputsize=100):
    params = urllib.parse.urlencode({
        "symbol": pair, "interval": interval,
        "outputsize": outputsize, "apikey": TD_API_KEY, "format": "JSON",
    })
    try:
        with urllib.request.urlopen(f"{TD_BASE}/time_series?{params}", timeout=20) as r:
            data = json.loads(r.read().decode())
        if data.get("status") == "error" or "values" not in data:
            return None
        return data["values"]
    except Exception:
        return None


async def fetch_candles(pair, interval, outputsize=100):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_candles_sync, pair, interval, outputsize)


# ── Indicator calculations ────────────────────────────────────────────────────

def get_closes(c): return [float(x["close"]) for x in c]
def get_highs(c):  return [float(x["high"])  for x in c]
def get_lows(c):   return [float(x["low"])   for x in c]
def get_volumes(c):
    try:    return [float(x.get("volume", 0)) for x in c]
    except: return [0.0] * len(c)


def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    p = list(reversed(prices))
    gains = [max(p[i]-p[i-1], 0) for i in range(1, period+1)]
    losses= [max(p[i-1]-p[i], 0) for i in range(1, period+1)]
    ag, al = sum(gains)/period, sum(losses)/period
    for i in range(period+1, len(p)):
        d = p[i]-p[i-1]
        ag = (ag*(period-1)+max(d,0))/period
        al = (al*(period-1)+max(-d,0))/period
    return round(100-(100/(1+ag/al)), 2) if al else 100.0


def calc_ema(prices, period):
    if len(prices) < period: return None
    p = list(reversed(prices))
    k = 2/(period+1)
    ema = sum(p[:period])/period
    for x in p[period:]: ema = x*k + ema*(1-k)
    return round(ema, 6)


def calc_macd(prices):
    if len(prices) < 35: return None
    p = list(reversed(prices))
    k12, k26, k9 = 2/13, 2/27, 2/10
    e12 = sum(p[:12])/12
    e26 = sum(p[:26])/26
    ms = []
    for i in range(26, len(p)):
        e12 = p[i]*k12+e12*(1-k12)
        e26 = p[i]*k26+e26*(1-k26)
        ms.append(e12-e26)
    if len(ms) < 9: return None
    sig = sum(ms[:9])/9
    for v in ms[9:]: sig = v*k9+sig*(1-k9)
    ml = ms[-1]
    prev = ms[-2] if len(ms) >= 2 else ml
    return round(ml,6), round(sig,6), round(ml-sig,6), prev


def calc_bollinger(prices, period=20):
    if len(prices) < period: return None
    w = [float(x) for x in prices[:period]]
    mid = sum(w)/period
    std = (sum((x-mid)**2 for x in w)/period)**0.5
    upper, lower = mid+2*std, mid-2*std
    cur = prices[0]
    pct_b = (cur-lower)/(upper-lower) if upper != lower else 0.5
    bw = (upper-lower)/mid if mid else 0
    return {"pct_b": round(pct_b,4), "bw": round(bw,6), "upper": upper, "lower": lower, "mid": mid}


def calc_stochastic(candles, k_period=14, d_period=3):
    if len(candles) < k_period+d_period: return None
    cl, hi, lo = get_closes(candles), get_highs(candles), get_lows(candles)
    kv = []
    for i in range(d_period+1):
        wh, wl = max(hi[i:i+k_period]), min(lo[i:i+k_period])
        kv.append(100*(cl[i]-wl)/(wh-wl) if wh != wl else 50.0)
    return {"k": round(kv[0],2), "d": round(sum(kv[:d_period])/d_period,2), "prev_k": kv[1]}


def calc_atr(candles, period=14):
    """Average True Range — measures volatility."""
    if len(candles) < period+1: return None
    cl, hi, lo = get_closes(candles), get_highs(candles), get_lows(candles)
    trs = []
    for i in range(period):
        tr = max(hi[i]-lo[i], abs(hi[i]-cl[i+1]), abs(lo[i]-cl[i+1]))
        trs.append(tr)
    atr = sum(trs)/period
    # Express as % of price for cross-pair comparison
    atr_pct = (atr / cl[0]) * 100 if cl[0] else 0
    return {"atr": round(atr, 6), "atr_pct": round(atr_pct, 4)}


def calc_adx(candles, period=14):
    """ADX — measures trend strength (not direction). >25 = strong trend."""
    if len(candles) < period*2: return None
    hi, lo, cl = get_highs(candles), get_lows(candles), get_closes(candles)
    # Reverse to oldest-first
    hi = list(reversed(hi)); lo = list(reversed(lo)); cl = list(reversed(cl))

    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(hi)):
        h_diff = hi[i] - hi[i-1]
        l_diff = lo[i-1] - lo[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
        tr_list.append(tr)

    def wilder_smooth(data, p):
        s = sum(data[:p])
        result = [s]
        for v in data[p:]:
            s = s - s/p + v
            result.append(s)
        return result

    atr_s   = wilder_smooth(tr_list, period)
    pdm_s   = wilder_smooth(plus_dm, period)
    mdm_s   = wilder_smooth(minus_dm, period)

    di_plus  = [100*p/a if a else 0 for p,a in zip(pdm_s, atr_s)]
    di_minus = [100*m/a if a else 0 for m,a in zip(mdm_s, atr_s)]
    dx = [100*abs(p-m)/(p+m) if (p+m) else 0 for p,m in zip(di_plus, di_minus)]

    if len(dx) < period: return None
    adx = sum(dx[:period])/period
    for v in dx[period:]:
        adx = (adx*(period-1)+v)/period

    return {
        "adx": round(adx, 2),
        "di_plus": round(di_plus[-1], 2),
        "di_minus": round(di_minus[-1], 2),
    }


def calc_vwap(candles):
    """VWAP — volume weighted average price (intraday fair value)."""
    vols = get_volumes(candles)
    cl   = get_closes(candles)
    hi   = get_highs(candles)
    lo   = get_lows(candles)

    total_pv = 0; total_v = 0
    for i in range(len(candles)):
        typical = (hi[i]+lo[i]+cl[i])/3
        v = vols[i] if vols[i] > 0 else 1
        total_pv += typical * v
        total_v  += v

    vwap = total_pv / total_v if total_v else cl[0]
    cur  = cl[0]
    diff_pct = ((cur - vwap) / vwap * 100) if vwap else 0
    return {"vwap": round(vwap, 6), "cur": round(cur, 6), "diff_pct": round(diff_pct, 4)}


def calc_support_resistance(candles, lookback=50):
    """
    Finds key support/resistance levels using recent swing highs/lows.
    Returns nearest support below and resistance above current price.
    """
    if len(candles) < 10: return None
    hi = get_highs(candles[:lookback])
    lo = get_lows(candles[:lookback])
    cl = get_closes(candles)
    cur = cl[0]

    # Find swing highs (local maxima) and lows (local minima)
    swing_highs, swing_lows = [], []
    for i in range(2, len(hi)-2):
        if hi[i] > hi[i-1] and hi[i] > hi[i-2] and hi[i] > hi[i+1] and hi[i] > hi[i+2]:
            swing_highs.append(hi[i])
        if lo[i] < lo[i-1] and lo[i] < lo[i-2] and lo[i] < lo[i+1] and lo[i] < lo[i+2]:
            swing_lows.append(lo[i])

    # Nearest resistance above price
    resistances = [h for h in swing_highs if h > cur]
    resistance  = min(resistances) if resistances else max(hi)

    # Nearest support below price
    supports = [l for l in swing_lows if l < cur]
    support  = max(supports) if supports else min(lo)

    dist_to_res = ((resistance - cur) / cur * 100) if cur else 0
    dist_to_sup = ((cur - support) / cur * 100) if cur else 0

    return {
        "support":     round(support, 6),
        "resistance":  round(resistance, 6),
        "dist_to_res": round(dist_to_res, 4),
        "dist_to_sup": round(dist_to_sup, 4),
        "cur":         round(cur, 6),
    }


# ── Scoring engine ────────────────────────────────────────────────────────────
# Max scores per indicator:
#   RSI:        ±3
#   MACD:       ±4 (with crossover bonus)
#   EMA stack:  ±3
#   BB:         ±2
#   Stoch:      ±2
#   ATR:        ±1 (filter only)
#   ADX:        ±2 (trend strength bonus)
#   VWAP:       ±2
#   S/R:        ±2
# Total max per TF: ~±21

def score_timeframe(candles):
    score = 0
    details = {}
    cl = get_closes(candles)

    # ── RSI ──────────────────────────────────────────────────────────────────
    rsi = calc_rsi(cl)
    if rsi is not None:
        if rsi <= 20:    score += 3; lbl = f"Extremely oversold ({rsi}) 🔥"
        elif rsi <= 30:  score += 2; lbl = f"Oversold ({rsi})"
        elif rsi <= 44:  score += 1; lbl = f"Mild bullish ({rsi})"
        elif rsi >= 80:  score -= 3; lbl = f"Extremely overbought ({rsi}) 🔥"
        elif rsi >= 70:  score -= 2; lbl = f"Overbought ({rsi})"
        elif rsi >= 56:  score -= 1; lbl = f"Mild bearish ({rsi})"
        else:                        lbl = f"Neutral ({rsi})"
        details["rsi"] = {"value": rsi, "label": lbl}

    # ── MACD ─────────────────────────────────────────────────────────────────
    macd_res = calc_macd(cl)
    if macd_res:
        ml, sl, hist, prev = macd_res
        cup   = ml > sl and prev <= sl
        cdown = ml < sl and prev >= sl
        if ml > sl:
            base = 3 if abs(hist) > 0.00005 else 1
            score += base + (1 if cup else 0)
            lbl = "Fresh bullish crossover ✅" if cup else ("Strong bullish" if base == 3 else "Bullish")
        else:
            base = 3 if abs(hist) > 0.00005 else 1
            score -= base + (1 if cdown else 0)
            lbl = "Fresh bearish crossover ❌" if cdown else ("Strong bearish" if base == 3 else "Bearish")
        details["macd"] = {"label": lbl}

    # ── EMA Triple Stack ─────────────────────────────────────────────────────
    e9, e21, e50 = calc_ema(cl, 9), calc_ema(cl, 21), calc_ema(cl, 50)
    if e9 and e21 and e50:
        if e9 > e21 > e50:    score += 3; lbl = "Bull stack 9>21>50 ✅"
        elif e9 > e21:        score += 1; lbl = "Bullish (9>21)"
        elif e9 < e21 < e50:  score -= 3; lbl = "Bear stack 9<21<50 ❌"
        elif e9 < e21:        score -= 1; lbl = "Bearish (9<21)"
        else:                             lbl = "Mixed EMAs"
        details["ema"] = {"label": lbl}

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb = calc_bollinger(cl)
    if bb:
        pb, bw = bb["pct_b"], bb["bw"]
        if bw < 0.001:   score -= 1; lbl = "Squeeze — avoid ⚠️"
        elif pb <= 0.05: score += 2; lbl = "At lower band — buy zone 🔥"
        elif pb <= 0.2:  score += 1; lbl = "Near lower band"
        elif pb >= 0.95: score -= 2; lbl = "At upper band — sell zone 🔥"
        elif pb >= 0.8:  score -= 1; lbl = "Near upper band"
        else:                        lbl = f"Mid-band ({round(pb*100,1)}%)"
        details["bb"] = {"label": lbl}

    # ── Stochastic ────────────────────────────────────────────────────────────
    stoch = calc_stochastic(candles)
    if stoch:
        k, d, pk = stoch["k"], stoch["d"], stoch["prev_k"]
        cu = k > d and pk <= d
        cd = k < d and pk >= d
        if k < 20 and d < 20:    score += 2; lbl = f"Oversold K={k} D={d} 🔥"
        elif k < 20:             score += 1; lbl = f"Oversold zone K={k}"
        elif k > 80 and d > 80:  score -= 2; lbl = f"Overbought K={k} D={d} 🔥"
        elif k > 80:             score -= 1; lbl = f"Overbought K={k}"
        elif cu:                 score += 1; lbl = f"Bullish cross K={k}"
        elif cd:                 score -= 1; lbl = f"Bearish cross K={k}"
        else:                                lbl = f"Neutral K={k} D={d}"
        details["stoch"] = {"label": lbl}

    # ── ATR — Volatility Filter ───────────────────────────────────────────────
    atr_res = calc_atr(candles)
    if atr_res:
        ap = atr_res["atr_pct"]
        if ap < 0.02:    score -= 1; lbl = f"Very low volatility ({ap}%) ⚠️"
        elif ap > 0.3:   score -= 1; lbl = f"Very high volatility ({ap}%) ⚠️"
        else:            score += 1; lbl = f"Good volatility ({ap}%) ✅"
        details["atr"] = {"label": lbl}

    # ── ADX — Trend Strength ──────────────────────────────────────────────────
    adx_res = calc_adx(candles)
    if adx_res:
        adx = adx_res["adx"]
        dip = adx_res["di_plus"]
        dim = adx_res["di_minus"]
        if adx >= 30 and dip > dim:   score += 2; lbl = f"Strong uptrend ADX={adx} 💪"
        elif adx >= 25 and dip > dim: score += 1; lbl = f"Uptrend ADX={adx}"
        elif adx >= 30 and dim > dip: score -= 2; lbl = f"Strong downtrend ADX={adx} 💪"
        elif adx >= 25 and dim > dip: score -= 1; lbl = f"Downtrend ADX={adx}"
        elif adx < 20:                            lbl = f"Weak trend ADX={adx} — ranging ⚠️"
        else:                                     lbl = f"Moderate trend ADX={adx}"
        details["adx"] = {"label": lbl}

    # ── VWAP — Fair Value ─────────────────────────────────────────────────────
    vwap_res = calc_vwap(candles)
    if vwap_res:
        dp = vwap_res["diff_pct"]
        if dp <= -0.1:   score += 2; lbl = f"Price below VWAP — buy zone ✅"
        elif dp <= -0.03:score += 1; lbl = f"Slightly below VWAP"
        elif dp >= 0.1:  score -= 2; lbl = f"Price above VWAP — sell zone ✅"
        elif dp >= 0.03: score -= 1; lbl = f"Slightly above VWAP"
        else:                        lbl = f"At VWAP — fair value"
        details["vwap"] = {"label": lbl}

    # ── Support & Resistance ──────────────────────────────────────────────────
    sr = calc_support_resistance(candles)
    if sr:
        dtr = sr["dist_to_res"]
        dts = sr["dist_to_sup"]
        if dts < 0.05:   score += 2; lbl = f"At support {sr['support']} 🔥"
        elif dts < 0.15: score += 1; lbl = f"Near support {sr['support']}"
        elif dtr < 0.05: score -= 2; lbl = f"At resistance {sr['resistance']} 🔥"
        elif dtr < 0.15: score -= 1; lbl = f"Near resistance {sr['resistance']}"
        else:                        lbl = f"S: {sr['support']} | R: {sr['resistance']}"
        details["sr"] = {"label": lbl}

    return {"score": score, "details": details, "error": False}


async def analyse_pair(pair, duration):
    tf_list = TIMEFRAMES[duration]
    tasks = [fetch_candles(pair, iv, 120) for iv, _ in tf_list]
    candle_sets = await asyncio.gather(*tasks)

    tf_results = {}
    total_score = 0
    any_data = False

    for (iv, label), candles in zip(tf_list, candle_sets):
        if not candles or len(candles) < 50:
            tf_results[label] = {"error": True, "score": 0, "details": {}}
            continue
        any_data = True
        r = score_timeframe(candles)
        tf_results[label] = r
        total_score += r["score"]

    if not any_data:
        return {"error": True, "message": "Could not fetch market data. Check your API key."}

    scored = [r for r in tf_results.values() if not r.get("error")]
    n = len(scored)
    bulls = sum(1 for r in scored if r["score"] > 0)
    bears = sum(1 for r in scored if r["score"] < 0)

    # Looser: 2 out of 3 TFs agreeing is enough
    bull_ratio = bulls / n if n else 0
    bear_ratio = bears / n if n else 0
    bull_agree = bull_ratio >= MIN_TF_AGREE_RATIO
    bear_agree = bear_ratio >= MIN_TF_AGREE_RATIO
    all_agree  = bulls == n or bears == n

    strength = min(round(abs(total_score) / (n * 21) * 100), 95)

    if total_score >= MIN_CONFLUENCE and bull_agree:
        direction, emoji, sig = "BUY",  "🟢", "📈"
    elif total_score <= -MIN_CONFLUENCE and bear_agree:
        direction, emoji, sig = "SELL", "🔴", "📉"
    else:
        direction, emoji, sig = "WAIT", "🟡", "⏳"
        strength = 0

    return {
        "error": False, "direction": direction, "emoji": emoji, "signal_emoji": sig,
        "strength": strength, "total_score": total_score,
        "all_agree": all_agree, "bull_ratio": bull_ratio, "bear_ratio": bear_ratio,
        "tf_results": tf_results, "tfs": [lbl for _, lbl in tf_list],
    }


# ── Formatter ─────────────────────────────────────────────────────────────────

INDICATOR_LABELS = {
    "rsi": "RSI  ", "macd": "MACD ", "ema": "EMA  ",
    "bb": "BB   ", "stoch": "STOCH", "atr": "ATR  ",
    "adx": "ADX  ", "vwap": "VWAP ", "sr": "S/R  ",
}

def format_signal(pair, duration, analysis):
    now = datetime.utcnow().strftime("%H:%M UTC  %d %b %Y")
    lines = [
        "╔══════════════════════════════╗",
        "║   📊  QUOTEX SIGNAL BOT      ║",
        "╚══════════════════════════════╝", "",
        f"💱  Pair:       *{pair}*",
        f"⏱   Duration:  *{duration}*",
        f"🕐  Time:       {now}", "",
        "━━━━  MULTI-TIMEFRAME ANALYSIS  ━━━━",
    ]

    for tf in analysis["tfs"]:
        r = analysis["tf_results"].get(tf, {})
        if r.get("error"):
            lines += ["", f"📌 *{tf}* — ⚠️ No data"]; continue
        sc = r["score"]; d = r["details"]
        bias = "🟢 Bullish" if sc > 0 else ("🔴 Bearish" if sc < 0 else "🟡 Neutral")
        lines += ["", f"📌 *{tf}* — {bias}  (score {sc:+d})"]
        for k, lbl in INDICATOR_LABELS.items():
            if k in d:
                lines.append(f"  • {lbl} → {d[k]['label']}")

    pct  = analysis["strength"]
    fill = min(int(pct/10), 10)
    bar  = "█" * fill + "░" * (10-fill)

    # TF agreement display
    br = round(analysis.get("bull_ratio", 0)*100)
    er = round(analysis.get("bear_ratio", 0)*100)
    agree_str = f"{br}% bull" if analysis["direction"] == "BUY" else (
                f"{er}% bear" if analysis["direction"] == "SELL" else "Mixed")

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]

    if analysis["direction"] == "WAIT":
        lines += [
            "🟡  *SIGNAL:  WAIT ⏳*", "",
            "_Confluence not strong enough._",
            "_Wait for a cleaner setup._",
        ]
    else:
        lines += [
            f"{analysis['emoji']}  *SIGNAL:  {analysis['direction']} {analysis['signal_emoji']}*",
            f"📶  Confidence:    *{pct}%*",
            f"📊  Confluence:    `[{bar}]`",
            f"🎯  TF Agreement:  {agree_str}",
            f"{'✅  All TFs aligned!' if analysis['all_agree'] else '⚡  Majority aligned'}",
            "", f"_Enter at open of next {duration} candle._",
        ]

    lines += ["", "⚠️ _For educational purposes only. Trade responsibly._"]
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📊 Generate Signal", callback_data="new_signal")]]
    await update.message.reply_text(
        "👋 Welcome to *Quotex Signal Bot*!\n\n"
        "🧠 *9 indicators across 3 timeframes:*\n"
        "• RSI  • MACD  • EMA 9/21/50\n"
        "• Bollinger Bands  • Stochastic\n"
        "• ATR  • ADX  • VWAP  • Support/Resistance\n\n"
        "📡 Live data — real market prices\n"
        "⚡ Signals fire when majority of TFs agree\n\n"
        "Tap below to get started 👇",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def new_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [[InlineKeyboardButton(p, callback_data=f"pair_{p}") for p in row]
          for row in FOREX_PAIRS]
    await q.edit_message_text(
        "💱 *Select a Forex pair:*",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SELECT_PAIR


async def pair_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pair = q.data.replace("pair_", "")
    ctx.user_data["pair"] = pair
    kb = [[InlineKeyboardButton(d, callback_data=f"dur_{d}")] for d in DURATIONS]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="new_signal")])
    await q.edit_message_text(
        f"✅ Pair: *{pair}*\n\n⏱ *Select trade duration:*",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SELECT_DURATION


async def duration_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    duration = q.data.replace("dur_", "")
    pair = ctx.user_data.get("pair", "EUR/USD")
    await q.edit_message_text(
        f"📡 Fetching live data for *{pair}*...\n"
        f"🧠 Running 9 indicators × 3 timeframes ⏳",
        parse_mode="Markdown")
    analysis = await analyse_pair(pair, duration)
    if analysis.get("error"):
        await q.edit_message_text(
            f"⚠️ *Error:* {analysis['message']}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Retry", callback_data=f"dur_{duration}"),
                InlineKeyboardButton("🔙 Back",  callback_data="new_signal"),
            ]]))
        return SELECT_DURATION
    await q.edit_message_text(
        format_signal(pair, duration, analysis),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh",  callback_data=f"dur_{duration}"),
            InlineKeyboardButton("💱 New Pair", callback_data="new_signal"),
        ]]))
    return SELECT_DURATION


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Quotex Signal Bot — 9 Indicator Engine*\n\n"
        "• RSI (14) — overbought/oversold\n"
        "• MACD (12,26,9) — momentum & crossovers\n"
        "• EMA 9/21/50 — triple stack trend\n"
        "• Bollinger Bands — volatility & squeeze\n"
        "• Stochastic (14,3) — entry timing\n"
        "• ATR — volatility filter\n"
        "• ADX — trend strength\n"
        "• VWAP — fair value level\n"
        "• Support & Resistance — key levels\n\n"
        "Signal fires when *majority of TFs agree*.\n\n"
        "⚠️ _For educational purposes only._",
        parse_mode="Markdown",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN: raise ValueError("TELEGRAM_BOT_TOKEN not set")
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
    print("🤖 Quotex Signal Bot (9 indicators) running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
