
#!/usr/bin/env python3
# =============================================================================
#  RSI SYSTEM  --  persistent arm->fire signals + Two Step execution
#  Innovative Investment Research -- TradingSystem
#  TradeStation WebAPI v3 (OAuth refresh-token)  |  Lightsail / tmux
#
#  WHAT THIS IS
#  ------------
#  Three layers, from three places:
#
#    SIGNALS    RSI system (rsiAlgo.py), ported verbatim in behavior:
#               arm on RSI cross-up, fire when the black line (EMA20) has
#               risen FIRE_DISTANCE% off its low since the arm. PERSISTENT --
#               once the rise is achieved inside the window, it fires. Not a
#               single-bar inflection.
#    EXECUTION  Two Step: pair mutual-exclusion, portfolio slots,
#               stop-and-reverse, ATR hard stop, EOD flatten.
#    PLUMBING   Algo 1 v5: TradeStationClient (OAuth + WebAPI v3).
#
#  REMOVED vs. the previous merge (deliberately):
#    * black-line slope-degree blocker (sl6/sl10, block_thresh_deg)
#    * chop filter (HMA flips + swing amplitude)
#    * HMA entirely -- nothing uses it now
#    * slope_per_degree -- the uncalibrated placeholder is gone from the
#      entry path completely. Nothing in the default config depends on it.
#    * single-bar turn_up inflection -- replaced by the persistent rise
#
#  Bars are 1-MINUTE.
#
#  SAFETY
#  ------
#   * TS_ENV   = "sim" (default) or "live".
#   * DRY_RUN  = "1" (default). Logs FIRE/EXIT but sends nothing.
#     With the filters stripped this system fires considerably more often
#     than the previous build. Watch a full sim session before DRY_RUN=0.
#
#  Run (tmux on Lightsail, venv active):
#      source ~/algotrend1v5/venv/bin/activate
#      set -a; source .env; set +a
#      python3 rsi_system_ts.py
# =============================================================================

import os
import sys
import time
import signal as _sig
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# ------------------------------------------------------------------ config ---

AZ = ZoneInfo("America/Phoenix")     # log/display timezone
ET = ZoneInfo("America/New_York")    # session gating (exchange time)

# Each entry is a GROUP that can hold at most one position at a time.
#   ("LONG", "INVERSE")  -- mutually exclusive; firing one reverses the other
#   ("SOLO",)            -- no inverse counterpart; just occupies a slot
#
# One group == one slot. 10 groups here, so the slot ceiling is 10.
#
# !! VERIFY BEFORE TRADING !!  The pairings marked below are inferred from
# the ticker naming, not from anything authoritative. A wrong pairing means
# the engine will "reverse" into a security that is not actually the inverse.
# Run the symbol check (see chat) before this goes anywhere near live.
PAIRS = [
    ("ASTX", "ASTZ"),      # X/Z convention
    ("MSTU", "MSTZ"),      # carried over, previously in use
    ("NBIL", "NBIZ"),      # carried over, previously in use
    ("RKLX", "RKLZ"),      # X/Z convention
    ("SOXL", "SOXS"),      # Direxion semis bull/bear
    ("CWVX", "CORD"),      # INFERRED -- confirm CORD is CWVX's counterpart
    ("SNXX", "SNDR"),      # INFERRED -- confirm these are actually a pair
    ("AAOX",),             # unpaired -- no counterpart given
    ("LITX",),             # unpaired -- LITZ not in your list
    ("BEX",),              # unpaired -- no counterpart given
]

# --- derived; handles 1- and 2-symbol groups -------------------------------
SYMBOLS = [s for group in PAIRS for s in group]
PAIR_OF = {s: i for i, group in enumerate(PAIRS) for s in group}
SIDE_OF = {}
for _g in PAIRS:
    SIDE_OF[_g[0]] = "LONG"
    if len(_g) > 1:
        SIDE_OF[_g[1]] = "INVERSE"

_dupes = [s for s in set(SYMBOLS) if SYMBOLS.count(s) > 1]
if _dupes:
    raise SystemExit(f"FATAL: duplicate symbols in PAIRS: {sorted(_dupes)}")


def group_label(i):
    """'ASTX/ASTZ' or 'AAOX' -- for logs."""
    return "/".join(PAIRS[i])


@dataclass
class Params:
    # ---- signal layer (RSI system) --------------------------------------
    rsi_period: int = 7
    rsi_arm_level: float = 40.0    # 40 = shake-out/plumbing test. 35 for real.
    ema_period: int = 20           # the black line
    arm_window: int = 25           # bars; arm expires if no fire within this
    fire_distance_pct: float = 0.4 # % the black line must rise off its low

    # trend gate -- also black-line based. OFF by default: on 1-min bars it
    # fights the fire condition. RSI arms on a dip, and 30 bars later the dip
    # is still inside the lookback, so the trend reads negative exactly when
    # you're arming. Turn it back on only after you've seen the raw signal
    # rate in sim, and if you do, start near 0.0 ("black line not falling")
    # rather than 1.0.
    use_trend_gate: bool = False
    trend_gate_pct: float = 1.0    # % the black line must have risen...
    trend_lookback: int = 30       # ...over this many bars (still computed
                                   #    for the status board either way)

    # ---- execution layer (Two Step) -------------------------------------
    atr_period: int = 14
    stop_mult_atr: float = 1.5
    money_per_position: float = 10_000.0
    fixed_shares: int = 1          # >0 overrides money sizing (1 = live toe-dip)
    # NOTE: slots count GROUPS, not symbols. 10 groups here -> ceiling 10.
    # 25 concurrent would need 25 groups in PAIRS.
    max_slots: int = 10

    # Hard stop is 1.5xATR by default. On a quiet tape ATR can be tiny and the
    # stop lands a few cents away; on a wild one it can land 10% out. This
    # clamps it into a band you can reason about. 0 disables either end.
    stop_min_pct: float = 1.0      # never tighter than this % below entry
    stop_max_pct: float = 4.0      # never wider than this % below entry

    # ---- conservative auto-exits (backstops to the cyborg sell) ---------
    # These exist so a position is never fully unmanaged. Tuned to fire
    # RARELY -- if one trips before you sell by eye, it cost you the trade.
    use_trail: bool = True
    trail_arm_pct: float = 1.0     # don't trail until up this much from entry
    trail_giveback_pct: float = 2.0  # then exit on this much off the peak

    use_time_stop: bool = True
    time_stop_bars: int = 90       # bars held...
    time_stop_only_if_losing: bool = True   # ...and only if flat-or-down

    # ---- broker-side protective stop ------------------------------------
    # The ONLY exit that survives this process dying. Resting GTC stop order
    # placed at the broker on entry, cancelled on any other exit.
    use_broker_stop: bool = True

    # exit rule: "stop_eod" = hard stop + EOD flatten only, trader works the
    # profit exit by hand (the RSI system's cyborg design).
    # "slope_decay" = Two Step's automated slope exit. Requires calibrating
    # slope_per_degree first; leave it off until you do.
    exit_mode: str = "stop_eod"
    sell_thresh_deg: float = 10.0
    sell_window: int = 6
    sell_consec_bars: int = 2
    slope_per_degree: float = 0.0025   # PLACEHOLDER -- only used if exit_mode="slope_decay"

    # ---- session (exchange time HH:MM) ----------------------------------
    market_open_et: str = "09:30"
    market_close_et: str = "16:00"
    no_entry_after_et: str = "15:45"
    flatten_at_et: str = "15:55"

    # ---- engine ----------------------------------------------------------
    bar_minutes: int = 1
    poll_seconds: int = 15
    bar_lookback: int = 400        # raised: ~half get dropped by the session
                                   # filter below, and we need 42 clean ones
    # Drop any bar whose timestamp falls outside 09:30-16:00 ET before it
    # reaches an indicator. rsiAlgo did this per-bar; the merge lost it and
    # was feeding overnight/pre-market prints straight into EMA/RSI/ATR.
    filter_bars_to_session: bool = True
    status_every_seconds: int = 60   # status board cadence; 0 = off
    resync_every_seconds: int = 60   # re-read broker positions (catches manual closes)


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
#  BROKER ADAPTER  --  TradeStation WebAPI v3   (from Algo 1 v5, unchanged)
# =============================================================================

def _to_utc_index(values):
    """Coerce TS bar timestamps to a tz-aware UTC DatetimeIndex.

    Why this is not a one-liner: pd.to_datetime(list, utc=True) infers ONE
    format from the first element and raises if a later element differs.
    TS normally sends '...Z' on every bar, but a single naive or offset-style
    stamp in the batch would take the whole engine down mid-session -- and a
    mixed naive/aware index goes object-dtype, which makes the
    `newest <= last_bar_ts` compare raise TypeError instead. Cascade instead.
    """
    for kw in ({"format": "ISO8601"}, {"format": "mixed"}, {}):
        try:
            return pd.DatetimeIndex(pd.to_datetime(values, utc=True, **kw))
        except (ValueError, TypeError):
            continue
    out = []
    for v in values:                      # last resort: element by element
        t = pd.Timestamp(v)
        out.append(t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC"))
    return pd.DatetimeIndex(out)


TS_OAUTH_URL = "https://signin.tradestation.com/oauth/token"
TS_BASE = {
    "live": "https://api.tradestation.com/v3",
    "sim":  "https://sim-api.tradestation.com/v3",
}


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
        # Several naming conventions accepted -- the .env already on the box
        # predates this script and uses TS_API_KEY / TS_SECRET.
        self.client_id = (os.getenv("TS_CLIENT_ID") or os.getenv("TS_API_KEY")
                          or os.getenv("TRADESTATION_CLIENT_ID"))
        self.client_secret = (os.getenv("TS_CLIENT_SECRET") or os.getenv("TS_SECRET")
                              or os.getenv("TRADESTATION_CLIENT_SECRET"))
        self.refresh_token = os.getenv("TS_REFRESH_TOKEN") or os.getenv("TRADESTATION_REFRESH_TOKEN")
        self.account_id = os.getenv("TS_ACCOUNT_ID") or os.getenv("TRADESTATION_ACCOUNT_ID")
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

    # ---- auth -----------------------------------------------------------
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

    # ---- market data ----------------------------------------------------
    def get_bars(self, symbol, tf=None, start=None, limit=None, feed=None):
        params = {
            "interval": P.bar_minutes,
            "unit": "Minute",
            "barsback": limit or P.bar_lookback,
        }
        data = self._get(f"/marketdata/barcharts/{symbol}", params)
        bars = data.get("Bars", [])
        if not bars:
            return _Bars(None)
        rows, raw_ts = [], []
        for b in bars:
            raw_ts.append(b["TimeStamp"])
            rows.append({
                "open":   float(b["Open"]),
                "high":   float(b["High"]),
                "low":    float(b["Low"]),
                "close":  float(b["Close"]),
                "volume": float(b.get("TotalVolume", 0) or 0),
            })
        df = pd.DataFrame(rows, index=_to_utc_index(raw_ts)).sort_index()
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
        """Resolve the account for THIS environment.

        sim and live have different account numbers, so a single
        TS_ACCOUNT_ID in .env cannot serve both. Previously a mismatch fell
        back silently here while list_positions() kept using the raw .env
        value -- startup logged a plausible account and every positions call
        403'd. Adopt the resolved id so the rest of the client follows."""
        data = self._get("/brokerage/accounts")
        accts = data.get("Accounts", [])
        me = next((a for a in accts if a.get("AccountID") == self.account_id), None)
        if me is None and accts:
            # prefer a Margin account -- equities do not live in Futures
            me = next((a for a in accts
                       if str(a.get("AccountType", "")).lower() == "margin"), accts[0])
            log(f"WARN TS_ACCOUNT_ID={self.account_id!r} not found in "
                f"env={self.env}. Using {me.get('AccountID')} "
                f"(type={me.get('AccountType')}) instead. Fix .env to silence this.")
            self.account_id = me.get("AccountID")
        me = me or {}
        return _Account(me.get("AccountID", self.account_id),
                        me.get("Status", "UNKNOWN"))

    def get_balance(self):
        """Cash/equity for the resolved account -- used to sanity-check that
        you are pointed at the account you think you are."""
        try:
            data = self._get(f"/brokerage/accounts/{self.account_id}/balances")
            b = (data.get("Balances") or [{}])[0]
            return {
                "equity": float(b.get("Equity", 0) or 0),
                "cash": float(b.get("CashBalance", 0) or 0),
                "buying_power": float(b.get("BuyingPower", 0) or 0),
            }
        except Exception as e:
            log(f"WARN could not read balances: {e}")
            return None

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

    def close_position(self, symbol, qty=None):
        """Market sell to flat.

        In dry-run there is no broker position to look up, so we take the qty
        the engine thinks it holds. Previously this called get_position(),
        which raised in dry-run -- the exception surfaced as ORDER-ERR and the
        exit never completed. That is why no exit path had ever run."""
        if self.dry_run:
            log(f"DRY-RUN order suppressed: SELL {qty or 0} {symbol}")
            return {"dry_run": True}
        pos = self.get_position(symbol)
        if pos.qty <= 0:
            return {"noop": True}
        return self._order(symbol, pos.qty, "SELL")

    # ---- resting protective stop (survives this process dying) ----------
    def submit_stop_order(self, symbol, qty, stop_price):
        """GTC StopMarket SELL resting at the broker.

        This is the only exit that still exists if the box reboots, the
        network drops, or python dies. Returns the order id, or None."""
        body = {
            "AccountID": self.account_id,
            "Symbol": symbol,
            "Quantity": str(int(qty)),
            "OrderType": "StopMarket",
            "StopPrice": f"{stop_price:.2f}",
            "TradeAction": "SELL",
            "TimeInForce": {"Duration": "GTC"},
            "Route": "Intelligent",
        }
        if self.dry_run:
            log(f"DRY-RUN broker stop suppressed: SELL {qty} {symbol} "
                f"stop {stop_price:.2f} GTC")
            return None
        resp = self._post("/orderexecution/orders", body)
        oid = None
        for o in (resp.get("Orders") or []):
            oid = o.get("OrderID") or o.get("OrderId")
            if oid:
                break
        return oid or resp.get("OrderID") or resp.get("OrderId")

    def cancel_order(self, order_id):
        """Cancel a resting order. Must be called on every non-stop exit or
        the orphan will sell you short on a later rally."""
        if self.dry_run or not order_id:
            return {"dry_run": True}
        r = self._session.delete(f"{self.base}/orderexecution/orders/{order_id}",
                                 headers=self._headers(), timeout=20)
        r.raise_for_status()
        return r.json() if r.content else {}


# ------------------------------------------------------------ stops ---------

def clamp_stop(entry_price, atr_val):
    """Hard stop = entry - stop_mult_atr*ATR, clamped into a % band.

    Raw ATR stops are unusable across a mixed universe: on a quiet 1-min tape
    1.5xATR can be 0.15% away (you get stopped on noise), and on a leveraged
    name in a flush it can be 9% away (the 'stop' is theatre). The band makes
    one setting behave sanely on both."""
    stop = entry_price - P.stop_mult_atr * float(atr_val or 0.0)
    if P.stop_max_pct:                      # not wider than X% below entry
        stop = max(stop, entry_price * (1 - P.stop_max_pct / 100.0))
    if P.stop_min_pct:                      # not tighter than X% below entry
        stop = min(stop, entry_price * (1 - P.stop_min_pct / 100.0))
    return stop


# ------------------------------------------------------------ state ----------

@dataclass
class SymbolState:
    # --- arm/fire state machine (RSI system) ---
    armed: bool = False
    bars_since_arm: int = 0
    black_low_since_arm: float = None
    # --- bar bookkeeping ---
    last_bar_ts: pd.Timestamp = None
    # --- status board fields ---
    last_rsi: float = None
    last_trend: float = None       # black % over trend_lookback bars
    last_rose: float = None        # black % off its low since arm
    bars_seen: int = 0
    # --- position bookkeeping (Two Step) ---
    atr_at_entry: float = 0.0
    stop_level: float = 0.0
    entry_price: float = 0.0
    qty: int = 0
    stop_set: bool = False
    # entry_price came from the signal bar, not a confirmed fill. Stays True
    # for the whole trade in dry-run (there is no fill to confirm against).
    provisional_entry: bool = False
    # --- conservative exit bookkeeping ---
    peak_price: float = 0.0        # high-water mark since entry
    trail_armed: bool = False      # gain cleared trail_arm_pct at least once
    bars_held: int = 0
    stop_order_id: str = None      # resting broker-side stop, if any


@dataclass
class PairState:
    state: str = "FLAT"      # FLAT | LONG | INVERSE
    symbol: str = None


class Portfolio:
    """Slots + pair mutual-exclusion + stop-and-reverse.  (Two Step)"""

    def __init__(self, api, sym_state):
        self.api = api
        self.sym = sym_state
        self.pairs = [PairState() for _ in PAIRS]

    def slots_used(self):
        return sum(1 for p in self.pairs if p.state != "FLAT")

    def sync_from_broker(self, quiet=False):
        """Reconcile internal state to actual TradeStation positions.
           Run at startup AND periodically -- this is what catches a manual
           close the trader did by hand (replaces rsiAlgo's mark_flat)."""
        # In dry-run no order was ever sent, so the broker reports nothing and
        # this would mark every pair FLAT -- silently deleting the simulated
        # positions every resync_every_seconds. Until now the 403 on the
        # positions endpoint was the only thing preventing that. Skip it.
        if self.api.dry_run:
            if not quiet:
                log("SYNC skipped (dry_run): broker has no simulated positions "
                    "to reconcile against")
            return
        try:
            live = {p.symbol: p for p in self.api.list_positions()}
        except Exception as e:
            log(f"WARN could not list positions: {e}")
            return
        for i, group in enumerate(PAIRS):
            long_s = group[0]
            inv_s = group[1] if len(group) > 1 else None
            if long_s in live:
                new = PairState("LONG", long_s)
            elif inv_s and inv_s in live:
                new = PairState("INVERSE", inv_s)
            else:
                new = PairState("FLAT", None)
            # detect a position that vanished on us (manual close / stop filled)
            old = self.pairs[i]
            if old.state != "FLAT" and new.state == "FLAT":
                log(f"RESYNC {old.symbol} no longer held -- pair released, "
                    f"engine will watch it again")
                st = self.sym.get(old.symbol)
                if st:
                    st.stop_set = False
                    st.stop_level = 0.0
                    st.qty = 0
                    st.entry_price = 0.0
            self.pairs[i] = new
        for sym, pos in live.items():
            if sym not in self.sym:
                continue
            st = self.sym[sym]
            st.entry_price = float(pos.avg_entry_price)
            st.qty = abs(int(float(pos.qty)))
        if not quiet:
            log(f"SYNC slots_used={self.slots_used()} pairs="
                + (", ".join(f"{group_label(i)}:{p.state}"
                             for i, p in enumerate(self.pairs) if p.state != "FLAT")
                   or "none"))

    def _submit_buy(self, symbol, price, atr_val):
        if P.fixed_shares > 0:
            qty = P.fixed_shares
        else:
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

        # --- entry price and stop are set NOW, from the signal bar. -------
        # Previously both waited on api.get_position(), which never returns
        # in dry-run and 403s in sim -- so entry_price stayed 0.0, stop_level
        # stayed 0.0, stop_set stayed False, and the hard stop was dead code.
        # The broker fill (if any) refines this later; see main().
        st.entry_price = float(price)
        st.stop_level = clamp_stop(st.entry_price, atr_val)
        st.stop_set = True
        st.provisional_entry = True
        st.peak_price = float(price)
        st.trail_armed = False
        st.bars_held = 0
        st.stop_order_id = None

        log(f"FIRE   {symbol} buy qty={qty} in @ {st.entry_price:.2f} "
            f"stop {st.stop_level:.2f} atr={atr_val:.4f}")

        # --- resting protective stop at the broker -----------------------
        if P.use_broker_stop:
            try:
                st.stop_order_id = self.api.submit_stop_order(
                    symbol, qty, st.stop_level)
                if st.stop_order_id:
                    log(f"BSTOP  {symbol} resting GTC stop {st.stop_level:.2f} "
                        f"id={st.stop_order_id}")
            except Exception as e:
                log(f"BSTOP-ERR {symbol}: {e} -- position has NO broker-side "
                    f"protection, client-side stop only")
        return True

    def _close(self, symbol, reason):
        st = self.sym[symbol]

        # Cancel the resting stop FIRST. If the market sell fills and the GTC
        # stop is still live, it stays working and will short you on a rally.
        if st.stop_order_id:
            try:
                self.api.cancel_order(st.stop_order_id)
                log(f"BSTOP  {symbol} resting stop cancelled "
                    f"(id={st.stop_order_id})")
            except Exception as e:
                log(f"BSTOP-ERR {symbol} cancel failed: {e} -- CHECK FOR AN "
                    f"ORPHANED STOP ORDER AT THE BROKER")
            st.stop_order_id = None

        try:
            self.api.close_position(symbol, qty=st.qty)
        except Exception as e:
            log(f"ORDER-ERR close {symbol}: {e}")
            return

        pnl_txt = ""
        if st.entry_price and st.peak_price:
            pnl_txt = (f" entry={st.entry_price:.2f} peak={st.peak_price:.2f} "
                       f"bars={st.bars_held}")
        st.stop_level = 0.0
        st.stop_set = False
        st.qty = 0
        st.provisional_entry = False
        st.peak_price = 0.0
        st.trail_armed = False
        st.bars_held = 0
        log(f"EXIT   {symbol} ({reason}){pnl_txt}")

    def on_fire(self, symbol, price, atr_val):
        i = PAIR_OF[symbol]
        pair = self.pairs[i]
        side = SIDE_OF[symbol]
        if pair.state != "FLAT" and pair.symbol == symbol:
            return
        if pair.state == "FLAT":
            if self.slots_used() >= P.max_slots:
                log(f"SLOT-SKIP {symbol} (slots full {P.max_slots})")
                return
            if self._submit_buy(symbol, price, atr_val):
                self.pairs[i] = PairState(side, symbol)
        else:
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


# ------------------------------------------------------------ indicators ----

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
    """Only used when exit_mode='slope_decay'."""
    x = np.arange(n)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    def _slope(y):
        return ((x - xm) * (y - y.mean())).sum() / denom
    return s.rolling(n).apply(_slope, raw=True)


# ------------------------------------------------------------ signals -------

def bar_et(ts):
    """Bar timestamp as ET HH:MM -- what you'd see on a TradeStation chart."""
    try:
        return pd.Timestamp(ts).tz_convert(ET).strftime("%H:%M")
    except Exception:
        return str(ts)


def session_filter(df):
    """Keep only bars inside 09:30-16:00 ET, on weekdays.

    Filters by TIME OF DAY, not by date, so prior sessions' bars survive --
    that's what lets the indicators be warm at 09:31 instead of waiting until
    ~10:12 for 42 fresh bars. Same behavior as rsiAlgo's rolling deque.

    NOTE: TradeStation's bar TimeStamp is the END of the bar period, so the
    boundary is inclusive on both ends and a 09:30-stamped bar is the last
    pre-market minute. One bar of EMA20 contamination per day; if you'd
    rather be strict, change inclusive to "right".
    """
    if df is None or df.empty:
        return df
    et = df.tz_convert(ET)
    try:
        et = et.between_time(P.market_open_et, P.market_close_et, inclusive="both")
    except TypeError:   # pandas < 1.4
        et = et.between_time(P.market_open_et, P.market_close_et)
    et = et[et.index.weekday < 5]
    return et.tz_convert("UTC")


def fetch_bars(api, symbol, feed=None):
    bars = api.get_bars(symbol, start=None, limit=P.bar_lookback, feed=feed)
    df = bars.df
    if df is None or df.empty:
        return None
    if df.index.tz is None:                 # belt and braces
        df = df.tz_localize("UTC")
    if P.filter_bars_to_session:
        df = session_filter(df)
    if df is None or df.empty:
        return None
    return df[["open", "high", "low", "close", "volume"]].copy()


WARMUP = max(20, 0)  # recomputed below once P exists in full


def bars_needed():
    return max(P.ema_period, P.atr_period, P.trend_lookback) + P.rsi_period + 5


def compute_signals(df):
    """Per-bar values for the LAST CLOSED bar, or None if not enough history.

    Deliberately thin: RSI, the black line (EMA20), its trend, and ATR for
    the stop. No HMA, no slope-degree blocker, no chop filter."""
    if len(df) < bars_needed():
        return None

    c = df["close"]
    rsi = rsi_wilder(c, P.rsi_period)
    blk = ema(c, P.ema_period)
    a = atr_wilder(df, P.atr_period)

    atr_now = float(a.iloc[-1])
    if atr_now <= 0:
        return None

    black = float(blk.iloc[-1])
    past = float(blk.iloc[-1 - P.trend_lookback])
    trend_pct = (black - past) / past * 100 if past else 0.0

    sig = {
        "ts": df.index[-1],
        "price": float(c.iloc[-1]),
        "atr": atr_now,
        "rsi": float(rsi.iloc[-1]),
        "rsi_prev": float(rsi.iloc[-2]),
        "black": black,
        "trend_pct": trend_pct,
        "sell_now": False,
    }

    if P.exit_mode == "slope_decay":
        s6 = linreg_slope(blk, P.sell_window) / atr_now
        sell_t = P.sell_thresh_deg * P.slope_per_degree
        sig["sell_now"] = bool((s6.iloc[-P.sell_consec_bars:] <= sell_t).all())
        sig["sl6"] = float(s6.iloc[-1])

    return sig


# ------------------------------------------------------------ status --------

def status_board(sym_state, pf, current_prices=None):
    """What every ticker is doing right now.  (from rsiAlgo)
         LONG      = held; work the sell by eye (unless exit_mode=slope_decay)
         ARMED     = RSI crossed up; watching the black line rise
         watch     = has data, waiting for an arm
         (warming) = not enough bars yet
    """
    lines = ["", "===== STATUS BOARD =====",
             "%-8s %-9s %6s %8s %8s  %s" % ("ticker", "state", "RSI", "trend%", "rose%", "note")]
    held = {p.symbol for p in pf.pairs if p.state != "FLAT"}
    for sym in SYMBOLS:
        st = sym_state[sym]
        rsi = "  -  " if st.last_rsi is None else "%5.1f" % st.last_rsi
        trd = "   -  " if st.last_trend is None else "%+5.1f" % st.last_trend
        rse = "   -  " if st.last_rose is None else "%+5.2f" % st.last_rose

        if st.bars_seen < bars_needed():
            state, note = "(warming)", "collecting bars (%d/%d)" % (st.bars_seen, bars_needed())
        elif sym in held:
            state = "LONG"
            if current_prices and sym in current_prices and st.entry_price:
                pnl = (current_prices[sym] / st.entry_price - 1) * 100
                note = "in @ %.2f  now %+.1f%%  stop %.2f" % (
                    st.entry_price, pnl, st.stop_level)
            else:
                note = "in @ %.2f  stop %.2f" % (st.entry_price or 0, st.stop_level)
            if st.provisional_entry:
                note += "*"          # * = signal-bar price, fill unconfirmed
            if P.use_trail and st.peak_price:
                if st.trail_armed and current_prices and sym in current_prices:
                    give = (st.peak_price - current_prices[sym]) / st.peak_price * 100
                    note += "  peak %.2f (-%.2f%%/%.1f%%)" % (
                        st.peak_price, give, P.trail_giveback_pct)
                else:
                    note += "  peak %.2f (trail@+%.1f%%)" % (
                        st.peak_price, P.trail_arm_pct)
            if P.use_time_stop:
                note += "  %db/%db" % (st.bars_held, P.time_stop_bars)
            if st.stop_order_id:
                note += "  [bstop]"
            if P.exit_mode == "stop_eod":
                note += "  >>> CYBORG SELL by eye"
        elif st.armed:
            state = "ARMED"
            note = "need black +%.2f%% off low (bar %d/%d)" % (
                P.fire_distance_pct, st.bars_since_arm, P.arm_window)
        else:
            state, note = "watch", "waiting for RSI to cross up thru %.0f" % P.rsi_arm_level
        lines.append("%-8s %-9s %6s %8s %8s  %s" % (sym, state, rsi, trd, rse, note))
    lines.append("slots %d/%d   dry_run=%s" % (pf.slots_used(), P.max_slots,
                                               pf.api.dry_run))
    lines.append("========================")
    log("\n".join(lines))


# ------------------------------------------------------------ session -------

def _hhmm(s):
    hh, mm = s.split(":")
    return int(hh), int(mm)


def et_now():
    return datetime.now(ET)


def in_session(now_et):
    oh, om = _hhmm(P.market_open_et)
    ch, cm = _hhmm(P.market_close_et)
    o = now_et.replace(hour=oh, minute=om, second=0, microsecond=0)
    c = now_et.replace(hour=ch, minute=cm, second=0, microsecond=0)
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
    api = TradeStationClient()

    # ---- clock diagnostic: if anything is off, it shows up right here ----
    _u = datetime.now(timezone.utc)
    log("CLOCK  utc=%s | et=%s | az=%s | host_tz=%s"
        % (_u.strftime("%Y-%m-%d %H:%M:%S"),
           _u.astimezone(ET).strftime("%H:%M:%S %Z"),
           _u.astimezone(AZ).strftime("%H:%M:%S %Z"),
           "/".join(time.tzname)))
    log("       session gate uses ET. Bars are stamped UTC by TS and "
        "filtered to %s-%s ET before any indicator sees them (filter=%s)."
        % (P.market_open_et, P.market_close_et, P.filter_bars_to_session))

    acct = api.get_account()
    log(f"START RSI System [persistent fire] [TradeStation/{api.env}] "
        f"account={acct.account_number} status={acct.status} "
        f"dry_run={api.dry_run} bars={P.bar_minutes}min "
        f"symbols={len(SYMBOLS)} | log={_log_path}")
    bal = api.get_balance()
    if bal:
        log(f"BALANCE equity=${bal['equity']:,.2f} cash=${bal['cash']:,.2f} "
            f"buying_power=${bal['buying_power']:,.2f}")
        if api.env == "live" and not api.dry_run:
            log("       ^ CHECK THIS NUMBER. If it is not the account you "
                "meant to trade, Ctrl-C now.")
    log(f"SIGNAL arm=RSI{P.rsi_period} cross up thru {P.rsi_arm_level:.0f} | "
        f"fire=black(EMA{P.ema_period}) +{P.fire_distance_pct}% off low "
        f"within {P.arm_window} bars | "
        f"trend_gate={'+%.1f%%/%db' % (P.trend_gate_pct, P.trend_lookback) if P.use_trend_gate else 'OFF'}")
    log(f"EXEC   exit_mode={P.exit_mode} stop={P.stop_mult_atr}xATR "
        f"clamped {P.stop_min_pct}-{P.stop_max_pct}% "
        f"slots={P.max_slots} "
        f"size={'%d sh fixed' % P.fixed_shares if P.fixed_shares > 0 else '$%.0f' % P.money_per_position}")
    log(f"EXITS  trail={'-%.1f%% off peak, arms at +%.1f%%' % (P.trail_giveback_pct, P.trail_arm_pct) if P.use_trail else 'OFF'} | "
        f"time={'%db%s' % (P.time_stop_bars, ' if losing' if P.time_stop_only_if_losing else '') if P.use_time_stop else 'OFF'} | "
        f"broker_stop={'GTC at broker' if P.use_broker_stop else 'OFF'}")
    if P.max_slots > len(PAIRS):
        log(f"WARN max_slots={P.max_slots} but only {len(PAIRS)} pairs exist -- "
            f"the real ceiling is {len(PAIRS)}. Add pairs to PAIRS to raise it.")
    if P.use_broker_stop and api.dry_run:
        log("NOTE dry_run: no resting broker stop is placed. The exit that "
            "survives a crash is the one thing dry-run cannot test.")
    if api.env == "live" and not api.dry_run:
        log("*** LIVE TRADING ENABLED -- real orders will be sent ***")
    if P.exit_mode == "slope_decay":
        log("WARN exit_mode=slope_decay uses slope_per_degree, which is a "
            "PLACEHOLDER (0.0025). Calibrate before trusting the exits.")

    sym_state = {s: SymbolState() for s in SYMBOLS}
    pf = Portfolio(api, sym_state)
    pf.sync_from_broker()

    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)

    last_status = 0.0
    last_resync = time.time()

    while _RUNNING:
        now_et = et_now()

        if not in_session(now_et):
            time.sleep(P.poll_seconds)
            continue

        # ---- EOD flatten: highest priority ------------------------------
        if past(now_et, P.flatten_at_et):
            for sym in list(pf.held_symbols()):
                pf.on_exit(sym, "EOD-flatten")
            time.sleep(P.poll_seconds)
            continue

        # ---- periodic broker resync (catches manual closes) -------------
        if P.resync_every_seconds and time.time() - last_resync >= P.resync_every_seconds:
            pf.sync_from_broker(quiet=True)
            last_resync = time.time()

        # ---- fast hard-stop check between bars --------------------------
        live_px = {}
        for sym in list(pf.held_symbols()):
            st = sym_state[sym]
            if not st.stop_set:
                continue
            try:
                last = float(api.get_latest_trade(sym).price)
            except Exception:
                continue
            live_px[sym] = last

            # high-water mark, updated between bars so a spike counts
            if last > st.peak_price:
                st.peak_price = last

            # ---- hard stop (client side; broker also holds a resting one) --
            if last <= st.stop_level:
                log(f"STOP   {sym} px={last:.2f} <= stop={st.stop_level:.2f}")
                pf.on_exit(sym, "hard-stop")
                continue

            # ---- trailing giveback from the peak --------------------------
            # Conservative by design: dormant until the trade has proved
            # itself by trail_arm_pct, then only acts after a real reversal.
            # It should almost never beat you to the cyborg sell.
            if P.use_trail and st.entry_price:
                gain = (last - st.entry_price) / st.entry_price * 100.0
                if not st.trail_armed and gain >= P.trail_arm_pct:
                    st.trail_armed = True
                    log(f"TRAIL  {sym} armed at {gain:+.2f}% "
                        f"(gives back {P.trail_giveback_pct}% off peak)")
                if st.trail_armed and st.peak_price > 0:
                    give = (st.peak_price - last) / st.peak_price * 100.0
                    if give >= P.trail_giveback_pct:
                        log(f"TRAIL  {sym} px={last:.2f} is -{give:.2f}% off "
                            f"peak {st.peak_price:.2f}")
                        pf.on_exit(sym, "trail-giveback")
                        continue

        no_new_entries = past(now_et, P.no_entry_after_et)

        # ---- per-symbol bar evaluation ----------------------------------
        for sym in SYMBOLS:
            try:
                df = fetch_bars(api, sym)
            except Exception as e:
                log(f"DATA-ERR {sym}: {e}")
                continue
            if df is None:
                continue

            st = sym_state[sym]
            st.bars_seen = len(df)
            newest = df.index[-1]
            i = PAIR_OF[sym]
            held_here = pf.pairs[i].symbol == sym

            # ---- refine the provisional entry with the real fill ---------
            # entry_price/stop_level are already set from the signal bar, so
            # this only upgrades them. If it fails (dry-run, or the 403 on the
            # positions endpoint) the provisional values stand and the stop
            # still works -- unlike before, where failure here left the stop
            # at 0.00 forever. The failure is now logged, not swallowed.
            if held_here and st.provisional_entry and not api.dry_run:
                try:
                    pos = api.get_position(sym)
                    real = float(pos.avg_entry_price)
                    if real > 0:
                        drift = abs(real - st.entry_price) / st.entry_price * 100
                        st.entry_price = real
                        st.stop_level = clamp_stop(real, st.atr_at_entry)
                        st.peak_price = max(st.peak_price, real)
                        st.provisional_entry = False
                        log(f"ENTERED {sym} fill={real:.2f} "
                            f"(signal px drifted {drift:.2f}%) "
                            f"stop={st.stop_level:.2f}")
                        # move the resting stop to match the real fill
                        if P.use_broker_stop and st.stop_order_id:
                            try:
                                api.cancel_order(st.stop_order_id)
                                st.stop_order_id = api.submit_stop_order(
                                    sym, st.qty, st.stop_level)
                                log(f"BSTOP  {sym} re-placed at {st.stop_level:.2f} "
                                    f"id={st.stop_order_id}")
                            except Exception as e:
                                log(f"BSTOP-ERR {sym} re-place failed: {e}")
                except Exception as e:
                    log(f"FILL-WARN {sym}: {e} -- keeping provisional entry "
                        f"{st.entry_price:.2f}, stop {st.stop_level:.2f}")

            # one evaluation per closed bar
            if st.last_bar_ts is not None:
                try:
                    if newest <= st.last_bar_ts:
                        continue
                except TypeError:
                    # naive/aware mismatch -- shouldn't happen now, but never
                    # let it take the whole engine down mid-session
                    log(f"TZ-WARN {sym} bar ts mismatch "
                        f"({newest!r} vs {st.last_bar_ts!r}); resetting")
                    st.last_bar_ts = None
                    continue
            st.last_bar_ts = newest

            sig = compute_signals(df)
            if sig is None:
                continue

            # status-board fields update every bar, held or not
            st.last_rsi = sig["rsi"]
            st.last_trend = sig["trend_pct"]

            black = sig["black"]

            # ---- held: exit logic only ----------------------------------
            if held_here:
                st.armed = False
                st.last_rose = None
                st.bars_held += 1
                if sig["price"] > st.peak_price:
                    st.peak_price = sig["price"]

                # ---- time stop: the position that is neither working nor
                # losing enough to stop out. Only fires flat-or-down, so it
                # can never take a winner off you.
                if P.use_time_stop and st.bars_held > P.time_stop_bars:
                    losing = sig["price"] <= st.entry_price
                    if losing or not P.time_stop_only_if_losing:
                        pnl = ((sig["price"] / st.entry_price - 1) * 100
                               if st.entry_price else 0.0)
                        log(f"TIME   {sym} {st.bars_held} bars held, "
                            f"{pnl:+.2f}% -- no progress")
                        pf.on_exit(sym, "time-stop")
                        continue

                if P.exit_mode == "slope_decay" and sig["sell_now"]:
                    log(f"SELL   {sym} sl6={sig.get('sl6', 0):.5f}")
                    pf.on_exit(sym, "slope-decay")
                continue

            # ================= RSI SYSTEM ARM -> FIRE ====================
            # ---- ARM: RSI crosses UP through level ----
            if (not st.armed) and sig["rsi_prev"] < P.rsi_arm_level <= sig["rsi"]:
                st.armed = True
                st.bars_since_arm = 0
                st.black_low_since_arm = black
                st.last_rose = 0.0
                log(f"ARMED  {sym} [{bar_et(sig['ts'])} ET] "
                    f"rsi {sig['rsi_prev']:.1f}->{sig['rsi']:.1f} "
                    f"black={black:.4f} window={P.arm_window}b")
                continue

            if st.armed:
                st.bars_since_arm += 1
                st.black_low_since_arm = min(st.black_low_since_arm, black)

                if st.bars_since_arm > P.arm_window:
                    st.armed = False
                    st.last_rose = None
                    log(f"DISARM {sym} arm window expired ({P.arm_window}b, "
                        f"best rise never hit {P.fire_distance_pct}%)")
                    continue

                # ---- FIRE: black rose fire_distance% off its low since arm ----
                low = st.black_low_since_arm
                rose = (black - low) / low * 100 if low else 0.0
                st.last_rose = rose

                log(f"EVAL   {sym} [{bar_et(sig['ts'])} ET] "
                    f"bar {st.bars_since_arm}/{P.arm_window} "
                    f"rsi={sig['rsi']:.1f} black={black:.4f} low={low:.4f} "
                    f"rose={rose:+.2f}% (need +{P.fire_distance_pct}%) "
                    f"trend={sig['trend_pct']:+.2f}%")

                if rose >= P.fire_distance_pct:
                    st.armed = False        # arm is consumed either way
                    st.last_rose = None

                    if P.use_trend_gate and sig["trend_pct"] < P.trend_gate_pct:
                        log(f"SKIP   {sym} trend gate: {sig['trend_pct']:+.2f}% "
                            f"< {P.trend_gate_pct}% over {P.trend_lookback}b")
                        continue
                    if no_new_entries:
                        log(f"SKIP   {sym} past no-entry cutoff {P.no_entry_after_et} ET")
                        continue

                    pf.on_fire(sym, sig["price"], sig["atr"])

        # ---- status board -----------------------------------------------
        if P.status_every_seconds and time.time() - last_status >= P.status_every_seconds:
            status_board(sym_state, pf, live_px)
            last_status = time.time()

        time.sleep(P.poll_seconds)

    log("STOPPED.")


if __name__ == "__main__":
    main()
