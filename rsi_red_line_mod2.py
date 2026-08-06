#!/usr/bin/env python3
# =============================================================================
#  RSI RED LINE  --  MOD 2
#
#  Built to the mod2 spec. This is a SEPARATE engine from rsiAlgo.py -- it does
#  not share its signal, its exits, or its parameters. It reuses only the
#  proven TradeStation plumbing (auth, bar fetch, session filter, indicators).
#
#  Behaviour (per spec + Gary's choices):
#    - Signal: RSI(7) arm at 35, black-line angle "was-steep then shallowed"
#      gate, red-line (HMA7) rising. All three true on the same bar -> FIRE.
#    - Entry:  AUTO. Market buy on fire; recorded fill is the entry price.
#              (Spec's reference entry is next bar's open; live fills at market.)
#    - Size:   1 share. Learning run.
#    - Exit:   ONLY mechanical exit is the hard floor at entry x 0.95.
#              Selling is MANUAL (Gary, by hand). EOD flat.
#    - Suppression: per-ticker while open, global slot cap (3), same-pair lock.
#
#  Deliberately ABSENT (spec sections 9 & 10 forbid these):
#    - No volatility-based stop or gate of any kind.
#    - No trailing exit, no bars-held exit, no mechanical sell whatsoever.
#    - No chop, meander, or price-to-red distance filter.
#    - No upper-bound angle gate. No degree-based sell logic.
#    - Nothing from the Black Line Trend System.
#  The ONLY mechanical exit in this file is the hard floor at entry x 0.95.
#
#  Run:
#      cd ~/rsi_system
#      set -a; source .env; set +a
#      ~/algotrend1v5/venv/bin/python3 rsi_red_line_mod2.py
# =============================================================================
 
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
 
# ------------------------------------------------------------ constants ------
# Straight from the spec. CEILING is intentionally absent and must stay absent.
 
RSI_LEN        = 7
RSI_ARM        = 35
WAIT_WINDOW    = 15        # bars an arm stays live
EMA_LEN        = 20        # black
HMA_LEN        = 7         # red
ANGLE_LOOKBACK = 5         # bars
STEEP_LOOKBACK = 30        # bars, excludes current bar
WAS_STEEP      = -20.0     # degrees
SHALLOWED      = -15.0     # degrees
WARMUP_BARS    = 30
HARD_FLOOR     = -5.0      # percent; the ONLY mechanical exit
DISPLAY_TARGET = 2.5       # DISPLAY ONLY -- must not touch control flow
MAX_SLOTS      = 3
 
# Pairs exactly as written in the spec (note SNDQ, not SNDR).
PAIRS = [
    ("NBIL", "NBIZ"),
    ("SOXL", "SOXS"),
    ("RKLX", "RKLZ"),
    ("IRE",  "IREZ"),
    ("IONX", "IONZ"),
    ("SNXX", "SNDQ"),
    ("CWVX", "CORD"),
]
SYMBOLS = [s for pr in PAIRS for s in pr]
PARTNER = {}
for a, b in PAIRS:
    PARTNER[a] = b
    PARTNER[b] = a
 
BAR_MINUTES = 1
BAR_LOOKBACK = 400            # enough for warmup + a full session
MARKET_OPEN_ET = "09:30"
MARKET_CLOSE_ET = "16:00"
NO_NEW_AFTER_ET = "15:58"     # need a next bar in the same session
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
 
 
# ------------------------------------------------------------ indicators -----
# ema and rsi_wilder are identical to the proven versions in rsiAlgo.py.
 
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
    """Hull MA: WMA(2*WMA(n/2) - WMA(n), sqrt(n)).
 
    The spec writes the HMA(7) as WMA(2*WMA(close,3) - WMA(close,7), 3):
    half = round(7/2) = 3 (wait: round(3.5)=4 in banker's rounding, but the
    spec pins half=3 and final=3 explicitly), so we use the spec's exact
    windows rather than the textbook rounding. Do not "fix" this to 4/2."""
    half = 3
    root = 3
    return wma(2 * wma(s, half) - wma(s, n), root)
 
 
def black_angle(black: pd.Series) -> pd.Series:
    """Angle of the black line in MATH degrees, per spec section 3.
 
    pct_per_bar = (black[i]/black[i-5] - 1) * 100 / 5
    angle       = degrees(arctan(pct_per_bar))
    Calibration: 1% per bar == 45 degrees. These are not protractor degrees.
    Do not correct them."""
    prev = black.shift(ANGLE_LOOKBACK)
    pct_per_bar = (black / prev - 1.0) * 100.0 / ANGLE_LOOKBACK
    return np.degrees(np.arctan(pct_per_bar))
 
 
# ------------------------------------------------------------ session --------
 
def session_filter(df):
    """Keep only weekday bars inside 09:30-16:00 ET. Filters by time-of-day so
    prior sessions keep the indicators warm at the open."""
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
# Trimmed to what mod2 needs: auth, bars, quote, account/balance, market buy,
# market sell-to-flat. No ATR, no resting stop -- the spec forbids them.
 
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
    """Everything the signal needs for the latest closed bar."""
    ts: object
    close: float
    open_: float
    low: float
    rsi_now: float
    rsi_prev: float
    red_now: float
    red_prev: float
    angle_now: float
    angle_was: float     # min angle over the prior STEEP_LOOKBACK bars
    bar_index: int       # position in the current session (0-based)
 
 
def build_frame(df: pd.DataFrame) -> Frame:
    close = df["close"]
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
    rsi = rsi_wilder(close, RSI_LEN)
    angle = black_angle(black)
 
    # session index: how many bars since this session's 09:30 ET open
    et_idx = df.index.tz_convert(ET)
    today = et_idx[-1].date()
    session_mask = (et_idx.date == today)
    bar_index = int(session_mask.sum()) - 1
 
    was = angle.iloc[-(STEEP_LOOKBACK + 1):-1]   # prior 30 bars, excl current
    return Frame(
        ts=df.index[-1],
        close=float(close.iloc[-1]),
        open_=float(df["open"].iloc[-1]),
        low=float(df["low"].iloc[-1]),
        rsi_now=float(rsi.iloc[-1]),
        rsi_prev=float(rsi.iloc[-2]),
        red_now=float(red.iloc[-1]),
        red_prev=float(red.iloc[-2]),
        angle_now=float(angle.iloc[-1]),
        angle_was=float(was.min()) if len(was) else float("nan"),
        bar_index=bar_index,
    )
 
 
def arm_fires(fr: Frame) -> bool:
    """ARM event: RSI crosses up through 35. rsi[i-1] < 35 <= rsi[i]."""
    return fr.rsi_prev < RSI_ARM <= fr.rsi_now
 
 
def black_gate_open(fr: Frame) -> bool:
    """Was steep in the last 30 bars, and has now shallowed above -15.
    No upper bound: flat and rising both qualify."""
    if math.isnan(fr.angle_was):
        return False
    return (fr.angle_was <= WAS_STEEP) and (fr.angle_now > SHALLOWED)
 
 
def red_rising(fr: Frame) -> bool:
    return fr.red_now > fr.red_prev
 
 
# ------------------------------------------------------------ state ----------
 
@dataclass
class Pos:
    symbol: str
    entry: float
    floor: float
    qty: int
    opened_ts: object
    armed_until: int = -1     # session bar index the arm expires on
 
 
# per-symbol arm latch (independent of holding a position)
@dataclass
class Arm:
    until_index: int = -1     # arm valid while bar_index <= until_index
 
 
# ------------------------------------------------------------ CSV ------------
 
def ensure_csv():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerow(LOG_COLUMNS)
 
 
def append_alert(row: dict):
    with open(LOG_CSV, "a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in LOG_COLUMNS])
 
 
# ------------------------------------------------------------ clock ----------
 
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
 
 
# ------------------------------------------------------------ main -----------
 
def main():
    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)
 
    api = TradeStationClient()
    api._access_token()
    acct = api.get_account()
    ensure_csv()
 
    _u = datetime.now(ZoneInfo("UTC"))
    log("CLOCK  utc=%s | et=%s | az=%s"
        % (_u.strftime("%H:%M:%S"),
           _u.astimezone(ET).strftime("%H:%M:%S %Z"),
           _u.astimezone(AZ).strftime("%H:%M:%S %Z")))
    log("=" * 64)
    log("RSI RED LINE -- MOD 2")
    log("=" * 64)
    log(f"account={acct.account_number} status={acct.status} "
        f"env={api.env} dry_run={api.dry_run}")
    bal = api.get_balance()
    if bal:
        log(f"BALANCE equity=${bal['equity']:,.2f} cash=${bal['cash']:,.2f}")
        if api.env == "live" and not api.dry_run:
            log("        ^ CHECK THIS ACCOUNT. Ctrl-C now if it is wrong.")
    log(f"SIGNAL  arm=RSI{RSI_LEN} x-up thru {RSI_ARM} (latches {WAIT_WINDOW} bars)"
        f" | black: was<= {WAS_STEEP:.0f} in {STEEP_LOOKBACK}b then now> {SHALLOWED:.0f}"
        f" | red(HMA{HMA_LEN}) rising")
    log(f"EXEC    entry=AUTO market buy, 1 share | floor={HARD_FLOOR:.0f}% "
        f"(ONLY mechanical exit) | sell=MANUAL | EOD flat {EOD_FLATTEN_ET} ET")
    log(f"SUPPRESS per-ticker + global slots<= {MAX_SLOTS} + same-pair lock")
    log(f"UNIVERSE {', '.join(SYMBOLS)}")
    log(f"LOG     {LOG_CSV}")
    if api.env == "live" and not api.dry_run:
        log("*** LIVE TRADING ENABLED -- real orders will be sent ***")
    log("=" * 64)
 
    positions = {}          # symbol -> Pos
    arms = {s: Arm() for s in SYMBOLS}
 
    def slots_in_use():
        return len(positions)
 
    def names_open():
        return "|".join(sorted(positions.keys()))
 
    def pair_leg_open(sym):
        partner = PARTNER.get(sym)
        return partner in positions
 
    while _RUNNING:
        now = et_now()
        if not in_session(now):
            log("Session closed. Sleeping.")
            time.sleep(30)
            continue
 
        # ---- EOD: flatten everything, per spec (session end closes) --------
        if past(now, EOD_FLATTEN_ET):
            for sym, pos in list(positions.items()):
                try:
                    api.market_sell(sym, pos.qty)
                    log(f"EOD-FLAT {sym} qty={pos.qty}")
                except Exception as e:
                    log(f"EOD-FLAT-ERR {sym}: {e}")
                positions.pop(sym, None)
            log("EOD reached. Flat. Exiting loop.")
            break
 
        allow_new = not past(now, NO_NEW_AFTER_ET)
 
        for sym in SYMBOLS:
            try:
                df = api.get_bars(sym)
            except Exception as e:
                log(f"DATA-ERR {sym}: {e}")
                continue
            if df is not None:
                df = session_filter(df)
            if df is None or len(df) < WARMUP_BARS + ANGLE_LOOKBACK + 2:
                continue
 
            fr = build_frame(df)
 
            # -------- manage an OPEN position: hard floor only --------------
            if sym in positions:
                pos = positions[sym]
                if fr.low <= pos.floor:
                    try:
                        api.market_sell(sym, pos.qty)
                    except Exception as e:
                        log(f"FLOOR-SELL-ERR {sym}: {e}")
                    log(f"FLOOR  {sym} low={fr.low:.2f} <= floor={pos.floor:.2f} "
                        f"-- mechanical exit")
                    positions.pop(sym, None)
                # Layer-1 suppression: no re-alert while open, and Gary sells
                # by hand, so nothing else to do for a held name.
                continue
 
            # -------- look for a fresh signal on a FLAT name ----------------
            # ARM latch first (an arm can set even while suppressed elsewhere)
            if arm_fires(fr):
                arms[sym].until_index = fr.bar_index + WAIT_WINDOW
 
            armed = fr.bar_index <= arms[sym].until_index
            if not armed:
                continue
            if not (black_gate_open(fr) and red_rising(fr)):
                continue
 
            # all three conditions hold on this bar -> candidate FIRE
            if fr.bar_index < WARMUP_BARS:
                continue                       # earliest alert is bar 30
            if not allow_new:
                log(f"LATE-SKIP {sym} (no next bar in session)")
                continue
            if slots_in_use() >= MAX_SLOTS:
                log(f"SLOT-SKIP {sym} (slots full {MAX_SLOTS})")
                continue
            if pair_leg_open(sym):
                log(f"PAIR-SKIP {sym} (partner {PARTNER[sym]} already open)")
                continue
 
            # ---- capture slots/names AT THE MOMENT OF FIRE (spec 8) --------
            slots_at_fire = slots_in_use()
            names_at_fire = names_open()
 
            # ---- AUTO enter: market buy 1 share ----------------------------
            fill = fr.close                    # provisional; refined below
            try:
                api.market_buy(sym, 1)
                if not api.dry_run:
                    time.sleep(1.5)
                    live = api.list_positions()
                    if sym in live and live[sym]["avg"] > 0:
                        fill = live[sym]["avg"]
            except Exception as e:
                log(f"BUY-ERR {sym}: {e} -- not entering")
                continue
 
            floor = fill * (1 + HARD_FLOOR / 100.0)
            positions[sym] = Pos(symbol=sym, entry=fill, floor=floor, qty=1,
                                 opened_ts=fr.ts)
 
            log(f"FIRE   {sym} [{bar_et(fr.ts)}] bar {fr.bar_index} "
                f"in @ {fill:.4f} floor {floor:.4f} "
                f"rsi={fr.rsi_now:.1f} angle_now={fr.angle_now:.1f} "
                f"angle_was={fr.angle_was:.1f} slots={slots_at_fire} "
                f"target(disp)={DISPLAY_TARGET}%")
 
            append_alert({
                "time": datetime.now(AZ).strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": sym,
                "price": f"{fill:.4f}",
                "floor_price": f"{floor:.4f}",
                "display_target": DISPLAY_TARGET,
                "rsi": f"{fr.rsi_now:.2f}",
                "angle_now": f"{fr.angle_now:.2f}",
                "angle_was": f"{fr.angle_was:.2f}",
                "slots_in_use": slots_at_fire,
                "names_open": names_at_fire,
                # took onward are Gary's to fill by hand:
                "took": "", "fill": "", "sold_at": "",
                "sold_time": "", "reason": "",
            })
 
        time.sleep(POLL_SECONDS)
 
    log("Shutdown. Open positions (if any) left for manual handling:")
    for sym, pos in positions.items():
        log(f"  HELD {sym} entry={pos.entry:.2f} floor={pos.floor:.2f}")
 
 
if __name__ == "__main__":
    WARMUP_BARS = WARMUP_BARS  # keep name in scope for clarity
    main()
 
