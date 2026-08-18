
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
#    - Size:   ~$1000 notional per entry. Share count = floor(TRADE_NOTIONAL /
#              price at fire), minimum 1 share. Updated 2026-08-18 (was fixed
#              1 share -- "learning run" sizing).
#    - Exit:   ONLY mechanical exit is the hard floor at entry x 0.95.
#              Selling is MANUAL (Gary, by hand). EOD flat.
#    - Suppression: per-ticker while open, global slot cap (10), same-pair lock.
#              Slot cap raised 2026-08-18 from 3 -> 10 (Gary's choice). Note:
#              the broker-sync below already detects Gary's manual sells and
#              frees the slot mid-day -- that logic did not change, only the
#              cap did.
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
SIMUL_BARS     = 3         # all three conditions must occur within this many
                           # bars of each other (replaces the old wait window)
EMA_LEN        = 20        # black
HMA_LEN        = 7         # red
ANGLE_LOOKBACK = 5         # bars
STEEP_LOOKBACK = 30        # bars, excludes current bar
WAS_STEEP      = -20.0     # degrees
SHALLOWED      = -15.0     # degrees
WARMUP_BARS    = 30
HARD_FLOOR     = -5.0      # percent; the ONLY mechanical exit
DISPLAY_TARGET = 2.5       # DISPLAY ONLY -- must not touch control flow
MAX_SLOTS      = 10        # raised 2026-08-18 from 3 -> 10 (Gary's choice)
 
# Position sizing: dollar notional per entry rather than a fixed share count.
# qty = floor(TRADE_NOTIONAL / price-at-fire), minimum 1 share. Overridable
# via env for testing without touching this file.
TRADE_NOTIONAL = float(os.getenv("TRADE_NOTIONAL") or 1000.0)
 
# Groups. A 2-tuple is a long/inverse pair (mutually exclusive, one slot);
# a 1-tuple is a lone symbol that just occupies a slot with no partner.
# SNXX/SNDQ per spec (note SNDQ, not SNDR). OKLL/OKLS, SKUU/SKDD, AAOX added.
# !! VERIFY: confirm every symbol resolves at TradeStation and that each
#    pair's two legs are genuine inverses of the same underlying before live.
PAIRS = [
    ("NBIL", "NBIZ"),
    ("SOXL", "SOXS"),
    ("RKLX", "RKLZ"),
    ("IRE",  "IREZ"),
    ("IONX", "IONZ"),
    ("SNXX", "SNDQ"),
    ("CWVX", "CORD"),
    ("OKLL", "OKLS"),      # added
    ("SKUU", "SKDD"),      # added
    ("AAOX",),             # added -- singleton, no inverse leg
]
SYMBOLS = [s for pr in PAIRS for s in pr]
PARTNER = {}
for _grp in PAIRS:
    if len(_grp) == 2:
        PARTNER[_grp[0]] = _grp[1]
        PARTNER[_grp[1]] = _grp[0]
    # singletons have no partner; PARTNER.get(sym) will return None
 
_dupes = [s for s in set(SYMBOLS) if SYMBOLS.count(s) > 1]
if _dupes:
    raise SystemExit(f"FATAL: duplicate symbols in PAIRS: {sorted(_dupes)}")
 
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
    red_prev2: float
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
        red_prev2=float(red.iloc[-3]),
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
    """Red line rising TWO bars in a row (param #3, CHANGED from 1 bar).
    red[i] > red[i-1] > red[i-2]."""
    return fr.red_now > fr.red_prev > fr.red_prev2
 
 
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
    # 3-bar simultaneity clock (params #5/#6). Instead of an arm latching for a
    # fixed window, we remember the most recent session-bar index at which each
    # of the three conditions was individually satisfied. FIRE when all three
    # fall within SIMUL_BARS of each other. -1 means "not seen this session".
    last_rsi_cross: int = -1     # RSI crossed up through the level
    last_black: int = -1         # black gate open
    last_red: int = -1           # red rising 2 bars
 
 
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
    log(f"SIGNAL  arm=RSI{RSI_LEN} x-up thru {RSI_ARM}"
        f" | black: was<= {WAS_STEEP:.0f} in {STEEP_LOOKBACK}b then now> {SHALLOWED:.0f}"
        f" | red(HMA{HMA_LEN}) rising 2 bars"
        f" | all 3 within {SIMUL_BARS} bars")
    log(f"EXEC    entry=AUTO market buy, ~${TRADE_NOTIONAL:,.0f} notional "
        f"(dynamic share count) | floor={HARD_FLOOR:.0f}% "
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
 
        # ---- reconcile with the broker: did Gary sell by hand? -------------
        # The engine cannot see Gary's manual sells directly. Each cycle we ask
        # the broker what actually exists. If we think we hold a name but the
        # broker shows it flat, Gary sold it -> drop it, free the slot, and
        # reset its arm state so a FRESH 3-condition cluster is required before
        # re-entering (no instant re-fire on stale conditions).
        # Skipped in dry_run: the broker has no simulated positions, so syncing
        # would wrongly wipe everything (the same trap rsiAlgo.py hit).
        if not api.dry_run and positions:
            try:
                live_pos = api.list_positions()
                for sym in list(positions.keys()):
                    held_qty = live_pos.get(sym, {}).get("qty", 0)
                    if held_qty <= 0:
                        positions.pop(sym, None)
                        arms[sym] = Arm()      # reset -> needs a fresh signal
                        log(f"GARY-SOLD {sym} -- broker flat, engine was holding. "
                            f"Slot freed, {sym} eligible to re-enter today.")
            except Exception as e:
                log(f"SYNC-WARN could not list positions: {e} "
                    f"(keeping engine state as-is this cycle)")
 
        allow_new = not past(now, NO_NEW_AFTER_ET)
        board = []          # one row per symbol, printed at end of cycle
 
        for sym in SYMBOLS:
            try:
                df = api.get_bars(sym)
            except Exception as e:
                log(f"DATA-ERR {sym}: {e}")
                board.append((sym, "DATA-ERR", ""))
                continue
            if df is not None:
                df = session_filter(df)
            if df is None or len(df) < WARMUP_BARS + ANGLE_LOOKBACK + 2:
                board.append((sym, "warmup", ""))
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
                    board.append((sym, "FLOOR-EXIT", ""))
                else:
                    pnl = (fr.close / pos.entry - 1) * 100 if pos.entry else 0.0
                    board.append((sym, "HELD",
                                  "in @ %.2f x%d  %+.1f%%  floor %.2f  (Gary sells)"
                                  % (pos.entry, pos.qty, pnl, pos.floor)))
                # Layer-1 suppression: no re-alert while open, and Gary sells
                # by hand, so nothing else to do for a held name.
                continue
 
            # -------- 3-bar simultaneity clock (params #5/#6) ---------------
            # Evaluate all three conditions on THIS bar and record when each was
            # last true. Fire only when all three have occurred within a window
            # of SIMUL_BARS (3) bars of one another. This replaces the old
            # 15-bar arm latch; the conditions no longer need the exact same bar
            # but must cluster inside 3 bars.
            a = arms[sym]
            i = fr.bar_index
            c_rsi = arm_fires(fr)
            c_black = black_gate_open(fr)
            c_red = red_rising(fr)
            if c_rsi:
                a.last_rsi_cross = i
            if c_black:
                a.last_black = i
            if c_red:
                a.last_red = i
 
            # ---- how close is this ticker to firing? (for the board) -------
            # A condition counts as "live" if it fired within the last
            # SIMUL_BARS bars. Show R/B/^ for rsi-cross / black-gate / red-rise.
            def _live(last):
                return last >= 0 and (i - last) <= (SIMUL_BARS - 1)
            live_r = _live(a.last_rsi_cross)
            live_b = _live(a.last_black)
            live_e = _live(a.last_red)
            n_live = sum((live_r, live_b, live_e))
            flags = "%s%s%s" % ("R" if live_r else "-",
                                "B" if live_b else "-",
                                "^" if live_e else "-")
            if n_live == 3:
                state = "ARMED*"           # all three live -> should fire now
            elif n_live == 2:
                state = "ready"            # two of three within window
            elif n_live == 1:
                state = "watch"
            else:
                state = "flat"
            detail = ("%s  rsi=%.1f ang=%.1f was=%.1f red=%.3f"
                      % (flags, fr.rsi_now, fr.angle_now, fr.angle_was, fr.red_now))
            board.append((sym, state, detail))
 
            seen = (a.last_rsi_cross, a.last_black, a.last_red)
            if -1 in seen:
                continue                       # not all three have happened yet
            if (max(seen) - min(seen)) > (SIMUL_BARS - 1):
                continue                       # they did not cluster in 3 bars
            if max(seen) != i:
                continue                       # only fire on the completing bar
 
            # all three conditions satisfied within a 3-bar window -> FIRE
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
 
            # ---- size the order: ~$1000 notional at the current price ------
            # fr.close is the last closed bar's price -- close enough to a
            # market fill to size the order; the ACTUAL fill price (and
            # therefore actual notional) is refined just below once the
            # broker reports the average fill.
            if fr.close <= 0:
                log(f"BUY-ERR {sym}: non-positive price {fr.close}, not entering")
                continue
            qty = max(1, int(TRADE_NOTIONAL // fr.close))
 
            # ---- AUTO enter: market buy ~$1000 notional ---------------------
            fill = fr.close                    # provisional; refined below
            try:
                api.market_buy(sym, qty)
                if not api.dry_run:
                    time.sleep(1.5)
                    live = api.list_positions()
                    if sym in live and live[sym]["avg"] > 0:
                        fill = live[sym]["avg"]
                        if live[sym]["qty"] > 0:
                            qty = live[sym]["qty"]   # true filled quantity
            except Exception as e:
                log(f"BUY-ERR {sym}: {e} -- not entering")
                continue
 
            floor = fill * (1 + HARD_FLOOR / 100.0)
            positions[sym] = Pos(symbol=sym, entry=fill, floor=floor, qty=qty,
                                 opened_ts=fr.ts)
 
            log(f"FIRE   {sym} [{bar_et(fr.ts)}] bar {fr.bar_index} "
                f"in @ {fill:.4f} x{qty} (~${fill * qty:,.0f}) floor {floor:.4f} "
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
 
        # ---- status board: who is armed / close to firing -----------------
        # Order by how close each ticker is: ARMED* first, then ready, watch...
        rank = {"ARMED*": 0, "ready": 1, "watch": 2, "HELD": 3, "flat": 4,
                "warmup": 5, "DATA-ERR": 6, "FLOOR-EXIT": 0}
        board.sort(key=lambda r: (rank.get(r[1], 9), r[0]))
        et_hm = now.strftime("%H:%M")
        log("---- BOARD %s ET  (flags R=rsi-cross B=black-gate ^=red-rising; "
            "all 3 within %d bars = fire) ----" % (et_hm, SIMUL_BARS))
        for sym, state, detail in board:
            log("  %-5s %-8s %s" % (sym, state, detail))
        n_armed = sum(1 for _, s, _ in board if s == "ARMED*")
        n_ready = sum(1 for _, s, _ in board if s == "ready")
        log("  slots %d/%d   armed=%d ready=%d   held=%s"
            % (slots_in_use(), MAX_SLOTS, n_armed, n_ready,
               names_open() or "none"))
 
        time.sleep(POLL_SECONDS)
 
    log("Shutdown. Open positions (if any) left for manual handling:")
    for sym, pos in positions.items():
        log(f"  HELD {sym} entry={pos.entry:.2f} qty={pos.qty} floor={pos.floor:.2f}")
 
 
if __name__ == "__main__":
    WARMUP_BARS = WARMUP_BARS  # keep name in scope for clarity
    main()
