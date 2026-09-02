
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
@@ -63,34 +26,28 @@
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
SIMUL_BARS     = 3
EMA_LEN        = 20
HMA_LEN        = 7
ANGLE_LOOKBACK = 5
STEEP_LOOKBACK = 30
WAS_STEEP      = -20.0
SHALLOWED      = -15.0
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
@@ -99,27 +56,26 @@
    ("IONX", "IONZ"),
    ("SNXX", "SNDQ"),
    ("CWVX", "CORD"),
    ("OKLL", "OKLS"),      # added
    ("SKUU", "SKDD"),      # added
    ("AAOX",),             # added -- singleton, no inverse leg
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
    # singletons have no partner; PARTNER.get(sym) will return None

_dupes = [s for s in set(SYMBOLS) if SYMBOLS.count(s) > 1]
if _dupes:
    raise SystemExit(f"FATAL: duplicate symbols in PAIRS: {sorted(_dupes)}")

BAR_MINUTES = 1
BAR_LOOKBACK = 400            # enough for warmup + a full session
BAR_LOOKBACK = 400
MARKET_OPEN_ET = "09:30"
MARKET_CLOSE_ET = "16:00"
NO_NEW_AFTER_ET = "15:58"     # need a next bar in the same session
NO_NEW_AFTER_ET = "15:58"
EOD_FLATTEN_ET = "15:59"
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or 20)
LOG_CSV = os.getenv("MOD2_LOG") or "rsi_red_line_mod2_log.csv"
@@ -136,14 +92,11 @@ def log(msg):
    print(f"{ts} | {msg}", flush=True)


# ------------------------------------------------------------ indicators -----
# ema and rsi_wilder are identical to the proven versions in rsiAlgo.py.
 
def ema(s: pd.Series, n: int) -> pd.Series:
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
def rsi_wilder(close, n):
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
@@ -153,40 +106,33 @@ def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    return (100 - 100 / (1 + rs)).fillna(100)


def wma(s: pd.Series, n: int) -> pd.Series:
def wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hma(s: pd.Series, n: int) -> pd.Series:
    """Hull MA: WMA(2*WMA(n/2) - WMA(n), sqrt(n)).
 
    The spec writes the HMA(7) as WMA(2*WMA(close,3) - WMA(close,7), 3):
    half = round(7/2) = 3 (wait: round(3.5)=4 in banker's rounding, but the
    spec pins half=3 and final=3 explicitly), so we use the spec's exact
    windows rather than the textbook rounding. Do not "fix" this to 4/2."""
def hma(s, n):
    half = 3
    root = 3
    return wma(2 * wma(s, half) - wma(s, n), root)


def black_angle(black: pd.Series) -> pd.Series:
    """Angle of the black line in MATH degrees, per spec section 3.
 
    pct_per_bar = (black[i]/black[i-5] - 1) * 100 / 5
    angle       = degrees(arctan(pct_per_bar))
    Calibration: 1% per bar == 45 degrees. These are not protractor degrees.
    Do not correct them."""
def black_angle(black, day_high_so_far, day_low_so_far):
    """Angle of the black line, calibrated to match Gary's REAL chart --
    11in wide x 4.5in tall, showing the full 9:30-4:00 trading day, with the
    y-axis scaled to that day's own high-to-low range so far. This is what
    WAS_STEEP and SHALLOWED were actually designed by eye to mean."""
    in_per_min = CHART_W_IN / FULL_DAY_MINUTES
    rng = (day_high_so_far - day_low_so_far).replace(0, np.nan)
    in_per_dollar = CHART_H_IN / rng
    prev = black.shift(ANGLE_LOOKBACK)
    pct_per_bar = (black / prev - 1.0) * 100.0 / ANGLE_LOOKBACK
    return np.degrees(np.arctan(pct_per_bar))
 
    delta = black - prev
    horiz_in = ANGLE_LOOKBACK * in_per_min
    vert_in = delta * in_per_dollar
    return np.degrees(np.arctan(vert_in / horiz_in))

# ------------------------------------------------------------ session --------

def session_filter(df):
    """Keep only weekday bars inside 09:30-16:00 ET. Filters by time-of-day so
    prior sessions keep the indicators warm at the open."""
    if df is None or df.empty:
        return df
    et = df.tz_convert(ET)
@@ -213,10 +159,6 @@ def _to_utc_index(values):
    return pd.DatetimeIndex(out)


# ------------------------------------------------------------ TS client ------
# Trimmed to what mod2 needs: auth, bars, quote, account/balance, market buy,
# market sell-to-flat. No ATR, no resting stop -- the spec forbids them.
 
@dataclass
class _Trade:
    price: float
@@ -373,11 +315,8 @@ def market_sell(self, symbol, qty):
        return self._post("/orderexecution/orders", body)


# ------------------------------------------------------------ signal ---------
 
@dataclass
class Frame:
    """Everything the signal needs for the latest closed bar."""
    ts: object
    close: float
    open_: float
@@ -388,24 +327,29 @@ class Frame:
    red_prev: float
    red_prev2: float
    angle_now: float
    angle_was: float     # min angle over the prior STEEP_LOOKBACK bars
    bar_index: int       # position in the current session (0-based)
    angle_was: float
    bar_index: int


def build_frame(df: pd.DataFrame) -> Frame:
def build_frame(df):
    close = df["close"]
    black = ema(close, EMA_LEN)
    red = hma(close, HMA_LEN)
    rsi = rsi_wilder(close, RSI_LEN)
    angle = black_angle(black)

    # session index: how many bars since this session's 09:30 ET open
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

    was = angle.iloc[-(STEEP_LOOKBACK + 1):-1]   # prior 30 bars, excl current
    was = angle.iloc[-(STEEP_LOOKBACK + 1):-1]
    return Frame(
        ts=df.index[-1],
        close=float(close.iloc[-1]),
@@ -422,64 +366,48 @@ def build_frame(df: pd.DataFrame) -> Frame:
    )


def arm_fires(fr: Frame) -> bool:
    """ARM event: RSI crosses up through 35. rsi[i-1] < 35 <= rsi[i]."""
def arm_fires(fr):
    return fr.rsi_prev < RSI_ARM <= fr.rsi_now


def black_gate_open(fr: Frame) -> bool:
    """Was steep in the last 30 bars, and has now shallowed above -15.
    No upper bound: flat and rising both qualify."""
def black_gate_open(fr):
    if math.isnan(fr.angle_was):
        return False
    return (fr.angle_was <= WAS_STEEP) and (fr.angle_now > SHALLOWED)


def red_rising(fr: Frame) -> bool:
    """Red line rising TWO bars in a row (param #3, CHANGED from 1 bar).
    red[i] > red[i-1] > red[i-2]."""
def red_rising(fr):
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
    armed_until: int = -1


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
 
    last_rsi_cross: int = -1
    last_black: int = -1
    last_red: int = -1

# ------------------------------------------------------------ CSV ------------

def ensure_csv():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerow(LOG_COLUMNS)


def append_alert(row: dict):
def append_alert(row):
    with open(LOG_CSV, "a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in LOG_COLUMNS])


# ------------------------------------------------------------ clock ----------
 
def et_now():
    return datetime.now(ET)

@@ -511,8 +439,6 @@ def _stop(*_):
    _RUNNING = False


# ------------------------------------------------------------ main -----------
 
def main():
    _sig.signal(_sig.SIGINT, _stop)
    _sig.signal(_sig.SIGTERM, _stop)
@@ -521,270 +447,10 @@ def main():
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
    positions = {}
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
