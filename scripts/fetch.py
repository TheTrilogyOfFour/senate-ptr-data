"""Probe which Senate eFDS endpoints are reachable and what they return."""
import urllib.request, urllib.error, urllib.parse, json, re

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
}

def get(url, extra_headers=None):
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(1000)
            ct = r.headers.get("Content-Type", "?")
            print(f"OK {r.status} [{ct[:40]}]: {url}")
            print(f"  body: {body[:200]}")
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read(200)
        print(f"HTTP {e.code}: {url}")
        print(f"  {body[:100]}")
        return e.code, body, {}
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {url} -- {e}")
        return 0, b"", {}

# 1. Is efdsearch.senate.gov reachable?
status, body, hdrs = get("https://efdsearch.senate.gov/search/home/")

if status == 200:
    # 2. Extract CSRF token
    csrf = re.search(rb"csrfmiddlewaretoken.*?value=['"]([^'"]+)", body)
    csrf_token = csrf.group(1).decode() if csrf else ""
    cookies = hdrs.get("Set-Cookie", "")
    csrf_cookie = re.search(r"csrftoken=([^;]+)", cookies)
    csrf_cookie_val = csrf_cookie.group(1) if csrf_cookie else ""
    print(f"CSRF token from form: {csrf_token[:20]}...")
    print(f"CSRF cookie: {csrf_cookie_val[:20]}...")

    # 3. Try JSON search via POST
    form_data = urllib.parse.urlencode({
        "csrfmiddlewaretoken": csrf_token,
        "action": "search",
        "type[]": "6",
        "filer_type": "1",
        "submitted_start_date": "01/01/2021",
        "submitted_end_date": "03/31/2021",
    }).encode()
    post_req = urllib.request.Request(
        "https://efdsearch.senate.gov/search/results/",
        data=form_data, method="POST",
        headers={**HEADERS,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://efdsearch.senate.gov/search/home/",
                 "Cookie": f"csrftoken={csrf_cookie_val}",
                 "Accept": "application/json, text/html, */*",
                 "X-CSRFToken": csrf_token,
                 "X-Requested-With": "XMLHttpRequest"},
    )
    try:
        with urllib.request.urlopen(post_req, timeout=15) as r:
            body2 = r.read(2000)
            ct = r.headers.get("Content-Type", "?")
            print(f"POST results: {r.status} [{ct}]")
            print(f"  body: {body2[:500]}")
    except urllib.error.HTTPError as e:
        print(f"POST results HTTP {e.code}: {e.read(200)}")
    except Exception as e:
        print(f"POST results FAIL: {e}")
else:
    print("efdsearch.senate.gov not reachable from this runner")

# 4. Also try old efts domain
get("https://efts.senate.gov/LATEST/search-results?q=%7B%22source%22%3A%22ptr%22%7D&limit=1")
