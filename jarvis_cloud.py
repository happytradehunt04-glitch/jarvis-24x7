import os, threading, io, asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf
import pandas as pd
import mplfinance as mpf
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = set() # Jinhone /start kiya unko auto alert jayega

# --- Web Server for Render ---
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis V3 Auto Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

PAIRS = {
    "xauusd": ("GC=F", "GOLD"), "gold": ("GC=F", "GOLD"),
    "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"), "gbpjpy": ("GBPJPY=X", "GBPJPY"),
    "audusd": ("AUDUSD=X", "AUDUSD"), "usdcad": ("USDCAD=X", "USDCAD"),
    "us30": ("^DJI", "US30"), "nas100": ("^IXIC", "NAS100"), "btc": ("BTC-USD", "BTC")
}

def get_df(sym):
    for per, inter in [("2d","15m"), ("5d","1h"), ("1mo","1d")]:
        try:
            df = yf.download(sym, period=per, interval=inter, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) > 40: return df
        except: pass
    return None

def is_session():
    h = datetime.now().hour + datetime.now().minute/60
    # IST me 13:30 - 21:30 (London + NY Overlap) -> isme hi auto alert
    return 13.5 <= h <= 21.5

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, None, "WAIT"
    close = df['Close']; price = float(close.iloc[-1])
    ema50 = close.ewm(50).mean(); ema200 = close.ewm(200).mean()
    delta = close.diff(); gain = delta.where(delta>0,0).rolling(14).mean(); loss = -delta.where(delta<0,0).rolling(14).mean(); rsi = 100 - (100/(1+gain/loss))
    rsi_val = float(rsi.iloc[-1]); e50 = float(ema50.iloc[-1]); e200 = float(ema200.iloc[-1])
    asian = close.iloc[-32:-8]; ah = float(asian.max()); al = float(asian.min())

    bias="WAIT"; reason=""
    if price > ah and e50 > e200 and rsi_val > 55:
        bias="BUY"; reason=f"Asian High {ah:.2f} BREAKOUT | EMA Bull | RSI {rsi_val:.1f}"
    elif price < al and e50 < e200 and rsi_val < 45:
        bias="SELL"; reason=f"Asian Low {al:.2f} BREAKDOWN | EMA Bear | RSI {rsi_val:.1f}"

    df_plot = df[-60:].copy()
    ap = [mpf.make_addplot(ema50[-60:], color='blue'), mpf.make_addplot(ema200[-60:], color='orange')]
    buf = io.BytesIO()
    mpf.plot(df_plot, type='candle', style='yahoo', addplot=ap, title=f"{name} {price:.2f} RSI:{rsi_val:.1f}", ylabel='Price', savefig=dict(fname=buf, dpi=120, bbox_inches='tight'))
    buf.seek(0)

    sl_p=0.004; tp1_p=0.008; tp2_p=0.015
    if bias=="BUY":
        sl=price*(1-sl_p); tp1=price*(1+tp1_p); tp2=price*(1+tp2_p)
    else:
        sl=price*(1+sl_p); tp1=price*(1-tp1_p); tp2=price*(1-tp2_p)

    if bias=="WAIT":
        msg=f"**{name} | {price:.2f} | RSI {rsi_val:.1f}**\nWAIT - No Breakout\nHigh: {ah:.2f} Low: {al:.2f}\nAct as: London Breakout Trader"
    else:
        msg=f"""**🚨 AUTO {bias} {name} | {price:.2f}**

**ENTRY: {price:.2f}**
**SL: {sl:.2f}**
**TP1: {tp1:.2f}**
**TP2: {tp2:.2f}**
**RR: 1:2**

**Reason:** {reason}
**Lot:** 0.01 for $50-$100 (1% Risk)
**Time:** {datetime.now().strftime('%I:%M %p IST')}

**Act as:** Professional London Breakout Trader
Edu Only.
"""
    return buf, msg, bias

# --- Commands ---
async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V3 AUTO ON! ✅\n\nAb 1:30PM - 9:30PM IST me har 15min me auto breakout alert ayega.\nTujhe /signal likhne ki zarurat nahi.\n\n/pairs - list\n/signal xauusd - manual\n/lot 50 - lot calc\n/status - session")

async def pairs_cmd(update, context): await update.message.reply_text("10 Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")
async def lot_cmd(update, context):
    try:
        bal=float(context.args[0]) if context.args else 100
        await update.message.reply_text(f"Acc: ${bal}\nRisk 1% = ${bal*0.01}\nLot: 0.01 (20 pips SL) - $50-$100 pe 0.01 hi rakh")
    except: await update.message.reply_text("Use: /lot 50")
async def status_cmd(update, context):
    await update.message.reply_text(f"Time: {datetime.now().strftime('%I:%M %p IST')}\nLondon Session (1:30-9:30PM): {is_session()}\nAuto Scan: ON (15 min)\nChats registered: {len(CHAT_IDS)}")
async def sig(update, context):
    arg = (context.args[0].lower() if context.args else "xauusd")
    sym, name = PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} manual check...")
    buf, msg, _ = analyse(sym, name)
    if buf: await update.message.reply_photo(photo=buf, caption=msg, parse_mode='Markdown')
    else: await update.message.reply_text("Market band hai")

# --- AUTO JOB ---
async def auto_scanner(app: Application):
    if not is_session(): return
    if not CHAT_IDS: return
    print(f"Auto Scan {datetime.now()} for {len(CHAT_IDS)} chats")
    for key, (sym, name) in PAIRS.items():
        try:
            buf, msg, bias = analyse(sym, name)
            if bias in ["BUY","SELL"]:
                for cid in list(CHAT_IDS):
                    try: await app.bot.send_photo(chat_id=cid, photo=buf, caption=msg, parse_mode='Markdown')
                    except: pass
        except Exception as e: print(f"Scan err {name} {e}")

if __name__ == "__main__":
    try: asyncio.get_event_loop()
    except: asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    app.add_handler(CommandHandler("lot", lot_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(auto_scanner(app)), 'interval', minutes=15)
    scheduler.start()

    print("Jarvis V3 Auto Started")
    app.run_polling()
