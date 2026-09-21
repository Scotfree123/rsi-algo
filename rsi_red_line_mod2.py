#!/usr/bin/env python3
"""
============================================================
  ENGINE A -- AUTO-BUY  (this is "SINGLE-CYBORG" mode)
  Buy happens AUTOMATICALLY the instant a signal fires.
  Selling is entirely manual, done by Gary directly in
  TradeStation -- this script only watches and reconciles.
============================================================

CHANGED 2026-09-17 (Gary's decision, after a full day of testing):
    ONE change from the version that was live before today: MIN_BEND_PCT
    lowered from 2.0 to 1.75. Nothing else in the signal, exits, universe,
    or safety protections changed. See the MIN_BEND_PCT constant below for
    the exact reasoning and the numbers behind this specific choice.

    Also confirmed today, NOT changed (documenting so this doesn't get
    re-litigated by mistake later): a ticker is correctly NOT locked out
    for the rest of the day once it trades. The only lockout is "while a
    position is currently open" (see signal_worker's `if sym in
    open_positions: continue`), released the moment that position closes,
    whether by Gary's manual sell or by the reconcile check noticing it's
    gone. Confirmed this is real, in the actual running code, not just in
    a comment -- Gary specifically wants unlimited same-day re-entry and
    this file already does that correctly.

ONE SELF-CONTAINED FILE. This does NOT import any other script --
everything it needs (TradeStation connection, indicators, signal
detection, the ticker list) lives right here, so there is nothing
else to keep in sync and nothing else that needs to be "connected."

SIGNAL (2-part rule, RSI removed 2026-08-31 -- Gary's decision):
    Black line (EMA20)'s CURRENT angle is shallower than SHALLOWED(-15
    degrees) -- not in a strong downtrend right now, no requirement it
    was ever steep beforehand -- AND red line (HMA7) rising 2 bars in a
    row, both checked on the SAME bar. RSI used to be a third, required
    condition here; extensive testing found essentially zero relationship
    (correlation 0.064) between how "oversold" RSI got before a cross and
    how well the trade performed afterward -- the theory behind requiring
    it didn't hold up. Confirmed by construction: this simpler rule can
    never fire LATER than the old 3-part rule would have on the same day,
    only ever at the same time or earlier -- and testing found 71 real,
    good trades the old rule missed entirely because RSI simply never
    crossed 35 that day. One honest caveat found during testing: once
    measured with a REALISTIC exit (not "held the whole day"), the two
    versions perform similarly -- the real benefit here is genuinely more
    candidates of comparable quality, not dramatically better ones, which
    is exactly what matters for a Cyborg design where you review every
    candidate yourself.

ENTRY: automatic market buy the instant the signal fires. Size is
    $500 notional per trade (TRADE_DOLLARS, raised 2026-09-18 from the
    earlier fixed-1-share test level) -- shares = TRADE_DOLLARS / price,
    minimum 1 share.

EXIT: RESTORED 2026-09-21 (Gary's decision), after finding that the
    2026-09-03 removal (see sell_monitor_worker) left real positions
    (e.g. BEZ, bought 9/18, still open Monday morning with zero
    automatic protection) exposed with no safety net at all. All three
    of the following now fire AUTOMATICALLY, with no approval prompt --
    they're safety nets, not trading decisions, so waiting on a human
    answer would defeat the purpose:
      - -2% hard stop from entry (STOP_PCT)
      - 2.5% trailing-stop pullback from the peak once in profit (TRAIL_PCT)
      - end-of-day flatten at EOD_FLATTEN_ET (15:59 ET) -- sells everything
        still open, once, near the close
    This is DELIBERATELY DIFFERENT from the plain system's -5% hard-floor/
    manual-only exit -- that's the whole point of Cyborg mode.

SAFETY PROTECTIONS (ported over from the plain system, 2026-08-26 --
    these were missing from earlier versions of this combined file):
    - MAX_SLOTS = 10: won't open more than 10 positions at once.
    - Same-pair lock: won't buy NBIL if NBIZ is already open (and
      vice versa for every inverse pair), same as the plain system.

BUG FIX (2026-08-26 evening, historical -- fixed in an earlier version,
    no longer directly relevant since the "was steep" concept it was
    protecting is gone entirely as of 2026-08-30):
    Found by backtesting Aug 24/25 against real signals: the "black line
    was steep" check could reach back across a DIFFERENT trading day (even
    a week+ earlier) and combine with today's real conditions, firing a
    false signal. This entire mechanism (the historical "was steep"
    lookback) was later removed altogether, so this specific bug class
    can no longer occur.

LOGGING: writes a complete round-trip row (entry + exit + reason) to
    its own CSV log the moment each trade actually closes -- a
    separate file from the plain system's log, so the two never
    collide or overwrite each other.

HOW TO RUN:
    cd ~/rsi_system
    set -a; source .env; set +a
    ~/algotrend1v5/venv/bin/python3 rsi_mod2_A_autobuy.py

Needs to run on a computer/server that stays on and connected during
market hours.
"""
import os
import sys
import csv
import time
import math
import signal as _sig
import threading
import queue
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

AZ = ZoneInfo("America/Phoenix")
ET = ZoneInfo("America/New_York")

TS_OAUTH_URL = "https://signin.tradestation.com/oauth/token"
TS_BASE = {
    "sim":  "https://sim-api.tradestation.com/v3",
    "live": "https://api.tradestation.com/v3",
}

# ------------------------------------------------------------ constants ------

EMA_LEN        = 20        # black
HMA_LEN        = 7         # red
ANGLE_LOOKBACK = 5
SHALLOWED      = -15.0
ATR_LEN        = 14   # ADDED (2026-09-21, Gary's decision, live mid-session):
                       # Wilder-smoothed Average True Range, computed fresh
                       # each day from real TradeStation minute bars (High/
                       # Low/Close), same daily-reset pattern as black/red.
                       # Used to make the bend requirement scale with each
                       # ticker's OWN current volatility instead of one fixed
                       # percentage for every ticker on every day -- Gary's
                       # observation this morning: a calmer market than when
                       # this was originally tuned means a fixed % bend is
                       # either too strict (calm day) or too loose (volatile
                       # day) depending on conditions, and it should instead
                       # track actual volatility as it changes.
                       # IMPORTANT CAVEAT: this could NOT be backtested
                       # against history before going live, because the
                       # downloaded 9/18 dataset used for prior backtests
                       # only has closing prices, not real per-minute High/
                       # Low bars, so no valid historical ATR could be
                       # computed offline. The live TradeStation feed DOES
                       # return real High/Low every bar (see get_bars), so
                       # the math here is correct once running -- but today
                       # is genuinely this mechanism's first real test,
                       # not a backtested-and-confirmed change. Watch it
                       # closely.
WARMUP_BARS    = 12  # SET (2026-09-14, Gary's decision): after building the
                      # daily-reset black/red calculation (see build_frame),
                      # tested warmup lengths 6-25 minutes against the real
                      # reach-1%-within-N-minutes measure across the full
                      # ~3-month dataset. 6/8/10 min all produced identical
                      # results (the math's first real signal never landed
                      # before minute 12 anyway); 12 min was the actual
                      # sweet spot, slightly beating even the shorter
                      # warmups on most measures while giving the black
                      # line meaningfully more time to mature past its
                      # first few, still-forming minutes. 15+ min showed a
                      # steady decline from there. Since this runs as
                      # Double Cyborg (human approves every buy), more
                      # candidate signals is a feature, not a cost.

# ---- Cyborg exit rule (deliberately different from the plain system's
# -5% hard-floor/manual-only exit) ----
STOP_PCT = 2.0
TRAIL_PCT = 2.5
SELL_ALERT_COOLDOWN_SECONDS = 90
POPUP_TIMEOUT_SECONDS = 180   # CHANGED (2026-09-03, Gary's decision): was 600
                              # (10 min), now 3 min -- if you don't answer a
                              # buy prompt in this long,
                               # it auto-expires and keeps holding

TRADE_DOLLARS = 500  # RAISED (2026-09-18, Gary's decision): moving up from the
                        # $1/share test level now that this engine (daily-reset
                        # indicators, WARMUP_BARS=12, MIN_BEND_PCT=1.75) is going
                        # live for real. $500 notional per trade, same for every
                        # ticker regardless of price.
                        # Shares are computed at buy time: int(TRADE_DOLLARS /
                        # current price), minimum 1 share.
                        # Previously 1 (set 2026-09-14, deliberately forced every
                        # trade to exactly 1 share while watching this new engine
                        # react to real market conditions before risking real
                        # money-sized positions on it). Before that: 1000 (set
                        # 2026-09-10), SHARES_PER_TRADE=1 (set 2026-08-26).


def shares_for_dollars(price: float) -> int:
    """How many whole shares $TRADE_DOLLARS buys at this price, min 1."""
    if price is None or price <= 0:
        return 1
    return max(1, int(TRADE_DOLLARS / price))

MAX_SLOTS = 50   # RAISED (2026-09-02, matching Engine B): high enough it
                 # should never actually bind given the current ticker list.

RSI_MOD2_MODE = "FULLY_AUTOMATIC"  # buy AND sell both automatic -- no approval for anything

# Same PAIRS/SYMBOLS/PARTNER as the plain system, so both files always
# watch the exact same tickers with the exact same pair-lock logic.
# UNIVERSE REDUCED (2026-09-02, Gary's decision): dropped from 19 down
# to just the 9 highest-volatility tickers (5 pairs + AAOX). Two
# independent reasons converged on this exact same cutoff: (1) this
# was the natural volatility ranking cutoff identified much earlier in
# this project (a clean gap between CWVX at 15.2% and IONZ at 14.5%),
# and (2) tested tonight -- cutting to just these 9 roughly HALVES the
# typical signal clustering (10.8 -> 5.5 tickers in any 10-min window)
# and mathematically GUARANTEES the worst case can never exceed 9,
# while trade quality is the same or slightly BETTER (avg peak
# actually improved, from 6.55% to 7.27%, in the smaller group).
PAIRS = [
    ("NBIL", "NBIZ"),
    ("IRE",  "IREZ"),
    ("SNXX", "SNDQ"),
    ("LITX", "LITZ"),
    ("IONX", "IONZ"),
    ("BEX",  "BEZ"),
    ("BMNU", "BMNZ"),
    ("MSTU", "MSTZ"),
    ("CRCG", "CRCD"),
    ("SMCX", "SMCZ"),
    ("RKLX", "RKLZ"),   # ADDED (2026-09-18, Gary's decision): re-added after
    ("ASTX", "ASTN"),   # being set aside on 2026-09-03 -- Gary spotted what
    ("QBTX", "QBTZ"),   # looked like real, gradual Tandem-System-style moves
    ("CWVX", "CORD"),   # on these names (RKLZ, OKLS, ASTN specifically) and
    ("OKLL", "OKLS"),   # wants them back in Mod 3's universe to test live.
                        # Same MIN_BEND_PCT=1.75 and TRADE_DOLLARS=500 as the
                        # rest of the universe. NOTE (2026-09-18): checked
                        # directly with Gary -- AAOX has NO real inverse
                        # (AAOZ does not exist/trade); AAOX/AAOZ stays out.
                        # !! VERIFY per the original spec's own warning:
                        # confirm all 10 of these new symbols actually
                        # resolve at TradeStation, and that each pair's two
                        # legs are genuine inverses of the same underlying,
                        # before trusting real fires on them.
]  # ORIGINAL FINAL LIST was set 2026-09-03, after a full day of live
   # testing plus careful review of volume, correlation, and news for every
   # candidate; the 5 pairs above were part of that same original review
   # ("considered and set aside for this final cut") and are only being
   # added back now, 2026-09-18, on Gary's explicit decision above.
SYMBOLS = [s for pr in PAIRS for s in pr]
PARTNER = {}
for _grp in PAIRS:
    if len(_grp) == 2:
        PARTNER[_grp[0]] = _grp[1]
        PARTNER[_grp[1]] = _grp[0]

_dupes = [s for s in set(SYMBOLS) if SYMBOLS.count(s) > 1]
if _dupes:
    raise SystemExit(f"FATAL: duplicate symbols in PAIRS: {sorted(_dupes)}")

BAR_MINUTES = 1
BAR_LOOKBACK = 400
MARKET_OPEN_ET = "09:30"
MARKET_CLOSE_ET = "16:00"
NO_NEW_AFTER_ET = "15:58"
EOD_FLATTEN_ET = "15:59"
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or 20)

# Separate log file from the plain system's, so they never collide.
LOG_CSV = os.getenv("MOD2_CYBORG_LOG") or "rsi_mod2_A_autobuy_log.csv"
LOG_COLUMNS = [
    "time_opened", "time_closed", "ticker", "entry", "exit_price",
    "qty", "pnl_pct", "reason",
    "angle_now_at_entry", "angle_was_at_entry",
]


def log(msg):
    ts = datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S AZ")
    print(f"{ts} | {msg}", flush=True)


def ensure_csv():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerow(LOG_COLUMNS)


def append_trade_row(row: dict):
    with open(LOG_CSV, "a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in LOG_COLUMNS])


# ------------------------------------------------------------ indicators -----

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hma(s: pd.Series, n: int) -> pd.Series:
    half = 3
    root = 3
    return wma(2 * wma(s, half) - wma(s, n), root)


def black_angle(black: pd.Series) -> pd.Series:
    """Angle of the black line, in degrees. Uses a best-fit straight line
    through the whole ANGLE_LOOKBACK(5)-minute window (all 6 points), not
    just the two endpoints -- validated 2026-08-30: this uses every
    available data point instead of throwing most of them away, so it
    can't be blind to a real move that happens to sit in the middle of
    the window. Tested against the two-point method across the full
    trade history: 72 trades vs 71, essentially identical -- this change
    is safe, and is the more thorough of the two methods."""
    window_size = ANGLE_LOOKBACK + 1
    x = np.arange(window_size, dtype=float)
    x_mean = x.mean()
    x_centered = x - x_mean
    denom = (x_centered ** 2).sum()

    def _slope_pct_per_min(vals):
        if np.isnan(vals).any():
            return np.nan
        y_mean = vals.mean()
        slope = (x_centered * (vals - y_mean)).sum() / denom
        if y_mean == 0:
            return np.nan
        return (slope / y_mean) * 100.0

    pct_per_bar = black.rolling(window_size).apply(_slope_pct_per_min, raw=True)
    return np.degrees(np.arctan(pct_per_bar))


def atr_wilder_pct(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Average True Range, Wilder-smoothed, expressed as a PERCENT of price
    so it's directly comparable to bend_pct (which is also a percent of
    price). True Range per bar = max(high-low, |high-prev_close|,
    |low-prev_close|); ATR = Wilder EMA (alpha=1/n) of True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    return (atr / close) * 100.0


# ------------------------------------------------------------ session --------

def session_filter(df):
    if df is None or df.empty:
        return df
    et = df.tz_convert(ET)
    try:
        et = et.between_time(MARKET_OPEN_ET, MARKET_CLOSE_ET, inclusive="both")
    except TypeError:
        et = et.between_time(MARKET_OPEN_ET, MARKET_CLOSE_ET)
    et = et[et.index.weekday < 5]
    return et.tz_convert("UTC")


def bar_et(ts):
    try:
        return pd.Timestamp(ts).tz_convert(ET).strftime("%H:%M")
    except Exception:
        return str(ts)


def _to_utc_index(values):
    out = []
    for v in values:
        t = pd.Timestamp(v)
        out.append(t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC"))
    return pd.DatetimeIndex(out)


# ------------------------------------------------------------ TS client ------

@dataclass
class _Trade:
    price: float


@dataclass
class _Account:
    account_number: str
    status: str


class TradeStationClient:
    def __init__(self):
        self.client_id = (os.getenv("TS_CLIENT_ID") or os.getenv("TS_API_KEY")
                          or os.getenv("TRADESTATION_CLIENT_ID"))
        self.client_secret = (os.getenv("TS_CLIENT_SECRET") or os.getenv("TS_SECRET")
                              or os.getenv("TRADESTATION_CLIENT_SECRET"))
        self.refresh_token = (os.getenv("TS_REFRESH_TOKEN")
                              or os.getenv("TRADESTATION_REFRESH_TOKEN"))
        self.account_id = (os.getenv("TS_ACCOUNT_ID")
                           or os.getenv("TRADESTATION_ACCOUNT_ID"))
        self.env = (os.getenv("TS_ENV") or "sim").lower()
        self.dry_run = (os.getenv("DRY_RUN") or "1") != "0"
        self.drop_forming_bar = (os.getenv("DROP_FORMING_BAR") or "1") != "0"

        if self.env not in TS_BASE:
            raise SystemExit(f"TS_ENV must be 'sim' or 'live', got {self.env!r}")
        self.base = TS_BASE[self.env]

        missing = [k for k, v in {
            "TS_CLIENT_ID (or TS_API_KEY)": self.client_id,
            "TS_CLIENT_SECRET (or TS_SECRET)": self.client_secret,
            "TS_REFRESH_TOKEN": self.refresh_token,
            "TS_ACCOUNT_ID": self.account_id,
        }.items() if not v]
        if missing:
            raise SystemExit(f"FATAL: missing TradeStation creds in .env: {missing}")

        self._token = None
        self._token_exp = 0.0
        self._session = requests.Session()

    def _access_token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        log("Refreshing TradeStation access token...")
        r = self._session.post(TS_OAUTH_URL, data={
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }, timeout=15)
        r.raise_for_status()
        tok = r.json()
        self._token = tok["access_token"]
        self._token_exp = time.time() + int(tok.get("expires_in", 1200))
        log(f"Token valid for {int(tok.get('expires_in', 1200))}s")
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _get(self, path, params=None):
        r = self._session.get(self.base + path, headers=self._headers(),
                              params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = self._session.post(self.base + path, headers=self._headers(),
                               json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def get_bars(self, symbol, limit=None):
        params = {"interval": BAR_MINUTES, "unit": "Minute",
                  "barsback": limit or BAR_LOOKBACK}
        data = self._get(f"/marketdata/barcharts/{symbol}", params)
        bars = data.get("Bars", [])
        if not bars:
            return None
        rows, raw_ts = [], []
        for b in bars:
            raw_ts.append(b["TimeStamp"])
            rows.append({
                "open": float(b["Open"]), "high": float(b["High"]),
                "low": float(b["Low"]), "close": float(b["Close"]),
                "volume": float(b.get("TotalVolume", 0) or 0),
            })
        df = pd.DataFrame(rows, index=_to_utc_index(raw_ts)).sort_index()
        if self.drop_forming_bar and len(df) > 1:
            df = df.iloc[:-1]
        return df

    def get_latest_trade(self, symbol):
        data = self._get(f"/marketdata/quotes/{symbol}")
        q = (data.get("Quotes") or [{}])[0]
        last = q.get("Last") or q.get("Close") or 0.0
        return _Trade(float(last))

    def get_account(self):
        data = self._get("/brokerage/accounts")
        accts = data.get("Accounts", [])
        me = next((a for a in accts if a.get("AccountID") == self.account_id), None)
        if me is None and accts:
            me = next((a for a in accts
                       if str(a.get("AccountType", "")).lower() == "margin"), accts[0])
            log(f"WARN TS_ACCOUNT_ID={self.account_id!r} not in env={self.env}; "
                f"using {me.get('AccountID')} (type={me.get('AccountType')})")
            self.account_id = me.get("AccountID")
        me = me or {}
        return _Account(me.get("AccountID", self.account_id),
                        me.get("Status", "UNKNOWN"))

    def get_balance(self):
        try:
            data = self._get(f"/brokerage/accounts/{self.account_id}/balances")
            b = (data.get("Balances") or [{}])[0]
            return {"equity": float(b.get("Equity", 0) or 0),
                    "cash": float(b.get("CashBalance", 0) or 0),
                    "buying_power": float(b.get("BuyingPower", 0) or 0)}
        except Exception as e:
            log(f"WARN could not read balances: {e}")
            return None

    def list_positions(self):
        data = self._get(f"/brokerage/accounts/{self.account_id}/positions")
        out = {}
        for p in data.get("Positions", []):
            out[p.get("Symbol")] = {
                "qty": abs(int(float(p.get("Quantity", 0) or 0))),
                "avg": float(p.get("AveragePrice", 0) or 0),
            }
        return out

    def market_buy(self, symbol, qty):
        body = {"AccountID": self.account_id, "Symbol": symbol,
                "Quantity": str(int(qty)), "OrderType": "Market",
                "TradeAction": "BUY", "TimeInForce": {"Duration": "DAY"},
                "Route": "Intelligent"}
        if self.dry_run:
            log(f"DRY-RUN buy suppressed: BUY {qty} {symbol}")
            return {"dry_run": True}
        return self._post("/orderexecution/orders", body)

    def market_sell(self, symbol, qty):
        body = {"AccountID": self.account_id, "Symbol": symbol,
                "Quantity": str(int(qty)), "OrderType": "Market",
                "TradeAction": "SELL", "TimeInForce": {"Duration": "DAY"},
                "Route": "Intelligent"}
        if self.dry_run:
            log(f"DRY-RUN sell suppressed: SELL {qty} {symbol}")
            return {"dry_run": True}
        return self._post("/orderexecution/orders", body)


# ------------------------------------------------------------ signal ---------

@dataclass
class Frame:
    ts: object
    close: float
    open_: float
    low: float
    red_now: float
    red_prev: float
    red_prev2: float
    red_prev3: float
    red_prev4: float
    angle_now: float
    angle_was: float
    bar_index: int
    atr_pct_now: float


def build_frame(df: pd.DataFrame) -> Frame:
    et_idx = df.index.tz_convert(ET)
    today = et_idx[-1].date()
    session_mask = (et_idx.date == today)
    bar_index = int(session_mask.sum()) - 1

    # RESET DAILY (2026-09-14, Gary's decision -- reverting the 2026-09-02
    # change): black/red/angle are now computed using ONLY today's bars,
    # not the continuous multi-day stitched history. Backtesting found
    # that chaining across the overnight gap meant the lines reacted to
    # whatever gap-up/gap-down had already happened while the market was
    # closed, and 92% of opening-window signals turned out to be riding a
    # big overnight gap rather than catching real intraday movement (e.g.
    # IONX 9/8: gapped +14% overnight, signal fired at the open on the
    # gap-momentum, then gave back to a -10% loss by end of day). Gary's
    # call: don't let something that happened while the market was closed
    # get treated like it's happening right now. Tradeoff, confirmed
    # deliberately accepted: the lines need to warm back up each morning,
    # so WARMUP_BARS goes back up and the system can't fire for the first
    # ~25 minutes of each session again (this is the dead zone the 9/02
    # change was originally trying to avoid).
    today_df = df[session_mask]
    close = today_df["close"]
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
    angle = black_angle(black)
    atr_pct = atr_wilder_pct(today_df["high"], today_df["low"], close, ATR_LEN)

    def _safe(series, pos):
        try:
            v = series.iloc[pos]
            return float(v) if not (isinstance(v, float) and math.isnan(v)) else float("nan")
        except Exception:
            return float("nan")

    # NOTE (2026-08-31, Gary's decision): RSI dropped from the signal
    # entirely. Extensive testing found essentially zero relationship
    # (correlation 0.064) between how "oversold" RSI got before a cross
    # and how well the trade performed afterward -- the theory behind
    # requiring it didn't hold up. The real, meaningful signal has always
    # come from the black line and red line; RSI was mostly adding delay,
    # not protection. Confirmed by construction: this simpler 2-part rule
    # can never fire LATER than the old 3-part rule would have on the
    # same day -- only ever at the same time or earlier.
    return Frame(
        ts=df.index[-1],
        close=float(close.iloc[-1]),
        open_=float(df["open"].iloc[-1]),
        low=float(df["low"].iloc[-1]),
        red_now=_safe(red, -1),
        red_prev=_safe(red, -2),
        red_prev2=_safe(red, -3),
        red_prev3=_safe(red, -4),
        red_prev4=_safe(red, -5),
        angle_now=_safe(angle, -1),
        angle_was=float("nan"),
        bar_index=bar_index,
        atr_pct_now=_safe(atr_pct, -1),
    )


def black_gate_open(fr: Frame) -> bool:
    """CHANGED (2026-08-30, Gary's decision): dropped the "was steep
    beforehand" requirement entirely. Now this ONLY checks the black
    line's CURRENT angle -- if it's not in a strong downtrend right now
    (shallower than SHALLOWED), the trade is allowed through. No history
    check at all. Rationale: don't buy against a strong current
    downtrend, but a weak/flat/rising black line is fine even if it was
    never dramatically steep beforehand -- waiting for a full -30 degree
    prior decline meant waiting until it was too late to get in."""
    if math.isnan(fr.angle_now):
        return False
    return fr.angle_now > SHALLOWED


# ---- ATR-based bend (2026-09-21, tried live mid-session, then set aside
# the same morning) -- kept here, unused, in case it's worth revisiting
# properly backtested later. NOT wired into red_rising() below anymore. ----
BEND_ATR_MULT = 5.5
BEND_PCT_FLOOR = 0.5

BEND_LOOKBACK_BARS = 3  # NARROWED from 4 to 3 (2026-09-21, Gary's final
                        # decision, same morning): after seeing the full
                        # 4-straight-bar requirement, Gary judged it would
                        # cut out too many good signals -- "three greens in
                        # a row with the red line bending up is sufficient."
                        # Bend is now measured across BEND_LOOKBACK_BARS(3)
                        # bars -- i.e. "did the red line rise on 3 straight
                        # bars, moving at least MIN_BEND_PCT total over
                        # those 3 minutes." Still requires EVERY bar in the
                        # window to be rising (not just net higher at the
                        # end) -- that sustained-trend requirement stays,
                        # only the window length changed.
                        # History: was 4 (tried a few minutes earlier, same
                        # morning), before that an ATR-scaled version (tried
                        # and set aside, same morning -- see BEND_ATR_MULT
                        # above, still in the file but unused), before that
                        # a flat 2-bar window (the original design).

MIN_BEND_PCT = 1.00  # target for the 4-minute bend (Gary's own words: "one
                     # percent within four minutes"). NOTE, an honest
                     # caveat Gary raised himself: neither this number nor
                     # the historical hit-rate stats in this file's older
                     # comments were tested against TODAY's specific
                     # volatility regime -- both this file's 73-day
                     # backtest and the 9/18 single-day test were run
                     # against whatever conditions existed on those past
                     # days, calmer or wilder than today. Treat today as a
                     # live test of this exact number, not a confirmed one.


def red_rising(fr: Frame) -> bool:
    """RELAXED (2026-09-21, Gary's final decision, same morning): dropped
    the requirement that EVERY bar in the window be individually rising.
    Now it's a 3-minute WINDOW to reach the target -- fire the moment the
    red line is up at least MIN_BEND_PCT vs. 3 bars ago, as long as it's
    still rising right now (not already turning over). Gary's own words:
    "give it a window of three minutes to reach it... even if it only is
    one or two or three greens" -- the move can arrive as one sharp step,
    two, or a steady climb across all three; what matters is that 1% got
    covered somewhere in that 3-minute window, not that every single
    minute individually ticked up."""
    if not (fr.red_now > fr.red_prev):
        return False
    if math.isnan(fr.red_prev3):
        return False
    bend_pct = (fr.red_now - fr.red_prev3) / fr.close * 100
    return bend_pct >= MIN_BEND_PCT


def et_now():
    return datetime.now(ET)


def _hhmm(s):
    hh, mm = s.split(":")
    return int(hh), int(mm)


def in_session(now_et):
    oh, om = _hhmm(MARKET_OPEN_ET)
    ch, cm = _hhmm(MARKET_CLOSE_ET)
    o = now_et.replace(hour=oh, minute=om, second=0, microsecond=0)
    c = now_et.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return o <= now_et <= c and now_et.weekday() < 5


def past(now_et, hhmm_str):
    hh, mm = _hhmm(hhmm_str)
    mark = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_et >= mark


_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


# ------------------------------------------------------------ Cyborg UI ------

approval_queue = queue.Queue()
decision_queue = queue.Queue()
open_positions = {}   # symbol -> dict with entry/peak/qty/opened_ts/entry indicator snapshot
latest_frame = {}     # symbol -> most recent Frame, for the live status board


class TerminalApproval:
    """Plain-text sell approval, right here in this terminal (no popup window,
    since this runs headless over SSH with no screen attached)."""

    @staticmethod
    def _read_line_with_timeout(prompt, timeout_sec):
        result_q = queue.Queue()

        def _reader():
            try:
                line = input(prompt)
            except EOFError:
                return
            result_q.put(line)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            return result_q.get(timeout=timeout_sec)
        except queue.Empty:
            return None

    def run_forever(self):
        while _RUNNING:
            try:
                symbol, price, ts, kind, reason = approval_queue.get(timeout=1)
            except queue.Empty:
                continue

            if kind == "BUY":
                header = f"BUY SIGNAL -- {symbol} @ ~${price:.2f}"
                ask = "Type Y and press Enter to BUY, or just press Enter to skip."
            else:
                header = f"SELL SIGNAL -- {symbol} @ ~${price:.2f}  ({reason})"
                ask = "Type Y and press Enter to SELL, or just press Enter to keep holding."

            banner = (
                f"\n{'='*60}\n{header}\n{'='*60}\n{ask}\n"
                f"(you have {POPUP_TIMEOUT_SECONDS} seconds -- after that it auto-"
                f"{'skips' if kind=='BUY' else 'expires (stays held)'})\n> "
            )

            answer = self._read_line_with_timeout(banner, POPUP_TIMEOUT_SECONDS)

            if answer is None:
                decision_queue.put((f"EXPIRED_{kind}", symbol, price, ts))
                print(f"(no answer in time -- {symbol} {kind} EXPIRED)")
                continue

            if answer.strip().lower() in ("y", "yes"):
                decision_queue.put((f"APPROVE_{kind}", symbol, price, ts))
            else:
                decision_queue.put((f"SKIP_{kind}", symbol, price, ts))


def slots_in_use():
    return len(open_positions)


def pair_leg_open(sym):
    # DISABLED (2026-09-02, matching Engine B): a real signal on one side
    # of a pair is genuine information, not noise, even while holding
    # the other side.
    return False


def signal_worker(api):
    """Watches every symbol, auto-buys the instant the 2-part signal fires --
    black line's current angle is shallow enough, AND the red line is
    rising 2 bars in a row, both true on the SAME bar (2026-08-31, Gary's
    decision: RSI dropped entirely -- extensive testing found it added no
    real predictive value, mostly just delay). No arm-tracking or
    multi-bar alignment window needed now that there are only two
    conditions to check, and they're required simultaneously."""
    last_signaled_bar = {s: None for s in SYMBOLS}

    log(f"Signal worker started. Black-line check: current angle must be shallower "
        f"than {SHALLOWED:.1f} degrees. Red line rising 2 bars in a row. "
        f"MAX_SLOTS={MAX_SLOTS}, pair-lock ON.")

    while _RUNNING:
        now_et = et_now()
        if not in_session(now_et):
            time.sleep(POLL_SECONDS)
            continue

        for sym in SYMBOLS:
            if sym in open_positions:
                continue
            try:
                df = api.get_bars(sym)
                df = session_filter(df) if df is not None else None
                if df is None or len(df) < WARMUP_BARS + ANGLE_LOOKBACK + 35:
                    continue
                fr = build_frame(df)
                latest_frame[sym] = fr   # cache for the live status board
            except Exception as e:
                log(f"WARN {sym}: {e}")
                continue

            if fr.bar_index < WARMUP_BARS:
                continue

            if not (black_gate_open(fr) and red_rising(fr)):
                continue

            sig_key = (str(now_et.date()), fr.bar_index)
            if last_signaled_bar[sym] == sig_key:
                continue
            last_signaled_bar[sym] = sig_key

            # ---- safety protections, ported from the plain system ----
            if slots_in_use() >= MAX_SLOTS:
                log(f"SLOT-SKIP {sym} (slots full {MAX_SLOTS})")
                continue
            if pair_leg_open(sym):
                log(f"PAIR-SKIP {sym} (partner {PARTNER[sym]} already open)")
                continue

            qty = shares_for_dollars(fr.close)
            log(f"SIGNAL {sym} @ {fr.close:.4f} bar={fr.bar_index} -- AUTO-BUYING "
                f"(single-cyborg mode, {qty} shares, ~${TRADE_DOLLARS} notional)")
            try:
                result = api.market_buy(sym, qty)
                log(f"Buy order result for {sym}: {result} ({qty} shares @ ${fr.close:.2f})")
                open_positions[sym] = {
                    "entry": fr.close, "peak": fr.close, "qty": qty,
                    "opened_ts": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_angle_now": fr.angle_now,
                    "entry_angle_was": fr.angle_was,
                }
                log(f"Now tracking open position: {sym} entry=${fr.close:.2f} -- "
                    f"will alert on sell via this terminal")
            except Exception as e:
                log(f"ERROR auto-buying {sym}: {e}")

        time.sleep(POLL_SECONDS)


STATUS_BOARD_SECONDS = 60   # how often the live status board prints


def status_board_worker():
    """Prints a one-line status board every minute showing what every
    ticker is currently doing (black-line angle, red-line direction) --
    same idea as the plain system's live board, added 2026-08-27 per Gary's
    request so you can watch the angle move toward the threshold, even on
    minutes where nothing fires."""
    while _RUNNING:
        time.sleep(STATUS_BOARD_SECONDS)
        now_et = et_now()
        if not in_session(now_et):
            continue
        if not latest_frame:
            continue

        lines = []
        for sym in SYMBOLS:
            fr = latest_frame.get(sym)
            if fr is None:
                if sym in open_positions:
                    lines.append(f"{sym}:HOLDING")
                else:
                    lines.append(f"{sym}:no data yet")
                continue
            red_dir = "up" if fr.red_now > fr.red_prev else "down"
            held = " [HOLDING]" if sym in open_positions else ""
            lines.append(
                f"{sym}:angle={fr.angle_now:+5.1f}deg red={red_dir}{held}"
            )

        log("--- status board ---")
        # print 3 tickers per line so it stays readable in a normal terminal width
        for i in range(0, len(lines), 3):
            print("   " + "   |   ".join(lines[i:i + 3]), flush=True)


RECONCILE_SECONDS = 10   # TIGHTENED (2026-09-03, Gary's decision): was 60
                          # seconds, tightened down since a manually-sold
                          # ticker being stuck even briefly isn't acceptable.
                          # Checks the real account 6x more often now.


def reconcile_positions_worker(api):
    """NEW (2026-09-03, Gary's decision): periodically checks the REAL
    broker account directly, and clears out anything this script thinks
    it's still holding that's actually already gone -- specifically to
    handle the case where Gary sells a position manually, directly in
    TradeStation, outside this script entirely. Without this check, the
    script would keep believing it still holds that ticker forever,
    permanently blocking any new signal on it, and would try (and fail)
    to sell something that no longer exists the moment a stop condition
    is checked. This fixes both problems by keeping the script's own
    memory honest against what's actually true in the account."""
    while _RUNNING:
        time.sleep(RECONCILE_SECONDS)
        if not open_positions:
            continue
        try:
            real_positions = api.list_positions()
        except Exception as e:
            log(f"WARN reconcile check failed: {e}")
            continue

        for sym in list(open_positions.keys()):
            real = real_positions.get(sym)
            if real is None or real.get("qty", 0) == 0:
                pos = open_positions.pop(sym, None)
                log(f"RECONCILE: {sym} is no longer held in the real account "
                    f"(sold manually, outside this script) -- clearing it "
                    f"from memory so it can trade again.")
                if pos:
                    try:
                        quote = api.get_latest_trade(sym)
                        exit_price = quote.price
                    except Exception:
                        exit_price = None
                    entry = pos.get("entry", 0)
                    if exit_price and entry:
                        pnl_pct = (exit_price / entry - 1) * 100
                        exit_str = f"{exit_price:.4f}"
                        pnl_str = f"{pnl_pct:+.2f}"
                    else:
                        exit_str = "unknown (sold manually)"
                        pnl_str = ""
                    append_trade_row({
                        "time_opened": pos.get("opened_ts", ""),
                        "time_closed": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": sym, "entry": f"{entry:.4f}" if entry else "",
                        "exit_price": exit_str,
                        "qty": pos.get("qty", ""), "pnl_pct": pnl_str,
                        "reason": "manual sell outside script (detected by reconcile check)",
                        "angle_now_at_entry": pos.get("entry_angle_now", ""),
                        "angle_was_at_entry": pos.get("entry_angle_was", ""),
                    })


def sell_monitor_worker(api):
    """RESTORED (2026-09-21, Gary's decision): automatic selling is back --
    -2% hard stop-loss (STOP_PCT), 2.5% trailing stop once in profit
    (TRAIL_PCT), and an end-of-day flatten at EOD_FLATTEN_ET. All three
    fire automatically, no approval prompt -- they're safety nets, not
    trading decisions, so waiting on a human answer would defeat the
    purpose. This reverses the 2026-09-03 change that removed all
    automatic selling; that change left real positions (e.g. BEZ, bought
    9/18) carried over into the next session with zero automatic
    protection, which is what prompted restoring this."""
    eod_done_today = None  # date EOD flatten last ran, so it only fires once/day
    while _RUNNING:
        now_et = et_now()
        if not in_session(now_et):
            time.sleep(POLL_SECONDS)
            continue

        # ---- end-of-day flatten: sell everything still open, once ----
        oh, om = _hhmm(EOD_FLATTEN_ET)
        if (now_et.hour, now_et.minute) >= (oh, om) and eod_done_today != now_et.date():
            for sym, pos in list(open_positions.items()):
                qty = pos.get("qty", 1)
                try:
                    result = api.market_sell(sym, qty)
                    log(f"EOD-FLATTEN {sym}: sell order result {result} ({qty} shares)")
                except Exception as e:
                    log(f"EOD-FLATTEN-ERR {sym}: {e}")
                    continue
                entry = pos.get("entry", 0)
                try:
                    quote = api.get_latest_trade(sym)
                    exit_price = quote.price
                except Exception:
                    exit_price = entry
                pnl_pct = (exit_price / entry - 1) * 100 if entry else 0.0
                append_trade_row({
                    "time_opened": pos.get("opened_ts", ""),
                    "time_closed": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "ticker": sym, "entry": f"{entry:.4f}" if entry else "",
                    "exit_price": f"{exit_price:.4f}",
                    "qty": qty, "pnl_pct": f"{pnl_pct:+.2f}", "reason": "end-of-day flatten",
                    "angle_now_at_entry": pos.get("entry_angle_now", ""),
                    "angle_was_at_entry": pos.get("entry_angle_was", ""),
                })
                open_positions.pop(sym, None)
            eod_done_today = now_et.date()
            time.sleep(POLL_SECONDS)
            continue

        for sym, pos in list(open_positions.items()):
            try:
                quote = api.get_latest_trade(sym)
                price = quote.price
            except Exception as e:
                log(f"WARN could not get price for open position {sym}: {e}")
                continue
            pos["peak"] = max(pos["peak"], price)
            entry = pos.get("entry", price)
            peak = pos["peak"]
            qty = pos.get("qty", 1)

            stop_hit = entry and price <= entry * (1 - STOP_PCT / 100)
            trail_hit = peak > entry and price <= peak * (1 - TRAIL_PCT / 100)

            if not (stop_hit or trail_hit):
                continue

            reason = "stop-loss" if stop_hit else "trailing-stop"
            try:
                result = api.market_sell(sym, qty)
                log(f"{reason.upper()} {sym} @ {price:.4f} (entry {entry:.4f}, peak {peak:.4f}) "
                    f"-- sell order result {result}")
            except Exception as e:
                log(f"{reason.upper()}-ERR {sym}: {e}")
                continue
            pnl_pct = (price / entry - 1) * 100 if entry else 0.0
            append_trade_row({
                "time_opened": pos.get("opened_ts", ""),
                "time_closed": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": sym, "entry": f"{entry:.4f}" if entry else "",
                "exit_price": f"{price:.4f}",
                "qty": qty, "pnl_pct": f"{pnl_pct:+.2f}", "reason": reason,
                "angle_now_at_entry": pos.get("entry_angle_now", ""),
                "angle_was_at_entry": pos.get("entry_angle_was", ""),
            })
            open_positions.pop(sym, None)

        time.sleep(POLL_SECONDS)


def decision_worker(api):
    """Waits for your typed answer, only THEN talks to TradeStation. Logs a
    complete round-trip row to the CSV the moment a position actually closes."""
    while _RUNNING:
        try:
            action, symbol, price, ts = decision_queue.get(timeout=1)
        except queue.Empty:
            continue

        if action == "APPROVE_SELL":
            log(f"APPROVED by you -- selling {symbol} @ ~{price:.4f}")
            pos = open_positions.get(symbol, {})
            qty = pos.get("qty", 1)
            try:
                result = api.market_sell(symbol, qty)
                log(f"Sell order result for {symbol}: {result}")
                entry = pos.get("entry", price)
                pnl_pct = (price / entry - 1) * 100 if entry else 0.0
                append_trade_row({
                    "time_opened": pos.get("opened_ts", ""),
                    "time_closed": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "ticker": symbol, "entry": f"{entry:.4f}", "exit_price": f"{price:.4f}",
                    "qty": qty, "pnl_pct": f"{pnl_pct:+.2f}", "reason": "approved sell",
                    "angle_now_at_entry": pos.get("entry_angle_now", ""),
                    "angle_was_at_entry": pos.get("entry_angle_was", ""),
                })
                open_positions.pop(symbol, None)
            except Exception as e:
                log(f"ERROR placing sell for {symbol}: {e}")
        elif action == "SKIP_SELL":
            log(f"SKIPPED sell by you: {symbol} @ ~{price} -- still holding, will ask again if condition persists")
        elif action == "EXPIRED_SELL":
            log(f"Sell alert EXPIRED (no answer within {POPUP_TIMEOUT_SECONDS}s): {symbol} -- still holding")


def adopt_existing_positions(api):
    """ADDED (2026-09-21, Gary's decision): at startup, pull in any REAL
    positions already open in the broker account for symbols in our
    universe (e.g. carried over from a previous session, like BEZ from
    9/18) and adopt them into open_positions, so the automatic
    stop-loss/trailing-stop/EOD-flatten protections apply to them too --
    not just to positions this script opens itself. Without this, a
    carryover position would sit invisible to this script forever, with
    no automatic protection, until sold manually in TradeStation. This
    means you do NOT need to manually liquidate carryover positions
    before starting the engine -- it will pick them up the moment it
    starts and apply the same -2% stop / 2.5% trailing-stop / EOD-flatten
    rules to them as any position it opens itself. Entry price is taken
    from the broker's own average price; entry-angle fields are left
    blank since we don't know what the signal looked like when it was
    actually bought (doesn't affect the exit logic, which only uses
    entry/peak price)."""
    try:
        real_positions = api.list_positions()
    except Exception as e:
        log(f"WARN could not check for existing positions at startup: {e}")
        return
    now_et = et_now()
    for sym, real in real_positions.items():
        if sym not in SYMBOLS:
            continue
        qty = real.get("qty", 0)
        if qty <= 0 or sym in open_positions:
            continue
        entry = real.get("avg", 0) or 0
        try:
            price_now = api.get_latest_trade(sym).price
        except Exception:
            price_now = entry
        open_positions[sym] = {
            "entry": entry, "peak": max(entry, price_now), "qty": qty,
            "opened_ts": now_et.strftime("%Y-%m-%d %H:%M:%S") + " (adopted at startup)",
            "entry_angle_now": "", "entry_angle_was": "",
        }
        log(f"ADOPTED existing position at startup: {sym} qty={qty} avg_entry={entry:.4f} "
            f"current={price_now:.4f} -- now under automatic stop-loss/trailing-stop/"
            f"EOD-flatten protection, same as any position this engine opens itself")


def main():
    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)

    api = TradeStationClient()
    api._access_token()
    acct = api.get_account()
    ensure_csv()
    adopt_existing_positions(api)

    log(f"Connected to TradeStation account {acct.account_number} "
        f"(status={acct.status}, env={api.env}, dry_run={api.dry_run})")
    if api.dry_run:
        log("DRY_RUN is ON -- approvals will be logged but NOT sent as real orders. "
            "Set DRY_RUN=0 in .env once you're ready to trade live with this.")

    bal = api.get_balance()
    log("=" * 70)
    log("RSI MOD2 -- ENGINE A (single, self-contained file)")
    log("=" * 70)
    if bal:
        log(f"BALANCE  equity=${bal['equity']:,.2f}  cash=${bal['cash']:,.2f}")
        if api.env == "live" and not api.dry_run:
            log("         ^ CHECK THIS ACCOUNT. Ctrl-C now if it is wrong.")
    log(f"MODE     {RSI_MOD2_MODE}  |  SHALLOWED={SHALLOWED:.1f}  "
        f"MIN_BEND_PCT={MIN_BEND_PCT:.2f}% over {BEND_LOOKBACK_BARS} bars  "
        f"STOP_PCT={STOP_PCT:.1f}%  TRAIL_PCT={TRAIL_PCT:.1f}%  "
        f"TRADE_DOLLARS=${TRADE_DOLLARS}")
    log(f"SUPPRESS per-ticker while open + global slots<={MAX_SLOTS} + same-pair lock")
    log(f"UNIVERSE ({len(SYMBOLS)} tickers) {', '.join(SYMBOLS)}")
    log(f"LOG      {LOG_CSV}")
    if api.env == "live" and not api.dry_run:
        log("*** LIVE TRADING ENABLED -- real orders will be sent ***")
    log("=" * 70)
    log("Watching quietly now -- will only print again when a real signal, "
        "buy, sell, or heartbeat happens. This is normal; no news is good news.")

    ui = TerminalApproval()

    threading.Thread(target=ui.run_forever, daemon=True).start()
    threading.Thread(target=signal_worker, args=(api,), daemon=True).start()
    threading.Thread(target=sell_monitor_worker, args=(api,), daemon=True).start()
    threading.Thread(target=reconcile_positions_worker, args=(api,), daemon=True).start()
    threading.Thread(target=decision_worker, args=(api,), daemon=True).start()
    threading.Thread(target=status_board_worker, daemon=True).start()

    HEARTBEAT_SECONDS = 600
    last_heartbeat = time.time()
    while _RUNNING:
        time.sleep(1)
        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            held = list(open_positions.keys())
            log(f"(heartbeat) still watching {len(SYMBOLS)} tickers -- "
                f"holding: {held if held else 'nothing right now'}")
            last_heartbeat = time.time()


if __name__ == "__main__":
    main()
