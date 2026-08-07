"""Fetch Senate STOCK Act PTR filings from efdsearch.senate.gov.

Uses Playwright (headless Chromium) to:
  1. Accept the prohibition agreement.
  2. Submit the search form with date range + PTR report type.
  3. Intercept DataTables AJAX responses from /search/report/data/ for pagination.
  4. Merge new filings into aggregate/all_transactions.json.

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
_DATA_PATH = "https://efdsearch.senate.gov/search/report/data/"
_PAGE_SIZE = 100
_AGGREGATE_PATH = Path(__file__).parent.parent / "aggregate" / "all_transactions.json"


def _to_mdy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _normalise(row: list) -> dict | None:
    """Normalise one DataTables row from /search/report/data/.

    Columns (5):
      [0] First name (HTML)
      [1] Last name (HTML)
      [2] Office (HTML)
      [3] Report type + link HTML — e.g. <a href="/search/view/ptr/UUID/">Periodic Transaction Report</a>
      [4] Date filed MM/DD/YYYY (HTML)
    """
    if not isinstance(row, list) or len(row) < 5:
        return None

    first  = _strip_tags(row[0])
    last   = _strip_tags(row[1])
    senator = f"{first} {last}".strip()
    if not senator:
        return None

    link_m = re.search(r'href=["\']([^"\']+)["\']', row[3])
    ptr_link = ""
    if link_m:
        path = link_m.group(1)
        ptr_link = ("https://efdsearch.senate.gov" + path) if path.startswith("/") else path

    filed_date = _strip_tags(row[4])

    return {
        "transaction_date": "",    # not in search results; only in individual PTR PDF
        "owner": "self",
        "ticker": "--",
        "asset_description": "",
        "asset_type": "",
        "type": "",
        "amount": "",
        "comment": "",
        "senator": senator,
        "disclosure_date": filed_date,
        "ptr_link": ptr_link,
    }


def fetch_range(from_date: str, to_date: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    all_results: list[dict] = []
    total: int | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # ── Step 1: accept the prohibition agreement ─────────────────────────────
        page.goto(_HOME_URL, wait_until="domcontentloaded")
        page.click("#agree_statement")
        page.wait_for_url("https://efdsearch.senate.gov/search/", timeout=10_000)
        print(f"Accepted agreement → {page.url}")

        # ── Step 2: submit search form (fills in dates + triggers DataTables) ────
        page.fill('input[name="submitted_start_date"]', _to_mdy(from_date))
        page.fill('input[name="submitted_end_date"]',   _to_mdy(to_date))

        # Set page length to 100 so fewer AJAX calls are needed
        # DataTables renders a <select> for length
        page.select_option("select[name='filedReports_length']", "100")

        # Intercept the first DataTables response to get total count
        captured: list[dict] = []
        def on_response(resp):
            if _DATA_PATH in resp.url and resp.status == 200:
                try:
                    captured.append(resp.json())
                except Exception:
                    pass
        page.on("response", on_response)

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=20_000)

        if not captured:
            print("ERROR: no successful /search/report/data/ response after form submit", file=sys.stderr)
            browser.close()
            sys.exit(1)

        first_page = captured[-1]  # last captured = most recent AJAX call
        total = int(first_page.get("recordsFiltered", first_page.get("recordsTotal", 0)))
        rows = first_page.get("data", [])
        all_results.extend(r for r in (_normalise(row) for row in rows) if r)
        print(f"Total PTR filings: {total} | fetched {len(all_results)}/{total}")

        # ── Step 3: paginate by clicking "Next" until all rows collected ─────────
        while len(all_results) < total:
            captured.clear()
            # Click DataTables "Next" button
            next_btn = page.query_selector("a.paginate_button.next:not(.disabled)")
            if not next_btn:
                print("No more pages (Next button disabled or missing)")
                break
            next_btn.click()
            page.wait_for_load_state("networkidle", timeout=15_000)

            if not captured:
                print("WARNING: Next page click yielded no AJAX response")
                break

            page_data = captured[-1]
            rows = page_data.get("data", [])
            all_results.extend(r for r in (_normalise(row) for row in rows) if r)
            print(f"  fetched {len(all_results)}/{total}")

        browser.close()

    return all_results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to",   dest="to_date",   default=None)
    args = parser.parse_args()

    today     = date.today()
    from_date = args.from_date or (today - timedelta(days=14)).isoformat()
    to_date   = args.to_date   or today.isoformat()

    print(f"Fetching Senate PTR filings {from_date} → {to_date}")
    new_rows = fetch_range(from_date, to_date)
    print(f"Fetched {len(new_rows)} filings")

    existing: list[dict] = json.loads(_AGGREGATE_PATH.read_text())
    existing_links: set[str] = {r.get("ptr_link", "") for r in existing}

    added = [r for r in new_rows if r.get("ptr_link") and r["ptr_link"] not in existing_links]
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
