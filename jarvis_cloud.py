import os
import yfinance as yf
import pandas as pd
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Web server for Render
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Boss Jarvis 24x7 LIVE hai! \n/signal GBPUSD")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = " ".join(context.args) if context.args else "GBPUSD=X"
    if "USD" in symbol and "=X" not in symbol:
        symbol = symbol.replace("USD","USD=X") if "GBPUSD" in symbol or "EURUSD" in symbol else symbol
    try:
        data = yf.download(symbol, period="1d", interval="15m", progress=False)
        price = float(data['Close'].iloc[-1])
        await update.message.reply_text(f"**{symbol} | Price: {price:.4f}**\n\nBIAS: WAIT\nEducational only.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    print("Jarvis polling started")
    app.run_polling()

if __name__ == "__main__":
    main()
