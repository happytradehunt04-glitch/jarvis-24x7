import os
import yfinance as yf
import pandas as pd
import ta
import matplotlib.pyplot as plt
import mplfinance as mpf
import io
from telegram import Update
from telegram.ext import Application, CommandHandler
from groq import Groq

TOKEN = "8700856917:AAEvVtVwQtMty0FqeTYpYdnFJSIfJH0VCe0" # tera token
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)

async def get_data(ticker, interval):
    data = yf.download(ticker, period="14d", interval=interval, progress=False)
    if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.droplevel(1)
    data['Close']=data['Close'].squeeze()
    data['High']=data['High'].squeeze()
    data['Low']=data['Low'].squeeze()
    data['RSI']=ta.momentum.RSIIndicator(data['Close']).rsi()
    data['EMA20']=ta.trend.EMAIndicator(data['Close'],20).ema_indicator()
    data['EMA50']=ta.trend.EMAIndicator(data['Close'],50).ema_indicator()
    data['ATR']=ta.volatility.AverageTrueRange(data['High'],data['Low'],data['Close']).average_true_range()
    return data

async def signal(update, context):
    pair = context.args[0].lower() if context.args else "gold"
    mp = {"gold":("GC=F","4h","GOLD"),"xauusd":("GC=F","4h","GOLD"),"eurusd":("EURUSD=X","60m","EURUSD"),"gbpusd":("GBPUSD=X","60m","GBPUSD"),"btc":("BTC-USD","15m","BTC")}
    ticker, interval, name = mp.get(pair, ("GC=F","4h","GOLD"))
    data = await get_data(ticker, interval)
    last = data.iloc[-1]

    prompt = f"Give trade plan for {name} Price {last['Close']:.2f} RSI {last['RSI']:.1f} EMA20 {last['EMA20']:.2f} EMA50 {last['EMA50']:.2f}. Format BIAS, ENTRY, SL, TP1, TP2, REASON Hindi"

    comp = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role":"user","content":prompt}], max_tokens=300)
    ai = comp.choices[0].message.content

    await update.message.reply_text(f"📊 {name} 24x7 PLAN\n\n{ai}\n\nPC Band pe bhi working ✅")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("gold", signal))
    print("Cloud Bot Started")
    app.run_polling()

if __name__ == '__main__': main()