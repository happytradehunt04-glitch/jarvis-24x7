"""Jarvis V12 PAPER: resilient HTTP, local process lock, Telegram UX."""

import asyncio
import copy
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, parse_qs
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from telegram import Update
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    RetryAfter,
    TimedOut,
)
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

DISPLAY_TZ = ZoneInfo("Asia/Kolkata")
PLOT_LOCK = threading.Lock()

HTTP_LOCAL = threading.local()
CACHE_LOCK = threading.Lock()
YAHOO_CACHE = {}
YAHOO_RETRY_AFTER = {}
YAHOO_FALLBACK_MAX_AGE = 900

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@contextmanager
def single_instance(path="/tmp/jarvis_bot.lock"):
    """
    Linux local-filesystem lock.

    Separate Render containers do not share this lock.
    """
    import fcntl

    fd = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    acquired = False

    try:
        try:
            fcntl.flock(
                fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            acquired = True
        except BlockingIOError:
            LOG.warning("Another instance is running, exiting")

        if acquired:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())

        yield acquired

    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)

        os.close(fd)

        # Do not unlink: unlinking can introduce an inode race.


def http_session():
    # requests.Session is thread-local, not shared between workers.
    if not hasattr(HTTP_LOCAL, "session"):
        HTTP_LOCAL.session = requests.Session()
        HTTP_LOCAL.session.headers.update(BROWSER_HEADERS)

        ca = (
            os.getenv("REQUESTS_CA_BUNDLE")
            or os.getenv("SSL_CERT_FILE")
        )
        if ca:
            HTTP_LOCAL.session.verify = ca

    return HTTP_LOCAL.session


def closed_yahoo_snapshot(payload, url, observed_at):
    """
    Cache only candles already complete at observation time.

    Never promote a cached partial candle to a completed candle later.
    """
    result = copy.deepcopy(payload)
    chart = result.get("chart", {})

    if chart.get("error") or not chart.get("result"):
        raise ValueError("Yahoo returned a JSON API error")

    interval = parse_qs(
        urlsplit(url).query
    ).get("interval", [""])[0]

    seconds = {
        "1h": 3600,
        "5m": 300,
    }.get(interval)

    if seconds is None:
        raise ValueError("Unsupported Yahoo cache interval")

    for item in chart["result"]:
        stamps = item["timestamp"]

        keep = [
            i
            for i, ts in enumerate(stamps)
            if int(ts) + seconds <= observed_at
        ]

        item["timestamp"] = [
            stamps[i] for i in keep
        ]

        for group in item["indicators"].values():
            for values in group:
                for key, series in values.items():
                    if isinstance(series, list):
                        if len(series) != len(stamps):
                            raise ValueError(
                                "Mismatched Yahoo quote lengths"
                            )
                        values[key] = [
                            series[i] for i in keep
                        ]

        frame = pd.DataFrame(
            item["indicators"]["quote"][0],
            index=item["timestamp"],
        ).rename(columns=str.title)

        if not frame.empty:
            frame = frame.loc[
                ~frame[
                    ["Open", "High", "Low", "Close"]
                ].isna().all(axis=1)
            ]

            # Invalid data must not overwrite the last valid cache.
            normalize(frame, seconds, observed_at)

    return result


def yahoo_cached(url):
    with CACHE_LOCK:
        entry = YAHOO_CACHE.get(url)

        if (
            entry
            and time.monotonic() - entry[0]
            <= YAHOO_FALLBACK_MAX_AGE
        ):
            return copy.deepcopy(entry[1])

    return None


def json_get(url):
    host = (
        urlsplit(url).hostname or ""
    ).lower()

    yahoo = host in {
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
    }

    if yahoo:
        with CACHE_LOCK:
            entry = YAHOO_CACHE.get(url)
            cooldown = YAHOO_RETRY_AFTER.get(url, 0)

            # Short successful-response cache reduces duplicate requests.
            if (
                entry
                and time.monotonic() - entry[0] < 30
            ):
                return copy.deepcopy(entry[1])

        if time.monotonic() < cooldown:
            cached = yahoo_cached(url)

            if cached is not None:
                LOG.warning(
                    "Yahoo cooldown: using cached CLOSED candles; "
                    "freshness gates remain active"
                )
                return cached

            raise RuntimeError(
                "Yahoo retry cooldown; no usable cache"
            )

    last_error = None

    # Initial request + three Yahoo retries: 2s, 4s, 8s.
    attempts = 4 if yahoo else 1

    for attempt in range(attempts):
        observed = int(time.time())
        retry_after = 0
        transient = True
        status = None

        try:
            with http_session().get(
                url,
                timeout=(5, 15),
                stream=True,
            ) as response:
                status = response.status_code

                transient = status in (
                    403,
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                )

                header = response.headers.get(
                    "Retry-After", ""
                )

                if header.isdigit():
                    retry_after = min(30, int(header))

                response.raise_for_status()

                parts = []
                size = 0

                for part in response.iter_content(
                    chunk_size=65536
                ):
                    size += len(part)

                    if size > 8_000_000:
                        raise ValueError(
                            "Market-data response too large"
                        )

                    parts.append(part)

                payload = json.loads(b"".join(parts))

            if yahoo:
                safe = closed_yahoo_snapshot(
                    payload,
                    url,
                    observed,
                )

                with CACHE_LOCK:
                    YAHOO_CACHE[url] = (
                        time.monotonic(),
                        safe,
                    )
                    YAHOO_RETRY_AFTER.pop(url, None)

                return copy.deepcopy(safe)

            return payload

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            last_error = exc

            code = getattr(
                getattr(exc, "response", None),
                "status_code",
                status,
            )

            LOG.warning(
                "Market data %s failed: HTTP=%s "
                "type=%s attempt=%s/%s",
                host,
                code,
                type(exc).__name__,
                attempt + 1,
                attempts,
            )

            if not yahoo:
                break

            if (
                isinstance(exc, requests.HTTPError)
                and not transient
            ):
                break

            if attempt < attempts - 1:
                # Executed in a worker thread, not the event loop.
                time.sleep(
                    max(
                        2 ** (attempt + 1),
                        retry_after,
                    )
                )

    if yahoo:
        with CACHE_LOCK:
            YAHOO_RETRY_AFTER[url] = (
                time.monotonic() + 60
            )

        cached = yahoo_cached(url)

        if cached is not None:
            LOG.warning(
                "Yahoo failed: using cached CLOSED candles; "
                "no timestamps are advanced"
            )
            return cached

    if last_error is not None:
        raise last_error

    raise RuntimeError("Market data unavailable")


def price(pair, value):
    return (
        f"{value:.2f}"
        if pair == "btcusd"
        else f"{value:.5f}"
    )


def local_time(ts):
    return datetime.fromtimestamp(
        ts, DISPLAY_TZ
    ).strftime("%d %b %H:%M IST")


def next_window(pair, now):
    candidate = (
        int(now) // 3600 + 1
    ) * 3600

    for _ in range(8 * 24):
        if in_session(pair, candidate):
            return candidate
        candidate += 3600

    return None


def chart_snapshot(
    frame,
    pair,
    timeframe,
    event,
    trade=None,
    cutoff=None,
):
    if frame is None:
        return None

    if cutoff is not None:
        frame = frame.loc[frame.index <= cutoff]

    if frame.empty:
        return None

    rows = [
        [
            int(ts),
            *[
                float(row[column])
                for column in (
                    "Open", "High", "Low", "Close"
                )
            ],
        ]
        for ts, row in frame.tail(40).iterrows()
    ]

    levels = None

    if trade:
        levels = {
            key: trade.get(key)
            for key in (
                "entry",
                "reference",
                "sl",
                "tp1",
                "tp2",
                "distance",
                "side",
                "id",
            )
        }

    return {
        "pair": pair,
        "timeframe": timeframe,
        "event": event,
        "rows": rows,
        "trade": levels,
    }


def unpack_notice(raw):
    try:
        data = json.loads(raw)

        if (
            isinstance(data, dict)
            and data.get("ux_version") == 1
        ):
            return data["text"], data.get("chart")

    except (ValueError, TypeError):
        pass

    return raw, None


def update_message(trade, kind, stamp):
    t = trade
    pair = t["pair"]
    header = f"{pair.upper()} | #{t['id']}"

    if kind == "OPEN":
        text = (
            f"✅ TRADE OPENED: {header}\n"
            f"{'📈 BUY' if t['side'] == 1 else '📉 SELL'}\n"
            f"🎯 Entry: {price(pair, t['entry'])}\n"
            f"🛑 SL: {price(pair, t['sl'])}\n"
            f"🎯 TP1: {price(pair, t['tp1'])} "
            "(50% at 1R)\n"
            f"🎯 TP2: {price(pair, t['tp2'])} "
            "(50% at 2R)\n"
            f"💰 Initial risk: ${t['risk']:.2f}\n"
            "Entry at 5m open, confirmed using a completed candle."
        )

    elif kind == "TP1":
        text = (
            f"🎯 PARTIAL TP1 HIT: {header}\n"
            f"Closed 50% at {price(pair, t['tp1'])} "
            "(+1R on that half).\n"
            "💵 Booked: +0.50R / "
            f"+${t['risk'] * 0.5:.2f} before costs.\n"
            f"🛑 Original SL retained: {price(pair, t['sl'])}\n"
            f"⏳ Remaining 50% target: {price(pair, t['tp2'])}"
        )

    elif kind in ("TP2", "SL"):
        net = t["net_r"]

        text = (
            (
                f"🏆 FULL TP2 HIT: {header}\n"
                "Remaining 50% closed at 2R; trade completed.\n"
            )
            if kind == "TP2"
            else (
                f"❌ STOP LOSS HIT: {header}\n"
                "Trade closed at the original SL "
                "or adverse opening-gap price.\n"
            )
        )

        text += (
            f"📊 Whole-trade gross: {t['gross_r']:+.2f}R\n"
            f"💸 Assumed costs: {t['cost_r']:.2f}R\n"
            f"💰 Whole-trade NET: {net:+.2f}R / "
            f"${net * t['risk']:+.2f}"
        )

        if kind == "SL":
            text += (
                "\nIncludes any previous TP1 exit; "
                "opening gaps may exceed planned risk."
            )

    elif kind == "CANCELLED":
        text = (
            f"⚠️ TRADE CANCELLED: {header}\n"
            f"Reason: {t.get('reason') or 'Entry could not be verified.'}\n"
            "No model fill; no costs or PnL booked. "
            "Reserved risk released."
        )

    elif kind == "REVIEW":
        text = (
            f"🔎 TRADE NEEDS REVIEW: {header}\n"
            "Missing 5m history; outcome is unknown.\n"
            "New entries paused. Tracking retries when "
            "the missing data is available."
        )

    else:
        text = (
            f"🔄 TRACKING RESUMED: {header}\n"
            f"State: {t['status']}; missing history recovered."
        )

    return (
        text
        + f"\n🕒 {local_time(stamp)}"
        + "\n🧪 PAPER ONLY"
    )


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
            raise ValueError(
                "Invalid account/risk configuration"
            )

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

    rsi = (
        50
        if gain == loss == 0
        else (
            100
            if loss == 0
            else 100 - 100 / (1 + gain / loss)
        )
    )

    true_range = pd.concat(
        [
            frame.High - frame.Low,
            (frame.High - close.shift()).abs(),
            (frame.Low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = wilder_last(true_range)
    current_price = float(close.iloc[-1])

    side = (
        1
        if e20 > e50 and 55 < rsi < 70
        else (
            -1
            if e20 < e50 and 30 < rsi < 45
            else 0
        )
    )

    if (
        not all(
            math.isfinite(x)
            for x in (current_price, atr, rsi, e20, e50)
        )
        or atr <= 0
        or abs(e20 - e50) / current_price < 0.0012
        or not side
    ):
        return (
            frame,
            None,
            f"WAIT: EMA/RSI/ATR filter; RSI {rsi:.1f}.",
        )

    return (
        frame,
        {
            "pair": pair,
            "candle": candle,
            "side": side,
            "reference": current_price,
            "distance": 1.5 * atr,
            "rsi": rsi,
        },
        "Qualified paper setup.",
    )


def advance(trade, ts, bar):
    t = trade
    events = []

    if t["status"] == "PENDING":
        if (
            ts != t["fill_at"]
            or not in_session(t["pair"], ts)
            or abs(bar.Open - t["reference"])
            > 0.25 * t["distance"]
        ):
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
        events.append((ts, "COST", -t["cost_r"]))

    side = t["side"]

    stop = (
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

    if stop:
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
            t.update(
                status="PARTIAL",
                remaining=0.5,
            )
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
            "Ledger %s initialized; initial balance %.2f; "
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

    def notify(
        self,
        db,
        text,
        now,
        extra=None,
        chart=None,
    ):
        chats = {
            row[0]
            for row in db.execute(
                f"SELECT chat_id FROM {S}"
            )
        } & self.cfg.chats

        if extra in self.cfg.chats:
            chats.add(extra)

        payload = (
            json.dumps(
                {
                    "ux_version": 1,
                    "text": text,
                    "chart": chart,
                },
                allow_nan=False,
            )
            if chart
            else text
        )

        db.executemany(
            f"INSERT INTO {O}(chat_id,text,created) "
            "VALUES (?,?,?)",
            [
                (chat_id, payload, now)
                for chat_id in chats
            ],
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

    def reserve(
        self,
        setup,
        now,
        requester=None,
        frame=None,
    ):
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
                not math.isfinite(value) or value <= 0
                for value in levels
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

            if db.execute(
                f"SELECT 1 FROM {T} "
                "WHERE pair=? AND (candle=? OR created>?)",
                (
                    pair,
                    setup["candle"],
                    now - 21600,
                ),
            ).fetchone():
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

            day = datetime.fromtimestamp(
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
                (day,),
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
                        pair,candle,created,fill_at,checked,
                        side,reference,distance,risk,cost_r,status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
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

            if requester in self.cfg.chats:
                db.execute(
                    f"INSERT OR IGNORE INTO {S} VALUES (?)",
                    (requester,),
                )

            text = (
                "🚨 NEW PAPER SIGNAL: "
                f"{'BUY' if setup['side'] == 1 else 'SELL'} "
                f"{pair.upper()} | #{cursor.lastrowid}\n"
                f"📉 Entry Ref: {price(pair, setup['reference'])}\n"
                f"🛑 SL Dist: {price(pair, setup['distance'])} "
                "(1.5 ATR)\n"
                f"📊 RSI: {setup['rsi']:.1f}\n"
                f"💰 Risk: ${risk:.2f} + "
                f"{self.cfg.cost_r:.2f}R assumed costs\n"
                "⏳ Status: PENDING — next 5m open at "
                f"{local_time(fill_at)}\n"
                "50% at 1R; 50% at 2R. Original SL retained.\n"
                "🧪 PAPER ONLY"
            )

            snapshot = dict(
                setup,
                id=cursor.lastrowid,
                entry=None,
                sl=None,
                tp1=None,
                tp2=None,
            )

            image = chart_snapshot(
                frame,
                pair,
                "1H",
                "PENDING",
                snapshot,
            )

            self.notify(
                db,
                text,
                now,
                requester,
                image,
            )

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

                emitted = False

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

                    bar_events = advance(trade, ts, bar)

                    for stamp, kind, delta_r in bar_events:
                        db.execute(
                            f"INSERT INTO {E}"
                            "(trade_id,ts,kind,pnl) "
                            "VALUES (?,?,?,?)",
                            (
                                trade["id"],
                                stamp,
                                kind,
                                delta_r * trade["risk"],
                            ),
                        )

                        notice = (
                            "OPEN" if kind == "COST" else kind
                        )

                        image = chart_snapshot(
                            frame,
                            pair,
                            "5m",
                            notice,
                            trade,
                            ts,
                        )

                        self.notify(
                            db,
                            update_message(
                                trade, notice, stamp
                            ),
                            now,
                            chart=image,
                        )

                        emitted = True

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

                if (
                    trade["status"] in ("CANCELLED", "REVIEW")
                    and trade["status"] != old_status
                ):
                    image = chart_snapshot(
                        frame,
                        pair,
                        "5m",
                        trade["status"],
                        trade,
                        trade["checked"],
                    )

                    self.notify(
                        db,
                        update_message(
                            trade, trade["status"], now
                        ),
                        now,
                        chart=image,
                    )

                elif (
                    old_status == "REVIEW"
                    and trade["status"] != old_status
                    and not emitted
                ):
                    image = chart_snapshot(
                        frame,
                        pair,
                        "5m",
                        "RESUMED",
                        trade,
                        trade["checked"],
                    )

                    self.notify(
                        db,
                        update_message(
                            trade, "RESUMED", now
                        ),
                        now,
                        chart=image,
                    )

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
                    "SET attempts=attempts+1,next_try=? "
                    "WHERE id=?",
                    (now + delay, message["id"]),
                )


def make_chart(
    frame,
    pair,
    trade=None,
    timeframe="1H",
    event="ANALYSIS",
):
    os.environ.setdefault(
        "MPLCONFIGDIR",
        "/tmp/jarvis-mpl",
    )

    with PLOT_LOCK:
        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FormatStrFormatter

        fig, ax = plt.subplots(figsize=(9, 4.8))

        try:
            data = frame.tail(40)
            dates = pd.to_datetime(
                data.index,
                unit="s",
                utc=True,
            ).to_pydatetime()

            for stamp, row in zip(
                dates, data.itertuples()
            ):
                color = (
                    "#138b58"
                    if row.Close >= row.Open
                    else "#d44343"
                )

                ax.plot(
                    [stamp, stamp],
                    [row.Low, row.High],
                    color=color,
                    lw=1,
                )
                ax.plot(
                    [stamp, stamp],
                    [row.Open, row.Close],
                    color=color,
                    lw=4,
                )

            if trade:
                entry = trade.get("entry")
                reference = trade.get("reference")

                levels = [
                    (
                        "ENTRY"
                        if entry is not None
                        else "REF (planned)",
                        entry
                        if entry is not None
                        else reference,
                        "#2864c8",
                    )
                ]

                for key, color in (
                    ("sl", "#d44343"),
                    ("tp1", "#c78c00"),
                    ("tp2", "#138b58"),
                ):
                    value = trade.get(key)

                    if (
                        value is None
                        and reference is not None
                        and trade.get("distance") is not None
                    ):
                        multiplier = (
                            -1
                            if key == "sl"
                            else (1 if key == "tp1" else 2)
                        )

                        value = (
                            reference
                            + multiplier
                            * trade["side"]
                            * trade["distance"]
                        )

                    levels.append(
                        (key.upper(), value, color)
                    )

                for label, value, color in levels:
                    if value is not None and value > 0:
                        ax.axhline(
                            value,
                            color=color,
                            linestyle="--",
                            lw=1,
                            label=(
                                f"{label}: {price(pair, value)}"
                            ),
                        )

                ax.legend(loc="best", fontsize=8)

            ax.set_title(
                f"{pair.upper()} | {timeframe} | "
                f"{event} | PAPER"
            )
            ax.set_xlabel(
                "Candle open time — IST | "
                "completed candles only"
            )
            ax.set_ylabel("USD / quote price")

            if len(dates):
                padding = timedelta(
                    seconds=(
                        3600 if timeframe == "1H" else 300
                    )
                )
                ax.set_xlim(
                    dates[0] - padding,
                    dates[-1] + padding,
                )

            ax.xaxis.set_major_locator(
                mdates.AutoDateLocator(
                    minticks=3,
                    maxticks=6,
                )
            )
            ax.xaxis.set_major_formatter(
                mdates.DateFormatter(
                    "%d %b\n%H:%M",
                    tz=DISPLAY_TZ,
                )
            )
            ax.yaxis.set_major_formatter(
                FormatStrFormatter(
                    "%.2f"
                    if pair == "btcusd"
                    else "%.5f"
                )
            )

            ax.grid(alpha=0.15)
            fig.tight_layout()

            output = io.BytesIO()
            fig.savefig(
                output,
                format="png",
                dpi=120,
            )

            return output.getvalue()

        finally:
            plt.close(fig)


def render_notice(raw):
    text, snapshot = unpack_notice(raw)

    if not snapshot:
        return text, None

    frame = pd.DataFrame(
        snapshot["rows"],
        columns=["ts", "Open", "High", "Low", "Close"],
    ).set_index("ts")

    return (
        text,
        make_chart(
            frame,
            snapshot["pair"],
            snapshot.get("trade"),
            snapshot["timeframe"],
            snapshot["event"],
        ),
    )


def explain_check(pair, now):
    frame, setup, reason = signal_check(pair, now)

    if setup:
        return frame, setup, reason

    next_ts = next_window(pair, now)

    following = (
        "Next scheduled entry window: "
        f"{local_time(next_ts)}–"
        f"{datetime.fromtimestamp(next_ts + 600, DISPLAY_TZ).strftime('%H:%M')} IST."
        if next_ts is not None
        else "Next eligible session is unavailable."
    )

    if len(frame) >= 200:
        candle = int(frame.index[-1])

        if not 0 <= now - (candle + 3600) < 600:
            expected = (int(now) // 3600) * 3600

            if (
                now - expected < 600
                and in_session(pair, now)
            ):
                text = (
                    "⏳ WAIT: The provider has not supplied "
                    "the latest completed 1H candle yet. "
                    "Retry shortly."
                )
            else:
                text = (
                    "⏳ WAIT: You are outside the 10-minute "
                    "entry window. No late paper trade was created."
                )

            close = frame.Close
            e20 = float(
                close.ewm(
                    span=20,
                    adjust=False,
                ).mean().iloc[-1]
            )
            e50 = float(
                close.ewm(
                    span=50,
                    adjust=False,
                ).mean().iloc[-1]
            )

            delta = close.diff().dropna()
            gain = wilder_last(delta.clip(lower=0))
            loss = wilder_last(-delta.clip(upper=0))

            rsi = (
                50
                if gain == loss == 0
                else (
                    100
                    if loss == 0
                    else 100 - 100 / (1 + gain / loss)
                )
            )

            true_range = pd.concat(
                [
                    frame.High - frame.Low,
                    (frame.High - close.shift()).abs(),
                    (frame.Low - close.shift()).abs(),
                ],
                axis=1,
            ).max(axis=1)

            aligned = (
                (
                    (e20 > e50 and 55 < rsi < 70)
                    or (e20 < e50 and 30 < rsi < 45)
                )
                and abs(e20 - e50)
                / float(close.iloc[-1]) >= 0.0012
                and wilder_last(true_range) > 0
            )

            if aligned:
                text += (
                    "\n📊 Indicators align on the last "
                    "available closed candle, but its entry "
                    "window is over. A fresh setup must "
                    "qualify again."
                )

            text += (
                "\n🕒 Last candle closed: "
                f"{local_time(candle + 3600)}\n"
                f"{following}"
            )

            if pair == "btcusd":
                text += (
                    "\nBTC hours close at :00 UTC "
                    "= :30 IST, not :00 IST."
                )

            return frame, None, text

    return frame, None, "⏳ " + reason + "\n" + following


class Engine:
    def __init__(self, cfg, health):
        self.cfg = cfg
        self.health = health
        self.store = Store(cfg)

        self.pipeline = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.chart_lock = asyncio.Lock()
        self.workers = asyncio.Semaphore(3)

    async def run(self, function, *args):
        if function in (
            fetch,
            signal_check,
            explain_check,
        ):
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
                self.run(
                    fetch,
                    pair,
                    5,
                    now,
                    since,
                )
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
            explain_check,
            pair,
            int(time.time()),
        )

        if setup:
            reason = await self.run(
                self.store.reserve,
                setup,
                int(time.time()),
                requester,
                frame,
            )

            if (
                "expired" in reason
                or "outside session" in reason
            ):
                next_ts = next_window(
                    pair, int(time.time())
                )

                reason = (
                    "⏳ "
                    + reason
                    + (
                        "\nNext scheduled check: "
                        f"{local_time(next_ts)}."
                        if next_ts
                        else ""
                    )
                )

        return frame, reason

    async def broadcast(self, bot, message, cache):
        raw = message["text"]

        if raw not in cache:
            try:
                cache[raw] = await self.run(
                    render_notice,
                    raw,
                )
            except Exception as exc:
                text, _ = await self.run(
                    unpack_notice,
                    raw,
                )

                cache[raw] = (
                    text
                    + "\n⚠️ Chart unavailable; "
                    "accounting is unaffected.",
                    None,
                )

                LOG.warning(
                    "Update chart failed: %s",
                    type(exc).__name__,
                )

        text, photo = cache[raw]
        text = f"[event {message['id']}] {text}"

        if photo:
            caption = (
                text
                if len(text.encode("utf-16-le")) // 2 <= 1024
                else (
                    "📊 PAPER trade update | "
                    f"event {message['id']}"
                )
            )

            try:
                await bot.send_photo(
                    chat_id=message["chat_id"],
                    photo=io.BytesIO(photo),
                    caption=caption,
                )

            except BadRequest:
                await bot.send_message(
                    chat_id=message["chat_id"],
                    text=(
                        text
                        + "\n⚠️ Telegram could not attach the chart."
                    ),
                )
                return

            if caption != text:
                await bot.send_message(
                    chat_id=message["chat_id"],
                    text=text,
                )

        else:
            await bot.send_message(
                chat_id=message["chat_id"],
                text=text,
            )

    async def flush(self, bot):
        if self.delivery_lock.locked():
            return

        async with self.delivery_lock:
            messages = await self.run(
                self.store.pending_messages,
                int(time.time()),
            )
            cache = {}

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
                    await self.broadcast(
                        bot, message, cache
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
                    retry = (
                        exc.retry_after
                        if isinstance(exc, RetryAfter)
                        else min(
                            3600,
                            30 * 2 ** min(
                                message["attempts"], 7
                            ),
                        )
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

            new_signal = reason.startswith(
                "🚨 NEW PAPER SIGNAL:"
            )

            await message.reply_text(
                "🔔 Paper signal created; entry/outcome "
                "updates enabled. Use /stop to unsubscribe."
                if new_signal
                else reason
            )

            await self.flush(context.bot)

            if not new_signal and not frame.empty:
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
                            "📊 Completed 1H candles • "
                            "timestamps in IST • PAPER only."
                        ),
                    )

                except Exception as exc:
                    LOG.warning(
                        "Chart failed: %s",
                        type(exc).__name__,
                    )

            await self.flush(context.bot)

        elif command in ("stats", "weekly"):
            await message.reply_text(
                await self.run(
                    self.store.summary,
                    7 if command == "weekly" else None,
                )
            )

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
                "🔔 Charts + explicit OPEN / TP1 / TP2 / SL updates.\n"
                "BTC hourly close :00 UTC = :30 IST.\n"
                "Paper estimates only; no broker orders "
                "or guaranteed win rate."
            )

    async def startup(self, app):
        await self.run(self.store.initialize)

        self.health.ready = True
        self.health.beat = time.monotonic()

        delay = float(
            os.getenv(
                "POLLING_START_DELAY_SECONDS",
                "0",
            )
        )

        if (
            not math.isfinite(delay)
            or not 0 <= delay <= 120
        ):
            raise ValueError(
                "POLLING_START_DELAY_SECONDS "
                "must be between 0 and 120"
            )

        if delay:
            LOG.info(
                "HTTP health ready; allowing %.0fs "
                "for old deployment to drain before polling",
                delay,
            )
            await asyncio.sleep(delay)

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
    exc = context.error

    if isinstance(exc, Conflict):
        LOG.error(
            "Telegram 409 Conflict: another poller uses "
            "this token. Local /tmp locks do not coordinate "
            "separate Render containers. Stop duplicate "
            "services/local bots; deployment overlap can "
            "also cause this."
        )
        return

    if isinstance(exc, TimedOut):
        LOG.warning(
            "Telegram request timed out; "
            "subscription may already have been saved"
        )

        if (
            isinstance(update, Update)
            and update.effective_message
        ):
            try:
                await update.effective_message.reply_text(
                    "⏳ Telegram connection timed out. "
                    "Please retry your command shortly."
                )
            except Exception:
                LOG.warning(
                    "Timeout notification could not be delivered"
                )

        return

    LOG.error(
        "Handler/job failed: %s",
        type(exc).__name__,
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


def run_bot():
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
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(45)
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
            drop_pending_updates=True,
            timeout=20,
            allowed_updates=["message"],
        )
    finally:
        health.ready = False
        server.shutdown()
        server.server_close()


def main():
    with single_instance() as acquired:
        if acquired:
            run_bot()


if __name__ == "__main__":
    main()
