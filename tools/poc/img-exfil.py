#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import re
import sys
import time
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; JetGhost-ImagePoC/1.0; +https://labs.itresit.es)"

def fetch(url: str, timeout: int = 15) -> requests.Response:
    resp = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        },
        timeout=timeout,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp

def normalize_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse(p._replace(query="", fragment=""))

def filename_key(u: str) -> str:
    path = urlparse(u).path
    base = path.rsplit("/", 1)[-1]
    name = base.rsplit(".", 1)[0] if "." in base else base
    name = re.sub(r"-(\d{2,5})x(\d{2,5})$", "", name)
    name = re.sub(r"-scaled$", "", name, flags=re.I)
    name = re.sub(r"@[\dx]+$", "", name)
    return name.lower()

def tag_localname(tag: str) -> str:
    # '{ns}url' -> 'url' ; 'url' -> 'url'
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def direct_children_by_localname(el: ET.Element, name: str):
    return [c for c in list(el) if tag_localname(c.tag) == name]

def first_direct_child_text(el: ET.Element, name: str) -> str | None:
    for c in list(el):
        if tag_localname(c.tag) == name:
            t = (c.text or "").strip()
            if t:
                return t
    return None

def parse_xml(text: str) -> ET.Element:
    return ET.fromstring(text)

def guess_sitemap_url(site: str, timeout: int) -> str | None:
    site = site.rstrip("/")
    for cand in (f"{site}/sitemap.xml", f"{site}/sitemap_index.xml"):
        try:
            r = fetch(cand, timeout)
            if r.status_code == 200 and r.text.strip():
                return cand
        except Exception:
            pass
    # robots.txt as a last resort
    try:
        r = fetch(f"{site}/robots.txt", timeout)
        for ln in r.text.splitlines():
            if ln.lower().startswith("sitemap:"):
                sm_url = ln.split(":", 1)[1].strip()
                if sm_url:
                    try:
                        rr = fetch(sm_url, timeout)
                        if rr.status_code == 200 and rr.text.strip():
                            return sm_url
                    except Exception:
                        continue
    except Exception:
        pass
    return None

def iter_urlsets_from_sitemap(sitemap_url: str, timeout: int = 15):
    r = fetch(sitemap_url, timeout)
    root = parse_xml(r.text)
    root_name = tag_localname(root.tag)

    if root_name == "sitemapindex":
        for sm in direct_children_by_localname(root, "sitemap"):
            loc = first_direct_child_text(sm, "loc")
            if not loc:
                continue
            try:
                rr = fetch(loc, timeout)
                sub_root = parse_xml(rr.text)
                if tag_localname(sub_root.tag) == "urlset":
                    yield loc, sub_root
            except Exception as e:
                print(f"[WARN] Could not open sub-sitemap {loc}: {e}", file=sys.stderr)
    elif root_name == "urlset":
        yield sitemap_url, root
    else:
        print(f"[WARN] {sitemap_url} is not a sitemapindex/urlset (root={root_name}).", file=sys.stderr)

def extract_entries(urlset_root: ET.Element):
    entries = []
    for url_el in direct_children_by_localname(urlset_root, "url"):
        loc_text = first_direct_child_text(url_el, "loc")
        if not loc_text:
            continue

        imgs = []
        # Dentro de <url>, WordPress suele poner múltiples <image:image> (localname 'image')
        for img_el in direct_children_by_localname(url_el, "image"):
            for loc_el in direct_children_by_localname(img_el, "loc"):
                t = (loc_el.text or "").strip()
                if t:
                    imgs.append(t)

        entries.append({"loc": loc_text, "images": imgs})
    return entries

def extract_page_images(page_url: str, html: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    # <img ...>, atributos lazy y srcset
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            v = img.get(attr)
            if v:
                urls.add(normalize_url(urljoin(page_url, v)))
        for attr in ("srcset", "data-srcset"):
            v = img.get(attr)
            if v:
                for part in v.split(","):
                    u = part.strip().split(" ")[0]
                    if u:
                        urls.add(normalize_url(urljoin(page_url, u)))

    # <source srcset=...> dentro de <picture>
    for src in soup.find_all("source"):
        v = src.get("srcset")
        if v:
            for part in v.split(","):
                u = part.strip().split(" ")[0]
                if u:
                    urls.add(normalize_url(urljoin(page_url, u)))

    # og:image
    for m in soup.find_all("meta", attrs={"property": "og:image"}):
        v = m.get("content")
        if v:
            urls.add(normalize_url(urljoin(page_url, v)))

    keys = {filename_key(u) for u in urls if u}
    return urls, keys

def check_article_images(article_url: str, declared_imgs: list[str], timeout: int = 15) -> dict:
    try:
        r = fetch(article_url, timeout)
    except Exception as e:
        return {
            "url": article_url,
            "declared": declared_imgs,
            "present_exact": [],
            "present_fuzzy": [],
            "missing": declared_imgs[:],
            "error": f"Could not fetch the article: {e}",
        }

    page_urls, page_keys = extract_page_images(article_url, r.text)

    present_exact, present_fuzzy, missing = [], [], []
    for img in declared_imgs:
        abs_img = img if urlparse(img).netloc else urljoin(article_url, img)
        norm_img = normalize_url(abs_img)
        key = filename_key(norm_img)

        if norm_img in page_urls:
            present_exact.append(img)
        elif key in page_keys:
            present_fuzzy.append(img)
        else:
            missing.append(img)

    return {
        "url": article_url,
        "declared": declared_imgs,
        "present_exact": present_exact,
        "present_fuzzy": present_fuzzy,
        "missing": missing,
        "error": None,
    }

def main():
    ap = argparse.ArgumentParser(description="Check whether image URLs declared in a sitemap are present in the live article HTML.")
    ap.add_argument("site", help="Base site (https://domain) or direct sitemap URL (.xml)")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=0, help="Only process N URLs per urlset (0 = all)")
    args = ap.parse_args()

    # Locate the sitemap if you pass a base domain
    if args.site.endswith(".xml"):
        sitemap_url = args.site
    else:
        sitemap_url = guess_sitemap_url(args.site, args.timeout)

    if not sitemap_url:
        print("[ERROR] Could not locate a sitemap.", file=sys.stderr)
        sys.exit(2)

    print(f"[INFO] Using sitemap: {sitemap_url}")

    any_alert = False
    total_entries = 0

    for sub_url, urlset in iter_urlsets_from_sitemap(sitemap_url, timeout=args.timeout):
        entries = extract_entries(urlset)
        if args.limit > 0:
            entries = entries[: args.limit]

        print(f"\n[INFO] Sub-sitemap: {sub_url} — {len(entries)} entries")
        total_entries += len(entries)

        for i, entry in enumerate(entries, 1):
            loc = entry["loc"]
            imgs = entry["images"]
            print(f"\n[{i}/{len(entries)}] URL: {loc}")
            print(f"    Images declared in sitemap: {len(imgs)}")

            res = check_article_images(loc, imgs, timeout=args.timeout)
            if res["error"]:
                print(f"    [ERROR] {res['error']}")
                any_alert = True
                continue

            present_total = len(res["present_exact"]) + len(res["present_fuzzy"])
            print(f"    Found in live HTML: {present_total} "
                  f"(exact: {len(res['present_exact'])}, fuzzy: {len(res['present_fuzzy'])})")

            if res["missing"]:
                any_alert = True
                print("    >>> ALERT: sitemap declares images that are NOT present in the live article HTML:")
                for m in res["missing"]:
                    print(f"        - {m}")

            if res["present_exact"]:
                print("    Present (exact):")
                for u in res["present_exact"]:
                    print(f"        - {u}")
            if res["present_fuzzy"]:
                print("    Present (fuzzy filename match):")
                for u in res["present_fuzzy"]:
                    print(f"        - {u}")

            time.sleep(args.sleep)

    if total_entries == 0:
        print("\n[WARN] The analyzed sitemap contains zero <url> entries (WAF / error HTML? sitemapindex without accessible sub-sitemaps?).")
        sys.exit(3)

    if any_alert:
        print("\n[SUMMARY] Discrepancies detected (see ALERT lines above).")
        sys.exit(1)
    else:
        print("\n[SUMMARY] OK: sitemap image declarations match the live HTML.")
        sys.exit(0)

if __name__ == "__main__":
    main()
