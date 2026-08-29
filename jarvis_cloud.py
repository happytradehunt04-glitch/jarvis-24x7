import os, threading, io
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = set()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis V4 Pro Candle Live")
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

def is_session(): return 13.5 <= datetime.now().hour + datetime.now().minute/60 <= 21.5

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
        bias="BUY"; reason=f"✅ Asian High {ah:.2f} BREAKOUT\n✅ EMA50 {e50:.2f} > EMA200 Bullish\n✅ RSI {rsi_val:.1f} Strong"
    elif price < al and e50 < e200 and rsi_val < 45:
        bias="SELL"; reason=f"✅ Asian Low {al:.2f} BREAKDOWN\n✅ EMA50 {e50:.2f} < EMA200 Bearish\n✅ RSI {rsi_val:.1f} Weak"
    else:
        reason=f"❌ Range me: {al:.2f} - {ah:.2f}\n❌ EMA {e50:.2f}/{e200:.2f}\n❌ RSI {rsi_val:.1f} Neutral"

    # --- PRO CANDLE CHART (Bina mplfinance ke) ---
    d = df[-40:].copy()
    buf = io.BytesIO()
    fig, ax = plt.subplots(figsize=(7,3.5))
    # Candle banane ka logic
    for i in range(len(d)):
        o = float(d['Open'].iloc[i]); h = float(d['High'].iloc[i]); l = float(d['Low'].iloc[i]); c = float(d['Close'].iloc[i])
        color = 'green' if c >= o else 'red'
        ax.plot([i, i], [l, h], color=color, linewidth=0.8)
        ax.plot([i, i], [o, c], color=color, linewidth=3)
    ax.plot(ema50[-40:].values, color='blue', lw=0.8, label='EMA50')
    ax.plot(ema200[-40:].values, color='orange', lw=0.8, label='EMA200')
    ax.axhline(ah, color='green', ls='--', lw=0.8); ax.axhline(al, color='red', ls='--', lw=0.8)
    ax.set_title(f"{name} {price:.2f} | RSI {rsi_val:.1f} | {bias}"); ax.legend(fontsize=6)
    plt.tight_layout(); plt.savefig(buf, format='png', dpi=150); buf.seek(0); plt.close(fig)

    if bias=="WAIT":
        msg=f"**{name} | {price:.2f} | {bias}**\n\n{reason}\n\nAH: {ah:.2f} | AL: {al:.2f}\nAction: WAIT"
    else:
        sl=price*0.996 if bias=="BUY" else price*1.004; tp1=price*1.008 if bias=="BUY" else price*0.992; tp2=price*1.015 if bias=="BUY" else price*0.985
        msg=f"""**🚨 {bias} {name} | {price:.2f}**
ENTRY: {price:.2f}
SL: {sl:.2f}
TP1: {tp1:.2f}
TP2: {tp2:.2f}
RR 1:2

**Reason:**
{reason}

**Chart:** Pro Candle + EMA
**Act as:** London Breakout Trader
"""
    return buf, msg, bias

async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V4 Pro Candle ON! ✅\nCandle chart ayega\nAuto 1:30-9:30PM ON\n/signal xauusd")
async def pairs_cmd(update, context): await update.message.reply_text("Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")
async def sig(update, context):
    arg = (context.args[0].lower() if context.args else "xauusd")
    sym, name = PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} Pro Candle...")
    buf, msg, _ = analyse(sym, name)
    if buf: await update.message.reply_photo(photo=buf, caption=msg, parse_mode='Markdown')
    else: await update.message.reply_text("Market band")

async def auto_job(context):
    if not is_session() or not CHAT_IDS: return
    for _, (sym, name) in PAIRS.items():
        try:
            buf, msg, bias = analyse(sym, name)
            if bias in ["BUY","SELL"]:
                for cid in list(CHAT_IDS):
                    try: await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO {msg}", parse_mode='Markdown')
                    except: pass
        except: pass

if __name__ == "__main__":
    import asyncio
    try: asyncio.get_event_loop()
    except: asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    app.job_queue.run_repeating(auto_job, interval=900, first=15)
    print("Jarvis V4 Started")
    app.run_polling()
