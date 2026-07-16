RSI TREND CYBORG SELL — Live/Paper Signal Engine  (starter for Alexander to hook up)
=====================================================================================
WHAT THIS DOES
  Watches a set of tickers on 1-minute bars. When a BUY setup fires (per the exact
  backtested rules below), it places a paper BUY + a -5% protective STOP via Alpaca.
  The PROFIT sell is NOT automated — it is CYBORG (done by hand). The engine only
  flags/logs an open position for the trader to work the exit by eye.

WHAT ALEXANDER NEEDS TO HOOK UP  (search for  >>> HOOK UP)
  1. Real-time 1-minute bar feed  ->  call engine.on_bar(symbol, bar) for each new bar
  2. Alpaca paper client          ->  fill in Broker.buy() and Broker.stop()
  3. (optional) an alert/log sink ->  Broker already prints; wire to Slack/file if wanted

MUST MATCH THE BACKTEST EXACTLY (do not change the math):
  - black line  = EMA-20 of close
  - RSI         = Wilder RSI-7 of close
  - session     = regular hours 09:30–15:59 ET, 1-min bars
"""

from collections import deque, defaultdict
from datetime import datetime, time
import numpy as np

# ============================== CONFIG ==============================
SYMBOLS = ["ASTX","ASTN","IONX","IONZ","RGTX","RGTZ","NBIL","NBIZ"]

RSI_LEVEL     = 40      # 40 for the shake-out/plumbing test. Use 35 for real trading.
RSI_PERIOD    = 7
EMA_PERIOD    = 20
ARM_WINDOW    = 25      # bars: arm expires if no fire within this many bars
FIRE_DISTANCE = 0.4     # % the black line must rise off its low (since arm) to fire
TREND_GATE    = 1.0     # % the black line must have risen over the last 30 bars
TREND_LOOKBACK= 30      # bars
STOP_PCT      = 5.0     # % hard protective stop (the disaster floor)
SHARES        = 1       # 1 share for the live test
SESSION_START = time(9,30)
SESSION_END   = time(15,59)
NEW_BUY_CUTOFF = None   # None = all day (day-1 shake-out). Set time(11,0) for the rush version.
BUFFER = 120

# ============================== INDICATORS (match backtest) ==============================
def ema_series(vals, period):
    a = 2/(period+1); out=[vals[0]]
    for v in vals[1:]:
        out.append(a*v + (1-a)*out[-1])
    return out

def rsi_wilder(vals, period):
    if len(vals) < period+1: return float("nan")
    deltas = np.diff(vals)
    up = np.clip(deltas,0,None); dn = -np.clip(deltas,None,0)
    ru = up[0]; rd = dn[0]
    a = 1/period
    for i in range(1,len(deltas)):
        ru = a*up[i] + (1-a)*ru
        rd = a*dn[i] + (1-a)*rd
    if rd == 0: return 100.0
    return 100 - 100/(1+ru/rd)

# ============================== BROKER (>>> HOOK UP Alpaca here) ==============================
class Broker:
    def __init__(self, paper=True):
        self.paper = paper
        # >>> HOOK UP: create Alpaca client here, e.g.
        # from alpaca.trading.client import TradingClient
        # self.client = TradingClient(API_KEY, API_SECRET, paper=True)
        self.client = None

    def buy(self, symbol, shares, ref_price):
        # >>> HOOK UP: submit a market BUY for `shares` of `symbol`
        print(f"[BUY ] {symbol}  {shares}sh  ~{ref_price:.2f}   (paper={self.paper})")

    def stop(self, symbol, shares, stop_price):
        # >>> HOOK UP: submit a STOP (sell) at stop_price to cap the -5% downside
        print(f"[STOP] {symbol}  sell {shares}sh @ {stop_price:.2f}  (-{STOP_PCT}% floor)")

    def alert_open(self, symbol, entry):
        print(f"[OPEN] {symbol} filled ~{entry:.2f}. >>> CYBORG SELL: trader works the exit by eye. Stop is set at -{STOP_PCT}%.")

# ============================== SIGNAL ENGINE ==============================
class SymbolState:
    def __init__(self):
        self.closes = deque(maxlen=BUFFER)
        self.armed = False
        self.bars_since_arm = 0
        self.black_low_since_arm = None
        self.in_position = False

class Engine:
    def __init__(self, broker):
        self.broker = broker
        self.state = defaultdict(SymbolState)

    def mark_flat(self, symbol):
        """Call when the trader has manually closed a position (or stop hit)."""
        self.state[symbol].in_position = False

    def on_bar(self, symbol, ts: datetime, close: float):
        """>>> HOOK UP: call this once per NEW 1-min bar per symbol."""
        if not (SESSION_START <= ts.time() <= SESSION_END): return
        st = self.state[symbol]
        st.closes.append(close)
        vals = list(st.closes)
        if len(vals) < TREND_LOOKBACK + RSI_PERIOD + 2: return
        if st.in_position: return

        blk = ema_series(vals, EMA_PERIOD)
        rsi_now  = rsi_wilder(vals, RSI_PERIOD)
        rsi_prev = rsi_wilder(vals[:-1], RSI_PERIOD)
        black    = blk[-1]

        # ---- ARM: RSI crosses UP through level ----
        if (not st.armed) and rsi_prev < RSI_LEVEL <= rsi_now:
            st.armed = True; st.bars_since_arm = 0; st.black_low_since_arm = black
            return

        if st.armed:
            st.bars_since_arm += 1
            st.black_low_since_arm = min(st.black_low_since_arm, black)
            if st.bars_since_arm > ARM_WINDOW:
                st.armed = False; return
            # ---- FIRE: black rose FIRE_DISTANCE% off its low since arm ----
            rose = (black - st.black_low_since_arm)/st.black_low_since_arm*100
            if rose >= FIRE_DISTANCE:
                st.armed = False
                # ---- TREND GATE: black up >= TREND_GATE% over last 30 bars ----
                past = blk[-1-TREND_LOOKBACK]
                trend = (black - past)/past*100
                if trend < TREND_GATE: return
                # ---- TIME CUTOFF for new buys ----
                if NEW_BUY_CUTOFF and ts.time() >= NEW_BUY_CUTOFF: return
                # ---- BUY + STOP ----
                self.broker.buy(symbol, SHARES, close)
                self.broker.stop(symbol, SHARES, close*(1-STOP_PCT/100))
                self.broker.alert_open(symbol, close)
                st.in_position = True

# ============================== RUN (>>> HOOK UP live feed) ==============================
if __name__ == "__main__":
    broker = Broker(paper=True)
    engine = Engine(broker)
    print("RSI TREND CYBORG SELL live engine ready. RSI_LEVEL=%d  STOP=-%d%%  symbols=%s"
          % (RSI_LEVEL, STOP_PCT, ",".join(SYMBOLS)))
    # >>> HOOK UP: subscribe to Alpaca (or Barchart) 1-min bars -> engine.on_bar:
    #   from alpaca.data.live import StockDataStream
    #   stream = StockDataStream(API_KEY, API_SECRET)
    #   async def handler(bar):
    #       engine.on_bar(bar.symbol, bar.timestamp, bar.close)
    #   for s in SYMBOLS: stream.subscribe_bars(handler, s)
    #   stream.run()
    #
    # Cyborg sell: when the trader closes a position by hand, call engine.mark_flat(symbol).
