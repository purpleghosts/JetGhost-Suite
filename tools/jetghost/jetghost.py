#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JetGhost — WordPress sitemap leakage auditor (images, videos, attachments)
vendor related to Jetpack/WP.com

Focus of this build:
- Stronger Jetpack/WP.com fingerprint
- Sitemap discovery also checks index.php/sitemap*.xml endpoints
- --jetpack-only and --assert-jetpack-leak modes for advisory workflows

Brief output (one leak per line):
  LEAKTYPE<TAB>context_url<TAB>leaked_url

Exit codes:
 0 = OK (no leaks)
 1 = Leaks found
 2 = Could not locate/fetch sitemap
 3 = Sitemap contained zero <url> entries (for the current phase)
 4 = Not a Jetpack/WP.com vendor (when --jetpack-only/--assert-jetpack-leak is used)
"""

import argparse
import re
import sys
import time
from collections import defaultdict
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; JetGhost/2.1; +https://labs.itresit.es)"

# --- HTTP helpers ------------------------------------------------------------

def fetch(url: str, timeout: int = 15) -> requests.Response:
    """HTTP GET with a friendly UA and sane defaults."""
    resp = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/xml,text/xml;text/html;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        },
        timeout=timeout,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp

def head(url: str, timeout: int = 10) -> requests.Response | None:
    """HTTP HEAD best-effort; returns None on failure."""
    try:
        resp = requests.head(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        if 200 <= resp.status_code < 400:
            return resp
    except Exception:
        return None
    return None

# --- URL / filename normalization -------------------------------------------

def normalize_url(u: str) -> str:
    """Normalize URL for comparisons: strip query/fragment."""
    p = urlparse(u)
    return urlunparse(p._replace(query="", fragment=""))

def filename_key(u: str) -> str:
    """
    Fuzzy key by filename (WordPress-friendly):
    - lowercase basename without extension
    - strip -123x456, -scaled, @2x suffixes
    """
    path = urlparse(u).path
    base = path.rsplit("/", 1)[-1]
    name = base.rsplit(".", 1)[0] if "." in base else base
    name = re.sub(r"-(\d{2,5})x(\d{2,5})$", "", name)   # -800x600
    name = re.sub(r"-scaled$", "", name, flags=re.I)    # -scaled
    name = re.sub(r"@[\dx]+$", "", name)                # @2x
    return name.lower()

def is_probably_image(u: str) -> bool:
    return re.search(r"\.(?:png|jpe?g|gif|webp|svg|bmp|tiff?)$", u.split("?")[0], re.I) is not None

def is_probably_video(u: str) -> bool:
    return re.search(r"\.(?:mp4|mov|webm|m4v|ogg|ogv)$", u.split("?")[0], re.I) is not None

# --- XML helpers (namespace-agnostic) ---------------------------------------

def tag_localname(tag: str) -> str:
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

# --- Vendor fingerprinting ---------------------------------------------------

def detect_vendor(xml_text: str) -> str:
    """
    Returns one of: 'wpcom', 'jetpack', 'yoast', 'rank-math', 'aioseo', 'seopress', 'core', 'unknown'
    """
    t = xml_text.lower()

    # Core WP: wp-sitemap (no image/video extensions)
    if "<urlset" in t and "wp-sitemap" in t:
        return "core"

    # WordPress.com (Automattic) markers
    if 'generator="wordpress.com"' in t:
        return "wpcom"
    # Jetpack markers
    if "jetpack" in t:
        return "jetpack"

    # SEO plugins
    if "yoast" in t or "yoast seo" in t:
        return "yoast"
    if "rank math" in t or "rank-math" in t:
        return "rank-math"
    if "all in one seo" in t or "aioseo" in t:
        return "aioseo"
    if "seopress" in t:
        return "seopress"

    # If images/videos namespace present but no string markers → unknown plugin flavor
    if "<image:image" in t or "<video:video" in t:
        return "unknown"

    return "unknown"

# --- Sitemap discovery -------------------------------------------------------

def guess_sitemap_url(site: str, timeout: int) -> str | None:
    """
    Try common sitemap endpoints, including index.php based routes.
    Preference order keeps typical SEO plugin indices early.
    """
    site = site.rstrip("/")
    candidates = (
        f"{site}/sitemap_index.xml",
        f"{site}/sitemap.xml",
        f"{site}/wp-sitemap.xml",
        f"{site}/index.php/sitemap_index.xml",
        f"{site}/index.php/sitemap.xml",
    )
    for cand in candidates:
        try:
            r = fetch(cand, timeout)
            if r.status_code == 200 and r.text.strip():
                return cand
        except Exception:
            pass

    # robots.txt fallback
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
    """
    Yield (sub_sitemap_url, urlset_root, raw_text) for each urlset discovered.
    Follows sitemapindex -> sub-sitemaps automatically.
    """
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
                    yield loc, sub_root, rr.text
            except Exception as e:
                print(f"[WARN] Could not open sub-sitemap {loc}: {e}", file=sys.stderr)
    elif root_name == "urlset":
        yield sitemap_url, root, r.text
    else:
        print(f"[WARN] {sitemap_url} is neither sitemapindex nor urlset (root={root_name}).", file=sys.stderr)

# --- Extraction from urlset --------------------------------------------------

def extract_entries(urlset_root: ET.Element):
    """
    Return list of dicts: {loc: str, images: [str], videos: [str]}
    - images from <image:image><image:loc>
    - videos from <video:video><video:content_loc>/<video:player_loc>/<video:thumbnail_loc>
    """
    entries = []
    for url_el in direct_children_by_localname(urlset_root, "url"):
        loc_text = first_direct_child_text(url_el, "loc")
        if not loc_text:
            continue

        imgs, vids = [], []

        # Images
        for img_el in direct_children_by_localname(url_el, "image"):
            for loc_el in direct_children_by_localname(img_el, "loc"):
                t = (loc_el.text or "").strip()
                if t:
                    imgs.append(t)

        # Videos
        for vid_el in direct_children_by_localname(url_el, "video"):
            # content_loc (direct file), player_loc (embed), thumbnail_loc (preview)
            for nm in ("content_loc", "player_loc", "thumbnail_loc"):
                for loc_el in direct_children_by_localname(vid_el, nm):
                    t = (loc_el.text or "").strip()
                    if t:
                        vids.append(t)

        entries.append({"loc": loc_text, "images": imgs, "videos": vids})
    return entries

# --- HTML media discovery ----------------------------------------------------

def extract_page_media(page_url: str, html: str) -> tuple[set[str], set[str], set[str]]:
    """
    Collect media URLs from HTML:
    - <img src|data-src|...> and srcset
    - <video src>, <source src>, <iframe src> (for embeds), and og:image
    Returns (normalized_urls_all, fuzzy_image_keys, fuzzy_video_keys)
    """
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    # <img>
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

    # <video> / <source>
    for vid in soup.find_all(["video", "source"]):
        v = vid.get("src")
        if v:
            urls.add(normalize_url(urljoin(page_url, v)))

    # <iframe> embeds
    for ifr in soup.find_all("iframe"):
        v = ifr.get("src")
        if v:
            urls.add(normalize_url(urljoin(page_url, v)))

    # og:image
    for m in soup.find_all("meta", attrs={"property": "og:image"}):
        v = m.get("content")
        if v:
            urls.add(normalize_url(urljoin(page_url, v)))

    img_keys = {filename_key(u) for u in urls if is_probably_image(u)}
    vid_keys = {filename_key(u) for u in urls if is_probably_video(u)}
    return urls, img_keys, vid_keys

# --- Post page checker (images/videos declared vs HTML) ----------------------

def check_post_media(article_url: str, declared_imgs: list[str], declared_vids: list[str], timeout: int = 15):
    try:
        r = fetch(article_url, timeout)
    except Exception as e:
        return {
            "url": article_url,
            "missing_images": declared_imgs[:],
            "missing_videos": declared_vids[:],
            "error": f"Article fetch failed: {e}",
        }

    page_urls, img_keys, vid_keys = extract_page_media(article_url, r.text)

    missing_images, missing_videos = [], []

    # Images
    for img in declared_imgs:
        abs_img = img if urlparse(img).netloc else urljoin(article_url, img)
        norm_img = normalize_url(abs_img)
        key = filename_key(norm_img)
        if norm_img in page_urls:
            continue
        if key in img_keys:
            continue
        missing_images.append(img)

    # Videos
    for v in declared_vids:
        abs_v = v if urlparse(v).netloc else urljoin(article_url, v)
        norm_v = normalize_url(abs_v)
        key = filename_key(norm_v)
        if norm_v in page_urls:
            continue
        if key in vid_keys:
            continue
        missing_videos.append(v)

    return {"url": article_url, "missing_images": missing_images, "missing_videos": missing_videos, "error": None}

# --- Core attachments auditor ------------------------------------------------

def collect_all_post_pages(sitemap_index_url: str, timeout: int = 15, limit: int = 0):
    """
    Crawl all post urlsets (vendor-agnostic) and return:
      - list of post URLs
      - sets of discovered media keys (images/videos) from the HTML of ALL posts
    Used to detect orphan public attachments (Core mode).
    """
    all_posts = []
    seen = set()
    img_keys_all, vid_keys_all = set(), set()

    for sub_url, urlset, _raw in iter_urlsets_from_sitemap(sitemap_index_url, timeout=timeout):
        entries = extract_entries(urlset)
        urls = [e["loc"] for e in entries]
        if not urls:
            continue

        # crude heuristic to skip attachment-only urlsets
        sample = urls[: min(10, len(urls))]
        ext_like_media = sum(1 for u in sample if is_probably_image(u) or is_probably_video(u))
        if ext_like_media >= max(3, len(sample)//2):
            continue

        if limit > 0:
            urls = urls[:limit]

        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            try:
                r = fetch(u, timeout)
                _all, imgk, vidk = extract_page_media(u, r.text)
                img_keys_all |= imgk
                vid_keys_all |= vidk
                all_posts.append(u)
            except Exception:
                continue

    return all_posts, img_keys_all, vid_keys_all

def iter_core_attachment_urls(core_root_url: str, timeout: int = 15):
    """
    Iterate all Core attachment sitemaps:
      /wp-sitemap-posts-attachment-1.xml, -2.xml, ...
    Yields each attachment URL.
    """
    # Discover from /wp-sitemap.xml
    base = core_root_url
    if not base.endswith("wp-sitemap.xml"):
        # best effort: assume root and fetch /wp-sitemap.xml
        site = core_root_url.rstrip("/").split("/wp-")[0]
        base = f"{site}/wp-sitemap.xml"

    try:
        r = fetch(base, timeout)
        root = parse_xml(r.text)
    except Exception:
        return

    # Find links to attachment urlsets inside the index (Core structure)
    for sm in root.iter():
        if tag_localname(sm.tag) == "sitemap":
            loc = first_direct_child_text(sm, "loc")
            if loc and "-attachment-" in loc:
                try:
                    rr = fetch(loc, timeout)
                    urlset = parse_xml(rr.text)
                    for url_el in direct_children_by_localname(urlset, "url"):
                        loc_text = first_direct_child_text(url_el, "loc")
                        if loc_text:
                            yield loc_text
                except Exception:
                    continue

# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="JetGhost — vendor-aware sitemap leakage auditor (Jetpack-focused).")
    ap.add_argument("site", help="Base site (https://domain) or direct sitemap URL (.xml)")
    ap.add_argument("--timeout", type=int, default=15, help="Network timeout in seconds (default: 15)")
    ap.add_argument("--sleep", type=float, default=0.15, help="Pause between requests (default: 0.15s)")
    ap.add_argument("--limit", type=int, default=0, help="Process only N URLs per urlset / per phase (0 = all)")

    # Leak types
    ap.add_argument("--leaks", choices=["all", "images", "videos", "attachments"], default="all",
                    help="Leak types to check (default: all)")

    # Output controls
    ap.add_argument("--brief", action="store_true",
                    help="Only print leaks, one per line: 'LEAKTYPE\\t<context_url>\\t<leaked_url>'")

    # Vendor assertions (advisory)
    ap.add_argument("--detect-only", action="store_true", help="Only fingerprint vendor and exit (0).")
    ap.add_argument("--jetpack-only", action="store_true",
                    help="Exit with code 4 if vendor is not Jetpack/WP.com.")
    ap.add_argument("--assert-jetpack-leak", action="store_true",
                    help="Exit 1 iff vendor is Jetpack/WP.com AND at least one leak is found; "
                         "exit 0 if none; exit 4 if vendor is not Jetpack/WP.com.")

    # Attachments refinement
    ap.add_argument("--verify-head", action="store_true",
                    help="For attachments mode, HEAD each URL and require image/* or video/* content-type.")
    args = ap.parse_args()

    # Determine sitemap URL
    if args.site.endswith(".xml"):
        sitemap_url = args.site
    else:
        sitemap_url = guess_sitemap_url(args.site, args.timeout)

    if not sitemap_url:
        print("[ERROR] Could not locate a sitemap for the provided site.", file=sys.stderr)
        sys.exit(2)

    # Vendor fingerprint
    try:
        raw0 = fetch(sitemap_url, args.timeout).text
        vendor = detect_vendor(raw0)
    except Exception:
        vendor = "unknown"

    is_jetpack_family = vendor in {"jetpack", "wpcom"}

    if not args.brief:
        print(f"[INFO] Using sitemap: {sitemap_url}")
        print(f"[INFO] Detected vendor: {vendor}")

    if args.detect_only:
        # Optional hard gate
        if args.jetpack_only and not is_jetpack_family:
            sys.exit(4)
        sys.exit(0)

    if args.jetpack_only and not is_jetpack_family:
        if not args.brief:
            print("[INFO] Skipping: not a Jetpack/WP.com sitemap.", file=sys.stderr)
        sys.exit(4)

    any_leak = False
    total_entries = 0
    per_type = defaultdict(int)

    # Phase A — Post urlsets (images/videos via plugin flavors)
    if args.leaks in ("all", "images", "videos"):
        for sub_url, urlset, _raw in iter_urlsets_from_sitemap(sitemap_url, timeout=args.timeout):
            entries = extract_entries(urlset)
            if args.limit > 0:
                entries = entries[: args.limit]

            # Skip pure attachment urlsets here; handled in Phase B
            if entries and all(is_probably_image(e["loc"]) or is_probably_video(e["loc"]) for e in entries[:min(5, len(entries))]):
                continue

            total_entries += len(entries)

            for entry in entries:
                loc = entry["loc"]
                imgs = entry["images"]
                vids = entry["videos"]

                if args.leaks == "images":
                    vids = []
                elif args.leaks == "videos":
                    imgs = []

                if not imgs and not vids:
                    time.sleep(args.sleep)
                    continue

                res = check_post_media(loc, imgs, vids, timeout=args.timeout)
                if res["error"]:
                    if not args.brief:
                        print(f"[ERROR] {loc}: {res['error']}", file=sys.stderr)
                    time.sleep(args.sleep)
                    continue

                for m in res["missing_images"]:
                    any_leak = True
                    per_type["IMAGE"] += 1
                    print(f"IMAGE\t{loc}\t{m}")
                for m in res["missing_videos"]:
                    any_leak = True
                    per_type["VIDEO"] += 1
                    print(f"VIDEO\t{loc}\t{m}")

                time.sleep(args.sleep)

    # Phase B — Core attachments (public but unreferenced across ALL posts)
    # Runs only if Core is present OR user explicitly asked for attachments.
    if args.leaks in ("all", "attachments"):
        posts, img_keys_all, vid_keys_all = collect_all_post_pages(sitemap_url, timeout=args.timeout, limit=args.limit)
        for att_url in iter_core_attachment_urls(sitemap_url, timeout=args.timeout):
            is_img = is_probably_image(att_url)
            is_vid = is_probably_video(att_url)

            if args.verify_head and not (is_img or is_vid):
                h = head(att_url, timeout=args.timeout)
                if h:
                    ct = (h.headers.get("Content-Type") or "").lower()
                    is_img = is_img or ct.startswith("image/")
                    is_vid = is_vid or ct.startswith("video/")

            if not (is_img or is_vid):
                continue

            key = filename_key(att_url)
            used = (key in img_keys_all) or (key in vid_keys_all)
            if not used:
                any_leak = True
                per_type["ATTACH"] += 1
                print(f"ATTACH\t-\t{att_url}")

            time.sleep(args.sleep)

    # Advisory mode: assert Jetpack leak
    if args.assert_jetpack_leak:
        if not is_jetpack_family:
            sys.exit(4)
        sys.exit(1 if any_leak else 0)

    # Standard exit logic
    if total_entries == 0 and args.leaks in ("all", "images", "videos"):
        if not args.brief:
            print("\n[WARN] The analyzed sitemap contains zero <url> entries (or only attachments).", file=sys.stderr)
        if args.leaks not in ("attachments",):
            sys.exit(3)

    if not args.brief:
        if any_leak:
            total = sum(per_type.values())
            print(f"\n[SUMMARY] Vendor={vendor} — leaks found: {total} "
                  f"(images={per_type['IMAGE']}, videos={per_type['VIDEO']}, attach={per_type['ATTACH']})")
        else:
            print(f"\n[SUMMARY] Vendor={vendor} — no leaks detected.")

    sys.exit(1 if any_leak else 0)

if __name__ == "__main__":
    main()
