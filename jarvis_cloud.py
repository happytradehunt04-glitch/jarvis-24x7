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
    "audusd": ("AUDUSD=X", "AUDUSD"), "usdcad": ("USDCAD=X", "USDCAD"),
}

def get_sheet():
    if not SHEET_ID or not GOOGLE_CREDS: return None
    try:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
        return gc.open_by_key(SHEET_ID)
    except: return None

def load_chats():
    try:
        sh=get_sheet()
        if not sh: return
        try: ws=sh.worksheet("chats")
        except: ws=sh.add_worksheet("chats",100,2)
        for r in ws.get_all_values():
            if r and r[0].isdigit(): CHAT_IDS.add(int(r[0]))
    except: pass

def save_chat(cid):
    CHAT_IDS.add(cid)
    try:
        sh=get_sheet()
        if not sh: return
        try: ws=sh.worksheet("chats")
        except: ws=sh.add_worksheet("chats",100,2)
        vals=[int(r[0]) for r in ws.get_all_values() if r and r[0].isdigit()]
        if cid not in vals: ws.append_row([str(cid), datetime.now().strftime("%Y-%m-%d")])
    except: pass

def get_df(sym):
    if "BTC" in sym:
        try:
            r=requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100", timeout=10).json()
            df=pd.DataFrame(r, columns=["ot","Open","High","Low","Close","vol","ct","qav","tr","tbav","tqav","i"])
            return df[["Open","High","Low","Close"]].astype(float)
        except: pass
    headers={"User-Agent":"Mozilla/5.0"}
    for range_, interval in [("1mo","1d"), ("5d","1h")]:
        try:
            url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range_}&interval={interval}"
            r=requests.get(url, headers=headers, timeout=15).json()
            q=r['chart']['result'][0]['indicators']['quote'][0]
            df=pd.DataFrame({"Open":q['open'],"High":q['high'],"Low":q['low'],"Close":q['close']}).dropna()
            if len(df)>20: return df
        except: continue
    return None

def analyse(sym, name):
    df=get_df(sym)
    if df is None: return None, None, "WAIT"
    close=df['Close']; price=float(close.iloc[-1])
    ema50=close.ewm(50).mean(); ema200=close.ewm(200).mean()
    e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    # Simple setup
    bias="WAIT"
    if e50>e200: bias="BUY"
    elif e50<e200: bias="SELL"
    else: bias="WAIT"
    d=df[-25:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
    for i in range(len(d)):
        o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
        ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
    ax.plot(ema50[-25:].values,color='blue',lw=0.8); ax.plot(ema200[-25:].values,color='orange',lw=0.8)
    ax.set_title(f"{name} {price:.2f} | {bias}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=120); buf.seek(0); plt.close(fig)
    return buf, {"price":price}, bias

# ✅ FIX: Saturday/Sunday Logic
def is_market_open(name):
    weekday = datetime.utcnow().weekday() # 0=Mon, 5=Sat, 6=Sun
    if name == "BTC": return True # Crypto 24x7
    # Forex & Gold: Mon-Fri only
    if weekday >= 5: return False # Sat, Sun band
    return True

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_IDS: load_chats()
    if not CHAT_IDS: return
    # Check day
    weekday = datetime.utcnow().weekday()
    day_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][weekday]
    print(f"Auto check {day_name} - Chats {CHAT_IDS}")
    for _,(sym,name) in PAIRS.items():
        if not is_market_open(name):
            print(f"Skip {name} - Market closed {day_name}")
            continue
        try:
            buf,data,bias=analyse(sym,name)
            if bias in ["BUY","SELL"] and buf:
                for cid in list(CHAT_IDS):
                    try:
                        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
                        buf.seek(0)
                        sent=await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f}\nDay: {day_name}\nSL {sl:.2f} TP1 {tp1:.2f}")
                        tid=f"{name}_{int(datetime.now().timestamp())}_{cid}"
                        TRADES[tid]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"status":"OPEN","msg_id":sent.message_id,"chat_id":cid}
                        sh=get_sheet()
                        if sh: sh.sheet1.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, entry*1.015, "OPEN", "", sent.message_id, cid])
                    except: pass
        except: pass

async def start(update, context):
    save_chat(update.effective_chat.id)
    wd = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][datetime.utcnow().weekday()]
    await update.message.reply_text(f"V6.4 SATURDAY FIX LIVE ✅\nAaj {wd} hai\nSat-Sun: Sirf BTC chalega\nMon-Fri: Forex+GOLD+BTC\nChat Save: {update.effective_chat.id}\n/signal btcusd\n/weekly")

async def sig(update, context):
    arg=(context.args[0].lower() if context.args else "btcusd")
    sym,name=PAIRS.get(arg, ("BTC-USD","BTC"))
    save_chat(update.effective_chat.id)
    if not is_market_open(name):
        await update.message.reply_text(f"⚠️ Aaj Saturday/Sunday hai\n{name} ka market band hai\nSirf BTC ka signal milega\nKal Monday se {name} chalega"); return
    await update.message.reply_text(f"{name} checking...")
    buf,data,bias=analyse(sym,name)
    if not buf: await update.message.reply_text("Data nahi mila"); return
    entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
    tid=f"{name}_{int(datetime.now().timestamp())}"
    sent=await update.message.reply_photo(photo=buf, caption=f"{bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f}\nID:{tid}")
    if bias!="WAIT":
        sh=get_sheet()
        if sh:
            try: sh.sheet1.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", "", sent.message_id, update.effective_chat.id])
            except: pass

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh: await update.message.reply_text("Sheet nahi"); return
    vals=sh.sheet1.get_all_values()
    if len(vals)<2: await update.message.reply_text("Sheet khali - isliye track nahi hua"); return
    rows=vals[1:]; total=len(rows)
    await update.message.reply_text(f"📊 WEEKLY\nTotal: {total}\nAaj: {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][datetime.utcnow().weekday()]} ko sirf BTC ka count hoga")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V6.4 Saturday Fix Live")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

if __name__=="__main__":
    load_chats()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("result", weekly_cmd))
    app.job_queue.run_repeating(auto_job, interval=600, first=30)
    print("V6.4 Saturday Fix Starting")
    app.run_polling(drop_pending_updates=True)
