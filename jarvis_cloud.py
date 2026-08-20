import os, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf
import pandas as pd

TOKEN = os.getenv("BOT_TOKEN")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

PAIRS = {
    "xauusd": "GC=F", "gold": "GC=F", "eurusd": "EURUSD=X",
    "gbpusd": "GBPUSD=X", "usdjpy": "USDJPY=X", "gbpjpy": "GBPJPY=X",
    "audusd": "AUDUSD=X", "usdcad": "USDCAD=X",
    "us30": "^DJI", "nas100": "^IXIC", "btc": "BTC-USD"
}

def get_price(sym):
    df = yf.download(sym, period="5d", interval="1h", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    c = df['Close'] if 'Close' in df.columns else df.iloc[:,0]
    if isinstance(c, pd.DataFrame): c = c.iloc[:,0]
    return float(c.iloc[-1])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Jarvis V2 Lite LIVE! ✅\n\n/signal usdjpy\n/signal xauusd\n/pairs")

async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pairs: xauusd, eurusd, gbpusd, usdjpy, gbpjpy, audusd, usdcad, us30, nas100, btc")

async def sig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "xauusd")
    sym = PAIRS.get(arg, "GC=F")
    await update.message.reply_text(f"{arg.upper()} ka price le raha hu...")
    try:
        price = get_price(sym)
        await update.message.reply_text(f"**{arg.upper()} Price: {price:.2f}**\nBug Fixed! Ab sahi pair ka price aayega.\n\nAb ispe full breakout logic add karenge.", parse_mode='Markdown')
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
    print("Jarvis Lite Started")
    app.run_polling()
