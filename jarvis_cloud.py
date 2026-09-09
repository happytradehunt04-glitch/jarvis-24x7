import os, threading, io, json, time, requests
from datetime import datetime, timedelta
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
LAST_SIGNAL_TIME = {}

PAIRS = {"xauusd": ("GC=F", "GOLD"), "btcusd": ("BTC-USD", "BTC"), "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD")}

def get_sheet():
    try:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
        return gc.open_by_key(SHEET_ID)
    except: return None

def load_chats():
    try:
        sh=get_sheet(); ws=sh.worksheet("chats")
        for r in ws.get_all_values():
            if r and r[0].isdigit(): CHAT_IDS.add(int(r[0]))
    except: pass

def save_chat(cid):
    CHAT_IDS.add(cid)
    try:
        sh=get_sheet(); ws=sh.worksheet("chats")
        if str(cid) not in [r[0] for r in ws.get_all_values()]: ws.append_row([str(cid)])
    except: pass

def get_df(sym):
    try:
        if "BTC" in sym:
            data=requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=80", timeout=10).json()
            if isinstance(data, list) and len(data)>20:
                df=pd.DataFrame(data, columns=["ot","Open","High","Low","Close","vol","ct","qav","tr","tbav","tqav","i"])
                return df[["Open","High","Low","Close"]].astype(float)
    except: pass
    headers={"User-Agent":"Mozilla/5.0"}
    for rg, itv in [("1mo","1d")]:
        try:
            url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rg}&interval={itv}"
            r=requests.get(url, headers=headers, timeout=15).json()
            q=r['chart']['result'][0]['indicators']['quote'][0]
            df=pd.DataFrame({"Open":q['open'],"High":q['high'],"Low":q['low'],"Close":q['close']}).dropna()
            if len(df)>10: return df
        except: continue
    return None

def analyse(sym, name):
    try:
        df=get_df(sym)
        if df is None: return None, None, "WAIT"
        close=df['Close']; price=float(close.iloc[-1])
        ema20=close.ewm(20).mean(); ema50=close.ewm(50).mean()
        e20=float(ema20.iloc[-1]); e50=float(ema50.iloc[-1])
        bias="BUY" if e20>e50 else "SELL" if e20<e50 else "WAIT"
        # Strict
        if abs(e20-e50)/price < 0.001: bias="WAIT"
        d=df[-25:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
        for i in range(len(d)):
            o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
            ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
        ax.set_title(f"{name} {price:.2f} | {bias}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=120); buf.seek(0); plt.close(fig)
        return buf, {"price":price}, bias
    except: return None, None, "WAIT"

def is_market_open(name):
    wd=datetime.utcnow().weekday()
    if name=="BTC": return True
    return wd < 5 # Mon-Fri

def can_send(name, bias):
    key=f"{name}_{bias}"
    last=LAST_SIGNAL_TIME.get(key)
    if last and datetime.now() - last < timedelta(hours=6): return False
    LAST_SIGNAL_TIME[key]=datetime.now()
    return True

async def auto_job(context):
    if not CHAT_IDS: load_chats()
    for _,(sym,name) in PAIRS.items():
        if not is_market_open(name): continue
        try:
            buf,data,bias=analyse(sym,name)
            if bias=="WAIT" or not buf: continue
            if not can_send(name,bias): continue
            for cid in list(CHAT_IDS):
                try:
                    entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
                    buf.seek(0)
                    sent=await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f}")
                    sh=get_sheet()
                    if sh: sh.sheet1.append_row([f"{name}_{int(time.time())}", datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, entry*1.015, "OPEN", "", sent.message_id, cid])
                except: pass
        except: pass

async def tp_checker(context):
    try:
        sh=get_sheet(); ws=sh.sheet1; vals=ws.get_all_values()
        if len(vals)<2: return
        # Sirf last 100 check
        for i in range(max(2, len(vals)-100), len(vals)+1):
            r=vals[i-1]
            if len(r)<9 or r[8]!="OPEN": continue
            try:
                pair=r[2]; bias=r[3]; sym=PAIRS.get(pair.lower(),(None,None))[0]
                if not sym: continue
                entry=float(r[4]); sl=float(r[5]); tp1=float(r[6])
                df=get_df(sym)
                if df is None: continue
                price=float(df['Close'].iloc[-1])
                new=None
                if bias=="BUY" and price>=tp1: new="TP1_HIT"
                elif bias=="BUY" and price<=sl: new="SL_HIT"
                elif bias=="SELL" and price<=tp1: new="TP1_HIT"
                elif bias=="SELL" and price>=sl: new="SL_HIT"
                if new: ws.update_cell(i, 9, new)
            except: continue
    except: pass

async def start(update, context):
    save_chat(update.effective_chat.id)
    await update.message.reply_text("V8 FIX LIVE ✅\n/cleanup - 1 baar me saaf\n/weekly - result\n/signal btcusd")

async def sig(update, context):
    try:
        arg=(context.args[0].lower() if context.args else "btcusd")
        sym,name=PAIRS.get(arg, ("BTC-USD","BTC"))
        save_chat(update.effective_chat.id)
        if not is_market_open(name):
            await update.message.reply_text(f"⚠️ {name} Weekend band"); return
        await update.message.reply_text(f"{name} checking...")
        buf,data,bias=analyse(sym,name)
        if not buf: await update.message.reply_text("Data nahi mila"); return
        if bias=="WAIT":
            await update.message.reply_photo(photo=buf, caption=f"WAIT {name} {data['price']:.2f}"); return
        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992
        sent=await update.message.reply_photo(photo=buf, caption=f"{bias} {name} {entry:.2f}")
        sh=get_sheet()
        if sh: sh.sheet1.append_row([f"{name}_{int(time.time())}", datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, entry*1.015, "OPEN", "", sent.message_id, update.effective_chat.id])
    except Exception as e: await update.message.reply_text(f"Error {e}")

async def weekly_cmd(update, context):
    sh=get_sheet(); vals=sh.sheet1.get_all_values()
    if len(vals)<2: await update.message.reply_text("Sheet khali"); return
    rows=vals[1:]
    total=len(rows); open_r=len([r for r in rows if len(r)>8 and r[8]=="OPEN"]); tp=len([r for r in rows if len(r)>8 and "TP" in r[8]]); sl=len([r for r in rows if len(r)>8 and "SL" in r[8]])
    await update.message.reply_text(f"📊 RESULT\nTotal: {total}\n✅ TP: {tp}\n❌ SL: {sl}\n🟡 OPEN: {open_r}")

# ✅ Yahi tha main bug - ab fix
async def cleanup_cmd(update, context):
    try:
        await update.message.reply_text("🧹 DELETE MODE: Purane 3000 rows ek baar me delete ho rahe hai...")
        sh=get_sheet(); ws=sh.sheet1
        vals=ws.get_all_values()
        if len(vals)<=80:
            await update.message.reply_text(f"Already clean Total: {len(vals)-1}"); return

        header=vals[0]
        keep=vals[-80:] # last 80 rakho

        # Clear aur naya likho
        ws.clear()
        time.sleep(2)
        ws.append_row(header)
        time.sleep(1)
        # Batch append
        ws.append_rows(keep)

        # Naya count fetch
        time.sleep(2)
        new_vals=ws.get_all_values()
        await update.message.reply_text(f"✅ DELETE DONE!\nPehle: 3346\nAb: {len(new_vals)-1}\nOPEN: {len([r for r in new_vals if len(r)>8 and r[8]=='OPEN'])}\n\nAb /weekly sahi dikhayega")
    except Exception as e:
        await update.message.reply_text(f"Error {e}\nManual: Sheet khol ke Row 2 se 3260 tak delete kar do")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V8 Live")
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
    app.add_handler(CommandHandler("cleanup", cleanup_cmd))
    app.job_queue.run_repeating(auto_job, interval=3600, first=30) # ✅ 10 min se 1 hour kar diya taaki spam na ho
    app.job_queue.run_repeating(tp_checker, interval=300, first=60)
    print("V8 Starting")
    app.run_polling(drop_pending_updates=True)
