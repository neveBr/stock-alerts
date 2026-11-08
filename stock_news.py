#!/usr/bin/env python3
"""Stock news alerts -> Telegram. Runs on GitHub Actions (every few minutes) or locally.

Watchlist alerts: every new news item about your stocks (+ big price moves).
Market-wide alerts: only critical news about ANY public company (M&A, bankruptcy, FDA, halts...).
Control it from Telegram: /add /remove /list /market /level /help

Usage:
  python3 stock_news.py              # one check (what GitHub Actions runs)
  python3 stock_news.py --loop       # keep running locally
  python3 stock_news.py --test-telegram
Secrets: env TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS (comma separated), or local_secrets.json.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
STATE = BASE / "state.json"
SECRETS = BASE / "local_secrets.json"
UA = {"User-Agent": "Mozilla/5.0 (StockNewsAlerts)"}

# ---------- scoring ----------
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

# Market-wide: only these "really moves the stock" events
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
# Contract signings: alert only if the headline mentions >= BIG_CONTRACT_USD
CONTRACT = ["contract", "awarded", "deal", "order", "agreement"]
BIG_CONTRACT_USD = 100_000_000
MAX_MARKET_ALERTS = 10
MONEY_RE = re.compile(r"(?:US|A|C)?[$€£]\s?(\d+(?:[.,]\d+)?)\s?(trillion|billion|bln|bn|million|mln|mn|[bmt])\b", re.I)


def money_in(text):
    """Largest dollar amount in the text, e.g. '$1.2 billion' -> 1.2e9."""
    mult = {"t": 1e12, "trillion": 1e12, "b": 1e9, "billion": 1e9, "bln": 1e9, "bn": 1e9,
            "m": 1e6, "million": 1e6, "mln": 1e6, "mn": 1e6}
    vals = [float(n.replace(",", ".")) * mult[u.lower()] for n, u in MONEY_RE.findall(text)]
    return max(vals) if vals else 0
# Law-firm "investor notice" spam etc.
NOISE = ["class action", "investor notice", "investors who", "shareholder alert",
         "reminds investors", "law firm", "rosen", "pomerantz", "levi & korsinsky",
         "on behalf of investors", "lead plaintiff", "investigation on behalf", "deadline"]
MARKET_MAX_AGE_HOURS = 6
FINANCIAL_SITES = {"reuters.com", "cnbc.com", "bloomberg.com", "wsj.com", "marketwatch.com",
                   "finance.yahoo.com", "investing.com", "barrons.com", "ft.com", "seekingalpha.com",
                   "businesswire.com", "globenewswire.com", "prnewswire.com", "fiercebiotech.com",
                   "biopharmadive.com", "stocktitan.net", "benzinga.com", "fool.com", "thestreet.com", "tradingview.com"}
TICKER_RE = re.compile(r"\((?:NASDAQ|NYSE|NYSE American|AMEX|OTC\w*|TSX|LSE|ASX)\s*:\s*([A-Z.]{1,6})\)", re.I)


def score(title):
    t = title.lower()
    s, hits = 0, []
    for w, words in KEYWORDS.items():
        for k in words:
            if k in t:
                s += w
                hits.append(k)
    return s, hits


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


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def send(text, chat=None):
    """Send to one chat, or to all configured chats. Prints too."""
    print("→ " + re.sub(r"<[^>]+>", "", text).replace("\n", " | ")[:200])
    _, chats = secrets()
    for c in ([chat] if chat else chats):
        tg("sendMessage", chat_id=c, text=text, parse_mode="HTML", disable_web_page_preview="true")


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
                          "source": source, "domain": domain})
    return items


def google_news(query):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return parse_feed(http_get(url), "Google News")


# ---------- per-stock ----------
SUFFIX_RE = re.compile(r"[,.]?\s+(corp(oration)?|inc|ltd|limited|plc|co|holdings|group|n\.?v|s\.?a)\.?$", re.I)


def clean_name(name):
    prev = None
    while prev != name:
        prev, name = name, SUFFIX_RE.sub("", name).strip()
    return name


def price_info(ticker):
    try:
        d = json.loads(http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=5d&interval=1d"))
        m = d["chart"]["result"][0]["meta"]
        return {"price": m.get("regularMarketPrice"), "chg": m.get("regularMarketChangePercent"),
                "name": m.get("longName") or m.get("shortName"), "exchange": m.get("fullExchangeName")}
    except Exception:
        return None


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
    try:
        items += google_news(f'{t} OR "{stock["name"]}" when:7d')
    except Exception as e:
        print(f"[warn] Google {t}: {e}", file=sys.stderr)
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


def fetch_market_news():
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
    try:
        # Only serious financial outlets, so "Cyprus to acquire fighter jets" doesn't get in
        items += [i for i in google_news(q) if i["domain"].replace("www.", "") in FINANCIAL_SITES]
    except Exception as e:
        print(f"[warn] Google market: {e}", file=sys.stderr)
    # Google sometimes returns old stories - keep only the last few hours
    cutoff = time.time() - MARKET_MAX_AGE_HOURS * 3600
    items = [i for i in items if i["date"] and i["date"].timestamp() >= cutoff]
    return dedupe(items)


# ---------- telegram commands ----------
HELP = """<b>📊 Stock Alerts – commands</b>
/add TSLA – add a stock
/add TSLA AAPL NVDA – add several at once (spaces, commas or new lines)
/add TSLA=Tesla Motors – with a custom name for searching
/remove TSLA AAPL – remove one or several
/list – your stocks + prices
/market on|off – critical news on ANY company
/level 1|2|3 – which watchlist news to send (1 = most, 3 = only big)
/help – this message"""
MAX_PER_COMMAND = 30


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
            if t and t not in seen:
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
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        reply = run_command(cmd, args, wl, state)
        if reply:
            send(reply, chat)


def run_command(cmd, args, wl, state):
    stocks = wl["stocks"]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add":
        items = parse_items(args)
        if not items:
            return "Usage: /add TSLA AAPL NVDA  (or /add TSLA=Tesla Motors for a custom name)"
        if len(items) > MAX_PER_COMMAND:
            return f"Too many at once – up to {MAX_PER_COMMAND} stocks per command."
        added, already, missing = [], [], []
        for t, custom in items:
            if any(s["ticker"] == t for s in wl["stocks"]):
                already.append(t)
                continue
            info = price_info(t)
            if not info or info["price"] is None:
                missing.append(t)
                continue
            name = custom or clean_name(info["name"] or t)
            wl["stocks"].append({"ticker": t, "name": name})
            state.setdefault("new_tickers", []).append(t)
            added.append((t, name, info))
        if len(added) == 1 and len(items) == 1:
            t, name, info = added[0]
            state.setdefault("show_latest", []).append(t)  # single add: also send latest headlines
            return (f"✅ Added <b>{esc(t)}</b> – {esc(info['name'] or name)} ({esc(info['exchange'] or '')})\n"
                    f"${info['price']:.2f} ({info['chg'] or 0:+.2f}%)\n"
                    f"Searching news for: {esc(t)} / \"{esc(name)}\"")
        lines = [f"✅ <b>{esc(t)}</b> {esc(name)} – ${i['price']:.2f} ({i['chg'] or 0:+.2f}%)" for t, name, i in added]
        if already:
            lines.append("Already on the list: " + ", ".join(esc(t) for t in already))
        if missing:
            lines.append("❓ Not found on Yahoo Finance: " + ", ".join(esc(t) for t in missing))
        head = f"Added {len(added)} of {len(items)}:" if added else "Nothing added."
        return head + "\n" + "\n".join(lines) + f"\n\nTotal on the list: {len(wl['stocks'])}"
    if cmd in ("/remove", "/del", "/delete"):
        items = parse_items(args)
        if not items:
            return "Usage: /remove TSLA AAPL NVDA"
        wanted = [t for t, _ in items]
        have = {s["ticker"] for s in wl["stocks"]}
        removed = [t for t in wanted if t in have]
        not_there = [t for t in wanted if t not in have]
        wl["stocks"] = [s for s in wl["stocks"] if s["ticker"] not in removed]
        lines = []
        if removed:
            lines.append("🗑 Removed: " + ", ".join(esc(t) for t in removed))
        if not_there:
            lines.append("Not on the list: " + ", ".join(esc(t) for t in not_there))
        return "\n".join(lines) + f"\n\nTotal on the list: {len(wl['stocks'])}"
    if cmd == "/list":
        if not stocks:
            return "The list is empty. Add with /add TICKER"
        lines = []
        for s in stocks:
            i = price_info(s["ticker"])
            p = f" – ${i['price']:.2f} ({i['chg'] or 0:+.2f}%)" if i and i["price"] is not None else ""
            lines.append(f"• <b>{esc(s['ticker'])}</b> {esc(s['name'])}{p}")
        return ("<b>Your stocks:</b>\n" + "\n".join(lines) +
                f"\n\nMarket-wide critical news: {'ON' if wl.get('market_alerts') else 'OFF'}"
                f"\nLevel: {wl.get('min_score', 1)}")
    if cmd == "/market":
        if args and args[0].lower() in ("on", "off"):
            wl["market_alerts"] = args[0].lower() == "on"
            if wl["market_alerts"]:
                state["market_init"] = False
        return f"Market-wide critical news: {'ON' if wl.get('market_alerts') else 'OFF'}"
    if cmd == "/level":
        if args and args[0] in ("1", "2", "3"):
            wl["min_score"] = int(args[0])
        return f"Level: {wl.get('min_score', 1)} (1 = all relevant news, 3 = only big events)"
    return "Unknown command. /help"


# ---------- main check ----------
def check_once():
    wl = load(WATCHLIST, {"stocks": [], "market_alerts": True, "min_score": 1, "price_move_alert_percent": 5})
    state = load(STATE, {})
    seen = set(state.get("seen", []))
    moves = state.get("moves", {})
    first_run = not state.get("initialized")

    handle_commands(wl, state)
    new_tickers = set(state.pop("new_tickers", []))
    show_latest = set(state.pop("show_latest", []))
    min_score = wl.get("min_score", 1)
    step = wl.get("price_move_alert_percent", 5)

    for stock in wl["stocks"]:
        t = stock["ticker"]
        info = price_info(t) or {}
        price, chg = info.get("price"), info.get("chg")
        header = f"📈 {t} ({stock['name']})" + (f" — ${price:.2f} ({chg:+.2f}%)" if price is not None else "")
        news = fetch_stock_news(stock)
        new = [i for i in news if i["link"] not in seen]
        silent = first_run or t in new_tickers  # don't flood when a stock is first added
        print(f"[{time.strftime('%H:%M:%S')}] {header} — {len(new)} new{' (silent)' if silent else ''}")
        if t in show_latest and news:
            latest = "\n".join(f"• <a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>" for i in news[-3:][::-1])
            send(f"<b>{esc(header)}</b>\nLatest news:\n{latest}")
        for i in new:
            seen.add(i["link"])
            s, _ = score(i["title"])
            if silent or s < min_score:
                continue
            tag = "🔴 HIGH" if s >= 3 else ("🟡 MED" if s >= 2 else "⚪ low")
            send(f"<b>{esc(header)}</b>\n{tag} <a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>")
        if chg is not None and abs(chg) >= step:
            level = int(abs(chg) // step) * (1 if chg > 0 else -1)
            key = f"{t}:{time.strftime('%Y-%m-%d')}"
            if moves.get(key) != level:
                moves[key] = level
                if not silent:
                    send(f"⚠️ <b>{esc(t)}</b> moved {chg:+.2f}% today (${price:.2f})")

    if wl.get("market_alerts"):
        market_silent = first_run or not state.get("market_init")
        sent = 0
        for i in fetch_market_news():
            if i["link"] in seen:
                continue
            seen.add(i["link"])
            k = is_critical(i["title"])
            if not k or market_silent or sent >= MAX_MARKET_ALERTS:
                continue
            m = TICKER_RE.search(i["title"] + " " + i["desc"])
            tick = f" <b>${esc(m.group(1).upper())}</b>" if m else ""
            send(f"🚨 <b>Critical market news</b>{tick}\n<a href=\"{esc(i['link'])}\">{esc(i['title'])}</a>\n"
                 f"<i>[{esc(i['source'])} · {esc(k)}]</i>")
            sent += 1
        state["market_init"] = True

    state["initialized"] = True
    state["seen"] = sorted(seen)[-5000:]
    state["moves"] = dict(list(moves.items())[-100:])
    save(WATCHLIST, wl)
    save(STATE, state)


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
