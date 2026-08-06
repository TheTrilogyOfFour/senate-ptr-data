"""Fetch Senate STOCK Act PTR filings from efts.senate.gov and merge into
aggregate/all_transactions.json in the senate_stock_watcher field format.

Usage:
    python scripts/fetch.py                          # last 14 days
    python scripts/fetch.py --from 2021-01-01 --to 2021-12-31   # backfill range

Deduplicates on ptr_link (the UUID in the URL is stable across re-fetches).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

_HEADERS = {"User-Agent": "senate-ptr-data/1.0 (github.com/vandijkray/senate-ptr-data)"}
_EFTS_URL = "https://efts.senate.gov/LATEST/search-results"
_PAGE_SIZE = 250
_AGGREGATE_PATH = Path(__file__).parent.parent / "aggregate" / "all_transactions.json"


def _efts_fetch_page(from_date: str, to_date: str, start: int) -> dict:
    params = urllib.parse.urlencode({
        "q": json.dumps({"source": "ptr"}),
        "dateRange": "custom",
        "fromDate": from_date,
        "toDate": to_date,
        "limit": _PAGE_SIZE,
        "start": start,
    })
    url = f"{_EFTS_URL}?{params}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_range(from_date: str, to_date: str) -> list[dict]:
    """Fetch all PTR filings between from_date and to_date (YYYY-MM-DD).

    The EFTS dateRange filter applies to filing_date (disclosure date), not
    transaction_date — correct for incremental pulls.
    """
    all_hits: list[dict] = []
    start = 0
    total = None

    while True:
        try:
            data = _efts_fetch_page(from_date, to_date, start)
        except urllib.error.URLError as exc:
            print(f"ERROR fetching page start={start}: {exc}", file=sys.stderr)
            sys.exit(1)

        hits = data.get("hits", {})
        if total is None:
            total = hits.get("total", {}).get("value", 0)
            print(f"Total filings in range: {total}")

        page_hits = hits.get("hits", [])
        all_hits.extend(page_hits)
        print(f"  fetched {len(all_hits)}/{total}")

        if len(page_hits) < _PAGE_SIZE or len(all_hits) >= total:
            break

        start += _PAGE_SIZE
        time.sleep(0.5)

    return all_hits


def _normalise(hit: dict) -> dict | None:
    """Convert one EFTS hit to senate_stock_watcher field format."""
    src = hit.get("_source", {})

    first = (src.get("first_name") or "").strip()
    last = (src.get("last_name") or "").strip()
    # EFTS sometimes has a combined senator field too; fall back if names missing.
    senator = (
        f"{first} {last}".strip()
        or src.get("senator_name", "").strip()
        or src.get("senator", "").strip()
    )
    if not senator:
        return None

    tx_date = (
        src.get("transaction_date")
        or src.get("date")
        or ""
    ).strip()
    if not tx_date:
        return None

    # Disclosure/filing date — EFTS uses filing_date or disclosure_date
    filing_date = (
        src.get("filing_date")
        or src.get("disclosure_date")
        or tx_date
    ).strip()

    # Build ptr_link from _id if not present in _source
    ptr_link = src.get("ptr_link") or src.get("doc_link") or ""
    if not ptr_link and hit.get("_id"):
        ptr_link = f"https://efdsearch.senate.gov/search/view/ptr/{hit['_id']}/"

    return {
        "transaction_date": tx_date,
        "owner": src.get("owner", "").strip() or "self",
        "ticker": (src.get("ticker") or "").strip().upper() or "--",
        "asset_description": (src.get("asset_description") or "").strip(),
        "asset_type": src.get("asset_type", ""),
        "type": src.get("type", "").strip(),
        "amount": src.get("amount", "").strip(),
        "comment": src.get("comment", ""),
        "senator": senator,
        "disclosure_date": filing_date,
        "ptr_link": ptr_link,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default=None,
                        help="Start date YYYY-MM-DD (default: 14 days ago)")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    today = date.today()
    from_date = args.from_date or (today - timedelta(days=14)).isoformat()
    to_date = args.to_date or today.isoformat()

    print(f"Fetching Senate PTR filings {from_date} → {to_date}")

    hits = fetch_range(from_date, to_date)
    new_rows = [r for r in (_normalise(h) for h in hits) if r is not None]
    print(f"Normalised {len(new_rows)} rows from {len(hits)} hits")

    existing: list[dict] = json.loads(_AGGREGATE_PATH.read_text())
    existing_links: set[str] = {r.get("ptr_link", "") for r in existing}

    added = [r for r in new_rows if r["ptr_link"] not in existing_links]
    print(f"New (not already in aggregate): {len(added)}")

    if not added:
        print("Nothing new — aggregate unchanged.")
        return

    merged = existing + added
    # Sort by disclosure_date descending so newest filings are first
    def _sort_key(r: dict) -> tuple:
        raw = r.get("disclosure_date") or r.get("transaction_date") or ""
        # Dates are MM/DD/YYYY — convert to YYYY-MM-DD for sorting
        parts = raw.split("/")
        if len(parts) == 3:
            return (parts[2], parts[0], parts[1])
        return (raw, "", "")

    merged.sort(key=_sort_key, reverse=True)

    _AGGREGATE_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    print(f"Wrote {len(merged)} rows to {_AGGREGATE_PATH}")


if __name__ == "__main__":
    main()
