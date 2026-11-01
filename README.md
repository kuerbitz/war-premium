# War Premium

**Did the Iran war move the bond market?**
Live site: https://kuerbitz.github.io/war-premium/

This page tracks defense stocks (RTX, ITA) against the S&P 500 (IVV). It then tests whether those moves line up with the 10-year Treasury term premium and with a Treasury duration measure. It was built for Duke ENGRMGMT/FINTECH 534 (Fall 2026).

## What's on the page
1. **Defense stocks vs. the market:** RTX, ITA and IVV indexed to 100, with four events marked: the Ukraine invasion, the start of the Iran war, the FY2027 Tomahawk budget request, and the $22.9B Tomahawk contract.
2. **The bond side:** the 10-year term premium (Kim–Wright) and the modified duration of a 10-year Treasury, computed from its yield.
3. **The test:** a weekly scatter plot of term-premium changes against defense stocks' excess return over IVV, with an OLS line of best fit (slope, intercept, R²).
4. **Event windows:** term premium, duration and stock log returns over the N trading days after each event.
5. **Findings:** sentences generated from the current data every time the page loads.

## How it stays current
`.github/workflows/update-and-deploy.yml` runs every weekday at 22:17 UTC on GitHub's servers. It runs
`scripts/update_data.py`, which downloads open-source data and saves it to `data/data.json`. The workflow then republishes the site. The page reads `data/data.json` and does every calculation in the browser.

To refresh the data by hand, open the **Actions** tab, select **Update data and deploy site**, and click **Run workflow**.

## Data sources (all public)
| Series | Source |
|---|---|
| 10-yr Treasury par yield | U.S. Treasury, Daily Treasury Par Yield Curve Rates (fallback: FRED DGS10) |
| 10-yr term premium | Federal Reserve Board, Kim–Wright model, via FRED `THREEFYTP10` |
| RTX, ITA, IVV | Yahoo Finance adjusted close (fallbacks: Nasdaq.com, Stooq). All three tickers always come from the same source. |

No licensed or non-public data is used.
