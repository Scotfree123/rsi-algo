#!/usr/bin/env python3
"""
CHANGED 2026-10-25 (Gary's decision) -- THIS FILE: rsi_engineB_redline.py
    Built from rsi_engineB_crossup2.py (a59d86a6). ENGINE B IS NOW A "LOOK, THEN DECIDE" ALERT SCREEN.
    1. NEW SIGNAL "RED-LINE BUY" (replaces the old RED TURN-UP): the red line's rise over the last
       3 one-minute bars, as a percent of price per minute, converted to an angle, is at least
       REDLINE_MIN_ANGLE (13 degrees, about +0.7% in 3 minutes); red is still rising this bar;
       red is BELOW black; the black line's 10-minute angle is above -35 (REDLINE_BLACK_GATE,
       was -15). Strength word from the same angle: STRONG >= 25, MEDIUM >= 18, WEAK >= 13.
    2. STEEP CROSS-UP kept exactly as it was (price angle >= 30, black gate -15). Printed "CROSS-UP BUY".
    3. NO MORE Y/Enter questions. Alerts go on a numbered BUY ALERT list under the status board.
       To buy: type the number and press Enter (several numbers with a space also work). Doing
       nothing buys nothing. An alert leaves the list when its signal ends, when you buy it, when you
       hold it, or after ALERT_LIST_MAX_MINUTES (10). Numbers never change while a stock is on the
       list and are never reused during a run. An alert stays up at least ALERT_MIN_MINUTES (3) even if its
       signal ends sooner, so you have time to look. A stock never appears twice (a red-line alert that
       then crosses up stays on the same number and becomes CROSS-UP BUY).
    4. At most MAX_NEW_PER_MINUTE (3) NEW alerts are shown per minute, strongest first; the rest are
       only written to the alerts CSV. Every alert (shown or not) goes to engineB_alerts_log.csv.
    5. LINES NOW USE PRE-MARKET BARS (04:00 ET on) when a stock has at least MIN_PM_BARS (10) of them
       today, so alerts can start at the open. Otherwise that stock falls back to lines from 9:30.
       Needs TradeStation sessiontemplate=USEQPreAndPost. A PREMARKET CHECK prints at startup.
    6. 14 stocks, alphabetical: AAOX ASTX BEX BEZ CORD CWVX IRE LITX NBIL SMCX SNDQ SNXX SOXL SOXS.
       Pair lock only for BEX/BEZ, CWVX/CORD, SNXX/SNDQ, SOXL/SOXS.
    7. No new alerts or buys after NO_NEW_AFTER_ET (15:58 ET = 12:58 PM Arizona) -- now enforced.
    8. The status board is unchanged. Sells unchanged (-2% stop, 2.5% trail, flat at 15:59 ET).
    9. After a buy it prints BOUGHT ... and says plainly whether a real order was sent or whether
       DRY_RUN is ON (test mode, no order).

============================================================
  ENGINE B -- DOUBLE CYBORG  (built 2026-08-30, updated through
  2026-09-21 evening -- signal rule now matches Engine A's final
  window-based bend rule; this is its first live test)
  Neither buy NOR sell happens automatically. Every signal
  asks for your approval right here in this terminal (type
  Y and press Enter to act, or just press Enter to skip/hold).
============================================================

CHANGED 2026-10-21 (Gary's decision) -- THIS FILE: rsi_engineB_crossup2.py
    Built from rsi_engineB_crossup.py (68e45c0d). ONE change: the STEEP CROSS-UP angle is now measured on
    the PRICE line (best fit through the last 4 closes, % per minute -> degrees), not on the red line,
    and CROSS_MIN_ANGLE is 30 (was 25 on the red line). Reason: the red line lags price, so it read only
    22 deg on a vertical jump at the open (LITZ 2026-07-28) and missed it. Price angle catches it.
    Everything else the same as rsi_engineB_crossup.py (two alerts, ALREADY HOLDING note, 45 s timer).
    NOTE: the lines in this engine still start at 9:30 ET (no pre-market bars). The history test that
    showed an edge for the price-angle cross-up used pre-market lines in the first 30 minutes; with
    lines starting at 9:30 and all day, the cross-up tested about break-even. Treat it as an alert.

CHANGED 2026-10-20 (Gary's decision) -- FILE: rsi_engineB_crossup.py
    Built from rsi_engineB_matchA_angle10.py (ff281b9f). Changes:
    1. NEW SECOND ALERT, "STEEP CROSS-UP": the red line (HMA7) crosses UP through the black line
       (EMA20) on a finished bar, the red line's angle (best fit over the last 4 points, percent per
       minute -> degrees) is at least CROSS_MIN_ANGLE (25), and the black line's 10-minute angle is
       above -15 (same gate as the turn-up alert). Runs all day, same hours as the turn-up alert.
       The existing alert is now labelled "RED TURN-UP". Its rule is unchanged.
    2. If you already HOLD the stock when a steep cross-up fires, nothing is asked and no money moves:
       one line prints "ALREADY HOLDING -- STEEP CROSS-UP ...".
    3. If you skipped (or let expire) the RED TURN-UP on that stock earlier today, the steep cross-up
       banner says so, and asks as a fresh question.
    4. Question timer POPUP_TIMEOUT_SECONDS 180 -> 45 (the next signal on the same stock cannot
       come sooner than the next 1-minute bar, so a question never overlaps with a second one).
    Everything else identical: 4 slots, $500, pair lock, -2% stop, 2.5% trail, 22 tickers.

CHANGED 2026-10-18 (Gary's decision) -- FILE: rsi_engineB_matchA_angle10.py
    ONE change from rsi_engineB_matchA.py: ANGLE_LOOKBACK 5 -> 10. The black-line angle gate (angle must be
    above -15 degrees) is now measured over the last 10 minutes (best-fit line through 11 points)
    instead of the last 5 minutes (6 points). Reason: the 5-minute reading let buys through while the
    black line had been falling hard for a long time (NBIL 2026-06-09, NBIZ 2026-08-03).
    Everything else identical to rsi_engineB_matchA.py: 4 slots, $500, 1.00% rise over 3 bars, red below black,
    stale-bar guard, pair lock, -2% stop, 2.5% trail, 22 tickers.

CHANGED 2026-10-15 (Gary's decision) -- THIS FILE: rsi_engineB_matchA.py
  ENGINE B now uses the SAME signal rules, tickers, limit and size as Engine A
  (rsi_engineA_4slots_belowblack.py). The only difference: BUYS ASK YOUR APPROVAL
  here (type Y), where Engine A buys on its own. Sells: stop, trailing stop and
  end-of-day are automatic, same as A.
    - Signal: black angle above -15, red up and gaining >= 1.00% over 3 bars,
      red BELOW black, stale-bar guard, 12-bar warmup. (RSI-dip requirement REMOVED.)
    - MAX_SLOTS 4 (held + waiting-for-answer), $500 per trade, same-pair lock ON.
    - 11 pairs (22 tickers), same list as Engine A.
    - Difference kept on purpose: ONE PROMPT PER SIGNAL RUN. If you say no, it won't
      ask again for that ticker until the signal has gone away and come back.
      Engine A has no such rule (it buys, so there is nothing to re-ask).
  NOTE: older comments below (RSI dip, 50 slots) are out of date.

CHANGED 2026-10-08 (Gary's decision) -- "B2" EARLY HEADS-UP VERSION of Engine B.
  Goal: the alert should come EARLIER in a bounce, so you can look at the
  chart before the move is mostly over. Still asks for your approval on every
  buy and every sell. Differences from the 9/21 Engine B:
   1. Black-line gate loosened: angle must be above -15 (was above 0).
   2. Red-line bend lowered: MIN_BEND_PCT 1.00% over 3 bars (was 1.25%).
   3. RSI-DIP REQUIREMENT: RSI(7) must have been at or below RSI_DIP_LEVEL (30)
      at some point in the last RSI_DIP_LOOKBACK_BARS (15) minutes, so the
      looser bend only applies to recoveries from a dip.
   4. ONE ALERT PER RUN: once an alert fires for a ticker it will not ask again
      until the conditions have gone false and then true again (the old file
      re-asked every minute).
   5. STALE-BAR GUARD: ignores bars not from today or older than 4 minutes
      (Engine B fired a false signal on 10/7 from yesterday's closing bars).
   6. Own log file: rsi_mod2_B2_belowblack_log.csv.
   7. BELOW-BLACK RULE (added 2026-10-13, Gary's decision): no alert while the red
      line is at or above the black line. Alerts only fire with red BELOW black.
  History test (35 tickers, 82 days): ~4 min earlier than the 9/21 rule, similar
  hit rates, about the same number of alerts. Best-case peak measure, NOT a
  realistic trade simulation.

ONE SELF-CONTAINED FILE. This does NOT import any other script --
everything it needs (TradeStation connection, indicators, signal
detection, the ticker list) lives right here, so there is nothing
else to keep in sync and nothing else that needs to be "connected."

SIGNAL (2-part rule, UPDATED 2026-09-21 evening -- Gary's decision,
    after testing Engine A's rule against real 9/18 data one ticker at
    a time):
    Black line (EMA20)'s CURRENT angle is above SHALLOWED (now 0.0
    degrees -- flat or rising, not declining at all right now) AND red
    line (HMA7) is still rising right now AND has moved up at least
    MIN_BEND_PCT (1.25%) net over the last BEND_LOOKBACK_BARS (3)
    minutes -- a WINDOW to reach the target, not a requirement that
    every single bar in it be individually rising. Both conditions
    checked on the SAME bar. This replaces the earlier -15-degree/
    ATR-scaled-bend version tested live mid-session on 2026-09-21 --
    the ATR mechanism could not be validated offline (only closing
    prices were available, not real intraday High/Low) and was
    abandoned same-day in favor of directly testing against real
    historical data. See SHALLOWED and MIN_BEND_PCT below for the
    numbers and reasoning behind each choice. RSI was dropped earlier
    (2026-08-31); extensive testing found essentially zero relationship
    (correlation 0.064) between how "oversold" RSI got before a cross
    and how well the trade performed afterward.

WHY DOUBLE CYBORG: since every candidate gets a human look before any
    money moves, a false-positive signal here only costs you a glance
    and a "no", not a real trade. That's a different risk profile from
    Engine A (which buys the instant it fires, with no human check
    first) -- more candidates of comparable quality is a genuine
    advantage here in a way it wouldn't be for Engine A.

ENTRY: asks for your approval the instant the signal fires. Size is
    $500 notional per trade (RAISED from a forced 1 share, 2026-09-21
    night, Gary's decision -- one share wasn't enough real money on the
    line to actually judge performance by), same per-trade size as
    Engine A.

EXIT: RESTORED 2026-09-21 (Gary's decision), after finding that the
    2026-09-03 removal (see sell_monitor_worker) left real positions
    exposed overnight with zero automatic protection. All three of the
    following now fire AUTOMATICALLY, with NO approval prompt -- this is
    the one exception to Double Cyborg's "everything asks first" rule,
    because these are safety nets, not trading decisions, and waiting on
    a human answer here would defeat the purpose:
      - -2% hard stop from entry (STOP_PCT)
      - 2.5% trailing-stop pullback from the peak once in profit (TRAIL_PCT)
      - end-of-day flatten at EOD_FLATTEN_ET (15:59 ET) -- sells everything
        still open, once, near the close
    Buying still always asks for your approval first, right here in the
    terminal -- only these three safety exits bypass that.

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
import re
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
ANGLE_LOOKBACK = 10   # CHANGED 2026-10-18 (Gary): black-line angle measured over 10 minutes (11 points), was 5.
SHALLOWED      = -15.0  # B2 2026-10-08: loosened from 0.0 (see header). Older note: TIGHTENED (2026-09-21 evening, Gary's decision, from
                       # -15.0): testing today's rule against real 9/18 data
                       # found the -15-degree cutoff almost never actually
                       # blocked anything -- the black line (a smoothed
                       # 20-period average) rarely swings past about -10
                       # degrees even during a real intraday decline, so the
                       # filter wasn't doing meaningful work. Tightening all
                       # the way to 0.0 (black line must be flat or rising,
                       # not declining at all right now) tested clearly
                       # better across all 20 tickers: win rate rose from
                       # 32% to 41% and total simulated result improved from
                       # +19.44% to +30.08%. Matches the update made to
                       # Engine A the same evening.
ATR_LEN        = 14   # ADDED (2026-09-21, Gary's decision, live mid-session,
                       # matching Engine A), later SUPERSEDED same evening --
                       # see BEND_LOOKBACK_BARS/MIN_BEND_PCT below. Kept here,
                       # unused, in case real intraday High/Low data supports
                       # a proper ATR-scaled backtest later; the downloaded
                       # dataset used for tonight's testing only has closing
                       # prices, not true OHLC bars.
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
POPUP_TIMEOUT_SECONDS = 45    # CHANGED 2026-10-20 (Gary): 45 s. (2026-09-03: 180 s; was 600)
CROSS_MIN_ANGLE = 30.0        # 2026-10-21: steep cross-up alert needs PRICE angle >= this (degrees)
REDLINE_ANGLE_BARS = 3        # 2026-10-25: red-line rise is measured over this many minutes
REDLINE_MIN_ANGLE = 13.0      # degrees; 13 deg over 3 min is about +0.7% rise
STRONG_ANGLE = 25.0           # strength words, same angle
MEDIUM_ANGLE = 18.0
REDLINE_BLACK_GATE = -35.0    # black-line 10-minute angle must be above this for a RED-LINE BUY
ALERT_LIST_MAX_MINUTES = 10   # an alert leaves the BUY ALERT list after this long
ALERT_MIN_MINUTES = 3         # an alert stays up at least this long even if its signal ends sooner (time to look)
MAX_NEW_PER_MINUTE = 3        # at most this many NEW alerts shown per minute (strongest first)
REALERT_COOLDOWN_BARS = 10    # same stock cannot get a new red-line alert sooner than this many bars
MIN_PM_BARS = 10              # fewer pre-market bars than this today -> that stock uses lines from 9:30
SESSION_TEMPLATE = "USEQPreAndPost"
ALERT_LOG_CSV = os.getenv("ALERT_LOG_CSV") or "engineB_alerts_log.csv"
CROSS_RED_POINTS = 4          # points used for the best-fit angle (price closes) -- matches the backtest
                              # (10 min), now 3 min -- if you don't answer a
                              # buy prompt in this long,
                               # it auto-expires and keeps holding

TRADE_DOLLARS = 500  # CHANGED 2026-10-15 (Gary): $500 per trade on both engines. (Was 200.) Older note follows: CHANGED 2026-10-15 (Gary): $200 per trade, matches Engine A. Was 500; RAISED (2026-09-21 night, Gary's decision): was $1
                     # (forcing 1 share/trade) for the very first live test
                     # of this rewritten signal (0-degree angle filter,
                     # 1.25% bend over 3 min). Gary's own reasoning: "I
                     # can't really tell how well I do on trading unless
                     # there's real money, and one share is enough to
                     # tell" -- meaning 1 share doesn't put enough on the
                     # line to actually judge performance by. $500 matches
                     # Engine A's per-trade size, so both engines now risk
                     # the same amount per trade.
                     # Shares are computed at buy time: int(TRADE_DOLLARS /
                     # current price), minimum 1 share.
                     # History: $1 (2026-09-14, forced 1-share test), $1000
                     # (2026-09-10), SHARES_PER_TRADE=1 (2026-08-26).


def shares_for_dollars(price: float) -> int:
    """How many whole shares $TRADE_DOLLARS buys at this price, min 1."""
    if price is None or price <= 0:
        return 1
    return max(1, int(TRADE_DOLLARS / price))

MAX_SLOTS = 4    # CHANGED 2026-10-15 (Gary): matches Engine A. Never more than 4 positions open
                 # (positions held PLUS buys waiting for your answer count toward the 4).
STALE_BAR_MINUTES = 4   # 2026-10-15: newest bar must be from today and no older than this many minutes

RSI_LEN = 7                 # B2: RSI period (Wilder), computed from today's bars only
RSI_DIP_LEVEL = 30.0        # B2: RSI must have been at or below this recently
RSI_DIP_LOOKBACK_BARS = 15  # B2: ...within this many minutes (including now)

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
    ("AAOX",), ("ASTX",), ("BEX", "BEZ"), ("CWVX", "CORD"), ("IRE",),
    ("LITX",), ("NBIL",), ("SMCX",), ("SNXX", "SNDQ"), ("SOXL", "SOXS"),
]  # 2026-10-25 (Gary): 14 stocks. Pair lock only where a stock has an inverse partner here.
SYMBOLS = sorted(s for pr in PAIRS for s in pr)   # alphabetical: the status board reads in this order
PARTNER = {}
for _grp in PAIRS:
    if len(_grp) == 2:
        PARTNER[_grp[0]] = _grp[1]
        PARTNER[_grp[1]] = _grp[0]

_dupes = [s for s in set(SYMBOLS) if SYMBOLS.count(s) > 1]
if _dupes:
    raise SystemExit(f"FATAL: duplicate symbols in PAIRS: {sorted(_dupes)}")

BAR_MINUTES = 1
BAR_LOOKBACK = 1000   # 2026-10-25: more bars back, because pre-market bars are included
MARKET_OPEN_ET = "09:30"
MARKET_CLOSE_ET = "16:00"
NO_NEW_AFTER_ET = "15:58"
EOD_FLATTEN_ET = "15:59"
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or 20)

# Separate log file from the plain system's, so they never collide.
LOG_CSV = os.getenv("MOD2_CYBORG_LOG") or "rsi_engineB_redline_log.csv"
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
    through the whole ANGLE_LOOKBACK-minute window (now 10 minutes, all 11 points), not
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
        self.use_template = True    # 2026-10-25: ask for pre-market bars

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
        if self.use_template:
            params["sessiontemplate"] = SESSION_TEMPLATE
        r = self._session.get(self.base + f"/marketdata/barcharts/{symbol}",
                              headers=self._headers(), params=params, timeout=20)
        if r.status_code == 400 and self.use_template:
            log(f"WARN TradeStation rejected sessiontemplate={SESSION_TEMPLATE} ({r.text[:120]!r}). "
                f"Retrying without it: that gives REGULAR-HOURS bars only, so every stock uses lines from 9:30.")
            self.use_template = False
            return self.get_bars(symbol, limit)
        r.raise_for_status()
        data = r.json()
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
    angle_now: float
    angle_was: float
    bar_index: int
    atr_pct_now: float
    rsi_min_recent: float = float("nan")   # B2: lowest RSI(7) over the last 15 min
    black_now: float = float("nan")        # B2 below-black: black line (EMA20) value now
    black_prev: float = float("nan")       # 2026-10-20: black line value one bar ago
    red_angle: float = float("nan")        # 2026-10-20: red line angle, best fit over last CROSS_RED_POINTS points


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
    # B2: RSI(7), Wilder smoothing, from today's bars only
    _d = close.diff()
    _up = _d.clip(lower=0).ewm(alpha=1.0 / RSI_LEN, adjust=False).mean()
    _dn = (-_d.clip(upper=0)).ewm(alpha=1.0 / RSI_LEN, adjust=False).mean()
    rsi = 100 - 100 / (1 + _up / _dn.replace(0, np.nan))
    rsi_min = rsi.rolling(RSI_DIP_LOOKBACK_BARS + 1, min_periods=1).min()

    def _red_angle(series, npts):
        try:
            y = series.iloc[-npts:].to_numpy(dtype=float)
            if len(y) < npts or np.isnan(y).any() or y.mean() == 0:
                return float("nan")
            slope = np.polyfit(np.arange(npts, dtype=float), y, 1)[0]
            return float(np.degrees(np.arctan(slope / y.mean() * 100.0)))
        except Exception:
            return float("nan")

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
        angle_now=_safe(angle, -1),
        angle_was=float("nan"),
        bar_index=bar_index,
        atr_pct_now=_safe(atr_pct, -1),
        rsi_min_recent=_safe(rsi_min, -1),
        black_now=_safe(black, -1),
        black_prev=_safe(black, -2),
        red_angle=_red_angle(close, CROSS_RED_POINTS),   # 2026-10-21: field name kept, now holds the PRICE angle
    )


def black_gate_open(fr: Frame) -> bool:
    """TIGHTENED (2026-09-21 evening, Gary's decision): this checks the
    black line's CURRENT angle -- if it's flat or rising (angle above
    SHALLOWED, now 0.0 degrees), the trade is allowed through. Previously
    allowed some real decline through (SHALLOWED=-15), but testing found
    that threshold was almost never actually reached even during a real
    intraday slide -- the black line is a smoothed 20-period average, so
    its 5-minute slope rarely swings past about -10 degrees regardless.
    Requiring flat-or-rising (0.0) instead of "not too steep down"
    tested clearly better on real data. Matches Engine A."""
    if math.isnan(fr.angle_now):
        return False
    return fr.angle_now > SHALLOWED


BEND_LOOKBACK_BARS = 3  # window (in minutes/bars) the red line has to reach
                        # MIN_BEND_PCT within. Matches Engine A.

MIN_BEND_PCT = 1.00  # B2 2026-10-08: lowered from 1.25. Older note: REPLACED BEND_ATR_MULT/ATR-scaled bend (2026-09-21
                     # evening, Gary's decision): the ATR mechanism could
                     # not be validated offline (the downloaded dataset only
                     # has closing prices, not real intraday High/Low, so no
                     # honest historical ATR test was possible) and was
                     # abandoned the same day in favor of testing directly
                     # against real 9/18 minute data with a flat bend
                     # percentage instead. Tested 1.00% through 3.00% with
                     # the black line held flat-or-rising: 1.00% produced a
                     # lot of volume (65 trades/day across 20 tickers, ~10/
                     # hour -- too many to review one at a time as Double
                     # Cyborg). 1.25% cuts that down to 38 trades/day
                     # (~5.8/hour) while keeping a similar per-trade result
                     # to 1.00%, and Gary chose it specifically because this
                     # is Engine B's first live test and he wants a
                     # manageable number of prompts to review, not
                     # necessarily the single best-testing number (1.50%
                     # tested slightly better per-trade but let through
                     # even fewer candidates). Since Gary reviews and can
                     # decline any signal himself, a somewhat looser
                     # threshold here is fine -- his own judgment is the
                     # real second filter Engine A doesn't have. Position
                     # size is fixed at 1 share (see TRADE_DOLLARS) for this
                     # first run, independent of this threshold.
                     #
                     # ATR_LEN/atr_wilder_pct() left in the file, unused,
                     # for a future backtest once real intraday High/Low
                     # data is available.


def red_rising(fr: Frame) -> bool:
    """WINDOW rule (2026-09-21 evening, Gary's decision, matching Engine
    A): not "every bar in the window must be individually rising" --
    just needs to still be rising right now, and to have covered at
    least MIN_BEND_PCT net over the last BEND_LOOKBACK_BARS(3) minutes.
    Gary's own words (from tuning Engine A the same day): "give it a
    window of three minutes to reach it... even if it only is one or two
    or three greens.\""""
    if not (fr.red_now > fr.red_prev):
        return False
    if math.isnan(fr.red_prev3):
        return False
    bend_pct = (fr.red_now - fr.red_prev3) / fr.close * 100
    return bend_pct >= MIN_BEND_PCT


def prepare_today(df, now_et):
    """2026-10-25: keep TODAY's bars only. If the stock has at least MIN_PM_BARS pre-market bars today
    (04:00-09:30 ET) the lines use them (starting 04:00 ET); otherwise lines start at 09:30 ET.
    Returns (frame, used_premarket, premarket_bar_count)."""
    et = df.index.tz_convert(ET)
    mins = np.asarray(et.hour) * 60 + np.asarray(et.minute)
    is_today = np.asarray(et.date == now_et.date())
    pm_n = int((is_today & (mins >= 240) & (mins < 570)).sum())
    if pm_n >= MIN_PM_BARS:
        return df[is_today & (mins >= 240) & (mins <= 960)], True, pm_n
    return df[is_today & (mins >= 570) & (mins <= 960)], False, pm_n


def redline_angle(fr):
    """Red-line angle over the last REDLINE_ANGLE_BARS minutes (degrees): rise as % of price, per minute."""
    if math.isnan(fr.red_now) or math.isnan(fr.red_prev3) or not fr.close or fr.close <= 0:
        return float("nan")
    rise_pct = (fr.red_now - fr.red_prev3) / fr.close * 100.0
    return float(math.degrees(math.atan(rise_pct / REDLINE_ANGLE_BARS)))


def redline_buy(fr):
    """Returns the red-line angle if the RED-LINE BUY rule holds on this bar, else None."""
    a = redline_angle(fr)
    if math.isnan(a) or math.isnan(fr.black_now) or math.isnan(fr.angle_now):
        return None
    if not (fr.red_now > fr.red_prev):
        return None
    if not (fr.red_now < fr.black_now):
        return None
    if fr.angle_now <= REDLINE_BLACK_GATE:
        return None
    if a < REDLINE_MIN_ANGLE:
        return None
    return a


def strength_word(a):
    return "STRONG" if a >= STRONG_ANGLE else "MEDIUM" if a >= MEDIUM_ANGLE else "WEAK"


ALERT_LOG_COLUMNS = ["date", "time_az", "ticker", "kind", "strength", "redline_angle",
                     "price_angle", "black_angle", "price", "shown", "note"]


def append_alert_row(row):
    new = not os.path.exists(ALERT_LOG_CSV)
    with open(ALERT_LOG_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALERT_LOG_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in ALERT_LOG_COLUMNS})


def premarket_check(api):
    now_et = et_now()
    if now_et.weekday() >= 5:
        return
    log(f"PREMARKET CHECK: asking TradeStation for bars with sessiontemplate={SESSION_TEMPLATE}")
    ok = 0
    for sym in SYMBOLS:
        try:
            raw = api.get_bars(sym)
            if raw is None:
                log(f"PREMARKET CHECK {sym}: no bars returned")
                continue
            _, _, pm = prepare_today(raw, now_et)
            log(f"PREMARKET CHECK {sym}: {pm} pre-market bars today"
                + ("" if pm >= MIN_PM_BARS else f"  <-- fewer than {MIN_PM_BARS}: this stock will use lines from 9:30 instead"))
            ok += pm >= MIN_PM_BARS
        except Exception as e:
            log(f"PREMARKET CHECK {sym}: ERROR {e}")
    log(f"PREMARKET CHECK done: {ok} of {len(SYMBOLS)} stocks have at least {MIN_PM_BARS} pre-market bars "
        f"(before about 4:00 AM ET there are none yet)")


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
skipped_turnup = {}   # symbol -> date string, set when you skip/let expire a RED TURN-UP question
pending_buy_meta = {} # symbol -> indicator snapshot at signal time, held until you approve/skip the buy


live_alerts = {}      # symbol -> dict(num, kind, text, price, t0)  -- the numbered BUY ALERT list
alert_counter = [0]   # numbers only go up during a run, never reused
live_lock = threading.Lock()


def az_clock():
    return datetime.now(AZ).strftime("%I:%M %p").lstrip("0")


def show_alert_list():
    with live_lock:
        items = sorted(live_alerts.items(), key=lambda kv: kv[1]["num"])
    if not items:
        return
    lines = ["", "", f"BUY ALERT   {az_clock()}"]
    for sym, a in items:
        lines.append(f"    {a['num']}   {sym}: {a['text']}")
    lines.append("To buy: type the number, press Enter.")
    print("\n".join(lines), flush=True)


def handle_buy_number(api, n):
    with live_lock:
        found = next(((sym, a) for sym, a in live_alerts.items() if a["num"] == n), None)
    if found is None:
        print(f"{n}: not on the BUY ALERT list any more.", flush=True)
        return
    sym, a = found
    if past(et_now(), NO_NEW_AFTER_ET):
        print(f"No new buys after 12:58 PM Arizona -- not buying {sym}.", flush=True)
        return
    if sym in open_positions or sym in pending_buy_meta:
        print(f"{sym}: already holding.", flush=True)
        return
    if slots_in_use() >= MAX_SLOTS:
        print(f"Slots full ({MAX_SLOTS} positions) -- not buying {sym}.", flush=True)
        return
    if pair_leg_open(sym):
        print(f"{sym}: its partner {PARTNER.get(sym)} is already open -- not buying {sym}.", flush=True)
        return
    try:
        price = float(api.get_latest_trade(sym).price)
        if price <= 0:
            price = a["price"]
    except Exception:
        price = a["price"]
    fr = latest_frame.get(sym)
    pending_buy_meta[sym] = {
        "entry_angle_now": fr.angle_now if fr else "",
        "entry_angle_was": "",
        "label": a["text"],
    }
    with live_lock:
        live_alerts.pop(sym, None)
    log(f"YOU TYPED {n}: buying {sym} ({a['text']}) at ~{price:.4f}")
    decision_queue.put(("APPROVE_BUY", sym, price, None))


def buy_input_worker(api):
    """Reads what you type. A number (or several, separated by spaces) buys that stock from the list."""
    while _RUNNING:
        try:
            line = input()
        except EOFError:
            return
        except Exception:
            time.sleep(1)
            continue
        for tok in re.split(r"[,\s]+", line.strip()):
            if not tok:
                continue
            if not tok.isdigit():
                print(f"'{tok}' is not a number from the BUY ALERT list. Nothing bought.", flush=True)
                continue
            try:
                handle_buy_number(api, int(tok))
            except Exception as e:
                log(f"ERROR handling buy number {tok}: {e}")


def steep_cross_up(fr):
    """2026-10-20: red line crosses UP through black on this bar, red-line angle >= CROSS_MIN_ANGLE,
    black 10-minute angle above SHALLOWED. Same rule that was backtested (cross this bar, PRICE angle
    over 4 points, black gate)."""
    vals = (fr.red_now, fr.red_prev, fr.black_now, fr.black_prev, fr.red_angle)
    if any(math.isnan(v) for v in vals):
        return False
    return (fr.red_prev <= fr.black_prev and fr.red_now > fr.black_now
            and fr.red_angle >= CROSS_MIN_ANGLE and black_gate_open(fr))


def slots_in_use():
    # 2026-10-15: positions held PLUS buys waiting for your answer
    return len(open_positions) + len(pending_buy_meta)


def pair_leg_open(sym):
    # RE-ENABLED 2026-10-15 (Gary): won't alert on one side of a pair while the
    # other side is held or waiting for your answer. Matches Engine A.
    partner = PARTNER.get(sym)
    return bool(partner) and (partner in open_positions or partner in pending_buy_meta)


def signal_worker(api):
    """Every poll: rebuild the lines for each stock, then
       - RED-LINE BUY: red-line angle over 3 min >= 13, red still rising, red below black, black angle > -35
       - CROSS-UP BUY: red crosses up through black, price angle >= 30, black angle > -15 (unchanged)
    New alerts go on the numbered BUY ALERT list (at most 3 new per minute, strongest first)."""
    last_cross_bar = {}
    processed_bar = {}
    alert_armed = {s: True for s in SYMBOLS}
    last_alert_idx = {}
    shown_by_minute = {}
    stale_logged = set()
    mode_logged = set()

    log(f"Signal worker started. RED-LINE BUY: red-line angle over {REDLINE_ANGLE_BARS} min >= {REDLINE_MIN_ANGLE:.0f} deg "
        f"(STRONG>={STRONG_ANGLE:.0f}, MEDIUM>={MEDIUM_ANGLE:.0f}), red still rising, red below black, black angle > {REDLINE_BLACK_GATE:.0f}. "
        f"CROSS-UP BUY: price angle >= {CROSS_MIN_ANGLE:.0f}, black angle > {SHALLOWED:.0f}. "
        f"Max {MAX_NEW_PER_MINUTE} new alerts per minute; list keeps an alert at least {ALERT_MIN_MINUTES} and at most {ALERT_LIST_MAX_MINUTES} min. "
        f"MAX_SLOTS={MAX_SLOTS}, pair-lock ON, no new alerts after {NO_NEW_AFTER_ET} ET.")

    while _RUNNING:
        now_et = et_now()
        if not in_session(now_et):
            time.sleep(POLL_SECONDS)
            continue
        new_ok = not past(now_et, NO_NEW_AFTER_ET)
        changed = False
        candidates = []

        for sym in SYMBOLS:
            held = sym in open_positions
            try:
                raw = api.get_bars(sym)
                if raw is None:
                    continue
                df, used_pm, pm_n = prepare_today(raw, now_et)
                if df is None or len(df) < WARMUP_BARS + 1:
                    continue
                fr = build_frame(df)
                latest_frame[sym] = fr   # cache for the live status board
            except Exception as e:
                log(f"WARN {sym}: {e}")
                continue

            if sym not in mode_logged:
                mode_logged.add(sym)
                log(f"LINES {sym}: " + (f"built from pre-market bars ({pm_n} pre-market bars today)" if used_pm
                    else f"built from 9:30 ET (only {pm_n} pre-market bars today, need {MIN_PM_BARS})"))

            try:
                last_bar_et = pd.Timestamp(fr.ts).tz_convert(ET)
                bar_age_min = (now_et - last_bar_et).total_seconds() / 60.0
            except Exception:
                continue
            if last_bar_et.date() != now_et.date() or bar_age_min > STALE_BAR_MINUTES:
                if sym not in stale_logged:
                    stale_logged.add(sym)
                    log(f"STALE-SKIP {sym}: newest bar is {last_bar_et.strftime('%Y-%m-%d %H:%M')} ET "
                        f"({bar_age_min:.1f} min old) -- waiting for fresh bars from today")
                continue
            if fr.bar_index < WARMUP_BARS:
                continue

            a3 = redline_buy(fr)
            bar_key = str(fr.ts)
            is_cross = steep_cross_up(fr) and last_cross_bar.get(sym) != bar_key
            entry = live_alerts.get(sym)

            if held:
                if entry is not None:
                    with live_lock:
                        live_alerts.pop(sym, None)
                    changed = True
                if is_cross:
                    last_cross_bar[sym] = bar_key
                    print(f"\n{sym}: already holding\n", flush=True)
                    log(f"ALREADY-HOLDING {sym} cross-up @ {fr.close:.4f}")
                continue

            if is_cross:
                last_cross_bar[sym] = bar_key
                if entry is not None:
                    entry["kind"] = "CROSS"
                    entry["text"] = "CROSS-UP BUY"
                    entry["t0"] = time.time()
                    changed = True
                    log(f"ALERT-UPGRADE {sym}: red-line alert is now a CROSS-UP BUY @ {fr.close:.4f}")
                    append_alert_row({"date": str(now_et.date()), "time_az": az_clock(), "ticker": sym,
                                      "kind": "CROSS-UP", "price_angle": f"{fr.red_angle:.0f}",
                                      "black_angle": f"{fr.angle_now:+.1f}", "price": f"{fr.close:.4f}",
                                      "shown": "Y", "note": "upgrade of a listed red-line alert"})
                elif new_ok:
                    candidates.append({"sym": sym, "kind": "CROSS", "score": 100.0 + fr.red_angle,
                                       "fr": fr, "a3": a3, "minute": bar_key, "word": ""})
            elif entry is not None:
                age_min = (time.time() - entry["t0"]) / 60.0
                if entry["kind"] == "RED":
                    ended = a3 is None
                else:
                    ended = not (fr.red_now > fr.black_now and fr.red_now > fr.red_prev)
                if (ended and age_min >= ALERT_MIN_MINUTES) or age_min > ALERT_LIST_MAX_MINUTES:
                    with live_lock:
                        live_alerts.pop(sym, None)
                    changed = True
                    log(f"ALERT-ENDED {sym} ({'signal ended' if ended else 'listed over %d min' % ALERT_LIST_MAX_MINUTES})")
            else:
                if a3 is None:
                    alert_armed[sym] = True
                elif (alert_armed[sym] and new_ok and processed_bar.get(sym) != bar_key
                      and fr.bar_index - last_alert_idx.get(sym, -999) >= REALERT_COOLDOWN_BARS):
                    processed_bar[sym] = bar_key
                    alert_armed[sym] = False
                    candidates.append({"sym": sym, "kind": "RED", "score": a3, "fr": fr, "a3": a3,
                                       "minute": bar_key, "word": strength_word(a3)})

        # ---- decide which new alerts to show (strongest first, at most MAX_NEW_PER_MINUTE per minute)
        by_min = {}
        for c in candidates:
            by_min.setdefault(c["minute"], []).append(c)
        for minute, lst in by_min.items():
            lst.sort(key=lambda c: -c["score"])
            room = MAX_NEW_PER_MINUTE - shown_by_minute.get(minute, 0)
            shown_now = 0
            for c in lst:
                sym, fr = c["sym"], c["fr"]
                base = {"date": str(now_et.date()), "time_az": az_clock(), "ticker": sym,
                        "kind": "CROSS-UP" if c["kind"] == "CROSS" else "RED-LINE",
                        "strength": c["word"],
                        "redline_angle": "" if c["a3"] is None else f"{c['a3']:.1f}",
                        "price_angle": "" if math.isnan(fr.red_angle) else f"{fr.red_angle:.0f}",
                        "black_angle": f"{fr.angle_now:+.1f}", "price": f"{fr.close:.4f}"}
                if slots_in_use() >= MAX_SLOTS:
                    log(f"SLOT-SKIP {sym} (slots full {MAX_SLOTS})")
                    append_alert_row({**base, "shown": "N", "note": "slots full"})
                    continue
                if pair_leg_open(sym):
                    log(f"PAIR-SKIP {sym} (partner {PARTNER[sym]} already open)")
                    append_alert_row({**base, "shown": "N", "note": "pair lock"})
                    continue
                if shown_now >= room:
                    log(f"LOGGED-ONLY {sym} {base['kind']} {c['word']} (more than {MAX_NEW_PER_MINUTE} new alerts this minute)")
                    append_alert_row({**base, "shown": "N", "note": "cap per minute"})
                    continue
                alert_counter[0] += 1
                text = f"RED-LINE BUY {c['word']}" if c["kind"] == "RED" else "CROSS-UP BUY"
                with live_lock:
                    live_alerts[sym] = {"num": alert_counter[0], "kind": c["kind"], "text": text,
                                        "price": fr.close, "t0": time.time()}
                shown_now += 1
                last_alert_idx[sym] = fr.bar_index
                changed = True
                log(f"ALERT #{alert_counter[0]} {sym} {text} @ {fr.close:.4f} redline-angle={base['redline_angle']} "
                    f"price-angle={base['price_angle']} black-angle={fr.angle_now:+.1f}")
                append_alert_row({**base, "shown": "Y", "note": ""})
            shown_by_minute[minute] = shown_by_minute.get(minute, 0) + shown_now

        if changed:
            show_alert_list()
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


RECONCILE_SECONDS = 10   # how often to check the real account for manual sells


def reconcile_positions_worker(api):
    """ADDED (2026-09-03, Gary's decision): now that all selling is
    manual, this check becomes essential -- periodically checks the
    REAL broker account directly, and clears out anything this script
    thinks it's still holding that's actually already gone (sold
    manually by Gary, directly in TradeStation). Without this, a
    manually-sold ticker would be permanently blocked from ever
    trading again, since the script would keep believing it's still
    held. Matches the same check already running in Engine A."""
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
    fire automatically, with NO approval prompt -- the one exception to
    this engine's normal "everything asks first" rule, because these are
    safety nets, not trading decisions. Buying (and the BUY side of
    decision_worker) is untouched and still requires your typed approval.
    This reverses the 2026-09-03 change that removed all automatic
    selling; that change left positions carried over indefinitely,
    including overnight, with zero automatic protection, which is what
    prompted restoring this (matches the same fix in Engine A)."""
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
            qty = shares_for_dollars(price)
            try:
                result = api.market_buy(symbol, qty)
                log(f"Buy order result for {symbol}: {result} ({qty} shares @ ${price:.2f})")
                if api.dry_run:
                    print(f"BOUGHT {symbol}: {qty} shares @ ~${price:.2f}  *** TEST MODE (DRY_RUN is ON): NO REAL ORDER WAS SENT ***", flush=True)
                else:
                    print(f"BOUGHT {symbol}: {qty} shares @ ~${price:.2f}  -- real order sent", flush=True)
                meta = pending_buy_meta.pop(symbol, {})
                open_positions[symbol] = {
                    "entry": price, "peak": price, "qty": qty,
                    "opened_ts": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_angle_now": meta.get("entry_angle_now", ""),
                    "entry_angle_was": meta.get("entry_angle_was", ""),
                }
                log(f"Now tracking open position: {symbol} entry=${price:.2f} -- "
                    f"will alert on sell via this terminal")
            except Exception as e:
                log(f"ERROR buying {symbol}: {e}")
                print(f"BUY FAILED for {symbol}: {e}", flush=True)
                pending_buy_meta.pop(symbol, None)
        elif action == "SKIP_BUY":
            log(f"SKIPPED by you: {symbol} @ ~{price} -- not buying this signal")
            _m = pending_buy_meta.pop(symbol, {})
            if _m.get("label") == "RED TURN-UP":
                skipped_turnup[symbol] = datetime.now(ET).strftime("%Y-%m-%d")
            print(f"SKIPPED -- {symbol}", flush=True)
        elif action == "EXPIRED_BUY":
            log(f"Buy alert EXPIRED (no answer within {POPUP_TIMEOUT_SECONDS}s): {symbol} -- not buying")
            _m = pending_buy_meta.pop(symbol, {})
            if _m.get("label") == "RED TURN-UP":
                skipped_turnup[symbol] = datetime.now(ET).strftime("%Y-%m-%d")
        elif action == "APPROVE_SELL":
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
    log("RSI MOD2 -- ENGINE B REDLINE (you look, then you decide; self-contained file)")
    log("=" * 70)
    if bal:
        log(f"BALANCE  equity=${bal['equity']:,.2f}  cash=${bal['cash']:,.2f}")
        if api.env == "live" and not api.dry_run:
            log("         ^ CHECK THIS ACCOUNT. Ctrl-C now if it is wrong.")
    log(f"REDLINE  angle over {REDLINE_ANGLE_BARS} min >= {REDLINE_MIN_ANGLE:.0f}  STRONG>={STRONG_ANGLE:.0f} MEDIUM>={MEDIUM_ANGLE:.0f}  "
        f"BLACK_GATE>{REDLINE_BLACK_GATE:.0f}  MAX_NEW_PER_MINUTE={MAX_NEW_PER_MINUTE}  LIST_MAX_MIN={ALERT_LIST_MAX_MINUTES}  "
        f"NO_NEW_AFTER={NO_NEW_AFTER_ET} ET  ALERT_LOG={ALERT_LOG_CSV}")
    log(f"MODE     {RSI_MOD2_MODE}  |  SHALLOWED={SHALLOWED:.1f}  "
        f"MIN_BEND_PCT={MIN_BEND_PCT:.2f}% over {BEND_LOOKBACK_BARS} bars  "
        f"STOP_PCT={STOP_PCT:.1f}%  TRAIL_PCT={TRAIL_PCT:.1f}%  "
        f"TRADE_DOLLARS=${TRADE_DOLLARS}  MAX_SLOTS={MAX_SLOTS}  RED_BELOW_BLACK=ON  STALE_BAR_MIN={STALE_BAR_MINUTES}  ANGLE_LOOKBACK={ANGLE_LOOKBACK}  CROSS_MIN_ANGLE={CROSS_MIN_ANGLE:.0f}  POPUP_SECONDS={POPUP_TIMEOUT_SECONDS}")
    log(f"SUPPRESS per-ticker while open + global slots<={MAX_SLOTS} + same-pair lock")
    log(f"UNIVERSE ({len(SYMBOLS)} tickers) {', '.join(SYMBOLS)}")
    log(f"LOG      {LOG_CSV}")
    if api.env == "live" and not api.dry_run:
        log("*** LIVE TRADING ENABLED -- real orders will be sent ***")
    log("=" * 70)
    log("Watching quietly now -- will only print again when a real signal, "
        "buy, sell, or heartbeat happens. This is normal; no news is good news.")

    premarket_check(api)
    print("\nHOW TO BUY: when a BUY ALERT list appears, type the number next to the stock and press Enter.\n"
          "Do nothing to skip. Nothing is ever bought unless you type a number.\n", flush=True)

    threading.Thread(target=buy_input_worker, args=(api,), daemon=True).start()
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
  
