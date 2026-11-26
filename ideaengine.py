import feedparser
import re
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, date, time as datetime_time
from zoneinfo import ZoneInfo
import os
import requests
import io
import json
import time
import argparse
import calendar
import tempfile
from statistics import mean
from pathlib import Path
from dotenv import load_dotenv
SGT = ZoneInfo("Asia/Singapore")
ET = ZoneInfo("America/New_York")

# ==========================================
# 1. THE PLAYBOOK (Strategy)
# ==========================================

# --- SEC 8-K ITEM CODES (Highest Signal Quality) ---
# These are parsed from official SEC filings - very reliable
SEC_ITEM_CODES = {
    "SEC_Material_Agreement": {
        "item_code": "Item 1.01",  # Entry into Material Definitive Agreement
        "story": "CONTRACT/M&A: Company signed a major binding deal.",
        "direction": "BULLISH", "horizon_days": 5
    },
    "SEC_Bankruptcy": {
        "item_code": "Item 1.03",  # Bankruptcy or Receivership
        "story": "DISTRESS: Bankruptcy filing.",
        "direction": "BEARISH", "horizon_days": 1
    },
    "SEC_Earnings_Release": {
        "item_code": "Item 2.02",  # Results of Operations (Earnings)
        "story": "EARNINGS: Check if off-cycle (surprise).",
        "direction": "NEUTRAL_CHECK", "horizon_days": 1
    },
    "SEC_Asset_Acquisition": {
        "item_code": "Item 2.01",  # Completion of Acquisition/Disposition
        "story": "M&A COMPLETE: Deal closed, integration begins.",
        "direction": "BULLISH", "horizon_days": 10
    },
    "SEC_Delisting": {
        "item_code": "Item 3.01",  # Delisting notice
        "story": "DELISTING: Stock being removed from exchange.",
        "direction": "BEARISH", "horizon_days": 1
    },
    "SEC_Change_Control": {
        "item_code": "Item 5.01",  # Changes in Control
        "story": "ACTIVIST/TAKEOVER: Major ownership change.",
        "direction": "VOLATILITY", "horizon_days": 10
    },
    "SEC_Executive_Change": {
        "item_code": "Item 5.02",  # Departure/Election of Directors/Officers
        "story": "LEADERSHIP: Executive change announced.",
        "direction": "NEUTRAL_CHECK", "horizon_days": 3
    },
    "SEC_Bylaw_Amendment": {
        "item_code": "Item 5.03",  # Amendments to Articles/Bylaws
        "story": "GOVERNANCE: Corporate structure change.",
        "direction": "NEUTRAL_CHECK", "horizon_days": 5
    },
}

# --- TWO-PART FUZZY LOGIC (Required + Modifier) ---
# More precise than single keywords: "guidance" alone isn't a signal,
# but "guidance" + "raise" together IS a signal
FUZZY_PLAYBOOK = {
    "Guidance_Hike": {
        "required": ["guidance", "outlook", "forecast", "full-year", "quarter"],
        "modifier": ["raise", "boost", "hike", "beat", "strong", "above", "increase", "higher", "up"],
        "story": "POSITIVE OUTLOOK: Management confident, raising targets.",
        "direction": "BULLISH", "horizon_days": 10
    },
    "Guidance_Cut": {
        "required": ["guidance", "outlook", "forecast", "full-year", "quarter"],
        "modifier": ["cut", "lower", "miss", "weak", "warn", "below", "reduce", "down", "disappoint"],
        "story": "NEGATIVE OUTLOOK: Management sees trouble ahead.",
        "direction": "BEARISH", "horizon_days": 5
    },
    "M_and_A_Confirmed": {
        "required": ["acquire", "merger", "buyout", "takeover", "buy"],
        "modifier": ["agree", "definitive", "announce", "complete", "close", "final", "approve"],
        "story": "M&A CONFIRMED: Deal announced or closed.",
        "direction": "BULLISH", "horizon_days": 30
    },
    "M_and_A_Rumor": {
        "required": ["acquire", "merger", "buyout", "takeover", "deal"],
        "modifier": ["talks", "rumor", "consider", "explore", "weigh", "potential", "could", "may"],
        "story": "M&A RUMOR: Buy the rumor, sell the news.",
        "direction": "BULLISH", "horizon_days": 2
    },
    "FDA_Positive": {
        "required": ["fda", "drug", "therapy", "treatment", "trial", "phase"],
        "modifier": ["approve", "clear", "grant", "positive", "success", "met", "breakthrough", "fast track"],
        "story": "FDA/CLINICAL: Positive regulatory catalyst.",
        "direction": "BULLISH", "horizon_days": 10
    },
    "FDA_Negative": {
        "required": ["fda", "drug", "therapy", "treatment", "trial", "phase"],
        "modifier": ["reject", "deny", "fail", "miss", "delay", "hold", "concern", "crl"],
        "story": "FDA/CLINICAL: Negative regulatory catalyst.",
        "direction": "BEARISH", "horizon_days": 5
    },
    "Analyst_Upgrade": {
        "required": ["upgrade", "rating", "price target", "analyst", "initiates"],
        "modifier": ["buy", "overweight", "outperform", "raise", "boost", "bullish", "positive"],
        "story": "UPGRADE: Street getting more bullish.",
        "direction": "BULLISH", "horizon_days": 5
    },
    "Analyst_Downgrade": {
        "required": ["downgrade", "rating", "price target", "analyst", "cut"],
        "modifier": ["sell", "underweight", "underperform", "lower", "reduce", "bearish", "negative"],
        "story": "DOWNGRADE: Street getting more bearish.",
        "direction": "BEARISH", "horizon_days": 5
    },
}

# --- SINGLE KEYWORD PLAYBOOK (Fallback) ---
# Used when two-part logic doesn't match
KEYWORD_PLAYBOOK = {
    # === CORPORATE ACTIONS ===
    "Spinoff": {
        "keywords": ["spinoff", "spin-off", "splits into", "separate company", "divests",
                     "divestiture", "carve-out"],
        "story": "SPINOFF: Unlocking hidden value.",
        "direction": "BULLISH", "horizon_days": 60
    },
    "Contract_Win": {
        "keywords": ["wins contract", "awarded contract", "secures deal",
                     "strategic partnership", "major contract", "billion dollar deal",
                     "multi-year agreement", "selected by", "chosen by", "lands deal"],
        "story": "CONTRACT: Revenue visibility improving.",
        "direction": "BULLISH", "horizon_days": 10
    },

    # === INSIDER / INSTITUTIONAL ===
    "Insider_Buy": {
        "keywords": ["insider buying", "ceo buys", "director buys", "insider purchase",
                     "10% owner buys", "cluster buying", "open market purchase"],
        "story": "INSIDER: Smart money accumulating.",
        "direction": "BULLISH", "horizon_days": 20
    },

    # === SHORT SQUEEZE ===
    "Short_Squeeze": {
        "keywords": ["short squeeze", "heavily shorted", "high short interest", "shorts covering",
                     "most shorted", "short sellers", "squeeze potential"],
        "story": "SQUEEZE: Shorts trapped, forced buying.",
        "direction": "BULLISH", "horizon_days": 3
    },

    # === BUYBACK / DIVIDEND ===
    "Buyback": {
        "keywords": ["buyback", "share repurchase", "repurchase program", "billion buyback",
                     "authorized repurchase", "accelerated buyback", "stock buyback"],
        "story": "BUYBACK: Company returning capital to shareholders.",
        "direction": "BULLISH", "horizon_days": 30
    },
    "Dividend": {
        "keywords": ["dividend increase", "raises dividend", "special dividend", "dividend hike",
                     "boosts payout", "initiates dividend", "dividend boost"],
        "story": "DIVIDEND: Shareholder return improving.",
        "direction": "BULLISH", "horizon_days": 10
    },

    # === NEGATIVE EVENTS ===
    "Lawsuit": {
        "keywords": ["lawsuit", "sued", "class action", "sec investigation", "doj probe",
                     "regulatory probe", "accounting fraud", "securities fraud", "subpoena"],
        "story": "LEGAL: Headline risk / liability exposure.",
        "direction": "BEARISH", "horizon_days": 5
    },
    "Bankruptcy": {
        "keywords": ["bankruptcy", "chapter 11", "chapter 7", "restructuring", "defaults on",
                     "liquidity crisis", "going concern", "delisting warning"],
        "story": "DISTRESS: Solvency risk / potential wipeout.",
        "direction": "BEARISH", "horizon_days": 1
    },

    # === EARNINGS KEYWORDS (Fallback) ===
    "Earnings_Beat": {
        "keywords": ["beats estimates", "tops expectations", "blowout quarter", "record earnings",
                     "exceeds forecast", "strong results", "better than expected"],
        "story": "EARNINGS BEAT: Company outperformed.",
        "direction": "BULLISH", "horizon_days": 5
    },
    "Earnings_Miss": {
        "keywords": ["misses estimates", "below expectations", "disappointing quarter",
                     "weak results", "revenue miss", "profit miss", "worse than expected"],
        "story": "EARNINGS MISS: Company underperformed.",
        "direction": "BEARISH", "horizon_days": 5
    },
}

# === NEWS FEEDS ===
# Targeted Google News queries filtering by TRUSTED financial domains only
# This reduces spam and increases signal quality
GNEWS_QUERIES = [
    # Earnings & Guidance - trusted sources only
    "https://news.google.com/rss/search?q=when:1d+(guidance+OR+earnings+OR+forecast)+site:finance.yahoo.com+OR+site:seekingalpha.com+OR+site:benzinga.com+OR+site:marketwatch.com&hl=en-US&gl=US&ceid=US:en",
    # M&A activity - trusted sources only
    "https://news.google.com/rss/search?q=when:1d+(acquisition+OR+merger+OR+buyout+OR+takeover)+site:finance.yahoo.com+OR+site:seekingalpha.com+OR+site:benzinga.com+OR+site:reuters.com&hl=en-US&gl=US&ceid=US:en",
    # FDA/Biotech - trusted sources only
    "https://news.google.com/rss/search?q=when:1d+(fda+approval+OR+fda+clears+OR+drug+trial)+site:finance.yahoo.com+OR+site:biospace.com+OR+site:fiercepharma.com&hl=en-US&gl=US&ceid=US:en",
    # Analyst actions - trusted sources only
    "https://news.google.com/rss/search?q=when:1d+(upgrade+OR+downgrade+OR+price+target)+site:finance.yahoo.com+OR+site:thefly.com+OR+site:benzinga.com&hl=en-US&gl=US&ceid=US:en",
]

# SEC EDGAR Feed (The Truth - Official Filings)
SEC_FEED = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&CIK=&type=8-K&company=&dateb=&owner=include&start=0&count=40&output=atom"

# Additional RSS feeds (less targeted but broader coverage)
RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
    "https://www.investing.com/rss/news.rss",
]

LEDGER_FILE = "signals_ledger.csv"
CACHE_FILE = ".alpha_cache.json"
REPORT_FILE = "performance_report.md"
CACHE_TTL_SECONDS = 24 * 60 * 60
NEUTRAL_RELATIVE_MOVE_THRESHOLD_PCT = 2.0

def load_project_environment(env_path=None):
    """Load project-local configuration without overriding exported variables."""
    path = Path(env_path) if env_path else Path(__file__).resolve().with_name(".env")
    load_dotenv(dotenv_path=path, override=False)

# ==========================================
# ALTERNATIVE DATA SOURCES (Free APIs)
# ==========================================

def fetch_finnhub_news(api_key=None):
    """
    Fetch market news from Finnhub (free tier: 60 calls/min).
    Get free API key at: https://finnhub.io/
    """
    if not api_key:
        api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        return []

    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            news = resp.json()
            return [
                {"title": n.get("headline", ""), "source": "finnhub", "url": n.get("url", "")}
                for n in news[:30]
            ]
    except Exception as e:
        print(f"Finnhub error: {e}")
    return []

def fetch_alphavantage_news(api_key=None, tickers=None):
    """
    Fetch news from Alpha Vantage (free tier: 25 calls/day).
    Get free API key at: https://www.alphavantage.co/support/#api-key
    """
    if not api_key:
        api_key = os.environ.get('ALPHAVANTAGE_API_KEY', '')
    if not api_key:
        return []

    try:
        ticker_str = tickers if tickers else "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META"
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={ticker_str}&apikey={api_key}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            feed = data.get("feed", [])
            return [{"title": n.get("title", ""), "source": "alphavantage",
                     "url": n.get("url", ""),
                     "tickers": [t["ticker"] for t in n.get("ticker_sentiment", [])]}
                    for n in feed[:30]]
    except Exception as e:
        print(f"AlphaVantage error: {e}")
    return []

def fetch_polygon_news(api_key=None):
    """
    Fetch news from Polygon.io (free tier: 5 calls/min).
    Get free API key at: https://polygon.io/
    """
    if not api_key:
        api_key = os.environ.get('POLYGON_API_KEY', '')
    if not api_key:
        return []

    try:
        url = f"https://api.polygon.io/v2/reference/news?limit=50&apiKey={api_key}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            return [{"title": n.get("title", ""), "source": "polygon",
                     "url": n.get("article_url", ""),
                     "tickers": n.get("tickers", [])} for n in results]
    except Exception as e:
        print(f"Polygon error: {e}")
    return []

def fetch_unusual_whales(api_key=None):
    """
    Fetch unusual options flow (requires paid API).
    Alternative: Check their free Twitter/X feed.
    """
    # Placeholder - requires subscription
    return []

def fetch_sec_filings():
    """
    Fetch recent 8-K filings from SEC EDGAR.
    8-K = Material events (earnings, M&A, executive changes, etc.)
    Extracts Item codes for high-precision signal detection.
    """
    try:
        headers = {'User-Agent': 'AlphaEngine/1.0 (contact@example.com)'}
        feed = feedparser.parse(SEC_FEED, request_headers=headers)
        filings = []
        for entry in feed.entries[:40]:
            title = entry.get('title', '')
            summary = entry.get('summary', '')
            full_text = f"{title} {summary}"

            # Extract SEC Item codes from the filing
            item_codes = re.findall(r'Item \d+\.\d+', full_text)

            filings.append({
                "title": title,
                "summary": summary,
                "full_text": full_text,
                "item_codes": item_codes,
                "source": "sec_8k",
                "url": entry.get("link", ""),
                "is_sec": True  # Flag for special SEC processing
            })
        return filings
    except Exception as e:
        print(f"SEC EDGAR error: {e}")
    return []

def contains_exact_phrase(phrase, text):
    normalized_text = re.sub(r"\s+", " ", text).strip()
    normalized_phrase = re.sub(r"\s+", " ", phrase).strip()
    return bool(
        re.search(
            r"(?<!\w)" + re.escape(normalized_phrase) + r"(?!\w)",
            normalized_text,
            flags=re.IGNORECASE,
        )
    )

def check_sec_item_codes(item):
    """
    Check SEC 8-K filings for specific Item codes.
    These are the most reliable signals.
    """
    item_codes = item.get('item_codes', [])
    full_text = item.get('full_text', '')

    for event_key, config in SEC_ITEM_CODES.items():
        if config['item_code'] in full_text or config['item_code'] in str(item_codes):
            return event_key, config
    return None, None

def check_two_part_logic(headline):
    """
    Two-part fuzzy matching: requires BOTH a 'required' word AND a 'modifier' word.
    Much more precise than single keyword matching.
    Example: "guidance" alone isn't a signal, but "guidance" + "raise" IS.
    """
    headline_lower = headline.lower()

    for event_key, config in FUZZY_PLAYBOOK.items():
        has_required = any(word in headline_lower for word in config['required'])
        has_modifier = any(word in headline_lower for word in config['modifier'])

        if has_required and has_modifier:
            return event_key, config

    return None, None

def check_keyword_fallback(headline):
    """
    Match complete playbook phrases only; fuzzy title similarity is too noisy
    for directional signals.
    """
    for event_key, config in KEYWORD_PLAYBOOK.items():
        for kw in config['keywords']:
            if contains_exact_phrase(kw, headline):
                return event_key, config

    return None, None

def check_all_playbooks(item):
    """
    Master function: Checks all playbooks in order of reliability.
    1. SEC Item Codes (highest precision)
    2. Two-Part Logic (required + modifier)
    3. Keyword Fallback (single exact phrase match)
    """
    headline = item.get('title', '')

    # Priority 1: SEC Item Codes (if this is an SEC filing)
    if item.get('is_sec'):
        event_key, config = check_sec_item_codes(item)
        if event_key:
            return event_key, config, 'SEC_ITEM'

    # Priority 2: Two-Part Logic
    event_key, config = check_two_part_logic(headline)
    if event_key:
        return event_key, config, 'TWO_PART'

    # Priority 3: Keyword Fallback
    event_key, config = check_keyword_fallback(headline)
    if event_key:
        return event_key, config, 'KEYWORD'

    return None, None, None

# ==========================================
# 2. THE INTELLIGENT MAPPER (The New Brain)
# ==========================================
# Words that are companies but also common English words. WE MUST IGNORE THESE.
AMBIGUITY_FILTER = {
    # Common words that are also tickers
    "TARGET", "GAP", "BEST", "VISA", "MACY", "ALL", "NOW", "FIVE", "ON", "IT",
    "ARE", "CAN", "SEE", "GO", "NEXT", "OUT", "FOR", "AN", "AT", "BE", "BY",
    # Financial terms that get false-matched
    "CEO", "CFO", "COO", "CTO", "EST", "EPS", "USA", "USD", "ETF", "IPO", "SEC",
    # Legal suffixes
    "INC", "LLC", "CORP", "PLC", "AG", "THE", "AND", "LTD", "CO",
    # Common headline words
    "NEW", "BIG", "TOP", "LOW", "HIGH", "GOOD", "BAD", "UP", "DOWN"
}

def build_ticker_map():
    """
    Downloads the listed-company directory and adds unambiguous short aliases.
    """
    print("...Downloading official stock list (Name -> Ticker map)...")
    try:
        # User-Agent header is required for some downloads
        headers = {'User-Agent': 'Mozilla/5.0'}
        # Official Nasdaq Traded List
        url = "http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
        s = requests.get(url, headers=headers, timeout=20).content
        df = pd.read_csv(
            io.StringIO(s.decode('utf-8')), sep='|', skipfooter=1, engine='python'
        )

        # Keep only normal stocks (avoid test stocks)
        df = df[df['Test Issue'] == 'N']

        full_names = {}
        alias_candidates = {}
        removable_suffixes = (
            "Platforms", "Holdings", "Technologies", "Systems", "Group",
            "Services", "Communications",
        )
        for _, row in df.iterrows():
            ticker = row['Symbol']
            raw_name = str(row['Security Name'])

            clean_name = re.sub(
                r' (Inc|Corp|Corporation|Ltd|PLC|Co|Company|AG)\.?$',
                '', raw_name, flags=re.IGNORECASE,
            )
            clean_name = clean_name.split(' - ')[0]
            clean_name = clean_name.upper().strip()

            if len(clean_name) > 3 and clean_name not in AMBIGUITY_FILTER:
                full_names[clean_name] = ticker
                alias = clean_name
                while True:
                    suffix = next(
                        (word for word in removable_suffixes
                         if alias.endswith(" " + word.upper())),
                        None,
                    )
                    if not suffix:
                        break
                    alias = alias[:-(len(suffix) + 1)].strip()
                if (
                    len(alias) > 3
                    and alias not in AMBIGUITY_FILTER
                    and alias != clean_name
                ):
                    alias_candidates.setdefault(alias, set()).add(ticker)

        name_map = dict(full_names)
        name_map.update({
            alias: next(iter(tickers))
            for alias, tickers in alias_candidates.items()
            if len(tickers) == 1
            and (alias not in full_names or full_names[alias] in tickers)
        })
        name_map = dict(sorted(name_map.items(), key=lambda pair: (-len(pair[0]), pair[0])))
        print(f"-> Mapped {len(name_map)} companies.")
        return name_map
    except Exception as e:
        print(f"Map build failed: {e}")
        return {}

# Populated when scanning, rather than performing network I/O during import.
GLOBAL_TICKER_MAP = {}

# ==========================================
# 3. EXTRACTION LOGIC
# ==========================================
def find_ticker_in_headline(headline):
    headline_upper = headline.upper()

    # STRATEGY 1: Explicit Ticker in Parens "(AAPL)"
    explicit = re.findall(r'\(([A-Z]{2,5})\)', headline)
    if explicit: return explicit[0]

    # Prioritize the longest company names to avoid matching a shorter prefix.
    headline_tokens = set(re.findall(r"[A-Z0-9]+", headline_upper))
    for name, ticker in sorted(GLOBAL_TICKER_MAP.items(), key=lambda pair: -len(pair[0])):
        name_tokens = set(re.findall(r"[A-Z0-9]+", name))
        if not name_tokens.issubset(headline_tokens):
            continue
        if re.search(r'\b' + re.escape(name) + r'\b', headline_upper):
            return ticker
    return None

def get_bulk_market_data(ticker_list):
    if not ticker_list:
        return {}

    valid_data = {}
    skipped = []

    try:
        data = yf.download(ticker_list, period="5d", group_by='ticker', progress=False)
    except Exception as e:
        print(f"  -> Market data download failed: {e}")
        return {}

    for t in ticker_list:
        try:
            df_t = data if len(ticker_list) == 1 else data[t]
            if df_t is None or df_t.empty:
                skipped.append(t)
                continue

            # Drop rows with missing price/volume to avoid NaN propagation
            df_t = df_t[['Close', 'Volume']].dropna()
            if df_t.empty:
                skipped.append(t)
                continue

            price = float(df_t['Close'].iloc[-1])
            volume = float(df_t['Volume'].tail(5).mean())  # Smooth odd days

            if pd.isna(price) or pd.isna(volume) or price <= 0 or volume <= 0:
                skipped.append(t)
                continue

            valid_data[t] = {"price": price, "volume": volume}
        except Exception:
            skipped.append(t)
            continue

    if skipped:
        skipped_preview = ", ".join(skipped[:5])
        suffix = "..." if len(skipped) > 5 else ""
        print(f"  -> Skipped {len(skipped)} ticker(s) with missing market data: {skipped_preview}{suffix}")

    return valid_data

def _atomic_json_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, delete=False,
        prefix=".alpha-cache-", suffix=".tmp",
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except Exception:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise

def _cached_exa_call(key, call, refresh=False):
    """Run an Exa call once per key per TTL and persist it using atomic replace."""
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as cache_file:
                cache = json.load(cache_file)
        except (OSError, json.JSONDecodeError) as error:
            print(f"Exa cache could not be read; continuing without cached data: {error}")
    if not isinstance(cache, dict):
        print("Exa cache has an invalid top-level shape; rebuilding it.")
        cache = {}
    now = time.time()
    cache = {
        cache_key: entry
        for cache_key, entry in cache.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("value"), (str, int, float, bool, list, dict, type(None)))
        and _cache_entry_is_fresh(entry, now)
    }
    cached = cache.get(key)
    if not refresh and cached:
        return cached.get("value")

    value = call()
    cache[key] = {"created_at": now, "value": value}
    try:
        _atomic_json_write(CACHE_FILE, cache)
    except OSError as error:
        print(f"Exa cache could not be written: {error}")
    return value

def _cache_entry_is_fresh(entry, now):
    try:
        age = now - float(entry["created_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age < CACHE_TTL_SECONDS

SEMANTIC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {
            "type": "string",
            "enum": ["BULLISH", "BEARISH", "NEUTRAL"],
        },
        "is_negated_or_rumor": {"type": "boolean"},
        "catalyst_type": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": [
        "direction", "is_negated_or_rumor", "catalyst_type",
        "confidence", "rationale",
    ],
    "additionalProperties": False,
}

RATIONALE_ABBREVIATIONS = re.compile(
    r"\b(?:[A-Za-z]\.){2,}|\b(?:etc|Inc|Corp|Co|Ltd|Jr|Sr|Mr|Mrs|Ms|Dr|"
    r"Prof|No|vs)\.",
    re.IGNORECASE,
)

def _one_sentence_rationale(value):
    """Keep the first complete sentence without splitting on common abbreviations."""
    placeholder = "__ALPHA_ABBREVIATION_PERIOD__"
    safe = RATIONALE_ABBREVIATIONS.sub(
        lambda match: match.group().replace(".", placeholder), str(value)
    )
    ending = re.search(r"[.!?](?=\s|$)", safe)
    if ending:
        safe = safe[:ending.end()]
    sentence = safe.replace(placeholder, ".").strip()
    if sentence and sentence[-1] not in ".!?":
        sentence += "."
    return sentence

NEGATED_CATALYST = re.compile(
    r"\b(?:rules?\s+out|ruled\s+out|denies|denied|dismisses|dismissed|"
    r"no plans to|not planning to|will not|won't)\b.{0,80}\b"
    r"(?:cut|lower|reduce|miss|fail|reject|delay|downgrade|sell)\b",
    re.IGNORECASE,
)
RUMOR_TERMS = re.compile(
    r"\b(?:rumou?red?|speculation|unconfirmed|reportedly|talks? to|"
    r"considering|potential deal|could acquire|may acquire)\b",
    re.IGNORECASE,
)
LOCAL_BULLISH_CATALYST = re.compile(
    r"\b(?:raises? guidance|raises? (?:its )?outlook|beats? estimates|"
    r"tops? expectations|wins? contract|awarded contract|secures? deal|"
    r"lands? deal|fda approves?|positive trial|analyst upgrade|"
    r"raises? dividend|buyback|share repurchase|spinoff|spin-off)\b",
    re.IGNORECASE,
)
LOCAL_BEARISH_CATALYST = re.compile(
    r"\b(?:guidance cut|cuts? guidance|lowers? (?:its )?outlook|"
    r"misses? estimates|below expectations|analyst downgrade|lawsuit|"
    r"bankruptcy|defaults? on|delisting|fda rejects?|trial fails?|"
    r"accounting fraud)\b",
    re.IGNORECASE,
)

def _local_semantic_extraction(headline, rule_config):
    if NEGATED_CATALYST.search(headline):
        return {
            "direction": "NEUTRAL",
            "is_negated_or_rumor": True,
            "catalyst_type": "NEGATED_CATALYST",
            "confidence": 0.96,
            "rationale": "The headline explicitly rules out or denies the negative catalyst.",
        }
    if RUMOR_TERMS.search(headline):
        return {
            "direction": "NEUTRAL",
            "is_negated_or_rumor": True,
            "catalyst_type": rule_config.get("event_key", "RUMOR"),
            "confidence": 0.72,
            "rationale": "The catalyst is presented as unconfirmed or speculative.",
        }
    bullish = bool(LOCAL_BULLISH_CATALYST.search(headline))
    bearish = bool(LOCAL_BEARISH_CATALYST.search(headline))
    if bullish != bearish:
        direction = "BULLISH" if bullish else "BEARISH"
    elif bullish and bearish:
        direction = "NEUTRAL"
    else:
        event_directions = {
            "SEC_Bankruptcy": "BEARISH",
            "SEC_Delisting": "BEARISH",
            "SEC_Material_Agreement": "BULLISH",
            "SEC_Asset_Acquisition": "BULLISH",
            "Bankruptcy": "BEARISH",
            "Lawsuit": "BEARISH",
            "Earnings_Beat": "BULLISH",
            "Earnings_Miss": "BEARISH",
            "Contract_Win": "BULLISH",
            "Spinoff": "BULLISH",
            "Buyback": "BULLISH",
            "Dividend": "BULLISH",
            "Guidance_Hike": "BULLISH",
            "Guidance_Cut": "BEARISH",
            "Analyst_Upgrade": "BULLISH",
            "Analyst_Downgrade": "BEARISH",
        }
        direction = event_directions.get(rule_config.get("event_key"), "NEUTRAL")
    return {
        "direction": direction,
        "is_negated_or_rumor": False,
        "catalyst_type": rule_config.get("event_key", "OTHER"),
        "confidence": 0.58 if direction != "NEUTRAL" else 0.4,
        "rationale": "A local structured classifier identified the catalyst direction.",
    }

def _parse_semantic_output(content):
    if isinstance(content, str):
        content = json.loads(content)
    if not isinstance(content, dict):
        raise ValueError("Exa semantic output must be a JSON object")
    expected_fields = {
        "direction", "is_negated_or_rumor", "catalyst_type",
        "confidence", "rationale",
    }
    if set(content) != expected_fields:
        raise ValueError("Exa semantic output contains missing or unexpected fields")
    direction = content.get("direction")
    confidence = content.get("confidence")
    rationale = _one_sentence_rationale(content.get("rationale", ""))
    if direction not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        raise ValueError("Exa returned an unsupported direction")
    if not isinstance(content.get("is_negated_or_rumor"), bool):
        raise ValueError("Exa returned an invalid negation/rumor flag")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ValueError("Exa returned an invalid confidence")
    if not rationale:
        raise ValueError("Exa rationale must not be empty")
    catalyst_type = str(content.get("catalyst_type", "")).strip()
    if not catalyst_type:
        raise ValueError("Exa returned an empty catalyst type")
    return {
        "direction": direction,
        "is_negated_or_rumor": content["is_negated_or_rumor"],
        "catalyst_type": catalyst_type,
        "confidence": float(confidence),
        "rationale": rationale,
    }

def _exa_contents_excerpt(exa, url, refresh=False):
    if not exa or not url:
        return ""
    key = "contents:" + url

    def fetch():
        response = exa.get_contents(
            [url], highlights={"max_characters": 1200}, text=False
        )
        results = getattr(response, "results", response)
        if not isinstance(results, (list, tuple)):
            results = [results]
        excerpts = []
        for result in results:
            highlights = getattr(result, "highlights", None)
            if highlights:
                excerpts.extend(
                    [str(value) for value in highlights]
                    if isinstance(highlights, (list, tuple)) else [str(highlights)]
                )
        return " ".join(excerpts)[:1200]

    return _cached_exa_call(key, fetch, refresh=refresh) or ""

def _sec_document_excerpt(item):
    url = item.get("url")
    if not url:
        return ""
    headers = {
        "User-Agent": os.environ.get(
            "SEC_USER_AGENT", "alpha-catalyst-eval/1.0 contact@example.com"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return re.sub(r"<[^>]+>", " ", response.text)[:6000]
    except requests.RequestException as error:
        print(f"SEC document fetch failed for {url}: {error}")
        return ""

def semantic_extract(item, rule_config, exa=None, refresh=False, include_method=False):
    headline = item.get("title", "")
    rule_config = dict(rule_config or {})
    rule_config["event_key"] = item.get("event_key", "OTHER")

    def result_with_method(result, method="LOCAL"):
        return (result, method) if include_method else result

    if NEGATED_CATALYST.search(headline):
        return result_with_method(_local_semantic_extraction(headline, rule_config))

    excerpt = ""
    if exa:
        if item.get("is_sec"):
            excerpt = _sec_document_excerpt(item)
        elif item.get("url"):
            try:
                excerpt = _exa_contents_excerpt(exa, item["url"], refresh=refresh)
            except Exception as error:
                print(f"Exa contents lookup failed for {item['url']}: {error}")

    if not exa:
        return result_with_method(_local_semantic_extraction(headline, rule_config))

    prompt = (
        "Independently assess the likely company-specific equity-price impact from the "
        "facts in the headline and source excerpt; do not infer direction from an event "
        "category or playbook label. Consider which company is affected, whether the "
        "event is confirmed or already expected, and any material countervailing context. "
        "Use NEUTRAL when the evidence does not support a clear direction. "
        "A denial, explicit negation, or unconfirmed rumor must not be treated as a "
        "confirmed catalyst; in particular, 'CEO rules out guidance cut' is NEUTRAL. "
        "Return only the requested structured fields. "
        f"Headline: {headline}\nSource excerpt: {excerpt[:1200]}"
    )
    key = "search:" + prompt
    try:
        content = _cached_exa_call(
            key,
            lambda: exa.search(
                prompt, type="auto", output_schema=SEMANTIC_OUTPUT_SCHEMA
            ).output.content,
            refresh=refresh,
        )
        result = _parse_semantic_output(content)
        if NEGATED_CATALYST.search(headline):
            return result_with_method(_local_semantic_extraction(headline, rule_config))
        return result_with_method(result, method="EXA")
    except Exception as error:
        print(f"Exa semantic extraction failed for '{headline[:80]}': {error}")
        return result_with_method(_local_semantic_extraction(headline, rule_config))

def _observed_us_holiday(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day

def _nth_weekday(year, month, weekday, occurrence):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))

def _last_weekday(year, month, weekday):
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    return last - timedelta(days=(last.weekday() - weekday) % 7)

def nyse_holidays(year):
    holidays = {
        _observed_us_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, calendar.MONDAY, 3),
        _nth_weekday(year, 2, calendar.MONDAY, 3),
        _last_weekday(year, 5, calendar.MONDAY),
        _observed_us_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, calendar.MONDAY, 1),
        _nth_weekday(year, 11, calendar.THURSDAY, 4),
        _observed_us_holiday(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_us_holiday(date(year, 6, 19)))
    # Good Friday is two days before Easter Sunday.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    easter = date(year, month, day)
    holidays.add(easter - timedelta(days=2))
    return holidays

def is_nyse_trading_day(day):
    observed_holidays = nyse_holidays(day.year) | nyse_holidays(day.year + 1)
    if day.weekday() >= 5 or day in observed_holidays:
        return False
    return True

def next_nyse_trading_day(day, include_day=True):
    candidate = day if include_day else day + timedelta(days=1)
    while not is_nyse_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate

def previous_nyse_trading_day(day, include_day=False):
    candidate = day if include_day else day - timedelta(days=1)
    while not is_nyse_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate

def nyse_open_sgt(day):
    return datetime.combine(day, datetime_time(9, 30), tzinfo=ET).astimezone(SGT).replace(tzinfo=None)

def trading_schedule(horizon, now=None):
    now_et = now.astimezone(ET) if now and now.tzinfo else (now.replace(tzinfo=ET) if now else datetime.now(ET))
    today = now_et.date()
    entry_day = next_nyse_trading_day(today)
    if (
        not is_nyse_trading_day(today)
        or now_et.time().replace(tzinfo=None) > datetime_time(9, 30)
    ):
        entry_day = next_nyse_trading_day(today, include_day=False)

    exit_day = entry_day
    for _ in range(max(0, int(horizon))):
        exit_day = next_nyse_trading_day(exit_day, include_day=False)
    return nyse_open_sgt(entry_day), nyse_open_sgt(exit_day)

# ==========================================
# 4. MAIN LOOP
# ==========================================
def scan_news(limit=None, no_exa=False, refresh=False):
    load_project_environment()
    potential_signals = []
    tickers_to_check = set()
    all_headlines = []

    if not GLOBAL_TICKER_MAP:
        GLOBAL_TICKER_MAP.update(build_ticker_map())

    print(f"\n{'='*60}")
    print(f"  ALPHA CATALYST EVAL - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    exa = None
    api_key = os.environ.get("EXA_API_KEY")
    if not no_exa and api_key:
        try:
            from exa_py import Exa
            exa = Exa(api_key=api_key)
        except ImportError:
            print("exa-py is unavailable; using the local structured classifier.")
    elif not no_exa:
        print("EXA_API_KEY is unset; using the local structured classifier.")

    # === STEP 1: SEC EDGAR (Highest Priority - Official Filings) ===
    print("\n[1/5] 📋 Scanning SEC EDGAR 8-K Filings...")
    sec_filings = fetch_sec_filings()
    all_headlines.extend(sec_filings)
    print(f"  -> Found {len(sec_filings)} SEC filings")

    # === STEP 2: TARGETED GOOGLE NEWS (Filtered by Trusted Domains) ===
    print("\n[2/5] 🎯 Scanning targeted Google News (trusted sources)...")
    gnews_count = 0
    for url in GNEWS_QUERIES:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            feed = feedparser.parse(url, request_headers=headers)
            for entry in feed.entries[:20]:
                headline = entry.get('title', '')
                if headline:
                    all_headlines.append({
                        "title": headline, "source": "gnews_targeted",
                        "url": entry.get("link", ""), "is_sec": False,
                    })
                    gnews_count += 1
        except Exception as e:
            print(f"Google News feed failed ({url}): {e}")
            continue
    print(f"  -> Found {gnews_count} targeted headlines")

    # === STEP 3: GENERAL RSS FEEDS ===
    print("\n[3/5] 📰 Scanning general RSS feeds...")
    rss_count = 0
    for url in RSS_FEEDS:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            feed = feedparser.parse(url, request_headers=headers)
            for entry in feed.entries[:25]:
                headline = entry.get('title', '')
                if headline:
                    all_headlines.append({
                        "title": headline, "source": url.split('/')[2],
                        "url": entry.get("link", ""), "is_sec": False,
                    })
                    rss_count += 1
        except Exception as e:
            print(f"RSS feed failed ({url}): {e}")
            continue
    print(f"  -> Found {rss_count} RSS headlines")

    # === STEP 4: API DATA SOURCES ===
    print("\n[4/5] 🔌 Fetching API data sources...")

    # Finnhub
    finnhub_news = fetch_finnhub_news()
    for item in finnhub_news:
        item['is_sec'] = False
    all_headlines.extend(finnhub_news)
    print(f"  -> Finnhub: {len(finnhub_news)} headlines")

    # Alpha Vantage
    av_news = fetch_alphavantage_news()
    for item in av_news:
        item['is_sec'] = False
    all_headlines.extend(av_news)
    print(f"  -> AlphaVantage: {len(av_news)} headlines")

    # Polygon
    polygon_news = fetch_polygon_news()
    for item in polygon_news:
        item['is_sec'] = False
    all_headlines.extend(polygon_news)
    print(f"  -> Polygon: {len(polygon_news)} headlines")

    print(f"\n[5/5] 🧠 Analyzing {len(all_headlines)} total items...")

    # === PROCESS ALL HEADLINES ===
    seen_headlines = set()  # Dedup

    for item in all_headlines:
        headline = item.get('title', '')
        if not headline or headline in seen_headlines:
            continue
        seen_headlines.add(headline)

        event_key, config, match_type = check_all_playbooks(item)

        if event_key and config:
            ticker = None
            if 'tickers' in item and item['tickers']:
                ticker = str(item['tickers'][0]).upper()
            else:
                ticker = find_ticker_in_headline(headline)

            if ticker and ticker not in AMBIGUITY_FILTER:
                tickers_to_check.add(ticker)
                rule_direction = config.get('direction', 'NEUTRAL')
                if rule_direction not in {'BULLISH', 'BEARISH'}:
                    rule_direction = 'NEUTRAL'
                item['event_key'] = event_key
                semantic, semantic_method = semantic_extract(
                    item, config, exa=exa, refresh=refresh, include_method=True
                )
                potential_signals.append({
                    "Ticker": ticker,
                    "Event_Key": event_key,
                    "Headline": headline,
                    "URL": item.get("url", ""),
                    "Source": item.get('source', 'unknown'),
                    "Match_Type": match_type,
                    "Story": config.get('story', ''),
                    "Direction": rule_direction,
                    "rule_direction": rule_direction,
                    "semantic_direction": semantic["direction"],
                    "semantic_source": semantic_method,
                    "agreement": rule_direction == semantic["direction"],
                    "semantic_is_negated_or_rumor": semantic["is_negated_or_rumor"],
                    "catalyst_type": semantic["catalyst_type"],
                    "semantic_confidence": semantic["confidence"],
                    "semantic_rationale": semantic["rationale"],
                    "Horizon": config.get('horizon_days', 5),
                })

    print(f"  -> Found {len(potential_signals)} potential signals")

    if not tickers_to_check:
        print("\n[4/4] No tickers to validate.")
        return pd.DataFrame()

    print(f"\n[4/4] Validating {len(tickers_to_check)} tickers with market data...")
    market_map = get_bulk_market_data(list(tickers_to_check))
    final_signals = []

    now = datetime.now(ET)
    for item in potential_signals:
        if limit is not None and len(final_signals) >= max(0, limit):
            break
        t = item['Ticker']
        if t in market_map:
            mkt = market_map[t]
            price = mkt.get('price')
            volume = mkt.get('volume')

            # Skip tickers with bad or insufficient data
            if price is None or volume is None or pd.isna(price) or pd.isna(volume):
                continue

            if price < 2 or volume < 200000:
                continue

            horizon = item.get('Horizon', 5)
            entry_sgt, exit_sgt = trading_schedule(horizon, now=now)
            final_signals.append({
                "Entry_Date": pd.Timestamp(entry_sgt),
                "Ticker": t,
                "actual_entry_date": pd.Timestamp(entry_sgt),
                "Direction": item.get('Direction', 'NEUTRAL'),
                "Price": round(price, 2),
                "Volume": int(volume),
                "Event_Type": item['Event_Key'],
                "Match_Type": item.get('Match_Type', 'KEYWORD'),
                "Story": item.get('Story', ''),
                "Source": item.get('Source', 'rss'),
                "Headline": item['Headline'][:120],
                "URL": item.get("URL", ""),
                "Exit_Date": pd.Timestamp(exit_sgt),
                "actual_exit_date": pd.Timestamp(exit_sgt),
                "rule_direction": item["rule_direction"],
                "semantic_direction": item["semantic_direction"],
                "semantic_source": item["semantic_source"],
                "agreement": item["agreement"],
                "semantic_is_negated_or_rumor": item["semantic_is_negated_or_rumor"],
                "catalyst_type": item["catalyst_type"],
                "semantic_confidence": item["semantic_confidence"],
                "semantic_rationale": item["semantic_rationale"],
                "entry_price": None,
                "spy_entry_price": None,
                "exit_price": None,
                "spy_exit_price": None,
                "raw_stock_return_pct": None,
                "signal_return_pct": None,
                "spy_return_pct": None,
                "market_neutral_alpha_pct": None,
                "excess_alpha_pct": None,
                "rule_excess_alpha_pct": None,
                "semantic_excess_alpha_pct": None,
                "abs_relative_move_pct": None,
                "direction_matched": None,
                "beat_spy_relative": None,
                "neutral_move_classification": None,
                "status": "OPEN",
            })

    print(f"  -> {len(final_signals)} signals passed validation")
    return pd.DataFrame(final_signals)

LEDGER_COLUMNS = [
    "Entry_Date", "Ticker", "Direction", "Price", "Volume", "Event_Type",
    "Match_Type", "Story", "Source", "Headline", "URL", "Exit_Date",
    "actual_entry_date", "actual_exit_date",
    "rule_direction", "semantic_direction", "semantic_source", "agreement",
    "semantic_is_negated_or_rumor", "catalyst_type", "semantic_confidence",
    "semantic_rationale", "entry_price", "spy_entry_price", "exit_price",
    "spy_exit_price", "raw_stock_return_pct", "signal_return_pct",
    "spy_return_pct", "market_neutral_alpha_pct", "excess_alpha_pct",
    "rule_excess_alpha_pct", "semantic_excess_alpha_pct",
    "abs_relative_move_pct", "direction_matched", "beat_spy_relative",
    "neutral_move_classification", "status", "evaluation_note",
]

def load_ledger():
    if os.path.exists(LEDGER_FILE):
        ledger = pd.read_csv(LEDGER_FILE)
    else:
        ledger = pd.DataFrame(columns=LEDGER_COLUMNS)
    for column in LEDGER_COLUMNS:
        if column not in ledger:
            ledger[column] = None
    if "Status" in ledger.columns and "status" in ledger.columns:
        ledger["status"] = ledger["status"].fillna(ledger["Status"])
    ledger["status"] = ledger["status"].fillna("OPEN")
    ledger["evaluation_note"] = ledger["evaluation_note"].astype("object")
    for column in (
        "Entry_Date", "Exit_Date", "actual_entry_date", "actual_exit_date"
    ):
        ledger[column] = pd.to_datetime(
            ledger[column], errors="coerce", format="mixed"
        )
    ledger["rule_direction"] = ledger["rule_direction"].fillna(ledger.get("Direction"))
    has_initial_semantic = ledger["semantic_source"].astype("string").str.upper().isin(
        {"EXA", "LOCAL"}
    )
    semantic_columns = [
        "semantic_direction", "agreement", "semantic_is_negated_or_rumor",
        "catalyst_type", "semantic_confidence", "semantic_rationale",
        "semantic_excess_alpha_pct",
    ]
    for column in semantic_columns:
        ledger[column] = ledger[column].astype("object")
    ledger.loc[~has_initial_semantic, semantic_columns] = None
    for column in ("direction_matched", "beat_spy_relative", "neutral_move_classification"):
        ledger[column] = ledger[column].astype("object")
    return ledger[LEDGER_COLUMNS]

def save_ledger(ledger):
    directory = os.path.dirname(os.path.abspath(LEDGER_FILE))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=directory,
        delete=False, prefix=".signals-ledger-", suffix=".tmp",
    )
    try:
        with handle:
            ledger.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, LEDGER_FILE)
    except Exception:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise

def manage_ledger(new_df):
    ledger = load_ledger()
    if not new_df.empty:
        print(f"\nDETECTED {len(new_df)} ALPHA OPPORTUNITIES\n")
        for _, row in new_df.iterrows():
            print(f"  {row['Ticker']:6} | ${row['Price']:<8.2f} | {row['Direction']} | {row['Event_Type']}")
        ledger = pd.concat([ledger, new_df], ignore_index=True)
    else:
        print("\nNo new signals detected.")
    ledger = deduplicate_signals(ledger)
    save_ledger(ledger)
    print(f"Saved primary ledger to {LEDGER_FILE}")
    return ledger

def deduplicate_signals(ledger):
    seen = set()
    keep_indices = []
    for index, row in ledger.iterrows():
        entry = pd.to_datetime(row.get("Entry_Date"), errors="coerce")
        if pd.isna(entry):
            entry = pd.to_datetime(row.get("actual_entry_date"), errors="coerce")
        ticker = "" if pd.isna(row.get("Ticker")) else str(row.get("Ticker")).strip().upper()
        event = "" if pd.isna(row.get("Event_Type")) else str(row.get("Event_Type")).strip()
        if not pd.isna(entry):
            key = ("dated", entry.isoformat(), ticker, event)
        else:
            source = "" if pd.isna(row.get("Source")) else str(row.get("Source")).strip()
            headline = "" if pd.isna(row.get("Headline")) else str(row.get("Headline")).strip()
            url = "" if pd.isna(row.get("URL")) else str(row.get("URL")).strip()
            key = ("undated", ticker, event, source, headline, url)
        if key not in seen:
            seen.add(key)
            keep_indices.append(index)
    return ledger.loc[keep_indices].reset_index(drop=True)

def _daily_open(data, ticker, day):
    try:
        frame = data
        if isinstance(data.columns, pd.MultiIndex):
            matching_level = next(
                (level for level in range(data.columns.nlevels)
                 if ticker in data.columns.get_level_values(level)),
                None,
            )
            if matching_level is None:
                return None
            frame = data.xs(ticker, axis=1, level=matching_level)
        frame = frame.dropna(subset=["Open"])
        target = pd.Timestamp(day)
        matching = frame.loc[frame.index.date == target.date()]
        if matching.empty:
            return None
        return float(matching["Open"].iloc[0])
    except (KeyError, AttributeError, IndexError, TypeError):
        return None

def _directional_return(start_price, end_price, direction):
    if not start_price or not end_price:
        return None
    raw_return = (end_price / start_price - 1) * 100
    if direction == "BULLISH":
        return raw_return
    if direction == "BEARISH":
        return -raw_return
    return None

def _percent_return(start_price, end_price):
    if not start_price or not end_price:
        return None
    return (end_price / start_price - 1) * 100

def _market_neutral_alpha(raw_stock_return, spy_return, direction):
    if direction == "BULLISH":
        return raw_stock_return - spy_return
    if direction == "BEARISH":
        return spy_return - raw_stock_return
    return None

def recalculate_performance_metrics(ledger):
    """Recompute derived benchmark metrics from already recorded prices."""
    for index, row in ledger.iterrows():
        if row["status"] != "CLOSED":
            continue
        prices = [
            pd.to_numeric(row[column], errors="coerce")
            for column in ("entry_price", "exit_price", "spy_entry_price", "spy_exit_price")
        ]
        if any(pd.isna(price) or price <= 0 for price in prices):
            continue
        entry_price, exit_price, spy_entry_price, spy_exit_price = prices
        raw_return = _percent_return(entry_price, exit_price)
        spy_return = _percent_return(spy_entry_price, spy_exit_price)
        if raw_return is None or spy_return is None:
            continue

        relative_move = raw_return - spy_return
        direction = str(row["rule_direction"]).upper()
        alpha = _market_neutral_alpha(raw_return, spy_return, direction)
        ledger.at[index, "raw_stock_return_pct"] = raw_return
        ledger.at[index, "signal_return_pct"] = _directional_return(
            entry_price, exit_price, direction
        )
        ledger.at[index, "spy_return_pct"] = spy_return
        ledger.at[index, "market_neutral_alpha_pct"] = alpha
        ledger.at[index, "excess_alpha_pct"] = alpha
        ledger.at[index, "rule_excess_alpha_pct"] = alpha
        ledger.at[index, "abs_relative_move_pct"] = abs(relative_move)
        if direction == "BULLISH":
            ledger.at[index, "direction_matched"] = raw_return > 0
            ledger.at[index, "beat_spy_relative"] = raw_return > spy_return
            ledger.at[index, "neutral_move_classification"] = None
        elif direction == "BEARISH":
            ledger.at[index, "direction_matched"] = raw_return < 0
            ledger.at[index, "beat_spy_relative"] = raw_return < spy_return
            ledger.at[index, "neutral_move_classification"] = None
        else:
            ledger.at[index, "direction_matched"] = (
                abs(relative_move) <= NEUTRAL_RELATIVE_MOVE_THRESHOLD_PCT
            )
            ledger.at[index, "beat_spy_relative"] = None
            ledger.at[index, "neutral_move_classification"] = (
                "NOISE_CONFIRMED"
                if abs(relative_move) <= NEUTRAL_RELATIVE_MOVE_THRESHOLD_PCT
                else "MATERIAL_SURPRISE"
            )

        semantic_direction = (
            "" if pd.isna(row["semantic_direction"])
            else str(row["semantic_direction"]).upper()
        )
        semantic_alpha = _market_neutral_alpha(
            raw_return, spy_return, semantic_direction
        )
        ledger.at[index, "semantic_excess_alpha_pct"] = semantic_alpha
    return ledger

def evaluate_ledger(now=None):
    ledger = load_ledger()
    now_sgt = now.astimezone(SGT).replace(tzinfo=None) if now and now.tzinfo else (
        now if now else datetime.now(SGT).replace(tzinfo=None)
    )

    # Older evaluator runs marked non-trading legacy dates terminal. Reopen
    # only those records once so their dates can be aligned to real sessions.
    for index, row in ledger.iterrows():
        note = row["evaluation_note"]
        if (
            row["status"] == "UNVERIFIABLE"
            and isinstance(note, str)
            and "is not an NYSE trading day" in note
            and pd.isna(row["actual_entry_date"])
            and pd.isna(row["actual_exit_date"])
        ):
            ledger.at[index, "status"] = "OPEN"

    def mark_unverifiable(index, ticker, reason):
        ledger.at[index, "status"] = "UNVERIFIABLE"
        ledger.at[index, "evaluation_note"] = reason
        print(f"UNVERIFIABLE {ticker}: {reason}")

    for index, row in ledger.iterrows():
        if row["status"] != "OPEN":
            continue
        ticker = "" if pd.isna(row["Ticker"]) else str(row["Ticker"]).strip().upper()
        if not ticker:
            mark_unverifiable(index, "unknown ticker", "Ticker is missing.")
            continue
        exit_date = pd.to_datetime(row["Exit_Date"], errors="coerce")
        if pd.isna(exit_date):
            mark_unverifiable(index, ticker, "Exit_Date is missing or invalid.")
            continue
        entry_date = pd.to_datetime(row["Entry_Date"], errors="coerce")
        if pd.isna(entry_date):
            mark_unverifiable(index, ticker, "Entry_Date is missing or invalid.")
            continue
        if exit_date.to_pydatetime() > now_sgt:
            continue

        adjustment_notes = []
        saved_entry = pd.to_datetime(row["actual_entry_date"], errors="coerce")
        if not pd.isna(saved_entry):
            entry_day = saved_entry.date()
            if not is_nyse_trading_day(entry_day):
                original_entry_day = entry_day
                entry_day = next_nyse_trading_day(entry_day, include_day=False)
                adjustment_notes.append(
                    f"Recorded actual entry {original_entry_day.isoformat()} was not an "
                    f"NYSE session; moved to next session {entry_day.isoformat()}."
                )
            elif entry_day != entry_date.date():
                scheduled_entry_day = entry_date.date()
                adjustment_notes.append(
                    f"Scheduled entry {scheduled_entry_day.isoformat()} "
                    f"({scheduled_entry_day.strftime('%A')}) aligned to actual NYSE "
                    f"session {entry_day.isoformat()}."
                )
        else:
            scheduled_entry_day = entry_date.date()
            entry_day = (
                scheduled_entry_day if is_nyse_trading_day(scheduled_entry_day)
                else next_nyse_trading_day(scheduled_entry_day, include_day=False)
            )
            if entry_day != scheduled_entry_day:
                adjustment_notes.append(
                    f"Scheduled entry {scheduled_entry_day.isoformat()} "
                    f"({scheduled_entry_day.strftime('%A')}) was not an NYSE session; "
                    f"actual entry moved to next session {entry_day.isoformat()}."
                )
            ledger.at[index, "actual_entry_date"] = nyse_open_sgt(entry_day)

        saved_exit = pd.to_datetime(row["actual_exit_date"], errors="coerce")
        if not pd.isna(saved_exit):
            exit_day = saved_exit.date()
            if not is_nyse_trading_day(exit_day):
                original_exit_day = exit_day
                exit_day = next_nyse_trading_day(exit_day, include_day=False)
                adjustment_notes.append(
                    f"Recorded actual exit {original_exit_day.isoformat()} was not an "
                    f"NYSE session; moved to next session {exit_day.isoformat()}."
                )
            elif exit_day != exit_date.date():
                scheduled_exit_day = exit_date.date()
                adjustment_notes.append(
                    f"Scheduled exit {scheduled_exit_day.isoformat()} "
                    f"({scheduled_exit_day.strftime('%A')}) aligned to actual NYSE "
                    f"session {exit_day.isoformat()}."
                )
        else:
            scheduled_exit_day = exit_date.date()
            exit_day = (
                scheduled_exit_day if is_nyse_trading_day(scheduled_exit_day)
                else next_nyse_trading_day(scheduled_exit_day, include_day=False)
            )
            if exit_day != scheduled_exit_day:
                adjustment_notes.append(
                    f"Scheduled exit {scheduled_exit_day.isoformat()} "
                    f"({scheduled_exit_day.strftime('%A')}) was not an NYSE session; "
                    f"actual exit moved to next session {exit_day.isoformat()}."
                )

        if exit_day <= entry_day:
            exit_day = next_nyse_trading_day(entry_day, include_day=False)
            adjustment_notes.append(
                f"Actual exit moved to {exit_day.isoformat()} so it follows the actual entry."
            )
        ledger.at[index, "actual_exit_date"] = nyse_open_sgt(exit_day)

        start = entry_day.isoformat()
        end = (exit_day + timedelta(days=1)).isoformat()
        try:
            bars = yf.download(
                [ticker, "SPY"], start=start, end=end,
                interval="1d", auto_adjust=False, progress=False, group_by="ticker",
            )
        except Exception as error:
            reason = " ".join(adjustment_notes + [f"Price download failed: {error}"])
            mark_unverifiable(index, ticker, reason)
            continue
        entry_price = _daily_open(bars, ticker, entry_day)
        exit_price = _daily_open(bars, ticker, exit_day)
        spy_entry = _daily_open(bars, "SPY", entry_day)
        spy_exit = _daily_open(bars, "SPY", exit_day)
        missing_bars = [
            f"{symbol} {day.isoformat()}"
            for symbol, day, value in (
                (ticker, entry_day, entry_price),
                (ticker, exit_day, exit_price),
                ("SPY", entry_day, spy_entry),
                ("SPY", exit_day, spy_exit),
            )
            if value is None
        ]
        if missing_bars:
            reason = " ".join(adjustment_notes + [
                "Missing exact-date daily open bar(s): " + ", ".join(missing_bars) + "."
            ])
            mark_unverifiable(
                index,
                ticker,
                reason,
            )
            continue
        ledger.at[index, "entry_price"] = entry_price
        ledger.at[index, "spy_entry_price"] = spy_entry
        ledger.at[index, "exit_price"] = exit_price
        ledger.at[index, "spy_exit_price"] = spy_exit
        ledger.at[index, "status"] = "CLOSED"
        ledger.at[index, "evaluation_note"] = " ".join(adjustment_notes)
    ledger = recalculate_performance_metrics(ledger)
    save_ledger(ledger)
    write_performance_report(ledger)
    return ledger

def write_performance_report(ledger):
    closed = ledger[ledger["status"] == "CLOSED"].copy()
    unverifiable = ledger[ledger["status"] == "UNVERIFIABLE"].copy()
    invalidated = ledger[ledger["status"] == "INVALID_SIGNAL"].copy()
    open_rows = ledger[ledger["status"] == "OPEN"]

    def summary(values):
        usable = pd.to_numeric(values, errors="coerce").dropna()
        if usable.empty:
            return "No evaluated signals yet."
        win_rate = (usable > 0).mean() * 100
        return f"{len(usable)} signals; win rate {win_rate:.1f}%; mean excess alpha {usable.mean():.2f}%."

    semantic_source = closed["semantic_source"].astype("string").fillna("LEGACY")
    exa_rows = closed[semantic_source == "EXA"]
    local_rows = closed[semantic_source == "LOCAL"]
    legacy_rows = closed[semantic_source == "LEGACY"]

    def agreement_summary(rows):
        agreements = rows["agreement"]
        known = agreements.notna()
        if not known.any():
            return "No recorded semantic comparisons."
        matches = agreements[known].astype("string").str.lower().isin({"true", "1"})
        return f"{int(matches.sum())}/{len(matches)} agree ({matches.mean() * 100:.1f}%)."

    adjusted_dates = ledger[
        ledger["evaluation_note"].astype("string").str.contains(
            r"Scheduled (?:entry|exit)|Actual exit moved", regex=True, na=False
        )
    ]
    direction_labels = closed["rule_direction"].astype("string").str.upper()
    directional_rows = closed[direction_labels.isin({"BULLISH", "BEARISH"})]
    matched = directional_rows["direction_matched"].astype("string").str.lower().eq("true")
    lines = [
        "# Alpha Catalyst Evaluation",
        "",
        "Performance uses market-open prices for the signal and SPY over the actual entry/exit sessions.",
        "",
        f"- Ledger coverage: {len(closed)} CLOSED, {len(open_rows)} OPEN, "
        f"{len(unverifiable)} UNVERIFIABLE, {len(invalidated)} INVALID_SIGNAL.",
        f"- Rule-direction performance (all evaluated rows): {summary(closed['rule_excess_alpha_pct'])}",
        f"- Direction matched: {int(matched.sum())}/{len(directional_rows)} directional signals.",
        f"- Rule-direction performance (same Exa-evaluated rows): {summary(exa_rows['rule_excess_alpha_pct'])}",
        f"- Exa-semantic-direction performance: {summary(exa_rows['semantic_excess_alpha_pct'])}",
        f"- Local-fallback semantic performance: {summary(local_rows['semantic_excess_alpha_pct'])}",
        f"- Legacy rows without a recorded semantic run: {len(legacy_rows)} (excluded from semantic breakdown).",
        f"- Rule-vs-Exa agreement: {agreement_summary(exa_rows)}",
        f"- Rule-vs-local-fallback agreement: {agreement_summary(local_rows)}",
        f"- Neutral checks: {(closed['neutral_move_classification'] == 'NOISE_CONFIRMED').sum()} NOISE_CONFIRMED, "
        f"{(closed['neutral_move_classification'] == 'MATERIAL_SURPRISE').sum()} MATERIAL_SURPRISE.",
        "",
        "Only CLOSED rows with available exact-date market bars are included. Neutral directions are excluded from directional return statistics.",
    ]
    if not adjusted_dates.empty:
        lines.extend(["", "## Trading date adjustments"])
        for _, row in adjusted_dates.iterrows():
            ticker = "unknown ticker" if pd.isna(row["Ticker"]) else str(row["Ticker"])
            note = str(row["evaluation_note"])
            lines.append(
                f"- {ticker}: scheduled {row['Entry_Date']} to {row['Exit_Date']}; "
                f"actual {row['actual_entry_date']} to {row['actual_exit_date']}. {note}"
            )
    if not unverifiable.empty:
        lines.extend(["", "## UNVERIFIABLE rows (excluded from performance)"])
        for _, row in unverifiable.iterrows():
            ticker = "unknown ticker" if pd.isna(row["Ticker"]) else str(row["Ticker"])
            entry_date = str(row["Entry_Date"]) if not pd.isna(row["Entry_Date"]) else "unknown entry"
            exit_date = str(row["Exit_Date"]) if not pd.isna(row["Exit_Date"]) else "unknown exit"
            actual_entry = (
                str(row["actual_entry_date"])
                if not pd.isna(row["actual_entry_date"]) else "not assigned"
            )
            actual_exit = (
                str(row["actual_exit_date"])
                if not pd.isna(row["actual_exit_date"]) else "not assigned"
            )
            note = (
                "No evaluation details recorded."
                if pd.isna(row["evaluation_note"]) or not row["evaluation_note"]
                else str(row["evaluation_note"])
            )
            lines.append(
                f"- {ticker}: scheduled {entry_date} to {exit_date}; "
                f"actual {actual_entry} to {actual_exit}: {note}"
            )
    if not invalidated.empty:
        lines.extend(["", "## INVALID_SIGNAL rows (excluded from performance)"])
        for _, row in invalidated.iterrows():
            ticker = "unknown ticker" if pd.isna(row["Ticker"]) else str(row["Ticker"])
            note = (
                "No longer matches the exact rule playbook."
                if pd.isna(row["evaluation_note"]) or not row["evaluation_note"]
                else str(row["evaluation_note"])
            )
            lines.append(f"- {ticker} ({row['Entry_Date']}): {row['Event_Type']} - {note}")
    lines.append("")

    directory = os.path.dirname(os.path.abspath(REPORT_FILE))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, delete=False,
        prefix=".performance-report-", suffix=".tmp",
    )
    try:
        with handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, REPORT_FILE)
    except Exception:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise

def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan catalyst headlines and evaluate signals.")
    parser.add_argument("--scan", action="store_true", help="Ingest headlines and append new signals.")
    parser.add_argument("--evaluate", action="store_true", help="Close due OPEN signals and report performance.")
    parser.add_argument("--scan-only", action="store_true", help="Do not evaluate after scanning.")
    parser.add_argument("--limit", type=int, help="Maximum new signals to append in one scan.")
    parser.add_argument("--no-exa", action="store_true", help="Use the local structured classifier only.")
    parser.add_argument("--refresh", action="store_true", help="Bypass valid Exa cache entries.")
    args = parser.parse_args(argv)
    if not args.scan and not args.evaluate:
        args.scan = True
    if args.scan:
        manage_ledger(scan_news(limit=args.limit, no_exa=args.no_exa, refresh=args.refresh))
    if args.evaluate or (args.scan and not args.scan_only):
        evaluate_ledger()

if __name__ == "__main__":
    main()