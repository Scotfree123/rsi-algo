
#!/usr/bin/env python3
"""
============================================================
  WINNING SIDE  --  ALERT ONLY  (built 2026-10-20, Gary's system)
  NOTHING IS BOUGHT OR SOLD. This program places NO orders and needs no
  trading account. It watches the 22 tickers, prints an alert when the
  system says BUY, then keeps tracking that "paper" trade and prints an
  EXIT alert when the system says get out. Every alert and every paper
  trade is written to a CSV file so the results can be reviewed.
  Because it never trades, it can run in a second terminal at the same
  time as Engine B.
============================================================

THE SYSTEM (Gary's theory: "line cross, stay on the winning side of the black line")

  BLACK line = EMA20 of the 1-minute closes.  RED line = HMA7.
  Both are built from the first bar of the day, INCLUDING PRE-MARKET bars
  (04:00 ET), so the lines are already formed at the 9:30 ET open.

  BUY ALERT (checked on every finished 1-minute bar):
    1. RED crosses UP through BLACK on this bar (red was at/below black, now above).
    2. PRICE ANGLE >= CROSS_MIN_ANGLE (30 deg): best-fit line through the last 4 closes,
       percent of price per minute, converted to degrees (arctan).
       (Price, not the red line: red lags price and misses a jump at the open.)
    3. BLACK LINE not falling: black angle over the last 10 minutes (11 points) > -15 deg.
    4. At least 12 bars of today's data (pre-market bars count).
    5. Bar is from today and no more than 4 minutes old.
    6. Inside the alert window (default 09:30-10:00 ET = 6:30-7:00 AM Arizona).
       This is where the history test showed an edge; later in the day it was break-even.

  EXIT (first one that happens, checked on every finished bar after the buy):
    a. STOP: close is 2.0% or more below the alert price.
    b. RED TOUCHES BLACK: red closes below black.  (leaving the winning side)
    c. SHARP PEAK: the red line's angle over its last 4 points is -35 deg or steeper.
    d. End of day 15:59 ET.
  A slight dip, or a flat top, that never touches black and never bends down sharply
  is NOT an exit.

  HISTORY (85 days, first 30 min, pre-market lines, no trading costs, exits at bar closes):
    price angle >= 30: ~6 alerts/day, avg +0.34% per trade, win ~38%, held ~12 min
    price angle >= 35: ~4.7 alerts/day, avg +0.48%
    price angle >= 45: ~2.4 alerts/day, avg +0.79%
    Whole day (not just the open): about break-even.  Small sample; costs not included.

IMPORTANT -- PRE-MARKET DATA:
  This needs TradeStation to return pre-market 1-minute bars. The program asks for them
  (sessiontemplate=USEQPreAndPost) and prints a CHECK line at startup saying how many
  pre-market bars it received. If a ticker has fewer than MIN_PM_BARS pre-market bars
  today, it is SKIPPED (logged once) so alerts never come from lines that were not built
  the tested way. Look at the PREMARKET CHECK lines at startup.

RUN:  set -a; source .env; set +a; ./venv/bin/python3 rsi_alert_winningside.py 2>&1 | tee winningside_$(date +%F).log
Needs in .env: TS_CLIENT_ID, TS_CLIENT_SECRET, TS_REFRESH_TOKEN (and TS_ENV). No account id needed.
Start it before 6:30 AM Arizona (9:30 ET); it can start any time after 4:00 AM ET.
"""
import os
import sys
import csv
import time
import math
import signal as _sig
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
TS_BASE = {"sim": "https://sim-api.tradestation.com/v3",
           "live": "https://api.tradestation.com/v3"}

# ------------------------------------------------------------ settings -------
EMA_LEN = 20                 # black
HMA_LEN = 7                  # red
ANGLE_LOOKBACK = 10          # black-line angle: 10 minutes (11 points)
SHALLOWED = -15.0            # black angle must be above this
CROSS_MIN_ANGLE = 30.0       # PRICE angle at the cross must be at least this (degrees)  <-- one number to change
PRICE_ANGLE_POINTS = 4       # closes used for the price angle
SHARP_EXIT_ANGLE = 35.0      # exit when the red line's angle is -35 deg or steeper
RED_ANGLE_POINTS = 4         # points used for the red-line angle
STOP_PCT = 2.0               # paper stop, % below the alert price
WARMUP_BARS = 12
STALE_BAR_MINUTES = 4
MIN_PM_BARS = 10             # fewer pre-market bars than this today -> ticker skipped
ALERT_START_ET = "09:30"
ALERT_END_ET = "10:00"       # no NEW alerts after this (open paper trades are still tracked)
EOD_ET = "15:59"
CLOSE_ET = "16:00"
HI_WINDOW_MIN = 8            # also record the highest price within this many minutes of the alert
BAR_LOOKBACK = 700
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or 20)
SESSION_TEMPLATE = "USEQPreAndPost"
LOG_CSV = os.getenv("WINNINGSIDE_LOG") or "winningside_log.csv"
LOG_COLUMNS = ["date", "ticker", "alert_time_az", "alert_price", "price_angle", "black_angle",
               "exit_time_az", "exit_price", "pnl_pct", "exit_reason", "minutes_held",
               "high_within_8min_pct", "high_before_exit_pct"]

PAIRS = [("NBIL", "NBIZ"), ("IRE", "IREZ"), ("SNXX", "SNDQ"), ("LITX", "LITZ"),
         ("AAOX", "AAOZ"), ("APLX", "APLZ"), ("BEX", "BEZ"), ("SMCX", "SMCZ"),
         ("RKLX", "RKLZ"), ("ASTX", "ASTN"), ("CWVX", "CORD")]
SYMBOLS = [s for pr in PAIRS for s in pr]

_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


_sig.signal(_sig.SIGINT, _stop)
_sig.signal(_sig.SIGTERM, _stop)


def log(msg):
    print(f"{datetime.now(AZ).strftime('%Y-%m-%d %H:%M:%S AZ')} | {msg}", flush=True)


def ensure_csv():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerow(LOG_COLUMNS)


def append_row(row):
    with open(LOG_CSV, "a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in LOG_COLUMNS])


# ------------------------------------------------------------ indicators -----
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hma(s, n):
    return wma(2 * wma(s, 3) - wma(s, n), 3)


def best_fit_angle_series(vals, npts):
    """Angle (degrees) of the best-fit line through each window of npts points:
    slope as % of the window's mean value per bar, then arctan."""
    x = np.arange(npts, dtype=float)
    xc = x - x.mean()
    den = (xc ** 2).sum()
    arr = np.asarray(vals, dtype=float)
    out = np.full(len(arr), np.nan)
    for i in range(npts - 1, len(arr)):
        y = arr[i - npts + 1:i + 1]
        if np.isnan(y).any():
            continue
        m = y.mean()
        if m == 0:
            continue
        slope = (xc * (y - m)).sum() / den
        out[i] = math.degrees(math.atan(slope / m * 100.0))
    return out


def compute_arrays(today_df):
    """All lines/angles for today's bars (causal: value at bar k uses only bars <= k)."""
    close = today_df["close"].astype(float)
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
    return {
        "close": close.to_numpy(), "high": today_df["high"].astype(float).to_numpy(),
        "black": black.to_numpy(), "red": red.to_numpy(),
        "black_ang": best_fit_angle_series(black.to_numpy(), ANGLE_LOOKBACK + 1),
        "price_ang": best_fit_angle_series(close.to_numpy(), PRICE_ANGLE_POINTS),
        "red_ang": best_fit_angle_series(red.to_numpy(), RED_ANGLE_POINTS),
    }


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def et_minutes(ts):
    t = pd.Timestamp(ts).tz_convert(ET)
    return t.hour * 60 + t.minute


def az_label(ts):
    return pd.Timestamp(ts).tz_convert(AZ).strftime("%-I:%M %p AZ")


# ------------------------------------------------------------ the system -----
def buy_signal(A, k, ts):
    """True if the BUY rule holds on bar k."""
    if k < WARMUP_BARS or k < 1:
        return False
    vals = (A["red"][k], A["red"][k - 1], A["black"][k], A["black"][k - 1],
            A["price_ang"][k], A["black_ang"][k])
    if any(math.isnan(v) for v in vals):
        return False
    m = et_minutes(ts)
    if not (hhmm(ALERT_START_ET) <= m < hhmm(ALERT_END_ET)):
        return False
    crossed = A["red"][k - 1] <= A["black"][k - 1] and A["red"][k] > A["black"][k]
    return (crossed and A["price_ang"][k] >= CROSS_MIN_ANGLE and A["black_ang"][k] > SHALLOWED)


def exit_reason(A, k, ts, entry):
    """Why to get out on bar k, or None (stay). Same order as the history test."""
    if A["close"][k] <= entry * (1 - STOP_PCT / 100.0):
        return f"stop: down {STOP_PCT:.1f}% from the alert price"
    if A["red"][k] < A["black"][k]:
        return "red line touched the black line"
    ra = A["red_ang"][k]
    if not math.isnan(ra) and ra <= -SHARP_EXIT_ANGLE:
        return f"sharp peak: red line turned down at {ra:.0f} deg"
    if et_minutes(ts) >= hhmm(EOD_ET):
        return "end of day"
    return None


# ------------------------------------------------------------ TradeStation ---
class Client:
    """READ-ONLY market data. There is no order code anywhere in this file."""

    def __init__(self):
        self.cid = (os.getenv("TS_CLIENT_ID") or os.getenv("TS_API_KEY")
                    or os.getenv("TRADESTATION_CLIENT_ID"))
        self.sec = (os.getenv("TS_CLIENT_SECRET") or os.getenv("TS_SECRET")
                    or os.getenv("TRADESTATION_CLIENT_SECRET"))
        self.rt = os.getenv("TS_REFRESH_TOKEN") or os.getenv("TRADESTATION_REFRESH_TOKEN")
        self.env = (os.getenv("TS_ENV") or "sim").lower()
        if self.env not in TS_BASE:
            raise SystemExit(f"TS_ENV must be 'sim' or 'live', got {self.env!r}")
        miss = [n for n, v in (("TS_CLIENT_ID", self.cid), ("TS_CLIENT_SECRET", self.sec),
                               ("TS_REFRESH_TOKEN", self.rt)) if not v]
        if miss:
            raise SystemExit(f"FATAL: missing TradeStation creds in .env: {miss}")
        self.base = TS_BASE[self.env]
        self.tok = None
        self.exp = 0.0
        self.s = requests.Session()
        self.use_template = True

    def token(self):
        if self.tok and time.time() < self.exp - 60:
            return self.tok
        r = self.s.post(TS_OAUTH_URL, data={"grant_type": "refresh_token", "client_id": self.cid,
                                            "client_secret": self.sec, "refresh_token": self.rt},
                        timeout=15)
        r.raise_for_status()
        j = r.json()
        self.tok = j["access_token"]
        self.exp = time.time() + int(j.get("expires_in", 1200))
        return self.tok

    def bars(self, symbol):
        params = {"interval": 1, "unit": "Minute", "barsback": BAR_LOOKBACK}
        if self.use_template:
            params["sessiontemplate"] = SESSION_TEMPLATE
        r = self.s.get(self.base + f"/marketdata/barcharts/{symbol}",
                       headers={"Authorization": f"Bearer {self.token()}"}, params=params, timeout=20)
        if r.status_code == 400 and self.use_template:
            log(f"WARN TradeStation rejected sessiontemplate={SESSION_TEMPLATE} ({r.text[:120]!r}). "
                f"Retrying without it -- that gives REGULAR-HOURS bars only, so pre-market lines "
                f"are NOT available and every ticker will be skipped.")
            self.use_template = False
            return self.bars(symbol)
        r.raise_for_status()
        data = r.json().get("Bars", [])
        if not data:
            return None
        rows, ts = [], []
        for b in data:
            ts.append(pd.Timestamp(b["TimeStamp"]))
            rows.append({"open": float(b["Open"]), "high": float(b["High"]),
                         "low": float(b["Low"]), "close": float(b["Close"])})
        idx = pd.DatetimeIndex([t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
                                for t in ts])
        df = pd.DataFrame(rows, index=idx).sort_index()
        return df.iloc[:-1] if len(df) > 1 else df     # drop the still-forming bar


def today_bars(df, now_et):
    """Bars from today (ET date), including pre-market (04:00 ET on)."""
    et = df.index.tz_convert(ET)
    mask = np.array([t.date() == now_et.date() for t in et]) & \
           np.array([t.hour * 60 + t.minute >= 4 * 60 for t in et])
    return df[mask]


# ------------------------------------------------------------ per-ticker step
def process_bar(sym, A, idx, k, state, now_check=True):
    """Apply the system to bar k of today's series. Prints alerts, logs closed trades."""
    ts = idx[k]
    pos = state["pos"].get(sym)
    if pos is not None:
        if k <= pos["k"]:
            return
        pos["maxhigh"] = max(pos["maxhigh"], A["high"][k])
        if (pd.Timestamp(ts) - pd.Timestamp(pos["ts"])).total_seconds() / 60.0 <= HI_WINDOW_MIN:
            pos["hi8"] = max(pos["hi8"], A["high"][k])
        why = exit_reason(A, k, ts, pos["entry"])
        if why:
            px = A["close"][k]
            pnl = (px / pos["entry"] - 1) * 100
            mins = int(round((pd.Timestamp(ts) - pd.Timestamp(pos["ts"])).total_seconds() / 60.0))
            print("\a\n" + "-" * 60 + f"\nWINNING SIDE -- EXIT: {sym} @ ~${px:.2f}  ({az_label(ts)})"
                  f"   {pnl:+.1f}%  after {mins} min\nReason: {why}\n" + "-" * 60 + "\n", flush=True)
            log(f"EXIT {sym} @ {px:.4f} pnl={pnl:+.2f}% held={mins}min reason={why}")
            append_row({"date": pd.Timestamp(ts).tz_convert(ET).strftime("%Y-%m-%d"), "ticker": sym,
                        "alert_time_az": az_label(pos["ts"]), "alert_price": f"{pos['entry']:.4f}",
                        "price_angle": f"{pos['pang']:.0f}", "black_angle": f"{pos['bang']:+.1f}",
                        "exit_time_az": az_label(ts), "exit_price": f"{px:.4f}", "pnl_pct": f"{pnl:+.2f}",
                        "exit_reason": why, "minutes_held": mins,
                        "high_within_8min_pct": f"{(pos['hi8'] / pos['entry'] - 1) * 100:+.2f}",
                        "high_before_exit_pct": f"{(pos['maxhigh'] / pos['entry'] - 1) * 100:+.2f}"})
            state["pos"].pop(sym, None)
        return
    if buy_signal(A, k, ts):
        px = A["close"][k]
        state["pos"][sym] = {"k": k, "ts": ts, "entry": px, "maxhigh": A["high"][k], "hi8": A["high"][k],
                             "pang": A["price_ang"][k], "bang": A["black_ang"][k]}
        print("\a\n" + "=" * 60 + f"\nWINNING SIDE -- ALERT: BUY {sym} @ ~${px:.2f}   ({az_label(ts)})\n"
              f"Red line crossed UP through black.   Price angle: {A['price_ang'][k]:.0f} deg   "
              f"Black line angle: {A['black_ang'][k]:+.1f} deg\n"
              f"Plan: stay in until red touches black, or red turns sharply down ({-SHARP_EXIT_ANGLE:.0f} deg), "
              f"or -{STOP_PCT:.0f}% from here.\n"
              f"ALERT ONLY -- nothing was bought. Buy it yourself in TradeStation if you want it.\n"
              + "=" * 60 + "\n", flush=True)
        log(f"ALERT {sym} @ {px:.4f} price-angle={A['price_ang'][k]:.0f} black-angle={A['black_ang'][k]:+.1f}")


def premarket_check(client):
    now_et = datetime.now(ET)
    log(f"PREMARKET CHECK: asking TradeStation for bars with sessiontemplate={SESSION_TEMPLATE}")
    ok = 0
    for sym in SYMBOLS:
        try:
            df = client.bars(sym)
            if df is None:
                log(f"PREMARKET CHECK {sym}: no bars returned")
                continue
            t = today_bars(df, now_et)
            et = t.index.tz_convert(ET)
            pm = sum(1 for x in et if x.hour * 60 + x.minute < 9 * 60 + 30)
            first = et[0].strftime("%H:%M") if len(et) else "none"
            log(f"PREMARKET CHECK {sym}: {len(t)} bars today, {pm} before 9:30 ET, earliest {first} ET"
                + ("" if pm >= MIN_PM_BARS or now_et.hour * 60 + now_et.minute < 9 * 60 + 30
                   else "   <-- TOO FEW, this ticker will be skipped"))
            ok += pm >= MIN_PM_BARS
        except Exception as e:
            log(f"PREMARKET CHECK {sym}: ERROR {e}")
    log(f"PREMARKET CHECK done: {ok} of {len(SYMBOLS)} tickers have at least {MIN_PM_BARS} pre-market bars")


def main():
    ensure_csv()
    client = Client()
    log("WINNING SIDE (ALERT ONLY) starting. NO ORDERS are ever placed by this program.")
    log(f"MODE     CROSS_MIN_ANGLE={CROSS_MIN_ANGLE:.0f} (price, {PRICE_ANGLE_POINTS} closes)  "
        f"BLACK_GATE>{SHALLOWED:.0f} over {ANGLE_LOOKBACK} min  SHARP_EXIT=-{SHARP_EXIT_ANGLE:.0f}  "
        f"STOP={STOP_PCT:.1f}%  WINDOW={ALERT_START_ET}-{ALERT_END_ET} ET  WARMUP={WARMUP_BARS}  "
        f"MIN_PM_BARS={MIN_PM_BARS}  STALE_BAR_MIN={STALE_BAR_MINUTES}")
    log(f"UNIVERSE ({len(SYMBOLS)} tickers) {', '.join(SYMBOLS)}")
    log(f"LOG      {LOG_CSV}")
    premarket_check(client)

    state = {"pos": {}}
    last_k = {}                 # sym -> timestamp of the last bar already processed
    skipped_logged = set()
    while _RUNNING:
        now_et = datetime.now(ET)
        m = now_et.hour * 60 + now_et.minute
        if now_et.weekday() >= 5 or m >= hhmm(CLOSE_ET):
            time.sleep(60)
            continue
        busy = bool(state["pos"]) or (hhmm(ALERT_START_ET) - 2 <= m < hhmm(ALERT_END_ET))
        if not busy:
            time.sleep(30)
            continue
        for sym in SYMBOLS:
            try:
                df = client.bars(sym)
                if df is None:
                    continue
                t = today_bars(df, now_et)
                if len(t) < WARMUP_BARS + ANGLE_LOOKBACK + 2:
                    continue
                et = t.index.tz_convert(ET)
                pm = sum(1 for x in et if x.hour * 60 + x.minute < 9 * 60 + 30)
                if pm < MIN_PM_BARS:
                    if sym not in skipped_logged and m >= 9 * 60 + 30:
                        skipped_logged.add(sym)
                        log(f"SKIP {sym}: only {pm} pre-market bars today (need {MIN_PM_BARS}) -- no alerts for it today")
                    continue
                age_min = (now_et - pd.Timestamp(t.index[-1]).tz_convert(ET)).total_seconds() / 60.0
                if age_min > STALE_BAR_MINUTES:
                    continue
                A = compute_arrays(t)
                n = len(t)
                if sym not in last_k:
                    last_k[sym] = t.index[-1]        # start from now; do not alert on old bars
                    continue
                start = int(np.searchsorted(t.index.values, np.datetime64(last_k[sym].tz_convert("UTC").tz_localize(None)), side="right"))
                for k in range(start, n):
                    process_bar(sym, A, t.index, k, state)
                last_k[sym] = t.index[-1]
            except Exception as e:
                log(f"WARN {sym}: {e}")
        time.sleep(POLL_SECONDS)
    log("stopped. open paper trades at shutdown: " + (", ".join(state["pos"]) or "none"))


if __name__ == "__main__":
    main()
