"""Fetch Senate STOCK Act PTR filings from efdsearch.senate.gov.

Uses Playwright (headless Chromium) to:
  1. Accept the prohibition agreement.
  2. Submit the search form with date range, collecting all PTR filing links.
  3. Visit each PTR view page within the same browser session to extract
     individual transactions (ticker, type, amount, transaction date).
  4. Merge new transactions into aggregate/all_transactions.json.

Each row in the aggregate is one *transaction* (not one PTR filing), so the
format matches senatestockwatcher.com's pre-parsed JSON:

  {
    "senator":          "Thomas H Tuberville",
    "disclosure_date":  "08/05/2026",   # MM/DD/YYYY — date PTR was filed
    "transaction_date": "07/15/2026",   # date of the actual trade
    "ticker":           "NVDA",
    "asset_description":"NVIDIA Corp",
    "asset_type":       "Stock",
    "type":             "Purchase",
    "amount":           "$15,001 - $50,000",
    "comment":          "",
    "owner":            "self",
    "ptr_link":         "https://efdsearch.senate.gov/search/view/ptr/…/"
  }

Usage:
    python scripts/fetch.py                          # last 14 days
    python scripts/fetch.py --from 2021-01-01 --to 2021-12-31

Requires: pip install playwright && playwright install chromium --with-deps
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

_HOME_URL = "https://efdsearch.senate.gov/search/home/"
_AGGREGATE_PATH = Path(__file__).parent.parent / "aggregate" / "all_transactions.json"


def _to_mdy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _normalise_search_row(row: list) -> dict | None:
    """Parse one DataTables search-result row (5 columns) into filing metadata.

    Columns: [0] First name, [1] Last name, [2] Office,
             [3] Report type + link HTML, [4] Date filed MM/DD/YYYY.

    Returns None for non-PTR report types (annual reports, extension notices,
    etc.) since those require a different parsing strategy.
    """
    if not isinstance(row, list) or len(row) < 5:
        return None

    first = _strip_tags(row[0])
    last  = _strip_tags(row[1])
    senator = f"{first} {last}".strip()
    if not senator:
        return None

    link_m = re.search(r'href=["\']([^"\']+)["\']', row[3])
    if not link_m:
        return None
    path = link_m.group(1)
    ptr_link = ("https://efdsearch.senate.gov" + path) if path.startswith("/") else path

    # Only scrape actual PTR view pages (not annual reports or extension notices)
    if "/ptr/" not in ptr_link:
        return None

    filed_date = _strip_tags(row[4])
    return {
        "senator": senator,
        "disclosure_date": filed_date,
        "ptr_link": ptr_link,
    }


def _scrape_ptr_transactions(page, filing: dict) -> list[dict]:
    """Navigate to one PTR view page and extract individual transactions.

    Returns a list of transaction dicts (one per row in the PTR table).
    The browser session (with the efdsearch.senate.gov agreement cookie) must
    already be active — the PTR view page is behind the same Akamai wall.

    PTR table columns (order may vary by filing era):
      Asset Name | Asset Type | Transaction Type | Transaction Date |
      Notification Date | Amount | Comment

    Falls back gracefully: if the page structure doesn't match, returns [].
    """
    senator       = filing["senator"]
    disclosure_date = filing["disclosure_date"]
    ptr_link      = filing["ptr_link"]

    try:
        page.goto(ptr_link, wait_until="domcontentloaded", timeout=15_000)
    except Exception as exc:
        print(f"  WARN: could not load {ptr_link}: {exc}")
        return []

    # Wait for the transaction table (class "table" on efdsearch PTR pages)
    try:
        page.wait_for_selector("table.table", timeout=10_000)
    except Exception:
        # Some PTRs have no transactions (e.g. correction notices)
        return []

    # Extract header + rows
    raw = page.evaluate("""
        () => {
            const tables = document.querySelectorAll('table.table');
            const results = [];
            for (const tbl of tables) {
                const headers = Array.from(
                    tbl.querySelectorAll('thead th, thead td')
                ).map(th => th.innerText.trim().toLowerCase());
                const rows = Array.from(tbl.querySelectorAll('tbody tr')).map(tr =>
                    Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())
                );
                results.push({headers, rows});
            }
            return results;
        }
    """)

    transactions = []
    for table in raw:
        headers = table["headers"]
        rows    = table["rows"]

        # Map column names to indices — exact match first to avoid "type" matching
        # "asset type" via substring. Actual efdsearch headers (as of 2026-08):
        #   ['#', 'transaction date', 'owner', 'ticker', 'asset name',
        #    'asset type', 'type', 'amount', 'comment']
        def _col(candidates: list[str]) -> int | None:
            for c in candidates:
                for i, h in enumerate(headers):
                    if h == c:                   # exact — checked first
                        return i
                for i, h in enumerate(headers):
                    if h.startswith(c + " "):    # "transaction date" → matches
                        return i
            return None

        i_asset   = _col(["asset name", "issuer name"])
        i_type    = _col(["asset type"])
        i_tx_type = _col(["type", "purchase/sale", "transaction type"])  # "type" exact
        i_tx_date = _col(["transaction date", "trade date"])
        i_amount  = _col(["amount"])
        i_comment = _col(["comment"])
        i_ticker  = _col(["ticker", "symbol"])
        i_owner   = _col(["owner"])

        if i_asset is None and i_tx_date is None:
            continue  # not a transaction table

        for row in rows:
            if not row:
                continue

            def _get(idx: int | None) -> str:
                return row[idx].strip() if idx is not None and idx < len(row) else ""

            asset_name = _get(i_asset)
            if not asset_name or asset_name == "--":
                continue  # blank row or header repeated

            # Ticker extraction — try dedicated column first, then asset_name patterns
            if i_ticker is not None:
                ticker = _get(i_ticker) or "--"
            else:
                # "Company Name (TICKER)" — ticker at end in parens
                m = re.search(r'\(([A-Z]{1,5}(?:\.[A-Z])?)\)\s*$', asset_name)
                if not m:
                    # "TICKER (Description)" — ticker at start before paren
                    m = re.match(r'^([A-Z]{1,5}(?:\.[A-Z])?)\s*[\(\[]', asset_name)
                ticker = m.group(1) if m else "--"

            transactions.append({
                "senator":          senator,
                "disclosure_date":  disclosure_date,
                "transaction_date": _get(i_tx_date),
                "ticker":           ticker,
                "asset_description": asset_name,
                "asset_type":       _get(i_type),
                "type":             _get(i_tx_type),
                "amount":           _get(i_amount),
                "comment":          _get(i_comment),
                "owner":            _get(i_owner) or "self",
                "ptr_link":         ptr_link,
            })

    return transactions


def fetch_range(from_date: str, to_date: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    all_transactions: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # ── Step 1: accept the prohibition agreement ──────────────────────────────
        page.goto(_HOME_URL, wait_until="domcontentloaded")
        page.click("#agree_statement")
        page.wait_for_url("https://efdsearch.senate.gov/search/", timeout=10_000)
        print(f"Accepted agreement → {page.url}")

        # ── Step 2: submit search form ────────────────────────────────────────────
        page.fill('input[name="submitted_start_date"]', _to_mdy(from_date))
        page.fill('input[name="submitted_end_date"]',   _to_mdy(to_date))

        page.click('button[type="submit"]')
        page.wait_for_selector("#filedReports tbody tr", timeout=20_000)

        def _read_table_rows() -> list[list[str]]:
            return page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('#filedReports tbody tr')
                ).map(tr =>
                    Array.from(tr.querySelectorAll('td')).map(td => td.innerHTML)
                )
            """)

        def _get_total() -> int:
            info = page.inner_text(".dataTables_info") if page.query_selector(".dataTables_info") else ""
            m = re.search(r"of\s+([\d,]+)", info)
            return int(m.group(1).replace(",", "")) if m else 0

        # ── Step 3: collect all PTR filing links via DataTables pagination ────────
        total = _get_total()
        filings: list[dict] = []
        rows_seen = 0

        raw_rows = _read_table_rows()
        rows_seen += len(raw_rows)
        for row in raw_rows:
            f = _normalise_search_row(row)
            if f:
                filings.append(f)
        print(f"Search total: {total} | page 1 rows: {len(raw_rows)}, PTRs found so far: {len(filings)}")

        while rows_seen < total:
            next_btn = page.query_selector("a.paginate_button.next:not(.disabled)")
            if not next_btn:
                print("No more pages")
                break

            expected_start = rows_seen + 1
            next_btn.click()
            try:
                page.wait_for_function(
                    f"""() => {{
                        const info = document.querySelector('.dataTables_info')?.innerText || '';
                        const m = info.match(/Showing ([\\d,]+) to/);
                        return m && parseInt(m[1].replace(/,/g, '')) >= {expected_start};
                    }}""",
                    timeout=15_000,
                )
            except Exception:
                page.wait_for_load_state("networkidle", timeout=15_000)

            raw_rows = _read_table_rows()
            rows_seen += len(raw_rows)
            for row in raw_rows:
                f = _normalise_search_row(row)
                if f:
                    filings.append(f)
            print(f"  rows seen: {rows_seen}/{total}, PTRs so far: {len(filings)}")

        print(f"\nCollected {len(filings)} PTR filing links out of {rows_seen} total results")

        # ── Step 4: visit each PTR page and extract transactions ──────────────────
        for i, filing in enumerate(filings, 1):
            txns = _scrape_ptr_transactions(page, filing)
            all_transactions.extend(txns)
            status = f"{len(txns)} txn(s)" if txns else "0 txns (empty/error)"
            print(f"  [{i}/{len(filings)}] {filing['senator']} {filing['disclosure_date']} — {status}")

        browser.close()

    return all_transactions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to",   dest="to_date",   default=None)
    args = parser.parse_args()

    today     = date.today()
    from_date = args.from_date or (today - timedelta(days=14)).isoformat()
    to_date   = args.to_date   or today.isoformat()

    print(f"Fetching Senate PTR transactions {from_date} → {to_date}")
    new_txns = fetch_range(from_date, to_date)
    print(f"\nFetched {len(new_txns)} transactions total")

    existing: list[dict] = json.loads(_AGGREGATE_PATH.read_text()) if _AGGREGATE_PATH.exists() else []
    # Deduplicate on (ptr_link, ticker, transaction_date, type)
    existing_keys: set[tuple] = {
        (r.get("ptr_link",""), r.get("ticker",""), r.get("transaction_date",""), r.get("type",""))
        for r in existing
        if r.get("ptr_link")
    }

    added = [
        r for r in new_txns
        if (r.get("ptr_link",""), r.get("ticker",""), r.get("transaction_date",""), r.get("type",""))
        not in existing_keys
    ]
    print(f"New (not in aggregate): {len(added)}")

    if not added:
        print("Nothing new — aggregate unchanged.")
        return

    merged = existing + added

    def _sort_key(r: dict) -> str:
        raw = r.get("disclosure_date") or ""
        parts = raw.split("/")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[0]:0>2}/{parts[1]:0>2}"
        return raw

    merged.sort(key=_sort_key, reverse=True)
    _AGGREGATE_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    print(f"Wrote {len(merged)} rows to {_AGGREGATE_PATH}")


if __name__ == "__main__":
    main()
