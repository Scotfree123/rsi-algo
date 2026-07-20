# ============================== CONFIG ==============================
# The 4 pairs we have data for (add SOXL/SOXS, SNXX/SNDQ once available).
# Run ALL tickers — the falling twin simply won't fire (its black line won't turn up),
# so the engine self-selects the "up twin". No need to pick a side manually.
SYMBOLS = ["ASTX","ASTN","IONX","IONZ","RGTX","RGTZ","NBIL","NBIZ"]

RSI_LEVEL     = 40      # 40 for the shake-out/plumbing test (more trades). Use 35 for real trading.
RSI_PERIOD    = 7
EMA_PERIOD    = 20
ARM_WINDOW    = 25      # bars: arm expires if no fire within this many bars
FIRE_DISTANCE = 0.4     # % the black line must rise off its low (since arm) to fire
TREND_GATE    = 1.0     # % the black line must have risen over the last 30 bars
TREND_LOOKBACK= 30      # bars
STOP_PCT      = 5.0     # % hard protective stop (the disaster floor)
SHARES        = 1       # 1 share for the live test
SESSION_START = time(9,30)
SESSION_END   = time(15,59)
# Time cutoff for NEW buys. None = all day (use for day-1 shake-out).
# For the "rush" version set to a datetime.time like time(11,0).
NEW_BUY_CUTOFF = None
BUFFER = 120            # keep last N closes per symbol (need >= TREND_LOOKBACK+few)

# ============================== INDICATORS (match backtest) ==============================
def ema(vals, period):
    a = 2/(period+1); e = vals[0]
    for v in vals[1:]:
        e = a*v + (1-a)*e
    return e

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
        # order = MarketOrderRequest(symbol=symbol, qty=shares, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        # self.client.submit_order(order)
        print(f"[BUY ] {symbol}  {shares}sh  ~{ref_price:.2f}   (paper={self.paper})")

    def stop(self, symbol, shares, stop_price):
        # >>> HOOK UP: submit a STOP (sell) at stop_price to cap the -5% downside
        # order = StopOrderRequest(symbol=symbol, qty=shares, side=OrderSide.SELL, stop_price=round(stop_price,2), time_in_force=TimeInForce.DAY)
        # self.client.submit_order(order)
        print(f"[STOP] {symbol}  sell {shares}sh @ {stop_price:.2f}  (-{STOP_PCT}% floor)")

    def alert_open(self, symbol, entry):
        # Position is now OPEN. The PROFIT sell is CYBORG — trader works it by hand.
        print(f"[OPEN] {symbol} filled ~{entry:.2f}. >>> CYBORG SELL: trader works the exit by eye. Stop is set at -{STOP_PCT}%.")

# ============================== SIGNAL ENGINE ==============================
class SymbolState:
    def __init__(self):
        self.closes = deque(maxlen=BUFFER)
        self.armed = False
        self.bars_since_arm = 0
        self.black_low_since_arm = None
        self.in_position = False   # once open, stop watching for new buys until flat
        self.last_rsi = None       # for the status board
        self.last_trend = None     # black % over 30 bars, for the status board
        self.entry_price = None    # fill price when long, for the status board

class Engine:
    def __init__(self, broker):
        self.broker = broker
        self.state = defaultdict(SymbolState)

    def mark_flat(self, symbol):
        """Call this when the trader has manually closed a position (or stop hit),
           so the engine can look for the next setup on this symbol."""
        self.state[symbol].in_position = False
        self.state[symbol].entry_price = None

    def status(self, current_prices=None):
        """Print a status board of every ticker: what state it's in right now.
           Call this whenever you want to see the picture (e.g. once a minute).
           current_prices: optional {symbol: price} so LONG rows show live P&L.
           States:  LONG   = you own it, work the sell by eye
                    ARMED  = RSI crossed 35, watching for the black to turn up 0.4%
                    watch  = has data, waiting for an RSI arm
                    (warming) = not enough bars yet
        """
        print("\n===== STATUS BOARD =====")
        print("%-8s %-9s %6s %8s  %s" % ("ticker","state","RSI","trend%","note"))
        for sym in sorted(self.state):
            st = self.state[sym]
            rsi = "  -  " if st.last_rsi is None else "%5.1f" % st.last_rsi
            trd = "   -  " if st.last_trend is None else "%+5.1f" % st.last_trend
            if len(st.closes) < TREND_LOOKBACK + RSI_PERIOD + 2:
                state, note = "(warming)", "collecting bars"
            elif st.in_position:
                state = "LONG"
                if current_prices and sym in current_prices and st.entry_price:
                    pnl = (current_prices[sym]/st.entry_price - 1)*100
                    note = "in @ %.2f  now %+.1f%%  >>> CYBORG SELL by eye" % (st.entry_price, pnl)
                else:
                    note = "in @ %.2f  >>> work the sell by eye" % (st.entry_price or 0)
            elif st.armed:
                state = "ARMED"
                note = "watching for black to turn up 0.4%% (bar %d/%d)" % (st.bars_since_arm, ARM_WINDOW)
            else:
                state, note = "watch", "waiting for RSI to cross up thru %d" % RSI_LEVEL
            print("%-8s %-9s %6s %8s  %s" % (sym, state, rsi, trd, note))
        print("========================\n")

    def on_bar(self, symbol, ts: datetime, close: float):
        """>>> HOOK UP: call this once per NEW 1-min bar per symbol."""
        if not (SESSION_START <= ts.time() <= SESSION_END): return
        st = self.state[symbol]
        st.closes.append(close)
        vals = list(st.closes)
        if len(vals) < TREND_LOOKBACK + RSI_PERIOD + 2: return   # need history
        if st.in_position: return                                 # already long; trader is working it

        blk = ema_series(vals, EMA_PERIOD)
        rsi_now  = rsi_wilder(vals, RSI_PERIOD)
        rsi_prev = rsi_wilder(vals[:-1], RSI_PERIOD)
        black    = blk[-1]
        st.last_rsi = rsi_now
        st.last_trend = (black - blk[-1-TREND_LOOKBACK])/blk[-1-TREND_LOOKBACK]*100

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
                if trend < TREND_GATE: return          # not a real trend -> skip
                # ---- TIME CUTOFF for new buys ----
                if NEW_BUY_CUTOFF and ts.time() >= NEW_BUY_CUTOFF: return
                # ---- BUY + STOP ----
                self.broker.buy(symbol, SHARES, close)
                self.broker.stop(symbol, SHARES, close*(1-STOP_PCT/100))
                self.broker.alert_open(symbol, close)
                st.in_position = True
                st.entry_price = close

# ============================== RUN (>>> HOOK UP live feed) ==============================
if __name__ == "__main__":
    broker = Broker(paper=True)
    engine = Engine(broker)
    print("RSI TREND CYBORG SELL live engine ready. RSI_LEVEL=%d  STOP=-%d%%  symbols=%s"
          % (RSI_LEVEL, STOP_PCT, ",".join(SYMBOLS)))
    # >>> HOOK UP: subscribe to Alpaca (or Barchart) 1-min bars and route each to engine.on_bar:
    #
    #   from alpaca.data.live import StockDataStream
    #   stream = StockDataStream(API_KEY, API_SECRET)
    #   async def handler(bar):
    #       engine.on_bar(bar.symbol, bar.timestamp, bar.close)
    #   for s in SYMBOLS: stream.subscribe_bars(handler, s)
    #   stream.run()
    #
    # For the profit sell (cyborg): when the trader closes a position by hand,
    # call engine.mark_flat(symbol) so the engine resumes watching that symbol.
