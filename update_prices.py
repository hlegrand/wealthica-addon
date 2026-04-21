"""
Live price updater for the Wealthica portfolio dashboard.
Fetches current stock prices from Yahoo Finance (free, no API key),
updates the export JSON, and re-embeds into dashboard.html.

Usage:
    python update_prices.py
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parent
EXPORT_FILE = SCRIPT_DIR / "wealthica-export-2026-04-15.json"
DASHBOARD_FILE = SCRIPT_DIR / "dashboard.html"

# Also check for any wealthica-export-*.json if the dated one doesn't exist
def find_export():
    if EXPORT_FILE.exists():
        return EXPORT_FILE
    for f in sorted(SCRIPT_DIR.glob("wealthica-export-*.json"), reverse=True):
        return f
    return None


def yahoo_symbol(symbol, currency, geo):
    """Convert Wealthica symbol to Yahoo Finance symbol."""
    if not symbol:
        return None
    # Canadian stocks need .TO suffix, dots become dashes (KILO.B -> KILO-B.TO)
    if currency == "cad" and geo == "Canada" and ".TO" not in symbol.upper():
        return symbol.replace(".", "-") + ".TO"
    return symbol


def fetch_quotes(symbols):
    """Fetch live quotes from Yahoo Finance for a list of symbols."""
    if not symbols:
        return {}

    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + ",".join(symbols)
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            results = {}
            for q in data.get("quoteResponse", {}).get("result", []):
                sym = q.get("symbol", "")
                results[sym] = {
                    "price": q.get("regularMarketPrice"),
                    "change": q.get("regularMarketChange"),
                    "changePct": q.get("regularMarketChangePercent"),
                    "high": q.get("regularMarketDayHigh"),
                    "low": q.get("regularMarketDayLow"),
                    "volume": q.get("regularMarketVolume"),
                    "marketCap": q.get("marketCap"),
                    "name": q.get("shortName"),
                    "time": q.get("regularMarketTime"),
                }
            return results
    except urllib.error.HTTPError as e:
        print(f"  Yahoo API error: {e.code} - trying one by one...")
        return fetch_quotes_individually(symbols)
    except Exception as e:
        print(f"  Error fetching quotes: {e}")
        return {}


def fetch_quotes_individually(symbols):
    """Fallback: fetch each symbol one by one."""
    results = {}
    for sym in symbols:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                results[sym] = {
                    "price": meta.get("regularMarketPrice"),
                    "change": None,
                    "changePct": None,
                    "high": meta.get("regularMarketDayHigh"),
                    "low": meta.get("regularMarketDayLow"),
                    "volume": meta.get("regularMarketVolume"),
                    "name": None,
                    "time": meta.get("regularMarketTime"),
                }
            time.sleep(0.3)  # Rate limit
        except Exception as e:
            print(f"  Could not fetch {sym}: {e}")
    return results


def fetch_fx_rate():
    """Fetch USD/CAD exchange rate from Yahoo Finance."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/USDCAD=X?interval=1d&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            return meta.get("regularMarketPrice", 1.37)
    except Exception:
        print("  Could not fetch USD/CAD rate, using 1.37")
        return 1.37


def update_portfolio(export_path):
    """Load the export, fetch live prices, update values."""
    with open(export_path, encoding="utf-8") as f:
        portfolio = json.load(f)

    positions = portfolio.get("positions", [])
    if not positions:
        print("No positions found in export.")
        return None

    # Build symbol mapping: yahoo_sym -> position index
    sym_map = {}
    for i, pos in enumerate(positions):
        sec = pos.get("security", {})
        symbol = sec.get("symbol", "")
        currency = sec.get("currency", "")
        geo = sec.get("geo", "")
        ysym = yahoo_symbol(symbol, currency, geo)
        if ysym:
            sym_map[ysym] = i

    print(f"Fetching live prices for {len(sym_map)} symbols...")
    print(f"  Symbols: {', '.join(sym_map.keys())}")

    # Fetch USD/CAD rate
    usd_cad = fetch_fx_rate()
    print(f"  USD/CAD: {usd_cad:.4f}")

    # Fetch all quotes
    quotes = fetch_quotes(list(sym_map.keys()))
    print(f"  Got quotes for {len(quotes)} symbols")

    # Update positions with live data
    updated = 0
    for ysym, idx in sym_map.items():
        quote = quotes.get(ysym)
        if not quote or not quote.get("price"):
            print(f"  SKIP {ysym}: no price data")
            continue

        pos = positions[idx]
        sec = pos.get("security", {})
        qty = pos.get("quantity", 0)
        live_price = quote["price"]

        # Calculate new market value (convert USD to CAD if needed)
        if sec.get("currency") == "usd":
            live_mv = live_price * qty * usd_cad
        else:
            live_mv = live_price * qty

        old_mv = pos.get("market_value", 0)
        bv = pos.get("book_value", 0)
        new_gain = live_mv - bv
        new_gain_pct = (new_gain / bv) if bv > 0 else 0

        # Update position
        pos["market_value"] = round(live_mv, 2)
        pos["gain_amount"] = round(new_gain, 2)
        pos["gain_percent"] = round(new_gain_pct, 4)

        # Store live price info in security
        sec["last_price"] = live_price
        sec["last_date"] = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()
        if quote.get("change") is not None:
            sec["day_change"] = round(quote["change"], 4)
            sec["day_change_pct"] = round(quote["changePct"], 4)
        if quote.get("high"):
            sec["day_high"] = quote["high"]
        if quote.get("low"):
            sec["day_low"] = quote["low"]
        if quote.get("volume"):
            sec["volume"] = quote["volume"]

        change_str = f" ({quote.get('changePct', 0):+.2f}%)" if quote.get("changePct") else ""
        print(f"  {sec.get('symbol', '?'):8} ${live_price:>10.2f}{change_str}  MV: {fmt_cad(old_mv)} -> {fmt_cad(live_mv)}")
        updated += 1

    portfolio["liveUpdate"] = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()
    portfolio["usdCadRate"] = usd_cad
    print(f"\nUpdated {updated}/{len(sym_map)} positions.")
    return portfolio


def fmt_cad(n):
    return f"${n:,.2f}"


def embed_in_dashboard(portfolio, dashboard_path):
    """Embed the updated portfolio JSON into dashboard.html."""
    with open(dashboard_path, encoding="utf-8") as f:
        html = f.read()

    minified = json.dumps(portfolio, separators=(",", ":"))

    # Replace existing embedded data
    html = re.sub(
        r"<script>var EMBEDDED_DATA=.*?;</script>",
        "<script>var EMBEDDED_DATA=" + minified + ";</script>",
        html,
        flags=re.DOTALL,
    )

    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard updated ({len(minified):,} bytes embedded)")


def main():
    export_path = find_export()
    if not export_path:
        print("No wealthica-export-*.json found. Export from the Wealthica add-on first.")
        sys.exit(1)

    print(f"Loading {export_path.name}...")
    portfolio = update_portfolio(export_path)
    if not portfolio:
        sys.exit(1)

    # Save updated JSON
    out_path = SCRIPT_DIR / "wealthica-export-live.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, separators=(",", ":"))
    print(f"Saved {out_path.name}")

    # Embed in dashboard
    if DASHBOARD_FILE.exists():
        embed_in_dashboard(portfolio, DASHBOARD_FILE)
    else:
        print("dashboard.html not found, skipping embed.")

    print("\nDone! Open dashboard.html to see live prices.")


if __name__ == "__main__":
    main()
