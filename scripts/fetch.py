"""Fetch Senate STOCK Act PTR filings from efdsearch.senate.gov and merge into
aggregate/all_transactions.json in the senate_stock_watcher field format.

Usage:
    python scripts/fetch.py                          # last 14 days
    python scripts/fetch.py --from 2021-01-01 --to 2021-12-31   # backfill range

Deduplicates on ptr_link (the UUID in the URL is stable across re-fetches).

The Senate eFDS search (efdsearch.senate.gov) is a Django app — we need a
session cookie + CSRF token before each POST.  efts.senate.gov was decommissioned.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.cookiejar import CookieJar
from pathlib import Path

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}
_HOME_URL    = "https://efdsearch.senate.gov/search/home/"
_BASE_URL    = "https://efdsearch.senate.gov"
_PAGE_SIZE   = 100
_AGGREGATE_PATH = Path(__file__).parent.parent / "aggregate" / "all_transactions.json"


def _make_opener() -> urllib.request.OpenerDirector:
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _extract_csrf(body: str) -> str:
    for pat in [
        r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="csrfmiddlewaretoken"',
        r"csrfmiddlewaretoken.*?value=['\"]([^'\"]+)",
    ]:
        m = re.search(pat, body, re.DOTALL)
        if m:
            return m.group(1)
    return ""


def _find_ajax_url(body: str) -> str:
    """Find DataTables server-side AJAX URL embedded in inline scripts."""
    for pat in [
        r'"ajax"\s*:\s*["\']([^"\']+)["\']',
        r"ajax\s*:\s*['\"]([^'\"]+)['\"]",
        r"ajaxUrl\s*[=:]\s*['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            url = m.group(1)
            return (_BASE_URL + url) if url.startswith("/") else url
    return ""


def _get_session(opener: urllib.request.OpenerDirector) -> tuple[str, str]:
    """Accept the eFDS prohibition agreement and return (csrf, search_ajax_url).

    The flow:
      1. GET /search/home/ — renders an agreement checkbox form.
      2. POST /search/home/ with prohibition_agreement=1 + CSRF — accepts agreement,
         sets a session cookie, and redirects to the real search page.
      3. The real search page contains a DataTables init with the AJAX data URL.
    """
    # Step 1: load agreement page
    req = urllib.request.Request(_HOME_URL, headers=_HEADERS)
    with opener.open(req, timeout=20) as r:
        body1 = r.read().decode(errors="replace")

    csrf1 = _extract_csrf(body1)
    print(f"Agreement page CSRF: {csrf1[:20] if csrf1 else '(none)'}")

    # Step 2: submit agreement
    form_data = urllib.parse.urlencode({
        "prohibition_agreement": "1",
        "csrfmiddlewaretoken": csrf1,
    }).encode()
    req2 = urllib.request.Request(
        _HOME_URL, data=form_data, method="POST",
        headers={**_HEADERS,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": _HOME_URL},
    )
    with opener.open(req2, timeout=20) as r:
        body2 = r.read().decode(errors="replace")
        search_page_url = r.geturl()

    print(f"After agreement POST, landed on: {search_page_url}")

    # Extract fresh CSRF and DataTables AJAX URL from the search page
    csrf2 = _extract_csrf(body2)
    ajax_url = _find_ajax_url(body2)

    print(f"Search page CSRF: {csrf2[:20] if csrf2 else '(none)'}")
    print(f"DataTables AJAX URL: {ajax_url or '(not found — will dump scripts)'}")

    if not ajax_url:
        # Dump inline scripts so we can diagnose
        print("=== INLINE SCRIPTS ON SEARCH PAGE ===")
        for sm in re.finditer(r'<script(?![^>]*src)[^>]*>(.*?)</script>', body2, re.DOTALL | re.IGNORECASE):
            snippet = sm.group(1).strip()
            if snippet:
                print(snippet[:800])
                print("---")

    return csrf2, ajax_url or search_page_url


def _search_page(
    opener: urllib.request.OpenerDirector,
    csrf: str,
    search_url: str,
    from_date: str,
    to_date: str,
    start: int,
) -> tuple[list[dict], int]:
    """POST one page of PTR search results.  Returns (rows, total)."""
    # efdsearch uses MM/DD/YYYY in form fields
    def to_mdy(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{m}/{d}/{y}"

    form = urllib.parse.urlencode({
        "csrfmiddlewaretoken": csrf,
        "action": "search",
        "type[]": "6",          # 6 = Periodic Transaction Report
        "filer_type": "1",      # 1 = senator
        "submitted_start_date": to_mdy(from_date),
        "submitted_end_date":   to_mdy(to_date),
        "start": start,
        "length": _PAGE_SIZE,
        "draw": start // _PAGE_SIZE + 1,  # DataTables draw counter
    }).encode()

    req = urllib.request.Request(
        search_url, data=form, method="POST",
        headers={
            **_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": _HOME_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )
    with opener.open(req, timeout=30) as r:
        ct = r.headers.get("Content-Type", "")
        body = r.read()

    if "json" in ct:
        data = json.loads(body)
        rows = data.get("data", [])
        total = data.get("recordsTotal", len(rows))
        return rows, total

    # Fallback: HTML table parse
    text = body.decode(errors="replace")
    # The table has rows like: <td>name</td><td>ticker</td>...
    # Count entries from "Showing X to Y of Z entries"
    total_m = re.search(r"of\s+([\d,]+)\s+entries", text)
    total = int(total_m.group(1).replace(",", "")) if total_m else 0
    # Extract table rows
    row_pat = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    cell_pat = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
    tag_pat  = re.compile(r"<[^>]+>")
    rows = []
    for row_m in row_pat.finditer(text):
        cells = [tag_pat.sub("", c.group(1)).strip()
                 for c in cell_pat.finditer(row_m.group(1))]
        if len(cells) >= 4:
            rows.append(cells)
    return rows, total


def _normalise_json_row(row: dict | list) -> dict | None:
    """Normalise one row from efdsearch JSON or HTML-table parse."""
    if isinstance(row, list):
        # HTML table columns: [first, last, office, type, asset, ticker, tx_type, amount, tx_date, filed_date, ptr_link_html]
        if len(row) < 10:
            return None
        tag = re.compile(r"<[^>]+>")
        href = re.search(r'href="([^"]+)"', row[-1]) if len(row) > 10 else None
        ptr_link = ("https://efdsearch.senate.gov" + href.group(1)) if href else ""
        return {
            "transaction_date": row[8].strip(),
            "owner": "self",
            "ticker": tag.sub("", row[5]).strip().upper() or "--",
            "asset_description": tag.sub("", row[4]).strip(),
            "asset_type": "Stock",
            "type": tag.sub("", row[6]).strip(),
            "amount": tag.sub("", row[7]).strip(),
            "comment": "",
            "senator": f"{tag.sub('', row[0]).strip()} {tag.sub('', row[1]).strip()}".strip(),
            "disclosure_date": row[9].strip(),
            "ptr_link": ptr_link,
        }

    # JSON row (keys vary by efdsearch version)
    first = (row.get("first_name") or "").strip()
    last  = (row.get("last_name")  or "").strip()
    senator = f"{first} {last}".strip() or row.get("senator_name", "").strip()
    if not senator:
        return None

    tx_date = (row.get("transaction_date") or row.get("date") or "").strip()
    if not tx_date:
        return None

    ptr_link = row.get("ptr_link") or row.get("link") or ""
    if not ptr_link and row.get("document_id"):
        ptr_link = f"https://efdsearch.senate.gov/search/view/ptr/{row['document_id']}/"

    return {
        "transaction_date": tx_date,
        "owner": (row.get("owner") or "self").strip(),
        "ticker": (row.get("ticker") or "--").strip().upper(),
        "asset_description": (row.get("asset_description") or "").strip(),
        "asset_type": row.get("asset_type", ""),
        "type": (row.get("type") or row.get("transaction_type") or "").strip(),
        "amount": (row.get("amount") or "").strip(),
        "comment": row.get("comment", ""),
        "senator": senator,
        "disclosure_date": (row.get("filing_date") or row.get("disclosure_date") or tx_date).strip(),
        "ptr_link": ptr_link,
    }


def fetch_range(from_date: str, to_date: str) -> list[dict]:
    """Fetch all PTR filings where filing_date is in [from_date, to_date]."""
    opener = _make_opener()

    csrf, search_url = _get_session(opener)
    print(f"Using search URL: {search_url}")

    all_results: list[dict] = []
    start = 0
    total = None

    while True:
        try:
            raw_rows, page_total = _search_page(opener, csrf, search_url, from_date, to_date, start)
        except Exception as exc:
            print(f"ERROR fetching page start={start}: {exc}", file=sys.stderr)
            sys.exit(1)

        if total is None:
            total = page_total
            print(f"Total filings in range: {total}")

        new = [_normalise_json_row(r) for r in raw_rows]
        new = [r for r in new if r is not None]
        all_results.extend(new)
        print(f"  fetched {len(all_results)}/{total}")

        if len(raw_rows) < _PAGE_SIZE or len(all_results) >= total:
            break

        start += _PAGE_SIZE
        time.sleep(0.5)

    return all_results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default=None,
                        help="Start date YYYY-MM-DD (default: 14 days ago)")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    today = date.today()
    from_date = args.from_date or (today - timedelta(days=14)).isoformat()
    to_date   = args.to_date   or today.isoformat()

    print(f"Fetching Senate PTR filings {from_date} → {to_date}")

    new_rows = fetch_range(from_date, to_date)
    print(f"Normalised {len(new_rows)} rows")

    existing: list[dict] = json.loads(_AGGREGATE_PATH.read_text())
    existing_links: set[str] = {r.get("ptr_link", "") for r in existing}

    added = [r for r in new_rows if r["ptr_link"] not in existing_links]
    print(f"New (not already in aggregate): {len(added)}")

    if not added:
        print("Nothing new — aggregate unchanged.")
        return

    merged = existing + added

    def _sort_key(r: dict) -> tuple:
        raw = r.get("disclosure_date") or r.get("transaction_date") or ""
        parts = raw.split("/")
        if len(parts) == 3:
            return (parts[2], parts[0], parts[1])
        return (raw, "", "")

    merged.sort(key=_sort_key, reverse=True)
    _AGGREGATE_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    print(f"Wrote {len(merged)} rows to {_AGGREGATE_PATH}")


if __name__ == "__main__":
    main()
