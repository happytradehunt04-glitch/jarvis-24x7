import os, threading, io, json, time, requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import gspread

TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDENTIALS")
CHAT_IDS = set()
TRADES = {}

PAIRS = {
    "xauusd": ("GC=F", "GOLD"), "gold": ("GC=F", "GOLD"),
    "btcusd": ("BTC-USD", "BTC"), "btc": ("BTC-USD", "BTC"),
    "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"), "gbpjpy": ("GBPJPY=X", "GBPJPY"),
}

def get_sheet():
    if not SHEET_ID or not GOOGLE_CREDS: return None
    try:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
        return gc.open_by_key(SHEET_ID)
    except: return None

def load_chats_from_sheet():
    try:
        sh = get_sheet()
        if not sh: return
        # 2nd sheet for chats, if not exist create
        try: ws = sh.worksheet("chats")
        except: ws = sh.add_worksheet("chats", 100, 2)
        vals = ws.get_all_values()
        for r in vals:
            if r and r[0].isdigit(): CHAT_IDS.add(int(r[0]))
        print(f"Loaded chats {CHAT_IDS}")
    except Exception as e: print(e)

def save_chat(cid):
    CHAT_IDS.add(cid)
    try:
        sh = get_sheet()
        if not sh: return
        try: ws = sh.worksheet("chats")
        except: ws = sh.add_worksheet("chats", 100, 2)
        vals = ws.get_all_values()
        existing = [int(r[0]) for r in vals if r and r[0].isdigit()]
        if cid not in existing: ws.append_row([str(cid), datetime.now().strftime("%Y-%m-%d")])
    except: pass

def get_df(sym):
    if "BTC" in sym:
        try:
            url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"
            r = requests.get(url, timeout=10).json()
            df = pd.DataFrame(r, columns=["ot","Open","High","Low","Close","vol","ct","qav","tr","tbav","tqav","i"])
            df = df[["Open","High","Low","Close"]].astype(float)
            return df
        except: pass
    headers = {"User-Agent": "Mozilla/5.0"}
    for range_, interval in [("1mo","1d"), ("5d","1h")]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range_}&interval={interval}"
            r = requests.get(url, headers=headers, timeout=15).json()
            result = r['chart']['result'][0]
            quote = result['indicators']['quote'][0]
            df = pd.DataFrame({"Open":quote['open'],"High":quote['high'],"Low":quote['low'],"Close":quote['close']}).dropna()
            if len(df) > 20: return df
        except: continue
    return None

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, None, "WAIT"
    close=df['Close']; price=float(close.iloc[-1])
    ema50=close.ewm(50).mean(); ema200=close.ewm(200).mean()
    rsi_val=55
    try:
        delta=close.diff(); gain=delta.where(delta>0,0).rolling(14).mean(); loss=-delta.where(delta<0,0).rolling(14).mean(); rsi=100-(100/(1+gain/loss)); rsi_val=float(rsi.iloc[-1])
    except: pass
    e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    ah=float(close.iloc[-20:-5].max()); al=float(close.iloc[-20:-5].min())
    bias="WAIT"; reason=f"Range {al:.2f}-{ah:.2f}"
    # ✅ Easy condition for testing - taaki signal aaye
    if price>al and e50>e200: bias="BUY"; reason=f"BUY Setup AH {ah:.2f}"
    elif price<ah and e50<e200: bias="SELL"; reason=f"SELL Setup AL {al:.2f}"
    d=df[-30:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
    for i in range(len(d)):
        o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
        ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
    ax.plot(ema50[-30:].values,color='blue',lw=0.8); ax.plot(ema200[-30:].values,color='orange',lw=0.8)
    ax.set_title(f"{name} {price:.2f} | {bias}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=120); buf.seek(0); plt.close(fig)
    return buf, {"price":price,"reason":reason}, bias

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_IDS:
        load_chats_from_sheet()
        if not CHAT_IDS: return
    print(f"Auto job running for {CHAT_IDS}")
    for _,(sym,name) in PAIRS.items():
        try:
            buf,data,bias=analyse(sym,name)
            if bias in ["BUY","SELL"] and buf:
                for cid in list(CHAT_IDS):
                    try:
                        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
                        buf.seek(0)
                        sent=await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f}")
                        tid=f"{name}_{int(datetime.now().timestamp())}_{cid}"
                        TRADES[tid]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"status":"OPEN","msg_id":sent.message_id,"chat_id":cid}
                        sh=get_sheet()
                        if sh:
                            ws=sh.sheet1
                            ws.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, entry*1.015, "OPEN", "", sent.message_id, cid])
                    except Exception as e: print(f"Auto send fail {e}")
        except Exception as e: print(f"Auto analyse fail {e}")

async def start(update, context):
    save_chat(update.effective_chat.id)
    await update.message.reply_text(f"V6.3 AUTO FIX LIVE ✅\nTera Chat ID {update.effective_chat.id} Save ho gaya\nAb Auto Signal ayega\nSheet: {len(CHAT_IDS)} users\n/signal btcusd\n/weekly")

async def sig(update, context):
    arg=(context.args[0].lower() if context.args else "btcusd")
    sym,name=PAIRS.get(arg, ("BTC-USD","BTC"))
    save_chat(update.effective_chat.id)
    await update.message.reply_text(f"{name} checking...")
    buf,data,bias=analyse(sym,name)
    if not buf: await update.message.reply_text("Data nahi mila, 30 sec baad try karo"); return
    entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
    tid=f"{name}_{int(datetime.now().timestamp())}"
    cap=f"{'🟢 '+bias if bias!='WAIT' else '🟡 WAIT'} {name} {entry:.2f}\nENTRY {entry:.2f}\nSL {sl:.2f}\nTP1 {tp1:.2f}\nTP2 {tp2:.2f}\n{data['reason']}\nID:{tid}"
    sent=await update.message.reply_photo(photo=buf, caption=cap)
    if bias!="WAIT":
        TRADES[tid]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"status":"OPEN","msg_id":sent.message_id,"chat_id":update.effective_chat.id}
        sh=get_sheet()
        if sh:
            try: sh.sheet1.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", "", sent.message_id, update.effective_chat.id])
            except: pass

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh: await update.message.reply_text(f"Local {len(TRADES)}"); return
    vals=sh.sheet1.get_all_values()
    if len(vals)<2: await update.message.reply_text("Sheet khali - koi signal nahi gaya isliye"); return
    rows=vals[1:]; total=len(rows)
    tp1=len([r for r in rows if len(r)>8 and "TP" in r[8].upper()])
    sl=len([r for r in rows if len(r)>8 and "SL" in r[8].upper()])
    await update.message.reply_text(f"📊 WEEKLY\nTotal: {total}\n✅ TP: {tp1}\n❌ SL: {sl}\nOPEN: {total-tp1-sl}")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V6.3 Live")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

if __name__=="__main__":
    load_chats_from_sheet()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("result", weekly_cmd))
    app.job_queue.run_repeating(auto_job, interval=600, first=20)
    print("V6.3 Starting")
    app.run_polling(drop_pending_updates=True)
