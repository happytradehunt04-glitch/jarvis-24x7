import os, threading, io
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf
import pandas as pd
import mplfinance as mpf

TOKEN = os.getenv("BOT_TOKEN")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis Pro Live")
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

def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, None, "WAIT"
    close = df['Close']; price = float(close.iloc[-1])
    ema50 = close.ewm(50).mean(); ema200 = close.ewm(200).mean()
    rsi_delta = close.diff(); gain = rsi_delta.where(rsi_delta>0,0).rolling(14).mean(); loss = -rsi_delta.where(rsi_delta<0,0).rolling(14).mean(); rsi = 100 - (100/(1+gain/loss))
    rsi_val = float(rsi.iloc[-1]); e50 = float(ema50.iloc[-1]); e200 = float(ema200.iloc[-1])
    asian = close.iloc[-32:-8]; ah = float(asian.max()); al = float(asian.min())

    bias="WAIT"; reason=""
    if price > ah and e50 > e200 and rsi_val > 55:
        bias="BUY"; reason=f"Asian High {ah:.2f} BREAKOUT + EMA Bullish + RSI {rsi_val:.1f}"
    elif price < al and e50 < e200 and rsi_val < 45:
        bias="SELL"; reason=f"Asian Low {al:.2f} BREAKDOWN + EMA Bearish + RSI {rsi_val:.1f}"

    # Pro Candle Chart
    df_plot = df[-60:].copy()
    ap = [mpf.make_addplot(ema50[-60:], color='blue'), mpf.make_addplot(ema200[-60:], color='orange')]
    buf = io.BytesIO()
    mpf.plot(df_plot, type='candle', style='yahoo', addplot=ap, title=f"{name} - {price:.2f} | RSI {rsi_val:.1f}", ylabel='Price', volume=False, savefig=dict(fname=buf, dpi=150, bbox_inches='tight'))
    buf.seek(0)

    sl_p=0.004; tp1_p=0.008; tp2_p=0.015
    if bias=="BUY":
        sl=price*(1-sl_p); tp1=price*(1+tp1_p); tp2=price*(1+tp2_p)
    else:
        sl=price*(1+sl_p); tp1=price*(1-tp1_p); tp2=price*(1-tp2_p)

    if bias=="WAIT":
        msg=f"**{name} | {price:.2f} | RSI {rsi_val:.1f}**\nBIAS: WAIT\nAsian High: {ah:.2f}\nAsian Low: {al:.2f}\nEMA50 {e50:.2f} vs EMA200 {e200:.2f}\n\nAct as: London Breakout Trader - No setup now"
    else:
        msg=f"""**🚨 {bias} {name} | {price:.2f}**
**ENTRY: {price:.2f}**
**SL: {sl:.2f}**
**TP1: {tp1:.2f}**
**TP2: {tp2:.2f}**
**RR: 1:2**

**Reason:**
{reason}

**Chart:** Candle + EMA50(Blue) + EMA200(Orange)
**Risk:** 0.01 lot for $50-$100
**Act as:** Professional London Session Trader
Edu Only.
"""
    return buf, msg, bias

async def start(update, context): await update.message.reply_text("Jarvis Pro Candle LIVE! ✅\n/signal xauusd\n/signal usdjpy")
async def pairs_cmd(update, context): await update.message.reply_text("Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")
async def sig(update, context):
    arg = (context.args[0].lower() if context.args else "xauusd")
    sym, name = PAIRS.get(arg, ("GC=F","GOLD"))
    await update.message.reply_text(f"{name} ka Pro Candle Chart bana raha hu...")
    try:
        buf, msg, _ = analyse(sym, name)
        if buf is None: await update.message.reply_text("Market band hai, Monday ko try karo"); return
        await update.message.reply_photo(photo=buf, caption=msg, parse_mode='Markdown')
    except Exception as e: await update.message.reply_text(f"Err: {e}")

if __name__ == "__main__":
    import asyncio
    try: asyncio.get_event_loop()
    except: asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    print("Jarvis Candle Started")
    app.run_polling()
