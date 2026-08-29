import os, threading, io, json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf
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
    "xauusd":("GC=F","GOLD"),"gold":("GC=F","GOLD"),
    "btcusd":("BTC-USD","BTC"),"btc":("BTC-USD","BTC"),
    "eurusd":("EURUSD=X","EURUSD"),"gbpusd":("GBPUSD=X","GBPUSD"),
}
CRYPTO = ["BTC"]

def get_sheet():
    if not SHEET_ID or not GOOGLE_CREDS: return None
    try:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
        return gc.open_by_key(SHEET_ID).sheet1
    except: return None

def get_df(sym):
    try:
        df = yf.download(sym, period="2d", interval="15m", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        return df if len(df)>40 else None
    except: return None

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, "WAIT", None
    close=df['Close']; price=float(close.iloc[-1])
    ema50=close.ewm(50).mean(); ema200=close.ewm(200).mean()
    delta=close.diff(); gain=delta.where(delta>0,0).rolling(14).mean(); loss=-delta.where(delta<0,0).rolling(14).mean(); rsi=100-(100/(1+gain/loss))
    rsi_val=float(rsi.iloc[-1]); e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    ah=float(close.iloc[-32:-8].max()); al=float(close.iloc[-32:-8].min())
    bias="WAIT"; reason=f"Inside {al:.0f}-{ah:.0f} RSI {rsi_val:.1f}"
    if price>ah and e50>e200 and rsi_val>55: bias="BUY"
    elif price<al and e50<e200 and rsi_val<45: bias="SELL"
    d=df[-40:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
    for i in range(len(d)):
        o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
        ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
    ax.plot(ema50[-40:].values,color='blue',lw=0.8); ax.plot(ema200[-40:].values,color='orange',lw=0.8)
    ax.axhline(ah,color='green',ls='--',lw=0.8); ax.axhline(al,color='red',ls='--',lw=0.8)
    ax.set_title(f"{name} {price:.2f} | {bias}");
    plt.tight_layout(); plt.savefig(buf,format='png',dpi=130); buf.seek(0); plt.close(fig)
    return buf, bias, {"price":price,"reason":reason}

async def tp_checker(context: ContextTypes.DEFAULT_TYPE):
    if not TRADES: return
    for tid, t in list(TRADES.items()):
        df=get_df(t['sym'])
        if df is None: continue
        price=float(df['Close'].iloc[-1]); sh=get_sheet()
        if t['bias']=="BUY" and price>=t['tp1'] and t['status']=="OPEN":
            TRADES[tid]['status']="TP1_HIT"
            await context.bot.send_message(chat_id=t['chat_id'], text=f"✅ TP1 HIT {t['name']} {price:.2f}")
            if sh:
                try: sh.update_cell(sh.find(tid).row, 9, "TP1_HIT")
                except: pass

async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V5.7 LIVE ✅\n/signal xauusd\n/signal btcusd")

async def sig(update, context):
    arg=(context.args[0].lower() if context.args else "xauusd")
    sym,name=PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} checking...")
    buf,bias,data=analyse(sym,name)
    if not buf: await update.message.reply_text("Market band"); return
    entry=data["price"]; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
    caption=f"{bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f}\nID:{name}_{int(datetime.now().timestamp())}"
    sent=await update.message.reply_photo(photo=buf,caption=caption)

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Jarvis V5.7 Live")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

if __name__=="__main__":
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.job_queue.run_repeating(tp_checker, interval=300, first=30)
    print("Starting V5.7")
    app.run_polling(drop_pending_updates=True)
