#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jetpack-leak.py
Detect WP.com / Jetpack sitemap leak fingerprints with high throughput.

Reports ONLY when:
  CASE 1 (WP.com):  <!-- generator="wordpress.com" -->  AND  <image:loc> present in the same document
  CASE 2 (Jetpack): <!--generator='jetpack...--> AND (URL contains image-sitemap-*.xml OR the XML lists a <loc> to image-sitemap-*.xml)

Output (one line per hit):
  <url>\t<vendor>\tLEAK

Usage:
  python jetpack-leak.py -i sitemaps.txt -t 32 -T 6 --max-kb 256
"""

import argparse, threading, queue, requests, sys, re, time

UA = "Mozilla/5.0 (compatible; JetpressLeakDetector/1.2; +https://labs.itresit.es)"

# Flexible regexes
RE_WPCOM_GEN   = re.compile(r'generator\s*=\s*["\']wordpress\.com["\']', re.I)
RE_JETPACK_GEN = re.compile(r'generator\s*=\s*["\']?jetpack', re.I)
RE_JETPACK_SIG = re.compile(r'jetpack[_\-\s]?sitemap', re.I)  # extra hint like Jetpack_Sitemap_Buffer_...
RE_IMAGE_LOC   = re.compile(r'<image:loc', re.I)
RE_IMG_SM_URL  = re.compile(r'image-sitemap-\d+\.xml', re.I)

def fetch_snippet(url: str, timeout: int, max_kb: int):
    """
    GET with stream; return (status, snippet_text_lower) reading at most max_kb.
    Falls back to http:// if https:// fails.
    """
    headers = {"User-Agent": UA, "Accept": "application/xml,text/xml"}
    for attempt in (url, url.replace("https://", "http://", 1) if url.startswith("https://") else None):
        if not attempt: 
            break
        try:
            with requests.get(attempt, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as r:
                status = r.status_code
                if status != 200:
                    return status, ""
                # Read up to max_kb
                limit = max_kb * 1024
                chunks = []
                read = 0
                for chunk in r.iter_content(chunk_size=16384):
                    if not chunk: 
                        break
                    chunks.append(chunk)
                    read += len(chunk)
                    if read >= limit:
                        break
                # Decode best-effort
                enc = r.encoding or "utf-8"
                try:
                    text = b"".join(chunks).decode(enc, errors="ignore")
                except Exception:
                    text = b"".join(chunks).decode("utf-8", errors="ignore")
                return status, text.lower()
        except Exception:
            continue
    return None, ""

def evaluate(url: str, text_lc: str):
    """
    Return vendor string ('wpcom'|'jetpack') if leak fingerprint matches, else None.
    CASE 1: wp.com generator + <image:loc> present in same doc
    CASE 2: jetpack generator (or Jetpack signature) + image-sitemap-* in URL OR in XML (<loc>...)
    """
    if not text_lc:
        return None

    # CASE 1 — WordPress.com
    if RE_WPCOM_GEN.search(text_lc) and RE_IMAGE_LOC.search(text_lc):
        return "wpcom"

    # CASE 2 — Jetpack
    is_jetpack = RE_JETPACK_GEN.search(text_lc) or RE_JETPACK_SIG.search(text_lc)
    if is_jetpack:
        if RE_IMG_SM_URL.search(url) or RE_IMG_SM_URL.search(text_lc):
            return "jetpack"

    return None

def worker(q: queue.Queue, timeout: int, max_kb: int, counter, total, lock, progress_every: int):
    while True:
        try:
            url = q.get(timeout=1)
        except queue.Empty:
            return

        vendor = None
        status, snippet = fetch_snippet(url, timeout, max_kb)
        if status == 200 and snippet:
            vendor = evaluate(url, snippet)

        with lock:
            counter[0] += 1
            i = counter[0]
            if vendor:
                print(f"{url}\t{vendor}\tLEAK")
                sys.stdout.flush()
            elif progress_every and (i % progress_every == 0):
                print(f"[{i}/{total}] ...", file=sys.stderr)
                sys.stderr.flush()

        q.task_done()

def main():
    ap = argparse.ArgumentParser(description="WP.com / Jetpack sitemap leak detector (streaming).")
    ap.add_argument("-i", "--input", required=True, help="File with sitemap URLs (one per line)")
    ap.add_argument("-t", "--threads", type=int, default=32, help="Worker threads (default 32)")
    ap.add_argument("-T", "--timeout", type=int, default=6, help="HTTP timeout seconds (default 6)")
    ap.add_argument("--max-kb", type=int, default=256, help="Max KB to read per URL (default 256KB)")
    ap.add_argument("--progress-every", type=int, default=50, help="Stderr progress cadence (default 50)")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    total = len(urls)
    q = queue.Queue(maxsize=args.threads * 2)
    counter = [0]
    lock = threading.Lock()

    print(f"[INFO] Loaded {total} URLs", file=sys.stderr)

    for _ in range(args.threads):
        threading.Thread(target=worker, args=(q, args.timeout, args.max_kb, counter, total, lock, args.progress_every), daemon=True).start()

    start = time.time()
    for u in urls:
        q.put(u)
    q.join()
    print(f"[INFO] Finished {total} URLs in {time.time()-start:.1f}s", file=sys.stderr)

if __name__ == "__main__":
    main()
