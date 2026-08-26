
#!/usr/bin/env python3
"""
============================================================
  ENGINE A -- AUTO-BUY  (this is "SINGLE-CYBORG" mode)
  Buy happens AUTOMATICALLY the instant a signal fires.
  Sell always asks for your approval first.
  This is the one running live today, August 25th.
============================================================
 
RSI Mod2 -- Buy-Approval UI ("Cyborg" mode)
 
Runs the exact same signal-detection logic as the live rsi_red_line_mod2.py
script (same RSI/black/red conditions, same 3-bar alignment window), but
instead of buying automatically the moment a signal fires, it pops up a
single-button window on screen: "APPROVE BUY -- SYMBOL @ $price". Click it
and the real buy order goes to TradeStation immediately. Don't click it and
no trade happens.
 
Selling is UNCHANGED -- this script only touches the buy side. You still
close positions exactly the way you do today (the existing TradeStation/
TradingView position box).
 
VALIDATED CHANGE INCLUDED (2026-08-19 testing session):
    WAS_STEEP tightened from -20.0 to -30.0 -- requires a real, deeper
    decline before considering a dip valid. Tested against the full month
    of NBIL/SNXX data: kept all 9 known-good trades, blocked 14 of 34
    known-bad trades, at zero cost.
 
NOTHING ELSE about the signal logic was changed. RSI_LEN, RSI_ARM,
SIMUL_BARS, EMA_LEN, HMA_LEN, ANGLE_LOOKBACK, STEEP_LOOKBACK, SHALLOWED,
and WARMUP_BARS are all identical to the deployed system.
 
SAFETY: this reuses TradeStationClient from rsi_red_line_mod2.py, which
defaults to DRY_RUN=1 (orders are only logged, never actually sent) unless
your .env explicitly sets DRY_RUN=0. Leave DRY_RUN=1 while testing this
new approval window for the first few days, exactly like you'd want to
watch it work before trusting it with real trades.
 
HOW TO RUN:
    cd rsi_check
    python3 rsi_mod2_approve_ui.py
 
This needs to run on a computer that is on and connected during market
hours, same as the current live script.
"""
import os
import sys
import time
import math
import threading
import queue
import numpy as np
 
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rsi_red_line_mod2 as base
 
# ---- Validated parameter change from the 2026-08-19 testing session ----
base.WAS_STEEP = -30.0  # was -20.0
 
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image
 
POPUP_TIMEOUT_SECONDS = 600  # auto-dismiss an unapproved popup after 10 minutes
                              # (if you haven't clicked by then, the setup has
                              # likely already moved on -- adjust freely)
DOLLARS_PER_TRADE = 500      # dollars to put into EACH trade -- adjust to your sizing.
                              # The system converts this to a share count automatically
                              # at the actual price of each buy, since these tickers
                              # trade at very different prices from each other.
 
def shares_for_dollars(price):
    """How many whole shares $DOLLARS_PER_TRADE buys at this price.
    Always at least 1 share, even if the price is higher than the dollar amount."""
    if price <= 0:
        return 1
    return max(1, int(DOLLARS_PER_TRADE // price))
CHART_LOOKBACK_BARS = 90     # how many recent 1-min bars to show in the popup chart
 
# ---- Validated exit rule from the 2026-08-19 testing session ----
STOP_PCT = 2.0     # cut losses once down this much from entry -- widened from 1.0
                   # on 2026-08-24 after finding the 1% stop was getting blown
                   # through by normal 1-minute gaps on these volatile tickers
TRAIL_PCT = 2.5     # once in profit, exit if price pulls back this much from its peak
SELL_ALERT_COOLDOWN_SECONDS = 90  # if you skip a sell alert, wait this long before asking again
 
RSI_MOD2_MODE = "SINGLE_CYBORG"  # "SINGLE_CYBORG" = buy happens automatically (like
                                   # the original live system), only sell needs approval.
                                   # "DOUBLE_CYBORG" = both buy and sell need approval.
                                   # Set per Gary's request on 2026-08-23: double-cyborg
                                   # buy-approval wasn't working well in practice yet,
                                   # so single-cyborg (auto-buy, approve-sell) is the
                                   # mode to use until that's revisited.
 
# ---- OPENING TREND system (validated 2026-08-19 session) ----
# This is a SEPARATE, single-cyborg system: buys happen AUTOMATICALLY (too fast
# to approve by hand), but every exit still goes through the same sell-approval
# popup as the main RSI Mod2 system. Tested: 40deg angle over a 5-min lookback,
# checked ONLY in the first 15 minutes of the session, price within 1.5% of
# black -- 75 trades across 10 tickers/1 month, 39W/36L, total +10.17%.
#
# NOTE ON ANGLE CALCULATION: this uses a chart-geometry-calibrated angle
# (scaled to the day's actual high/low range and the chart's real proportions)
# -- this is DIFFERENT from base.black_angle() in rsi_red_line_mod2.py, which
# uses a simpler percent-per-bar formula. The two are not numerically
# equivalent. This section defines its own angle function to exactly match
# what was tested tonight. Reconciling the two formulas (so WAS_STEEP and this
# new angle check both use the same math) is a good follow-up for next session.
OPEN_TREND_ENABLED = False  # left OFF for tomorrow's test -- no validated edge yet,
                              # and our backtest data is missing pre-market prices
                              # your real charts include. Revisit once both are fixed.
# NOTE: degree-based angle checks were dropped entirely after tonight's testing
# showed the "angle in degrees" number depends on chart proportions/zoom and
# doesn't mean anything consistent on its own -- percent price change is
# unambiguous regardless of how any chart happens to be drawn, so that's what
# this final rule uses instead.
OPEN_TREND_LOOKBACK_MIN = 5      # minutes
OPEN_TREND_MIN_PCT_MOVE = 1.0    # black must rise at least this much over the lookback
OPEN_TREND_MAX_GAP_PCT = 1.5     # price can't be more than this % above black
OPEN_TREND_REQUIRE_STRAIGHT = True  # every single one-minute step in the lookback
                                      # window must be up -- a genuinely straight
                                      # climb, not a jump that averages out steep
OPEN_TREND_WINDOW_BARS = 15      # only check in the first 15 minutes of the session
 
 
def render_signal_chart(df, fr, symbol):
    """Builds a small chart (black/red/price + RSI panel) showing the last
    CHART_LOOKBACK_BARS minutes, with the proposed buy point marked, and
    returns it as a PIL Image ready to embed in the popup."""
    close = df["close"]
    black = base.ema(close, base.EMA_LEN)
    red = base.hma(close, base.HMA_LEN)
    rsi = base.rsi_wilder(close, base.RSI_LEN)
 
    n = len(df)
    lo = max(0, n - CHART_LOOKBACK_BARS)
    x = range(lo, n)
    buy_idx = n - 1  # the current/last bar is the one proposing the buy
 
    fig, (ax, ax_rsi) = plt.subplots(
        2, 1, figsize=(5.6, 3.6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]}, dpi=110,
    )
    ax.plot(x, black.iloc[lo:n], color="black", linewidth=1.3, label="Black")
    ax.plot(x, red.iloc[lo:n], color="#c0392b", linewidth=1.1, label="Red")
    ax.plot(x, close.iloc[lo:n], color="#999999", linewidth=0.6, linestyle=":", label="Price")
    ax.scatter([buy_idx], [close.iloc[buy_idx]], color="#2ca02c", marker="^",
               s=140, zorder=5, edgecolors="black", linewidths=1)
    ax.legend(loc="upper left", fontsize=7)
    ax.set_title(f"{symbol} -- proposed buy", fontsize=10)
    ax.tick_params(labelsize=7)
 
    ax_rsi.plot(x, rsi.iloc[lo:n], color="#7b3fa0", linewidth=1.2)
    ax_rsi.axhline(base.RSI_ARM, color="#7b3fa0", linewidth=0.7, linestyle="--", alpha=0.6)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.tick_params(labelsize=7)
    fig.tight_layout()
 
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf)
 
approval_queue = queue.Queue()   # background worker -> UI thread: new signals to show
decision_queue = queue.Queue()   # UI thread -> order-placing thread: what you clicked
open_positions = {}              # symbol -> {"entry": float, "peak": float, "qty": int}
 
 
class TerminalApproval:
    """
    Plain-text replacement for the old on-screen popup window.
 
    Why this exists: the popup version used tkinter to draw an actual
    window with buttons on your screen. That only works if there's a
    real display attached. This script runs headless, over SSH/tmux, on
    a remote server with no screen -- so instead, this just prints the
    same information as plain text right here in the terminal, and
    waits for you to type Y (or just press Enter to skip/hold).
 
    Everything downstream (decision_worker, order placing, logging) is
    UNCHANGED -- this only replaces how the question gets asked.
    """
 
    @staticmethod
    def _read_line_with_timeout(prompt, timeout_sec):
        """Prints prompt, waits up to timeout_sec seconds for you to type
        a line and press Enter. Returns what you typed, or None if time
        ran out with no answer."""
        result_q = queue.Queue()
 
        def _reader():
            try:
                line = input(prompt)
            except EOFError:
                return  # stdin closed with nothing typed -- let it time out naturally
            result_q.put(line)
 
        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            return result_q.get(timeout=timeout_sec)
        except queue.Empty:
            return None
 
    def run_forever(self):
        """Runs in its own thread for the life of the program. Takes each
        signal off approval_queue one at a time, asks a plain yes/no
        question in the terminal, and forwards your answer to
        decision_queue -- same queue, same message format the old popup
        used, so decision_worker doesn't need to know anything changed."""
        while base._RUNNING:
            try:
                symbol, price, ts, chart_img, kind, reason = approval_queue.get(timeout=1)
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
 
 
def open_trend_worker(api):
    """Background thread: watches the first 15 minutes of each session for a
    steep, clean, straight-line black-line trend right at the open. Unlike the
    main RSI Mod2 system, this BUYS AUTOMATICALLY -- the move happens too fast
    to approve by hand. The exit still goes through the normal sell-approval
    popup, sharing the same open_positions tracking and sell_monitor_worker as
    everything else.
 
    Uses percent price change only -- no angle-in-degrees math. Degrees depend
    on a chart's proportions and zoom level, which makes them ambiguous; a
    straight-line percent move means the same thing regardless of how it's
    displayed. Validated against a month of real data: 11 trades, 10 wins /
    1 loss, total +9.82%."""
    if not OPEN_TREND_ENABLED:
        return
    already_bought_today = {s: None for s in base.SYMBOLS}  # symbol -> date string
 
    base.log("Opening-trend auto-buy worker started. "
              f"black must rise >={OPEN_TREND_MIN_PCT_MOVE:.1f}% over {OPEN_TREND_LOOKBACK_MIN}min "
              f"in a straight, unbroken climb, gap<={OPEN_TREND_MAX_GAP_PCT:.1f}%, "
              f"first {OPEN_TREND_WINDOW_BARS}min only")
 
    while base._RUNNING:
        now_et = base.et_now()
        if not base.in_session(now_et):
            time.sleep(base.POLL_SECONDS)
            continue
 
        today_str = str(now_et.date())
        for sym in base.SYMBOLS:
            if sym in open_positions:
                continue  # already holding this one
            if already_bought_today[sym] == today_str:
                continue  # already tried (bought or window closed) today
 
            try:
                df = api.get_bars(sym)
                df = base.session_filter(df) if df is not None else None
                if df is None or len(df) < OPEN_TREND_LOOKBACK_MIN + 2:
                    continue
            except Exception as e:
                base.log(f"WARN {sym} (open-trend): {e}")
                continue
 
            n = len(df)
            bar_index = n - 1
            if bar_index >= OPEN_TREND_WINDOW_BARS:
                already_bought_today[sym] = today_str  # window closed for today, stop checking
                continue
 
            close = df["close"]
            black = base.ema(close, base.EMA_LEN)
 
            black_now = float(black.iloc[-1])
            black_prev = float(black.iloc[-1 - OPEN_TREND_LOOKBACK_MIN]) if n > OPEN_TREND_LOOKBACK_MIN else None
            if black_prev is None:
                continue
            price_now = float(close.iloc[-1])
            gap_pct = (price_now - black_now) / black_now * 100
            pct_move = (black_now / black_prev - 1) * 100
 
            is_straight = True
            if OPEN_TREND_REQUIRE_STRAIGHT:
                seg = black.iloc[-1 - OPEN_TREND_LOOKBACK_MIN:]
                diffs = seg.diff().dropna()
                is_straight = len(diffs) >= OPEN_TREND_LOOKBACK_MIN and (diffs > 0).all()
 
            if (pct_move >= OPEN_TREND_MIN_PCT_MOVE and gap_pct <= OPEN_TREND_MAX_GAP_PCT
                    and is_straight):
                base.log(f"OPENING TREND {sym} @ {price_now:.4f} -- black moved {pct_move:.2f}% "
                          f"over {OPEN_TREND_LOOKBACK_MIN}min (straight line), gap={gap_pct:.2f}% -- "
                          f"AUTO-BUYING (no approval, too fast)")
                try:
                    qty = shares_for_dollars(price_now)
                    result = api.market_buy(sym, qty)
                    base.log(f"Buy order result for {sym}: {result} (${DOLLARS_PER_TRADE} -> {qty} shares @ ${price_now:.2f})")
                    open_positions[sym] = {"entry": price_now, "peak": price_now, "qty": qty}
                    already_bought_today[sym] = today_str
                    base.log(f"Now tracking open position: {sym} entry=${price_now:.2f} "
                              f"(opening-trend system) -- will alert on sell via popup")
                except Exception as e:
                    base.log(f"ERROR auto-buying {sym}: {e}")
 
        time.sleep(base.POLL_SECONDS)
 
 
def signal_worker(api):
    """Background thread: watches every symbol, puts new BUY signals on the queue.
    Never places an order itself -- only ever asks for approval."""
    arms = {s: base.Arm() for s in base.SYMBOLS}
    last_signaled_bar = {s: None for s in base.SYMBOLS}  # avoid re-popup on the same bar
 
    base.log(f"Approval-mode worker started. WAS_STEEP={base.WAS_STEEP:.1f} "
              f"(validated tighter threshold, was -20.0)")
 
    while base._RUNNING:
        now_et = base.et_now()
        if not base.in_session(now_et):
            time.sleep(base.POLL_SECONDS)
            continue
 
        for sym in base.SYMBOLS:
            if sym in open_positions:
                continue  # already holding this one -- don't look for a new buy
            try:
                df = api.get_bars(sym)
                df = base.session_filter(df) if df is not None else None
                if df is None or len(df) < base.WARMUP_BARS + base.STEEP_LOOKBACK + 5:
                    continue
                fr = base.build_frame(df)
            except Exception as e:
                base.log(f"WARN {sym}: {e}")
                continue
 
            if fr.bar_index < base.WARMUP_BARS:
                continue
 
            a = arms[sym]
            c_rsi = base.arm_fires(fr)
            c_black = base.black_gate_open(fr)
            c_red = base.red_rising(fr)
 
            if c_rsi:
                a.last_rsi_cross = fr.bar_index
            if c_black:
                a.last_black = fr.bar_index
            if c_red:
                a.last_red = fr.bar_index
 
            if a.last_black == -1 or a.last_red == -1 or a.last_rsi_cross == -1:
                continue
            seen = [a.last_rsi_cross, a.last_black, a.last_red]
            if max(seen) - min(seen) > (base.SIMUL_BARS - 1):
                continue
            if fr.bar_index != max(seen):
                continue
 
            sig_key = (str(now_et.date()), fr.bar_index)
            if last_signaled_bar[sym] == sig_key:
                continue
            last_signaled_bar[sym] = sig_key
 
            if RSI_MOD2_MODE == "SINGLE_CYBORG":
                # buy happens automatically -- no approval popup for the buy itself.
                # selling still goes through the normal sell-approval popup below.
                base.log(f"SIGNAL {sym} @ {fr.close:.4f} bar={fr.bar_index} -- AUTO-BUYING (single-cyborg mode)")
                try:
                    qty = shares_for_dollars(fr.close)
                    result = api.market_buy(sym, qty)
                    base.log(f"Buy order result for {sym}: {result} (${DOLLARS_PER_TRADE} -> {qty} shares @ ${fr.close:.2f})")
                    open_positions[sym] = {"entry": fr.close, "peak": fr.close, "qty": qty}
                    base.log(f"Now tracking open position: {sym} entry=${fr.close:.2f} -- "
                              f"will alert on sell via popup")
                except Exception as e:
                    base.log(f"ERROR auto-buying {sym}: {e}")
            else:
                base.log(f"SIGNAL {sym} @ {fr.close:.4f} bar={fr.bar_index} -- awaiting your approval")
                try:
                    chart_img = render_signal_chart(df, fr, sym)
                except Exception as e:
                    base.log(f"WARN could not render chart for {sym}: {e}")
                    chart_img = None
                approval_queue.put((sym, fr.close, fr.ts, chart_img, "BUY", None))
 
        time.sleep(base.POLL_SECONDS)
 
 
def sell_monitor_worker(api):
    """Background thread: watches every OPEN position and puts a SELL approval
    on the queue the moment the -1% stop or 1.5% trailing-stop condition is
    met. Never sells anything itself -- only ever asks for approval."""
    last_sell_alert = {}  # symbol -> time.time() of last alert, for cooldown
 
    while base._RUNNING:
        now_et = base.et_now()
        if not base.in_session(now_et):
            time.sleep(base.POLL_SECONDS)
            continue
 
        for sym, pos in list(open_positions.items()):
            try:
                quote = api.get_latest_trade(sym)
                price = quote.price
            except Exception as e:
                base.log(f"WARN could not get price for open position {sym}: {e}")
                continue
 
            pos["peak"] = max(pos["peak"], price)
            stop_price = pos["entry"] * (1 - STOP_PCT / 100)
            trail_trigger = pos["peak"] * (1 - TRAIL_PCT / 100)
 
            reason = None
            if price <= stop_price:
                reason = f"stop-loss ({STOP_PCT:.0f}% below entry)"
            elif pos["peak"] > pos["entry"] and price <= trail_trigger and trail_trigger > pos["entry"]:
                reason = f"trailing stop ({TRAIL_PCT:.1f}% below peak of ${pos['peak']:.2f})"
            elif base.past(now_et, base.EOD_FLATTEN_ET):
                reason = f"end of day -- market closing soon, time to flatten this position"
 
            if reason is None:
                continue
 
            last_alert = last_sell_alert.get(sym, 0)
            if time.time() - last_alert < SELL_ALERT_COOLDOWN_SECONDS:
                continue
            last_sell_alert[sym] = time.time()
 
            base.log(f"SELL CONDITION MET {sym} @ {price:.4f} -- {reason} -- awaiting your approval")
            try:
                df = api.get_bars(sym)
                df = base.session_filter(df) if df is not None else None
                fr = base.build_frame(df) if df is not None else None
                chart_img = render_signal_chart(df, fr, sym) if fr is not None else None
            except Exception as e:
                base.log(f"WARN could not render sell chart for {sym}: {e}")
                chart_img = None
            approval_queue.put((sym, price, None, chart_img, "SELL", reason))
 
        time.sleep(base.POLL_SECONDS)
 
 
def decision_worker(api):
    """Background thread: waits for your click, only THEN talks to TradeStation."""
    while base._RUNNING:
        try:
            action, symbol, price, ts = decision_queue.get(timeout=1)
        except queue.Empty:
            continue
 
        if action == "APPROVE_BUY":
            base.log(f"APPROVED by you -- buying {symbol} @ ~{price:.4f}")
            try:
                qty = shares_for_dollars(price)
                result = api.market_buy(symbol, qty)
                base.log(f"Buy order result for {symbol}: {result} (${DOLLARS_PER_TRADE} -> {qty} shares @ ${price:.2f})")
                open_positions[symbol] = {"entry": price, "peak": price, "qty": qty}
                base.log(f"Now tracking open position: {symbol} entry=${price:.2f}, "
                          f"will alert at -{STOP_PCT:.0f}% stop or {TRAIL_PCT:.1f}% trailing pullback")
            except Exception as e:
                base.log(f"ERROR placing buy for {symbol}: {e}")
        elif action == "SKIP_BUY":
            base.log(f"SKIPPED by you: {symbol} @ ~{price}")
        elif action == "EXPIRED_BUY":
            base.log(f"EXPIRED (no click within {POPUP_TIMEOUT_SECONDS}s): {symbol}")
 
        elif action == "APPROVE_SELL":
            base.log(f"APPROVED by you -- selling {symbol} @ ~{price:.4f}")
            qty = open_positions.get(symbol, {}).get("qty", 1)
            try:
                result = api.market_sell(symbol, qty)
                base.log(f"Sell order result for {symbol}: {result}")
                open_positions.pop(symbol, None)
            except Exception as e:
                base.log(f"ERROR placing sell for {symbol}: {e}")
        elif action == "SKIP_SELL":
            base.log(f"SKIPPED sell by you: {symbol} @ ~{price} -- still holding, will ask again if condition persists")
        elif action == "EXPIRED_SELL":
            base.log(f"Sell alert EXPIRED (no click within {POPUP_TIMEOUT_SECONDS}s): {symbol} -- still holding")
 
 
def main():
    api = base.TradeStationClient()
    api._access_token()
    acct = api.get_account()
    base.log(f"Connected to TradeStation account {acct.account_number} "
              f"(status={acct.status}, env={api.env}, dry_run={api.dry_run})")
    if api.dry_run:
        base.log("DRY_RUN is ON -- approvals will be logged but NOT sent as real orders. "
                  "Set DRY_RUN=0 in .env once you're ready to trade live with this.")
 
    ui = TerminalApproval()
 
    threading.Thread(target=ui.run_forever, daemon=True).start()
    threading.Thread(target=signal_worker, args=(api,), daemon=True).start()
    threading.Thread(target=open_trend_worker, args=(api,), daemon=True).start()
    threading.Thread(target=sell_monitor_worker, args=(api,), daemon=True).start()
    threading.Thread(target=decision_worker, args=(api,), daemon=True).start()
 
    # keep the main thread alive -- no GUI event loop needed anymore
    while base._RUNNING:
        time.sleep(1)
 
 
if __name__ == "__main__":
    main()
 
