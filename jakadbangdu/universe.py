"""Trading universe = NSE's official Nifty 200 constituent list, downloaded at runtime and cached.
Nothing is hardcoded: index membership changes at each rebalance, so the bot refreshes the list monthly."""
import csv
import io
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("jakad")

URLS = ("https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
        "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept": "text/csv,*/*", "Referer": "https://www.niftyindices.com/"}
REFRESH_DAYS = 30


def parse(text: str) -> list[str]:
    """Symbols from NSE's CSV (columns: Company Name, Industry, Symbol, Series, ISIN Code)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows or "Symbol" not in rows[0]:
        raise ValueError("unexpected Nifty 200 CSV format (no 'Symbol' column)")
    syms = [r["Symbol"].strip() for r in rows if r.get("Symbol", "").strip()]
    if len(syms) < 150:
        raise ValueError(f"Nifty 200 list looks wrong: only {len(syms)} symbols")
    return syms


def fetch() -> str:
    import requests
    err = None
    for u in URLS:
        try:
            r = requests.get(u, headers=HEADERS, timeout=30)
            r.raise_for_status()
            parse(r.text)          # validate before trusting it
            return r.text
        except Exception as e:
            err = e
            log.warning("Nifty 200 download from %s failed: %s", u, e)
    raise RuntimeError(f"could not download the Nifty 200 list: {err}")


def load(cache: Path, fetcher=fetch, now=None) -> list[str]:
    """Cached copy if fresh (<30 days); else download; if the download fails, use the stale copy and warn."""
    now = now or time.time()
    if cache.exists() and now - cache.stat().st_mtime < REFRESH_DAYS * 86400:
        return parse(cache.read_text(encoding="utf-8"))
    try:
        text = fetcher()
        parse(text)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        log.info("Nifty 200 list refreshed (%d symbols)", len(parse(text)))
        return parse(text)
    except Exception as e:
        if cache.exists():
            log.warning("Nifty 200 refresh failed (%s); using the saved copy from %s", e,
                        time.strftime("%Y-%m-%d", time.localtime(cache.stat().st_mtime)))
            return parse(cache.read_text(encoding="utf-8"))
        raise RuntimeError(f"{e}. No saved copy at {cache}. Pass --symbols to trade a list you choose, "
                           f"or save NSE's ind_nifty200list.csv to that path.") from e


def configured() -> list[str] | None:
    """Owner-chosen list from JAKAD_SYMBOLS (comma separated; trading symbols or company names). None if unset."""
    raw = os.getenv("JAKAD_SYMBOLS", "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] or None


def choose(cli: str | None, cache: Path) -> list[str]:
    """Priority: --symbols, then JAKAD_SYMBOLS, then NSE Nifty 200."""
    if cli:
        return [x.strip() for x in cli.split(",") if x.strip()]
    return configured() or load(cache)
