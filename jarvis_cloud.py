import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Render ko port chahiye isliye chota server
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Jarvis is Alive!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# Tokens Environment se lega
TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Jarvis 24x7 Online Boss! /signal gold bhejo")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = " ".join(context.args) if context.args else "gold"
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": f"Give a short trading signal analysis for {user_msg}"}],
            model="llama3-8b-8192",
        )
        reply = chat_completion.choices[0].message.content
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    print("Jarvis Cloud 24x7 Started")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
