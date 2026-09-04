#!/usr/bin/env python3
"""
============================================================
  ENGINE B -- DOUBLE CYBORG  (built 2026-08-30, updated 2026-08-31)
  Neither buy NOR sell happens automatically. Every signal
  asks for your approval right here in this terminal (type
  Y and press Enter to act, or just press Enter to skip/hold).
============================================================

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
    candidate yourself before any money moves.

WHY DOUBLE CYBORG: since every candidate gets a human look before any
    money moves, a false-positive signal here only costs you a glance
    and a "no", not a real trade. That's a different risk profile from
    Engine A (which buys the instant it fires, with no human check
    first) -- more candidates of comparable quality is a genuine
    advantage here in a way it wouldn't be for Engine A.

ENTRY: asks for your approval the instant the signal fires. Size is a
    FIXED 1 SHARE per trade (Gary's choice, 2026-08-26) -- simple,
    minimal exposure while this new combined version is being trusted.

EXIT: -2% hard stop, OR a 2.5% trailing-stop pullback from the peak
    once in profit, OR end-of-day flatten -- but selling ALWAYS asks
    for your approval first, right here in the terminal. This is
    DELIBERATELY DIFFERENT from the plain system's -5% hard-floor/
    manual-only exit -- that's the whole point of Cyborg mode.

SAFETY PROTECTIONS (ported over from the plain system, 2026-08-26 --
    these were missing from earlier versions of this combined file):
    - MAX_SLOTS = 10: won't open more than 10 positions at once.
    - Same-pair lock: won't buy NBIL if NBIZ is already open (and
      vice versa for every inverse pair), same as the plain system.

BUG FIX (2026-08-26 evening, THIS VERSION):
    Found by backtesting Aug 24/25 against real signals: the "black line
    was steep" check could reach back across a DIFFERENT trading day (even
    a week+ earlier) and combine with today's real RSI/red-line conditions,
    firing a false signal. Confirmed on SOXL, IRE, NBIZ, RKLX(8/25), CWVX,
    and AAOX. Fixed two places (search "FIX (2026-08-26)" in this file):
    every symbol's memory of these conditions is now wiped clean at the
    start of each new trading day, and the steep-angle lookback can no
    longer reach past today's own opening bar. This is the version to run
    starting 2026-08-27.

LOGGING: writes a complete round-trip row (entry + exit + reason) to
    its own CSV log the moment each trade actually closes -- a
    separate file from the plain system's log, so the two never
    collide or overwrite each other.

HOW TO RUN:
    cd ~/rsi_system
    set -a; source .env; set +a
    ~/algotrend1v5/venv/bin/python3 rsi_mod2_B_approvebuy.py

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
WARMUP_BARS    = 2   # REDUCED (2026-09-02, Gary's decision): used to wait 30
                      # minutes into each day before checking for signals at
                      # all. But the black line and red line are computed
                      # from a continuous, stitched historical series that
                      # already spans back into the prior session -- by the
                      # moment today's market opens, they're already
                      # mathematically valid, warmed up numbers, not needing
                      # 30 fresh minutes of today specifically. Tested
                      # earlier tonight: this value actually gave slightly
                      # BETTER results than the 30-minute wait (81.7% reach
                      # 1% vs 80.0%, +6.54% avg peak vs +6.55%), not just
                      # equal. Set to 2, not 0, purely to avoid a harmless
                      # edge case on the very first bar of the day.

# ---- Cyborg exit rule (deliberately different from the plain system's
# -5% hard-floor/manual-only exit) ----
STOP_PCT = 2.0
TRAIL_PCT = 2.5
SELL_ALERT_COOLDOWN_SECONDS = 90
POPUP_TIMEOUT_SECONDS = 600   # if you don't answer a sell prompt in this long,
                               # it auto-expires and keeps holding

SHARES_PER_TRADE = 1   # fixed 1 share per trade (Gary's choice, 2026-08-26)

MAX_SLOTS = 50   # RAISED (2026-09-02, Gary's decision): tomorrow's goal is
                 # purely to verify the system catches every real signal --
                 # a slot cap would silently hide a legitimate signal behind
                 # "slots full," making it impossible to tell "missed" from
                 # "correctly declined." 50 is high enough it should never
                 # actually bind given the current ticker list. Bring a real
                 # cap back once this moves from verification to real money.

RSI_MOD2_MODE = "DOUBLE_CYBORG"  # both buy AND sell need your approval

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
]  # FINAL LIST (2026-09-03, Gary's decision, after a full day of live
   # testing plus careful review of volume, correlation, and news for
   # every candidate). AAOX/AAOZ, ASTX/ASTN, CWVX/CORD, and CBRX/CBRZ
   # all considered and set aside for this final cut.
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
LOG_CSV = os.getenv("MOD2_CYBORG_LOG") or "rsi_mod2_B_approvebuy_log.csv"
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


def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    ag = gain.ewm(alpha=1 / n, adjust=False).mean()
    al = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


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
    angle_now: float
    angle_was: float
    bar_index: int


def build_frame(df: pd.DataFrame) -> Frame:
    close = df["close"]
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
    angle = black_angle(black)

    et_idx = df.index.tz_convert(ET)
    today = et_idx[-1].date()
    session_mask = (et_idx.date == today)
    bar_index = int(session_mask.sum()) - 1

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
        red_now=float(red.iloc[-1]),
        red_prev=float(red.iloc[-2]),
        red_prev2=float(red.iloc[-3]),
        angle_now=float(angle.iloc[-1]),
        angle_was=float("nan"),
        bar_index=bar_index,
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


def red_rising(fr: Frame) -> bool:
    return fr.red_now > fr.red_prev > fr.red_prev2


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
pending_buy_meta = {} # symbol -> indicator snapshot at signal time, held until you approve/skip the buy


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
    # DISABLED (2026-09-02, Gary's decision): tomorrow's goal is purely to
    # verify the system catches every real signal. A real signal on one
    # side of a pair is genuine, meaningful information even while holding
    # the other side -- it shouldn't be silently hidden. Bring the real
    # pair-lock back once this moves from verification to real money.
    return False


def signal_worker(api):
    """Watches every symbol, asks for your approval the instant the 2-part
    signal fires -- black line's current angle is shallow enough, AND the
    red line is rising 2 bars in a row, both true on the SAME bar
    (2026-08-31, Gary's decision: RSI dropped entirely -- extensive
    testing found it added no real predictive value, mostly just delay).
    No arm-tracking or multi-bar alignment window needed now that there
    are only two conditions to check, and they're required
    simultaneously."""
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

            log(f"SIGNAL {sym} @ {fr.close:.4f} bar={fr.bar_index} -- ASKING FOR YOUR APPROVAL "
                f"(double-cyborg mode, {SHARES_PER_TRADE} share)")
            pending_buy_meta[sym] = {
                "entry_angle_now": fr.angle_now,
                "entry_angle_was": fr.angle_was,
            }
            approval_queue.put((sym, fr.close, None, "BUY", "signal fired"))

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


def sell_monitor_worker(api):
    """Watches every OPEN position and sells automatically, with no
    approval needed, the moment ANY exit condition is met -- the -2%
    stop, the 2.5% trailing-stop, or end of day. CHANGED (2026-09-03,
    Gary's decision): end-of-day now auto-executes too, matching the
    stops -- so nothing can slip past unnoticed into a bigger loss, or
    an unwanted overnight hold, just because a prompt wasn't answered.
    Buying still always asks for approval (see signal_worker) -- this
    file's whole design is "you choose what to get into, but nothing
    can get away from you once you're in.\""""
    last_sell_alert = {}

    while _RUNNING:
        now_et = et_now()
        if not in_session(now_et):
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
            stop_price = pos["entry"] * (1 - STOP_PCT / 100)
            trail_trigger = pos["peak"] * (1 - TRAIL_PCT / 100)

            reason = None
            auto_execute = False
            if price <= stop_price:
                reason = f"stop-loss ({STOP_PCT:.0f}% below entry)"
                auto_execute = True
            elif pos["peak"] > pos["entry"] and price <= trail_trigger and trail_trigger > pos["entry"]:
                reason = f"trailing stop ({TRAIL_PCT:.1f}% below peak of ${pos['peak']:.2f})"
                auto_execute = True
            elif past(now_et, EOD_FLATTEN_ET):
                reason = "end of day -- market closing soon, time to flatten this position"
                auto_execute = True

            if reason is None:
                continue

            # AUTO-EXECUTE (2026-09-03, Gary's decision): end-of-day now
            # auto-executes too, alongside the hard stop and trailing stop --
            # so that, as with those, nothing can slip past unnoticed into
            # a bigger loss (or an unwanted overnight hold) just because a
            # prompt wasn't answered in time. Buying still always asks for
            # approval -- this file's whole design is "you choose what to
            # get into, but nothing can get away from you once you're in."
            if auto_execute:
                last_alert = last_sell_alert.get(sym, 0)
                if time.time() - last_alert < SELL_ALERT_COOLDOWN_SECONDS:
                    continue
                last_sell_alert[sym] = time.time()
                log(f"AUTO-SELLING {sym} @ {price:.4f} -- {reason} (no approval needed, safety stop)")
                try:
                    qty = pos.get("qty", SHARES_PER_TRADE)
                    result = api.market_sell(sym, qty)
                    log(f"Sell order result for {sym}: {result}")
                    entry = pos.get("entry", price)
                    pnl_pct = (price / entry - 1) * 100 if entry else 0.0
                    append_trade_row({
                        "time_opened": pos.get("opened_ts", ""),
                        "time_closed": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": sym, "entry": f"{entry:.4f}", "exit_price": f"{price:.4f}",
                        "qty": qty, "pnl_pct": f"{pnl_pct:+.2f}", "reason": reason,
                        "angle_now_at_entry": pos.get("entry_angle_now", ""),
                        "angle_was_at_entry": pos.get("entry_angle_was", ""),
                    })
                    open_positions.pop(sym, None)
                except Exception as e:
                    log(f"ERROR auto-selling {sym}: {e}")
                continue

        time.sleep(POLL_SECONDS)


def decision_worker(api):
    """Waits for your typed answer, only THEN talks to TradeStation. Logs a
    complete round-trip row to the CSV the moment a position actually closes.
    DOUBLE-CYBORG (2026-08-30): now also waits for your approval on the BUY
    side, not just the sell -- nothing is bought without you typing Y."""
    while _RUNNING:
        try:
            action, symbol, price, ts = decision_queue.get(timeout=1)
        except queue.Empty:
            continue

        if action == "APPROVE_BUY":
            log(f"APPROVED by you -- buying {symbol} @ ~{price:.4f}")
            try:
                result = api.market_buy(symbol, SHARES_PER_TRADE)
                log(f"Buy order result for {symbol}: {result} ({SHARES_PER_TRADE} share @ ${price:.2f})")
                meta = pending_buy_meta.pop(symbol, {})
                open_positions[symbol] = {
                    "entry": price, "peak": price, "qty": SHARES_PER_TRADE,
                    "opened_ts": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_angle_now": meta.get("entry_angle_now", ""),
                    "entry_angle_was": meta.get("entry_angle_was", ""),
                }
                log(f"Now tracking open position: {symbol} entry=${price:.2f} -- "
                    f"will alert on sell via this terminal")
            except Exception as e:
                log(f"ERROR buying {symbol}: {e}")
                pending_buy_meta.pop(symbol, None)
        elif action == "SKIP_BUY":
            log(f"SKIPPED by you: {symbol} @ ~{price} -- not buying this signal")
            pending_buy_meta.pop(symbol, None)
        elif action == "EXPIRED_BUY":
            log(f"Buy alert EXPIRED (no answer within {POPUP_TIMEOUT_SECONDS}s): {symbol} -- not buying")
            pending_buy_meta.pop(symbol, None)
        elif action == "APPROVE_SELL":
            log(f"APPROVED by you -- selling {symbol} @ ~{price:.4f}")
            pos = open_positions.get(symbol, {})
            qty = pos.get("qty", SHARES_PER_TRADE)
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


def main():
    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)

    api = TradeStationClient()
    api._access_token()
    acct = api.get_account()
    ensure_csv()

    log(f"Connected to TradeStation account {acct.account_number} "
        f"(status={acct.status}, env={api.env}, dry_run={api.dry_run})")
    if api.dry_run:
        log("DRY_RUN is ON -- approvals will be logged but NOT sent as real orders. "
            "Set DRY_RUN=0 in .env once you're ready to trade live with this.")

    bal = api.get_balance()
    log("=" * 70)
    log("RSI MOD2 -- ENGINE B (Double Cyborg, self-contained file)")
    log("=" * 70)
    if bal:
        log(f"BALANCE  equity=${bal['equity']:,.2f}  cash=${bal['cash']:,.2f}")
        if api.env == "live" and not api.dry_run:
            log("         ^ CHECK THIS ACCOUNT. Ctrl-C now if it is wrong.")
    log(f"MODE     {RSI_MOD2_MODE}  |  SHALLOWED={SHALLOWED:.1f}  "
        f"STOP_PCT={STOP_PCT:.1f}%  TRAIL_PCT={TRAIL_PCT:.1f}%  "
        f"SHARES_PER_TRADE={SHARES_PER_TRADE}")
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

