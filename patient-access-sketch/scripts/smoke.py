"""Browserless end-to-end smoke against the running sketch and the hosted SMART sandbox.

Runs inside the web container (it needs `manage.py mint_deeplink` and http://localhost:8080):
    docker compose exec web python scripts/smoke.py --brand "Express (scripted" --until done

Uses the sandbox's skip-login/skip-auth launch options, so the authorize endpoint redirects
straight back with a code. Prints the job JSON at the end; exits non-zero on failure.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import uuid

import requests

BASE = "http://localhost:8080"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="Express (scripted", help="substring of the directory location name")
    ap.add_argument("--until", default="queued", help="stop when the job reaches this status (or any terminal one)")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--ref", default=f"smoke-{uuid.uuid4().hex[:8]}")
    args = ap.parse_args()

    link = subprocess.check_output(["python", "manage.py", "mint_deeplink", "--ref", args.ref], text=True).strip()
    s = requests.Session()
    r = s.get(link.replace("http://localhost:8080", BASE), timeout=30)
    r.raise_for_status()
    brands = s.get(f"{BASE}/api/brands", params={"q": "sandbox"}, timeout=30).json()["results"]
    match = [b for b in brands if args.brand in b["name"]]
    if len(match) != 1:
        print(f"brand {args.brand!r} matched {[b['name'] for b in match]}", file=sys.stderr)
        return 2
    location = match[0]
    page = s.get(f"{BASE}/pick", params={"q": "sandbox"}, timeout=30).text
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
    t0 = time.time()
    r = s.post(
        f"{BASE}/launch",
        data={"location_id": location["id"], "csrfmiddlewaretoken": csrf},
        allow_redirects=False,
        timeout=60,
    )
    assert r.status_code == 302, (r.status_code, r.text[:500])
    url = r.headers["Location"]
    hops = 0
    # Follow the EHR's redirects until it sends us back to our own callback.
    while not url.startswith(f"{BASE}/callback"):
        hops += 1
        if hops > 10:
            print(f"too many hops; last {url}", file=sys.stderr)
            return 3
        r = s.get(url, allow_redirects=False, timeout=60)
        if r.status_code not in (301, 302, 303, 307):
            print(f"EHR stopped redirecting at {url[:200]}: {r.status_code} {r.text[:300]}", file=sys.stderr)
            return 3
        url = requests.compat.urljoin(url, r.headers["Location"])
    r = s.get(url, allow_redirects=False, timeout=60)
    if r.status_code != 302:
        err = re.search(r'id="error">([^<]*)<', r.text)
        print(f"callback failed: {r.status_code} {err.group(1) if err else r.text[:300]}", file=sys.stderr)
        return 4
    job_url = r.headers["Location"]
    job_id = job_url.rstrip("/").split("/")[-1]
    print(f"{location['name']}: authorized in {time.time() - t0:.1f}s, job {job_id}", file=sys.stderr)
    deadline = time.time() + args.timeout
    while True:
        job = s.get(f"{BASE}/api/jobs/{job_id}", timeout=30).json()
        if job["status"] == args.until or job["terminal"] or time.time() > deadline:
            break
        time.sleep(2)
    job["elapsed_seconds"] = round(time.time() - t0, 1)
    print(json.dumps(job, indent=2))
    ok = job["status"] == args.until or (args.until == "done" and job["status"] == "done")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
