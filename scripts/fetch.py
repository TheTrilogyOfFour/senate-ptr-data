"""Fetch Senate STOCK Act PTR filings from efdsearch.senate.gov and merge into
aggregate/all_transactions.json in the senate_stock_watcher field format.

Usage:
    python scripts/fetch.py                          # last 14 days
    python scripts/fetch.py --from 2021-01-01 --to 2021-12-31   # backfill range

Deduplicates on ptr_link (the UUID in the filing URL is stable across re-fetches).

Flow:
  1. GET /search/home/ → agreement page; extract CSRF token.
  2. POST /search/home/ with prohibition_agreement=1 → session cookie set; lands on /search/.
  3. POST /search/report/data/ (DataTables AJAX endpoint) with report_types=["6"] + date range.
     X-CSRFToken header from the csrftoken session cookie.
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.cookiejar import CookieJar
from pathlib import Path

_HOME_URL    = "https://efdsearch.senate.gov/search/home/"
_SEARCH_URL  = "https://efdsearch.senate.gov/search/"
_DATA_URL    = "https://efdsearch.senate.gov/search/report/data/"
_PAGE_SIZE   = 100
_AGGREGATE_PATH = Path(__file__).parent.parent / "aggregate" / "all_transactions.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _make_opener() -> tuple[urllib.request.OpenerDirector, CookieJar]:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener, jar


def _get_csrf_from_html(body: str) -> str:
    for pat in [
        r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="csrfmiddlewaretoken"',
        r"csrfmiddlewaretoken.*?value=['\"]([^'\"]+)",
    ]:
        m = re.search(pat, body, re.DOTALL)
        if m:
            return m.group(1)
    return ""


def _get_csrf_cookie(jar: CookieJar) -> str:
    for cookie in jar:
        if cookie.name == "csrftoken":
            return cookie.value
    return ""


def _accept_agreement(opener: urllib.request.OpenerDirector, jar: CookieJar) -> str:
    """Submit the prohibition agreement and return the csrftoken cookie value."""
    # Step 1: load agreement page to get CSRF token
    req = urllib.request.Request(
        _HOME_URL,
        headers={**_HEADERS, "Accept": "text/html,*/*;q=0.9"},
    )
    with opener.open(req, timeout=20) as r:
        body = r.read().decode(errors="replace")

    csrf_form = _get_csrf_from_html(body)
    print(f"Agreement page CSRF (form): {csrf_form[:20] if csrf_form else '(none)'}")

    # Step 2: submit the agreement checkbox
    form_data = urllib.parse.urlencode({
        "prohibition_agreement": "1",
        "csrfmiddlewaretoken": csrf_form,
    }).encode()
    req2 = urllib.request.Request(
        _HOME_URL, data=form_data, method="POST",
        headers={
            **_HEADERS,
            "Accept": "text/html,*/*;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": _HOME_URL,
        },
    )
    with opener.open(req2, timeout=20) as r:
        _ = r.read()
        landed = r.geturl()

    csrf_cookie = _get_csrf_cookie(jar)
    print(f"Landed on: {landed} | csrftoken cookie: {csrf_cookie[:20] if csrf_cookie else '(none)'}")
    return csrf_cookie


def _search_page(
    opener: urllib.request.OpenerDirector,
    csrf_cookie: str,
    from_date: str,
    to_date: str,
    start: int,
) -> tuple[list[dict], int]:
    """POST one page to /search/report/data/.  Returns (raw_rows, total_count)."""
    def to_mdy(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{m}/{d}/{y}"

    form = urllib.parse.urlencode({
        "report_types": '["6"]',    # 6 = Periodic Transaction Report
        "filer_types": '["1"]',     # 1 = senator
        "submitted_start_date": to_mdy(from_date),
        "submitted_end_date":   to_mdy(to_date),
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "start": start,
        "length": _PAGE_SIZE,
        "draw": start // _PAGE_SIZE + 1,
    }).encode()

    req = urllib.request.Request(
        _DATA_URL, data=form, method="POST",
        headers={
            **_HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": _SEARCH_URL,
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrf_cookie,
        },
    )
    with opener.open(req, timeout=30) as r:
        ct = r.headers.get("Content-Type", "")
        body = r.read()

    print(f"  /search/report/data/ response: {r.status} | Content-Type: {ct[:60]}")

    if "json" in ct:
        data = json.loads(body)
        rows = data.get("data", [])
        total = int(data.get("recordsFiltered", data.get("recordsTotal", len(rows))))
        return rows, total

    # Unexpected non-JSON — print for debugging
    text = body.decode(errors="replace")
    print(f"  Non-JSON response (first 500): {text[:500]}", file=sys.stderr)
    return [], 0


def _normalise(row: list) -> dict | None:
    """Normalise one DataTables row.

    The /search/report/data/ response 'data' field is a list of lists.
    Columns (from DataTables config — 5 columns):
      [0] First name
      [1] Last name
      [2] Office (role, e.g. "Senator (MD)")
      [3] Report type + link HTML  (e.g. '<a href="/search/view/ptr/UUID/">Periodic Transaction Report</a>')
      [4] Date filed (MM/DD/YYYY)
    """
    if not isinstance(row, list) or len(row) < 5:
        return None

    first = re.sub(r"<[^>]+>", "", row[0]).strip()
    last  = re.sub(r"<[^>]+>", "", row[1]).strip()
    senator = f"{first} {last}".strip()
    if not senator:
        return None

    # Extract ptr_link and report label from column 3
    link_m = re.search(r'href=["\']([^"\']+)["\']', row[3])
    ptr_link = ""
    if link_m:
        path = link_m.group(1)
        ptr_link = ("https://efdsearch.senate.gov" + path) if path.startswith("/") else path

    # Date filed (disclosure_date) is column 4
    filed_date = re.sub(r"<[^>]+>", "", row[4]).strip()

    return {
        "transaction_date": "",          # not available at this level; filled from PDF
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
    opener, jar = _make_opener()
    csrf_cookie = _accept_agreement(opener, jar)

    if not csrf_cookie:
        print("ERROR: no csrftoken cookie after agreement POST", file=sys.stderr)
        sys.exit(1)

    all_results: list[dict] = []
    start = 0
    total = None

    while True:
        raw_rows, page_total = _search_page(opener, csrf_cookie, from_date, to_date, start)

        if total is None:
            total = page_total
            print(f"Total PTR filings in range: {total}")

        new = [_normalise(r) for r in raw_rows]
        new = [r for r in new if r is not None]
        all_results.extend(new)
        print(f"  fetched {len(all_results)}/{total}")

        if not raw_rows or len(all_results) >= total:
            break

        start += _PAGE_SIZE
        time.sleep(0.3)

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
