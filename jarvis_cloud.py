"""JARVIS V12: Telegram paper-signal bot. Never places broker orders."""

import asyncio
import csv
import io
import json
import logging
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd


LOG = logging.getLogger("jarvis")

DB = os.getenv("DB_PATH", "data/jarvis_v12.sqlite3")
BALANCE = float(os.getenv("ACCOUNT_SIZE", "5000"))

# Percent values: 0.25 means 0.25%, not 25%.
RISK = float(os.getenv("RISK_PCT", "0.25")) / 100
MAX_OPEN = float(os.getenv("MAX_OPEN_RISK_PCT", "0.5")) / 100
DAY_LIMIT = float(os.getenv("DAILY_LOSS_PCT", "1")) / 100
DD_LIMIT = float(os.getenv("MAX_DRAWDOWN_PCT", "5")) / 100

# Estimated total trading costs as a fraction of initial risk.
# Example: 0.05 means costs of 0.05R. Adjust using broker data.
COST_R = float(os.getenv("COST_R", "0.05"))

FIRM_TZ = ZoneInfo(os.getenv("FIRM_TIMEZONE", "Europe/Prague"))

ADMINS = {
    int(x)
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip()
}
ALLOWED = {
    int(x)
    for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}

PAIRS = {
    "btcusd": "BTC-USD",
    "eurusd": "EURUSD=X",
    "gbpusd": "GBPUSD=X",
}

ACTIVE = ("PENDING", "OPEN", "PARTIAL", "REVIEW")
LOCK = asyncio.Lock()
HEARTBEAT = time.monotonic()


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)

    with connect() as db:
        existing = {
            row[1] for row in db.execute("PRAGMA table_info(trades)")
        }
        if existing and not {
            "id", "review_from", "fill_at", "net_r"
        } <= existing:
            raise RuntimeError(
                "Legacy/incompatible DB_PATH. Use a NEW "
                "jarvis_v12.sqlite3; keep the old database as an archive."
            )

        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS subscribers(
                chat_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY,
                pair TEXT NOT NULL,
                candle INTEGER NOT NULL,
                created INTEGER NOT NULL,
                fill_at INTEGER NOT NULL,
                checked INTEGER NOT NULL,
                side INTEGER NOT NULL,
                reference REAL NOT NULL,
                distance REAL NOT NULL,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                risk REAL NOT NULL,
                status TEXT NOT NULL,
                closed INTEGER,
                net_r REAL,
                review_from TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                UNIQUE(pair, candle)
            );
        """)


def all_trades():
    with connect() as db:
        return [
            dict(row)
            for row in db.execute("SELECT * FROM trades ORDER BY id")
        ]


def market_open(pair, ts):
    if pair == "btcusd":
        return True

    ny = datetime.fromtimestamp(
        ts, ZoneInfo("America/New_York")
    )

    return not (
        ny.weekday() == 5
        or (ny.weekday() == 4 and ny.hour >= 17)
        or (ny.weekday() == 6 and ny.hour < 17)
    )


def session_open(pair, ts):
    if pair == "btcusd":
        return True

    london = datetime.fromtimestamp(
        ts, ZoneInfo("Europe/London")
    )
    ny = datetime.fromtimestamp(
        ts, ZoneInfo("America/New_York")
    )

    return market_open(pair, ts) and (
        8 <= london.hour < 12
        or (
            13 <= london.hour < 17
            and 8 <= ny.hour < 12
        )
    )


def request_json(url):
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def get_frame(pair, minutes, now=None):
    """UTC epoch index; completed candles only."""
    now = int(time.time() if now is None else now)
    symbol = PAIRS[pair]
    interval = "1h" if minutes == 60 else "5m"

    if pair == "btcusd":
        query = urlencode({"granularity": minutes * 60})
        raw = request_json(
            "https://api.exchange.coinbase.com/products/"
            "BTC-USD/candles?" + query
        )
        if not isinstance(raw, list):
            raise ValueError("Coinbase returned an error")

        # Coinbase: timestamp, low, high, open, close, volume.
        rows = [
            (
                int(row[0]),
                float(row[3]),
                float(row[2]),
                float(row[1]),
                float(row[4]),
            )
            for row in raw
        ]
        frame = pd.DataFrame(
            rows,
            columns=["ts", "Open", "High", "Low", "Close"],
        ).set_index("ts")

    else:
        query = urlencode({
            "range": "3mo" if minutes == 60 else "1mo",
            "interval": interval,
        })
        raw = request_json(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + quote(symbol, safe="")
            + "?"
            + query
        )
        if raw["chart"].get("error"):
            raise ValueError("Yahoo returned an error")

        result = raw["chart"]["result"][0]
        frame = pd.DataFrame(
            result["indicators"]["quote"][0],
            index=result["timestamp"],
        )
        frame = frame.rename(columns=str.title)[
            ["Open", "High", "Low", "Close"]
        ]

    frame = (
        frame.apply(pd.to_numeric, errors="coerce")
        .dropna()
        .sort_index()
    )

    if frame.index.has_duplicates:
        raise ValueError("Duplicate candle timestamps")

    frame = frame.loc[
        frame.index + minutes * 60 <= now
    ]
    if frame.empty:
        raise ValueError("No completed candles")

    valid = (
        (frame > 0).all(axis=1)
        & (
            frame.High
            >= frame[["Open", "Close", "Low"]].max(axis=1)
        )
        & (
            frame.Low
            <= frame[["Open", "Close", "High"]].min(axis=1)
        )
    )
    infinite = frame.isin([
        float("inf"), float("-inf")
    ]).any().any()

    if not valid.all() or infinite:
        raise ValueError("Invalid OHLC data")

    return frame


def analyse(frame, now):
    # Only act within 10 minutes of the latest hourly close.
    if len(frame) < 200:
        return None

    age = now - (int(frame.index[-1]) + 3600)
    if not 0 <= age <= 600:
        return None

    close = frame.Close
    e20 = close.ewm(
        span=20, adjust=False
    ).mean().iloc[-1]
    e50 = close.ewm(
        span=50, adjust=False
    ).mean().iloc[-1]

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean().iloc[-1]

    if gain == loss == 0:
        rsi = 50
    elif loss == 0:
        rsi = 100
    else:
        rsi = 100 - 100 / (1 + gain / loss)

    tr = pd.concat([
        frame.High - frame.Low,
        (frame.High - close.shift()).abs(),
        (frame.Low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean().iloc[-1]

    price = float(close.iloc[-1])

    if not all(
        math.isfinite(value)
        for value in [price, atr, rsi, e20, e50]
    ):
        return None

    if atr <= 0 or abs(e20 - e50) / price < 0.0012:
        return None

    side = 0
    if e20 > e50 and 55 < rsi < 70:
        side = 1
    elif e20 < e50 and 30 < rsi < 45:
        side = -1

    if not side:
        return None

    return side, price, float(1.5 * atr), float(rsi)


def reserve(pair, candle, now, setup):
    """Shared paper account; manual requests use the same risk gate."""
    side, reference, distance, _ = setup
    day = datetime.fromtimestamp(now, FIRM_TZ).date()

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = [
            dict(row)
            for row in db.execute("SELECT * FROM trades")
        ]

        if any(
            trade["pair"] == pair
            and (
                trade["status"] in ACTIVE
                or trade["created"] > now - 21600
            )
            for trade in rows
        ):
            return None

        if any(
            trade["pair"] == pair
            and trade["candle"] == candle
            for trade in rows
        ):
            return None

        if any(
            trade["status"] == "REVIEW"
            for trade in rows
        ):
            return None

        equity = BALANCE + sum(
            trade["risk"] * trade["net_r"]
            for trade in rows
            if trade["net_r"] is not None
        )

        risk = min(BALANCE, equity) * RISK

        reserved = sum(
            trade["risk"] * (1 + COST_R)
            for trade in rows
            if trade["status"] in ACTIVE
        )

        daily_losses = sum(
            max(0, -trade["risk"] * trade["net_r"])
            for trade in rows
            if trade["closed"]
            and trade["net_r"] is not None
            and datetime.fromtimestamp(
                trade["closed"], FIRM_TZ
            ).date() == day
        )

        worst = reserved + risk * (1 + COST_R)

        if (
            risk <= 0
            or worst > BALANCE * MAX_OPEN
            or daily_losses + worst > BALANCE * DAY_LIMIT
            or equity - worst < BALANCE * (1 - DD_LIMIT)
        ):
            return None

        fill_at = (now // 300 + 1) * 300

        cursor = db.execute("""
            INSERT INTO trades(
                pair, candle, created, fill_at, checked,
                side, reference, distance, risk, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pair,
            candle,
            now,
            fill_at,
            fill_at - 300,
            side,
            reference,
            distance,
            risk,
            "PENDING",
        ))
        return cursor.lastrowid


def finish(trade, status, ts, gross_r):
    trade.update(
        status=status,
        closed=ts,
        net_r=gross_r - COST_R,
    )


def process_bar(trade, ts, bar):
    """
    Exit 50% at 1R and 50% at 2R.
    Retain the original stop.
    Use stop-first when intrabar ordering is unknown.
    """
    t = trade
    side = t["side"]

    if t["status"] == "PENDING":
        excessive_gap = (
            abs(bar.Open - t["reference"])
            > t["distance"] * 0.25
        )
        if ts != t["fill_at"] or excessive_gap:
            t.update(status="CANCELLED", closed=ts)
            return

        t.update(status="OPEN", entry=float(bar.Open))
        t.update(
            sl=t["entry"] - side * t["distance"],
            tp1=t["entry"] + side * t["distance"],
            tp2=t["entry"] + side * 2 * t["distance"],
        )

    if t["status"] not in ("OPEN", "PARTIAL"):
        return

    # Opening prices have a known ordering.
    if side * (bar.Open - t["tp2"]) >= 0:
        finish(t, "CLOSED", ts, 1.5)
        t["checked"] = ts
        return

    if side * (bar.Open - t["tp1"]) >= 0:
        t["status"] = "PARTIAL"

    partial = t["status"] == "PARTIAL"

    stop_hit = (
        bar.Low <= t["sl"]
        if side == 1
        else bar.High >= t["sl"]
    )

    if stop_hit:
        # An adverse opening gap can lose more than 1R.
        fill = (
            min(t["sl"], bar.Open)
            if side == 1
            else max(t["sl"], bar.Open)
        )
        stop_r = (
            side * (fill - t["entry"]) / t["distance"]
        )
        gross_r = (
            0.5 + 0.5 * stop_r
            if partial
            else stop_r
        )
        finish(t, "CLOSED", ts + 300, gross_r)

    else:
        hit1 = (
            bar.High >= t["tp1"]
            if side == 1
            else bar.Low <= t["tp1"]
        )
        hit2 = (
            bar.High >= t["tp2"]
            if side == 1
            else bar.Low <= t["tp2"]
        )

        if hit2:
            finish(t, "CLOSED", ts + 300, 1.5)
        elif hit1:
            t["status"] = "PARTIAL"

    t["checked"] = ts


def update_trade(trade):
    columns = [
        "entry", "sl", "tp1", "tp2", "status",
        "closed", "net_r", "checked", "review_from",
    ]
    assignments = ",".join(
        column + "=?" for column in columns
    )

    with connect() as db:
        db.execute(
            "UPDATE trades SET "
            + assignments
            + " WHERE id=?",
            [trade.get(column) for column in columns]
            + [trade["id"]],
        )


def track(pair, frame, now):
    events = []

    for trade in all_trades():
        if (
            trade["pair"] != pair
            or trade["status"] not in ACTIVE
        ):
            continue

        old_status = trade["status"]
        bars = frame.loc[
            frame.index > trade["checked"]
        ]

        if old_status == "REVIEW":
            if bars.empty:
                continue
            trade["status"] = trade["review_from"]

        for timestamp, bar in bars.iterrows():
            ts = int(timestamp)

            missing = range(
                trade["checked"] + 300,
                ts,
                300,
            )

            if (
                trade["status"] != "PENDING"
                and any(
                    market_open(pair, stamp)
                    for stamp in missing
                )
            ):
                trade["review_from"] = trade["status"]
                trade["status"] = "REVIEW"
                break

            process_bar(trade, ts, bar)

            if trade["status"] not in ("OPEN", "PARTIAL"):
                break

        if (
            trade["status"] == "PENDING"
            and now > trade["fill_at"] + 3600
        ):
            trade.update(
                status="CANCELLED",
                closed=now,
            )

        update_trade(trade)

        if trade["status"] != old_status:
            text = (
                f"Paper #{trade['id']} {pair.upper()}: "
                f"{trade['status']}"
            )
            if trade["entry"] is not None:
                text += (
                    f" | entry {trade['entry']:.5f}"
                    f", SL {trade['sl']:.5f}"
                    f", TP1 {trade['tp1']:.5f}"
                    f", TP2 {trade['tp2']:.5f}"
                    f", risk ${trade['risk']:.2f}"
                )
            if trade["net_r"] is not None:
                text += f" | net {trade['net_r']:.2f}R"

            events.append(text)

    return events


def report(days=None):
    now = int(time.time())
    rows = [
        trade
        for trade in all_trades()
        if days is None
        or (
            trade["closed"] or trade["created"]
        ) >= now - days * 86400
    ]

    lines = [
        "PAPER estimates; assumed costs, no broker fills."
    ]

    for pair in PAIRS:
        group = [
            trade for trade in rows
            if trade["pair"] == pair
        ]
        closed = [
            trade for trade in group
            if trade["net_r"] is not None
        ]
        wins = sum(
            trade["net_r"] > 0
            for trade in closed
        )
        rate = (
            f"{100 * wins / len(closed):.1f}%"
            if closed
            else "N/A"
        )
        pnl = sum(
            trade["risk"] * trade["net_r"]
            for trade in closed
        )
        active = sum(
            trade["status"] in ACTIVE
            for trade in group
        )

        lines.append(
            f"{pair.upper()}: closed {len(closed)}, "
            f"win {rate}, net ${pnl:.2f}, "
            f"active/review {active}"
        )

    return "\n".join(lines)


def chart(frame, pair):
    os.environ.setdefault(
        "MPLCONFIGDIR",
        str(Path(DB).resolve().parent / "mpl-cache"),
    )

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 3.5))
    try:
        for i, row in enumerate(frame.tail(40).itertuples()):
            color = (
                "green" if row.Close >= row.Open else "red"
            )
            ax.plot(
                [i, i], [row.Low, row.High],
                color=color, lw=1,
            )
            ax.plot(
                [i, i], [row.Open, row.Close],
                color=color, lw=4,
            )

        ax.set_title(
            f"{pair.upper()} | completed 1H candles | PAPER"
        )
        fig.tight_layout()

        output = io.BytesIO()
        fig.savefig(output, format="png", dpi=120)
        return output.getvalue()
    finally:
        plt.close(fig)


def backup_sheet():
    """Optional V12 snapshot; never imports old V11 results."""
    if (
        not os.getenv("SHEET_ID")
        or not os.getenv("GOOGLE_CREDENTIALS")
    ):
        return

    import gspread

    rows = all_trades()
    if not rows:
        return

    header = list(rows[0])
    client = gspread.service_account_from_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"])
    )
    book = client.open_by_key(os.environ["SHEET_ID"])

    try:
        sheet = book.worksheet("jarvis_v12")
        existing = sheet.row_values(1)
        if existing and existing != header:
            raise ValueError(
                "Unexpected jarvis_v12 worksheet header; "
                "refusing overwrite"
            )
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(
            title="jarvis_v12",
            rows=max(100, len(rows) + 1),
            cols=len(header),
        )

    if (
        sheet.row_count < len(rows) + 1
        or sheet.col_count < len(header)
    ):
        sheet.resize(
            rows=max(sheet.row_count, len(rows) + 1),
            cols=max(sheet.col_count, len(header)),
        )

    values = [header] + [
        [
            "" if row[key] is None else row[key]
            for key in header
        ]
        for row in rows
    ]
    sheet.update(
        values=values,
        range_name="A1",
        raw=True,
    )


async def permitted(update, admin=False):
    ok = (
        update.effective_chat
        and update.effective_chat.id in ALLOWED
    )
    if admin:
        ok = (
            ok
            and update.effective_user
            and update.effective_user.id in ADMINS
        )

    if not ok:
        await update.effective_message.reply_text(
            "Access denied."
        )

    return bool(ok)


async def broadcast(context, text, photo=None):
    with connect() as db:
        chats = [
            row[0]
            for row in db.execute(
                "SELECT chat_id FROM subscribers"
            )
            if row[0] in ALLOWED
        ]

    for chat_id in chats:
        try:
            if photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=io.BytesIO(photo),
                    caption=text,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                )
        except Exception:
            LOG.exception("Telegram delivery failed")


async def new_signal(pair, context):
    now = int(time.time())

    if not session_open(pair, now):
        return "Outside session / market closed."

    frame = await asyncio.to_thread(
        get_frame, pair, 60
    )
    setup = analyse(frame, now)

    if not setup:
        return (
            "WAIT: no qualified fresh "
            "closed-candle setup."
        )

    trade_id = await asyncio.to_thread(
        reserve,
        pair,
        int(frame.index[-1]),
        now,
        setup,
    )

    if not trade_id:
        return (
            "No new entry: duplicate, cooldown, "
            "exposure or risk limit."
        )

    side, price, distance, rsi = setup

    with connect() as db:
        risk = db.execute(
            "SELECT risk FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()[0]

    text = (
        f"PAPER #{trade_id} "
        f"{'BUY' if side == 1 else 'SELL'} "
        f"{pair.upper()}\n"
        f"Reference: {price:.5f} | RSI {rsi:.1f}\n"
        f"Paper risk: ${risk:.2f} before assumed costs.\n"
        f"SL distance: {distance:.5f}; "
        "50% TP1=1R, 50% TP2=2R.\n"
        "Model entry: next 5m open; "
        "excessive gap cancels.\n"
        "No broker order. Yahoo FX indicative / "
        "Coinbase BTC-USD.\n"
        "Manual broker sizing and prop-firm "
        "checks required."
    )

    photo = None
    try:
        photo = await asyncio.to_thread(
            chart, frame, pair
        )
    except Exception:
        LOG.exception("Chart unavailable; sending text")

    await broadcast(context, text, photo)
    return text


async def refresh(context):
    healthy = True
    rows = await asyncio.to_thread(all_trades)

    active_pairs = {
        trade["pair"]
        for trade in rows
        if trade["status"] in ACTIVE
    }

    for pair in active_pairs:
        try:
            frame = await asyncio.to_thread(
                get_frame, pair, 5
            )
            now = int(time.time())
            age = now - int(frame.index[-1]) - 300

            if market_open(pair, now) and age > 900:
                raise ValueError("Stale tracking data")

            events = await asyncio.to_thread(
                track, pair, frame, now
            )
            for event in events:
                await broadcast(context, event)

        except Exception:
            healthy = False
            LOG.exception(
                "Tracking failed for %s; "
                "new entries paused this cycle",
                pair,
            )

    return healthy


async def command(update, context):
    cmd = (
        update.effective_message.text
        .split()[0]
        .split("@")[0]
        .lstrip("/")
    )

    if not await permitted(
        update,
        cmd in ("export", "cleanup"),
    ):
        return

    message = update.effective_message

    async with LOCK:
        if cmd in ("start", "stop"):
            with connect() as db:
                if cmd == "start":
                    db.execute(
                        "INSERT OR IGNORE INTO "
                        "subscribers VALUES (?)",
                        (update.effective_chat.id,),
                    )
                else:
                    db.execute(
                        "DELETE FROM subscribers "
                        "WHERE chat_id=?",
                        (update.effective_chat.id,),
                    )

            await message.reply_text(
                "Subscribed."
                if cmd == "start"
                else "Unsubscribed."
            )

        elif cmd == "signal":
            pair = (
                context.args[0].lower()
                if context.args
                else "btcusd"
            )

            if pair not in PAIRS:
                await message.reply_text(
                    "Available: btcusd, eurusd, gbpusd. "
                    "XAUUSD disabled until a matching "
                    "spot feed is configured."
                )
                return

            if not await refresh(context):
                await message.reply_text(
                    "Tracking data unavailable; "
                    "new entries paused."
                )
                return

            text = await new_signal(pair, context)
            await message.reply_text(text)

        elif cmd in ("stats", "weekly", "result"):
            text = await asyncio.to_thread(
                report,
                7 if cmd == "weekly" else None,
            )
            await message.reply_text(text)

        elif cmd == "export":
            rows = await asyncio.to_thread(all_trades)

            if not rows:
                await message.reply_text("No trades.")
                return

            stream = io.StringIO()
            writer = csv.DictWriter(
                stream,
                fieldnames=list(rows[0]),
            )
            writer.writeheader()
            writer.writerows(rows)

            await message.reply_document(
                document=io.BytesIO(
                    stream.getvalue().encode()
                ),
                filename="jarvis_v12.csv",
            )

        elif cmd == "cleanup":
            with connect() as db:
                db.execute(
                    "UPDATE trades SET archived=1 "
                    "WHERE status IN "
                    "('CLOSED', 'CANCELLED')"
                )

            await message.reply_text(
                "Closed records archived. History and "
                "all active/review records retained."
            )

        else:
            await message.reply_text(
                "JARVIS V12 PAPER\n"
                "/start /stop\n"
                "/signal btcusd|eurusd|gbpusd\n"
                "/stats /weekly /result\n"
                "Admin: /export /cleanup\n\n"
                "Forex: London 08-12 OR "
                "(London 13-17 AND New York 08-12), "
                "DST-aware. BTC 24/7.\n"
                "New signals: first 10 minutes "
                "after hourly close.\n"
                "No guaranteed win rate; "
                "no broker execution."
            )


async def job(context):
    global HEARTBEAT
    HEARTBEAT = time.monotonic()

    async with LOCK:
        last_backup = context.application.bot_data.get(
            "last_backup", 0
        )
        if time.time() - last_backup >= 600:
            context.application.bot_data[
                "last_backup"
            ] = time.time()

            try:
                await asyncio.to_thread(backup_sheet)
            except Exception:
                LOG.exception(
                    "Optional Sheets backup failed; "
                    "SQLite remains authoritative"
                )

        if not await refresh(context):
            return

        with connect() as db:
            subscribed = any(
                row[0] in ALLOWED
                for row in db.execute(
                    "SELECT chat_id FROM subscribers"
                )
            )

        if not subscribed:
            return

        for pair in PAIRS:
            try:
                await new_signal(pair, context)
            except Exception:
                LOG.exception(
                    "Signal check failed for %s",
                    pair,
                )


async def on_error(update, context):
    LOG.error(
        "Unhandled bot error",
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__,
        ),
    )

    if update and update.effective_message:
        await update.effective_message.reply_text(
            "Operation failed. Check server logs; "
            "no broker order was placed."
        )


def main():
    from telegram.ext import Application, CommandHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if (
        not os.getenv("BOT_TOKEN")
        or not ADMINS
        or not ALLOWED
    ):
        raise SystemExit(
            "Set BOT_TOKEN, ADMIN_USER_IDS and "
            "ALLOWED_CHAT_IDS "
            "(comma-separated numeric IDs)."
        )

    finite = all(
        math.isfinite(value)
        for value in [
            BALANCE, RISK, MAX_OPEN,
            DAY_LIMIT, DD_LIMIT, COST_R,
        ]
    )
    valid = (
        BALANCE > 0
        and 0 < RISK <= MAX_OPEN <= DAY_LIMIT <= DD_LIMIT < 1
        and COST_R >= 0
    )

    if not finite or not valid:
        raise SystemExit("Invalid risk configuration")

    init_db()

    if os.getenv("PORT"):
        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                healthy = (
                    time.monotonic() - HEARTBEAT < 180
                )
                self.send_response(
                    200 if healthy else 503
                )
                self.end_headers()
                self.wfile.write(
                    b"Jarvis V12 paper bot"
                )

            def do_HEAD(self):
                healthy = (
                    time.monotonic() - HEARTBEAT < 180
                )
                self.send_response(
                    200 if healthy else 503
                )
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(
            ("0.0.0.0", int(os.environ["PORT"])),
            Health,
        )
        threading.Thread(
            target=server.serve_forever,
            daemon=True,
        ).start()

    app = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .build()
    )

    for name in (
        "start", "stop", "help", "signal",
        "stats", "weekly", "result",
        "export", "cleanup",
    ):
        app.add_handler(
            CommandHandler(name, command)
        )

    app.add_error_handler(on_error)

    if app.job_queue is None:
        raise SystemExit(
            "Install python-telegram-bot[job-queue]."
        )

    app.job_queue.run_repeating(
        job,
        interval=60,
        first=5,
        job_kwargs={
            "max_instances": 1,
            "coalesce": True,
        },
    )

    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
