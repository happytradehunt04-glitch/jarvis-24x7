import os, threading, io
from http.server import BaseHTTPRequestHandler, HTTPServer
import yfinance as yf
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

TOKEN = os.getenv("BOT_TOKEN")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Jarvis Live")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT",10000))), H).serve_forever(), daemon=True).start()

def get_signal(symbol_yf, name):
    try:
        df = yf.download(symbol_yf, period="2d", interval="15m", progress=False)
        if len(df) < 20: return None, "Data kam hai"
        df['Close'] = df['Close'].astype(float)
        price = float(df['Close'].iloc[-1])

        # RSI simple
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1])

        # Chart banao
        fig, ax = plt.subplots(figsize=(6,3))
        ax.plot(df['Close'][-50:], color='gold' if 'GOLD' in name else 'blue')
        ax.set_title(f"{name} - {price:.2f} | RSI {rsi_val:.1f}")
        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close(fig)

        # Signal logic
        if rsi_val > 65:
            bias, sl_p, tp1_p, tp2_p = "SELL", 0.003, 0.005, 0.01
            reason = f"RSI {rsi_val:.1f} Overbought"
        elif rsi_val < 35:
            bias, sl_p, tp1_p, tp2_p = "BUY", 0.003, 0.005, 0.01
            reason = f"RSI {rsi_val:.1f} Oversold"
        else:
            bias, sl_p, tp1_p, tp2_p = "WAIT", 0, 0, 0
            reason = f"RSI {rsi_val:.1f} Neutral"

        if bias == "BUY":
            sl = price * (1 - sl_p)
            tp1 = price * (1 + tp1_p)
            tp2 = price * (1 + tp2_p)
        elif bias == "SELL":
            sl = price * (1 + sl_p)
            tp1 = price * (1 - tp1_p)
            tp2 = price * (1 - tp2_p)
        else:
            sl = tp1 = tp2 = price

        msg = f"""**{name} | Price: {price:.2f} | RSI: {rsi_val:.1f}**
**BIAS: {bias}**
**ENTRY: {price:.2f}**
**SL: {sl:.2f}**
**TP1: {tp1:.2f}**
**TP2: {tp2:.2f}**

**Reason:** {reason}
Risk: 1-2% only. Educational.
"""
        return buf, msg
    except Exception as e:
        return None, f"Error {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Boss Jarvis 24x7 LIVE hai! 🚀\n\n/signal xauusd - Gold\n/signal gbpusd - GBP\n/signal eurusd - Euro\n/signal btc - BTC")

async def sig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "xauusd")
    if "xau" in arg or "gold" in arg: sym, name = "GC=F", "GOLD / XAUUSD"
    elif "gbp" in arg: sym, name = "GBPUSD=X", "GBPUSD"
    elif "eur" in arg: sym, name = "EURUSD=X", "EURUSD"
    elif "btc" in arg: sym, name = "BTC-USD", "BTCUSD"
    else: sym, name = "GC=F", "GOLD / XAUUSD"

    await update.message.reply_text(f"{name} ka chart bana raha hu... 10 sec")
    chart, msg = get_signal(sym, name)
    if chart:
        await update.message.reply_photo(photo=chart, caption=msg, parse_mode='Markdown')
    else:
        await update.message.reply_text(msg)

if __name__ == "__main__":
    import asyncio
    try: asyncio.get_event_loop()
    except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    print("Jarvis Full Started")
    app.run_polling()
