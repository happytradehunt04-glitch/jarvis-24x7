import os, threading, io, json, time
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
        creds = json.loads(GOOGLE_CREDS)
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(SHEET_ID).sheet1
        vals = sh.get_all_values()
        if not vals or vals[0][0].lower()!= "trade_id":
            sh.clear()
            sh.append_row(["trade_id","date","pair","bias","entry","sl","tp1","tp2","status","pnl","message_id","chat_id"])
        return sh
    except Exception as e:
        print(f"Sheet err {e}"); return None

# ✅ FIX 1: Order change + Yahoo block fix
def get_df(sym):
    for attempt in range(2):
        for per, inter in [("1mo","1d"), ("5d","1h"), ("2d","15m")]: # Pehle Daily data lo
            try:
                df = yf.download(sym, period=per, interval=inter, progress=False, auto_adjust=True, threads=False)
                if df is None or df.empty: continue
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                if len(df) > 30: return df
            except: continue
        time.sleep(1)
    return None

def is_session(name):
    if name in CRYPTO: return True
    return 13.5 <= datetime.now().hour + datetime.now().minute/60 <= 21.5

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, None, "WAIT", None
    close=df['Close']; price=float(close.iloc[-1])
    ema50=close.ewm(50).mean(); ema200=close.ewm(200).mean()
    delta=close.diff(); gain=delta.where(delta>0,0).rolling(14).mean(); loss=-delta.where(delta<0,0).rolling(14).mean(); rsi=100-(100/(1+gain/loss))
    rsi_val=float(rsi.iloc[-1]); e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    ah=float(close.iloc[-32:-8].max()); al=float(close.iloc[-32:-8].min())
    bias="WAIT"; reason=f"Range {al:.2f}-{ah:.2f} RSI {rsi_val:.1f}"
    if price>ah and e50>e200 and rsi_val>55: bias="BUY"; reason=f"Breakout AH {ah:.2f} + EMA Bull + RSI {rsi_val:.1f}"
    elif price<al and e50<e200 and rsi_val<45: bias="SELL"; reason=f"Breakdown AL {al:.2f} + EMA Bear + RSI {rsi_val:.1f}"
    d=df[-40:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
    for i in range(len(d)):
        o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
        ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
    ax.plot(ema50[-40:].values,color='blue',lw=0.8); ax.plot(ema200[-40:].values,color='orange',lw=0.8)
    ax.axhline(ah,color='green',ls='--',lw=0.8); ax.axhline(al,color='red',ls='--',lw=0.8)
    ax.set_title(f"{name} {price:.2f} | {bias}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=130); buf.seek(0); plt.close(fig)
    return buf, {"price":price,"reason":reason}, bias, df

async def tp_checker(context: ContextTypes.DEFAULT_TYPE):
    if not TRADES: return
    for tid, t in list(TRADES.items()):
        if t['status']!="OPEN": continue
        df=get_df(t['sym'])
        if df is None: continue
        price=float(df['Close'].iloc[-1]); sh=get_sheet()
        if t['bias']=="BUY":
            if price>=t['tp1']:
                TRADES[tid]['status']="TP1_HIT"
                try:
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"✅ TP1 HIT {t['name']}\nEntry {t['entry']:.2f} -> {price:.2f}\n♻️ Breakeven ON", reply_to_message_id=t['msg_id'])
                    if sh: sh.update_cell(sh.find(tid).row, 9, "TP1_HIT")
                except: pass
            elif price<=t['sl']:
                TRADES[tid]['status']="SL_HIT"
                try:
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"❌ SL HIT {t['name']} {price:.2f}", reply_to_message_id=t['msg_id'])
                    if sh: sh.update_cell(sh.find(tid).row, 9, "SL_HIT")
                except: pass
        else:
            if price<=t['tp1']:
                TRADES[tid]['status']="TP1_HIT"
                try:
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"✅ TP1 HIT {t['name']}\nEntry {t['entry']:.2f} -> {price:.2f}", reply_to_message_id=t['msg_id'])
                    if sh: sh.update_cell(sh.find(tid).row, 9, "TP1_HIT")
                except: pass
            elif price>=t['sl']:
                TRADES[tid]['status']="SL_HIT"
                try:
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"❌ SL HIT {t['name']} {price:.2f}", reply_to_message_id=t['msg_id'])
                    if sh: sh.update_cell(sh.find(tid).row, 9, "SL_HIT")
                except: pass

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_IDS: return
    for _,(sym,name) in PAIRS.items():
        if not is_session(name): continue
        try:
            buf,data,bias,_=analyse(sym,name)
            if bias in ["BUY","SELL"] and buf:
                for cid in list(CHAT_IDS):
                    try:
                        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
                        buf.seek(0)
                        sent=await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f}\n{data['reason']}")
                        tid=f"{name}_{int(datetime.now().timestamp())}_{cid}"
                        TRADES[tid]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"status":"OPEN","msg_id":sent.message_id,"chat_id":cid}
                        sh=get_sheet()
                        if sh: sh.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, entry*1.015, "OPEN", "", sent.message_id, cid])
                    except: pass
        except: pass

async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V6.1 FIXED LIVE ✅\n✅ Result Track ON\n✅ TP Hit Reply ON\n✅ Weekly ON\n✅ Yahoo Fix ON\n\n/signal btcusd\n/signal xauusd\n/weekly")

async def sig(update, context):
    arg=(context.args[0].lower() if context.args else "xauusd")
    sym,name=PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} checking...")
    buf,data,bias,_=analyse(sym,name)
    if not buf:
        await update.message.reply_text(f"{name} Data nahi mila, 1 min baad try karo."); return
    entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
    tid=f"{name}_{int(datetime.now().timestamp())}"
    caption=f"{'🟢 '+bias if bias!='WAIT' else '🟡 WAIT'} {name} {entry:.2f}\nENTRY {entry:.2f}\nSL {sl:.2f}\nTP1 {tp1:.2f}\nTP2 {tp2:.2f}\n{data['reason']}\nID:{tid}"
    sent=await update.message.reply_photo(photo=buf, caption=caption)
    if bias!="WAIT":
        TRADES[tid]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"status":"OPEN","msg_id":sent.message_id,"chat_id":update.effective_chat.id}
        sh=get_sheet()
        if sh:
            try: sh.append_row([tid, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", "", sent.message_id, update.effective_chat.id])
            except Exception as e: print(e)

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh:
        total=len(TRADES); tp=len([t for t in TRADES.values() if "TP1" in t['status']]); sl=len([t for t in TRADES.values() if "SL" in t['status']])
        await update.message.reply_text(f"📊 Weekly (Local)\nTotal: {total}\n✅ TP: {tp}\n❌ SL: {sl}"); return
    try:
        vals=sh.get_all_values()
        if len(vals)<2: await update.message.reply_text("Sheet khali"); return
        header=[h.lower() for h in vals[0]]; s_idx=header.index("status") if "status" in header else 8
        rows=vals[1:]; total=len(rows)
        tp1=len([r for r in rows if len(r)>s_idx and "TP1" in r[s_idx].upper()])
        sl=len([r for r in rows if len(r)>s_idx and "SL" in r[s_idx].upper()])
        win=int(tp1/total*100) if total else 0
        await update.message.reply_text(f"📊 WEEKLY\nTotal: {total}\n✅ TP: {tp1} ({win}%)\n❌ SL: {sl}\n🟡 OPEN: {total-tp1-sl}")
    except Exception as e: await update.message.reply_text(f"Error {e}")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Jarvis V6.1 Live")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

if __name__=="__main__":
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("result", weekly_cmd))
    app.job_queue.run_repeating(tp_checker, interval=300, first=30)
    app.job_queue.run_repeating(auto_job, interval=900, first=60)
    print("Jarvis V6.1 Starting")
    app.run_polling(drop_pending_updates=True)
