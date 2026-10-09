#!/usr/bin/env python3
"""
============================================================
  ENGINE C -- OPENING STRATEGY (EXPERIMENTAL)  (built 2026-09-21
  night, Gary's decision, in a hurry -- Alexander isn't in until
  after the open tomorrow, so Gary is putting this in himself)
  SINGLE CYBORG style, same as Engine A: buy fires AUTOMATICALLY,
  no approval prompt -- Gary's own words tonight: "it's going to
  move so fast that I won't have time to think about it... the
  engine buys and I sell." Sell is automatic too (stop-loss /
  trailing-stop / end-of-day), same safety nets as Engine A and
  B -- Gary can ALSO sell manually any time, straight in
  TradeStation, and this script will detect that and update
  itself (see reconcile_positions_worker) rather than staying
  stuck thinking the position is still open.
============================================================
 
CHANGED 2026-10-08 (Gary's decision), three things vs the 9/21 version:
  1. Gap band: red must be ABOVE black by GAP_MIN_PCT (0.5%) to GAP_MAX_PCT
     (2.0%) -- "not too close, not too far". Was: any gap up to 2.0% either side.
  2. STALE-BAR GUARD: signals are ignored unless the newest bar is from today
     and under 4 minutes old (see STALE_BAR_MINUTES). Log shows STALE-SKIP.
  3. Logs "FIRST FRAME <ticker> ... premarket_used=True/False" once per ticker.
Still 1 share per trade, auto-buy, automatic stop/trail/EOD sell.
 
BE HONEST WITH YOURSELF ABOUT WHAT THIS IS: this signal was backtested
against exactly ONE day (Friday 9/18), and only after two rushed fixes
found late Sunday night (restricting entries to the opening window, and
realizing the earlier "no restriction" version was firing 500+ times a
day and losing badly). The result on that one day: 12 signals across 12
different tickers, roughly half went on to move 1%+ within 10 minutes,
average peak was about +1.6% within 10 minutes. That is a promising
SIGN, not a proven system. Trade size is deliberately $1 (forces 1
share) for exactly this reason -- Gary's own words: "what we're testing
is a buy." This is here to generate real, live feedback on the entry
signal, not to make money yet.
 
ONE SELF-CONTAINED FILE, same pattern as Engine A and Engine B -- does
NOT import either of those files, so nothing here can break them and
nothing in them can break this.
 
SIGNAL (opening-only, 2-part rule):
    1. Only looks for a NEW entry in the first OPEN_WINDOW_MINUTES (20)
       minutes of the regular session (9:30-9:50 ET) -- backtesting
       tonight found this restriction alone was almost the entire
       reason the rule worked at all; the same math run all day long
       lost badly (513 trades, -34% on Friday's data) because a rising
       black line + small red/black gap is common all day, just not
       usually followed by a real move except right at the open.
    2. Black line (EMA20) must be RISING (current bar higher than the
       previous bar) -- this is Gary's core idea: "the main thing we're
       depending on is a good upward trend in the black line."
    3. Red line (HMA7) must not be too far from the black line -- the
       gap between them, as a percent of price, must be GAP_MAX_PCT
       (2.0%) or less. Gary's own words: "the red line can't be too
       divergent from the black line... they kind of climb up at the
       same angle." A gap that's blown out wide means the red line has
       "shot away" and this isn't the setup Gary is describing.
    Both checked together, same bar.
 
    NOT YET IN HERE, on purpose, because it was never backtested before
    tonight ran out of time: a direct "steepness" requirement on the
    black line itself (Gary's visual "60-80 degrees" -- translated
    tonight into real terms using BEZ's actual chart scale, that's
    roughly a 1%+ move in the first 1-2 minutes, but that number has
    NOT been tested against real data yet). Right now "black line
    rising" is the only steepness check -- ANY positive slope qualifies,
    not just a fast one. Expect this to fire on some weaker opens than
    Gary has in mind. That's deliberately left for tomorrow, once there's
    time to test it properly instead of guessing.
 
PRE-MARKET WARMUP (ATTEMPTED, UNVERIFIED): Gary wants the black/red
    lines already "hot" at 9:30 instead of starting cold, using the last
    PREMARKET_MINUTES (20) minutes before the open. This file attempts
    that by (a) asking TradeStation for extended-hours bars via a
    "sessiontemplate" request parameter, and (b) widening the indicator
    window to start at PREMARKET_OPEN_ET instead of MARKET_OPEN_ET.
    HONESTLY: neither piece has been tested against a real TradeStation
    response -- there was no pre-market data available tonight to verify
    the parameter name or the response shape. If it doesn't work, this
    degrades SAFELY to a cold start at 9:30 (same behavior already
    backtested) -- it will NOT crash the engine either way. First thing
    to check tomorrow morning: the startup log line "PREMARKET" tells
    you whether bars before 9:30 actually came back. If it says
    "0 premarket bars", the warmup didn't work and the lines start cold,
    same as tonight's tested version.
 
ENTRY: fires AUTOMATICALLY the instant the signal appears -- no approval
    prompt, no waiting on a typed answer. Gary's reasoning (2026-09-21
    night): this moves too fast right at the open to react to a prompt in
    time; he wants the engine to make the call on entry and handle exit
    himself by watching the screen. Size is $1 notional -- forces exactly
    1 share per trade regardless of price. Deliberate: "one share is
    enough to tell" whether the signal itself is any good, without real
    money on the line while it's this early.
 
EXIT: automatic, no approval needed, same three safety nets as Engine A
    and B: -2% hard stop (STOP_PCT), 2.5% trailing stop once in profit
    (TRAIL_PCT), end-of-day flatten at EOD_FLATTEN_ET (15:59 ET). These
    run as a BACKSTOP underneath whatever Gary decides by watching the
    screen -- he can sell manually, any time, straight in TradeStation,
    and reconcile_positions_worker (below) will notice and clear the
    position from this script's memory rather than leaving it stuck
    thinking the position is still open. Gary was clear tonight that a
    fancier automatic exit (like "sell when black line turns down")
    didn't actually help in testing once the opening-window fix was in
    place -- it was the window restriction doing the work, not the exit
    -- so this keeps the same exit logic already proven in Engine A and
    B rather than add untested complexity on that side too.
 
SAFETY PROTECTIONS: same as Engine B -- MAX_SLOTS cap, reconcile-with-
    real-account worker (so a position you sell manually in TradeStation
    doesn't stay stuck as "still held" in this script's memory).
 
LOGGING: own separate CSV, never touches Engine A's or B's log files.
 
HOW TO RUN:
    cd ~/rsi_system
    set -a; source .env; set +a
    ~/algotrend1v5/venv/bin/python3 rsi_opening_strategy_C.py
 
Needs to run on a computer/server that stays on and connected during
market hours. Runs alongside Engine A and Engine B -- separate terminal,
separate process, does not touch either of them.
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
 
EMA_LEN = 20   # black
HMA_LEN = 7    # red
 
GAP_MIN_PCT = 0.5   # ADDED 2026-10-08 (Gary): the red line must sit at least this
                    # far ABOVE the black line (percent of price). Too close
                    # = no real momentum yet (in the 10/8 history test the
                    # 0-0.5% gap group had the weakest results). Together with
                    # GAP_MAX_PCT this is the "not too close, not too far" band.
                    # FIRST GUESS -- not yet proven. Edit both numbers to tune.
GAP_MAX_PCT = 2.0   # "the red line isn't too far away from the black line" --
                    # gap between red and black, as a percent of price, must
                    # be at or under this to qualify. Tested tonight (with
                    # the opening-window restriction) on Friday's data: 12
                    # signals across 12 tickers, 6 of 12 reached +1% within
                    # 10 minutes, average peak +1.59% within 10 minutes.
                    # NOT yet tested at other values (1.0%, 1.5%, 3.0%) --
                    # 2.0% was simply the number used in tonight's one test.
 
OPEN_WINDOW_MINUTES = 20   # only look for NEW entries in the first N minutes
                           # of the regular session. This was the single
                           # biggest factor in tonight's testing -- removing
                           # this restriction (running the same math all day)
                           # produced 513 trades and -34% on Friday's data;
                           # adding it back produced 17 trades and +15.6%.
                           # The black line rarely sustains a real upward run
                           # later in the day the way it does right at the
                           # open, so this rule is deliberately opening-only.
 
PREMARKET_MINUTES = 20   # ATTEMPT to warm up the black/red lines using this
                         # many minutes of pre-market data before 9:30.
                         # UNVERIFIED -- see PRE-MARKET WARMUP note above.
PREMARKET_OPEN_ET = "09:10"   # 20 minutes before the 9:30 open
 
WARMUP_BARS = 8   # minimum bars (regular-session OR pre-market) before the
                  # black/red lines are trusted enough to check. Lower than
                  # Engine A/B's 12 on purpose: if pre-market warmup works,
                  # the lines are already mature by 9:30 and this barely
                  # matters; if it doesn't work, this still requires a
                  # little settling time before firing on the very first,
                  # noisiest bars of the day.
 
# ---- exit rule -- SAME as Engine A/B, proven, not re-invented tonight ----
STOP_PCT = 2.0
TRAIL_PCT = 2.5
 
TRADE_DOLLARS = 1   # Gary's explicit choice tonight (2026-09-21 night):
                    # "we could just test it with one share... what we're
                    # testing is a buy." Forces exactly 1 share/trade via
                    # the "minimum 1 share" floor below, regardless of
                    # price. Raise this only once this signal has actually
                    # been watched live and Gary decides it's worth more.
 
 
def shares_for_dollars(price: float) -> int:
    """How many whole shares $TRADE_DOLLARS buys at this price, min 1."""
    if price is None or price <= 0:
        return 1
    return max(1, int(TRADE_DOLLARS / price))
 
 
MAX_SLOTS = 10   # same cap style as Engine A/B's normal (non-verification)
                 # setting -- this isn't a "watch everything fire" test, it's
                 # a real (if tiny) trading test.
 
RSI_MOD2_MODE = "OPENING_STRATEGY_C (Single Cyborg: auto-buy, you sell)"
 
# Same universe as Engine A and B, so all three always watch the same
# tickers.
PAIRS = [
    ("NBIL", "NBIZ"),
    ("IRE",  "IREZ"),
    ("SNXX", "SNDQ"),
    ("LITX", "LITZ"),
    ("IONX", "IONZ"),
    ("BEX",  "BEZ"),
    ("AAOX", "AAOZ"),   # ADDED 2026-10-08 (Gary) -- symbols not yet confirmed live at TradeStation
    ("ASTX", "ASTN"),   # ADDED 2026-10-08 (Gary). BMNU/BMNZ REMOVED 2026-10-08. (BEX/BEZ was already in the list.)
    ("MSTU", "MSTZ"),
    ("CRCG", "CRCD"),
    ("SMCX", "SMCZ"),
]
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
EOD_FLATTEN_ET = "15:59"
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or 20)
 
LOG_CSV = os.getenv("MOD2_OPENING_LOG") or "rsi_opening_strategy_C_log.csv"
LOG_COLUMNS = [
    "time_opened", "time_closed", "ticker", "entry", "exit_price",
    "qty", "pnl_pct", "reason", "gap_pct_at_entry", "premarket_bars_used",
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
    return wma(2 * wma(s, 3) - wma(s, n), 3)
 
 
# ------------------------------------------------------------ session --------
 
def session_filter_with_premarket(df):
    """Widened version of the usual session filter -- starts
    PREMARKET_MINUTES before the open instead of exactly at 9:30, so the
    black/red lines have a head start if the pre-market bars actually
    came back from the API. If they didn't come back, this simply
    behaves like a normal 9:30-start filter with nothing extra in it --
    safe either way."""
    if df is None or df.empty:
        return df
    et = df.tz_convert(ET)
    try:
        et = et.between_time(PREMARKET_OPEN_ET, MARKET_CLOSE_ET, inclusive="both")
    except TypeError:
        et = et.between_time(PREMARKET_OPEN_ET, MARKET_CLOSE_ET)
    et = et[et.index.weekday < 5]
    return et.tz_convert("UTC")
 
 
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
 
    def _bars_from_response(self, data):
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
 
    def get_bars_premarket_attempt(self, symbol, limit=None):
        """Tries to get bars WITH pre-market/extended-hours data included,
        via TradeStation's 'sessiontemplate' request parameter. UNVERIFIED
        tonight -- if this parameter name is wrong, or the API rejects it,
        or it's simply ignored and only regular-session bars come back,
        this falls back to a plain regular-session request instead of
        crashing. Returns (dataframe_or_None, premarket_bars_found: bool)."""
        params = {"interval": BAR_MINUTES, "unit": "Minute",
                  "barsback": limit or BAR_LOOKBACK,
                  "sessiontemplate": "USEQPreAndPost"}
        try:
            data = self._get(f"/marketdata/barcharts/{symbol}", params)
            df = self._bars_from_response(data)
        except Exception as e:
            log(f"WARN {symbol}: pre-market bar request failed ({e}) -- "
                f"falling back to regular-session bars")
            df = None
 
        if df is None:
            # fall back to a plain request with no session template at all
            plain_params = {"interval": BAR_MINUTES, "unit": "Minute",
                             "barsback": limit or BAR_LOOKBACK}
            data = self._get(f"/marketdata/barcharts/{symbol}", plain_params)
            df = self._bars_from_response(data)
            return df, False
 
        # did we actually get any bars before 9:30 ET today?
        et_idx = df.index.tz_convert(ET)
        today = et_idx[-1].date() if len(et_idx) else None
        oh, om = 9, 30
        has_premarket = bool(
            today is not None and
            ((et_idx.date == today) &
             ((et_idx.hour < oh) | ((et_idx.hour == oh) & (et_idx.minute < om)))
             ).any()
        )
        return df, has_premarket
 
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
    black_now: float
    black_prev: float
    red_now: float
    gap_pct: float
    bars_since_open: int   # minutes since 9:30 -- used for the open-window gate
    premarket_bars_used: bool
 
 
def build_frame(df: pd.DataFrame, premarket_found: bool) -> Frame:
    """Computes black/red using every bar available today (pre-market
    included, if it came back from the API -- see session_filter_with_
    premarket). bars_since_open counts ONLY regular-session bars (9:30
    onward), since that's what OPEN_WINDOW_MINUTES gates against -- pre-
    market bars warm up the lines but don't count toward "minutes since
    the open" for entry timing."""
    et_idx = df.index.tz_convert(ET)
    today = et_idx[-1].date()
    session_mask = (et_idx.date == today)
    today_df = df[session_mask]
    today_et_idx = today_df.index.tz_convert(ET)
 
    close = today_df["close"]
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
 
    oh, om = 9, 30
    regular_mask = (today_et_idx.hour > oh) | ((today_et_idx.hour == oh) & (today_et_idx.minute >= om))
    bars_since_open = int(regular_mask.sum()) - 1  # 0-indexed: 0 = the 9:30 bar itself
 
    def _safe(series, pos):
        try:
            v = series.iloc[pos]
            return float(v) if not (isinstance(v, float) and math.isnan(v)) else float("nan")
        except Exception:
            return float("nan")
 
    black_now = _safe(black, -1)
    black_prev = _safe(black, -2)
    red_now = _safe(red, -1)
    gap_pct = float("nan")
    if not (math.isnan(red_now) or math.isnan(black_now)) and black_now != 0:
        gap_pct = (red_now - black_now) / black_now * 100
 
    return Frame(
        ts=df.index[-1],
        close=float(close.iloc[-1]),
        black_now=black_now,
        black_prev=black_prev,
        red_now=red_now,
        gap_pct=gap_pct,
        bars_since_open=bars_since_open,
        premarket_bars_used=premarket_found,
    )
 
 
def opening_signal_fires(fr: Frame) -> bool:
    """The rule tested tonight: black line rising (current bar higher
    than the previous bar) AND the red line isn't too far from black
    (|gap| <= GAP_MAX_PCT), both checked at the same moment. See the
    module docstring for exactly what this did and didn't prove on
    Friday's data."""
    if math.isnan(fr.black_now) or math.isnan(fr.black_prev) or math.isnan(fr.gap_pct):
        return False
    black_rising = fr.black_now > fr.black_prev
    # CHANGED 2026-10-08: red must be ABOVE black by GAP_MIN_PCT..GAP_MAX_PCT
    gap_ok = GAP_MIN_PCT <= fr.gap_pct <= GAP_MAX_PCT
    return black_rising and gap_ok
 
 
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
 
 
_RUNNING = True
 
 
def _stop(*_):
    global _RUNNING
    _RUNNING = False
 
 
# ------------------------------------------------------------ auto-buy -------
 
open_positions = {}
latest_frame = {}
STALE_BAR_MINUTES = 4          # newest bar must be from today and no older than this
_stale_logged = set()          # log each stale-skip once per symbol
_first_frame_logged = set()    # log premarket_used once per symbol
already_tried_today = {}   # symbol -> date string, so we don't keep re-buying
                           # the same symbol over and over within the opening
                           # window -- tonight's backtest only looked at the
                           # FIRST qualifying signal per symbol per day
 
 
def slots_in_use():
    return len(open_positions)
 
 
def signal_worker(api):
    """Watches every symbol during the opening window and BUYS
    IMMEDIATELY the instant the signal fires -- no approval prompt.
    Gary's call tonight: this moves too fast to react to a prompt in
    time. Selling is his to do by watching the screen; the automatic
    stop-loss/trailing-stop/EOD-flatten in sell_monitor_worker runs
    underneath as a backstop regardless."""
    log(f"Opening-strategy signal worker started (AUTO-BUY, no approval prompt). "
        f"Window=first {OPEN_WINDOW_MINUTES} min of session. Black line must be "
        f"rising. Red above black by {GAP_MIN_PCT:.1f}%..{GAP_MAX_PCT:.1f}%. MAX_SLOTS={MAX_SLOTS}.")
 
    while _RUNNING:
        now_et = et_now()
        if not in_session(now_et):
            time.sleep(POLL_SECONDS)
            continue
 
        today_str = str(now_et.date())
 
        for sym in SYMBOLS:
            if sym in open_positions:
                continue
            if already_tried_today.get(sym) == today_str:
                continue
 
            try:
                df, premarket_found = api.get_bars_premarket_attempt(sym)
                df = session_filter_with_premarket(df) if df is not None else None
                if df is None or len(df) < WARMUP_BARS:
                    continue
                fr = build_frame(df, premarket_found)
                latest_frame[sym] = fr
            except Exception as e:
                log(f"WARN {sym}: {e}")
                continue
 
            # ADDED 2026-10-08: ignore bars that are not from TODAY or are
            # stale. At the open the newest bar can still be yesterday's
            # close; acting on it gave a false signal in Engine B on 10/7
            # and here it could also wrongly mark a ticker "tried" for the day.
            # Skipping does NOT mark the ticker as tried.
            try:
                last_bar_et = pd.Timestamp(fr.ts).tz_convert(ET)
                bar_age_min = (now_et - last_bar_et).total_seconds() / 60.0
            except Exception:
                continue
            if last_bar_et.date() != now_et.date() or bar_age_min > STALE_BAR_MINUTES:
                if sym not in _stale_logged:
                    _stale_logged.add(sym)
                    log(f"STALE-SKIP {sym}: newest bar is {last_bar_et.strftime('%Y-%m-%d %H:%M')} ET "
                        f"({bar_age_min:.1f} min old) -- waiting for fresh bars from today")
                continue
            if sym not in _first_frame_logged:
                _first_frame_logged.add(sym)
                log(f"FIRST FRAME {sym}: newest bar {last_bar_et.strftime('%H:%M')} ET  "
                    f"premarket_used={premarket_found}")
 
            if fr.bars_since_open < 0:
                continue  # not open yet / no regular-session bar seen
            if fr.bars_since_open > OPEN_WINDOW_MINUTES:
                # outside the opening window -- mark as "tried" so we don't
                # keep re-checking this symbol pointlessly all day
                already_tried_today[sym] = today_str
                continue
 
            if not opening_signal_fires(fr):
                continue
 
            already_tried_today[sym] = today_str  # only ever ask once per symbol per day
 
            if slots_in_use() >= MAX_SLOTS:
                log(f"SLOT-SKIP {sym} (slots full {MAX_SLOTS})")
                continue
 
            log(f"OPENING SIGNAL {sym} @ {fr.close:.4f}  bars_since_open={fr.bars_since_open}  "
                f"gap={fr.gap_pct:+.2f}%  premarket_used={fr.premarket_bars_used} "
                f"-- AUTO-BUYING NOW (1 share, ~${TRADE_DOLLARS} notional)")
 
            qty = shares_for_dollars(fr.close)
            try:
                result = api.market_buy(sym, qty)
                log(f"Buy order result for {sym}: {result} ({qty} shares @ ${fr.close:.2f})")
                open_positions[sym] = {
                    "entry": fr.close, "peak": fr.close, "qty": qty,
                    "opened_ts": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "gap_pct_at_entry": fr.gap_pct,
                    "premarket_bars_used": fr.premarket_bars_used,
                }
                log(f"Now tracking open position: {sym} entry=${fr.close:.2f} -- "
                    f"WATCH THIS ONE YOURSELF; sell manually in TradeStation any "
                    f"time, or let the automatic stop-loss/trailing-stop/EOD "
                    f"flatten handle it")
            except Exception as e:
                log(f"ERROR buying {sym}: {e}")
 
        time.sleep(POLL_SECONDS)
 
 
STATUS_BOARD_SECONDS = 60
 
 
def status_board_worker():
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
                lines.append(f"{sym}:no data yet")
                continue
            held = " [HOLDING]" if sym in open_positions else ""
            tried = " [done-today]" if already_tried_today.get(sym) == str(now_et.date()) and sym not in open_positions else ""
            lines.append(f"{sym}:bar={fr.bars_since_open:>2d} gap={fr.gap_pct:+5.2f}%{held}{tried}")
        log("--- status board ---")
        for i in range(0, len(lines), 3):
            print("   " + "   |   ".join(lines[i:i + 3]), flush=True)
 
 
RECONCILE_SECONDS = 10
 
 
def reconcile_positions_worker(api):
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
                    f"from memory.")
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
                        "gap_pct_at_entry": pos.get("gap_pct_at_entry", ""),
                        "premarket_bars_used": pos.get("premarket_bars_used", ""),
                    })
 
 
def sell_monitor_worker(api):
    """Same three automatic safety exits as Engine A/B: stop-loss,
    trailing stop, end-of-day flatten. No approval needed for any of
    these -- proven pattern, not re-invented for this experimental
    engine."""
    eod_done_today = None
    while _RUNNING:
        now_et = et_now()
        if not in_session(now_et):
            time.sleep(POLL_SECONDS)
            continue
 
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
                    "gap_pct_at_entry": pos.get("gap_pct_at_entry", ""),
                    "premarket_bars_used": pos.get("premarket_bars_used", ""),
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
                "gap_pct_at_entry": pos.get("gap_pct_at_entry", ""),
                "premarket_bars_used": pos.get("premarket_bars_used", ""),
            })
            open_positions.pop(sym, None)
 
        time.sleep(POLL_SECONDS)
 
 
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
    log("ENGINE C -- OPENING STRATEGY (EXPERIMENTAL, self-contained file)")
    log("=" * 70)
    if bal:
        log(f"BALANCE  equity=${bal['equity']:,.2f}  cash=${bal['cash']:,.2f}")
        if api.env == "live" and not api.dry_run:
            log("         ^ CHECK THIS ACCOUNT. Ctrl-C now if it is wrong.")
    log(f"MODE     {RSI_MOD2_MODE}")
    log(f"RULE     black line rising + red ABOVE black by {GAP_MIN_PCT:.1f}%..{GAP_MAX_PCT:.1f}%, "
        f"first {OPEN_WINDOW_MINUTES} min of session only, first signal per "
        f"ticker per day only")
    log(f"EXIT     STOP_PCT={STOP_PCT:.1f}%  TRAIL_PCT={TRAIL_PCT:.1f}%  "
        f"EOD_FLATTEN={EOD_FLATTEN_ET}  (all automatic, no approval)")
    log(f"SIZE     TRADE_DOLLARS=${TRADE_DOLLARS} (forces 1 share/trade)")
    log(f"PREMARKET attempting {PREMARKET_MINUTES} min warmup via 'sessiontemplate' "
        f"param -- UNVERIFIED, check first few signals' log lines for "
        f"'premarket_used=True/False' to see if it actually worked")
    log(f"MAX_SLOTS={MAX_SLOTS}")
    log(f"UNIVERSE ({len(SYMBOLS)} tickers) {', '.join(SYMBOLS)}")
    log(f"LOG      {LOG_CSV}")
    if api.env == "live" and not api.dry_run:
        log("*** LIVE TRADING ENABLED -- real orders will be sent ***")
    log("=" * 70)
    log("This is EXPERIMENTAL -- backtested on exactly one day (9/18) with "
        "12 signals total. Watching quietly now.")
 
    threading.Thread(target=signal_worker, args=(api,), daemon=True).start()
    threading.Thread(target=sell_monitor_worker, args=(api,), daemon=True).start()
    threading.Thread(target=reconcile_positions_worker, args=(api,), daemon=True).start()
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
