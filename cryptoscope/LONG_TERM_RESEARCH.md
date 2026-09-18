# Long-term strategy research

This local research path is intentionally separate from the active MEANX market database.
It downloads completed daily Binance spot candles, evaluates fixed long-horizon rules, and
writes its evidence to `data/long_term_7y/`. Buy-and-hold is calculated over the same
eligible time window as a control benchmark.

Run from `cryptoscope/`:

```bash
python scripts/build_long_term_research.py \
  --top 10 \
  --years 7 \
  --horizon-days 365 \
  --target-return-pct 15 \
  --probability-threshold-pct 80
```

Outputs:

- `binance_top_daily.csv`: daily OHLCV research dataset.
- `long_term_events.csv`: non-overlapping historical signal episodes.
- `strategy_summary.csv`: comparable results for all 30 fixed rules.
- `long_term_report.json`: universe metadata, confidence intervals, and qualification result.

Recalculate from the saved dataset without network access by adding `--reuse-data`.

The first online run also creates `universe.json`. Later runs reuse that exact asset basket
and only refresh candles. The universe changes only when `--refresh-universe` is passed
explicitly. Strategy rules are versioned as `long-term-fixed-rules-v2-30`.

The universe is the current highest-market-cap non-stable assets that have a USDT spot pair
and at least 97% of the requested Binance history. This creates survivorship bias and is only
suitable for a first feasibility screen. A production backtest needs historical monthly market
cap rankings, including delisted assets.

A strategy is not accepted merely because its observed hit rate exceeds 80%. It must have at
least 20 independent market episodes, and its Bonferroni-adjusted family-wise confidence lower
bound must also exceed 80%. Entries occur on the next daily close after a month-end signal and
costs are deducted. Overlapping asset signals are combined into one equal-weight market episode
instead of being incorrectly counted as independent evidence.

The Long Term app screen uses the separate `long-term-investment-weekly-v1` model. It asks whether
a new purchase at the latest completed weekly price can be held for 3, 6, or 12 months. The amount
calculator changes only cash projections; it does not change the frozen strategy decision. Old
model entries are not shown as current opportunities, and the screen returns `Wait` when no setup
passes the 80% evidence threshold.
