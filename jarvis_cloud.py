import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from groq import Groq
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Render ke liye web server
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Jarvis 24x7 Alive!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()

TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Boss Jarvis 24x7 LIVE hai! /signal gold")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else "gold"
    if not client:
        await update.message.reply_text("GROQ key missing")
        return
    try:
        res = client.chat.completions.create(
            messages=[{"role":"user","content":f"Give short trading signal for {q}"}],
            model="llama3-8b-8192"
        )
        await update.message.reply_text(res.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    print("Jarvis Started Polling")
    app.run_polling()

if __name__ == "__main__":
    main()
