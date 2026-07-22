#!/usr/bin/env python3
# =============================================================================
#  RSI SYSTEM  (RSI Signal Buy-Sell)  --  Python runner v1.2  [TradeStation]
#  Innovative Investment Research -- TradingSystem
#  TradeStation WebAPI v3 (OAuth refresh-token)  |  Lightsail / tmux
#
#  Spec:  RSI_System_Spec_v1_2.docx (May 30, 2026)
#
#  PORT NOTE
#  ---------
#  This is the Alpaca-paper engine ported to TradeStation. The ENTIRE strategy
#  layer below the "broker adapter" line is UNCHANGED from the working Alpaca
#  runner: indicators, the two-step (arm -> fire) state machine, black-line and
#  chop filters, portfolio slots, stop-and-reverse, session gating. Only the
#  broker calls were swapped, via a TradeStationClient that exposes the SAME
#  method names the engine already called (get_bars/submit_order/list_positions/
#  get_position/get_latest_trade/close_position/get_account).
#
#  SAFETY (read this)
#  ------------------
#   * TS_ENV       = "sim" (default) or "live". "sim" hits sim-api.tradestation.com
#                    and touches nothing on the real account. Start here.
#   * DRY_RUN      = "1" (default). Logs FIRE/EXIT/orders but does NOT send them.
#                    Set DRY_RUN=0 only after you've watched a full sim session.
#   These two guards replace the Alpaca "refuse if not paper" guard. Flip them
#   deliberately -- this trades Gary's real account when TS_ENV=live AND DRY_RUN=0.
#
#  CALIBRATION: SLOPE_PER_DEGREE is STILL a placeholder (0.0025). This is the
#  reason the live logs never fired -- black_ok/chop_ok rejected every candidate.
#  Calibrate per spec 6.2 before the degree thresholds mean anything.
#
#  Run (in tmux on Lightsail, venv active):
#      source ~/algotrend1v5/venv/bin/activate
#      set -a; source .env; set +a
#      python3 rsi_system_ts.py
# =============================================================================

import os
import sys
import time
import math
import signal as _sig
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# ------------------------------------------------------------------ config ---

AZ = ZoneInfo("America/Phoenix")     # spec 10.4 -- log/display timezone
ET = ZoneInfo("America/New_York")    # session gating (exchange time)

PAIRS = [
    ("RGTX", "RGTZ"),
    ("NBIL", "NBIZ"),
    ("APLX", "APLZ"),
    ("IRE",  "IREZ"),   # verify ticker resolution (spec 11.1)
    ("MSTU", "MSTZ"),
    ("CRWV", "CORD"),   # verify pairing (spec 11.1)
]
SYMBOLS = [s for pair in PAIRS for s in pair]
PAIR_OF = {s: i for i, pair in enumerate(PAIRS) for s in pair}
SIDE_OF = {pair[0]: "LONG" for pair in PAIRS}
SIDE_OF.update({pair[1]: "INVERSE" for pair in PAIRS})


@dataclass
class Params:
    # indicator periods (locked, spec 7)
    rsi_period: int = 7
    rsi_arm_level: float = 30.0
    hma_period: int = 7
    ema_period: int = 20
    atr_period: int = 14
    # entry state machine (spec 5.2)
    armed_window: int = 8
    red_inflect_lookback: int = 2
    # black-line multi-window slope (spec 5.7)
    block_thresh_deg: float = 8.0
    block_window: int = 6
    decel_window: int = 10
    # sell rule (spec 5.3/5.7)
    sell_thresh_deg: float = 10.0
    sell_window: int = 6
    sell_consec_bars: int = 2
    # chop filter (spec 5.6)
    chop_flip_lookback: int = 20
    max_hma_flips: int = 3
    min_swing_amp_atr: float = 0.5
    # hard stop (spec 5.4)
    stop_mult_atr: float = 1.5
    # session (spec 5.5), exchange time HH:MM
    no_entry_after_et: str = "15:45"
    flatten_at_et: str = "15:55"
    market_open_et: str = "09:30"
    market_close_et: str = "16:00"
    # sizing (spec 5.1)
    money_per_position: float = 10_000.0
    max_slots: int = 3
    # calibration (spec 6) -- PLACEHOLDER, must be calibrated
    slope_per_degree: float = 0.0025
    # engine
    bar_minutes: int = 3
    poll_seconds: int = 15
    bar_lookback: int = 200   # bars fetched per symbol per cycle


P = Params()

# --------------------------------------------------------------- logging ----

os.makedirs("logs", exist_ok=True)
_run_ts = datetime.now(AZ).strftime("%Y%m%d_%H%M%S")
_log_path = f"logs/live_run_{_run_ts}.log"

logger = logging.getLogger("rsi_system")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s AZ | %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(_log_path)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logging.Formatter.converter = lambda *a: datetime.now(AZ).timetuple()
logger.addHandler(_fh)
logger.addHandler(_ch)


def log(msg):
    logger.info(msg)


# =============================================================================
#  BROKER ADAPTER  --  TradeStation WebAPI v3
#  Everything ABOVE stays generic; everything BELOW this block until the
#  "indicators" banner is the only broker-specific code. It mirrors the exact
#  method surface the engine used on the Alpaca REST object:
#     get_bars(...).df   list_positions()   get_position(sym)
#     get_latest_trade(sym).price   submit_order(...)   close_position(sym)
#     get_account()
#  so the strategy code needs zero changes.
#
#  If you'd rather wire into v5's existing TradeStation layer than use this
#  self-contained client, replace the method bodies with calls into that layer
#  -- the signatures are what the engine depends on, not the internals.
# =============================================================================

TS_OAUTH_URL = "https://signin.tradestation.com/oauth/token"
TS_BASE = {
    "live": "https://api.tradestation.com/v3",
    "sim":  "https://sim-api.tradestation.com/v3",
}


# lightweight attribute-style records so engine's pos.avg_entry_price etc. work
@dataclass
class _Position:
    symbol: str
    avg_entry_price: float
    qty: int


@dataclass
class _Trade:
    price: float


@dataclass
class _Account:
    account_number: str
    status: str


@dataclass
class _Bars:
    df: pd.DataFrame


class TradeStationClient:
    def __init__(self):
        # env var names -- adjust the getenv keys to match what's in your .env
        self.client_id = os.getenv("TS_CLIENT_ID") or os.getenv("TRADESTATION_CLIENT_ID")
        self.client_secret = os.getenv("TS_CLIENT_SECRET") or os.getenv("TRADESTATION_CLIENT_SECRET")
        self.refresh_token = os.getenv("TS_REFRESH_TOKEN") or os.getenv("TRADESTATION_REFRESH_TOKEN")
        self.account_id = os.getenv("TS_ACCOUNT_ID") or os.getenv("TRADESTATION_ACCOUNT_ID")
        self.env = (os.getenv("TS_ENV") or "sim").lower()
        self.dry_run = (os.getenv("DRY_RUN") or "1") != "0"
        self.drop_forming_bar = (os.getenv("DROP_FORMING_BAR") or "1") != "0"

        if self.env not in TS_BASE:
            raise SystemExit(f"TS_ENV must be 'sim' or 'live', got {self.env!r}")
        self.base = TS_BASE[self.env]

        missing = [k for k, v in {
            "TS_CLIENT_ID": self.client_id,
            "TS_CLIENT_SECRET": self.client_secret,
            "TS_REFRESH_TOKEN": self.refresh_token,
            "TS_ACCOUNT_ID": self.account_id,
        }.items() if not v]
        if missing:
            raise SystemExit(f"FATAL: missing TradeStation creds in .env: {missing}")

        self._token = None
        self._token_exp = 0.0
        self._session = requests.Session()

    # ---- auth -----------------------------------------------------------
    def _access_token(self):
        # refresh ~60s before the 1200s expiry TS grants
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

    # ---- market data ----------------------------------------------------
    def get_bars(self, symbol, tf=None, start=None, limit=None, feed=None):
        # TS barcharts: interval + unit; returns Bars[] with UTC TimeStamp.
        params = {
            "interval": P.bar_minutes,
            "unit": "Minute",
            "barsback": limit or P.bar_lookback,
        }
        data = self._get(f"/marketdata/barcharts/{symbol}", params)
        bars = data.get("Bars", [])
        if not bars:
            return _Bars(None)
        rows = []
        idx = []
        for b in bars:
            idx.append(pd.Timestamp(b["TimeStamp"]))  # ISO8601 UTC
            rows.append({
                "open":   float(b["Open"]),
                "high":   float(b["High"]),
                "low":    float(b["Low"]),
                "close":  float(b["Close"]),
                "volume": float(b.get("TotalVolume", 0) or 0),
            })
        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
        # TS may include the still-forming current bar; the Alpaca path only
        # saw CLOSED bars. Drop the last row to match that, unless disabled.
        if self.drop_forming_bar and len(df) > 1:
            df = df.iloc[:-1]
        return _Bars(df)

    def get_latest_trade(self, symbol):
        data = self._get(f"/marketdata/quotes/{symbol}")
        q = (data.get("Quotes") or [{}])[0]
        last = q.get("Last") or q.get("Close") or 0.0
        return _Trade(float(last))

    # ---- brokerage ------------------------------------------------------
    def get_account(self):
        data = self._get("/brokerage/accounts")
        accts = data.get("Accounts", [])
        me = next((a for a in accts if a.get("AccountID") == self.account_id),
                  accts[0] if accts else {})
        return _Account(me.get("AccountID", self.account_id),
                        me.get("Status", "UNKNOWN"))

    def list_positions(self):
        data = self._get(f"/brokerage/accounts/{self.account_id}/positions")
        out = []
        for p in data.get("Positions", []):
            qty = int(float(p.get("Quantity", 0)))
            out.append(_Position(
                symbol=p.get("Symbol"),
                avg_entry_price=float(p.get("AveragePrice", 0) or 0),
                qty=abs(qty),
            ))
        return out

    def get_position(self, symbol):
        for p in self.list_positions():
            if p.symbol == symbol:
                return p
        raise ValueError(f"no position for {symbol}")

    # ---- order execution ------------------------------------------------
    def _order(self, symbol, qty, action):
        body = {
            "AccountID": self.account_id,
            "Symbol": symbol,
            "Quantity": str(int(qty)),
            "OrderType": "Market",
            "TradeAction": action,          # "BUY" | "SELL"
            "TimeInForce": {"Duration": "DAY"},
            "Route": "Intelligent",
        }
        if self.dry_run:
            log(f"DRY-RUN order suppressed: {action} {qty} {symbol}")
            return {"dry_run": True}
        return self._post("/orderexecution/orders", body)

    def submit_order(self, symbol=None, qty=None, side="buy",
                     type="market", time_in_force="day"):
        action = "BUY" if side.lower() == "buy" else "SELL"
        return self._order(symbol, qty, action)

    def close_position(self, symbol):
        # TS has no one-shot close; offset the held qty with a market order.
        pos = self.get_position(symbol)
        if pos.qty <= 0:
            return {"noop": True}
        # long-only system (inverse ETFs are also long) -> SELL to close
        return self._order(symbol, pos.qty, "SELL")


# ------------------------------------------------------------ state ----------

@dataclass
class SymbolState:
    armed_bars_left: int = 0
    last_bar_ts: pd.Timestamp = None
    atr_at_entry: float = 0.0
    stop_level: float = 0.0
    entry_price: float = 0.0
    qty: int = 0
    stop_set: bool = False


@dataclass
class PairState:
    state: str = "FLAT"      # FLAT | LONG | INVERSE
    symbol: str = None       # held ETF symbol


class Portfolio:
    """Slots + pair mutual-exclusion + stop-and-reverse (spec 10.1/10.2)."""

    def __init__(self, api, sym_state):
        self.api = api
        self.sym = sym_state
        self.pairs = [PairState() for _ in PAIRS]

    def slots_used(self):
        return sum(1 for p in self.pairs if p.state != "FLAT")

    def sync_from_broker(self):
        """Reconcile internal state to actual TradeStation positions on startup."""
        try:
            live = {p.symbol: p for p in self.api.list_positions()}
        except Exception as e:
            log(f"WARN could not list positions: {e}")
            return
        for i, pair in enumerate(PAIRS):
            long_s, inv_s = pair
            if long_s in live:
                self.pairs[i] = PairState("LONG", long_s)
            elif inv_s in live:
                self.pairs[i] = PairState("INVERSE", inv_s)
            else:
                self.pairs[i] = PairState("FLAT", None)
        for sym, pos in live.items():
            if sym not in self.sym:
                continue
            st = self.sym[sym]
            st.entry_price = float(pos.avg_entry_price)
            st.qty = abs(int(float(pos.qty)))
            st.atr_at_entry = st.atr_at_entry or 0.0
        log(f"SYNC slots_used={self.slots_used()} pairs="
            + ", ".join(f"{PAIRS[i][0]}/{PAIRS[i][1]}:{p.state}"
                        for i, p in enumerate(self.pairs) if p.state != "FLAT"))

    def _submit_buy(self, symbol, price, atr_val):
        qty = max(1, int(P.money_per_position // price))
        try:
            self.api.submit_order(symbol=symbol, qty=qty, side="buy",
                                  type="market", time_in_force="day")
        except Exception as e:
            log(f"ORDER-ERR buy {symbol}: {e}")
            return False
        st = self.sym[symbol]
        st.atr_at_entry = atr_val
        st.qty = qty
        st.stop_set = False
        log(f"FIRE   {symbol} buy qty={qty} ~px={price:.2f} atr={atr_val:.4f}")
        return True

    def _close(self, symbol, reason):
        try:
            self.api.close_position(symbol)
        except Exception as e:
            log(f"ORDER-ERR close {symbol}: {e}")
            return
        st = self.sym[symbol]
        st.stop_level = 0.0
        st.stop_set = False
        st.qty = 0
        log(f"EXIT   {symbol} ({reason})")

    def on_fire(self, symbol, price, atr_val):
        i = PAIR_OF[symbol]
        pair = self.pairs[i]
        side = SIDE_OF[symbol]
        if pair.state != "FLAT" and pair.symbol == symbol:
            return  # already holding this side
        if pair.state == "FLAT":
            if self.slots_used() >= P.max_slots:
                log(f"SLOT-SKIP {symbol} (slots full {P.max_slots})")
                return
            if self._submit_buy(symbol, price, atr_val):
                self.pairs[i] = PairState(side, symbol)
        else:
            # opposite side held -> stop-and-reverse, reuse the slot
            log(f"REVERSE {pair.symbol} -> {symbol}")
            self._close(pair.symbol, "pair-reverse")
            if self._submit_buy(symbol, price, atr_val):
                self.pairs[i] = PairState(side, symbol)
            else:
                self.pairs[i] = PairState("FLAT", None)

    def on_exit(self, symbol, reason):
        i = PAIR_OF[symbol]
        if self.pairs[i].symbol == symbol:
            self._close(symbol, reason)
            self.pairs[i] = PairState("FLAT", None)

    def held_symbols(self):
        return [p.symbol for p in self.pairs if p.state != "FLAT"]


# ------------------------------------------------------------ signals -------

def fetch_bars(api, symbol, feed):
    bars = api.get_bars(symbol, start=None, limit=P.bar_lookback, feed=feed)
    df = bars.df
    if df is None or df.empty:
        return None
    return df[["open", "high", "low", "close", "volume"]].copy()


def compute_signals(df):
    """Return per-bar signal dict for the LAST closed bar, or None if short."""
    need = P.ema_period + max(12, P.decel_window, P.chop_flip_lookback) + 5
    if len(df) < need:
        return None

    c = df["close"]
    rsi = rsi_wilder(c, P.rsi_period)
    h = hma(c, P.hma_period)
    e = ema(c, P.ema_period)
    a = atr_wilder(df, P.atr_period)
    atr_now = float(a.iloc[-1])
    if atr_now <= 0:
        return None

    # ATR-normalized least-squares slopes (spec 5.7); 3-bar is diagnostic only
    sl3 = float(linreg_slope(e, 3).iloc[-1]) / atr_now
    sl6 = float(linreg_slope(e, P.block_window).iloc[-1]) / atr_now
    s10_series = linreg_slope(e, P.decel_window) / atr_now
    sl10 = float(s10_series.iloc[-1])
    sl10_prev = float(s10_series.iloc[-2])
    sl12 = float(linreg_slope(e, 12).iloc[-1]) / atr_now

    # thresholds: degrees -> normalized slope (spec 6.4)
    block_t = P.block_thresh_deg * P.slope_per_degree
    sell_t = P.sell_thresh_deg * P.slope_per_degree

    # Stage 1 arm trigger: RSI crosses up through arm level (spec 5.2)
    rsi_cross_up = (rsi.iloc[-2] < P.rsi_arm_level <= rsi.iloc[-1])

    # Stage 2 fire conditions
    k = P.red_inflect_lookback
    turn_up = (h.iloc[-1] - h.iloc[-1 - k] > 0) and \
              (h.iloc[-2] - h.iloc[-2 - k] <= 0)
    above_hma = c.iloc[-1] > h.iloc[-1]

    # black-line entry blocker (spec 5.7)
    blk_a = sl6 >= -block_t
    blk_b = (sl10 >= 0) or (sl10 > sl10_prev)
    black_ok = blk_a and blk_b

    # chop filter (spec 5.6) on HMA, immune to EMA weighting
    hd = np.sign(h.diff())
    win = hd.iloc[-P.chop_flip_lookback:]
    prev = hd.shift(1).iloc[-P.chop_flip_lookback:]
    flips = int(((win != prev) & (win != 0) & (prev != 0)).sum())
    hwin = h.iloc[-P.chop_flip_lookback:]
    amp_ok = (hwin.max() - hwin.min()) >= P.min_swing_amp_atr * atr_now
    chop_ok = (flips <= P.max_hma_flips) and amp_ok

    # sell rule: 6-bar slope <= sell thresh for last N bars (spec 5.3/5.7)
    s6_series = linreg_slope(e, P.sell_window) / atr_now
    sell_now = bool((s6_series.iloc[-P.sell_consec_bars:] <= sell_t).all())

    return {
        "ts": df.index[-1],
        "price": float(c.iloc[-1]),
        "atr": atr_now,
        "rsi": float(rsi.iloc[-1]),
        "rsi_cross_up": bool(rsi_cross_up),
        "turn_up": bool(turn_up),
        "above_hma": bool(above_hma),
        "black_ok": bool(black_ok),
        "chop_ok": bool(chop_ok),
        "sell_now": sell_now,
        "sl3": sl3, "sl6": sl6, "sl10": sl10, "sl12": sl12,
        "flips": flips,
    }


# ------------------------------------------------------------ indicators ----

def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hma(close: pd.Series, n: int) -> pd.Series:
    # HMA = WMA( 2*WMA(c, n/2) - WMA(c, n), round(sqrt n) )  (spec 4.2)
    half = max(1, int(n // 2))
    sq = max(1, int(round(math.sqrt(n))))
    raw = 2 * wma(close, half) - wma(close, n)
    return wma(raw, sq)


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


def atr_wilder(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def linreg_slope(s: pd.Series, n: int) -> pd.Series:
    x = np.arange(n)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    def _slope(y):
        return ((x - xm) * (y - y.mean())).sum() / denom
    return s.rolling(n).apply(_slope, raw=True)


# ------------------------------------------------------------ session -------

def _hhmm(s):
    hh, mm = s.split(":")
    return int(hh), int(mm)


def et_now():
    return datetime.now(ET)


def in_session(now_et):
    o = now_et.replace(**dict(zip(("hour", "minute"), _hhmm(P.market_open_et))),
                       second=0, microsecond=0)
    c = now_et.replace(**dict(zip(("hour", "minute"), _hhmm(P.market_close_et))),
                       second=0, microsecond=0)
    return o <= now_et <= c and now_et.weekday() < 5


def past(now_et, hhmm_str):
    hh, mm = _hhmm(hhmm_str)
    mark = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_et >= mark


# ------------------------------------------------------------ main ----------

_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False
    log("SIGINT -- shutting down (positions left to EOD logic).")


def main():
    load_dotenv()
    api = TradeStationClient()   # reads TS_* creds + TS_ENV + DRY_RUN from .env

    acct = api.get_account()
    log(f"START RSI System v1.2 [TradeStation/{api.env}] "
        f"account={acct.account_number} status={acct.status} "
        f"dry_run={api.dry_run} bars={P.bar_minutes}min "
        f"symbols={len(SYMBOLS)} | log={_log_path}")
    if api.env == "live" and not api.dry_run:
        log("*** LIVE TRADING ENABLED -- real orders will be sent ***")
    log("CALIBRATION REMINDER: slope_per_degree is a placeholder (spec 6).")

    sym_state = {s: SymbolState() for s in SYMBOLS}
    pf = Portfolio(api, sym_state)
    pf.sync_from_broker()

    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)

    while _RUNNING:
        now_et = et_now()

        if not in_session(now_et):
            time.sleep(P.poll_seconds)
            continue

        # ---- EOD flatten (spec 5.5): highest priority -------------------
        if past(now_et, P.flatten_at_et):
            for sym in list(pf.held_symbols()):
                pf.on_exit(sym, "EOD-flatten")
            time.sleep(P.poll_seconds)
            continue

        # ---- fast hard-stop check between bars (spec 5.4) ---------------
        for sym in list(pf.held_symbols()):
            st = sym_state[sym]
            if not st.stop_set:
                continue
            try:
                last = float(api.get_latest_trade(sym).price)
            except Exception:
                continue
            if last <= st.stop_level:
                log(f"STOP   {sym} px={last:.2f} <= stop={st.stop_level:.2f}")
                pf.on_exit(sym, "hard-stop")

        no_new_entries = past(now_et, P.no_entry_after_et)

        # ---- per-symbol bar evaluation ----------------------------------
        for sym in SYMBOLS:
            try:
                df = fetch_bars(api, sym, None)
            except Exception as e:
                log(f"DATA-ERR {sym}: {e}")
                continue
            if df is None:
                continue

            st = sym_state[sym]
            newest = df.index[-1]

            # set/refresh stop once the fill's avg price is known
            i = PAIR_OF[sym]
            if pf.pairs[i].symbol == sym and not st.stop_set:
                try:
                    pos = api.get_position(sym)
                    st.entry_price = float(pos.avg_entry_price)
                    st.stop_level = st.entry_price - P.stop_mult_atr * st.atr_at_entry
                    st.stop_set = True
                    log(f"ENTERED {sym} entry={st.entry_price:.2f} "
                        f"atr@entry={st.atr_at_entry:.4f} stop={st.stop_level:.2f}")
                except Exception:
                    pass

            if st.last_bar_ts is not None and newest <= st.last_bar_ts:
                continue  # no new closed bar for this symbol
            st.last_bar_ts = newest

            sig = compute_signals(df)
            if sig is None:
                continue

            # Stage 1: arm on RSI cross-up (spec 5.2)
            if sig["rsi_cross_up"]:
                st.armed_bars_left = P.armed_window
                log(f"ARMED  {sym} rsi={sig['rsi']:.1f} window={P.armed_window}")
            is_armed = st.armed_bars_left > 0

            held_here = pf.pairs[i].symbol == sym

            # Exit logic for a held symbol (slope decay) -- stop/EOD handled above
            if held_here and sig["sell_now"]:
                log(f"SELL   {sym} sl6={sig['sl6']:.5f} "
                    f"thresh={P.sell_thresh_deg * P.slope_per_degree:.5f}")
                pf.on_exit(sym, "slope-decay")
                st.armed_bars_left = max(0, st.armed_bars_left - 1)
                continue

            # Stage 2: fire (spec 5.2) -- only if not already holding this side
            fire = (is_armed and sig["turn_up"] and sig["above_hma"]
                    and sig["black_ok"] and sig["chop_ok"]
                    and not no_new_entries and not held_here)

            if is_armed and sig["turn_up"]:
                log(f"EVAL   {sym} turnUp={sig['turn_up']} "
                    f"aboveHMA={sig['above_hma']} black={sig['black_ok']} "
                    f"chop={sig['chop_ok']} flips={sig['flips']} "
                    f"sl3={sig['sl3']:.5f} sl6={sig['sl6']:.5f} "
                    f"sl10={sig['sl10']:.5f} sl12={sig['sl12']:.5f}")

            if fire:
                pf.on_fire(sym, sig["price"], sig["atr"])
                st.armed_bars_left = 0
            elif st.armed_bars_left > 0:
                st.armed_bars_left -= 1

        time.sleep(P.poll_seconds)

    log("STOPPED.")


if __name__ == "__main__":
    main()
