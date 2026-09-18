# Non-oil resource portfolio research

Run date: 2026-09-12. Prices end on 2026-09-11. Oil and gasoline are excluded.

## Method

Eight fixed portfolio rules were tested from January 2012 through August 2026.
Weights are calculated at month-end and applied only to the following month.
The portfolios are long-only, use no leverage and may hold uninvested USD cash.
Trading costs are 0.10% of one-way turnover.

The analysis contains 16,368 rolling windows and 96 portfolio/horizon tests for
every horizon from one through twelve months. A rolling window is useful for the
descriptive rate, but overlapping windows are not treated as independent in the
confidence calculation.

## Main results

| Portfolio | USD CAGR | Volatility | Maximum drawdown | Final 100k RUB | Real value in 2012 RUB |
|---|---:|---:|---:|---:|---:|
| Top-5 trends, inverse volatility | 9.00% | 18.77% | -49.02% | 941,184 RUB | 341,677 RUB |
| 50% macro core / 50% trend | 8.10% | 13.90% | -28.51% | 833,826 RUB | 302,703 RUB |
| 50% defensive / 50% trend | 7.44% | 12.07% | -30.56% | 762,167 RUB | 276,689 RUB |
| Fixed precious/real-assets barbell | 6.64% | 11.91% | -28.05% | 682,875 RUB | 247,904 RUB |
| Defensive trend-to-cash barbell | 5.32% | 7.17% | -13.69% | 568,343 RUB | 206,325 RUB |
| Counterfactual fixed 10% RUB deposit | — | — | — | 404,662 RUB | 146,904 RUB |

The core/trend mix is the most practical compromise in this test. The highest
returning portfolio has materially worse drawdown and turnover. The defensive
barbell has the smallest drawdown but also the lowest return.

No portfolio establishes an 80%+ probability of making at least 10% over any
1–12 month horizon.

| Horizon | Best rule for reaching +10% | Historical rolling rate | Median USD return | Worst window |
|---|---|---:|---:|---:|
| 3 months | Top-3 rotation | 20.69% | +0.84% | -29.98% |
| 6 months | Top-3 rotation | 30.41% | +3.54% | -38.73% |
| 12 months | Top-5 inverse-volatility trend | 47.27% | +8.29% | -44.51% |

## Current model allocation for 100k RUB

The recommended core/trend mix on 2026-09-11 is:

| Exposure | Weight | Amount |
|---|---:|---:|
| Agricultural commodities (DBA) | 22.50% | 22,500 RUB |
| Base metals (DBB) | 20.05% | 20,050 RUB |
| Gold (GLD) | 15.00% | 15,000 RUB |
| Copper (CPER) | 14.44% | 14,440 RUB |
| Steel producers (SLX) | 8.01% | 8,010 RUB |
| Nuclear-energy chain (NLR) | 7.50% | 7,500 RUB |
| Water infrastructure (PHO) | 7.50% | 7,500 RUB |
| Silver (SLV) | 5.00% | 5,000 RUB |

This is a model allocation, not a promise or personalised recommendation. It is
particularly sensitive to access, spreads, taxes, sanctions and custody risks
for a Russian investor. The 10% deposit comparison assumes a constant rate for
the whole period and is a counterfactual benchmark, not historical deposit data.

Machine-readable outputs are in `data/resource_portfolio/`.
