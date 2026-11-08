#!/usr/bin/env python3
"""Stock news alerts -> Telegram. Runs on GitHub Actions (every few minutes) or locally.

Watchlist alerts: new news about your stocks (+ big price moves). Up to 1000 stocks.
Market-wide alerts (/market on):
  🚨 critical news about ANY public company (M&A, bankruptcy, FDA, halts, big contracts...)
  🌍 market & sector news (Fed, inflation, jobs, tariffs, oil, export controls, big market moves...)
Control it from Telegram: /add /remove /list /market /level /help

Usage:
  python3 stock_news.py              # one check (what GitHub Actions runs)
  python3 stock_news.py --loop       # keep running locally
  python3 stock_news.py --test-telegram
Secrets: env TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS (comma separated), or local_secrets.json.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
STATE = BASE / "state.json"
SECRETS = BASE / "local_secrets.json"
UA = {"User-Agent": "Mozilla/5.0 (StockNewsAlerts)"}

# ---------- limits ----------
MAX_STOCKS = 1000          # total watchlist size
MAX_PER_COMMAND = 1000     # stocks in one /add or /remove
NEWS_PER_RUN = 120         # stocks whose news is checked per run (rotates through the list)
THREADS = 10
MAX_MARKET_ALERTS = 10     # 🚨 + 🌍 alerts per run
MAX_SINGLE_ALERTS = 15     # above this, watchlist alerts are grouped into a digest
SEEN_DAYS = 10             # remember sent links this long
MARKET_MAX_AGE_HOURS = 6
TG_LIMIT = 3900            # Telegram allows 4096 chars per message

# ---------- watchlist scoring ----------
KEYWORDS = {
    3: ["offering", "dilution", "acquisition", "acquire", "merger", "buyout", "takeover",
        "bankruptcy", "delist", "sec investigation", "lawsuit", "fda", "approval", "approved",
        "contract", "offtake", "share issuance", "shares issued", "guidance", "earnings",
        "downgrade", "upgrade", "reverse split", "halt"],
    2: ["partnership", "agreement", "funding", "grant", "loan", "price target", "results",
        "revenue", "short seller", "insider", "sells", "buys", "invest", "stake", "ceo", "cfo",
        "resign", "permit", "government", "tariff"],
    1: ["announces", "update", "rally", "surge", "plunge", "soar", "drop", "jump", "falls"],
}

# ---------- 🚨 company-level critical events ----------
CRITICAL = [
    "to acquire", "to be acquired", "agrees to buy", "agreed to buy", "agrees to sell",
    "definitive agreement", "merger agreement", "to merge", "takeover bid", "tender offer",
    "buyout offer", "all-cash deal", "files for bankruptcy", "chapter 11", "going concern",
    "fda approves", "fda approval", "fda rejects", "complete response letter", "clinical hold",
    "trading halt", "halted trading", "delisted", "delisting notice", "sec charges",
    "accounting fraud", "restatement", "short seller report", "cuts guidance",
    "lowers guidance", "raises guidance", "profit warning", "ceo resigns", "ceo steps down",
    "ceo ousted", "ceo fired", "massive recall", "data breach", "antitrust lawsuit",
    "doj sues", "ftc sues", "blocks merger", "reverse stock split", "stock split",
]
CONTRACT = ["contract", "awarded", "deal", "order", "agreement"]  # + amount >= BIG_CONTRACT_USD
BIG_CONTRACT_USD = 100_000_000
MONEY_RE = re.compile(r"(?:US|A|C)?[$€£]\s?(\d+(?:[.,]\d+)?)\s?(trillion|billion|bln|bn|million|mln|mn|[bmt])\b", re.I)
NOISE = ["class action", "investor notice", "investors who", "shareholder alert",
         "reminds investors", "law firm", "rosen", "pomerantz", "levi & korsinsky",
         "on behalf of investors", "lead plaintiff", "investigation on behalf", "deadline"]
FINANCIAL_SITES = {"reuters.com", "cnbc.com", "bloomberg.com", "wsj.com", "marketwatch.com",
                   "finance.yahoo.com", "investing.com", "barrons.com", "ft.com", "seekingalpha.com",
                   "businesswire.com", "globenewswire.com", "prnewswire.com", "fiercebiotech.com",
                   "biopharmadive.com", "stocktitan.net", "benzinga.com", "fool.com", "thestreet.com",
                   "tradingview.com"}
TICKER_RE = re.compile(r"\((?:NASDAQ|NYSE|NYSE American|AMEX|OTC\w*|TSX|LSE|ASX)\s*:\s*([A-Z.]{1,6})\)", re.I)

# ---------- 🌍 market & sector events ----------
# Only hard-news outlets for macro (no "5 stocks to buy" sites)
MACRO_SITES = {"reuters.com", "bloomberg.com", "cnbc.com", "wsj.com", "ft.com", "marketwatch.com",
               "barrons.com", "apnews.com"}
V_UP = r"(rises?|rose|jumps?|jumped|surges?|surged|soars?|soared|climbs?|climbed|spikes?|spiked|accelerat\w*|hotter)"
V_DOWN = r"(falls?|fell|drops?|dropped|plunges?|plunged|tumbles?|tumbled|slumps?|slumped|sinks?|sank|cools?|cooled|slows?|slowed|eases?|eased|softer)"
MOVE = rf"({V_UP}|{V_DOWN})"
MACRO_EVENTS = [
    (rf"\b(fed|fomc|federal reserve|powell)\b.*\b(cuts?|raises?|hikes?|holds?|keeps?|leaves?|lowers?)\b.*\brates?\b", "Fed rate decision"),
    (r"\b(fed|federal reserve)\b.*\brates? (cut|hike|decision)\b.*\b(announced|delivers?|approves?)\b", "Fed rate decision"),
    (rf"\b(cpi|consumer prices?|inflation|pce|producer prices?|ppi)\b.*\b{MOVE}\b", "Inflation data"),
    (rf"\b(payrolls|jobs report|job growth|unemployment rate|jobless claims|employers added|economy added)\b", "Jobs data"),
    (rf"\b(gdp|economy)\b.*\b(shrank|shrinks|contracts?|contracted|grew|grows|expanded|expands)\b", "GDP"),
    (r"\b(recession)\b.*\b(enters?|entered|confirmed|officially)\b", "Recession"),
    (r"\b(imposes?|announces?|slaps?|raises?|hikes?|doubles?|new|retaliatory|retaliat\w*|threatens?|expands?|lifts?|suspends?|scraps?|delays?)\b.*\btariffs?\b", "Tariffs"),
    (r"\btariffs?\b.*\b(on|against)\b.*\b(imports?|goods|us|u\.s\.|china|eu|canada|mexico|japan|india)\b", "Tariffs"),
    (r"\bexport (controls?|bans?|curbs|restrictions|limits)\b", "Export controls"),
    (r"\b(new|imposes?|announces?|expands?|lifts?|eases?)\b.*\bsanctions?\b", "Sanctions"),
    (r"\bopec\+?\b.*\b(cuts?|boosts?|raises?|agrees?|output|production|hike)\b", "OPEC"),
    (rf"\b(oil|crude|brent|natural gas)\b.*\b{MOVE}\b", "Oil & gas prices"),
    (r"\bgovernment shutdown\b", "Government shutdown"),
    (r"\bdebt (ceiling|limit)\b", "Debt ceiling"),
    (r"\b(credit rating|aaa rating)\b.*\b(us|u\.s\.|united states|treasury)\b|\bdowngrades? (the )?(us|u\.s\.)\b", "US credit rating"),
    (r"\b(bank (failure|collapse|run)|seized by regulators|fdic takes over)\b", "Bank stress"),
    (rf"\b(dow|s&p 500|s&p|nasdaq|wall street|stocks|global markets|markets)\b.*\b(plunges?|plunged|tumbles?|tumbled|crash\w*|sinks?|sank|slumps?|slumped|soars?|soared|surges?|surged|sell-?off|rout|record high|worst day|best day)\b", "Big market move"),
    (r"\b(treasury yields?|10-year yield|bond yields?)\b.*\b(jumps?|surges?|soars?|spikes?|plunges?|tumbles?|highest|lowest)\b", "Bond yields"),
    (r"\b(bitcoin|crypto)\b.*\b(plunges?|tumbles?|crash\w*|soars?|surges?|record)\b", "Crypto move"),
    (r"\b(antitrust|regulators?|congress|senate|white house|eu)\b.*\b(bans?|blocks?|passes|approves?|probe|crackdown|rules?)\b.*\b(ai|chips?|big tech|banks?|crypto|drug|pharma|social media|ev|energy)\b", "Regulation"),
]
MACRO_EVENTS = [(re.compile(p, re.I), name) for p, name in MACRO_EVENTS]
# Opinion / how-to / previews – not events
OPINION = re.compile(r"\?|\b(should|could|might|would|may|will|how to|here's|here is|what to|what's|why|"
                     r"i'm|i am|i'd|my|you|your|best|top \d|\d+ (stocks|ways|things)|to buy|to watch|"
                     r"opinion|analysis|explainer|podcast|live updates|preview|ahead of|expected to|"
                     r"forecast|outlook|week ahead|morning brief|newsletter)\b", re.I)
SECTORS = [
    ("Semiconductors", r"\b(chips?|semiconductors?|chipmakers?|foundry)\b"),
    ("Energy", r"\b(oil|crude|opec|natural gas|lng|refiners?|brent)\b"),
    ("Banks & finance", r"\b(banks?|lenders?|banking|credit card)\b"),
    ("Healthcare & pharma", r"\b(drug|pharma\w*|biotech|medicare|medicaid|health insurers?|vaccines?)\b"),
    ("Defense", r"\b(defense|pentagon|military|weapons|nato)\b"),
    ("Critical minerals & mining", r"\b(rare earths?|lithium|critical minerals?|cobalt|nickel|copper|uranium|mining|miners)\b"),
    ("Autos & EV", r"\b(autos?|automakers?|cars?|evs?|electric vehicles?)\b"),
    ("Crypto", r"\b(bitcoin|crypto\w*|ether)\b"),
    ("Tech & AI", r"\b(ai|big tech|tech|software|cloud|social media)\b"),
    ("Real estate", r"\b(mortgage|housing|home sales|real estate)\b"),
    ("Retail & consumer", r"\b(retail\w*|consumer spending|shoppers)\b"),
    ("Airlines & travel", r"\b(airlines?|travel)\b"),
    ("Steel & aluminum", r"\b(steel|aluminum)\b"),
    ("Agriculture", r"\b(farmers?|soybeans?|wheat|corn|agricultur\w*)\b"),
]
SECTORS = [(name, re.compile(p, re.I)) for name, p in SECTORS]
MACRO_QUERIES = [
    '("Fed" OR "Federal Reserve" OR "FOMC") (cuts OR raises OR holds) rates when:1d',
    '("inflation" OR "CPI" OR "payrolls" OR "jobs report" OR "GDP" OR "unemployment rate") when:1d',
    '("tariffs" OR "export controls" OR "sanctions" OR "OPEC" OR "oil prices") when:1d',
    '("stocks" OR "Wall Street" OR "S&P 500" OR "Nasdaq") (plunge OR tumble OR surge OR "sell-off" OR "record high") when:1d',
]


def score(title):
    t = title.lower()
    s, hits = 0, []
    for w, words in KEYWORDS.items():
        for k in words:
            if k in t:
                s += w
                hits.append(k)
    return s, hits


def money_in(text):
    """Largest money amount in the text, e.g. '$1.2 billion' -> 1.2e9."""
    mult = {"t": 1e12, "trillion": 1e12, "b": 1e9, "billion": 1e9, "bln": 1e9, "bn": 1e9,
            "m": 1e6, "million": 1e6, "mln": 1e6, "mn": 1e6}
    vals = [float(n.replace(",", ".")) * mult[u.lower()] for n, u in MONEY_RE.findall(text)]
    return max(vals) if vals else 0


def is_critical(text):
    t = text.lower()
    if any(n in t for n in NOISE):
        return None
    for k in CRITICAL:
        if k in t:
            return k
    for k in CONTRACT:
        if k in t:
            amount = money_in(text)
            if amount >= BIG_CONTRACT_USD:
                return f"big contract ${amount / 1e9:.1f}B" if amount >= 1e9 else f"big contract ${amount / 1e6:.0f}M"
    return None


def macro_event(title):
    """('Tariffs', ['Steel & aluminum', 'Autos & EV']) or None."""
    head = title.rsplit(" - ", 1)[0]  # drop " - Reuters"
    if OPINION.search(head):
        return None
    for rx, name in MACRO_EVENTS:
        if rx.search(head):
            sectors = [n for n, srx in SECTORS if srx.search(head)]
            return name, sectors
    return None


# ---------- io helpers ----------
def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path, data, pretty=True):
    path.write_text(json.dumps(data, indent=2 if pretty else None, ensure_ascii=False) + "\n")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link_id(link):
    return hashlib.sha1(link.encode()).hexdigest()[:12]


def secrets():
    s = load(SECRETS, {})
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or s.get("telegram_bot_token", "")
    chats = os.environ.get("TELEGRAM_CHAT_IDS") or s.get("telegram_chat_ids", [])
    if isinstance(chats, str):
        chats = chats.split(",")
    return tok.strip(), [str(c).strip() for c in chats if str(c).strip()]


def tg(method, **params):
    tok, _ = secrets()
    if not tok:
        return None
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/{method}", data, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[warn] Telegram {method} failed: {e}", file=sys.stderr)
        return None


def chunks(text, limit=TG_LIMIT):
    """Split a long message on line breaks so each part fits in Telegram."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line)[:limit]
    if cur:
        parts.append(cur)
    return parts


def send(text, chat=None):
    """Send to one chat, or to all configured chats. Long text is split."""
    print("→ " + re.sub(r"<[^>]+>", "", text).replace("\n", " | ")[:200])
    _, chats = secrets()
    for part in chunks(text):
        for c in ([chat] if chat else chats):
            tg("sendMessage", chat_id=c, text=part, parse_mode="HTML", disable_web_page_preview="true")
            time.sleep(0.05)


def parse_feed(raw, source):
    items = []
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        try:
            date = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            date = None
        src = it.find("source")
        domain = urllib.parse.urlparse(src.get("url", "")).netloc.lower() if src is not None else ""
        if title and link:
            items.append({"title": title, "link": link, "desc": desc, "date": date,
                          "source": source, "domain": domain.replace("www.", "")})
    return items


class Google:
    """Google News search; stops for the rest of the run if Google starts refusing."""
    failures = 0

    @classmethod
    def search(cls, query):
        if cls.failures >= 5:
            return []
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            return parse_feed(http_get(url), "Google News")
        except Exception as e:
            cls.failures += 1
            print(f"[warn] Google: {e}", file=sys.stderr)
            return []


# ---------- prices ----------
SUFFIX_RE = re.compile(r"[,.]?\s+(corp(oration)?|inc|ltd|limited|plc|co|holdings|group|n\.?v|s\.?a)\.?$", re.I)


def clean_name(name):
    prev = None
    while prev != name:
        prev, name = name, SUFFIX_RE.sub("", name).strip()
    return name


def prices(tickers):
    """{ticker: {price, chg, name, exchange}} for many tickers (20 per Yahoo request)."""
    tickers = list(dict.fromkeys(tickers))
    groups = [tickers[i:i + 20] for i in range(0, len(tickers), 20)]

    def fetch(group):
        out = {}
        try:
            d = json.loads(http_get("https://query1.finance.yahoo.com/v7/finance/spark?" + urllib.parse.urlencode(
                {"symbols": ",".join(group), "range": "1d", "interval": "1d"})))
            for r in d.get("spark", {}).get("result") or []:
                m = ((r.get("response") or [{}])[0]).get("meta", {})
                if m.get("regularMarketPrice") is None:
                    continue
                chg = m.get("regularMarketChangePercent")
                if chg is None and m.get("chartPreviousClose"):
                    chg = (m["regularMarketPrice"] / m["chartPreviousClose"] - 1) * 100
                out[r["symbol"].upper()] = {"price": m["regularMarketPrice"], "chg": chg or 0.0,
                                            "name": m.get("longName") or m.get("shortName"),
                                            "exchange": m.get("fullExchangeName")}
        except Exception as e:
            print(f"[warn] prices {group[:3]}…: {e}", file=sys.stderr)
        return out

    result = {}
    with ThreadPoolExecutor(THREADS) as ex:
        for part in ex.map(fetch, groups):
            result.update(part)
    return result


def price_info(ticker):
    return prices([ticker]).get(ticker.upper())


# ---------- news ----------
def mentions(title, stock):
    if re.search(rf"\b{re.escape(stock['ticker'])}\b", title):
        return True
    for m in re.finditer(re.escape(stock["name"]), title, re.I):
        before = title[:m.start()].rstrip()
        prev = before.split()[-1] if before.split() else ""
        # skip "Eastport Critical Metals" when tracking "Critical Metals"
        if not prev or not prev[0].isupper() or prev.endswith((":", "-", "|")):
            return True
    return False


def fetch_stock_news(stock):
    t = stock["ticker"]
    items = []
    try:
        items += parse_feed(http_get(
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(t)}&region=US&lang=en-US"), "Yahoo")
    except Exception as e:
        print(f"[warn] Yahoo {t}: {e}", file=sys.stderr)
    items += Google.search(f'{t} OR "{stock["name"]}" when:7d')
    return dedupe([i for i in items if mentions(i["title"], stock)])


def dedupe(items):
    out, keys = [], set()
    for i in items:
        k = re.sub(r"\W+", "", i["title"].lower().split(" - ")[0])[:80]
        if k not in keys:
            keys.add(k)
            out.append(i)
    out.sort(key=lambda i: i["date"].timestamp() if i["date"] else 0)
    return out


def recent(items, hours=MARKET_MAX_AGE_HOURS):
    cutoff = time.time() - hours * 3600
    return [i for i in items if i["date"] and i["date"].timestamp() >= cutoff]


def fetch_critical_news():
    items = []
    try:
        for i in parse_feed(http_get("https://www.prnewswire.com/rss/news-releases-list.rss"), "PR Newswire"):
            if TICKER_RE.search(i["title"] + " " + i["desc"]):  # public companies only
                items.append(i)
    except Exception as e:
        print(f"[warn] PRN: {e}", file=sys.stderr)
    q = ('"to acquire" OR "agrees to buy" OR "files for bankruptcy" OR "FDA approves" OR '
         '"trading halt" OR "merger agreement" OR "cuts guidance" OR "CEO resigns" OR '
         '"SEC charges" OR "buyout offer" OR "awarded contract" OR "billion contract" OR '
         '"million contract" OR "wins contract" OR "supply agreement" OR "billion deal" when:1d')
    # Only serious financial outlets, so "Cyprus to acquire fighter jets" doesn't get in
    items += [i for i in Google.search(q) if i["domain"] in FINANCIAL_SITES]
    return dedupe(recent(items))


def fetch_macro_news():
    items = []
    with ThreadPoolExecutor(4) as ex:
        for res in ex.map(Google.search, MACRO_QUERIES):
            items += [i for i in res if i["domain"] in MACRO_SITES]
    return dedupe(recent(items))


# ---------- telegram commands ----------
HELP = f"""<b>📊 Stock Alerts – commands</b>
/add TSLA – add a stock
/add TSLA AAPL NVDA – add several at once (spaces, commas or new lines, up to {MAX_PER_COMMAND})
/add TSLA=Tesla Motors – with a custom name for searching
/remove TSLA AAPL – remove one or several
/remove all – empty the list
/list – your stocks + prices
/market on|off – 🚨 critical company news + 🌍 market & sector news
/level 1|2|3 – which watchlist news to send (1 = most, 3 = only big)
/help – this message"""


def parse_items(args):
    """'/add TSLA AAPL, NVDA=Nvidia Corp' -> [('TSLA',None),('AAPL',None),('NVDA','Nvidia Corp')]"""
    raw = " ".join(args)
    out, seen = [], set()
    for chunk in re.split(r"[,\n;]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            t, name = chunk.split("=", 1)
            pairs = [(t, name.strip() or None)]
        else:
            pairs = [(t, None) for t in chunk.split()]
        for t, name in pairs:
            t = t.strip().upper().lstrip("$")
            if t and t not in seen and re.fullmatch(r"[A-Z0-9.\-^=]{1,12}", t):
                seen.add(t)
                out.append((t, name))
    return out


def handle_commands(wl, state):
    _, allowed = secrets()
    res = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    if not res or not res.get("ok"):
        return
    for u in res["result"]:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        if chat not in allowed:
            print(f"[info] ignoring command from unknown chat {chat}")
            tg("sendMessage", chat_id=chat, text=f"⛔ Not authorized. Your chat ID is {chat} – "
               "add it to TELEGRAM_CHAT_IDS to use this bot.")
            continue
        first, _, rest = text.partition(" ")
        if "\n" in first:  # "/add\nTSLA\nAAPL"
            first, _, more = first.partition("\n")
            rest = more + "\n" + rest
        cmd = first.split("@")[0].lower()
        reply = run_command(cmd, [rest] if rest.strip() else [], wl, state)
        if reply:
            send(reply, chat)


def fmt_price(p):
    return f"${p['price']:,.2f} ({p['chg']:+.2f}%)"


def run_command(cmd, args, wl, state):
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add":
        items = parse_items(args)
        if not items:
            return "Usage: /add TSLA AAPL NVDA  (or /add TSLA=Tesla Motors for a custom name)"
        if len(items) > MAX_PER_COMMAND:
            return f"Too many at once – up to {MAX_PER_COMMAND} stocks per command."
        have = {s["ticker"] for s in wl["stocks"]}
        already = [t for t, _ in items if t in have]
        todo = [(t, n) for t, n in items if t not in have]
        room = MAX_STOCKS - len(wl["stocks"])
        over = [t for t, _ in todo[room:]] if len(todo) > room else []
        todo = todo[:max(room, 0)]
        info = prices([t for t, _ in todo]) if todo else {}
        added, missing = [], []
        for t, custom in todo:
            p = info.get(t)
            if not p:
                missing.append(t)
                continue
            name = custom or clean_name(p["name"] or t)
            wl["stocks"].append({"ticker": t, "name": name})
            state.setdefault("priority", []).append(t)  # check its news first
            added.append((t, name, p))
        if len(added) == 1 and len(items) == 1:
            t, name, p = added[0]
            state.setdefault("show_latest", []).append(t)  # single add: also send latest headlines
            return (f"✅ Added <b>{esc(t)}</b> – {esc(p['name'] or name)} ({esc(p['exchange'] or '')})\n"
                    f"{fmt_price(p)}\nSearching news for: {esc(t)} / \"{esc(name)}\"")
        lines = []
        if len(added) <= 40:
            lines += [f"✅ <b>{esc(t)}</b> {esc(name)} – {fmt_price(p)}" for t, name, p in added]
        elif added:
            lines.append("✅ " + ", ".join(esc(t) for t, _, _ in added))
        if already:
            lines.append("Already on the list: " + ", ".join(esc(t) for t in already))
        if missing:
            lines.append("❓ Not found on Yahoo Finance: " + ", ".join(esc(t) for t in missing))
        if over:
            lines.append(f"⛔ List is full ({MAX_STOCKS}). Not added: " + ", ".join(esc(t) for t in over))
        head = f"Added {len(added)} of {len(items)}:" if added else "Nothing added."
        return head + "\n" + "\n".join(lines) + f"\n\nTotal on the list: {len(wl['stocks'])}"
    if cmd in ("/remove", "/del", "/delete"):
        if args and args[0].strip().lower() == "all":
            n = len(wl["stocks"])
            wl["stocks"] = []
            return f"🗑 Removed all {n} stocks. The list is empty."
        items = parse_items(args)
        if not items:
            return "Usage: /remove TSLA AAPL NVDA  (or /remove all)"
        wanted = [t for t, _ in items]
        have = {s["ticker"] for s in wl["stocks"]}
        removed = [t for t in wanted if t in have]
        not_there = [t for t in wanted if t not in have]
        wl["stocks"] = [s for s in wl["stocks"] if s["ticker"] not in set(removed)]
        lines = []
        if removed:
            lines.append("🗑 Removed: " + ", ".join(esc(t) for t in removed))
        if not_there:
            lines.append("Not on the list: " + ", ".join(esc(t) for t in not_there))
        return "\n".join(lines) + f"\n\nTotal on the list: {len(wl['stocks'])}"
    if cmd == "/list":
        stocks = wl["stocks"]
        if not stocks:
            return "The list is empty. Add with /add TICKER"
        info = prices([s["ticker"] for s in stocks])
        if len(stocks) <= 60:
            lines = [f"• <b>{esc(s['ticker'])}</b> {esc(s['name'])}"
                     + (f" – {fmt_price(info[s['ticker']])}" if s["ticker"] in info else "") for s in stocks]
        else:  # compact for long lists
            lines = [f"<b>{esc(s['ticker'])}</b> {info[s['ticker']]['chg']:+.1f}%" if s["ticker"] in info
                     else f"<b>{esc(s['ticker'])}</b>" for s in stocks]
            lines = [" · ".join(lines[i:i + 6]) for i in range(0, len(lines), 6)]
        return (f"<b>Your stocks ({len(stocks)}):</b>\n" + "\n".join(lines) +
                f"\n\nMarket-wide news: {'ON' if wl.get('market_alerts') else 'OFF'}"
                f"\nLevel: {wl.get('min_score', 1)}")
    if cmd == "/market":
        if args and args[0].strip().lower() in ("on", "off"):
            wl["market_alerts"] = args[0].strip().lower() == "on"
            if wl["market_alerts"]:
                state["market_init"] = False
        return (f"Market-wide news: {'ON' if wl.get('market_alerts') else 'OFF'}\n"
                "🚨 critical company news + 🌍 market & sector news")
    if cmd == "/level":
        if args and args[0].strip() in ("1", "2", "3"):
            wl["min_score"] = int(args[0].strip())
        return f"Level: {wl.get('min_score', 1)} (1 = all relevant news, 3 = only big events)"
    return "Unknown command. /help"


# ---------- main check ----------
def pick_batch(stocks, state):
    """Stocks whose news we check this run: new ones first, then rotate through the list."""
    tickers = [s["ticker"] for s in stocks]
    if len(tickers) <= NEWS_PER_RUN:
        return list(stocks)
    by_t = {s["ticker"]: s for s in stocks}
    prio = [t for t in state.get("priority", []) if t in by_t]
    batch = prio[:NEWS_PER_RUN]
    i = state.get("rotation", 0) % len(tickers)
    while len(batch) < NEWS_PER_RUN:
        t = tickers[i]
        if t not in batch:
            batch.append(t)
        i = (i + 1) % len(tickers)
        if len(batch) >= len(tickers):
            break
    state["rotation"] = i
    return [by_t[t] for t in batch]


def check_once():
    wl = load(WATCHLIST, {"stocks": [], "market_alerts": True, "min_score": 1, "price_move_alert_percent": 5})
    state = load(STATE, {})
    today = int(time.time() // 86400)
    seen = {k: v for k, v in state.get("seen_ids", {}).items() if today - v <= SEEN_DAYS}
    moves = state.get("moves", {})
    inited = set(state.get("inited", []))  # stocks whose existing news was already marked as seen
    if "seen" in state:  # migrate the old format (list of links, all stocks already initialised)
        for link in state["seen"]:
            seen.setdefault(link_id(link), today)
        inited |= {s["ticker"] for s in wl["stocks"]}
    first_run = not state.get("initialized")
    t0 = time.time()

    handle_commands(wl, state)
    save(WATCHLIST, wl)  # commands take effect even if a later step fails
    show_latest = set(state.pop("show_latest", []))
    min_score = wl.get("min_score", 1)
    step = wl.get("price_move_alert_percent", 5)
    stocks = wl["stocks"][:MAX_STOCKS]
    names = {s["ticker"]: s["name"] for s in stocks}

    # 1) prices for every stock (cheap: 20 per request)
    px = prices([s["ticker"] for s in stocks]) if stocks else {}
    big_moves = []
    for s in stocks:
        p = px.get(s["ticker"])
        if not p or abs(p["chg"]) < step:
            continue
        level = int(abs(p["chg"]) // step) * (1 if p["chg"] > 0 else -1)
        key = f"{s['ticker']}:{time.strftime('%Y-%m-%d')}"
        if moves.get(key) != level:
            moves[key] = level
            if not first_run:
                big_moves.append((s["ticker"], p))

    # 2) news for a batch of stocks (all of them when the list is ≤ NEWS_PER_RUN)
    batch = pick_batch(stocks, state)
    with ThreadPoolExecutor(THREADS) as ex:
        results = list(ex.map(fetch_stock_news, batch))
    alerts = []
    for s, news in zip(batch, results):
        t = s["ticker"]
        silent = first_run or t not in inited
        p = px.get(t)
        header = f"📈 {t} ({s['name']})" + (f" — {fmt_price(p)}" if p else "")
        if t in show_latest and news:
            latest = "\n".join(f"• <a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>" for i in news[-3:][::-1])
            send(f"<b>{esc(header)}</b>\nLatest news:\n{latest}")
        for i in news:
            lid = link_id(i["link"])
            if lid in seen:
                continue
            seen[lid] = today
            sc, _ = score(i["title"])
            if silent or sc < min_score:
                continue
            tag = "🔴 HIGH" if sc >= 3 else ("🟡 MED" if sc >= 2 else "⚪ low")
            alerts.append((sc, header, tag, i))
        inited.add(t)
    state["priority"] = [t for t in state.get("priority", []) if t not in {s["ticker"] for s in batch}]
    print(f"[{time.strftime('%H:%M:%S')}] {len(stocks)} stocks, news checked for {len(batch)}, "
          f"{len(alerts)} alerts, {len(big_moves)} big moves")

    if len(alerts) <= MAX_SINGLE_ALERTS:
        for sc, header, tag, i in alerts:
            send(f"<b>{esc(header)}</b>\n{tag} <a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>")
    else:  # many at once -> one digest, most important first
        alerts.sort(key=lambda a: -a[0])
        send(f"<b>📰 {len(alerts)} news items on your stocks</b>\n" + "\n".join(
            f"{tag} <b>{esc(header.split(' ')[1])}</b> <a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>"
            for sc, header, tag, i in alerts))
    if big_moves:
        big_moves.sort(key=lambda m: -abs(m[1]["chg"]))
        send("<b>⚠️ Big moves today</b>\n" + "\n".join(
            f"{'🟢' if p['chg'] > 0 else '🔴'} <b>{esc(t)}</b> {esc(names.get(t, ''))} {fmt_price(p)}" for t, p in big_moves))

    # 3) market-wide: 🚨 critical company news + 🌍 market & sector news
    if wl.get("market_alerts"):
        market_silent = first_run or not state.get("market_init")
        out = []
        for i in fetch_critical_news():
            lid = link_id(i["link"])
            if lid in seen:
                continue
            seen[lid] = today
            k = is_critical(i["title"])
            if k:
                m = TICKER_RE.search(i["title"] + " " + i["desc"])
                tick = f" <b>${esc(m.group(1).upper())}</b>" if m else ""
                out.append(f"🚨 <b>Critical company news</b>{tick}\n<a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>\n"
                           f"<i>[{esc(i['source'])} · {esc(k)}]</i>")
        for i in fetch_macro_news():
            lid = link_id(i["link"])
            if lid in seen:
                continue
            seen[lid] = today
            ev = macro_event(i["title"])
            if ev:
                name, sectors = ev
                where = ", ".join(sectors) if sectors else "Whole market"
                icon = "🏭 <b>Sector news</b>" if sectors else "🌍 <b>Market news</b>"
                out.append(f"{icon} · {esc(name)}\n<a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>\n"
                           f"<i>Affects: {esc(where)}</i>")
        if not market_silent:
            for msg in out[:MAX_MARKET_ALERTS]:
                send(msg)
        state["market_init"] = True

    state["initialized"] = True
    state["seen_ids"] = seen
    state["inited"] = sorted(inited & {s["ticker"] for s in stocks})
    state["moves"] = dict(list(moves.items())[-300:])
    state.pop("seen", None)  # old format
    save(WATCHLIST, wl)
    save(STATE, state, pretty=False)
    print(f"done in {time.time() - t0:.1f}s")


def main():
    if "--test-telegram" in sys.argv:
        tok, chats = secrets()
        if not tok or not chats:
            print("Missing token / chat IDs")
            return
        send("✅ Stock Alerts is connected. Send /help for commands.")
        return
    if "--loop" in sys.argv:
        every = int(os.environ.get("CHECK_EVERY_MINUTES", "3"))
        print(f"Live mode: every {every} min. Ctrl+C to stop.")
        while True:
            try:
                check_once()
            except Exception as e:
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(every * 60)
    check_once()


if __name__ == "__main__":
    main()
