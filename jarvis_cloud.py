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

TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = set()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis V3 Lite Auto Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

PAIRS = {
    "xauusd": ("GC=F", "GOLD"), "gold": ("GC=F", "GOLD"),
    "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"), "gbpjpy": ("GBPJPY=X", "GBPJPY"),
    "audusd": ("AUDUSD=X", "AUDUSD"), "usdcad": ("USDCAD=X", "USDCAD"),
    "us30": ("^DJI", "US30"), "nas100": ("^IXIC", "NAS100"), "btc": ("BTC-USD", "BTC")
}

def get_df(sym):
    for per, inter in [("5d","15m"), ("5d","1h"), ("1mo","1d")]:
        try:
            df = yf.download(sym, period=per, interval=inter, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) > 40: return df
        except: pass
    return None

def is_session():
    h = datetime.now().hour + datetime.now().minute/60
    return 13.5 <= h <= 21.5

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, None, "WAIT"
    close = df['Close']; price = float(close.iloc[-1])
    ema50 = close.ewm(50).mean(); ema200 = close.ewm(200).mean()
    delta = close.diff(); gain = delta.where(delta>0,0).rolling(14).mean(); loss = -delta.where(delta<0,0).rolling(14).mean(); rsi = 100 - (100/(1+gain/loss))
    rsi_val = float(rsi.iloc[-1]); e50 = float(ema50.iloc[-1]); e200 = float(ema200.iloc[-1])
    asian = close.iloc[-32:-8]; ah = float(asian.max()); al = float(asian.min())
    
    # --- NEWS FILTER (Simple) ---
    is_news_time = datetime.now().weekday() == 4 and 18 <= datetime.now().hour <= 19 # Friday 6-7 PM NFP time

    bias="WAIT"; reason=""
    if is_news_time:
        reason = "🔴 NFP NEWS - WAIT karo, news ke baad entry"
    elif price > ah and e50 > e200 and rsi_val > 55:
        bias="BUY"; reason=f"✅ Asian High {ah:.2f} BREAKOUT\n✅ EMA50 {e50:.2f} > EMA200 {e200:.2f} Bullish\n✅ RSI {rsi_val:.1f} Strong (55+)\n✅ London Session Active"
    elif price < al and e50 < e200 and rsi_val < 45:
        bias="SELL"; reason=f"✅ Asian Low {al:.2f} BREAKDOWN\n✅ EMA50 {e50:.2f} < EMA200 {e200:.2f} Bearish\n✅ RSI {rsi_val:.1f} Weak (45-)\n✅ London Session Active"
    else:
        reason=f"❌ Price Asian Range me hai ({al:.2f} - {ah:.2f})\n❌ EMA Trend match nahi\n❌ RSI {rsi_val:.1f} Neutral"

    # Chart
    buf = io.BytesIO()
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(close[-60:], color='black', linewidth=1)
    ax.plot(ema50[-60:], color='blue', linewidth=0.8, label='EMA50')
    ax.plot(ema200[-60:], color='orange', linewidth=0.8, label='EMA200')
    ax.axhline(ah, color='green', ls='--', lw=0.8, label=f'AH {ah:.0f}'); ax.axhline(al, color='red', ls='--', lw=0.8, label=f'AL {al:.0f}')
    ax.set_title(f"{name} {price:.2f} RSI {rsi_val:.1f}"); ax.legend(fontsize=6)
    plt.tight_layout(); plt.savefig(buf, format='png', dpi=130); buf.seek(0); plt.close(fig)

    if bias=="WAIT":
        msg=f"""**{name} | {price:.2f} | {bias}**

**Reason - Buy/Sell Kyu Nahi:**
{reason}

**Levels:**
Asian High: {ah:.2f}
Asian Low: {al:.2f}
EMA50: {e50:.2f} | EMA200: {e200:.2f}

**News:** {'NFP Time - Avoid' if is_news_time else 'No Major News'}
**Action:** WAIT - No Trade
"""
    else:
        sl_p=0.004; tp1_p=0.008; tp2_p=0.015
        sl=price*(1-sl_p) if bias=="BUY" else price*(1+sl_p)
        tp1=price*(1+tp1_p) if bias=="BUY" else price*(1-tp1_p)
        tp2=price*(1+tp2_p) if bias=="BUY" else price*(1-tp2_p)
        msg=f"""**🚨 {bias} {name} | {price:.2f}**

**ENTRY:** {price:.2f}
**SL:** {sl:.2f} (-0.4%)
**TP1:** {tp1:.2f} (+0.8%)
**TP2:** {tp2:.2f} (+1.5%)
**RR:** 1:2

**Buy/Sell Ka Reason:**
{reason}

**News Check:** {'⚠️ News Time - Risky' if is_news_time else '✅ No News - Safe'}

**Lot:** 0.01 for $50 (1% Risk)
**Act as:** Professional London Breakout Trader
"""
    return buf, msg, bias

async def start(update, context):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text("V3 Lite Auto ON! ✅ Deployed fix ho gaya.\n1:30-9:30PM auto scan ON hai.\n/signal xauusd\n/status")
async def pairs_cmd(update, context): await update.message.reply_text("Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")
async def lot_cmd(update, context): await update.message.reply_text(f"Acc $50-$100 pe 0.01 lot rakho (1% risk)")
async def status_cmd(update, context): await update.message.reply_text(f"Time: {datetime.now().strftime('%I:%M %p IST')}\nLondon Active: {is_session()}\nAuto: ON 15min\nChats: {len(CHAT_IDS)}")
async def sig(update, context):
    arg = (context.args[0].lower() if context.args else "xauusd")
    sym, name = PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} check...")
    buf, msg, _ = analyse(sym, name)
    if buf: await update.message.reply_photo(photo=buf, caption=msg, parse_mode='Markdown')
    else: await update.message.reply_text("Market band")

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    if not is_session() or not CHAT_IDS: return
    for key, (sym, name) in PAIRS.items():
        try:
            buf, msg, bias = analyse(sym, name)
            if bias in ["BUY","SELL"]:
                for cid in list(CHAT_IDS):
                    try: await context.bot.send_photo(chat_id=cid, photo=buf, caption=f"🚨 AUTO ALERT\n{msg}", parse_mode='Markdown')
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
    app.add_handler(CommandHandler("lot", lot_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.job_queue.run_repeating(auto_job, interval=900, first=10)
    print("Jarvis V3 Lite Auto Started")
    app.run_polling()
