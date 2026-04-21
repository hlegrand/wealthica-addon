"""
Local server for the portfolio dashboard.
- Serves dashboard.html
- /quotes endpoint for live Yahoo Finance prices
- /chat endpoint for AI assistant (Claude via LiteLLM gateway)

Usage:
    python server.py
Then open http://localhost:8777
"""

import json
import http.server
import socketserver
import urllib.request
import urllib.error
import threading
import time
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

PORT = 8777
DIR = Path(__file__).resolve().parent
CACHE = {}
CACHE_TTL = 25

# ── LiteLLM config ──
LITELLM_BASE_URL = "https://litellm.ubisoft.org/"
LLM_MODEL = "claude-haiku-4-5-20251001"


def load_llm_config():
    """Load LLM API key from env or .env file."""
    api_key = os.environ.get("LITELLM_API_KEY", "")
    base_url = os.environ.get("LITELLM_BASE_URL", LITELLM_BASE_URL)

    if not api_key:
        env_file = DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("LITELLM_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("LITELLM_BASE_URL="):
                    base_url = line.split("=", 1)[1].strip().strip('"').strip("'")

    return api_key, base_url

# ── Portfolio context for AI ──
def load_portfolio_context():
    """Load portfolio data and format as context for Claude. Prefer live sync file."""
    live = DIR / "wealthica-export-live.json"
    if live.exists():
        with open(live, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        for f in sorted(DIR.glob("wealthica-export*.json"), reverse=True):
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            break
        else:
            return "No portfolio data available."

    lines = ["## Current Portfolio\n"]
    tm, tb = 0, 0
    for p in data.get("positions", []):
        s = p.get("security", {})
        mv = p.get("market_value", 0)
        bv = p.get("book_value", 0)
        gl = mv - bv
        glp = (gl / bv * 100) if bv > 0 else 0
        tm += mv
        tb += bv
        sym = s.get("symbol", "?")
        sec = s.get("sector", "?")
        ind = s.get("industry", "?")
        cur = s.get("currency", "?")
        qty = p.get("quantity", 0)
        lines.append(
            f"- **{sym}**: {qty} shares | MV=${mv:,.2f} | BV=${bv:,.2f} | "
            f"G/L={gl:+,.2f} ({glp:+.1f}%) | {sec} | {ind} | {cur.upper()}"
        )

    gl_total = tm - tb
    glp_total = (gl_total / tb * 100) if tb > 0 else 0
    lines.append(f"\n**Total:** MV=${tm:,.2f} | BV=${tb:,.2f} | G/L={gl_total:+,.2f} ({glp_total:+.1f}%)")
    lines.append(f"**Account:** TFSA (CELI) + Non-registered | Currency: CAD")
    lines.append(f"**Total invested since 2021:** ~$5,939 CAD | **Dividends received:** ~$75 CAD")

    # Concentration
    lines.append("\n## Concentration")
    for p in sorted(data.get("positions", []), key=lambda x: x.get("market_value", 0), reverse=True):
        sym = p.get("security", {}).get("symbol", "?")
        w = (p.get("market_value", 0) / tm * 100) if tm > 0 else 0
        lines.append(f"- {sym}: {w:.1f}%")

    return "\n".join(lines)


SYSTEM_PROMPT = """You are Henri's portfolio AI assistant. You have deep expertise in trading and investment analysis.

You have access to Henri's real Wealthsimple portfolio data below. Use it to give personalized, actionable advice.

{portfolio}

## Your Trading Skills

You are an expert in these domains. Use the right one based on the question:

### 1. Technical Analysis
Analyze price action, trends, support/resistance, moving averages, volume, chart patterns.
- Identify trend direction (up/down/sideways) and strength
- Key support and resistance levels with specific prices
- Moving average analysis (50-day, 200-day crossovers)
- Develop 2-4 probabilistic scenarios with trigger levels
- Always specify invalidation levels for each scenario

### 2. Sector Rotation & Market Cycle
Assess current market cycle phase and sector positioning:
- **Early Cycle Recovery**: Financials, Consumer Discretionary, Industrials lead
- **Mid Cycle Expansion**: Technology, Communication Services outperform
- **Late Cycle**: Energy, Materials, Healthcare defensive
- **Recession**: Utilities, Consumer Staples, Healthcare safe havens
Analyze sector performance across 1-week and 1-month timeframes. Flag overweight/underweight sectors.

### 3. US Stock Analysis
Comprehensive equity research:
- Fundamental: revenue growth, margins, cash flow, competitive moat, management quality
- Valuation: P/E, PEG, EV/EBITDA, Price/Sales vs peers and historical average
- Technical: trend, momentum, volume confirmation
- Bull/bear cases with specific catalysts and risks
- Investment thesis with price targets and timeframes

### 4. Market News Impact
Analyze recent market-moving events:
- Score impact: (Price Impact x Breadth Multiplier) + Forward Modifier
- Categories: monetary policy, economic data, earnings, geopolitical, commodities
- Multi-asset perspective: equities, bonds, commodities, currencies
- Forward-looking implications and positioning advice

### 5. Market Breadth (0-100 score)
Quantify overall market health:
- 80-100: Full exposure (90-100%)
- 60-79: Normal (75-90%)
- 40-59: Reduced (60-75%)
- 20-39: Defensive (40-60%)
- 0-19: Capital preservation (25-40%)

### 6. Position Sizing
Calculate risk-based position sizes:
- Fixed Fractional: risk X% per trade (default 1%)
- ATR-Based: volatility-adjusted sizing
- Kelly Criterion: mathematical optimization (use half-Kelly)
- Apply constraints: max 10% single position, max 30% single sector
- Account size context: Henri has ~$6,000 CAD portfolio

### 7. Diversification & Risk
Evaluate portfolio risk:
- HHI concentration index
- Sector diversification score
- Currency exposure (CAD vs USD)
- Correlation between holdings
- Suggest specific improvements

### 8. Opportunity Screening
Find new investment ideas:
- Screen for stocks matching criteria (growth, value, dividend, momentum)
- Focus on sectors Henri is underweight in (Healthcare, Financials, Energy, Consumer)
- Canadian and US markets
- Consider TFSA-friendly investments (no withholding tax on Canadian dividends)

### 9. Macro Regime Detection
Identify structural market regime (1-2 year horizon):
- 5 regimes: Concentration, Broadening, Contraction, Inflationary, Transitional
- Cross-asset analysis: yield curve, credit spreads, equity-bond correlation
- Portfolio posture recommendations based on regime

## Response Guidelines
- Be direct and actionable — give specific numbers, prices, percentages
- Use markdown formatting: **bold** for key points, tables for comparisons, bullet lists
- Always consider Henri's specific portfolio when answering
- Quantify risks and opportunities in dollar terms relative to his $6K portfolio
- When suggesting buys, include position size recommendation
- Flag any tax implications (TFSA vs non-registered)
- If asked about a specific stock, provide bull/bear case with price levels
- Use CAD as default currency, note USD conversions
- Be honest about uncertainty — give probability ranges, not certainties
"""


# ── Claude API Chat ──
def handle_chat(handler):
    """Handle /chat POST request with Claude API streaming."""
    content_length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(content_length)
    req = json.loads(body.decode())

    user_msg = req.get("message", "")
    history = req.get("history", [])

    if not user_msg:
        handler.send_json({"error": "Empty message"}, 400)
        return

    api_key, base_url = load_llm_config()

    if not api_key:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        handler.wfile.write(b"data: {\"error\": \"No API key found. Create a .env file with LITELLM_API_KEY=...\"}\n\n")
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
        return

    # Build messages
    portfolio_ctx = load_portfolio_context()
    system = SYSTEM_PROMPT.replace("{portfolio}", portfolio_ctx)

    messages = []
    for h in history[-10:]:  # Keep last 10 messages for context
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_msg})

    # Call Claude API with streaming
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    try:
        sys.stdout.write(f'  [chat] Calling {LLM_MODEL}...\n')
        sys.stdout.flush()

        answer = call_llm(api_key, base_url, system, messages)

        sys.stdout.write(f'  [chat] Got {len(answer)} chars\n')
        sys.stdout.flush()

        try:
            data = json.dumps({"text": answer})
            handler.wfile.write(f"data: {data}\n\n".encode())
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    except Exception as e:
        sys.stdout.write(f'  [chat] ERROR: {type(e).__name__}: {e}\n')
        sys.stdout.flush()
        try:
            err = json.dumps({"error": str(e)})
            handler.wfile.write(f"data: {err}\n\n".encode())
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR), **kwargs)

    def do_GET(self):
        try:
            if self.path == '/':
                self.path = '/dashboard.html'
                return super().do_GET()
            if self.path.startswith('/quotes?'):
                return self.handle_quotes()
            return super().do_GET()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_POST(self):
        try:
            if self.path == '/chat':
                return handle_chat(self)
            if self.path == '/sync':
                return self.handle_sync()
            self.send_json({"error": "Not found"}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def handle_sync(self):
        """Receive portfolio data from Wealthica add-on and save it."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode())
            # Save as latest export
            out = DIR / "wealthica-export-live.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))

            # Also save dated version
            today = datetime.now().strftime("%Y-%m-%d")
            dated = DIR / f"wealthica-export-{today}.json"
            with open(dated, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            n_pos = len(data.get("positions", []))
            n_tx = len(data.get("transactions", []))
            sys.stdout.write(f'  [sync] Received {n_pos} positions, {n_tx} transactions\n')
            sys.stdout.flush()

            # Update embedded data in dashboard
            self.update_dashboard_embed(data)

            self.send_json({"ok": True, "positions": n_pos, "transactions": n_tx})
        except Exception as e:
            sys.stdout.write(f'  [sync] ERROR: {e}\n')
            sys.stdout.flush()
            self.send_json({"error": str(e)}, 500)

    def update_dashboard_embed(self, data):
        """Re-embed fresh data into dashboard.html."""
        import re as _re
        dashboard = DIR / "dashboard.html"
        if not dashboard.exists():
            return
        html = dashboard.read_text(encoding="utf-8")
        minified = json.dumps(data, separators=(",", ":"))
        old_pattern = r"<script>var EMBEDDED_DATA=.*?;</script>"
        new_block = "<script>var EMBEDDED_DATA=" + minified + ";</script>"
        # Use string find/replace to avoid regex issues with special chars
        start = html.find("<script>var EMBEDDED_DATA=")
        if start >= 0:
            end = html.find(";</script>", start) + len(";</script>")
            html = html[:start] + new_block + html[end:]
            dashboard.write_text(html, encoding="utf-8")
            sys.stdout.write(f'  [sync] Dashboard embed updated ({len(minified)} bytes)\n')
            sys.stdout.flush()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def handle_quotes(self):
        query = self.path.split('?', 1)[1] if '?' in self.path else ''
        params = dict(p.split('=', 1) for p in query.split('&') if '=' in p)
        symbols = params.get('symbols', '').split(',')
        symbols = [s.strip() for s in symbols if s.strip()]

        if not symbols:
            self.send_json({'error': 'No symbols'}, 400)
            return

        results = {}
        for sym in symbols:
            if sym in CACHE and (time.time() - CACHE[sym]['t']) < CACHE_TTL:
                results[sym] = CACHE[sym]['data']
                continue
            data = fetch_yahoo(sym)
            if data:
                CACHE[sym] = {'t': time.time(), 'data': data}
                results[sym] = data

        self.send_json({
            'quotes': results,
            'time': datetime.now(timezone.utc).isoformat(),
        })

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args if args else str(fmt)
        except Exception:
            msg = str(args[0]) if args else ''
        if '/quotes' in msg or '/chat' in msg:
            ts = datetime.now().strftime('%H:%M:%S')
            sys.stdout.write(f'  [{ts}] {msg}\n')
            sys.stdout.flush()


def call_llm(api_key, base_url, system, messages):
    """Call LiteLLM gateway using urllib (bypasses OpenAI SDK SSL issues)."""
    url = base_url.rstrip('/') + '/v1/chat/completions'
    payload = json.dumps({
        "model": LLM_MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "system", "content": system}] + messages,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]


def fetch_yahoo(symbol):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d'
    headers = {'User-Agent': 'Mozilla/5.0'}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            meta = data.get('chart', {}).get('result', [{}])[0].get('meta', {})
            return {
                'price': meta.get('regularMarketPrice'),
                'prevClose': meta.get('chartPreviousClose') or meta.get('previousClose'),
                'high': meta.get('regularMarketDayHigh'),
                'low': meta.get('regularMarketDayLow'),
                'volume': meta.get('regularMarketVolume'),
            }
    except Exception as e:
        sys.stdout.write(f'  Yahoo error for {symbol}: {e}\n')
        sys.stdout.flush()
        return None


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    api_key, base_url = load_llm_config()

    print(f'Portfolio Dashboard Server')
    print(f'Serving from: {DIR}')
    print(f'LLM: {LLM_MODEL} via {base_url}')
    print(f'API Key: {"ready" if api_key else "NOT SET — add LITELLM_API_KEY to .env"}')
    print(f'Open: http://localhost:{PORT}')
    print(f'Press Ctrl+C to stop\n')

    server = ThreadedHTTPServer(('127.0.0.1', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
        server.server_close()
