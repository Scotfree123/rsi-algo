d2 · PY
#!/usr/bin/env python3
import os
import sys
import csv
import time
import math
import signal as _sig
from dataclasses import dataclass, field
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
 
RSI_LEN        = 7
RSI_ARM        = 35
SIMUL_BARS     = 3
EMA_LEN        = 20
HMA_LEN        = 7
ANGLE_LOOKBACK = 5
STEEP_LOOKBACK = 30
WAS_STEEP      = -20.0
SHALLOWED      = -15.0
WARMUP_BARS    = 30
HARD_FLOOR     = -5.0
DISPLAY_TARGET = 2.5
MAX_SLOTS      = 3
 
# ---- Real-chart-geometry calibration, confirmed directly against Gary's actual
# chart on 2026-08-24: 11in wide x 4.5in tall, showing the full 9:30-4:00 day.
# This replaces the old plain-math angle formula, which measured "steepness"
# in a way that never matched what these thresholds were designed by eye to mean.
CHART_W_IN = 11.0
CHART_H_IN = 4.5
FULL_DAY_MINUTES = 390
 
PAIRS = [
    ("NBIL", "NBIZ"),
    ("SOXL", "SOXS"),
    ("RKLX", "RKLZ"),
    ("IRE",  "IREZ"),
    ("IONX", "IONZ"),
    ("SNXX", "SNDQ"),
    ("CWVX", "CORD"),
    ("OKLL", "OKLS"),
    ("SKUU", "SKDD"),
    ("AAOX",),
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
NO_NEW_AFTER_ET = "15:58"
EOD_FLATTEN_ET = "15:59"
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or 20)
LOG_CSV = os.getenv("MOD2_LOG") or "rsi_red_line_mod2_log.csv"
 
LOG_COLUMNS = [
    "time", "ticker", "price", "floor_price", "display_target",
    "rsi", "angle_now", "angle_was", "slots_in_use", "names_open",
    "took", "fill", "sold_at", "sold_time", "reason",
]
 
 
def log(msg):
    ts = datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S AZ")
    print(f"{ts} | {msg}", flush=True)
 
 
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()
 
 
def rsi_wilder(close, n):
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    ag = gain.ewm(alpha=1 / n, adjust=False).mean()
    al = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)
 
 
def wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
 
 
def hma(s, n):
    half = 3
    root = 3
    return wma(2 * wma(s, half) - wma(s, n), root)
 
 
def black_angle(black, day_high_so_far, day_low_so_far):
    """Angle of the black line, calibrated to match Gary's REAL chart --
    11in wide x 4.5in tall, showing the full 9:30-4:00 trading day, with the
    y-axis scaled to that day's own high-to-low range so far. This is what
    WAS_STEEP and SHALLOWED were actually designed by eye to mean."""
    in_per_min = CHART_W_IN / FULL_DAY_MINUTES
    rng = (day_high_so_far - day_low_so_far).replace(0, np.nan)
    in_per_dollar = CHART_H_IN / rng
    prev = black.shift(ANGLE_LOOKBACK)
    delta = black - prev
    horiz_in = ANGLE_LOOKBACK * in_per_min
    vert_in = delta * in_per_dollar
    return np.degrees(np.arctan(vert_in / horiz_in))
 
 
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
 
 
@dataclass
class Frame:
    ts: object
    close: float
    open_: float
    low: float
    rsi_now: float
    rsi_prev: float
    red_now: float
    red_prev: float
    red_prev2: float
    angle_now: float
    angle_was: float
    bar_index: int
 
 
def build_frame(df):
    close = df["close"]
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
    rsi = rsi_wilder(close, RSI_LEN)
 
    # running high/low FOR TODAY ONLY, matching how the y-axis on Gary's real
    # chart auto-scales to the current day's range as it unfolds
    dates_et = df.index.tz_convert(ET).date
    day_high_so_far = df["high"].groupby(dates_et).cummax()
    day_low_so_far = df["low"].groupby(dates_et).cummin()
    angle = black_angle(black, day_high_so_far, day_low_so_far)
 
    et_idx = df.index.tz_convert(ET)
    today = et_idx[-1].date()
    session_mask = (et_idx.date == today)
    bar_index = int(session_mask.sum()) - 1
 
    was = angle.iloc[-(STEEP_LOOKBACK + 1):-1]
    return Frame(
        ts=df.index[-1],
        close=float(close.iloc[-1]),
        open_=float(df["open"].iloc[-1]),
        low=float(df["low"].iloc[-1]),
        rsi_now=float(rsi.iloc[-1]),
        rsi_prev=float(rsi.iloc[-2]),
        red_now=float(red.iloc[-1]),
        red_prev=float(red.iloc[-2]),
        red_prev2=float(red.iloc[-3]),
        angle_now=float(angle.iloc[-1]),
        angle_was=float(was.min()) if len(was) else float("nan"),
        bar_index=bar_index,
    )
 
 
def arm_fires(fr):
    return fr.rsi_prev < RSI_ARM <= fr.rsi_now
 
 
def black_gate_open(fr):
    if math.isnan(fr.angle_was):
        return False
    return (fr.angle_was <= WAS_STEEP) and (fr.angle_now > SHALLOWED)
 
 
def red_rising(fr):
    return fr.red_now > fr.red_prev > fr.red_prev2
 
 
@dataclass
class Pos:
    symbol: str
    entry: float
    floor: float
    qty: int
    opened_ts: object
    armed_until: int = -1
 
 
@dataclass
class Arm:
    last_rsi_cross: int = -1
    last_black: int = -1
    last_red: int = -1
 
 
def ensure_csv():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerow(LOG_COLUMNS)
 
 
def append_alert(row):
    with open(LOG_CSV, "a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in LOG_COLUMNS])
 
 
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
 
 
def main():
    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)
 
    api = TradeStationClient()
    api._access_token()
    acct = api.get_account()
    ensure_csv()
    positions = {}
    arms = {s: Arm() for s in SYMBOLS}
 
 
if __name__ == "__main__":
    main()
 
