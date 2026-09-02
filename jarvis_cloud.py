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
    "xauusd": ("GC=F", "GOLD"), "gold": ("GC=F", "GOLD"), "xau": ("GC=F", "GOLD"),
    "btcusd": ("BTC-USD", "BTC"), "btc": ("BTC-USD", "BTC"),
    "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"), "gbpjpy": ("GBPJPY=X", "GBPJPY"),
    "audusd": ("AUDUSD=X", "AUDUSD"), "usdcad": ("USDCAD=X", "USDCAD"),
    "us30": ("^DJI", "US30"), "nas100": ("^IXIC", "NAS100")
}
CRYPTO = ["BTC"]

def get_sheet():
    if not SHEET_ID or not GOOGLE_CREDS: return None
    try:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
        sh = gc.open_by_key(SHEET_ID).sheet1
        vals = sh.get_all_values()
        if not vals or vals[0][0].lower()!= "trade_id":
            sh.clear()
            sh.append_row(["trade_id","date","pair","bias","entry","sl","tp1","tp2","status","message_id","chat_id"])
        return sh
    except: return None

# ✅ NEW: Direct Yahoo + Binance fix
def get_df(sym):
    # 1. Try Binance for BTC (100% works on Render)
    if "BTC" in sym:
        try:
            url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100"
            r = requests.get(url, timeout=10).json()
            df = pd.DataFrame(r, columns=["ot","Open","High","Low","Close","vol","ct","qav","tr","tbav","tqav","i"])
            df = df[["Open","High","Low","Close"]].astype(float)
            return df
        except Exception as e:
            print(f"Binance fail {e}")

    # 2. Try Direct Yahoo Chart API (bypasses yfinance block)
    headers = {"User-Agent": "Mozilla/5.0"}
    for range_, interval in [("1mo","1d"), ("5d","1h"), ("2d","15m")]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range_}&interval={interval}"
            r = requests.get(url, headers=headers, timeout=15).json()
            result = r['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            df = pd.DataFrame(quote)
            df['Open'] = quote['open']; df['High'] = quote['high']; df['Low'] = quote['low']; df['Close'] = quote['close']
            df = df.dropna()
            if len(df) > 30: return df
        except Exception as e:
            print(f"Yahoo direct fail {sym} {e}")
            continue
    return None

def analyse(sym, name):
    df = get_df(sym)
    if df is None or len(df) < 20:
        return None, None, "WAIT"
    close=df['Close']; price=float(close.iloc[-1])
    ema50=close.ewm(50).mean(); ema200=close.ewm(200).mean()
    delta=close.diff(); gain=delta.where(delta>0,0).rolling(14).mean(); loss=-delta.where(delta<0,0).rolling(14).mean()
    rsi = 100-(100/(1+gain/loss)); rsi_val=float(rsi.iloc[-1])
    e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    ah=float(close.iloc[-32:-8].max()); al=float(close.iloc[-32:-8].min())
    bias="WAIT"; reason=f"Range {al:.2f}-{ah:.2f} RSI {rsi_val:.1f}"
    if price>ah and e50>e200 and rsi_val>55: bias="BUY"; reason=f"Breakout {ah:.2f} Bull RSI {rsi_val:.1f}"
    elif price<al and e50<e200 and rsi_val<45: bias="SELL"; reason=f"Breakdown {al:.2f} Bear RSI {rsi_val:.1f}"
    d=df[-40:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
    for i in range(len(d)):
        o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
        ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
    ax.plot(ema50[-40:].values,color='blue',lw=0.8); ax.plot(ema200[-40:].values,color='orange',lw=0.8)
    ax.set_title(f"{name} {price:.2f} | {bias}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=120); buf.seek(0); plt.close(fig)
    return buf, {"price":price,"reason":reason}, bias

async def tp_checker(context):
    if not TRADES: return
    for tid, t in list(TRADES.items()):
        if t['status']!="OPEN": continue
        df=get_df(t['sym'])
        if df is None: continue
        price=float(df['Close'].iloc[-1]); sh=get_sheet()
        if (t['bias']=="BUY" and price>=t['tp1']) or (t['bias']=="SELL" and price<=t['tp1']):
            TRADES[tid]['status']="TP1_HIT"
            try:
                await context.bot.send_message(chat_id=t['chat_id'], text=f"✅ TP1 HIT {t['name']} {t['entry']:.2f}->{price:.2f}", reply_to_message_id=t['msg_id'])
                if sh: sh.update_cell(sh.find(tid).row, 9, "TP1_HIT")
            except: pass

async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V6.2 BLOCK FIX LIVE ✅\nAb Data ayega\n/signal btcusd\n/signal xauusd\n/weekly")

async def sig(update, context):
    arg=(context.args[0].lower() if context.args else "btcusd")
    sym,name=PAIRS.get(arg, ("BTC-USD","BTC"))
    await update.message.reply_text(f"{name} checking... direct API se")
    buf,data,bias=analyse(sym,name)
    if not buf:
        await update.message.reply_text(f"{name} Data nahi mila, 30 sec baad /signal {arg} try karo"); return
    entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
    tid=f"{name}_{int(datetime.now().timestamp())}"
    cap=f"{'🟢 '+bias if bias!='WAIT' else '🟡 WAIT'} {name} {entry:.2f}\nENTRY {entry:.2f}\nSL {sl:.2f}\nTP1 {tp1:.2f}\nTP2 {tp2:.2f}\n{data['reason']}\nID:{tid}"
    sent=await update.message.reply_photo(photo=buf, caption=cap)
    if bias!="WAIT":
        TRADES[tid]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"status":"OPEN","msg_id":sent.message_id,"chat_id":update.effective_chat.id}
        sh=get_sheet()
        if sh:
            try: sh.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", sent.message_id, update.effective_chat.id])
            except: pass

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh: await update.message.reply_text(f"Local Trades: {len(TRADES)}"); return
    vals=sh.get_all_values()
    if len(vals)<2: await update.message.reply_text("Sheet khali"); return
    rows=vals[1:]; total=len(rows)
    tp1=len([r for r in rows if len(r)>8 and "TP1" in r[8].upper()])
    sl=len([r for r in rows if len(r)>8 and "SL" in r[8].upper()])
    await update.message.reply_text(f"📊 WEEKLY\nTotal: {total}\n✅ TP: {tp1}\n❌ SL: {sl}\nOPEN: {total-tp1-sl}")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V6.2 Live")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

if __name__=="__main__":
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.job_queue.run_repeating(tp_checker, interval=300, first=30)
    print("V6.2 Starting")
    app.run_polling(drop_pending_updates=True)
