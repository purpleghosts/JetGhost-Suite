#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jetpack-detect.py
Concurrent Jetpack/WP.com sitemap detector with progress & fallbacks
"""

import argparse, threading, queue, sys, requests, time
from urllib.parse import urlparse

UA = "Mozilla/5.0 (compatible; JetpressLite/1.2; +https://labs.itresit.es)"

def detect_vendor(text: str) -> str:
    t = text.lower()
    if "generator=\"wordpress.com\"" in t or "wordpress.com/sitemap" in t:
        return "wpcom"
    if "jetpack" in t:
        return "jetpack"
    if "<urlset" in t or "<sitemapindex" in t:
        return "other"
    return "none"

def fetch(url: str, timeout: int):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        return r
    except Exception as e:
        # Retry in http if https fails
        if url.startswith("https://"):
            try:
                r = requests.get(url.replace("https://", "http://", 1), headers={"User-Agent": UA}, timeout=timeout)
                return r
            except Exception:
                return None
        return None

def worker(q: queue.Queue, total: int, timeout: int, counter, lock):
    while True:
        try:
            url = q.get(timeout=1)
        except queue.Empty:
            return

        result = {"url": url, "vendor": "error", "status": "-", "msg": ""}
        try:
            r = fetch(url, timeout)
            if not r:
                result["msg"] = "unreachable"
            elif r.status_code != 200:
                result["status"] = r.status_code
                result["vendor"] = "none"
                result["msg"] = "http_error"
            else:
                t = r.text[:4000]  # limit read
                vendor = detect_vendor(t)
                result["vendor"] = vendor
                result["status"] = 200
                result["msg"] = "ok" if vendor in ("wpcom", "jetpack", "other") else "no_xml"
        except Exception as e:
            result["msg"] = str(e).split()[0]

        with lock:
            counter[0] += 1
            i = counter[0]
            print(f"[{i}/{total}] {result['url']}\t{result['vendor']}\t{result['status']}\t{result['msg']}")
            sys.stdout.flush()

        q.task_done()

def main():
    ap = argparse.ArgumentParser(description="Parallel Jetpack/WP.com sitemap detector")
    ap.add_argument("-i", "--input", required=True, help="File with sitemap URLs")
    ap.add_argument("-t", "--threads", type=int, default=20, help="Number of concurrent workers")
    ap.add_argument("-T", "--timeout", type=int, default=6, help="Request timeout (default 6s)")
    args = ap.parse_args()

    q = queue.Queue(maxsize=args.threads * 2)
    counter = [0]
    lock = threading.Lock()

    with open(args.input, "r", encoding="utf-8") as f:
        targets = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    total = len(targets)
    print(f"[INFO] Loaded {total} targets", file=sys.stderr)

    for _ in range(args.threads):
        threading.Thread(target=worker, args=(q, total, args.timeout, counter, lock), daemon=True).start()

    start = time.time()
    for t in targets:
        q.put(t)
    q.join()
    print(f"[INFO] Finished {total} URLs in {time.time()-start:.1f}s", file=sys.stderr)

if __name__ == "__main__":
    main()
