"""Fetch Senate STOCK Act PTR filings from efdsearch.senate.gov.

Uses Playwright (headless Chromium) to bypass Akamai bot-protection on
/search/report/data/, then makes programmatic fetch() calls from within
the browser to paginate through all results.

Usage:
    python scripts/fetch.py                          # last 14 days
    python scripts/fetch.py --from 2021-01-01 --to 2021-12-31

Requires: pip install playwright && playwright install chromium
"""
import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_AGGREGATE_PATH = Path(__file__).parent.parent / "aggregate" / "all_transactions.json"
_HOME_URL = "https://efdsearch.senate.gov/search/home/"
_PAGE_SIZE = 100


def _to_mdy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def _fetch_page_js(from_date: str, to_date: str, start: int) -> str:
    """Return a JS snippet that POSTs /search/report/data/ and returns JSON string."""
    params = {
        "report_types": '["6"]',      # 6 = Periodic Transaction Report
        "filer_types": '["1"]',       # 1 = senator
        "submitted_start_date": _to_mdy(from_date),
        "submitted_end_date": _to_mdy(to_date),
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "start": str(start),
        "length": str(_PAGE_SIZE),
        "draw": str(start // _PAGE_SIZE + 1),
    }
    # Build URLSearchParams entries as a JS literal
    entries = json.dumps(list(params.items()))
    return f"""
(async function() {{
    const params = new URLSearchParams({entries});
    const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
    const resp = await fetch('/search/report/data/', {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': csrf,
            'X-Requested-With': 'XMLHttpRequest'
        }},
        body: params.toString()
    }});
    const text = await resp.text();
    return JSON.stringify({{status: resp.status, body: text}});
}})()
"""


def _normalise(row: list) -> dict | None:
    """Normalise one DataTables row.

    Columns on /search/report/data/ (5 cols):
      [0] First name (HTML)
      [1] Last name (HTML)
      [2] Office (HTML)
      [3] Report type + link HTML  e.g. <a href="/search/view/ptr/UUID/">Periodic…</a>
      [4] Date filed MM/DD/YYYY (HTML)
    """
    if not isinstance(row, list) or len(row) < 5:
        return None

    strip_tags = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()

    first = strip_tags(row[0])
    last  = strip_tags(row[1])
    senator = f"{first} {last}".strip()
    if not senator:
        return None

    link_m = re.search(r'href=["\']([^"\']+)["\']', row[3])
    ptr_link = ""
    if link_m:
        path = link_m.group(1)
        ptr_link = ("https://efdsearch.senate.gov" + path) if path.startswith("/") else path

    filed_date = strip_tags(row[4])

    return {
        "transaction_date": "",
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # Accept the prohibition agreement
        print(f"Loading agreement page: {_HOME_URL}")
        page.goto(_HOME_URL, wait_until="networkidle")
        page.check("#agree_statement")
        page.wait_for_url("**/search/**", timeout=10_000)
        print(f"Landed on: {page.url}")

        # Confirm we have a csrftoken cookie
        cookies = context.cookies()
        csrf_cookie = next((c["value"] for c in cookies if c["name"] == "csrftoken"), "")
        print(f"csrftoken cookie present: {bool(csrf_cookie)}")

        # Paginate through results using in-browser fetch() calls
        start = 0
        total = None

        while True:
            js = _fetch_page_js(from_date, to_date, start)
            result_str = page.evaluate(js)
            result = json.loads(result_str)

            status = result.get("status")
            body_text = result.get("body", "")
            print(f"  /search/report/data/ start={start} → HTTP {status}")

            if status != 200:
                print(f"  Unexpected status {status}. Body (first 500): {body_text[:500]}", file=sys.stderr)
                sys.exit(1)

            try:
                data = json.loads(body_text)
            except json.JSONDecodeError:
                print(f"  Non-JSON response: {body_text[:500]}", file=sys.stderr)
                sys.exit(1)

            if total is None:
                total = int(data.get("recordsFiltered", data.get("recordsTotal", 0)))
                print(f"Total PTR filings in range: {total}")

            raw_rows = data.get("data", [])
            new = [_normalise(r) for r in raw_rows]
            new = [r for r in new if r is not None]
            all_results.extend(new)
            print(f"  fetched {len(all_results)}/{total}")

            if not raw_rows or len(all_results) >= total:
                break

            start += _PAGE_SIZE
            time.sleep(0.3)

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
    print(f"Normalised {len(new_rows)} filings")

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
