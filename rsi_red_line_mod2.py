NOTE TO ALEXANDER - WINNING SIDE (alert-only), built Tue 10/20/2026, for a Thursday 10/22 test

WHAT IT IS
  A new, separate program: rsi_alert_winningside.py   sha256 starts 81d54821
  Full hash: 81d54821f7b977b2e13321cf55fb64572d6d397f804d276241c026f7a318b3bf
  ALERT ONLY. It places NO orders and needs no trading account. It prints BUY alerts and EXIT alerts and
  logs every paper trade to winningside_log.csv. It can run at the same time as Engine B (second terminal).
  Engine A / Engine B are unchanged.

THE RULE
  Buy alert: red line crosses UP through black, price angle over the last 4 closes >= 30 deg, black line
  not falling (10-minute angle > -15), 12+ bars, window 9:30-10:00 ET (6:30-7:00 AM Arizona).
  Exit: red closes below black, OR red line turns down at -35 deg or steeper, OR -2% from alert price.
  Lines are built from PRE-MARKET bars (from 4:00 ET).

THE ONE THING TO CHECK FIRST (pre-market data)
  The program asks TradeStation for pre-market bars (sessiontemplate=USEQPreAndPost). I could not test this
  from here. At startup it prints "PREMARKET CHECK" lines for all 22 tickers: how many bars today, how many
  before 9:30 ET. PLEASE START IT ONCE ANY MORNING AFTER ~5 AM ARIZONA (8 AM ET) OR TONIGHT-ISH WEEKDAY AND SEND ME THE
  CHECK LINES (or the first screen). If it says bars are rejected / zero pre-market bars, tell me; that is
  what I need to fix before Thursday.
  Tickers with fewer than 10 pre-market bars are skipped (logged), so alerts never use wrongly built lines.

STEPS
  1. Put rsi_alert_winningside.py on GitHub.
  2. cd ~/rsi-algo
     git fetch origin && git show origin/main:rsi_alert_winningside.py > rsi_alert_winningside.py
  3. sha256sum rsi_alert_winningside.py    -> must start 81d54821  (e3b0c442 = empty file, wrong name)
  4. Start (second terminal is fine, Engine B can keep running):
     set -a; source .env; set +a; ./venv/bin/python3 rsi_alert_winningside.py 2>&1 | tee winningside_$(date +%F).log
  5. Startup MODE line must show: CROSS_MIN_ANGLE=30 ... SHARP_EXIT=-35 STOP=2.0% WINDOW=09:30-10:00 ET
  6. Leave it running; do not restart intraday (paper trades are held in memory).
  7. After the close send back winningside_<date>.log and winningside_log.csv.

TESTED HERE: replayed 667 historical ticker-days through this exact code; it produced the same alerts and
the same exits as the backtest on all 667. Not tested: the live TradeStation connection / pre-market request.
