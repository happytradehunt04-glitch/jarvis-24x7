import os, threading, io, json, time, requests, sqlite3, logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import gspread

# ─────────────────────────── CONFIG ───────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDENTIALS")
DB_PATH = os.getenv("DB_PATH", "trades.db")

CHAT_IDS = set()
LAST_SIGNAL_TIME = {}
CLEANUP_LOCK = False

# Pair → (yahoo_symbol, name, min_sl_pct, min_tp_pct)
# EURUSD hata diya (90% SL tha)
PAIRS = {
    "xauusd": ("GC=F",      "GOLD",   0.003, 0.005),
    "btcusd": ("BTC-USD",   "BTC",    0.005, 0.010),
    "gbpusd": ("GBPUSD=X",  "GBPUSD", 0.003, 0.006),
}

# ─────────────────────── LOGGING SETUP ────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("jarvis")

# ─────────────────────── SQLITE SETUP ─────────────────────────
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                date TEXT,
                pair TEXT,
                bias TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                status TEXT,
                chat_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)
        conn.commit()
        conn.close()
        logger.info("SQLite DB initialized")
    except Exception as e:
        logger.error(f"init_db failed: {e}")


def save_to_db(trade_id, date, pair, bias, entry, sl, tp1, tp2, status, chat_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trade_id, date, pair, bias, entry, sl, tp1, tp2, status, chat_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_to_db failed: {e}")


def save_chat_db(cid):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO chats VALUES (?)", (cid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_chat_db failed: {e}")


def load_chats_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM chats")
        for (cid,) in cur.fetchall():
            CHAT_IDS.add(int(cid))
        conn.close()
        logger.info(f"Loaded {len(CHAT_IDS)} chats from SQLite")
    except Exception as e:
        logger.error(f"load_chats_db failed: {e}")


# ─────────────────────── GOOGLE SHEET ─────────────────────────
def get_sheet():
    try:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
        return gc.open_by_key(SHEET_ID)
    except Exception as e:
        logger.error(f"get_sheet failed: {e}")
        return None


def load_chats_sheet():
    """Sheet se chats load karo (fallback for first run)."""
    try:
        sh = get_sheet()
        if not sh:
            return
        ws = sh.worksheet("chats")
        count = 0
        for r in ws.get_all_values():
            if r and r[0].isdigit():
                CHAT_IDS.add(int(r[0]))
                save_chat_db(int(r[0]))
                count += 1
        logger.info(f"Loaded {count} chats from Sheet")
    except Exception as e:
        logger.error(f"load_chats_sheet failed: {e}")


def save_chat(cid):
    """Naya chat: memory + SQLite + Sheet."""
    if cid in CHAT_IDS:
        return
    CHAT_IDS.add(cid)
    save_chat_db(cid)
    try:
        sh = get_sheet()
        if not sh:
            return
        ws = sh.worksheet("chats")
        existing = [r[0] for r in ws.get_all_values() if r]
        if str(cid) not in existing:
            ws.append_row([str(cid)])
    except Exception as e:
        logger.error(f"save_chat sheet failed: {e}")


# ─────────────────────── DATA FETCH ──────────────────────────
def get_df(sym):
    """Sirf 1h timeframe - BTC via Binance, baaki via Yahoo."""
    # BTC - Binance
    if "BTC" in sym:
        try:
            data = requests.get(
                "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=100",
                timeout=10,
            ).json()
            if isinstance(data, list) and len(data) > 50:
                df = pd.DataFrame(
                    data,
                    columns=["ot","Open","High","Low","Close","vol",
                             "ct","qav","tr","tbav","tqav","i"],
                )
                return df[["Open","High","Low","Close"]].astype(float)
        except Exception as e:
            logger.error(f"Binance failed {sym}: {e}")

    # Yahoo - sirf 1h (15m fallback HATA diya)
    headers = {"User-Agent": "Mozilla/5.0"}
    for rg in ["1mo", "3mo"]:
        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{sym}?range={rg}&interval=1h")
            r = requests.get(url, headers=headers, timeout=15).json()
            q = r["chart"]["result"][0]["indicators"]["quote"][0]
            df = pd.DataFrame({
                "Open": q["open"], "High": q["high"],
                "Low": q["low"], "Close": q["close"],
            }).dropna()
            if len(df) > 55:
                return df
        except Exception as e:
            logger.warning(f"Yahoo {rg} failed {sym}: {e}")
            continue
    return None


# ─────────────────────── ATR (Wilder) ────────────────────────
def calc_atr(df, period=14):
    """Wilder's ATR — RMA smoothing."""
    try:
        hl = df["High"] - df["Low"]
        hc = (df["High"] - df["Close"].shift()).abs()
        lc = (df["Low"] - df["Close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
        return float(atr)
    except Exception as e:
        logger.error(f"calc_atr failed: {e}")
        return float(df["Close"].iloc[-1]) * 0.005


# ─────────────────────── ANALYSIS ────────────────────────────
def analyse(sym, name, min_sl_pct, min_tp_pct):
    """
    Returns: (chart_buf, data_dict, bias, None)
    bias: BUY / SELL / WAIT
    """
    try:
        df = get_df(sym)
        if df is None or len(df) < 60:
            logger.warning(f"Not enough data for {name}")
            return None, None, "WAIT", None

        close = df["Close"]
        price = float(close.iloc[-1])

        # ✅ BUG FIX #1: span use karo, adjust=False
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])

        bias = "BUY" if e20 > e50 else "SELL" if e20 < e50 else "WAIT"

        # Strict filter - cross gap kam ho to WAIT
        if abs(e20 - e50) / price < 0.0008:
            bias = "WAIT"

        # ✅ BUG FIX #3 & #8: Wilder ATR, min floor
        atr = calc_atr(df)
        sl_dist = max(atr * 1.5, price * min_sl_pct)
        tp_dist = max(atr * 2.0, price * min_tp_pct)

        # Chart
        d = df[-30:]
        buf = io.BytesIO()
        fig, ax = plt.subplots(figsize=(7, 3.5))
        for i in range(len(d)):
            o = float(d["Open"].iloc[i]); h = float(d["High"].iloc[i])
            l = float(d["Low"].iloc[i]); c = float(d["Close"].iloc[i])
            col = "green" if c >= o else "red"
            ax.plot([i, i], [l, h], color=col, lw=0.8)
            ax.plot([i, i], [o, c], color=col, lw=3)
        ax.set_title(f"{name} {price:.2f} | {bias} | ATR:{atr:.2f}")
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=120)
        buf.seek(0)
        plt.close(fig)

        data = {
            "price": price,
            "atr": atr,
            "sl_dist": sl_dist,
            "tp_dist": tp_dist,
            "ema20": e20,
            "ema50": e50,
        }
        return buf, data, bias, None

    except Exception as e:
        logger.error(f"analyse {name} failed: {e}", exc_info=True)
        return None, None, "WAIT", None


# ─────────────────────── MARKET HOURS ────────────────────────
def is_market_open(name):
    """BTC 24/7. Forex: Sun 22:00 UTC - Fri 22:00 UTC."""
    if name == "BTC":
        return True
    now = datetime.utcnow()
    wd, h = now.weekday(), now.hour
    if wd == 5:                     # Saturday
        return False
    if wd == 6:                     # Sunday
        return h >= 22
    if wd == 4:                     # Friday
        return h < 22
    return True


# ─────────────────────── SIGNAL DEDUP ────────────────────────
def can_send(name, bias):
    """Sirf check karta hai, set nahi karta."""
    key = f"{name}_{bias}"
    last = LAST_SIGNAL_TIME.get(key)
    if last and datetime.now() - last < timedelta(hours=6):
        return False
    return True


def mark_sent(name, bias):
    """Sirf send hone ke baad call karo."""
    LAST_SIGNAL_TIME[f"{name}_{bias}"] = datetime.now()


# ─────────────────────── AUTO JOB ────────────────────────────
async def auto_job(context):
    if not CHAT_IDS:
        load_chats_db()
        if not CHAT_IDS:
            load_chats_sheet()

    if not CHAT_IDS:
        logger.warning("No chats registered, auto_job skip")
        return

    for _key, (sym, name, min_sl_pct, min_tp_pct) in PAIRS.items():
        if not is_market_open(name):
            continue

        try:
            buf, data, bias, _ = analyse(sym, name, min_sl_pct, min_tp_pct)
            if bias == "WAIT" or not buf:
                continue
            if not can_send(name, bias):
                logger.info(f"Skip {name} {bias} - cooldown active")
                continue

            entry = data["price"]
            sl_dist = data["sl_dist"]
            tp_dist = data["tp_dist"]
            sl = entry - sl_dist if bias == "BUY" else entry + sl_dist
            tp1 = entry + tp_dist if bias == "BUY" else entry - tp_dist
            tp2 = entry + 2 * tp_dist if bias == "BUY" else entry - 2 * tp_dist

            # ✅ EK trade save (per-user nahi)
            tid = f"{name}_{int(time.time())}"
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            save_to_db(tid, date_str, name, bias, entry, sl, tp1, tp2, "OPEN", 0)

            caption = (
                f"🚨 AUTO {bias} {name}\n"
                f"Entry: {entry:.2f}\n"
                f"SL: {sl:.2f}  ({sl_dist/entry*100:.2f}%)\n"
                f"TP1: {tp1:.2f}  ({tp_dist/entry*100:.2f}%)\n"
                f"TP2: {tp2:.2f}\n"
                f"ATR: {data['atr']:.2f}"
            )

            # ✅ Sab users ko bhejo (ek hi trade_id)
            sent_ok = 0
            for cid in list(CHAT_IDS):
                try:
                    buf.seek(0)
                    await context.bot.send_photo(
                        chat_id=cid, photo=buf, caption=caption
                    )
                    sent_ok += 1
                except Exception as e:
                    logger.error(f"send to {cid} failed: {e}")

            if sent_ok > 0:
                mark_sent(name, bias)   # ✅ sirf tab mark karo jab bhej diya
                logger.info(f"Signal sent: {name} {bias} to {sent_ok} users")

                # Sheet backup
                sh = get_sheet()
                if sh:
                    try:
                        sh.sheet1.append_row([
                            tid, date_str, name, bias,
                            entry, sl, tp1, tp2, "OPEN", "", "", 0,
                        ])
                    except Exception as e:
                        logger.error(f"Sheet append failed: {e}")
            else:
                logger.warning(f"No users received {name} signal")

        except Exception as e:
            logger.error(f"auto_job {name} error: {e}", exc_info=True)


# ─────────────────────── TP/SL CHECKER ───────────────────────
async def tp_checker(context):
    """Har 5 min: OPEN trades check karo. Price cache se API spam avoid."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_id,pair,bias,entry,sl,tp1,tp2 "
            "FROM trades WHERE status='OPEN' "
            "ORDER BY date DESC LIMIT 200"
        )
        rows = cur.fetchall()

        if not rows:
            conn.close()
            return

        # ✅ BUG FIX #6: pair-wise price cache
        price_cache = {}
        updated = []

        for tid, pair, bias, entry, sl, tp1, tp2 in rows:
            try:
                sym = next(
                    (v[0] for k, v in PAIRS.items() if v[1] == pair), None
                )
                if not sym:
                    continue

                if pair not in price_cache:
                    df = get_df(sym)
                    price_cache[pair] = (
                        float(df["Close"].iloc[-1]) if df is not None else None
                    )
                price = price_cache[pair]
                if price is None:
                    continue

                new = None
                if bias == "BUY":
                    if price >= tp2:
                        new = "TP2_HIT"
                    elif price >= tp1:
                        new = "TP1_HIT"
                    elif price <= sl:
                        new = "SL_HIT"
                else:  # SELL
                    if price <= tp2:
                        new = "TP2_HIT"
                    elif price <= tp1:
                        new = "TP1_HIT"
                    elif price >= sl:
                        new = "SL_HIT"

                if new:
                    cur.execute(
                        "UPDATE trades SET status=? WHERE trade_id=?",
                        (new, tid),
                    )
                    updated.append((tid, new))
                    logger.info(f"Trade {tid} -> {new} @ {price:.2f}")
            except Exception as e:
                logger.error(f"tp_checker row {tid}: {e}")

        conn.commit()
        conn.close()

        # Sheet update (batch)
        if updated:
            sh = get_sheet()
            if sh:
                try:
                    vals = sh.sheet1.get_all_values()
                    tid_to_row = {
                        r[0]: i + 1 for i, r in enumerate(vals) if r and r[0]
                    }
                    for tid, new in updated:
                        row_idx = tid_to_row.get(tid)
                        if row_idx:
                            try:
                                sh.sheet1.update_cell(row_idx, 9, new)
                            except Exception as e:
                                logger.error(f"Sheet update {tid}: {e}")
                except Exception as e:
                    logger.error(f"Sheet batch update failed: {e}")

    except Exception as e:
        logger.error(f"tp_checker failed: {e}", exc_info=True)


# ─────────────────────── COMMANDS ────────────────────────────
async def start(update, context):
    save_chat(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 *JARVIS V10 LIVE* ✅\n\n"
        "📊 *Commands:*\n"
        "/signal btcusd — manual signal\n"
        "/signal xauusd\n"
        "/signal gbpusd\n"
        "/weekly — result summary\n"
        "/stats — pair-wise winrate\n"
        "/cleanup — old data clear\n\n"
        "⏰ Auto signal har 1 hour\n"
        "🎯 Pairs: GOLD, BTC, GBPUSD",
        parse_mode="Markdown",
    )


async def sig(update, context):
    try:
        arg = (context.args[0].lower() if context.args else "btcusd")
        if arg not in PAIRS:
            await update.message.reply_text(
                f"❌ Unknown pair. Available: {', '.join(PAIRS.keys())}"
            )
            return

        sym, name, min_sl_pct, min_tp_pct = PAIRS[arg]
        save_chat(update.effective_chat.id)

        if not is_market_open(name):
            await update.message.reply_text(
                f"⚠️ {name} abhi band hai (weekend)"
            )
            return

        await update.message.reply_text(f"🔍 {name} checking...")

        buf, data, bias, _ = analyse(sym, name, min_sl_pct, min_tp_pct)
        if not buf:
            await update.message.reply_text("❌ Data nahi mila")
            return

        if bias == "WAIT":
            await update.message.reply_photo(
                photo=buf,
                caption=(
                    f"⏸ WAIT {name}\n"
                    f"Price: {data['price']:.2f}\n"
                    f"ATR: {data['atr']:.2f}\n"
                    f"EMA gap chhota hai"
                ),
            )
            return

        entry = data["price"]
        sl_dist = data["sl_dist"]
        tp_dist = data["tp_dist"]
        sl = entry - sl_dist if bias == "BUY" else entry + sl_dist
        tp1 = entry + tp_dist if bias == "BUY" else entry - tp_dist
        tp2 = entry + 2 * tp_dist if bias == "BUY" else entry - 2 * tp_dist

        caption = (
            f"{'🟢' if bias=='BUY' else '🔴'} {bias} {name}\n"
            f"Entry: {entry:.2f}\n"
            f"SL: {sl:.2f}  ({sl_dist/entry*100:.2f}%)\n"
            f"TP1: {tp1:.2f}  ({tp_dist/entry*100:.2f}%)\n"
            f"TP2: {tp2:.2f}\n"
            f"ATR: {data['atr']:.2f}"
        )
        await update.message.reply_photo(photo=buf, caption=caption)

        # DB + Sheet save
        tid = f"{name}_{int(time.time())}"
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_to_db(tid, date_str, name, bias, entry, sl, tp1, tp2,
                   "OPEN", update.effective_chat.id)

        sh = get_sheet()
        if sh:
            try:
                sh.sheet1.append_row([
                    tid, date_str, name, bias,
                    entry, sl, tp1, tp2, "OPEN", "", "", update.effective_chat.id,
                ])
            except Exception as e:
                logger.error(f"Sheet append: {e}")

    except Exception as e:
        logger.error(f"sig failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


async def weekly_cmd(update, context):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT status FROM trades")
        all_r = cur.fetchall()
        conn.close()

        total = len(all_r)
        tp = len([r for r in all_r if "TP" in r[0]])
        sl = len([r for r in all_r if "SL" in r[0]])
        op = len([r for r in all_r if r[0] == "OPEN"])
        winrate = tp / (tp + sl) * 100 if (tp + sl) > 0 else 0

        await update.message.reply_text(
            f"📊 *RESULT*\n"
            f"Total: {total}\n"
            f"✅ TP: {tp}\n"
            f"❌ SL: {sl}\n"
            f"🟡 OPEN: {op}\n"
            f"📈 Winrate: {winrate:.1f}%",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"weekly_cmd failed: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


async def stats_cmd(update, context):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT pair, status, COUNT(*) FROM trades GROUP BY pair, status"
        )
        data = cur.fetchall()
        conn.close()

        if not data:
            await update.message.reply_text("📭 No trades yet")
            return

        pairs = {}
        for p, s, c in data:
            pairs.setdefault(p, {"TP": 0, "SL": 0, "OPEN": 0})
            if "TP" in s:
                pairs[p]["TP"] += c
            elif "SL" in s:
                pairs[p]["SL"] += c
            else:
                pairs[p]["OPEN"] += c

        msg = "📈 *STATS (Pair Wise)*\n\n"
        for p, v in pairs.items():
            tot = v["TP"] + v["SL"]
            win = v["TP"] / tot * 100 if tot > 0 else 0
            msg += (
                f"*{p}*\n"
                f"  ✅ TP: {v['TP']}  ❌ SL: {v['SL']}  🟡 OPEN: {v['OPEN']}\n"
                f"  📊 Win: {win:.1f}%\n\n"
            )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"stats_cmd failed: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


async def cleanup_cmd(update, context):
    """SQLite + Sheet: last 100 rakho, baaki delete."""
    global CLEANUP_LOCK
    if CLEANUP_LOCK:
        await update.message.reply_text("⏳ Cleanup already running...")
        return
    CLEANUP_LOCK = True
    try:
        await update.message.reply_text("🧹 Cleanup shuru...")

        # SQLite cleanup
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades")
        sqlite_before = cur.fetchone()[0]
        cur.execute("""
            DELETE FROM trades WHERE trade_id NOT IN (
                SELECT trade_id FROM trades ORDER BY date DESC LIMIT 100
            )
        """)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM trades")
        sqlite_after = cur.fetchone()[0]
        conn.close()

        # Sheet cleanup
        sheet_msg = "Sheet skip (error)"
        try:
            sh = get_sheet()
            if sh:
                ws = sh.sheet1
                vals = ws.get_all_values()
                sheet_before = len(vals) - 1

                if sheet_before > 120:
                    header = vals[0]
                    keep = vals[-100:]
                    ws.clear()
                    time.sleep(1)
                    ws.append_row(header)
                    time.sleep(0.5)
                    ws.append_rows(keep)
                    sheet_msg = f"Sheet: {sheet_before} → 100"
                else:
                    sheet_msg = f"Sheet already clean ({sheet_before})"
        except Exception as e:
            logger.error(f"Sheet cleanup failed: {e}")
            sheet_msg = f"Sheet error: {e}"

        await update.message.reply_text(
            f"✅ *DONE!*\n"
            f"SQLite: {sqlite_before} → {sqlite_after}\n"
            f"{sheet_msg}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"cleanup failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        CLEANUP_LOCK = False


# ─────────────────────── HEALTH CHECK ────────────────────────
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Jarvis V10 Live")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


threading.Thread(
    target=lambda: HTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", 10000))), H
    ).serve_forever(),
    daemon=True,
).start()


# ─────────────────────── MAIN ────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting Jarvis V10...")
    init_db()
    load_chats_db()
    if not CHAT_IDS:
        load_chats_sheet()

    if not TOKEN:
        logger.error("BOT_TOKEN missing!")
        exit(1)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", sig))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("result", weekly_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd))

    # Auto job: 1 hour
    app.job_queue.run_repeating(auto_job, interval=3600, first=30)
    # TP checker: 5 min
    app.job_queue.run_repeating(tp_checker, interval=300, first=60)

    logger.info("Jarvis V10 is running ✅")
    app.run_polling(drop_pending_updates=True)
