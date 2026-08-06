
import urllib.request, urllib.error, json, socket

urls = [
    ("efts old", "https://efts.senate.gov/LATEST/search-results?q=%7B%22source%22%3A%22ptr%22%7D&limit=1"),
    ("efdsearch home", "https://efdsearch.senate.gov/search/home/"),
    ("efdsearch results GET", "https://efdsearch.senate.gov/search/results/?limit=1"),
    ("efdsearch API", "https://efdsearch.senate.gov/search/results/?&type[]=6&filer_type=1&submitted_start_date=01%2F01%2F2021&submitted_end_date=12%2F31%2F2021"),
]

for label, url in urls:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/html, */*",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(500)
            print(f"OK {r.status}: {label}")
            print(f"  Content-Type: {r.headers.get('Content-Type','?')}")
            print(f"  Body: {body[:150]}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {label}")
        print(f"  {e.read(100)}")
    except Exception as e:
        print(f"FAIL ({type(e).__name__}): {label} -- {e}")
