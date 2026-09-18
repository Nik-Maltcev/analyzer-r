# Resource strategy research

Run date: 2026-09-12. Market prices end on 2026-09-11.

## Scope

Oil and gasoline are excluded. The universe contains physical/proxy exposure to
gold, silver, platinum, palladium, copper and natural gas, plus uranium, lithium,
rare earths, nuclear and clean energy, steel, base metals, agriculture, forestry,
water infrastructure and producer ETFs.

The test covers every holding period from 1 through 12 months. Rules use only
information available before entry. Events do not overlap inside one
instrument/strategy/horizon test. This avoids treating twelve overlapping
one-year positions as twelve independent observations.

## Outcome

- 21,230 historical events.
- 1,683 instrument/strategy/horizon tests.
- 279 tests have at least 20 independent events.
- Zero tests establish an 80%+ probability of a net USD gain of at least 10%.
- The target is **at least** 10%, not an exit fixed at exactly 10%.

| Test | Months | Events | USD >=10% | Any USD profit | Median USD | Median 100k RUB | Worst USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gold buy and hold | 12 | 20 | 55.00% | 70.00% | +12.24% | 114,415 RUB | -22.15% |
| Gold 10-month trend | 8 | 21 | 47.62% | 80.95% | +9.89% | 110,110 RUB | -20.67% |
| Water infrastructure buy and hold | 11 | 20 | 60.00% | 75.00% | +13.01% | 114,940 RUB | -44.35% |
| Palladium buy and hold | 9 | 20 | 60.00% | 65.00% | +12.90% | 104,818 RUB | -29.11% |
| Steel producers 10-month trend | 4 | 28 | 50.00% | 75.00% | +7.93% | 112,188 RUB | -67.65% |
| Gold miners / gold ratio reversion | 4 | 20 | 15.00% | 60.00% | +1.75% | 101,859 RUB | -10.06% |
| Gas glut + falling trend | 2 | 20 | 25.00% | 50.00% | -0.46% | 102,891 RUB | -36.75% |

The gold trend result illustrates the key distinction: it was profitable in
80.95% of independent eight-month events, but reached the requested +10% in only
47.62%. It therefore does not satisfy the user's rule.

Two attractive-looking results have only six independent events: uranium
10-month trend held for 11 months and gas glut/downtrend held for 11 months each
reached +10% in five of six cases. Their familywise-adjusted lower confidence
bound is only 17.93%, so neither is accepted as an 80% strategy.

## Rules tested

- Buy and hold.
- 10-month momentum plus price above the 200-day average.
- Short dip inside a positive 10-month trend.
- Oversold recovery.
- Six-month breakout.
- Top-two cross-resource momentum rotation.
- Three-year rolling mean reversion in gold/silver, platinum/palladium,
  miners/metal, nuclear/uranium and solar/clean-energy ratios.
- CFTC managed-money capitulation and producer accumulation.
- EIA Lower-48 gas shortage/glut relative to its prior five-year seasonal range,
  confirmed by the price trend.

## Data and caveats

- Prices: Yahoo Finance adjusted daily history.
- Positioning: CFTC Disaggregated Futures Only weekly reports.
- Gas storage: EIA Lower 48 weekly working gas.
- Currency: official Bank of Russia USD/RUB.
- Inflation: Rosstat monthly CPI.
- Costs: 0.20% round trip for one leg and 0.40% for a two-leg spread.
- Main result follows the requested zero-tax scenario. A simplified 13% tax
  sensitivity is retained in the event file.
- UNG and continuous commodity exposure contain futures roll effects. A
  historical test is not a promise of future returns.

Machine-readable results are in `data/resource_arbitrage/`.
