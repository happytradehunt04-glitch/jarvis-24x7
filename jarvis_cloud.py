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

PAIRS = {
    "xauusd": ("GC=F", "GOLD"), "gold": ("GC=F", "GOLD"),
    "btcusd": ("BTC-USD", "BTC"), "btc": ("BTC-USD", "BTC"),
    "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"),
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
        sh=get_sheet();
        if not sh: return
        try: ws=sh.worksheet("chats")
        except: ws=sh.add_worksheet("chats",100,2)
        vals=[int(r[0]) for r in ws.get_all_values() if r and r[0].isdigit()]
        if cid not in vals: ws.append_row([str(cid)])
    except: pass

# ✅ FIX: Binance + Yahoo dono me full try/except
def get_df(sym):
    # BTC ke liye 2 source
    if "BTC" in sym:
        # Source 1: Binance
        try:
            url="https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=80"
            data=requests.get(url, timeout=10).json()
            if isinstance(data, list) and len(data)>20:
                df=pd.DataFrame(data, columns=["ot","Open","High","Low","Close","vol","ct","qav","tr","tbav","tqav","i"])
                df=df[["Open","High","Low","Close"]].astype(float)
                print("BTC Binance OK"); return df
        except Exception as e: print(f"Binance fail {e}")
        # Source 2: Direct Yahoo for BTC
        try:
            headers={"User-Agent":"Mozilla/5.0"}
            url="https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1mo&interval=1d"
            r=requests.get(url, headers=headers, timeout=15).json()
            q=r['chart']['result'][0]['indicators']['quote'][0]
            df=pd.DataFrame({"Open":q['open'],"High":q['high'],"Low":q['low'],"Close":q['close']}).dropna()
            if len(df)>20: print("BTC Yahoo OK"); return df
        except Exception as e: print(f"BTC Yahoo fail {e}")

    # Forex / Gold
    headers={"User-Agent":"Mozilla/5.0"}
    for range_, interval in [("1mo","1d"), ("5d","1h")]:
        try:
            url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range_}&interval={interval}"
            r=requests.get(url, headers=headers, timeout=15).json()
            q=r['chart']['result'][0]['indicators']['quote'][0]
            df=pd.DataFrame({"Open":q['open'],"High":q['high'],"Low":q['low'],"Close":q['close']}).dropna()
            if len(df)>10: return df
        except: continue
    return None

def analyse(sym, name):
    try:
        df=get_df(sym)
        if df is None or len(df)<10: return None, None, "WAIT"
        close=df['Close']; price=float(close.iloc[-1])
        ema50=close.ewm(20).mean(); ema200=close.ewm(50).mean()
        e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
        bias="BUY" if e50>e200 else "SELL"
        d=df[-25:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
        for i in range(len(d)):
            o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
            ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
        ax.plot(ema50[-25:].values,color='blue',lw=0.8); ax.plot(ema200[-25:].values,color='orange',lw=0.8)
        ax.set_title(f"{name} {price:.2f} | {bias}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=120); buf.seek(0); plt.close(fig)
        return buf, {"price":price}, bias
    except Exception as e:
        print(f"Analyse error {e}")
        return None, None, "WAIT"

def is_market_open(name):
    wd=datetime.utcnow().weekday() # 0 Mon 5 Sat 6 Sun
    if name=="BTC": return True
    return wd < 5 # Mon-Fri only

async def auto_job(context):
    if not CHAT_IDS: load_chats()
    if not CHAT_IDS: return
    wd=datetime.utcnow().weekday()
    for _,(sym,name) in PAIRS.items():
        if not is_market_open(name): continue
        try:
            buf,data,bias=analyse(sym,name)
            if bias in ["BUY","SELL"] and buf:
                for cid in list(CHAT_IDS):
                    try:
                        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
                        buf.seek(0)
                        await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f} SL {sl:.2f} TP1 {tp1:.2f}")
                        sh=get_sheet()
                        if sh: sh.sheet1.append_row([f"{name}_{int(datetime.now().timestamp())}", datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, entry*1.015, "OPEN"])
                    except: pass
        except: pass

async def start(update, context):
    save_chat(update.effective_chat.id)
    wd=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][datetime.utcnow().weekday()]
    await update.message.reply_text(f"V6.5 HANG FIX LIVE ✅\nAaj {wd}\nSat-Sun: Only BTC\nMon-Fri: All\n/signal btcusd\n/signal xauusd\n/weekly")

# ✅ FIX: Sig me full try/except taaki hang na ho
async def sig(update, context):
    try:
        arg=(context.args[0].lower() if context.args else "btcusd")
        sym,name=PAIRS.get(arg, ("BTC-USD","BTC"))
        save_chat(update.effective_chat.id)
        if not is_market_open(name):
            await update.message.reply_text(f"⚠️ {name} market band hai (Sat/Sun)\nSirf BTC chalega"); return
        await update.message.reply_text(f"{name} checking... data le raha hu")
        buf,data,bias=analyse(sym,name)
        if not buf or not data:
            await update.message.reply_text(f"⚠️ {name} ka data abhi nahi mila\nBinance/Yahoo busy hai, 1 min baad /signal {arg} fir se bhejo"); return
        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
        await update.message.reply_photo(photo=buf, caption=f"{bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f}")
        sh=get_sheet()
        if sh and bias!="WAIT":
            try: sh.sheet1.append_row([f"{name}_{int(datetime.now().timestamp())}", datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN"])
            except: pass
    except Exception as e:
        print(f"Sig error {e}")
        await update.message.reply_text(f"Error aa gaya {e}, 1 min baad try karo")

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh: await update.message.reply_text("Sheet nahi"); return
    vals=sh.sheet1.get_all_values()
    if len(vals)<2: await update.message.reply_text("Sheet khali"); return
    rows=vals[1:]; await update.message.reply_text(f"📊 Total Signals: {len(rows)}")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V6.5 Hang Fix Live")
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
    print("V6.5 Starting")
    app.run_polling(drop_pending_updates=True)
