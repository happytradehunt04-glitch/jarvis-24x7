import os, threading, io
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

TOKEN = os.getenv("BOT_TOKEN")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

PAIRS = {
    "xauusd": ("GC=F", "GOLD"), "gold": ("GC=F", "GOLD"),
    "eurusd": ("EURUSD=X", "EURUSD"), "gbpusd": ("GBPUSD=X", "GBPUSD"),
    "usdjpy": ("USDJPY=X", "USDJPY"), "gbpjpy": ("GBPJPY=X", "GBPJPY"),
    "audusd": ("AUDUSD=X", "AUDUSD"), "usdcad": ("USDCAD=X", "USDCAD"),
    "us30": ("^DJI", "US30"), "nas100": ("^IXIC", "NAS100"), "btc": ("BTC-USD", "BTC")
}

def get_data(sym):
    # Fallback system - 15m -> 1h -> 1d
    for per, inter in [("5d","15m"), ("5d","1h"), ("1mo","1d")]:
        try:
            df = yf.download(sym, period=per, interval=inter, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) > 20: return df
        except: pass
    return None

def make_chart(df, name, price):
    c = df['Close'] if 'Close' in df.columns else df.iloc[:,0]
    if isinstance(c, pd.DataFrame): c = c.iloc[:,0]
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(c[-50:], color='black')
    ax.set_title(f"{name} - {price:.2f}")
    buf = io.BytesIO(); plt.tight_layout(); plt.savefig(buf, format='png', dpi=150); buf.seek(0); plt.close(fig)
    return buf, c

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Jarvis V2 Fixed LIVE! ✅\nChart wapas aa gaya.\n/signal usdjpy\n/signal xauusd\n/pairs")

async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("10 Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")

async def sig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "xauusd")
    sym, name = PAIRS.get(arg, ("GC=F", "GOLD"))
    await update.message.reply_text(f"{name} ka chart bana raha hu...")
    try:
        df = get_data(sym)
        if df is None:
            await update.message.reply_text(f"{name} ka market band hai (Weekend), Monday ko try karo.")
            return
        c = df['Close'] if 'Close' in df.columns else df.iloc[:,0]
        if isinstance(c, pd.DataFrame): c = c.iloc[:,0]
        price = float(c.iloc[-1])
        buf, _ = make_chart(df, name, price)
        msg = f"**{name} | Price: {price:.2f}**\nBug Fixed! Chart ke sath.\nAb auto alert logic add karna hai."
        await update.message.reply_photo(photo=buf, caption=msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

if __name__ == "__main__":
    import asyncio
    try: asyncio.get_event_loop()
    except: asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    print("Jarvis Chart Fixed Started")
    app.run_polling()
