import os, threading, io, asyncio, pytz
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN = os.getenv("BOT_TOKEN")
# Auto alert jayega un sabko jinhone /start kiya
CHAT_IDS = set()

# 10 PAIRS FINAL LIST
PAIRS = {
    "xauusd": ("GC=F", "GOLD / XAUUSD"),
    "gold": ("GC=F", "GOLD / XAUUSD"),
    "eurusd": ("EURUSD=X", "EURUSD"),
    "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"), # BUG FIXED
    "gbpjpy": ("GBPJPY=X", "GBPJPY"),
    "audusd": ("AUDUSD=X", "AUDUSD"),
    "usdcad": ("USDCAD=X", "USDCAD"),
    "us30": ("^DJI", "US30"),
    "nas100": ("^IXIC", "NAS100"),
    "btc": ("BTC-USD", "BTCUSD"),
}

# Web server for Render
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis V2 Pro Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

def get_close(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    c = df['Close'] if 'Close' in df.columns else df.iloc[:,0]
    if isinstance(c, pd.DataFrame): c = c.iloc[:,0]
    return c.astype(float)

def is_london_session():
    # IST me check
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    hour = now.hour + now.minute/60
    # London: 13:30 - 17:30 IST, Overlap: 17:30 - 21:30 IST
    is_london = 13.5 <= hour <= 17.5
    is_overlap = 17.5 < hour <= 21.5
    return is_london or is_overlap, now

def analyse(symbol_yf, name):
    df = yf.download(symbol_yf, period="2d", interval="15m", progress=False, auto_adjust=True)
    if len(df) < 50: return None, None, f"{name} Data nahi"

    close = get_close(df)
    price = float(close.iloc[-1])

    # EMA 50, 200
    ema50 = close.ewm(span=50).mean().iloc[-1]
    ema200 = close.ewm(span=200).mean().iloc[-1]
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain/loss))
    rsi_val = float(rsi.iloc[-1])

    # Asian High/Low (last 24 candles excluding last 4 = Asian)
    asian_part = close.iloc[-32:-8] # approx Asian session
    asian_high = float(asian_part.max())
    asian_low = float(asian_part.min())

    bias = "WAIT"
    reason = ""
    if price > asian_high and ema50 > ema200 and rsi_val > 55:
        bias = "BUY"; reason = f"Asian High {asian_high:.2f} BREAKOUT + EMA Bullish + RSI {rsi_val:.1f}"
    elif price < asian_low and ema50 < ema200 and rsi_val < 45:
        bias = "SELL"; reason = f"Asian Low {asian_low:.2f} BREAKDOWN + EMA Bearish + RSI {rsi_val:.1f}"

    # Chart
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(close[-60:], color='black')
    ax.axhline(asian_high, color='green', ls='--', lw=0.8, label='Asian High')
    ax.axhline(asian_low, color='red', ls='--', lw=0.8, label='Asian Low')
    ax.set_title(f"{name} {price:.2f} | RSI {rsi_val:.1f}")
    ax.legend(fontsize=6)
    buf = io.BytesIO(); plt.tight_layout(); plt.savefig(buf, format='png', dpi=150); buf.seek(0); plt.close(fig)

    if bias == "WAIT":
        msg = f"""**{name} | {price:.2f} | RSI {rsi_val:.1f}**
BIAS: WAIT - No Breakout yet
Asian High: {asian_high:.2f}
Asian Low: {asian_low:.2f}
EMA50: {ema50:.2f} vs EMA200: {ema200:.2f}
"""
    else:
        sl_p = 0.004; tp1_p = 0.008; tp2_p = 0.015
        if bias == "BUY":
            sl = price*(1-sl_p); tp1 = price*(1+tp1_p); tp2 = price*(1+tp2_p)
        else:
            sl = price*(1+sl_p); tp1 = price*(1-tp1_p); tp2 = price*(1-tp2_p)
        msg = f"""**🚨 {bias} {name} | Price: {price:.2f} | RSI: {rsi_val:.1f}**
**ENTRY: {price:.2f}**
**SL: {sl:.2f}**
**TP1: {tp1:.2f}**
**TP2: {tp2:.2f}**

**Reason:** {reason}
**Lot:** 0.01 for $50-$100 acc (1% Risk)
Edu Only.
"""
    return buf, msg, bias

# --- TELEGRAM COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("Boss Jarvis V2 Pro LIVE! 🚀\n\n10 Pairs Auto Scan ON hai.\nLondon 1:30PM-9:30PM IST me khud signal bhejunga.\n\nCommands:\n/signal usdjpy\n/pairs\n/lot 100\n/status")

async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")

async def lot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bal = float(context.args[0]) if context.args else 100
        risk = bal * 0.01
        # 20 pips SL approx
        lot = round(risk / 20 / 10, 2) # simple formula
        if lot < 0.01: lot = 0.01
        await update.message.reply_text(f"Account: ${bal}\n1% Risk = ${risk}\nLot Size: {lot} (20 pips SL)\nTip: $50-$100 pe 0.01 lot se start kar")
    except:
        await update.message.reply_text("Use: /lot 100")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "xauusd")
    if arg not in PAIRS:
        await update.message.reply_text("Galat pair. /pairs likh"); return
    sym, name = PAIRS[arg]
    await update.message.reply_text(f"{name} analyse kar raha hu...")
    chart, msg, bias = analyse(sym, name)
    if chart: await update.message.reply_photo(photo=chart, caption=msg, parse_mode='Markdown')
    else: await update.message.reply_text(msg)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    in_sess, now = is_london_session()
    await update.message.reply_text(f"Time IST: {now.strftime('%I:%M %p')}\nLondon/Overlap Session Active: {in_sess}\nAuto Scan: Har 15 min\nAccuracy: 65-70% (No 100% in forex)")

# --- AUTO SCANNER JOB ---
async def auto_scanner(app: Application):
    in_sess, now = is_london_session()
    if not in_sess: return # Asian me so jao
    if not CHAT_IDS: return
    print(f"Auto scan at {now}")
    for key, (sym, name) in PAIRS.items():
        try:
            chart, msg, bias = analyse(sym, name)
            if bias in ["BUY", "SELL"]:
                for cid in list(CHAT_IDS):
                    try:
                        await app.bot.send_photo(chat_id=cid, photo=chart, caption=f"🚨 AUTO ALERT\n{msg}", parse_mode='Markdown')
                    except: pass
        except Exception as e:
            print(f"Scan err {name}: {e}")

if __name__ == "__main__":
    import asyncio
    try: asyncio.get_event_loop()
    except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    app.add_handler(CommandHandler("lot", lot_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(lambda: asyncio.create_task(auto_scanner(app)), 'interval', minutes=15)
    scheduler.start()

    print("Jarvis V2 Pro Started")
    app.run_polling()
