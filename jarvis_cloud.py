import os, threading, io, json
from datetime import datetime, timedelta
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

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis V5.2 BugFree Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

# --- BUG FIX 1: Saare alias add kiye ---
PAIRS = {
    "xauusd":("GC=F","GOLD"),"gold":("GC=F","GOLD"),"xau":("GC=F","GOLD"),
    "btcusd":("BTC-USD","BTC"),"btc":("BTC-USD","BTC"),"btc-usd":("BTC-USD","BTC"),
    "eurusd":("EURUSD=X","EURUSD"),"gbpusd":("GBPUSD=X","GBPUSD"),
    "usdjpy":("USDJPY=X","USDJPY"),"gbpjpy":("GBPJPY=X","GBPJPY"),
    "audusd":("AUDUSD=X","AUDUSD"),"usdcad":("USDCAD=X","USDCAD"),
    "us30":("^DJI","US30"),"nas100":("^IXIC","NAS100")
}
CRYPTO = ["BTC"]

def get_sheet():
    if not SHEET_ID or not GOOGLE_CREDS: return None
    try:
        creds_dict = json.loads(GOOGLE_CREDS)
        gc = gspread.service_account_from_dict(creds_dict)
        return gc.open_by_key(SHEET_ID).sheet1
    except Exception as e:
        print(f"Sheet Error: {e}"); return None

def get_df(sym):
    for per, inter in [("2d","15m"),("5d","1h"),("1mo","1d")]:
        try:
            df = yf.download(sym, period=per, interval=inter, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) > 40: return df
        except: pass
    return None

def is_session(name):
    # BUG FIX 2: Crypto 24x7
    if name in CRYPTO: return True
    return 13.5 <= datetime.now().hour + datetime.now().minute/60 <= 21.5

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, "WAIT", None
    close=df['Close']; price=float(close.iloc[-1])
    ema50=close.ewm(50).mean(); ema200=close.ewm(200).mean()
    delta=close.diff(); gain=delta.where(delta>0,0).rolling(14).mean(); loss=-delta.where(delta<0,0).rolling(14).mean(); rsi=100-(100/(1+gain/loss))
    rsi_val=float(rsi.iloc[-1]); e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    ah=float(close.iloc[-32:-8].max()); al=float(close.iloc[-32:-8].min())
    bias="WAIT"; reason=f"Price {price:.2f} inside {al:.2f}-{ah:.2f} RSI {rsi_val:.1f}"
    if price>ah and e50>e200 and rsi_val>55:
        bias="BUY"; reason=f"Breakout: Price>{ah:.2f} + EMA50>{e200:.2f} + RSI {rsi_val:.1f}>55"
    elif price<al and e50<e200 and rsi_val<45:
        bias="SELL"; reason=f"Breakdown: Price<{al:.2f} + EMA50<{e200:.2f} + RSI {rsi_val:.1f}<45"
    d=df[-40:].copy(); buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
    for i in range(len(d)):
        o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
        ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
    ax.plot(ema50[-40:].values,color='blue',lw=0.8,label='EMA50'); ax.plot(ema200[-40:].values,color='orange',lw=0.8,label='EMA200')
    ax.axhline(ah,color='green',ls='--',lw=0.8); ax.axhline(al,color='red',ls='--',lw=0.8)
    ax.set_title(f"{name} {price:.2f} | RSI {rsi_val:.1f} | {bias}"); ax.legend(fontsize=6)
    plt.tight_layout(); plt.savefig(buf,format='png',dpi=130); buf.seek(0); plt.close(fig)
    data={"price":price,"ah":ah,"al":al,"e50":e50,"e200":e200,"rsi":rsi_val,"reason":reason}
    return buf, bias, data

async def tp_checker(context: ContextTypes.DEFAULT_TYPE):
    if not TRADES: return
    for tid, t in list(TRADES.items()):
        if t['status'] not in ["OPEN","TP1_HIT"]: continue
        try:
            df=get_df(t['sym'])
            if df is None: continue
            price=float(df['Close'].iloc[-1]); sh=get_sheet()
            if t['bias']=="BUY":
                if price>=t['tp1'] and t['status']=="OPEN":
                    TRADES[tid]['status']="TP1_HIT"; TRADES[tid]['sl']=t['entry']
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"✅ TP1 HIT {t['name']} {t['entry']:.2f} -> {price:.2f}\n♻️ Breakeven: SL -> {t['entry']:.2f}", reply_to_message_id=t['msg_id'])
                    if sh:
                        try: cell=sh.find(tid); sh.update_cell(cell.row, 9, "TP1_HIT_BREAKEVEN")
                        except: pass
                elif price<=t['sl']:
                    TRADES[tid]['status']="SL_HIT"
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"❌ SL HIT {t['name']} {price:.2f}", reply_to_message_id=t['msg_id'])
                    if sh:
                        try: cell=sh.find(tid); sh.update_cell(cell.row, 9, "SL_HIT")
                        except: pass
            else:
                if price<=t['tp1'] and t['status']=="OPEN":
                    TRADES[tid]['status']="TP1_HIT"; TRADES[tid]['sl']=t['entry']
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"✅ TP1 HIT {t['name']} {t['entry']:.2f} -> {price:.2f}\n♻️ Breakeven ON", reply_to_message_id=t['msg_id'])
                    if sh:
                        try: cell=sh.find(tid); sh.update_cell(cell.row, 9, "TP1_HIT_BREAKEVEN")
                        except: pass
                elif price>=t['sl']:
                    TRADES[tid]['status']="SL_HIT"
                    await context.bot.send_message(chat_id=t['chat_id'], text=f"❌ SL HIT {t['name']} {price:.2f}", reply_to_message_id=t['msg_id'])
                    if sh:
                        try: cell=sh.find(tid); sh.update_cell(cell.row, 9, "SL_HIT")
                        except: pass
        except Exception as e: print(f"Checker err {e}")

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_IDS: return
    for _,(sym,name) in PAIRS.items():
        if not is_session(name): continue
        try:
            buf,bias,data=analyse(sym,name)
            if bias in ["BUY","SELL"] and buf:
                for cid in list(CHAT_IDS):
                    try:
                        buf.seek(0)
                        entry=data["price"]; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
                        await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f}\n{data['reason']}")
                    except: pass
        except: pass

async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V5.2 BugFree ON! ✅\nCrypto 24x7 ON\nBreakeven ON\n/signal btcusd | /signal xauusd | /weekly")

async def sig(update, context):
    arg=(context.args[0].lower().replace("/","") if context.args else "xauusd")
    sym,name=PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} checking...")
    buf,bias,data=analyse(sym,name)
    if not buf: await update.message.reply_text("Market band"); return
    entry=data["price"]; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
    trade_id=f"{name}_{int(datetime.now().timestamp())}"
    # Status dikhana fix kiya
    status_line = f"🟢 {bias} SIGNAL" if bias!="WAIT" else "🟡 WAIT - No Clear Trend"
    caption=f"{status_line}\n{name} | {entry:.2f}\nENTRY {entry:.2f}\nSL {sl:.2f}\nTP1 {tp1:.2f}\nTP2 {tp2:.2f}\nRR 1:2\nReason: {data['reason']}\nID: {trade_id}"
    sent=await update.message.reply_photo(photo=buf,caption=caption)
    if bias!="WAIT":
        TRADES[trade_id]={"sym":sym,"name":name,"bias":bias,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"status":"OPEN","msg_id":sent.message_id,"chat_id":update.effective_chat.id}
        sh=get_sheet()
        if sh:
            try: sh.append_row([trade_id, datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", "", sent.message_id, update.effective_chat.id])
            except Exception as e: print(e)

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh: await update.message.reply_text(f"Local: {len(TRADES)}"); return
    try:
        vals = sh.get_all_values()
        if len(vals) < 2: await update.message.reply_text("Sheet khali hai"); return
        header = [h.lower() for h in vals[0]]
        s_idx = header.index("status") if "status" in header else 8
        data_rows = vals[1:]
        total=len(data_rows)
        tp1=len([r for r in data_rows if len(r)>s_idx and "TP1" in r[s_idx].upper()])
        sl=len([r for r in data_rows if len(r)>s_idx and "SL" in r[s_idx].upper()])
        await update.message.reply_text(f"📊 Weekly\nTotal: {total}\n✅ TP1: {tp1}\n❌ SL: {sl}\nOPEN: {total-tp1-sl}")
    except Exception as e:
        await update.message.reply_text(f"Fix header Row1: trade_id,date,pair,bias,entry,sl,tp1,tp2,status,pnl,message_id,chat_id\nError:{e}")

if __name__=="__main__":
    import asyncio
    try: asyncio.get_event_loop()
    except: asyncio.set_event_loop(asyncio.new_event_loop())
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("signal",sig)); app.add_handler(CommandHandler("weekly",weekly_cmd))
    app.job_queue.run_repeating(tp_checker, interval=300, first=30)
    app.job_queue.run_repeating(auto_job, interval=900, first=60)
    print("Jarvis V5.2 Started"); app.run_polling()
