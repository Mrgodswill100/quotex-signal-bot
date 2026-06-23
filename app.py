import os, sys, asyncio, json, threading, urllib.request, urllib.parse
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler

TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TD_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TD_BASE    = "https://api.twelvedata.com"

FOREX_PAIRS = [
    ["EUR/USD","GBP/USD","USD/JPY","USD/CHF"],
    ["AUD/USD","USD/CAD","NZD/USD","EUR/GBP"],
    ["EUR/JPY","GBP/JPY","EUR/AUD","GBP/AUD"],
    ["AUD/JPY","CAD/JPY","CHF/JPY","NZD/JPY"],
    ["EUR/CAD","EUR/CHF","GBP/CAD","GBP/CHF"],
    ["AUD/CAD","AUD/CHF","AUD/NZD","NZD/CAD"],
    ["USD/SGD","USD/MXN","USD/ZAR","USD/NOK"],
]

DURATIONS = ["15 sec","30 sec","1 min","5 min","15 min","30 min"]

# entry TF, confirmation TF, trend TF
TIMEFRAMES = {
    "15 sec":  [("1min","M1"), ("5min","M5"),  ("15min","M15")],
    "30 sec":  [("1min","M1"), ("5min","M5"),  ("15min","M15")],
    "1 min":   [("1min","M1"), ("5min","M5"),  ("15min","M15")],
    "5 min":   [("5min","M5"),  ("15min","M15"),("30min","M30")],
    "15 min":  [("15min","M15"),("30min","M30"),("1h","H1")],
    "30 min":  [("30min","M30"),("1h","H1"),   ("4h","H4")],
}

DURATION_MINUTES = {
    "15 sec": 0.25, "30 sec": 0.5,
    "1 min": 1, "5 min": 5, "15 min": 15, "30 min": 30
}

SELECT_PAIR, SELECT_DURATION = range(2)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_expiry(duration):
    mins = DURATION_MINUTES.get(duration, 1)
    expiry = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(minutes=mins + 1)
    if mins < 1:
        secs = int(mins * 60)
        expiry = datetime.utcnow() + timedelta(seconds=secs + 5)
        return expiry.strftime("%H:%M:%S UTC")
    return expiry.strftime("%H:%M UTC")

def fetch_sync(pair, interval, size=150):
    p = urllib.parse.urlencode({
        "symbol": pair, "interval": interval,
        "outputsize": size, "apikey": TD_API_KEY, "format": "JSON"
    })
    try:
        with urllib.request.urlopen(f"{TD_BASE}/time_series?{p}", timeout=20) as r:
            d = json.loads(r.read().decode())
        return d.get("values") if "values" in d else None
    except:
        return None

async def fetch(pair, interval, size=150):
    return await asyncio.get_event_loop().run_in_executor(None, fetch_sync, pair, interval, size)

def C(c): return [float(x["close"]) for x in c]
def H(c): return [float(x["high"])  for x in c]
def L(c): return [float(x["low"])   for x in c]

# ── Indicators ────────────────────────────────────────────────────────────────

def calc_rsi(cl, p=14):
    if len(cl) < p+1: return None
    pr = list(reversed(cl))
    g = [max(pr[i]-pr[i-1],0) for i in range(1,p+1)]
    l = [max(pr[i-1]-pr[i],0) for i in range(1,p+1)]
    ag,al = sum(g)/p, sum(l)/p
    for i in range(p+1, len(pr)):
        d = pr[i]-pr[i-1]
        ag = (ag*(p-1)+max(d,0))/p
        al = (al*(p-1)+max(-d,0))/p
    return round(100-(100/(1+ag/al)), 2) if al else 100.0

def calc_ema(cl, p):
    if len(cl) < p: return None
    pr = list(reversed(cl))
    k = 2/(p+1); e = sum(pr[:p])/p
    for x in pr[p:]: e = x*k + e*(1-k)
    return round(e, 6)

def calc_macd(cl):
    if len(cl) < 35: return None
    pr = list(reversed(cl))
    k12,k26,k9 = 2/13,2/27,2/10
    e12 = sum(pr[:12])/12; e26 = sum(pr[:26])/26; ms = []
    for i in range(26,len(pr)):
        e12=pr[i]*k12+e12*(1-k12); e26=pr[i]*k26+e26*(1-k26); ms.append(e12-e26)
    if len(ms)<9: return None
    sig = sum(ms[:9])/9
    for v in ms[9:]: sig = v*k9+sig*(1-k9)
    ml = ms[-1]; prev = ms[-2] if len(ms)>=2 else ml
    return ml, sig, ml-sig, prev

def calc_stoch(c, kp=14, dp=3):
    cl,hi,lo = C(c),H(c),L(c)
    if len(c) < kp+dp: return None
    kv = []
    for i in range(dp+1):
        wh,wl = max(hi[i:i+kp]),min(lo[i:i+kp])
        kv.append(100*(cl[i]-wl)/(wh-wl) if wh!=wl else 50.0)
    return kv[0], sum(kv[:dp])/dp, kv[1]

def calc_bb(cl, p=20):
    if len(cl)<p: return None
    w = cl[:p]; mid = sum(w)/p
    std = (sum((x-mid)**2 for x in w)/p)**0.5
    up,lo = mid+2*std, mid-2*std
    pb = (cl[0]-lo)/(up-lo) if up!=lo else 0.5
    bw = (up-lo)/mid if mid else 0
    return round(pb,4), round(bw,6)

def calc_atr(c, p=14):
    cl,hi,lo = C(c),H(c),L(c)
    if len(c)<p+1: return None
    trs = [max(hi[i]-lo[i],abs(hi[i]-cl[i+1]),abs(lo[i]-cl[i+1])) for i in range(p)]
    return sum(trs)/p

# ── Core signal logic ─────────────────────────────────────────────────────────
#
# GATEKEEPER RULES (all must pass or signal = WAIT):
#   1. Trend TF (3rd TF) EMA 9/21 must define clear direction
#   2. MACD on entry TF must agree with trend
#   3. RSI must not be against direction (no BUY when RSI>65, no SELL when RSI<35)
#
# SCORING: once gatekeepers pass, count confirming indicators
#   Need 4+ out of 7 to fire signal

def analyse_tf(c):
    cl = C(c)
    result = {}

    # EMA trend
    e9  = calc_ema(cl, 9)
    e21 = calc_ema(cl, 21)
    e50 = calc_ema(cl, 50)
    if e9 and e21 and e50:
        if e9 > e21 > e50:   result["ema"] = ("bull", 2)
        elif e9 > e21:       result["ema"] = ("bull", 1)
        elif e9 < e21 < e50: result["ema"] = ("bear", 2)
        elif e9 < e21:       result["ema"] = ("bear", 1)
        else:                result["ema"] = ("neutral", 0)
    result["ema_raw"] = (e9, e21, e50)

    # MACD
    m = calc_macd(cl)
    if m:
        ml,sl,hist,prev = m
        cup  = ml>sl and prev<=sl
        cdown= ml<sl and prev>=sl
        if ml > sl:
            strength = 2 if abs(hist)>0.00003 else 1
            result["macd"] = ("bull", strength + (1 if cup else 0), "FreshX✅" if cup else ("Strong" if strength==2 else "Bull"))
        else:
            strength = 2 if abs(hist)>0.00003 else 1
            result["macd"] = ("bear", strength + (1 if cdown else 0), "FreshX❌" if cdown else ("Strong" if strength==2 else "Bear"))

    # RSI
    r = calc_rsi(cl)
    if r is not None:
        if r <= 25:    result["rsi"] = ("bull", 2, f"Oversold({r})🔥")
        elif r <= 44:  result["rsi"] = ("bull", 1, f"MildBull({r})")
        elif r >= 75:  result["rsi"] = ("bear", 2, f"Overbought({r})🔥")
        elif r >= 56:  result["rsi"] = ("bear", 1, f"MildBear({r})")
        else:          result["rsi"] = ("neutral", 0, f"Neutral({r})")
        result["rsi_val"] = r

    # Stochastic
    st = calc_stoch(c)
    if st:
        k,d,pk = st
        cu = k>d and pk<=d; cd = k<d and pk>=d
        if k<20 and d<20:    result["stoch"] = ("bull", 2, f"Oversold🔥")
        elif k<25:           result["stoch"] = ("bull", 1, f"OvSold({round(k,1)})")
        elif k>80 and d>80:  result["stoch"] = ("bear", 2, f"Overbought🔥")
        elif k>75:           result["stoch"] = ("bear", 1, f"OvBought({round(k,1)})")
        elif cu:             result["stoch"] = ("bull", 1, f"BullX")
        elif cd:             result["stoch"] = ("bear", 1, f"BearX")
        else:                result["stoch"] = ("neutral", 0, f"Neutral")

    # Bollinger Bands
    bb = calc_bb(cl)
    if bb:
        pb,bw = bb
        if bw < 0.0008:      result["bb"] = ("neutral", 0, "Squeeze⚠️")
        elif pb <= 0.05:     result["bb"] = ("bull", 2, "LowerBand🔥")
        elif pb <= 0.25:     result["bb"] = ("bull", 1, "NearLower")
        elif pb >= 0.95:     result["bb"] = ("bear", 2, "UpperBand🔥")
        elif pb >= 0.75:     result["bb"] = ("bear", 1, "NearUpper")
        else:                result["bb"] = ("neutral", 0, f"MidBand({round(pb*100)}%)")

    # ATR — volatility context
    at = calc_atr(c)
    cl0 = cl[0] if cl else 1
    if at:
        atr_pct = (at/cl0)*100
        if atr_pct < 0.003:  result["atr"] = ("neutral", -1, "TooQuiet⚠️")
        elif atr_pct > 0.4:  result["atr"] = ("neutral", -1, "TooWild⚠️")
        else:                result["atr"] = ("neutral", 1,  "GoodVol✅")

    return result


async def run_analysis(pair, duration):
    tfl = TIMEFRAMES[duration]
    sets = await asyncio.gather(*[fetch(pair, iv, 150) for iv,_ in tfl])

    tf_data = {}
    for (iv,lbl), c in zip(tfl, sets):
        if not c or len(c) < 50:
            tf_data[lbl] = None
            continue
        tf_data[lbl] = analyse_tf(c)

    tfs = [lbl for _,lbl in tfl]
    entry_lbl  = tfs[0]   # entry timeframe
    confirm_lbl= tfs[1]   # confirmation timeframe
    trend_lbl  = tfs[2]   # trend timeframe

    entry   = tf_data.get(entry_lbl)
    confirm = tf_data.get(confirm_lbl)
    trend   = tf_data.get(trend_lbl)

    if not entry or not trend:
        return {"error": True, "message": "Could not fetch market data. Try again."}

    # ── GATEKEEPER 1: Trend EMA ───────────────────────────────────────────────
    trend_ema = trend.get("ema", ("neutral", 0))
    trend_dir = trend_ema[0]  # "bull", "bear", or "neutral"
    if trend_dir == "neutral":
        return _wait_result(tfs, tf_data, "Trend unclear — EMA mixed on trend TF")

    # ── GATEKEEPER 2: Entry MACD must agree with trend ────────────────────────
    entry_macd = entry.get("macd")
    if not entry_macd or entry_macd[0] != trend_dir:
        return _wait_result(tfs, tf_data, f"MACD disagrees with {trend_dir.upper()} trend")

    # ── GATEKEEPER 3: RSI must not be extreme against direction ───────────────
    rsi_val = entry.get("rsi_val")
    if rsi_val is not None:
        if trend_dir == "bull" and rsi_val > 68:
            return _wait_result(tfs, tf_data, f"RSI({rsi_val}) overbought — avoid BUY")
        if trend_dir == "bear" and rsi_val < 32:
            return _wait_result(tfs, tf_data, f"RSI({rsi_val}) oversold — avoid SELL")

    # ── SCORING: count confirming indicators ──────────────────────────────────
    bull_votes = 0; bear_votes = 0; details = {}

    for lbl, tfd in tf_data.items():
        if not tfd: continue
        for key in ["ema","macd","rsi","stoch","bb"]:
            ind = tfd.get(key)
            if not ind: continue
            direction, weight = ind[0], ind[1]
            tag = f"{lbl}_{key}"
            label = ind[2] if len(ind)>2 else direction
            details[tag] = (direction, label)
            if direction == "bull":   bull_votes += weight
            elif direction == "bear": bear_votes += weight

    # ATR bonus/penalty on entry TF
    atr_ind = entry.get("atr")
    if atr_ind: 
        bull_votes += atr_ind[1] if trend_dir=="bull" else 0
        bear_votes += atr_ind[1] if trend_dir=="bear" else 0

    total = bull_votes + bear_votes
    pct = min(round((max(bull_votes,bear_votes)/max(total,1))*100), 95)

    # Need 4+ weighted votes in trend direction
    if trend_dir == "bull" and bull_votes >= 4:
        return _signal_result("BUY","🟢","📈", bull_votes, bear_votes, pct, tfs, tf_data, details, duration)
    elif trend_dir == "bear" and bear_votes >= 4:
        return _signal_result("SELL","🔴","📉", bull_votes, bear_votes, pct, tfs, tf_data, details, duration)
    else:
        reason = f"Only {bull_votes if trend_dir=='bull' else bear_votes} votes — need 4+"
        return _wait_result(tfs, tf_data, reason)


def _signal_result(direction, emoji, sig, bv, bev, pct, tfs, tf_data, details, duration):
    return {
        "error":False,"direction":direction,"emoji":emoji,"sig":sig,
        "bull_votes":bv,"bear_votes":bev,"pct":pct,
        "tfs":tfs,"tf_data":tf_data,"details":details,"duration":duration,"wait":False
    }

def _wait_result(tfs, tf_data, reason):
    return {
        "error":False,"direction":"WAIT","emoji":"🟡","sig":"⏳",
        "tfs":tfs,"tf_data":tf_data,"reason":reason,"wait":True,
        "bull_votes":0,"bear_votes":0,"pct":0
    }


# ── Formatter ─────────────────────────────────────────────────────────────────

def ind_icon(direction):
    if direction=="bull": return "🟢"
    if direction=="bear": return "🔴"
    return "🟡"

def format_msg(pair, duration, a):
    now = datetime.utcnow().strftime("%H:%M  %d %b %Y")
    tfs = a["tfs"]
    lines = [
        "╔══════════════════════╗",
        "║  📊 CHIMA DTRADER AI ║",
        "╚══════════════════════╝",
        f"💱 *{pair}*  ⏱ *{duration}*",
        f"🕐 {now}","",
        "─── TIMEFRAME ANALYSIS ───",
    ]

    labels = {"ema":"EMA","macd":"MACD","rsi":"RSI","stoch":"STCH","bb":"BB"}
    for tf in tfs:
        tfd = a["tf_data"].get(tf)
        if not tfd:
            lines.append(f"*{tf}* ⚠️ No data"); continue
        ema_dir = tfd.get("ema",("neutral",0))[0]
        bias = "🟢" if ema_dir=="bull" else ("🔴" if ema_dir=="bear" else "🟡")
        inds = ""
        for k,lk in labels.items():
            ind = tfd.get(k)
            if ind: inds += f"{ind_icon(ind[0])}{lk} "
        lines.append(f"*{tf}* {bias}  {inds.strip()}")

    lines += ["","──────────────────────────",""]

    if a["wait"]:
        lines += [
            "🟡 *SIGNAL: WAIT ⏳*","",
            f"_Reason: {a.get('reason','Conditions not met')}_",
            "_Wait for a cleaner setup._"
        ]
    else:
        fill = min(int(a["pct"]/10),10)
        bar  = "█"*fill+"░"*(10-fill)
        lines += [
            f"{a['emoji']} *{a['direction']} {a['sig']}*",
            f"📶 Confidence: *{a['pct']}%*",
            f"📊 `[{bar}]`",
            f"🗳 Votes: 🟢{a['bull_votes']} vs 🔴{a['bear_votes']}",
            f"⏰ *Expiry: {get_expiry(duration)}*","",
            "_Enter at open of next candle_",
        ]

    lines += ["","⚠️ _Educational only. Trade responsibly._"]
    return "\n".join(lines)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📊 Generate Signal", callback_data="new_signal")]]
    await update.message.reply_text(
        "👋 Welcome to *Chima Dtrader Ai*!\n\n"
        "🧠 *Intelligent signal engine:*\n"
        "✅ Trend gatekeeper (EMA)\n"
        "✅ MACD must confirm trend\n"
        "✅ RSI extreme filter\n"
        "✅ 4+ indicator votes required\n\n"
        "⏱ *Durations:* 15s · 30s · 1m · 5m · 15m · 30m\n\n"
        "Tap below 👇",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def new_signal(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    kb=[[InlineKeyboardButton(p, callback_data=f"pair_{p}") for p in row] for row in FOREX_PAIRS]
    await q.edit_message_text("💱 *Select a Forex pair:*",
                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SELECT_PAIR

async def pair_selected(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    pair=q.data.replace("pair_",""); ctx.user_data["pair"]=pair
    kb=[[InlineKeyboardButton(d, callback_data=f"dur_{d}")] for d in DURATIONS]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="new_signal")])
    await q.edit_message_text(f"✅ *{pair}* selected\n\n⏱ *Select duration:*",
                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SELECT_DURATION

async def dur_selected(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    dur=q.data.replace("dur_",""); pair=ctx.user_data.get("pair","EUR/USD")
    await q.edit_message_text(
        f"📡 Fetching *{pair}* live data...\n🧠 Running analysis ⏳",
        parse_mode="Markdown")
    a = await run_analysis(pair, dur)
    kb=[[InlineKeyboardButton("🔄 Refresh", callback_data=f"dur_{dur}"),
         InlineKeyboardButton("💱 New Pair", callback_data="new_signal")]]
    if a.get("error"):
        await q.edit_message_text(f"⚠️ *Error:* {a['message']}",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return SELECT_DURATION
    await q.edit_message_text(format_msg(pair, dur, a),
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_DURATION

async def help_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Chima Dtrader Ai*\n\n"
        "*Signal only fires when:*\n"
        "1️⃣ Trend TF EMA is clearly bull/bear\n"
        "2️⃣ Entry MACD agrees with trend\n"
        "3️⃣ RSI is not extreme against direction\n"
        "4️⃣ 4+ weighted indicator votes confirm\n\n"
        "*Durations:* 15s · 30s · 1m · 5m · 15m · 30m\n\n"
        "⚠️ _Educational only._", parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== CHIMA DTRADER AI STARTING ===")
    print(f"TOKEN: {'SET' if TOKEN else 'MISSING!'}")
    print(f"API KEY: {'SET' if TD_API_KEY else 'MISSING!'}")
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN missing"); sys.exit(1)
    bot_app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(new_signal, pattern="^new_signal$")],
        states={
            SELECT_PAIR:[CallbackQueryHandler(pair_selected, pattern="^pair_")],
            SELECT_DURATION:[
                CallbackQueryHandler(dur_selected, pattern="^dur_"),
                CallbackQueryHandler(new_signal, pattern="^new_signal$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)], per_message=False)
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    bot_app.add_handler(conv)
    print("🤖 Bot polling started!")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def index(): return "Chima Dtrader Ai is running."

    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True).start()
    main()
