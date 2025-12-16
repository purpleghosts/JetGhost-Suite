#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LeakLoom — tech-agnostic predictable media pattern detector.

Inputs:
  - One or more HTML pages (URL / file / directory) OR
  - One sitemap XML (URL / file; supports sitemapindex -> urlset; supports .gz)

It extracts media URLs (images/videos by extension by default), then groups them into
"guessable patterns" like:
  /uploads/{YYYY}/{MM}/image-{n}(-{modifier}).png
  /uploads/{YYYY}/{MM}/screenshot-dashboard-{n1}-{n2}.png

Optional:
  - Suggest likely counterpart URLs (e.g., unredacted variants or missing numbers)
  - HEAD-check suggestions against the server.

This is NOT WordPress-specific: it works with any HTML and any standard sitemap.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; LeakLoom/1.0; +https://labs.itresit.es)"

MEDIA_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|bmp|tiff?|mp4|mov|webm|m4v|ogg|ogv)$", re.I)

DEFAULT_MODIFIERS = {
    # redaction / privacy
    "redacted", "censored", "anonymized", "anonymised", "masked", "obfuscated", "pixelated", "blur", "blurred",
    # edits / crops / alternates
    "cropped", "crop", "trimmed", "resized", "edited", "edit", "final", "draft",
}

SENSITIVE_MODIFIERS = {
    "redacted", "censored", "anonymized", "anonymised", "masked", "obfuscated", "pixelated", "blur", "blurred",
}


# ------------------------- IO / network helpers -------------------------

def is_url(s: str) -> bool:
    try:
        p = urlparse(s)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def fetch_url(url: str, timeout: int = 20) -> requests.Response:
    r = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xml,text/xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        },
        timeout=timeout,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r


def head_url(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        r = requests.head(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        return r
    except Exception:
        return None


def read_text_from_source(src: str, timeout: int) -> tuple[str, str]:
    """
    Returns (text, base_url_for_relatives).
    base_url is used only for HTML to resolve relative links.
    For file inputs, base_url is file://<dir>/ (best-effort) but we mainly work with paths.
    """
    if is_url(src):
        r = fetch_url(src, timeout=timeout)
        content = r.content
        # handle gz sitemaps
        if src.lower().endswith(".gz") or (r.headers.get("Content-Type", "").lower().find("gzip") != -1):
            try:
                content = gzip.decompress(content)
            except Exception:
                pass
        return content.decode(r.encoding or "utf-8", errors="replace"), src

    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(src)

    data = p.read_bytes()
    if p.suffix.lower() == ".gz":
        data = gzip.decompress(data)
    text = data.decode("utf-8", errors="replace")
    base = p.parent.as_uri() + "/"  # file://...
    return text, base


# ------------------------- URL normalization -------------------------

def normalize_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse(p._replace(query="", fragment=""))


def looks_like_media_url(u: str) -> bool:
    pu = urlparse(u)
    path = pu.path or ""
    return MEDIA_EXT_RE.search(path) is not None


def safe_urljoin(base: str, u: str) -> str:
    # skip data URIs, javascript, mailto, etc.
    if not u:
        return ""
    if u.startswith("data:") or u.startswith("javascript:") or u.startswith("mailto:") or u.startswith("tel:"):
        return ""
    return urljoin(base, u)


# ------------------------- HTML extraction -------------------------

def extract_urls_from_html(html: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()

    # <img>
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            v = img.get(attr)
            if v:
                j = safe_urljoin(base_url, v.strip())
                if j:
                    urls.add(normalize_url(j))
        for attr in ("srcset", "data-srcset"):
            v = img.get(attr)
            if v:
                for part in v.split(","):
                    u = part.strip().split(" ")[0]
                    j = safe_urljoin(base_url, u)
                    if j:
                        urls.add(normalize_url(j))

    # <video>, <source>, <audio>
    for tag in soup.find_all(["video", "source", "audio"]):
        v = tag.get("src")
        if v:
            j = safe_urljoin(base_url, v.strip())
            if j:
                urls.add(normalize_url(j))

    # <a href> (often direct downloads / media)
    for a in soup.find_all("a"):
        v = a.get("href")
        if v:
            j = safe_urljoin(base_url, v.strip())
            if j:
                urls.add(normalize_url(j))

    # <link href> (icons, etc.)
    for ln in soup.find_all("link"):
        v = ln.get("href")
        if v:
            j = safe_urljoin(base_url, v.strip())
            if j:
                urls.add(normalize_url(j))

    return urls


# ------------------------- Sitemap extraction -------------------------

def tag_localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_xml(text: str) -> ET.Element:
    return ET.fromstring(text)


def iter_sitemap_urls(xml_text: str, base_for_relatives: str = "") -> Iterable[str]:
    """
    Yields all <loc> URLs found in a sitemapindex or urlset.
    Namespace-agnostic. Does not fetch sub-sitemaps here.
    """
    root = parse_xml(xml_text)
    for el in root.iter():
        if tag_localname(el.tag) == "loc":
            t = (el.text or "").strip()
            if not t:
                continue
            if base_for_relatives and not is_url(t):
                t = safe_urljoin(base_for_relatives, t)
            yield t


def iter_sitemap_urlsets(start_sitemap: str, timeout: int) -> Iterable[tuple[str, str]]:
    """
    Yields (sitemap_url, xml_text) for each urlset discovered,
    following sitemapindex recursively (1 level deep by default, but enough in practice).
    """
    xml, base = read_text_from_source(start_sitemap, timeout=timeout)
    root = parse_xml(xml)
    root_name = tag_localname(root.tag)

    if root_name == "sitemapindex":
        for loc in iter_sitemap_urls(xml, base_for_relatives=base):
            try:
                sub_xml, _ = read_text_from_source(loc, timeout=timeout)
                sub_root = parse_xml(sub_xml)
                if tag_localname(sub_root.tag) == "urlset":
                    yield loc, sub_xml
                elif tag_localname(sub_root.tag) == "sitemapindex":
                    # one extra level
                    for loc2 in iter_sitemap_urls(sub_xml, base_for_relatives=loc):
                        try:
                            sub2, _ = read_text_from_source(loc2, timeout=timeout)
                            sub2_root = parse_xml(sub2)
                            if tag_localname(sub2_root.tag) == "urlset":
                                yield loc2, sub2
                        except Exception:
                            continue
            except Exception:
                continue
    elif root_name == "urlset":
        yield start_sitemap, xml
    else:
        # Not a standard sitemap; still try to just yield it as-is
        yield start_sitemap, xml


# ------------------------- Pattern parsing & grouping -------------------------

def dir_template_from_path(path: str) -> str:
    """
    Replace predictable date segments in the directory:
      /.../2025/10/ -> /.../{YYYY}/{MM}/
      /.../2025/10/07/ -> /.../{YYYY}/{MM}/{DD}/
    """
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"

    # remove filename
    dirs = parts[:-1]
    out = []
    for seg in dirs:
        if re.fullmatch(r"(?:19|20)\d{2}", seg):
            out.append("{YYYY}")
            continue
        if out and out[-1] == "{YYYY}" and re.fullmatch(r"(0[1-9]|1[0-2])", seg):
            out.append("{MM}")
            continue
        if len(out) >= 2 and out[-1] == "{MM}" and out[-2] == "{YYYY}" and re.fullmatch(r"(0[1-9]|[12]\d|3[01])", seg):
            out.append("{DD}")
            continue
        out.append(seg)
    return "/" + "/".join(out) + ("/" if out else "/")


@dataclass(frozen=True)
class ParsedMedia:
    url: str
    dir_template: str
    prefix: str
    indices: tuple[int, ...]
    modifier: Optional[str]
    ext: str
    numlen: int


def parse_media_pattern(u: str, modifiers: set[str]) -> Optional[ParsedMedia]:
    nu = normalize_url(u)
    p = urlparse(nu)
    path = p.path or ""
    if not path or "/" not in path:
        return None

    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return None
    name, ext = base.rsplit(".", 1)
    ext = ext.lower()

    tokens = [t for t in name.split("-") if t]
    if not tokens:
        return None

    mod = None
    if tokens and tokens[-1].lower() in modifiers:
        mod = tokens.pop().lower()

    # collect trailing numeric tokens
    nums = []
    while tokens and re.fullmatch(r"\d+", tokens[-1]):
        nums.append(int(tokens.pop()))
    nums = tuple(reversed(nums))

    if not nums:
        return None

    prefix = "-".join(tokens).lower()
    if not prefix:
        return None

    dt = dir_template_from_path(path)
    return ParsedMedia(url=nu, dir_template=dt, prefix=prefix, indices=nums, modifier=mod, ext=ext, numlen=len(nums))


@dataclass
class PatternGroup:
    key: tuple[str, str, str, int]  # (dir_template, prefix, ext, numlen)
    items: list[ParsedMedia] = field(default_factory=list)

    def add(self, item: ParsedMedia):
        self.items.append(item)

    @property
    def dir_template(self) -> str:
        return self.key[0]

    @property
    def prefix(self) -> str:
        return self.key[1]

    @property
    def ext(self) -> str:
        return self.key[2]

    @property
    def numlen(self) -> int:
        return self.key[3]

    def modifiers_present(self) -> set[str]:
        return {i.modifier for i in self.items if i.modifier}

    def indices_present(self) -> set[tuple[int, ...]]:
        return {i.indices for i in self.items}

    def score(self) -> int:
        s = 0
        if "{YYYY}" in self.dir_template and "{MM}" in self.dir_template:
            s += 2
        if any(i.modifier for i in self.items):
            s += 2
        if self.numlen >= 1:
            s += 2
        if self.prefix in {"image", "images", "img", "video", "videos", "screenshot", "screen", "capture"}:
            s += 1
        s += min(3, len(self.items) // 5)
        return s

    def pattern_string(self) -> str:
        idx_tpl = "{n}" if self.numlen == 1 else "-".join(f"{{n{i+1}}}" for i in range(self.numlen))
        mod_tpl = "(-{modifier})" if self.modifiers_present() else ""
        return f"{self.dir_template}{self.prefix}-{idx_tpl}{mod_tpl}.{self.ext}"


def group_patterns(urls: Iterable[str], modifiers: set[str]) -> dict[tuple[str, str, str, int], PatternGroup]:
    groups: dict[tuple[str, str, str, int], PatternGroup] = {}
    for u in urls:
        pm = parse_media_pattern(u, modifiers=modifiers)
        if not pm:
            continue
        key = (pm.dir_template, pm.prefix, pm.ext, pm.numlen)
        if key not in groups:
            groups[key] = PatternGroup(key=key)
        groups[key].add(pm)
    return groups


# ------------------------- Suggestions -------------------------

def build_suggestions(group: PatternGroup) -> set[str]:
    """
    Suggest:
      - unmodified counterparts for items that only exist with sensitive modifier
      - missing numeric indices (only for numlen==1) in the observed range
    """
    sugg = set()
    seen_urls = {i.url for i in group.items}

    idx_to_mods: dict[tuple[int, ...], set[Optional[str]]] = {}
    for it in group.items:
        idx_to_mods.setdefault(it.indices, set()).add(it.modifier)

    # Suggest unmodified if only sensitive-modified exists for that index
    for idx, mods in idx_to_mods.items():
        if None in mods:
            continue
        if not (set(mods) & SENSITIVE_MODIFIERS):
            continue

        any_item = next((i for i in group.items if i.indices == idx), None)
        if not any_item:
            continue

        idx_part = "-".join(str(n) for n in idx)
        candidate = f"{any_item.dir_template}{any_item.prefix}-{idx_part}.{any_item.ext}"
        if candidate not in seen_urls:
            sugg.add(candidate)

    # Missing numbers for 1D indices
    if group.numlen == 1:
        nums = sorted({idx[0] for idx in idx_to_mods})
        if nums:
            lo, hi = nums[0], nums[-1]
            missing = [n for n in range(lo, hi + 1) if n not in set(nums)]

            for n in missing[:500]:
                candidate = f"{group.dir_template}{group.prefix}-{n}.{group.ext}"
                if candidate not in seen_urls:
                    sugg.add(candidate)

            sens = sorted(group.modifiers_present() & SENSITIVE_MODIFIERS)
            for n in missing[:200]:
                for m in sens[:10]:
                    candidate = f"{group.dir_template}{group.prefix}-{n}-{m}.{group.ext}"
                    if candidate not in seen_urls:
                        sugg.add(candidate)

    return sugg


# ------------------------- CLI / main -------------------------

def collect_urls_from_inputs(
    html_inputs: list[str],
    sitemap_input: Optional[str],
    timeout: int,
    media_only: bool,
    include_re: Optional[re.Pattern],
    exclude_re: Optional[re.Pattern],
    crawl_from_sitemap: bool
) -> set[str]:
    urls: set[str] = set()

    # HTML inputs
    for src in html_inputs:
        p = Path(src)
        if not is_url(src) and p.exists() and p.is_dir():
            for fp in p.rglob("*.htm*"):
                try:
                    html = fp.read_text(encoding="utf-8", errors="replace")
                    base = fp.parent.as_uri() + "/"
                    urls |= extract_urls_from_html(html, base)
                except Exception:
                    continue
        else:
            try:
                html, base = read_text_from_source(src, timeout=timeout)
                urls |= extract_urls_from_html(html, base)
            except Exception as e:
                print(f"[WARN] Could not read HTML source {src}: {e}", file=sys.stderr)

    # Sitemap input
    if sitemap_input:
        try:
            for sm_url, xml in iter_sitemap_urlsets(sitemap_input, timeout=timeout):
                for loc in iter_sitemap_urls(xml, base_for_relatives=sm_url):
                    urls.add(normalize_url(loc))

                if crawl_from_sitemap:
                    page_locs = []
                    for loc in iter_sitemap_urls(xml, base_for_relatives=sm_url):
                        if is_url(loc) and not looks_like_media_url(loc):
                            page_locs.append(loc)

                    for page in page_locs:
                        try:
                            r = fetch_url(page, timeout=timeout)
                            urls |= extract_urls_from_html(r.text, page)
                        except Exception:
                            continue
        except Exception as e:
            print(f"[WARN] Could not read sitemap {sitemap_input}: {e}", file=sys.stderr)

    out = set()
    for u in urls:
        if not u:
            continue
        if include_re and not include_re.search(u):
            continue
        if exclude_re and exclude_re.search(u):
            continue
        if media_only and not looks_like_media_url(u):
            continue
        out.add(u)

    return out


def print_human(groups: list[PatternGroup], top: int, examples: int, show_missing: bool):
    shown = 0
    for g in groups:
        if shown >= top:
            break
        shown += 1

        mods = sorted(g.modifiers_present())
        idxs = sorted(g.indices_present())

        print(f"\nPattern: {g.pattern_string()}")
        print(f"  Score: {g.score()}  |  Seen: {len(g.items)}  |  Unique indices: {len(idxs)}")
        if mods:
            print(f"  Modifiers: {', '.join(mods)}")

        if g.numlen == 1:
            nums = sorted({i.indices[0] for i in g.items})
            print(f"  Indices: {', '.join(map(str, nums[:80]))}{' ...' if len(nums) > 80 else ''}")
            if show_missing and nums:
                lo, hi = nums[0], nums[-1]
                missing = [n for n in range(lo, hi + 1) if n not in set(nums)]
                if missing:
                    print(f"  Missing in range [{lo}-{hi}]: {', '.join(map(str, missing[:80]))}{' ...' if len(missing) > 80 else ''}")
        else:
            idx_preview = ["-".join(map(str, t)) for t in idxs[:30]]
            print(f"  Index tuples: {', '.join(idx_preview)}{' ...' if len(idxs) > 30 else ''}")

        ex = sorted({i.url for i in g.items})[:examples]
        if ex:
            print("  Examples:")
            for u in ex:
                print(f"    - {u}")


def main():
    ap = argparse.ArgumentParser(description="LeakLoom — tech-agnostic predictable media pattern detector.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--html", nargs="+", help="HTML source(s): URL, file, or directory of .html files")
    src.add_argument("--sitemap", help="Sitemap XML source: URL or file (.xml or .gz)")

    ap.add_argument("--also-sitemap", help="Combine with a sitemap (if you used --html)")
    ap.add_argument("--crawl-from-sitemap", action="store_true",
                    help="If sitemap only lists pages (not media), fetch pages and extract media URLs (slower).")

    ap.add_argument("--all-urls", action="store_true",
                    help="Do NOT restrict to media extensions; analyze all URLs (default: media only).")
    ap.add_argument("--include", help="Only keep URLs matching this regex")
    ap.add_argument("--exclude", help="Drop URLs matching this regex")

    ap.add_argument("--modifiers", help="Comma-separated extra modifier tokens (case-insensitive)")
    ap.add_argument("--top", type=int, default=50, help="Show top N patterns (default: 50)")
    ap.add_argument("--examples", type=int, default=4, help="Show up to N example URLs per pattern (default: 4)")
    ap.add_argument("--missing", action="store_true", help="Show missing numbers in detected ranges")
    ap.add_argument("--suggest", action="store_true", help="Suggest likely counterpart URLs")
    ap.add_argument("--check", action="store_true", help="HEAD-check suggested URLs (only http/https)")
    ap.add_argument("--timeout", type=int, default=20, help="Network timeout seconds (default: 20)")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of human text")
    ap.add_argument("--brief", action="store_true",
                    help="Brief tab output: PATTERN\\tcount\\tpattern_string (plus SUGGEST lines if --suggest)")
    args = ap.parse_args()

    html_inputs = args.html or []
    sitemap_input = args.sitemap or args.also_sitemap

    include_re = re.compile(args.include) if args.include else None
    exclude_re = re.compile(args.exclude) if args.exclude else None

    modifiers = set(DEFAULT_MODIFIERS)
    if args.modifiers:
        modifiers |= {t.strip().lower() for t in args.modifiers.split(",") if t.strip()}

    urls = collect_urls_from_inputs(
        html_inputs=html_inputs,
        sitemap_input=sitemap_input,
        timeout=args.timeout,
        media_only=not args.all_urls,
        include_re=include_re,
        exclude_re=exclude_re,
        crawl_from_sitemap=args.crawl_from_sitemap,
    )

    groups_map = group_patterns(urls, modifiers=modifiers)
    groups = sorted(groups_map.values(), key=lambda g: (g.score(), len(g.items)), reverse=True)

    suggestions: dict[str, dict] = {}
    if args.suggest:
        for g in groups:
            for s in build_suggestions(g):
                suggestions.setdefault(s, {"url": s})

        if args.check:
            for u in list(suggestions.keys()):
                if is_url(u):
                    r = head_url(u, timeout=args.timeout)
                    if r is None:
                        suggestions[u]["status"] = None
                    else:
                        suggestions[u]["status"] = r.status_code
                        suggestions[u]["content_type"] = r.headers.get("Content-Type")
                else:
                    suggestions[u]["status"] = None

    if args.json:
        import json
        out = {
            "url_count": len(urls),
            "pattern_count": len(groups),
            "patterns": [
                {
                    "pattern": g.pattern_string(),
                    "dir_template": g.dir_template,
                    "prefix": g.prefix,
                    "ext": g.ext,
                    "numlen": g.numlen,
                    "score": g.score(),
                    "seen": len(g.items),
                    "modifiers": sorted(g.modifiers_present()),
                    "indices": sorted(["-".join(map(str, t)) for t in g.indices_present()]),
                    "examples": sorted({i.url for i in g.items})[: args.examples],
                }
                for g in groups[: args.top]
            ],
            "suggestions": list(suggestions.values()) if args.suggest else [],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if args.brief:
        for g in groups[: args.top]:
            print(f"PATTERN\t{len(g.items)}\t{g.pattern_string()}")
        if args.suggest:
            for u, meta in sorted(suggestions.items(), key=lambda kv: kv[0]):
                if args.check and meta.get("status") is not None:
                    print(f"SUGGEST\t{meta.get('status')}\t{u}")
                else:
                    print(f"SUGGEST\t-\t{u}")
        return

    print(f"[INFO] URLs analyzed: {len(urls)}")
    print(f"[INFO] Patterns found: {len(groups)}")
    print_human(groups, top=args.top, examples=args.examples, show_missing=args.missing)

    if args.suggest:
        print(f"\n[INFO] Suggestions: {len(suggestions)}")
        for i, (u, meta) in enumerate(sorted(suggestions.items(), key=lambda kv: kv[0])):
            if i >= 200:
                print("  ...")
                break
            if args.check and meta.get("status") is not None:
                ct = meta.get("content_type") or ""
                print(f"  - {meta['status']}\t{u}\t{ct}")
            else:
                print(f"  - {u}")


if __name__ == "__main__":
    main()
