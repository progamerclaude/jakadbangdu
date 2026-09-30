# Jakadbangdu — self-improving multi-agent trading system (Dhan)

Head trader **Jakadbangdu** decides; five independent analysts feed it; every trade is journaled in full;
every closed trade is post-mortemed and the lesson feeds the next decision.

```
                 ┌ Technical Analyst ┐   candles + indicators (Dhan)
                 ├ Research Analyst  ┤   company news/results/events (web search)
 watchlist ─►setup rules ├ Market Researcher ┤   index, breadth, macro, FII/VIX (Dhan + web)   ──► Jakadbangdu ─► risk gate ─► Dhan
  (code)         ├ Trend Analyst     ┤   multi-timeframe trend, RS vs NIFTY (Dhan)          ▲  (LLM)      (code)
                 └ Data Analyst      ┘   volatility, liquidity, statistical edge (Dhan)      │
                                                                                  playbook + lessons ◄─ post-mortem ◄─ closed trade
```

* **Dhan only**: all prices/candles/orders via `dhanhq` (no OpenAlgo or other broker). News/macro research uses Claude's web search because Dhan has no news feed.
* **Analysts run in parallel and never see each other's output.** Jakadbangdu weighs the disagreement.
* **Rules you set are enforced in code (`risk.py`)**, not left to the LLM: INR 5 lakh book (compounds with realised P&L), ≥30% always free, ≤5 open positions.
  Everything else (entry, SL, target, risk per trade, setup) is the bot's. Backstops in `config.py`: risk starts at 2% of capital per trade (the AI may lower it from its reviews; hard ceiling 2%), reward:risk ≥1, ≤20% in one stock — edit freely.
* **Mandate: 10% of capital per month.** `performance.py` tracks month P&L against the target (ahead/behind pace) and Jakadbangdu sees it on every decision.
  The target is an outcome, not a lever: **falling behind never raises risk.** Code circuit breakers: month P&L ≤ -4% → risk halved, ≤ -8% → no new entries that month, target reached → risk halved to protect the month (`config.py`).
* **Self-improvement, three layers** (each is measured, and the last is enforced in code):
  1. *Every trade:* post-mortem (mistake type, what went wrong, one testable rule) → `lessons`; every 5 trades lessons are merged into `data/playbook.md`.
  2. *Scored inputs:* each analyst's stance is graded against the trade outcome → hit-rate scorecard shown to Jakadbangdu so unreliable analysts lose weight. Trades that applied a lesson are compared with those that didn't (does the "fix" actually help?).
  3. *Strategy review* (every 5 closed trades, and Fridays in `run`, or `review`): Jakadbangdu audits the numbers and outputs new parameters, saved to `data/strategy.json` and **enforced by the risk gate**: risk-per-trade cap, min reward:risk, min confidence, and an avoid-list of setups. Clamped: risk never above your hard ceiling, can rise at most 25% per review, RR never below the floor.
* **Trade log** (`data/journal.db`, export with `export`): entry/exit time+price, SL (initial and current), target, entry reason, exit reason, setup, qty, P&L, R-multiple, confidence, plus the full analyst reports and decision snapshot at entry and the post-mortem.

## Trading rules (your setup, enforced in code: `setups.py`)
All on **daily closed candles**, long-only. MA type/periods are in `config.py` (default EMA 20/40/89/100; set `ma_type="sma"` if you meant SMAs).

| Step | Rule |
|---|---|
| Market filter (optional, **off by default**) | Enable with `JAKAD_REGIME_FILTER=1`: no new entries unless NIFTY 50 closes above its EMA50. Not part of your rules; the backtest robustness table compares it |
| Trend stack | MA20 > MA40 > MA89 > MA100 for the last 10 bars, and price above MA40 (price > MA20 when not mid-pullback) |
| Entry | Pullback: low touches MA20 (within 1%, close holds it) **or** low touches MA40 (price under MA20 but holding MA40); price must have been ≥2% above the pullback low within 20 bars |
| Stop | Below the last confirmed swing low (3-bar fractal) minus 0.1×ATR. If price has already broken that swing low, or the stop is >10% away, no trade |
| Universe | NSE's official **Nifty 200** list, downloaded at runtime from NSE, cached in `data/nifty200.csv`, refreshed every 30 days (stale copy used with a warning if the download fails; `--symbols` overrides) |
| Minimum target | Trades whose target is under **+10%** above entry are rejected by the risk gate |
| Ranking | Signals ranked by 60-day return relative to NIFTY; top 6 go to the analysts |
| AI role | Jakadbangdu can only **TRADE/SKIP** a valid signal, pick risk %, confidence and target. Entry, SL and setup name are overwritten by the rules |
| Add | On break of structure (daily close above the last swing high), up to 2 adds of ≤50% of initial size, only if total risk at the trailed stop ≤ the original 1R and the 30%-free / 20%-per-stock limits hold |
| Trail | Stop moves to the newest higher swing low (only up); live mode re-places the exchange SL |
| Exit | Trailing stop, or Jakadbangdu's discretionary exit at the 12:00/15:00 reviews. The target is planning-only (`exit_at_target=False`) |

`python -m jakadbangdu manage` runs trail + BoS-add on demand; `run` does it at 09:50 and 15:20.

## Rules-only mode (pure Python strategy, no AI, no Anthropic key)
`JAKAD_RULES_ONLY=1` in `.env` (or `--rules-only`) removes every LLM call. The strategy is then exactly the table above, traded mechanically:
stack + pullback signal → ranked by relative strength → best signal taken → 1% risk (or the strategy cap), SL below swing low, target = max(swing high, 2R), adds on BoS, trailing stop. Same journal, risk gate (5 positions, 30% free, drawdown breakers) and `report`/`trades`/`export` commands. No analysts, no post-mortem lessons, no self-tuning (parameters stay fixed), no discretionary exits.
The signal logic is reusable on its own: `from jakadbangdu.setups import pullback_signal` takes a daily OHLCV DataFrame and returns the entry/SL/levels (or `None`).
The backtester never used the LLM, so its results describe this mode exactly.

## Run it unattended (paper, real-time)
Needs an always-on machine or VPS (this repo's cloud session can't stay running). Paper mode uses real Dhan data and **simulated fills**, so it never touches your money; it learns from those paper trades.
```
cp .env.example .env            # fill DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, ANTHROPIC_API_KEY  (TRADING_MODE stays paper)
python -m jakadbangdu preflight # checks Anthropic, Dhan token + Data plan, quotes, journal; must say READY
docker compose up -d --build    # or: python -m jakadbangdu run
```
* Schedule (IST, weekdays): SL monitor every minute in market hours; scans 09:45 & 13:30; trail/BoS-add 09:50 & 15:20; position review 12:00 & 15:00; **strategy self-audit Fridays 15:45**. A job missed by >60 min (bot was down) is skipped, not run late. Errors in a job are logged and the loop continues; logs in `data/jakad.log`.
* **Dhan access tokens expire** (check `tokenValidity` in preflight); refresh `DHAN_ACCESS_TOKEN` and `docker compose restart` when needed. Market holidays are not calendar-aware yet (quotes just stay flat).
* Watch it: `python -m jakadbangdu report | trades | book | playbook`.
* **Going real-money is deliberately hard**: `TRADING_MODE=live` refuses to start unless `JAKAD_CONFIRM_LIVE=YES_I_ACCEPT_REAL_MONEY_RISK` is set **and** there are ≥20 closed paper trades (`JAKAD_MIN_PAPER_TRADES` overrides). Also needs a whitelisted static IP on Dhan.

## Backtest (run this BEFORE paper/live)
```
python -m jakadbangdu backtest [--symbols ...] [--years 6]
```
Fetches Dhan daily history (cached in `data/cache/`), replays your rules and prints: **walk-forward** (first 60% in-sample, last 40% untouched out-of-sample) and a **robustness table** (SMA vs EMA, 0.5%/1%/2% touch tolerance, with the NIFTY filter, wider SL buffer, no adds). Reports win rate, expectancy in R, profit factor, CAGR, average/median month, months ≥10%, worst month, max drawdown.
Honest scope: decisions use data to day t's close and execute at day t+1's open; gap-down stops fill at the open; costs of ~0.18% per side (STT, fees, slippage). Not modelled: the LLM's take/skip filter and the reward:risk gate, so it measures the *rules alone*. A test proves that removing future bars changes no earlier trade (no lookahead).
Read the out-of-sample column, and whether neighbouring parameters also work. If the edge exists only at one setting, it is probably luck.

## Setup
```
pip install -r requirements.txt
cp .env.example .env     # DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, ANTHROPIC_API_KEY
```
Dhan needs an active **Data API plan** (history/quotes) and, for live orders, a whitelisted **static IP**.

## Use
```
python -m jakadbangdu scan  [--symbols TCS,INFY,...]   # one scan → maybe one trade
python -m jakadbangdu monitor [--review]               # SL/target check; --review = Jakadbangdu reviews each position
python -m jakadbangdu run                              # market-hours loop (IST): monitor 1/min, scan 09:45 & 13:30, review 12:00 & 15:00
python -m jakadbangdu report                           # month vs 10% target, win rate, expectancy, by-setup, analyst scorecard
python -m jakadbangdu review                           # force a strategy self-review now
python -m jakadbangdu book | trades | export | playbook
python -m pytest                                       # offline tests with fake broker/LLM
```

## Modes — start in paper
`TRADING_MODE=paper` (default): real Dhan data, simulated fills, nothing reaches the exchange.
`TRADING_MODE=live`: CNC delivery limit entry + exchange-side SL order (cancelled/re-placed on trail/exit); target exits are bot-monitored.
**The live executor is untested against a real account** — run paper for weeks, then start live with a small size and check the first fills by hand.

## Honest limits
* **10%/month (~214%/year) is an aggressive goal, and no system can guarantee it.** Professional funds target far less. That is why the bot is built to hit it through selectivity rather than leverage, and to cut risk after losses. Expect losing months; a -8% halt is designed in. Consider judging the system on expectancy and drawdown over 3-6 months of paper trading, not on any single month.
* LLM judgement is not an edge by itself; the value here is discipline, logging and the feedback loop. Judge it on the journal, not on a few trades.
* Only 5 closed trades per playbook rewrite means learning is slow and lessons from tiny samples can be noise — keep the sample size in mind before trusting a "rule".
* The bot monitors targets by polling; a crashed process misses target exits (stops are protected exchange-side in live mode).
* Not investment advice; you are responsible for orders placed.
