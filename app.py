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
DURATIONS = ["1 min","5 min","15 min","30 min","1 hour"]
TIMEFRAMES = {
    "1 min":  [("1min","M1"),("5min","M5"),("15min","M15")],
    "5 min":  [("5min","M5"),("15min","M15"),("1h","H1")],
    "15 min": [("15min","M15"),("1h","H1"),("4h","H4")],
    "30 min": [("30min","M30"),("1h","H1"),("4h","H4")],
    "1 hour": [("1h","H1"),("4h","H4"),("1day","D1")],
}
DURATION_MINUTES = {"1 min":1,"5 min":5,"15 min":15,"30 min":30,"1 hour":60}
SELECT_PAIR, SELECT_DURATION = range(2)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_expiry_time(duration):
    mins = DURATION_MINUTES.get(duration, 5)
    expiry = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(minutes=mins+1)
    return expiry.strftime("%H:%M UTC")

def fetch_sync(pair, interval, size=100):
    p = urllib.parse.urlencode({"symbol":pair,"interval":interval,"outputsize":size,"apikey":TD_API_KEY,"format":"JSON"})
    try:
        with urllib.request.urlopen(f"{TD_BASE}/time_series?{p}", timeout=20) as r:
            d = json.loads(r.read().decode())
        return d.get("values") if "values" in d else None
    except:
        return None

async def fetch(pair, interval, size=100):
    return await asyncio.get_event_loop().run_in_executor(None, fetch_sync, pair, interval, size)

def closes(c): return [float(x["close"]) for x in c]
def highs(c):  return [float(x["high"])  for x in c]
def lows(c):   return [float(x["low"])   for x in c]
def vols(c):
    try: return [float(x.get("volume",1)) for x in c]
    except: return [1.0]*len(c)

# ── Indicators ────────────────────────────────────────────────────────────────

def rsi(prices, p=14):
    if len(prices)<p+1: return None
    pr=list(reversed(prices))
    g=[max(pr[i]-pr[i-1],0) for i in range(1,p+1)]
    l=[max(pr[i-1]-pr[i],0) for i in range(1,p+1)]
    ag,al=sum(g)/p,sum(l)/p
    for i in range(p+1,len(pr)):
        d=pr[i]-pr[i-1]; ag=(ag*(p-1)+max(d,0))/p; al=(al*(p-1)+max(-d,0))/p
    return round(100-(100/(1+ag/al)),2) if al else 100.0

def ema(prices, p):
    if len(prices)<p: return None
    pr=list(reversed(prices)); k=2/(p+1); e=sum(pr[:p])/p
    for x in pr[p:]: e=x*k+e*(1-k)
    return round(e,6)

def macd(prices):
    if len(prices)<35: return None
    pr=list(reversed(prices)); k12,k26,k9=2/13,2/27,2/10
    e12=sum(pr[:12])/12; e26=sum(pr[:26])/26; ms=[]
    for i in range(26,len(pr)):
        e12=pr[i]*k12+e12*(1-k12); e26=pr[i]*k26+e26*(1-k26); ms.append(e12-e26)
    if len(ms)<9: return None
    sig=sum(ms[:9])/9
    for v in ms[9:]: sig=v*k9+sig*(1-k9)
    ml=ms[-1]; prev=ms[-2] if len(ms)>=2 else ml
    return round(ml,6),round(sig,6),round(ml-sig,6),prev

def bollinger(prices, p=20):
    if len(prices)<p: return None
    w=[float(x) for x in prices[:p]]; mid=sum(w)/p
    std=(sum((x-mid)**2 for x in w)/p)**0.5
    up,lo=mid+2*std,mid-2*std; cur=prices[0]
    pb=(cur-lo)/(up-lo) if up!=lo else 0.5
    return {"pct_b":round(pb,4),"bw":round((up-lo)/mid if mid else 0,6)}

def stochastic(c, kp=14, dp=3):
    if len(c)<kp+dp: return None
    cl,hi,lo=closes(c),highs(c),lows(c); kv=[]
    for i in range(dp+1):
        wh,wl=max(hi[i:i+kp]),min(lo[i:i+kp])
        kv.append(100*(cl[i]-wl)/(wh-wl) if wh!=wl else 50.0)
    return {"k":round(kv[0],2),"d":round(sum(kv[:dp])/dp,2),"pk":kv[1]}

def atr(c, p=14):
    if len(c)<p+1: return None
    cl,hi,lo=closes(c),highs(c),lows(c)
    trs=[max(hi[i]-lo[i],abs(hi[i]-cl[i+1]),abs(lo[i]-cl[i+1])) for i in range(p)]
    a=sum(trs)/p
    return round((a/cl[0])*100,4) if cl[0] else 0

def adx(c, p=14):
    if len(c)<p*2: return None
    hi=list(reversed(highs(c))); lo=list(reversed(lows(c))); cl=list(reversed(closes(c)))
    pdm,mdm,trl=[],[],[]
    for i in range(1,len(hi)):
        hd=hi[i]-hi[i-1]; ld=lo[i-1]-lo[i]
        pdm.append(hd if hd>ld and hd>0 else 0)
        mdm.append(ld if ld>hd and ld>0 else 0)
        trl.append(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])))
    def ws(d,p):
        s=sum(d[:p]); r=[s]
        for v in d[p:]: s=s-s/p+v; r.append(s)
        return r
    at=ws(trl,p); pd=ws(pdm,p); md=ws(mdm,p)
    dip=[100*a/b if b else 0 for a,b in zip(pd,at)]
    dim=[100*a/b if b else 0 for a,b in zip(md,at)]
    dx=[100*abs(a-b)/(a+b) if (a+b) else 0 for a,b in zip(dip,dim)]
    if len(dx)<p: return None
    adxv=sum(dx[:p])/p
    for v in dx[p:]: adxv=(adxv*(p-1)+v)/p
    return {"adx":round(adxv,2),"dip":round(dip[-1],2),"dim":round(dim[-1],2)}

def vwap(c):
    cl,hi,lo,vs=closes(c),highs(c),lows(c),vols(c)
    tpv=sum(((hi[i]+lo[i]+cl[i])/3)*(vs[i] if vs[i]>0 else 1) for i in range(len(c)))
    tv=sum(vs[i] if vs[i]>0 else 1 for i in range(len(c)))
    v=tpv/tv if tv else cl[0]
    return round(((cl[0]-v)/v*100),4) if v else 0

def snr(c, lb=50):
    if len(c)<10: return None
    hi=highs(c[:lb]); lo=lows(c[:lb]); cl=closes(c)
    cur=cl[0]; sh,sl=[],[]
    for i in range(2,min(len(hi),lb)-2):
        if hi[i]>hi[i-1] and hi[i]>hi[i-2] and hi[i]>hi[i+1] and hi[i]>hi[i+2]: sh.append(hi[i])
        if lo[i]<lo[i-1] and lo[i]<lo[i-2] and lo[i]<lo[i+1] and lo[i]<lo[i+2]: sl.append(lo[i])
    res=min((h for h in sh if h>cur),default=max(hi))
    sup=max((l for l in sl if l<cur),default=min(lo))
    return {"sup":round(sup,6),"res":round(res,6),
            "dtr":round((res-cur)/cur*100,4),"dts":round((cur-sup)/cur*100,4)}

# ── Score ─────────────────────────────────────────────────────────────────────

def score_tf(c):
    sc=0; det={}; cl=closes(c)

    v=rsi(cl)
    if v is not None:
        if v<=20:   sc+=3;lbl=f"Oversold({v})🔥"
        elif v<=44: sc+=1;lbl=f"Mild bull({v})"
        elif v>=80: sc-=3;lbl=f"Overbought({v})🔥"
        elif v>=56: sc-=1;lbl=f"Mild bear({v})"
        else:           lbl=f"Neutral({v})"
        det["rsi"]=lbl

    m=macd(cl)
    if m:
        ml,sl,hist,prev=m; cup=ml>sl and prev<=sl; cdn=ml<sl and prev>=sl
        if ml>sl:
            b=3 if abs(hist)>0.00005 else 1; sc+=b+(1 if cup else 0)
            lbl="FreshBullX✅" if cup else ("StrongBull" if b==3 else "Bull")
        else:
            b=3 if abs(hist)>0.00005 else 1; sc-=b+(1 if cdn else 0)
            lbl="FreshBearX❌" if cdn else ("StrongBear" if b==3 else "Bear")
        det["macd"]=lbl

    e9,e21,e50=ema(cl,9),ema(cl,21),ema(cl,50)
    if e9 and e21 and e50:
        if e9>e21>e50:   sc+=3;lbl="Bull9>21>50✅"
        elif e9>e21:     sc+=1;lbl="Bull(9>21)"
        elif e9<e21<e50: sc-=3;lbl="Bear9<21<50❌"
        elif e9<e21:     sc-=1;lbl="Bear(9<21)"
        else:                  lbl="Mixed"
        det["ema"]=lbl

    bb=bollinger(cl)
    if bb:
        pb=bb["pct_b"]; bw=bb["bw"]
        if bw<0.001:   sc-=1;lbl="Squeeze⚠️"
        elif pb<=0.05: sc+=2;lbl="LowerBand🔥"
        elif pb<=0.2:  sc+=1;lbl="NearLower"
        elif pb>=0.95: sc-=2;lbl="UpperBand🔥"
        elif pb>=0.8:  sc-=1;lbl="NearUpper"
        else:               lbl=f"Mid({round(pb*100)}%)"
        det["bb"]=lbl

    st=stochastic(c)
    if st:
        k,d,pk=st["k"],st["d"],st["pk"]
        cu=k>d and pk<=d; cd=k<d and pk>=d
        if k<20 and d<20:   sc+=2;lbl=f"Oversold🔥"
        elif k<20:          sc+=1;lbl=f"OversoldK={k}"
        elif k>80 and d>80: sc-=2;lbl=f"Overbought🔥"
        elif k>80:          sc-=1;lbl=f"OverboughtK={k}"
        elif cu:            sc+=1;lbl=f"BullX K={k}"
        elif cd:            sc-=1;lbl=f"BearX K={k}"
        else:                    lbl=f"Neutral"
        det["stoch"]=lbl

    ap=atr(c)
    if ap is not None:
        if ap<0.005:   sc-=1;lbl="LowVol⚠️"
        elif ap>0.5:   sc-=1;lbl="HighVol⚠️"
        else:          sc+=1;lbl=f"GoodVol✅"
        det["atr"]=lbl

    ad=adx(c)
    if ad:
        a,dip,dim=ad["adx"],ad["dip"],ad["dim"]
        if a>=30 and dip>dim:   sc+=2;lbl=f"StrongUp💪"
        elif a>=25 and dip>dim: sc+=1;lbl=f"Uptrend"
        elif a>=30 and dim>dip: sc-=2;lbl=f"StrongDown💪"
        elif a>=25 and dim>dip: sc-=1;lbl=f"Downtrend"
        else:                        lbl=f"Ranging"
        det["adx"]=lbl

    vd=vwap(c)
    if vd<=-0.1:   sc+=2;lbl="BelowVWAP✅"
    elif vd<=-0.03:sc+=1;lbl="SlightBelow"
    elif vd>=0.1:  sc-=2;lbl="AboveVWAP✅"
    elif vd>=0.03: sc-=1;lbl="SlightAbove"
    else:               lbl="AtVWAP"
    det["vwap"]=lbl

    sr=snr(c)
    if sr:
        if sr["dts"]<0.05:   sc+=2;lbl=f"AtSupport🔥"
        elif sr["dts"]<0.15: sc+=1;lbl=f"NearSupport"
        elif sr["dtr"]<0.05: sc-=2;lbl=f"AtResist🔥"
        elif sr["dtr"]<0.15: sc-=1;lbl=f"NearResist"
        else:                     lbl=f"S:{sr['sup']}|R:{sr['res']}"
        det["sr"]=lbl

    return {"score":sc,"det":det,"error":False}


async def analyse(pair, duration):
    tfl=TIMEFRAMES[duration]
    sets=await asyncio.gather(*[fetch(pair,iv,120) for iv,_ in tfl])
    results={}; total=0; any_data=False
    for (iv,lbl),c in zip(tfl,sets):
        if not c or len(c)<50: results[lbl]={"error":True,"score":0,"det":{}}; continue
        any_data=True; r=score_tf(c); results[lbl]=r; total+=r["score"]
    if not any_data: return {"error":True,"message":"No market data. Check API key."}

    # Count individual indicator votes
    bull=0; bear=0; total_i=0
    for r in results.values():
        if r.get("error"): continue
        for lbl in r["det"].values():
            total_i+=1; l=lbl.lower()
            if any(x in l for x in ["oversold","bull","belowvwap","support","goodvol","freshbullx","strongup","uptrend","lowerband","nearsupp"]):
                bull+=1
            elif any(x in l for x in ["overbought","bear","abovevwap","resist","freshbearx","strongdown","downtrend","upperband","nearresist"]):
                bear+=1

    if bull>=4 and bull>bear:    direction,emoji,sig="BUY","🟢","📈"
    elif bear>=4 and bear>bull:  direction,emoji,sig="SELL","🔴","📉"
    else:                        direction,emoji,sig="WAIT","🟡","⏳"

    pct=min(round(max(bull,bear)/max(total_i,1)*100),95)
    tfs=[lbl for _,lbl in tfl]
    return {"error":False,"direction":direction,"emoji":emoji,"sig":sig,
            "pct":pct,"bull":bull,"bear":bear,"total_i":total_i,
            "results":results,"tfs":tfs}


# ── Format ────────────────────────────────────────────────────────────────────

def icon(lbl):
    l=lbl.lower()
    if any(x in l for x in ["oversold","bull","belowvwap","support","goodvol","freshbullx","strongup","uptrend","lowerband","nearsupp"]): return "🟢"
    if any(x in l for x in ["overbought","bear","abovevwap","resist","freshbearx","strongdown","downtrend","upperband","nearresist"]): return "🔴"
    if any(x in l for x in ["squeeze","lowvol","highvol"]): return "⚠️"
    return "🟡"

def fmt(pair, duration, a):
    now=datetime.utcnow().strftime("%H:%M  %d %b %Y")
    lines=["╔══════════════════════╗","║  📊 QUOTEX SIGNALS   ║","╚══════════════════════╝",
           f"💱 *{pair}*  ⏱ *{duration}*",f"🕐 {now}","","─── TIMEFRAME BREAKDOWN ───"]
    for tf in a["tfs"]:
        r=a["results"].get(tf,{})
        if r.get("error"): lines.append(f"*{tf}* ⚠️ No data"); continue
        sc=r["score"]; bias="🟢" if sc>0 else ("🔴" if sc<0 else "🟡")
        inds=" ".join(f"{icon(v)}{k.upper()}" for k,v in r["det"].items())
        lines.append(f"*{tf}* {bias}`{sc:+d}`  {inds}")
    fill=min(int(a["pct"]/10),10); bar="█"*fill+"░"*(10-fill)
    lines+=["","──────────────────────────",""]
    if a["direction"]=="WAIT":
        lines+=["🟡 *WAIT ⏳*",
                f"_🟢x{a['bull']} vs 🔴x{a['bear']} / {a['total_i']} indicators_",
                "_Need 4+ to agree. Wait for better setup._"]
    else:
        lines+=[f"{a['emoji']} *{a['direction']} {a['sig']}*",
                f"📶 Confidence: *{a['pct']}%*  `[{bar}]`",
                f"🗳 Votes: 🟢x{a['bull']} vs 🔴x{a['bear']} / {a['total_i']}",
                f"⏰ *Expiry: {get_expiry_time(duration)}*",
                "_Enter at open of next candle_"]
    lines+=["","⚠️ _Educational only. Trade responsibly._"]
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("📊 Generate Signal",callback_data="new_signal")]]
    await update.message.reply_text(
        "👋 Welcome to *Chima Dtrader Ai*!\n\n"
        "🧠 *9 indicators × 3 timeframes:*\n"
        "RSI • MACD • EMA • BB • Stoch\n"
        "ATR • ADX • VWAP • Support/Resistance\n\n"
        "⚡ Signal fires when 4+ indicators agree\n"
        "⏰ Includes trade expiry time\n\n"
        "Tap below 👇",
        reply_markup=InlineKeyboardMarkup(kb),parse_mode="Markdown")

async def new_signal(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    kb=[[InlineKeyboardButton(p,callback_data=f"pair_{p}") for p in row] for row in FOREX_PAIRS]
    await q.edit_message_text("💱 *Select a Forex pair:*",reply_markup=InlineKeyboardMarkup(kb),parse_mode="Markdown")
    return SELECT_PAIR

async def pair_selected(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    pair=q.data.replace("pair_",""); ctx.user_data["pair"]=pair
    kb=[[InlineKeyboardButton(d,callback_data=f"dur_{d}")] for d in DURATIONS]
    kb.append([InlineKeyboardButton("🔙 Back",callback_data="new_signal")])
    await q.edit_message_text(f"✅ *{pair}* selected\n\n⏱ *Select duration:*",
                              reply_markup=InlineKeyboardMarkup(kb),parse_mode="Markdown")
    return SELECT_DURATION

async def dur_selected(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    dur=q.data.replace("dur_",""); pair=ctx.user_data.get("pair","EUR/USD")
    await q.edit_message_text(f"📡 Fetching *{pair}* data...\n🧠 Running 9 indicators × 3 TFs ⏳",parse_mode="Markdown")
    a=await analyse(pair,dur)
    kb=[[InlineKeyboardButton("🔄 Refresh",callback_data=f"dur_{dur}"),
         InlineKeyboardButton("💱 New Pair",callback_data="new_signal")]]
    if a.get("error"):
        await q.edit_message_text(f"⚠️ *Error:* {a['message']}",parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb)); return SELECT_DURATION
    await q.edit_message_text(fmt(pair,dur,a),parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_DURATION

async def help_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Chima Dtrader Ai — How it works*\n\n"
        "1️⃣ Select pair → 2️⃣ Select duration\n"
        "3️⃣ Live candles fetched\n"
        "4️⃣ 9 indicators × 3 TFs calculated\n"
        "5️⃣ Signal fires when 4+ agree\n"
        "6️⃣ Expiry time shown\n\n"
        "⚠️ _Educational only._",parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== CHIMA DTRADER AI STARTING ===")
    print(f"TOKEN: {'SET' if TOKEN else 'MISSING!'}")
    print(f"API KEY: {'SET' if TD_API_KEY else 'MISSING!'}")
    if not TOKEN: print("ERROR: Set TELEGRAM_BOT_TOKEN in Render Environment"); sys.exit(1)

    app=Application.builder().token(TOKEN).build()
    conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(new_signal,pattern="^new_signal$")],
        states={
            SELECT_PAIR:[CallbackQueryHandler(pair_selected,pattern="^pair_")],
            SELECT_DURATION:[CallbackQueryHandler(dur_selected,pattern="^dur_"),
                             CallbackQueryHandler(new_signal,pattern="^new_signal$")],
        },
        fallbacks=[CommandHandler("start",start)],per_message=False)
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(conv)
    print("🤖 Bot polling started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    port=int(os.environ.get("PORT",8080))
    flask_app=Flask(__name__)

    @flask_app.route("/")
    def index(): return "Chima Dtrader Ai is running."

    t=threading.Thread(target=lambda:flask_app.run(host="0.0.0.0",port=port,use_reloader=False),daemon=True)
    t.start()
    main()
