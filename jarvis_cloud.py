"""Jarvis V12: one shared PAPER account; no broker/order APIs exist here."""

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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd
from telegram import Update
from telegram.error import Forbidden, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

LOG = logging.getLogger("jarvis")

PAIRS = {
    "btcusd": "BTC-USD",
    "eurusd": "EURUSD=X",
    "gbpusd": "GBPUSD=X",
}

ACTIVE = ("PENDING", "OPEN", "PARTIAL", "REVIEW")

T = "j12_trades"
E = "j12_events"
S = "j12_subscribers"
O = "j12_outbox"
M = "j12_meta"


@dataclass(frozen=True)
class Settings:
    token: str
    admins: frozenset
    chats: frozenset
    db_path: str = "/tmp/jarvis_v12.sqlite3"
    account: float = 5000
    risk: float = 0.0025
    max_open: float = 0.005
    daily: float = 0.01
    drawdown: float = 0.05
    cost_r: float = 0.05
    firm_timezone: str = "Europe/Prague"

    @classmethod
    def load(cls):
        def ids(name):
            values = frozenset(
                int(x.strip())
                for x in os.getenv(name, "").split(",")
                if x.strip()
            )
            if not values:
                raise ValueError(
                    f"{name} must contain numeric Telegram IDs"
                )
            return values

        cfg = cls(
            token=os.getenv("BOT_TOKEN", "").strip(),
            admins=ids("ADMIN_USER_IDS"),
            chats=ids("ALLOWED_CHAT_IDS"),
            db_path=os.getenv(
                "DB_PATH", "/tmp/jarvis_v12.sqlite3"
            ),
            account=float(os.getenv("ACCOUNT_SIZE", "5000")),
            risk=float(os.getenv("RISK_PCT", "0.25")) / 100,
            max_open=float(
                os.getenv("MAX_OPEN_RISK_PCT", "0.5")
            ) / 100,
            daily=float(
                os.getenv("DAILY_LOSS_PCT", "1")
            ) / 100,
            drawdown=float(
                os.getenv("MAX_DRAWDOWN_PCT", "5")
            ) / 100,
            cost_r=float(os.getenv("COST_R", "0.05")),
            firm_timezone=os.getenv(
                "FIRM_TIMEZONE", "Europe/Prague"
            ),
        )

        numbers = (
            cfg.account,
            cfg.risk,
            cfg.max_open,
            cfg.daily,
            cfg.drawdown,
            cfg.cost_r,
        )

        if (
            not cfg.token
            or not cfg.db_path
            or cfg.db_path == ":memory:"
        ):
            raise ValueError(
                "Set BOT_TOKEN and a file-based DB_PATH"
            )

        if not all(math.isfinite(x) for x in numbers) or not (
            cfg.account > 0
            and 0 < cfg.risk
            <= cfg.max_open
            <= cfg.daily
            <= cfg.drawdown
            < 1
            and cfg.cost_r >= 0
        ):
            raise ValueError("Invalid account/risk configuration")

        if any(x <= 0 for x in cfg.admins) or 0 in cfg.chats:
            raise ValueError(
                "Admin user IDs must be positive; "
                "chat IDs cannot be zero"
            )

        ZoneInfo(cfg.firm_timezone)
        return cfg


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


def in_session(pair, ts):
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


def json_get(url):
    request = Request(
        url,
        headers={
            "User-Agent": "JarvisPaper/12.0",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=15) as response:
        body = response.read(8_000_001)

    if len(body) > 8_000_000:
        raise ValueError("Market-data response too large")

    return json.loads(body)


def normalize(frame, seconds, now):
    if frame.empty:
        return pd.DataFrame(
            columns=["Open", "High", "Low", "Close"]
        )

    frame = frame.copy()
    index = pd.to_numeric(frame.index, errors="raise")

    if any(
        not math.isfinite(float(x))
        or float(x) != int(x)
        or int(x) < 0
        for x in index
    ):
        raise ValueError("Invalid candle timestamp")

    frame.index = index.astype("int64")

    # Exclude forming/future candles before validating unfinished OHLC.
    frame = frame.loc[
        frame.index + seconds <= now
    ].sort_index()

    if (
        frame.index.has_duplicates
        or any(int(x) % seconds for x in frame.index)
    ):
        raise ValueError(
            "Duplicate or misaligned candle timestamp"
        )

    frame = frame[
        ["Open", "High", "Low", "Close"]
    ].apply(pd.to_numeric, errors="coerce")

    finite = (
        frame.notna().all(axis=1)
        & ~frame.isin([math.inf, -math.inf]).any(axis=1)
    )

    valid = finite & (frame > 0).all(axis=1)
    valid &= (
        frame.High
        >= frame[["Open", "Low", "Close"]].max(axis=1)
    )
    valid &= (
        frame.Low
        <= frame[["Open", "High", "Close"]].min(axis=1)
    )

    if not valid.all():
        raise ValueError(
            "Rejected invalid/nonfinite completed OHLC"
        )

    return frame


def fetch(pair, minutes, now, since=None):
    """Coinbase catch-up uses one bounded window per cycle."""
    seconds = minutes * 60

    if pair == "btcusd":
        query = {"granularity": seconds}

        if since is not None:
            if since + seconds > now:
                return normalize(
                    pd.DataFrame(), seconds, now
                )

            query.update(
                start=datetime.fromtimestamp(
                    since, timezone.utc
                ).isoformat(),
                end=datetime.fromtimestamp(
                    min(now, since + 299 * seconds),
                    timezone.utc,
                ).isoformat(),
            )

        raw = json_get(
            "https://api.exchange.coinbase.com/products/"
            "BTC-USD/candles?"
            + urlencode(query)
        )

        if not isinstance(raw, list):
            raise ValueError("Coinbase data error")

        # Coinbase: time, low, high, open, close, volume.
        frame = pd.DataFrame(
            [
                (row[0], row[3], row[2], row[1], row[4])
                for row in raw
            ],
            columns=["ts", "Open", "High", "Low", "Close"],
        ).set_index("ts")

    else:
        query = urlencode({
            "range": "3mo" if minutes == 60 else "1mo",
            "interval": "1h" if minutes == 60 else "5m",
        })

        raw = json_get(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + quote(PAIRS[pair], safe="")
            + "?"
            + query
        )

        if (
            raw["chart"].get("error")
            or not raw["chart"].get("result")
        ):
            raise ValueError("Yahoo data error")

        result = raw["chart"]["result"][0]

        frame = pd.DataFrame(
            result["indicators"]["quote"][0],
            index=result["timestamp"],
        ).rename(columns=str.title)

        # All-null Yahoo rows mean "no quote".
        # Preserve the gap; never fill it with an invented price.
        frame = frame.loc[
            ~frame[
                ["Open", "High", "Low", "Close"]
            ].isna().all(axis=1)
        ]

    frame = normalize(frame, seconds, now)

    if since is not None:
        frame = frame.loc[frame.index >= since]

    return frame


def wilder_last(values, period=14):
    values = list(values)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            result * (period - 1) + value
        ) / period

    return result


def signal_check(pair, now):
    frame = fetch(pair, 60, now)

    if len(frame) < 200:
        return (
            frame,
            None,
            "WAIT: fewer than 200 completed hourly candles.",
        )

    candle = int(frame.index[-1])

    if not 0 <= now - (candle + 3600) < 600:
        return (
            frame,
            None,
            "WAIT: outside the first 10 minutes "
            "after a fresh hourly close.",
        )

    if not in_session(pair, now):
        return frame, None, "WAIT: outside the trading session."

    close = frame.Close

    e20 = float(
        close.ewm(span=20, adjust=False).mean().iloc[-1]
    )
    e50 = float(
        close.ewm(span=50, adjust=False).mean().iloc[-1]
    )

    change = close.diff().dropna()
    gain = wilder_last(change.clip(lower=0))
    loss = wilder_last(-change.clip(upper=0))

    if gain == loss == 0:
        rsi = 50
    elif loss == 0:
        rsi = 100
    else:
        rsi = 100 - 100 / (1 + gain / loss)

    true_range = pd.concat(
        [
            frame.High - frame.Low,
            (frame.High - close.shift()).abs(),
            (frame.Low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = wilder_last(true_range)
    price = float(close.iloc[-1])

    side = 0
    if e20 > e50 and 55 < rsi < 70:
        side = 1
    elif e20 < e50 and 30 < rsi < 45:
        side = -1

    if (
        not all(
            math.isfinite(x)
            for x in (price, atr, rsi, e20, e50)
        )
        or atr <= 0
        or abs(e20 - e50) / price < 0.0012
        or not side
    ):
        return (
            frame,
            None,
            f"WAIT: EMA/RSI/ATR filter; RSI {rsi:.1f}.",
        )

    setup = {
        "pair": pair,
        "candle": candle,
        "side": side,
        "reference": price,
        "distance": 1.5 * atr,
        "rsi": rsi,
    }

    return frame, setup, "Qualified paper setup."


def advance(trade, ts, bar):
    """
    Pure state transition.

    Returns accounting events:
        (timestamp, event_kind, delta_R)

    Original stop is retained after TP1.
    """
    t = trade
    events = []

    if t["status"] == "PENDING":
        invalid_entry = (
            ts != t["fill_at"]
            or not in_session(t["pair"], ts)
            or abs(bar.Open - t["reference"])
            > 0.25 * t["distance"]
        )

        if invalid_entry:
            t.update(
                status="CANCELLED",
                closed=ts,
                reason="Unverifiable/session/gapped entry",
            )
            return events

        t.update(
            status="OPEN",
            entry=float(bar.Open),
            remaining=1.0,
        )
        t.update(
            sl=t["entry"] - t["side"] * t["distance"],
            tp1=t["entry"] + t["side"] * t["distance"],
            tp2=t["entry"] + 2 * t["side"] * t["distance"],
        )

        # Charge estimated full-trade costs once, at entry.
        events.append((ts, "COST", -t["cost_r"]))

    side = t["side"]

    stop_hit = (
        bar.Low <= t["sl"]
        if side == 1
        else bar.High >= t["sl"]
    )
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

    # Strict stop-first whenever both SL and a target are touched.
    if stop_hit:
        fill = (
            min(bar.Open, t["sl"])
            if side == 1
            else max(bar.Open, t["sl"])
        )

        delta = (
            t["remaining"]
            * side
            * (fill - t["entry"])
            / t["distance"]
        )

        events.append((ts + 300, "SL", delta))
        t["gross_r"] += delta

        t.update(
            status="CLOSED",
            remaining=0.0,
            closed=ts + 300,
            reason="SL",
        )

    else:
        if hit1 and t["status"] == "OPEN":
            t.update(status="PARTIAL", remaining=0.5)
            t["gross_r"] += 0.5
            events.append((ts + 300, "TP1", 0.5))

        if hit2:
            delta = t["remaining"] * 2
            t["gross_r"] += delta

            events.append((ts + 300, "TP2", delta))

            t.update(
                status="CLOSED",
                remaining=0.0,
                closed=ts + 300,
                reason="TP2",
            )

    t.update(
        checked=ts,
        mark=float(bar.Close),
        mark_ts=ts + 300,
    )

    if t["status"] == "CLOSED":
        t["net_r"] = t["gross_r"] - t["cost_r"]

    return events


class Store:
    """Public methods run through asyncio.to_thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.firm_timezone)

    @contextmanager
    def db(self):
        db = sqlite3.connect(
            self.cfg.db_path,
            timeout=10,
        )
        db.row_factory = sqlite3.Row

        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        Path(self.cfg.db_path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")

            # Namespaced tables preserve existing V11/V12 tables.
            # Incompatible legacy accounting is not silently imported.
            db.executescript(f"""
                CREATE TABLE IF NOT EXISTS {M}(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS {S}(
                    chat_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS {T}(
                    id INTEGER PRIMARY KEY,
                    pair TEXT NOT NULL,
                    candle INTEGER NOT NULL,
                    created INTEGER NOT NULL,
                    fill_at INTEGER NOT NULL,
                    checked INTEGER NOT NULL,
                    side INTEGER NOT NULL,
                    reference REAL NOT NULL,
                    distance REAL NOT NULL,
                    risk REAL NOT NULL,
                    cost_r REAL NOT NULL,
                    status TEXT NOT NULL,
                    review_from TEXT,
                    entry REAL,
                    sl REAL,
                    tp1 REAL,
                    tp2 REAL,
                    remaining REAL NOT NULL DEFAULT 1,
                    gross_r REAL NOT NULL DEFAULT 0,
                    mark REAL,
                    mark_ts INTEGER,
                    closed INTEGER,
                    net_r REAL,
                    reason TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(pair, candle)
                );

                CREATE TABLE IF NOT EXISTS {E}(
                    id INTEGER PRIMARY KEY,
                    trade_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    pnl REAL NOT NULL,
                    UNIQUE(trade_id, ts, kind)
                );

                CREATE TABLE IF NOT EXISTS {O}(
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    delivered INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_try INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS j12_status_idx
                    ON {T}(status);

                CREATE INDEX IF NOT EXISTS j12_event_time_idx
                    ON {E}(ts);
            """)

            fresh = not db.execute(
                f"SELECT 1 FROM {M} "
                "WHERE key='initial_balance'"
            ).fetchone()

            if fresh:
                legacy = db.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='trades'"
                ).fetchone()[0]

                db.executemany(
                    f"INSERT INTO {M} VALUES (?, ?)",
                    [
                        ("initial_balance", str(self.cfg.account)),
                        ("ledger_id", uuid.uuid4().hex[:12]),
                        ("created", str(int(time.time()))),
                        ("legacy_preserved", str(legacy)),
                    ],
                )

            balance = float(
                db.execute(
                    f"SELECT value FROM {M} "
                    "WHERE key='initial_balance'"
                ).fetchone()[0]
            )
            ledger = db.execute(
                f"SELECT value FROM {M} "
                "WHERE key='ledger_id'"
            ).fetchone()[0]
            legacy = db.execute(
                f"SELECT value FROM {M} "
                "WHERE key='legacy_preserved'"
            ).fetchone()[0]

        LOG.info(
            "Ledger %s initialized; balance %.2f; "
            "legacy preserved=%s",
            ledger,
            balance,
            legacy,
        )

        if balance != self.cfg.account:
            LOG.warning(
                "ACCOUNT_SIZE differs from existing ledger; "
                "using stored initial balance %.2f",
                balance,
            )

        return ledger

    def notify(self, db, text, now, extra=None):
        chats = {
            row[0]
            for row in db.execute(
                f"SELECT chat_id FROM {S}"
            )
        } & self.cfg.chats

        if extra in self.cfg.chats:
            chats.add(extra)

        db.executemany(
            f"INSERT INTO {O}(chat_id, text, created) "
            "VALUES (?, ?, ?)",
            [(chat_id, text, now) for chat_id in chats],
        )

    def subscribe(self, chat_id, enabled):
        with self.db() as db:
            if enabled:
                db.execute(
                    f"INSERT OR IGNORE INTO {S} VALUES (?)",
                    (chat_id,),
                )
            else:
                db.execute(
                    f"DELETE FROM {S} WHERE chat_id=?",
                    (chat_id,),
                )
                db.execute(
                    f"DELETE FROM {O} "
                    "WHERE chat_id=? AND delivered IS NULL",
                    (chat_id,),
                )

    def active(self):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {T} WHERE status IN "
                    "('PENDING','OPEN','PARTIAL','REVIEW')"
                )
            ]

    def subscribed(self):
        with self.db() as db:
            return any(
                row[0] in self.cfg.chats
                for row in db.execute(
                    f"SELECT chat_id FROM {S}"
                )
            )

    def reserve(self, setup, now, requester=None):
        pair = setup["pair"]

        levels = (
            setup["reference"]
            - setup["side"] * setup["distance"],
            setup["reference"]
            + 2 * setup["side"] * setup["distance"],
        )

        if (
            setup["distance"] <= 0
            or any(
                not math.isfinite(x) or x <= 0
                for x in levels
            )
        ):
            return "WAIT: invalid projected price levels."

        if (
            not in_session(pair, now)
            or not 0 <= now - setup["candle"] - 3600 < 600
        ):
            return "WAIT: setup/session expired during processing."

        fill_at = (now // 300 + 1) * 300

        if not in_session(pair, fill_at):
            return "WAIT: next model entry is outside session."

        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")

            active = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {T} WHERE status IN "
                    "('PENDING','OPEN','PARTIAL','REVIEW')"
                )
            ]

            if any(
                trade["status"] == "REVIEW"
                for trade in active
            ):
                return "Paused: unresolved REVIEW trade."

            if any(
                trade["pair"] == pair
                for trade in active
            ):
                return "Existing active paper trade for this pair."

            duplicate = db.execute(
                f"SELECT 1 FROM {T} "
                "WHERE pair=? AND (candle=? OR created>?)",
                (pair, setup["candle"], now - 21600),
            ).fetchone()

            if duplicate:
                return (
                    "Duplicate/cooldown: already processed; "
                    "cooldown is six hours."
                )

            base = float(
                db.execute(
                    f"SELECT value FROM {M} "
                    "WHERE key='initial_balance'"
                ).fetchone()[0]
            )

            cash = base + db.execute(
                f"SELECT COALESCE(SUM(pnl),0) FROM {E}"
            ).fetchone()[0]

            day_start = datetime.fromtimestamp(
                now, self.tz
            ).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()

            losses = -db.execute(
                f"SELECT COALESCE(SUM(pnl),0) FROM {E} "
                "WHERE ts>=? AND pnl<0",
                (day_start,),
            ).fetchone()[0]

            exposure = sum(
                trade["risk"] * (1 + trade["cost_r"])
                if trade["status"] == "PENDING"
                else trade["risk"] * trade["remaining"]
                for trade in active
            )

            risk = min(base, cash) * self.cfg.risk
            worst = exposure + risk * (1 + self.cfg.cost_r)

            if (
                risk <= 0
                or worst > base * self.cfg.max_open + 1e-9
                or losses + worst > base * self.cfg.daily + 1e-9
                or cash - worst
                < base * (1 - self.cfg.drawdown) - 1e-9
            ):
                return (
                    "Blocked by open-risk, daily-loss or "
                    "fixed-initial drawdown limit."
                )

            cursor = db.execute(
                f"""
                    INSERT INTO {T}(
                        pair, candle, created, fill_at, checked,
                        side, reference, distance, risk,
                        cost_r, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair,
                    setup["candle"],
                    now,
                    fill_at,
                    fill_at - 300,
                    setup["side"],
                    setup["reference"],
                    setup["distance"],
                    risk,
                    self.cfg.cost_r,
                    "PENDING",
                ),
            )

            text = (
                f"PAPER #{cursor.lastrowid} "
                f"{'BUY' if setup['side'] == 1 else 'SELL'} "
                f"{pair.upper()}\n"
                f"Reference {setup['reference']:.5f}; "
                f"SL distance {setup['distance']:.5f}; "
                f"RSI {setup['rsi']:.1f}\n"
                f"Risk ${risk:.2f} + "
                f"{self.cfg.cost_r:.2f}R costs. "
                "Pending next 5m open.\n"
                "50% at 1R; 50% at 2R; "
                "original stop retained. No broker order."
            )

            self.notify(db, text, now, requester)
            return text

    def track(self, pair, frame, now):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")

            trades = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {T} WHERE pair=? "
                    "AND status IN "
                    "('PENDING','OPEN','PARTIAL','REVIEW')",
                    (pair,),
                )
            ]

            for trade in trades:
                old_status = trade["status"]

                bars = frame.loc[
                    frame.index > trade["checked"]
                ]

                if old_status == "REVIEW":
                    if bars.empty:
                        continue
                    trade["status"] = trade["review_from"]
                    trade["reason"] = None

                for index, bar in bars.iterrows():
                    ts = int(index)

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
                        trade.update(
                            review_from=trade["status"],
                            status="REVIEW",
                            reason="Missing 5m history",
                        )
                        break

                    events = advance(trade, ts, bar)

                    for stamp, kind, delta_r in events:
                        db.execute(
                            f"INSERT INTO {E}"
                            "(trade_id, ts, kind, pnl) "
                            "VALUES (?, ?, ?, ?)",
                            (
                                trade["id"],
                                stamp,
                                kind,
                                delta_r * trade["risk"],
                            ),
                        )

                    if trade["status"] in (
                        "CLOSED", "CANCELLED"
                    ):
                        break

                if (
                    trade["status"] == "PENDING"
                    and now >= trade["fill_at"] + 3600
                ):
                    trade.update(
                        status="CANCELLED",
                        closed=now,
                        reason="Entry data unavailable for one hour",
                    )

                if (
                    trade["status"] in ("OPEN", "PARTIAL")
                    and market_open(pair, now)
                    and now - trade["checked"] - 300 > 900
                    and bars.empty
                ):
                    trade.update(
                        review_from=trade["status"],
                        status="REVIEW",
                        reason="Missing recent 5m history",
                    )

                columns = [
                    "status",
                    "review_from",
                    "entry",
                    "sl",
                    "tp1",
                    "tp2",
                    "remaining",
                    "gross_r",
                    "mark",
                    "mark_ts",
                    "closed",
                    "net_r",
                    "checked",
                    "reason",
                ]

                assignments = ",".join(
                    column + "=?" for column in columns
                )

                db.execute(
                    f"UPDATE {T} SET "
                    + assignments
                    + " WHERE id=?",
                    [trade.get(column) for column in columns]
                    + [trade["id"]],
                )

                if trade["status"] != old_status:
                    text = (
                        f"PAPER #{trade['id']} "
                        f"{pair.upper()}: {trade['status']}"
                    )

                    if trade["entry"] is not None:
                        text += (
                            f"\nEntry {trade['entry']:.5f}; "
                            f"SL {trade['sl']:.5f}; "
                            f"TP1 {trade['tp1']:.5f}; "
                            f"TP2 {trade['tp2']:.5f}"
                        )

                    if trade["net_r"] is not None:
                        text += (
                            f"\nNet {trade['net_r']:.2f}R / "
                            f"${trade['net_r'] * trade['risk']:.2f}"
                        )

                    if trade["reason"]:
                        text += "\n" + trade["reason"]

                    self.notify(db, text, now)

    def summary(self, days=None):
        now = int(time.time())
        cutoff = now - days * 86400 if days else 0

        with self.db() as db:
            base = float(
                db.execute(
                    f"SELECT value FROM {M} "
                    "WHERE key='initial_balance'"
                ).fetchone()[0]
            )
            ledger = db.execute(
                f"SELECT value FROM {M} "
                "WHERE key='ledger_id'"
            ).fetchone()[0]
            legacy = db.execute(
                f"SELECT value FROM {M} "
                "WHERE key='legacy_preserved'"
            ).fetchone()[0]

            cash = base + db.execute(
                f"SELECT COALESCE(SUM(pnl),0) FROM {E}"
            ).fetchone()[0]

            booked = db.execute(
                f"SELECT COALESCE(SUM(pnl),0) FROM {E} "
                "WHERE ts>=?",
                (cutoff,),
            ).fetchone()[0]

            rows = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {T}"
                )
            ]

        floating = sum(
            trade["remaining"]
            * trade["side"]
            * (trade["mark"] - trade["entry"])
            / trade["distance"]
            * trade["risk"]
            for trade in rows
            if trade["status"] in ACTIVE
            and trade["entry"] is not None
            and trade["mark"] is not None
        )

        lines = [
            f"PAPER ledger {ledger}"
            + (" | last 7 days" if days else " | all time"),
            f"Cash ${cash:.2f}; "
            f"last-mark equity ~${cash + floating:.2f}",
            f"Booked PnL in window ${booked:.2f} "
            "(includes fees/partials).",
        ]

        for pair in PAIRS:
            closed = [
                trade
                for trade in rows
                if trade["pair"] == pair
                and trade["status"] == "CLOSED"
                and trade["closed"] >= cutoff
            ]

            wins = sum(
                trade["net_r"] > 0
                for trade in closed
            )
            winrate = (
                f"{wins / len(closed) * 100:.1f}%"
                if closed
                else "N/A"
            )

            active = sum(
                trade["pair"] == pair
                and trade["status"] in ACTIVE
                for trade in rows
            )

            lines.append(
                f"{pair.upper()}: closed {len(closed)}, "
                f"win {winrate}, active/review {active}"
            )

        if any(
            trade["status"] == "REVIEW"
            for trade in rows
        ):
            lines.append(
                "REVIEW: equity marks may be stale; "
                "new entries blocked."
            )

        if any(
            trade["status"] in ACTIVE
            and trade["mark_ts"]
            and market_open(trade["pair"], now)
            and now - trade["mark_ts"] > 900
            for trade in rows
        ):
            lines.append(
                "Warning: some equity marks are "
                "older than 15 minutes."
            )

        if legacy == "1":
            lines.append(
                "Legacy tables preserved separately; "
                "not included in this fresh ledger."
            )

        return (
            "\n".join(lines)
            + "\nOHLC estimates, assumed costs; no broker fills."
        )

    def export(self):
        with self.db() as db:
            cursor = db.execute(
                f"SELECT * FROM {T} ORDER BY id"
            )

            output = io.StringIO()
            writer = csv.writer(output)

            writer.writerow(
                [column[0] for column in cursor.description]
            )
            writer.writerows(cursor)

            return output.getvalue().encode("utf-8-sig")

    def cleanup(self):
        with self.db() as db:
            count = db.execute(
                f"UPDATE {T} SET archived=1 "
                "WHERE status IN ('CLOSED','CANCELLED') "
                "AND archived=0"
            ).rowcount

            db.execute(
                f"DELETE FROM {O} "
                "WHERE delivered IS NOT NULL "
                "AND delivered<?",
                (int(time.time()) - 30 * 86400,),
            )

        return (
            f"Archived {count} terminal records; "
            "all trades and accounting history retained."
        )

    def pending_messages(self, now):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {O} "
                    "WHERE delivered IS NULL AND next_try<=? "
                    "ORDER BY id LIMIT 20",
                    (now,),
                )
            ]

    def delivery(
        self,
        message,
        now,
        delay=None,
        forbidden=False,
    ):
        with self.db() as db:
            if forbidden:
                db.execute(
                    f"DELETE FROM {S} WHERE chat_id=?",
                    (message["chat_id"],),
                )
                db.execute(
                    f"DELETE FROM {O} "
                    "WHERE chat_id=? AND delivered IS NULL",
                    (message["chat_id"],),
                )

            elif delay is None:
                db.execute(
                    f"UPDATE {O} SET delivered=? WHERE id=?",
                    (now, message["id"]),
                )

            else:
                db.execute(
                    f"UPDATE {O} "
                    "SET attempts=attempts+1, next_try=? "
                    "WHERE id=?",
                    (now + delay, message["id"]),
                )


def make_chart(frame, pair):
    os.environ.setdefault(
        "MPLCONFIGDIR",
        "/tmp/jarvis-mpl",
    )

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 3.5))

    try:
        for i, row in enumerate(
            frame.tail(40).itertuples()
        ):
            color = (
                "green"
                if row.Close >= row.Open
                else "red"
            )

            ax.plot(
                [i, i],
                [row.Low, row.High],
                color=color,
                lw=1,
            )
            ax.plot(
                [i, i],
                [row.Open, row.Close],
                color=color,
                lw=4,
            )

        ax.set_title(
            f"{pair.upper()} | completed 1H candles | PAPER"
        )
        fig.tight_layout()

        output = io.BytesIO()
        fig.savefig(output, format="png", dpi=110)

        return output.getvalue()

    finally:
        plt.close(fig)


class Engine:
    def __init__(self, cfg, health):
        self.cfg = cfg
        self.health = health
        self.store = Store(cfg)

        self.pipeline = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.chart_lock = asyncio.Lock()

        # Network slots are separate from database work.
        self.workers = asyncio.Semaphore(3)

    async def run(self, function, *args):
        if function in (fetch, signal_check):
            async with self.workers:
                return await asyncio.to_thread(
                    function, *args
                )

        return await asyncio.to_thread(
            function, *args
        )

    async def refresh(self):
        trades = await self.run(self.store.active)
        cursors = {}

        for trade in trades:
            pair = trade["pair"]
            start = trade["checked"] + 300

            cursors[pair] = min(
                cursors.get(pair, start),
                start,
            )

        now = int(time.time())

        results = await asyncio.gather(
            *(
                self.run(fetch, pair, 5, now, since)
                for pair, since in cursors.items()
            ),
            return_exceptions=True,
        )

        healthy = True

        for pair, result in zip(cursors, results):
            if isinstance(result, BaseException):
                LOG.warning(
                    "Tracking fetch failed for %s: %s",
                    pair,
                    type(result).__name__,
                )
                healthy = False
                continue

            await self.run(
                self.store.track,
                pair,
                result,
                now,
            )

        for trade in await self.run(self.store.active):
            stale = (
                trade["status"] != "PENDING"
                and market_open(trade["pair"], now)
                and now - trade["checked"] - 300 > 900
            )

            if trade["status"] == "REVIEW" or stale:
                healthy = False

        return healthy

    async def check(self, pair, requester=None):
        frame, setup, reason = await self.run(
            signal_check,
            pair,
            int(time.time()),
        )

        if setup:
            reason = await self.run(
                self.store.reserve,
                setup,
                int(time.time()),
                requester,
            )

        return frame, reason

    async def flush(self, bot):
        if self.delivery_lock.locked():
            return

        async with self.delivery_lock:
            messages = await self.run(
                self.store.pending_messages,
                int(time.time()),
            )

            for message in messages:
                if message["chat_id"] not in self.cfg.chats:
                    await self.run(
                        self.store.delivery,
                        message,
                        int(time.time()),
                        None,
                        True,
                    )
                    continue

                try:
                    await bot.send_message(
                        chat_id=message["chat_id"],
                        text=(
                            f"[event {message['id']}] "
                            f"{message['text']}"
                        ),
                    )

                except Forbidden:
                    await self.run(
                        self.store.delivery,
                        message,
                        int(time.time()),
                        None,
                        True,
                    )

                except Exception as exc:
                    if isinstance(exc, RetryAfter):
                        retry = exc.retry_after
                    else:
                        retry = min(
                            3600,
                            30 * 2 ** min(
                                message["attempts"], 7
                            ),
                        )

                    if hasattr(retry, "total_seconds"):
                        retry = retry.total_seconds()

                    await self.run(
                        self.store.delivery,
                        message,
                        int(time.time()),
                        max(1, int(retry)),
                    )

                    LOG.warning(
                        "Notification %s failed: %s",
                        message["id"],
                        type(exc).__name__,
                    )
                    break

                else:
                    await self.run(
                        self.store.delivery,
                        message,
                        int(time.time()),
                    )

    async def tick(self, context):
        self.health.beat = time.monotonic()

        if not self.pipeline.locked():
            async with self.pipeline:
                healthy = await self.refresh()

                if (
                    healthy
                    and await self.run(self.store.subscribed)
                ):
                    results = await asyncio.gather(
                        *(
                            self.check(pair)
                            for pair in PAIRS
                        ),
                        return_exceptions=True,
                    )

                    for pair, result in zip(PAIRS, results):
                        if isinstance(result, BaseException):
                            LOG.warning(
                                "Signal check failed for %s: %s",
                                pair,
                                type(result).__name__,
                            )

        await self.flush(context.bot)

    async def allowed(self, update, admin=False):
        ok = (
            update.effective_chat is not None
            and update.effective_chat.id in self.cfg.chats
        )

        if admin:
            ok = (
                ok
                and update.effective_user is not None
                and update.effective_user.id in self.cfg.admins
            )

        if not ok:
            await update.effective_message.reply_text(
                "Access denied."
            )

        return ok

    async def command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        command = (
            update.effective_message.text
            .split()[0]
            .split("@")[0]
            .lstrip("/")
            .lower()
        )

        if not await self.allowed(
            update,
            command in ("export", "cleanup"),
        ):
            return

        message = update.effective_message

        if command in ("start", "stop"):
            await self.run(
                self.store.subscribe,
                update.effective_chat.id,
                command == "start",
            )

            await message.reply_text(
                "Subscribed to PAPER signals. /help"
                if command == "start"
                else (
                    "Unsubscribed; existing model trades "
                    "continue tracking."
                )
            )

        elif command == "signal":
            aliases = {
                "btc-usd": "btcusd",
                "eurusd=x": "eurusd",
                "gbpusd=x": "gbpusd",
            }

            argument = (
                context.args[0].lower()
                if context.args
                else "btcusd"
            )
            pair = aliases.get(argument, argument)

            if pair not in PAIRS:
                await message.reply_text(
                    "Use /signal btcusd, eurusd or gbpusd. "
                    "XAUUSD is disabled."
                )
                return

            if self.pipeline.locked():
                await message.reply_text(
                    "Another check is running; retry shortly. "
                    "/stats remains available."
                )
                return

            await message.reply_text(
                "Checking fresh completed candles: "
                f"{pair.upper()}..."
            )

            async with self.pipeline:
                if not await self.refresh():
                    await message.reply_text(
                        "New entries paused: tracking stale, "
                        "catching up, unavailable or in REVIEW."
                    )
                    return

                frame, reason = await self.check(
                    pair,
                    update.effective_chat.id,
                )

            await message.reply_text(reason)

            if not frame.empty:
                try:
                    async with self.chart_lock:
                        photo = await self.run(
                            make_chart,
                            frame,
                            pair,
                        )

                    await message.reply_photo(
                        photo=io.BytesIO(photo),
                        caption=(
                            "Completed hourly candles; PAPER only."
                        ),
                    )

                except Exception as exc:
                    LOG.warning(
                        "Chart failed: %s",
                        type(exc).__name__,
                    )

            await self.flush(context.bot)

        elif command in ("stats", "weekly"):
            text = await self.run(
                self.store.summary,
                7 if command == "weekly" else None,
            )
            await message.reply_text(text)

        elif command == "export":
            data = await self.run(self.store.export)

            await message.reply_document(
                document=io.BytesIO(data),
                filename="jarvis_v12_paper.csv",
            )

        elif command == "cleanup":
            await message.reply_text(
                await self.run(self.store.cleanup)
            )

        else:
            await message.reply_text(
                "JARVIS V12 PAPER — one shared model account.\n"
                "/start /stop\n"
                "/signal btcusd|eurusd|gbpusd\n"
                "/stats /weekly\n"
                "Admin: /export /cleanup\n\n"
                "Signals: first 10 minutes after 1H close.\n"
                "Tracking: completed 5m candles.\n"
                "Forex: London 08-12 OR "
                "(London 13-17 AND NY 08-12), DST-aware.\n"
                "BTC: 24/7.\n"
                "SL=1.5 ATR. 50% at 1R, remaining at 2R; "
                "original SL retained.\n"
                "Strict stop-first if both touched.\n"
                "Missing history => REVIEW.\n"
                "Paper estimates only; no broker orders "
                "or guaranteed win rate."
            )

    async def startup(self, app):
        await self.run(self.store.initialize)

        self.health.ready = True
        self.health.beat = time.monotonic()

        app.job_queue.run_repeating(
            self.tick,
            interval=60,
            first=2,
            job_kwargs={
                "max_instances": 1,
                "coalesce": True,
            },
        )

    async def shutdown(self, app):
        self.health.ready = False


@dataclass
class Health:
    ready: bool = False
    beat: float = 0


def health_server(health):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/healthz"):
                self.send_error(404)
                return

            ok = (
                health.ready
                and time.monotonic() - health.beat < 300
            )

            body = (
                b"Jarvis V12 PAPER: ready\n"
                if ok
                else b"Starting or scheduler stalled\n"
            )

            self.send_response(200 if ok else 503)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(body)),
            )
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            ok = (
                health.ready
                and time.monotonic() - health.beat < 300
            )
            self.send_response(200 if ok else 503)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.getenv("PORT", "10000"))),
        Handler,
    )

    threading.Thread(
        target=server.serve_forever,
        daemon=True,
    ).start()

    return server


async def error_handler(update, context):
    # Avoid logging Telegram URLs/tokens or exposing raw errors.
    LOG.error(
        "Handler/job failed: %s",
        type(context.error).__name__,
    )

    if (
        isinstance(update, Update)
        and update.effective_message
    ):
        try:
            await update.effective_message.reply_text(
                "Check failed safely; no broker orders exist. "
                "Retry later or check server logs."
            )
        except Exception:
            LOG.warning(
                "Could not deliver error notification"
            )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cfg = Settings.load()
    health = Health()
    engine = Engine(cfg, health)

    app = (
        Application.builder()
        .token(cfg.token)
        .concurrent_updates(8)
        .post_init(engine.startup)
        .post_shutdown(engine.shutdown)
        .build()
    )

    if app.job_queue is None:
        raise RuntimeError(
            "Install python-telegram-bot[job-queue]"
        )

    for command in (
        "start",
        "stop",
        "help",
        "signal",
        "stats",
        "weekly",
        "export",
        "cleanup",
    ):
        app.add_handler(
            CommandHandler(command, engine.command)
        )

    app.add_error_handler(error_handler)
    server = health_server(health)

    try:
        app.run_polling(
            drop_pending_updates=False,
            allowed_updates=["message"],
        )
    finally:
        health.ready = False
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
