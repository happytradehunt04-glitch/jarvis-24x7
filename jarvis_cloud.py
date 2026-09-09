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
LAST_SIGNAL = {} # pair: time

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
    except Exception as e:
        print(e); return None

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
        sh=get_sheet(); ws=sh.worksheet("chats")
        vals=[int(r[0]) for r in ws.get_all_values() if r and r[0].isdigit()]
        if cid not in vals: ws.append_row([str(cid)])
    except: pass

def get_df(sym):
    if "BTC" in sym:
        try:
            data=requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=80", timeout=10).json()
            if isinstance(data, list) and len(data)>20:
                df=pd.DataFrame(data, columns=["ot","Open","High","Low","Close","vol","ct","qav","tr","tbav","tqav","i"])
                return df[["Open","High","Low","Close"]].astype(float)
        except: pass
    headers={"User-Agent":"Mozilla/5.0"}
    for rg, itv in [("1mo","1d"),("5d","1h")]:
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
        ema50=close.ewm(20).mean(); ema200=close.ewm(50).mean()
        delta=close.diff(); gain=delta.where(delta>0,0).rolling(14).mean(); loss=-delta.where(delta<0,0).rolling(14).mean()
        rsi=100-(100/(1+gain/loss)); rsi_val=float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50
        e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
        # Strict condition taaki duplicate na ho
        if e50>e200 and rsi_val>55: bias="BUY"
        elif e50<e200 and rsi_val<45: bias="SELL"
        else: bias="WAIT"
        d=df[-25:]; buf=io.BytesIO(); fig,ax=plt.subplots(figsize=(7,3.5))
        for i in range(len(d)):
            o=float(d['Open'].iloc[i]); h=float(d['High'].iloc[i]); l=float(d['Low'].iloc[i]); c=float(d['Close'].iloc[i]); col='green' if c>=o else 'red'
            ax.plot([i,i],[l,h],color=col,lw=0.8); ax.plot([i,i],[o,c],color=col,lw=3)
        ax.plot(ema50[-25:].values,color='blue',lw=0.8); ax.plot(ema200[-25:].values,color='orange',lw=0.8)
        ax.set_title(f"{name} {price:.2f} | {bias} RSI {rsi_val:.1f}"); plt.tight_layout(); plt.savefig(buf,format='png',dpi=120); buf.seek(0); plt.close(fig)
        return buf, {"price":price,"rsi":rsi_val}, bias
    except Exception as e:
        print(f"Analyse err {e}"); return None, None, "WAIT"

def is_market_open(name):
    wd=datetime.utcnow().weekday()
    if name=="BTC": return True
    return wd < 5

# ✅ FIX 1: Duplicate rokna - 4 ghante me ek hi pair ka ek hi signal
def can_send_signal(name, bias):
    key=f"{name}_{bias}"
    last=LAST_SIGNAL.get(key)
    if last and datetime.now() - last < timedelta(hours=4):
        return False
    LAST_SIGNAL[key]=datetime.now()
    return True

async def auto_job(context):
    if not CHAT_IDS: load_chats()
    if not CHAT_IDS: return
    wd=datetime.utcnow().weekday()
    print(f"Auto check {wd}")
    for _,(sym,name) in PAIRS.items():
        if not is_market_open(name): continue
        try:
            buf,data,bias=analyse(sym,name)
            if bias=="WAIT" or not buf: continue
            if not can_send_signal(name, bias):
                print(f"Skip duplicate {name} {bias}"); continue
            for cid in list(CHAT_IDS):
                try:
                    entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
                    buf.seek(0)
                    sent=await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f}\nRSI {data['rsi']:.1f}\nID:{name}_{int(datetime.now().timestamp())}")
                    sh=get_sheet()
                    if sh:
                        sh.sheet1.append_row([f"{name}_{int(datetime.now().timestamp())}_{cid}", datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", "", sent.message_id, cid])
                except Exception as e: print(e)
        except Exception as e: print(e)

# ✅ FIX 2: TP/SL Checker jo Sheet se padh ke update karega
async def tp_checker(context):
    try:
        sh=get_sheet()
        if not sh: return
        ws=sh.sheet1
        vals=ws.get_all_values()
        if len(vals)<2: return
        # Header index
        header=[h.lower() for h in vals[0]]
        try:
            idx_status=header.index("status"); idx_pair=header.index("pair"); idx_bias=header.index("bias")
            idx_entry=header.index("entry"); idx_sl=header.index("sl"); idx_tp1=header.index("tp1")
            idx_msg=header.index("message_id"); idx_chat=header.index("chat_id")
        except: return

        # Sirf OPEN wale check karo, last 50 rows hi
        for i, r in enumerate(vals[1:], start=2):
            if len(r)<=idx_status: continue
            if r[idx_status]!="OPEN": continue
            if i < len(vals)-50: continue # Purane 3000 rows skip, sirf latest 50 check
            try:
                pair=r[idx_pair]; bias=r[idx_bias]
                sym=PAIRS.get(pair.lower(), (None,None))[0]
                if not sym: continue
                entry=float(r[idx_entry]); sl=float(r[idx_sl]); tp1=float(r[idx_tp1])
                chat_id=int(r[idx_chat]) if r[idx_chat].isdigit() else None
                msg_id=int(r[idx_msg]) if len(r)>idx_msg and r[idx_msg].isdigit() else None
                if not chat_id: continue

                df=get_df(sym)
                if df is None: continue
                price=float(df['Close'].iloc[-1])

                hit=False; new_status="OPEN"; result=""
                if bias=="BUY":
                    if price>=tp1: new_status="TP1_HIT"; result=f"+0.8%"; hit=True
                    elif price<=sl: new_status="SL_HIT"; result=f"-0.4%"; hit=True
                else:
                    if price<=tp1: new_status="TP1_HIT"; result=f"+0.8%"; hit=True
                    elif price>=sl: new_status="SL_HIT"; result=f"-0.4%"; hit=True

                if hit:
                    ws.update_cell(i, idx_status+1, new_status)
                    ws.update_cell(i, 10, result) # result_pnl column J
                    try:
                        if msg_id:
                            await context.bot.send_message(chat_id=chat_id, text=f"{'✅' if 'TP' in new_status else '❌'} {new_status} {pair} {entry:.2f}->{price:.2f} {result}", reply_to_message_id=msg_id)
                    except: pass
                    print(f"Updated {pair} {new_status}")
            except Exception as e: print(f"TP row err {e}"); continue
    except Exception as e: print(f"TP checker err {e}")

async def start(update, context):
    save_chat(update.effective_chat.id)
    await update.message.reply_text("V7.2 SHEET FIX LIVE ✅\n✅ Duplicate band (4hr me 1 signal)\n✅ TP/SL auto update ON\n✅ Chat ID fix\n/weekly se result dekho\n/signal btcusd")

async def sig(update, context):
    try:
        arg=(context.args[0].lower() if context.args else "btcusd")
        sym,name=PAIRS.get(arg, ("BTC-USD","BTC"))
        save_chat(update.effective_chat.id)
        if not is_market_open(name):
            await update.message.reply_text(f"⚠️ {name} market band hai (Weekend)"); return
        await update.message.reply_text(f"{name} checking...")
        buf,data,bias=analyse(sym,name)
        if not buf: await update.message.reply_text("Data nahi mila"); return
        if bias=="WAIT":
            await update.message.reply_photo(photo=buf, caption=f"🟡 WAIT {name} {data['price']:.2f} RSI {data['rsi']:.1f} - No clear trend"); return
        entry=data['price']; sl=entry*0.996 if bias=="BUY" else entry*1.004; tp1=entry*1.008 if bias=="BUY" else entry*0.992; tp2=entry*1.015 if bias=="BUY" else entry*0.985
        sent=await update.message.reply_photo(photo=buf, caption=f"🟢 {bias} {name} {entry:.2f}\nSL {sl:.2f} TP1 {tp1:.2f} TP2 {tp2:.2f}")
        sh=get_sheet()
        if sh:
            sh.sheet1.append_row([f"{name}_{int(datetime.now().timestamp())}_{update.effective_chat.id}", datetime.now().strftime("%Y-%m-%d %H:%M"), name, bias, entry, sl, tp1, tp2, "OPEN", "", sent.message_id, update.effective_chat.id])
    except Exception as e: await update.message.reply_text(f"Error {e}")

async def weekly_cmd(update, context):
    sh=get_sheet()
    if not sh: return
    vals=sh.sheet1.get_all_values()
    if len(vals)<2: await update.message.reply_text("Sheet khali"); return
    rows=vals[1:]
    total=len(rows); tp=len([r for r in rows if len(r)>8 and "TP" in r[8]]); sl=len([r for r in rows if len(r)>8 and "SL" in r[8]]); open_t=total-tp-sl
    # Last 7 days
    win=int(tp/total*100) if total else 0
    await update.message.reply_text(f"📊 RESULT\nTotal: {total}\n✅ TP: {tp} ({win}%)\n❌ SL: {sl}\n🟡 OPEN: {open_t}\n\nNote: Purane 3000 OPEN ko ignore karo, ab se TP update hoga")

async def cleanup_cmd(update, context):
    # Admin: purane OPEN ko close karo
    if update.effective_chat.id!= 8796847869:
        await update.message.reply_text("Only admin"); return
    sh=get_sheet(); ws=sh.sheet1
    vals=ws.get_all_values()
    count=0
    for i, r in enumerate(vals[1:], start=2):
        if len(r)>8 and r[8]=="OPEN" and i < len(vals)-100: # 100 se purane
            ws.update_cell(i, 9, "EXPIRED")
            count+=1
            if count>50: break # ek baar me 50 hi
    await update.message.reply_text(f"Cleaned {count} old OPEN to EXPIRED")

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V7.2 Fix Live")
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
    app.job_queue.run_repeating(auto_job, interval=600, first=30) # 10 min
    app.job_queue.run_repeating(tp_checker, interval=300, first=60) # 5 min TP check
    print("V7.2 Starting")
    app.run_polling(drop_pending_updates=True)
