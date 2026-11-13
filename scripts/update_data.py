#!/usr/bin/env python3
"""
War Premium -- daily data refresh.

Runs on GitHub's servers (GitHub Actions) every weekday evening. It downloads
open-source data and writes it to data/data.json, which the web page reads.

Series pulled
  UST10  10-year Treasury par yield (CMT), from Treasury.gov  (fallback: FRED DGS10)
  UST3M  3-month Treasury bill yield (CMT), same Treasury file  (fallback: FRED DGS3MO); the risk-free rate
  TP10   10-year term premium, Adrian-Crump-Moench (ACM) model, from the New York Fed
         (column ACMTP10; fallback: the same series on FRED, ACMTP10)
  RTX    RTX Corp. (Raytheon's parent) daily prices
  ITA    iShares U.S. Aerospace & Defense ETF daily prices
  IVV    iShares Core S&P 500 ETF daily prices (the market benchmark)

Stock prices: Yahoo Finance adjusted close (includes dividends) is tried first for
all three tickers; if any ticker fails, all three fall back together to Nasdaq.com,
then Stooq, so the three always share one source and one price basis.

If a source fails, the previous day's data for that series is kept and marked
"stale" so the page can say so. Uses the Python standard library plus xlrd
(to read the New York Fed's .xls file; installed by the workflow).
"""

import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.request

START = dt.date(2021, 10, 1)  # a month before the page's 5-year window, for look-back room
TODAY = dt.datetime.now(dt.timezone.utc).date()
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data", "data.json")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

STOCKS = {"RTX": "stocks", "ITA": "etf", "IVV": "etf"}  # ticker -> Nasdaq asset class


# ---------------------------------------------------------------- helpers
def get(url, headers=None, tries=3, timeout=40):
    """Download a URL and return its text, retrying with a pause on failure."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 -- log and retry any network error
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


def check(points, name, max_age_days):
    """Sanity-check a downloaded series before trusting it."""
    if len(points) < 300:
        raise ValueError(f"{name}: only {len(points)} observations")
    last = dt.date.fromisoformat(points[-1][0])
    if (TODAY - last).days > max_age_days:
        raise ValueError(f"{name}: last observation {last} is older than {max_age_days} days")
    if any((not math.isfinite(v)) for _, v in points):
        raise ValueError(f"{name}: non-numeric values")
    return points


def clean(points):
    """Sort by date, drop duplicates and anything before START."""
    d = {p[0]: p[1] for p in points if p[0] >= START.isoformat()}
    return [[k, d[k]] for k in sorted(d)]


# ---------------------------------------------------------------- rates
def treasury_curve():
    """Return {'10 Yr': points, '3 Mo': points} from Treasury's daily par yield curve files."""
    cols = {"10 Yr": [], "3 Mo": []}
    for year in range(START.year, TODAY.year + 1):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
            f"&field_tdr_date_value={year}&page&_format=csv"
        )
        for row in csv.DictReader(io.StringIO(get(url))):
            d = dt.datetime.strptime(row["Date"].strip(), "%m/%d/%Y").date().isoformat()
            for c in cols:
                v = (row.get(c) or "").strip()
                if v:
                    cols[c].append([d, round(float(v), 4)])
    return {c: check(clean(pts), f"Treasury {c}", 10) for c, pts in cols.items()}


def nyfed_acm_tp10():
    """ACM 10-year term premium straight from the New York Fed's data file (.xls)."""
    import xlrd  # installed by the GitHub workflow
    url = "https://www.newyorkfed.org/medialibrary/media/research/data_indicators/ACMTermPremium.xls"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    else:
        raise RuntimeError(f"GET failed: {url} ({last})")
    book = xlrd.open_workbook(file_contents=raw)
    sheets = sorted(book.sheets(), key=lambda sh: "daily" not in sh.name.lower())  # daily sheet first
    for sh in sheets:
        for hr in range(min(sh.nrows, 20)):
            hdr = [str(v).strip().upper() for v in sh.row_values(hr)]
            if "ACMTP10" in hdr and "DATE" in hdr:
                dcol, vcol = hdr.index("DATE"), hdr.index("ACMTP10")
                pts = []
                for r in range(hr + 1, sh.nrows):
                    dv, vv = sh.cell_value(r, dcol), sh.cell_value(r, vcol)
                    if vv in ("", None):
                        continue
                    if isinstance(dv, float):
                        d = xlrd.xldate_as_datetime(dv, book.datemode).date()
                    else:
                        d = None
                        for f in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%y"):
                            try:
                                d = dt.datetime.strptime(str(dv).strip(), f).date()
                                break
                            except ValueError:
                                pass
                    if d:
                        pts.append([d.isoformat(), round(float(vv), 4)])
                if len(pts) > 300:
                    return check(clean(pts), "TP10 (New York Fed ACM)", 45)
    raise ValueError("ACMTP10 column not found in the New York Fed file")


def fred(series_id, max_age_days):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={START.isoformat()}"
    rows = list(csv.reader(io.StringIO(get(url, tries=2, timeout=90))))
    pts = []
    for r in rows[1:]:  # first row is the header: observation_date (or DATE), SERIES
        if len(r) >= 2 and r[1].strip() not in ("", "."):
            pts.append([r[0].strip(), round(float(r[1]), 4)])
    return check(clean(pts), f"{series_id} (FRED)", max_age_days)


# ---------------------------------------------------------------- stocks
def yahoo(sym):
    p1 = int(dt.datetime(START.year, START.month, START.day, tzinfo=dt.timezone.utc).timestamp())
    p2 = int(time.time()) + 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}"
        "&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
    )
    res = json.loads(get(url))["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", -18000)
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    pts = []
    for ts, v in zip(res["timestamp"], adj):
        if v is not None:
            d = dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date().isoformat()
            pts.append([d, round(float(v), 4)])
    return check(clean(pts), f"{sym} (Yahoo)", 10)


def nasdaq(sym):
    url = (
        f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={STOCKS[sym]}"
        f"&fromdate={START.isoformat()}&todate={TODAY.isoformat()}&limit=9999"
    )
    js = json.loads(get(url, {"Accept": "application/json, text/plain, */*"}))
    pts = []
    for row in js["data"]["tradesTable"]["rows"]:
        d = dt.datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat()
        pts.append([d, round(float(row["close"].replace("$", "").replace(",", "")), 4)])
    return check(clean(pts), f"{sym} (Nasdaq)", 10)


def stooq(sym):
    txt = get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d")
    pts = []
    for row in csv.DictReader(io.StringIO(txt)):
        if row.get("Close"):
            pts.append([row["Date"], round(float(row["Close"]), 4)])
    return check(clean(pts), f"{sym} (Stooq)", 10)


STOCK_SOURCES = [
    ("Yahoo Finance", "adjusted close (includes dividends and splits)", "https://finance.yahoo.com/", yahoo),
    ("Nasdaq.com", "closing price (price only; excludes dividends)", "https://www.nasdaq.com/market-activity", nasdaq),
    ("Stooq", "closing price (split-adjusted)", "https://stooq.com/", stooq),
]


# ---------------------------------------------------------------- main
def main():
    old = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = json.load(f).get("series", {})

    out = {}
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def keep_old(key, err):
        print(f"WARNING {key}: {err}", file=sys.stderr)
        if key in old:
            prev = dict(old[key])
            prev["status"] = f"stale: refresh failed {now}; showing data last fetched earlier"
            out[key] = prev
        else:
            print(f"ERROR {key}: no previous data to fall back on", file=sys.stderr)

    # Treasury yields: 10-year (for duration) and 3-month bill (risk-free rate for Sharpe)
    tsy_url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"
    curve = None
    try:
        curve = treasury_curve()
        srcs = {"10 Yr": ("U.S. Treasury, Daily Treasury Par Yield Curve Rates (10 Yr)", tsy_url),
                "3 Mo": ("U.S. Treasury, Daily Treasury Par Yield Curve Rates (3 Mo)", tsy_url)}
    except Exception as e:  # noqa: BLE001
        print(f"Treasury failed ({e}); trying FRED DGS10 / DGS3MO", file=sys.stderr)
        try:
            curve = {"10 Yr": fred("DGS10", 10), "3 Mo": fred("DGS3MO", 10)}
            srcs = {"10 Yr": ("U.S. Treasury 10-Year CMT via FRED (DGS10)", "https://fred.stlouisfed.org/series/DGS10"),
                    "3 Mo": ("U.S. Treasury 3-Month CMT via FRED (DGS3MO)", "https://fred.stlouisfed.org/series/DGS3MO")}
        except Exception as e2:  # noqa: BLE001
            keep_old("UST10", e2)
            keep_old("UST3M", e2)
    if curve:
        out["UST10"] = {"label": "10-year Treasury par yield", "units": "%/yr, bond-equivalent (semiannual)",
                        "source": srcs["10 Yr"][0], "source_url": srcs["10 Yr"][1], "status": "ok", "data": curve["10 Yr"]}
        out["UST3M"] = {"label": "3-month Treasury bill yield (risk-free rate)", "units": "%/yr, bond-equivalent",
                        "source": srcs["3 Mo"][0], "source_url": srcs["3 Mo"][1], "status": "ok", "data": curve["3 Mo"]}

    # 10-year term premium (ACM, New York Fed). Allow up to 45 days of publication lag.
    tp = None
    try:
        tp = (nyfed_acm_tp10(), "Federal Reserve Bank of New York, Adrian-Crump-Moench (ACM) model (ACMTP10)",
              "https://www.newyorkfed.org/research/data_indicators/term-premia-tabs")
    except Exception as e:  # noqa: BLE001
        print(f"New York Fed ACM failed ({e}); trying FRED ACMTP10", file=sys.stderr)
        try:
            tp = (fred("ACMTP10", 45), "Federal Reserve Bank of New York, ACM model, via FRED (ACMTP10)",
                  "https://fred.stlouisfed.org/series/ACMTP10")
        except Exception as e2:  # noqa: BLE001
            keep_old("TP10", e2)
    if tp:
        out["TP10"] = {"label": "10-year term premium (ACM)", "units": "percentage points",
                       "source": tp[1], "source_url": tp[2], "status": "ok", "data": tp[0]}

    # Stocks: all three tickers from the same source
    got = None
    for name, basis, url, fn in STOCK_SOURCES:
        try:
            got = {s: fn(s) for s in STOCKS}
            print(f"Stocks: using {name}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"Stocks: {name} failed ({e})", file=sys.stderr)
    labels = {"RTX": "RTX Corp. (Raytheon parent)", "ITA": "iShares U.S. Aerospace & Defense ETF",
              "IVV": "iShares Core S&P 500 ETF"}
    for s in STOCKS:
        if got:
            out[s] = {"label": labels[s], "units": "$/share, " + basis, "source": name,
                      "source_url": url, "status": "ok", "data": got[s]}
        else:
            keep_old(s, "all stock sources failed")

    if not out:
        print("ERROR: nothing fetched and no previous data", file=sys.stderr)
        sys.exit(1)

    for k, v in out.items():
        v["last"] = v["data"][-1][0]
        print(f"{k:6s} {len(v['data']):5d} obs  last {v['last']}  {v['status']}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"generated_utc": now, "start": START.isoformat(), "series": out},
                  f, separators=(",", ":"))


if __name__ == "__main__":
    main()
