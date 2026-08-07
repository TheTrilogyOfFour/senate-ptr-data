"""Fetch Senate STOCK Act PTR filings from efdsearch.senate.gov.

Uses Playwright (headless Chromium) to:
  1. Accept the prohibition agreement.
  2. Submit the search form with date range + PTR report type.
  3. Read rows directly from the DataTables DOM (#filedReports tbody tr) and
     paginate by clicking Next until all rows are collected.
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

        # ── Step 3: submit form, wait for table, read rows from DOM ─────────────
        page.click('button[type="submit"]')
        # Wait for DataTables to populate the table (processing indicator disappears)
        page.wait_for_selector("#filedReports tbody tr", timeout=20_000)

        def _read_table_rows() -> list[list[str]]:
            """Extract current DataTables page rows as [[cell_html, ...], ...]."""
            return page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('#filedReports tbody tr')
                ).map(tr =>
                    Array.from(tr.querySelectorAll('td')).map(td => td.innerHTML)
                )
            """)

        def _get_total() -> int:
            """Extract total row count from DataTables info text."""
            info = page.inner_text(".dataTables_info") if page.query_selector(".dataTables_info") else ""
            m = re.search(r"of\s+([\d,]+)", info)
            return int(m.group(1).replace(",", "")) if m else 0

        total = _get_total()
        rows = _read_table_rows()
        all_results.extend(r for r in (_normalise(row) for row in rows) if r)
        print(f"Total PTR filings: {total} | fetched {len(all_results)}/{total}")

        # ── Step 4: click Next until all rows collected ───────────────────────────
        while len(all_results) < total:
            next_btn = page.query_selector("a.paginate_button.next:not(.disabled)")
            if not next_btn:
                print("No more pages (Next button disabled or missing)")
                break

            expected_start = len(all_results) + 1
            next_btn.click()
            # Wait for DataTables info text to reflect the new page range:
            # e.g. "Showing 26 to 50 of 383 entries" — the start number must advance.
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

            rows = _read_table_rows()
            if not rows:
                print("WARNING: no rows on page after Next click")
                break
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
