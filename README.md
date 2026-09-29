# Jakadbangdu — self-improving multi-agent trading system (Dhan)

Head trader **Jakadbangdu** decides; five independent analysts feed it; every trade is journaled in full;
every closed trade is post-mortemed and the lesson feeds the next decision.

```
                 ┌ Technical Analyst ┐   candles + indicators (Dhan)
                 ├ Research Analyst  ┤   company news/results/events (web search)
 watchlist ─►screen ├ Market Researcher ┤   index, breadth, macro, FII/VIX (Dhan + web)   ──► Jakadbangdu ─► risk gate ─► Dhan
  (code)         ├ Trend Analyst     ┤   multi-timeframe trend, RS vs NIFTY (Dhan)          ▲  (LLM)      (code)
                 └ Data Analyst      ┘   volatility, liquidity, statistical edge (Dhan)      │
                                                                                  playbook + lessons ◄─ post-mortem ◄─ closed trade
```

* **Dhan only**: all prices/candles/orders via `dhanhq` (no OpenAlgo or other broker). News/macro research uses Claude's web search because Dhan has no news feed.
* **Analysts run in parallel and never see each other's output.** Jakadbangdu weighs the disagreement.
* **Rules you set are enforced in code (`risk.py`)**, not left to the LLM: INR 5 lakh book (compounds with realised P&L), ≥30% always free, ≤5 open positions.
  Everything else (entry, SL, target, risk per trade, setup) is the bot's. Backstops in `config.py`: risk ≤2% of capital per trade, reward:risk ≥1, ≤20% in one stock — edit freely.
* **Learning loop**: on every exit `Jakadbangdu.reflect()` writes a post-mortem (mistake type, what went wrong, one testable rule) → `lessons` table. Every 5 closed trades the lessons are consolidated into `data/playbook.md`. Playbook + recent lessons + win-rate/R by setup are injected into every future decision and position review; the decision records which `lessons_applied`.
* **Trade log** (`data/journal.db`, export with `export`): entry/exit time+price, SL (initial and current), target, entry reason, exit reason, setup, qty, P&L, R-multiple, confidence, plus the full analyst reports and decision snapshot at entry and the post-mortem.

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
python -m jakadbangdu book | trades | export | playbook
python -m pytest                                       # offline tests with fake broker/LLM
```

## Modes — start in paper
`TRADING_MODE=paper` (default): real Dhan data, simulated fills, nothing reaches the exchange.
`TRADING_MODE=live`: CNC delivery limit entry + exchange-side SL order (cancelled/re-placed on trail/exit); target exits are bot-monitored.
**The live executor is untested against a real account** — run paper for weeks, then start live with a small size and check the first fills by hand.

## Honest limits
* LLM judgement is not an edge by itself; the value here is discipline, logging and the feedback loop. Judge it on the journal, not on a few trades.
* Only 5 closed trades per playbook rewrite means learning is slow and lessons from tiny samples can be noise — keep the sample size in mind before trusting a "rule".
* The bot monitors targets by polling; a crashed process misses target exits (stops are protected exchange-side in live mode).
* Not investment advice; you are responsible for orders placed.
