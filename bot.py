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

MIN_CONFLUENCE = 9
SELECT_PAIR, SELECT_DURATION = range(2)


# ── Data fetching (stdlib only) ───────────────────────────────────────────────

def fetch_candles_sync(pair: str, interval: str, outputsize: int = 100):
    params = urllib.parse.urlencode({
        "symbol": pair, "interval": interval,
        "outputsize": outputsize, "apikey": TD_API_KEY, "format": "JSON",
    })
    url = f"{TD_BASE}/time_series?{params}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
        if data.get("status") == "error" or "values" not in data:
            return None
        return data["values"]
    except Exception:
        return None


async def fetch_candles(pair: str, interval: str, outputsize: int = 100):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_candles_sync, pair, interval, outputsize)


# ── Indicators ────────────────────────────────────────────────────────────────

def get_closes(c): return [float(x["close"]) for x in c]
def get_highs(c):  return [float(x["high"])  for x in c]
def get_lows(c):   return [float(x["low"])   for x in c]


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
    k12,k26,k9 = 2/13,2/27,2/10
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
    prev = ms[-2] if len(ms)>=2 else ml
    return round(ml,6), round(sig,6), round(ml-sig,6), prev


def calc_bollinger(prices, period=20):
    if len(prices) < period: return None
    w = [float(x) for x in prices[:period]]
    mid = sum(w)/period
    std = (sum((x-mid)**2 for x in w)/period)**0.5
    upper,lower = mid+2*std, mid-2*std
    cur = prices[0]
    pct_b = (cur-lower)/(upper-lower) if upper!=lower else 0.5
    bw = (upper-lower)/mid if mid else 0
    return {"pct_b": round(pct_b,4), "bw": round(bw,6)}


def calc_stochastic(candles, k_period=14, d_period=3):
    if len(candles) < k_period+d_period: return None
    cl,hi,lo = get_closes(candles),get_highs(candles),get_lows(candles)
    kv = []
    for i in range(d_period+1):
        wh,wl = max(hi[i:i+k_period]),min(lo[i:i+k_period])
        kv.append(100*(cl[i]-wl)/(wh-wl) if wh!=wl else 50.0)
    return {"k":round(kv[0],2),"d":round(sum(kv[:d_period])/d_period,2),"prev_k":kv[1]}


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_timeframe(candles):
    score = 0
    details = {}
    cl = get_closes(candles)

    rsi = calc_rsi(cl)
    if rsi is not None:
        if rsi<=20:   score+=3; lbl=f"Extremely oversold ({rsi}) 🔥"
        elif rsi<=30: score+=2; lbl=f"Oversold ({rsi})"
        elif rsi<=42: score+=1; lbl=f"Mild bullish ({rsi})"
        elif rsi>=80: score-=3; lbl=f"Extremely overbought ({rsi}) 🔥"
        elif rsi>=70: score-=2; lbl=f"Overbought ({rsi})"
        elif rsi>=58: score-=1; lbl=f"Mild bearish ({rsi})"
        else:                   lbl=f"Neutral ({rsi})"
        details["rsi"] = {"value":rsi,"label":lbl}

    macd_res = calc_macd(cl)
    if macd_res:
        ml,sl,hist,prev = macd_res
        cup  = ml>sl and prev<=sl
        cdown= ml<sl and prev>=sl
        if ml>sl:
            base=3 if abs(hist)>0.00005 else 1
            score+=base+(1 if cup else 0)
            lbl="Fresh bullish crossover ✅" if cup else ("Strong bullish" if base==3 else "Bullish")
        else:
            base=3 if abs(hist)>0.00005 else 1
            score-=base+(1 if cdown else 0)
            lbl="Fresh bearish crossover ❌" if cdown else ("Strong bearish" if base==3 else "Bearish")
        details["macd"] = {"label":lbl}

    e9,e21,e50 = calc_ema(cl,9),calc_ema(cl,21),calc_ema(cl,50)
    if e9 and e21 and e50:
        if e9>e21>e50:   score+=3; lbl="Bull stack 9>21>50 ✅"
        elif e9>e21:     score+=1; lbl="Bullish (9>21)"
        elif e9<e21<e50: score-=3; lbl="Bear stack 9<21<50 ❌"
        elif e9<e21:     score-=1; lbl="Bearish (9<21)"
        else:                      lbl="Mixed EMAs"
        details["ema"] = {"label":lbl}

    bb = calc_bollinger(cl)
    if bb:
        pb,bw = bb["pct_b"],bb["bw"]
        if bw<0.001:    score-=1; lbl="Squeeze ⚠️"
        elif pb<=0.05:  score+=2; lbl="At lower band 🔥"
        elif pb<=0.2:   score+=1; lbl="Near lower band"
        elif pb>=0.95:  score-=2; lbl="At upper band 🔥"
        elif pb>=0.8:   score-=1; lbl="Near upper band"
        else:                     lbl=f"Mid-band ({round(pb*100,1)}%)"
        details["bb"] = {"label":lbl}

    stoch = calc_stochastic(candles)
    if stoch:
        k,d,pk = stoch["k"],stoch["d"],stoch["prev_k"]
        cu = k>d and pk<=d; cd = k<d and pk>=d
        if k<20 and d<20:   score+=2; lbl=f"Oversold K={k} D={d} 🔥"
        elif k<20:          score+=1; lbl=f"Oversold K={k}"
        elif k>80 and d>80: score-=2; lbl=f"Overbought K={k} D={d} 🔥"
        elif k>80:          score-=1; lbl=f"Overbought K={k}"
        elif cu:            score+=1; lbl=f"Bullish cross K={k}"
        elif cd:            score-=1; lbl=f"Bearish cross K={k}"
        else:                         lbl=f"Neutral K={k} D={d}"
        details["stoch"] = {"label":lbl}

    return {"score":score,"details":details,"error":False}


async def analyse_pair(pair, duration):
    tf_list = TIMEFRAMES[duration]
    tasks = [fetch_candles(pair, iv, 100) for iv,_ in tf_list]
    candle_sets = await asyncio.gather(*tasks)

    tf_results = {}
    total_score = 0
    any_data = False

    for (iv, label), candles in zip(tf_list, candle_sets):
        if not candles or len(candles) < 50:
            tf_results[label] = {"error":True,"score":0,"details":{}}
            continue
        any_data = True
        r = score_timeframe(candles)
        tf_results[label] = r
        total_score += r["score"]

    if not any_data:
        return {"error":True,"message":"Could not fetch market data. Check your API key."}

    scored = [r for r in tf_results.values() if not r.get("error")]
    n = len(scored)
    bulls = sum(1 for r in scored if r["score"]>0)
    bears = sum(1 for r in scored if r["score"]<0)
    all_agree = (bulls==n) or (bears==n)

    strength = min(round(abs(total_score)/(n*14)*100), 95)

    if total_score>=MIN_CONFLUENCE and all_agree:
        direction,emoji,sig = "BUY","🟢","📈"
    elif total_score<=-MIN_CONFLUENCE and all_agree:
        direction,emoji,sig = "SELL","🔴","📉"
    else:
        direction,emoji,sig = "WAIT","🟡","⏳"
        strength = 0

    return {
        "error":False,"direction":direction,"emoji":emoji,"signal_emoji":sig,
        "strength":strength,"total_score":total_score,"all_agree":all_agree,
        "tf_results":tf_results,"tfs":[lbl for _,lbl in tf_list],
    }


# ── Formatter ─────────────────────────────────────────────────────────────────

def format_signal(pair, duration, analysis):
    now = datetime.utcnow().strftime("%H:%M UTC  %d %b %Y")
    lines = [
        "╔══════════════════════════════╗",
        "║   📊  QUOTEX SIGNAL BOT      ║",
        "╚══════════════════════════════╝","",
        f"💱  Pair:       *{pair}*",
        f"⏱   Duration:  *{duration}*",
        f"🕐  Time:       {now}","",
        "━━━━  MULTI-TIMEFRAME ANALYSIS  ━━━━",
    ]
    for tf in analysis["tfs"]:
        r = analysis["tf_results"].get(tf,{})
        if r.get("error"):
            lines += ["", f"📌 *{tf}* — ⚠️ No data"]; continue
        sc = r["score"]; d = r["details"]
        bias = "🟢 Bullish" if sc>0 else ("🔴 Bearish" if sc<0 else "🟡 Neutral")
        lines += ["", f"📌 *{tf}* — {bias}  (score {sc:+d})"]
        for k in ["rsi","macd","ema","bb","stoch"]:
            if k in d: lines.append(f"  • {k.upper():<5} → {d[k]['label']}")

    pct = analysis["strength"]
    bar = "█"*min(int(pct/10),10) + "░"*(10-min(int(pct/10),10))
    lines += ["","━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",""]

    if analysis["direction"]=="WAIT":
        reason = "Timeframes disagree" if not analysis.get("all_agree") else "Weak confluence"
        lines += [f"🟡  *SIGNAL:  WAIT ⏳*","",f"_Reason: {reason}._",
                  "_Wait for a cleaner setup._"]
    else:
        lines += [
            f"{analysis['emoji']}  *SIGNAL:  {analysis['direction']} {analysis['signal_emoji']}*",
            f"📶  Confidence:  *{pct}%*",
            f"📊  Confluence:  `[{bar}]`",
            f"✅  TF Agreement: {'All aligned ✅' if analysis['all_agree'] else 'Partial'}","",
            f"_Enter at open of next {duration} candle._",
        ]
    lines += ["","⚠️ _For educational purposes only. Trade responsibly._"]
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📊 Generate Signal", callback_data="new_signal")]]
    await update.message.reply_text(
        "👋 Welcome to *Quotex Signal Bot*!\n\n"
        "🧠 *Intelligent multi-timeframe analysis:*\n"
        "• RSI • MACD • EMA 9/21/50\n"
        "• Bollinger Bands • Stochastic\n\n"
        "📡 Live data from Twelve Data\n"
        "✅ Signal fires only when all TFs agree\n\n"
        "Tap below to get started 👇",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown",
    )


async def new_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [[InlineKeyboardButton(p, callback_data=f"pair_{p}") for p in row]
          for row in FOREX_PAIRS]
    await q.edit_message_text("💱 *Select a Forex pair:*",
                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SELECT_PAIR


async def pair_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pair = q.data.replace("pair_","")
    ctx.user_data["pair"] = pair
    kb = [[InlineKeyboardButton(d, callback_data=f"dur_{d}")] for d in DURATIONS]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="new_signal")])
    await q.edit_message_text(f"✅ Pair: *{pair}*\n\n⏱ *Select trade duration:*",
                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SELECT_DURATION


async def duration_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    duration = q.data.replace("dur_","")
    pair = ctx.user_data.get("pair","EUR/USD")
    await q.edit_message_text(
        f"📡 Fetching live data for *{pair}*...\n🧠 Analysing 3 timeframes ⏳",
        parse_mode="Markdown")
    analysis = await analyse_pair(pair, duration)
    if analysis.get("error"):
        await q.edit_message_text(f"⚠️ *Error:* {analysis['message']}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Retry", callback_data=f"dur_{duration}"),
                InlineKeyboardButton("🔙 Back",  callback_data="new_signal"),
            ]]))
        return SELECT_DURATION
    await q.edit_message_text(format_signal(pair, duration, analysis),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh",  callback_data=f"dur_{duration}"),
            InlineKeyboardButton("💱 New Pair", callback_data="new_signal"),
        ]]))
    return SELECT_DURATION


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How the bot works:*\n\n"
        "1️⃣ Select pair → 2️⃣ Select duration\n"
        "3️⃣ Bot fetches live candles\n"
        "4️⃣ Calculates 5 indicators × 3 timeframes\n"
        "5️⃣ All TFs must agree → BUY/SELL fires\n"
        "6️⃣ Otherwise → WAIT\n\n"
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
    print("🤖 Quotex Signal Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
                                                           
