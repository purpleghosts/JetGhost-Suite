#!/usr/bin/env python3
"""
wp_media_explorer.py

Simple client for WordPress /wp-json/wp/v2/media, intended to:

- Browse attachments via the REST API.
- Filter by parent post, mime_type, search, etc.
- Analyze filename patterns:
  - WordPress size suffixes: -300x200, -768x512, etc.
  - Numeric collision suffixes: -1, -2, -3 (name collisions).

Requires: requests

Examples:

  # List attachments for a specific post, in "table" mode
  python wp_media_explorer.py https://thedfirreport.com --parent 49943 --show-basic

  # Analyze filename patterns (suffixes -1 -2 -3)
  python wp_media_explorer.py https://thedfirreport.com --parent 49943 --analyze-patterns

  # Force a browser-like User-Agent and show HTTP logs
  python wp_media_explorer.py https://thedfirreport.com --parent 49943 --analyze-patterns \
      --user-agent "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36" \
      -v
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


SIZE_SUFFIX_RE = re.compile(r"-(\d+)x(\d+)$")  # ej: image-300x200.png
NUM_SUFFIX_RE = re.compile(r"-(\d+)$")        # ej: image-1.png, image-02.png


class WPMediaClient:
    """
    Minimal client for /wp-json/wp/v2/media with:
    - A browser-like User-Agent by default.
    - Basic retries on 503/429.
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 10,
        user_agent: Optional[str] = None,
        max_retries: int = 3,
        backoff: float = 2.0,
        verbose: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.verbose = verbose

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def _get_with_retries(self, url: str, params: Dict[str, Any]) -> requests.Response:
        last_exc: Optional[BaseException] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                last_exc = e
                self._log(
                    f"[!] Connection error on attempt {attempt}/{self.max_retries}: {e}"
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(self.backoff * attempt)
                continue

            if resp.status_code in (503, 429):
                self._log(
                    f"[!] {resp.status_code} for {url}, attempt {attempt}/{self.max_retries}"
                )
                if attempt == self.max_retries:
                    resp.raise_for_status()
                time.sleep(self.backoff * attempt)
                continue

            # For other errors, raise immediately
            resp.raise_for_status()
            return resp

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected error in _get_with_retries")

    def iter_media(
        self,
        max_pages: Optional[int] = None,
        **params: Any,
    ):
        """
        Iterate over /wp-json/wp/v2/media with pagination.

        Typical parameters:
          - parent (int)
          - mime_type (str)
          - search (str)
          - per_page (int, max 100)
        """
        page = 1
        per_page = int(params.pop("per_page", 100))
        if per_page > 100:
            per_page = 100

        while True:
            if max_pages is not None and page > max_pages:
                break

            query = dict(params)
            query["per_page"] = per_page
            query["page"] = page

            url = f"{self.base_url}/wp-json/wp/v2/media"
            self._log(f"[+] GET {url} params={query}")

            resp = self._get_with_retries(url, params=query)
            data = resp.json()

            if not isinstance(data, list):
                raise requests.exceptions.HTTPError(
                    f"Unexpected response format (expected a list, got {type(data)})",
                    response=resp,
                )

            if not data:
                break

            for item in data:
                yield item

            total_pages_header = resp.headers.get("X-WP-TotalPages")
            if total_pages_header is not None:
                try:
                    total_pages = int(total_pages_header)
                except ValueError:
                    total_pages = page
            else:
                total_pages = page

            self._log(f"[+] Page {page}/{total_pages}")

            if page >= total_pages:
                break
            page += 1


def parse_filename(name: str) -> Dict[str, Any]:
    """
    Parse a filename and extract:
      - root: base name without -WxH or numeric suffixes (-1, -2, ...)
      - num_suffix: entero si hay -1/-2/etc, o None
      - ext: extension (with dot, e.g. '.png')
      - is_wp_size: True si tiene sufijo tipo -300x200
      - width, height: if it is a WordPress size variant
      - basename: nombre base (sin ruta)
    """
    # Puede venir como ruta relativa (2025/11/file.png) o como URL completa
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)

    width = height = None
    is_wp_size = False

    m_size = SIZE_SUFFIX_RE.search(stem)
    if m_size:
        is_wp_size = True
        width = int(m_size.group(1))
        height = int(m_size.group(2))
        stem_no_size = stem[: m_size.start()]
    else:
        stem_no_size = stem

    m_num = NUM_SUFFIX_RE.search(stem_no_size)
    if m_num:
        root = stem_no_size[: m_num.start()]
        try:
            num_suffix = int(m_num.group(1))
        except ValueError:
            num_suffix = None
    else:
        root = stem_no_size
        num_suffix = None

    return {
        "root": root,
        "num_suffix": num_suffix,
        "ext": ext.lower(),
        "is_wp_size": is_wp_size,
        "width": width,
        "height": height,
        "basename": base,
    }


def collect_file_entries(media_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Given a WordPress 'media' object, return a list of file entries:
      {
        "kind": "original" | "size:<name>" | "source_url",
        "attachment_id": <id>,
        "filename": <relative path or URL>,
        "mime_type": <mime>,
        "media_type": <image|file|...>,
        "title": <title>
      }
    """
    entries: List[Dict[str, Any]] = []

    mid = media_item.get("id")
    mime_type = media_item.get("mime_type")
    media_type = media_item.get("media_type") or media_item.get("type")
    title = (media_item.get("title") or {}).get("rendered") or ""

    details = media_item.get("media_details") or {}

    # Main file
    main_file = details.get("file")
    if main_file:
        entries.append(
            {
                "kind": "original",
                "attachment_id": mid,
                "filename": main_file,
                "mime_type": mime_type,
                "media_type": media_type,
                "title": title,
            }
        )

    # WordPress sizes
    sizes = details.get("sizes") or {}
    for size_name, size_data in sizes.items():
        f = size_data.get("file")
        if not f:
            continue
        entries.append(
            {
                "kind": f"size:{size_name}",
                "attachment_id": mid,
                "filename": f,
                "mime_type": mime_type,
                "media_type": media_type,
                "title": title,
            }
        )

    # Fallback: URL directa si no hay media_details
    if not entries:
        src_url = media_item.get("source_url") or (media_item.get("guid") or {}).get(
            "rendered"
        )
        if src_url:
            entries.append(
                {
                    "kind": "source_url",
                    "attachment_id": mid,
                    "filename": src_url,
                    "mime_type": mime_type,
                    "media_type": media_type,
                    "title": title,
                }
            )

    return entries


def analyze_patterns(
    items: List[Dict[str, Any]],
    min_suffixes: int = 1,
    include_wp_sizes: bool = False,
) -> None:
    """
    Analyze filename patterns from a list of media items.

    - Group by (root, ext).
    - Show numeric suffixes (-1, -2, -3) and WP sizes (-300x200, etc).
    """

    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for media_item in items:
        file_entries = collect_file_entries(media_item)
        for entry in file_entries:
            info = parse_filename(entry["filename"])
            key = (info["root"], info["ext"])

            group = groups.setdefault(
                key,
                {
                    "root": info["root"],
                    "ext": info["ext"],
                    "files": [],  # lista de entries enriquecidas
                    "numeric_suffixes": set(),
                    "wp_sizes": set(),
                },
            )

            enriched = {
                "attachment_id": entry["attachment_id"],
                "kind": entry["kind"],
                "basename": info["basename"],
                "num_suffix": info["num_suffix"],
                "is_wp_size": info["is_wp_size"],
                "width": info["width"],
                "height": info["height"],
                "mime_type": entry["mime_type"],
                "media_type": entry["media_type"],
                "title": entry["title"],
            }

            group["files"].append(enriched)

            if info["num_suffix"] is not None:
                group["numeric_suffixes"].add(info["num_suffix"])

            if info["is_wp_size"] and info["width"] and info["height"]:
                group["wp_sizes"].add((info["width"], info["height"]))

    # Filter "interesting" groups
    interesting: List[Dict[str, Any]] = []
    for group in groups.values():
        has_numeric = len(group["numeric_suffixes"]) >= min_suffixes and len(
            group["numeric_suffixes"]
        ) > 0
        has_wp_sizes = len(group["wp_sizes"]) > 1  # varios sizes

        if has_numeric or (include_wp_sizes and has_wp_sizes):
            interesting.append(group)

    # Sort by number of suffixes and number of files
    interesting.sort(
        key=lambda g: (len(g["numeric_suffixes"]), len(g["files"])), reverse=True
    )

    if not interesting:
        print(
            "[i] No groups found with numeric suffixes or relevant WP sizes.",
            file=sys.stderr,
        )
        return

    for group in interesting:
        root = group["root"]
        ext = group["ext"]
        nums = sorted(group["numeric_suffixes"])
        wp_sizes_sorted = sorted(group["wp_sizes"])

        print(f"\n=== Group root='{root}' ext='{ext}' ===")
        print(f"  Total files in group: {len(group['files'])}")

        if nums:
            print(f"  Numeric suffixes found: {nums}")
        else:
            print("  Numeric suffixes found: none")

        if wp_sizes_sorted:
            sizes_str = ", ".join(f"{w}x{h}" for w, h in wp_sizes_sorted)
            print(f"  WP sizes found: {sizes_str}")
        else:
            print("  WP sizes found: none")

        print("  Files:")
        for f in group["files"]:
            parts = [f"    - id={f['attachment_id']:>6}", f"kind={f['kind']:<12}"]
            if f["num_suffix"] is not None:
                parts.append(f"suffix={f['num_suffix']}")
            if f["is_wp_size"] and f["width"] and f["height"]:
                parts.append(f"wp_size={f['width']}x{f['height']}")
            parts.append(f"name={f['basename']}")
            print(" | ".join(parts))


def print_basic(items: List[Dict[str, Any]]) -> None:
    """
    Print a tabular summary for each media item:
      id, date, mime_type, media_type, title, source_url
    """
    for media_item in items:
        mid = media_item.get("id")
        date = media_item.get("date")
        mime_type = media_item.get("mime_type")
        media_type = media_item.get("media_type") or media_item.get("type")
        title = (media_item.get("title") or {}).get("rendered") or ""
        src = media_item.get("source_url") or (media_item.get("guid") or {}).get(
            "rendered"
        )

        print(
            f"{mid}\t{date}\t{mime_type or ''}\t{media_type or ''}\t{title}\t{src or ''}"
        )


def print_json(items: List[Dict[str, Any]]) -> None:
    """
    Print the full JSON for each item (one block per item).
    """
    for media_item in items:
        print(json.dumps(media_item, ensure_ascii=False, indent=2))
        print()  # blank line between items


def cli() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Client for /wp-json/wp/v2/media with filename pattern analysis "
            "(-1 -2 -3 and WordPress size variants)."
        )
    )

    parser.add_argument(
        "base_url",
        help="WordPress base URL (e.g., https://thedfirreport.com)",
    )

    parser.add_argument(
        "--parent",
        type=int,
        help="Filter by parent post ID (the 'parent' parameter).",
    )
    parser.add_argument(
        "--mime-type",
        help="Filter by mime_type (e.g., image/png, application/pdf, ...).",
    )
    parser.add_argument(
        "--search",
        help="Search string (the 'search' parameter).",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Items per page (max 100; default 100).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Maximum number of pages to walk (default: all).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum total number of media items to process.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="HTTP request timeout in seconds (default 10).",
    )
    parser.add_argument(
        "--user-agent",
        help="Custom HTTP User-Agent (default: a Chrome-on-Linux UA string).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Max retries on 503/429 errors (default 3).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose mode (log requests and pages to stderr).",
    )

    # Modos de salida
    parser.add_argument(
        "--show-basic",
        action="store_true",
        help="Show a basic summary for each media item.",
    )
    parser.add_argument(
        "--show-json",
        action="store_true",
        help="Print the full JSON for each media item.",
    )
    parser.add_argument(
        "--analyze-patterns",
        action="store_true",
        help="Analyze filename patterns (-1, -2, -3 and WP size variants).",
    )
    parser.add_argument(
        "--patterns-min-suffixes",
        type=int,
        default=1,
        help=(
            "Minimum distinct numeric suffixes for a group to be shown "
            "(default 1)."
        ),
    )
    parser.add_argument(
        "--patterns-include-wp-sizes",
        action="store_true",
        help=(
            "Include groups that only have WP sizes (no numeric suffixes). "
            "Ignored by default."
        ),
    )

    args = parser.parse_args()

    # If the user does not specify an output mode, default to --show-basic
    if not (args.show_basic or args.show_json or args.analyze_patterns):
        args.show_basic = True

    client = WPMediaClient(
        args.base_url,
        timeout=args.timeout,
        user_agent=args.user_agent,
        max_retries=args.max_retries,
        verbose=args.verbose,
    )

    params: Dict[str, Any] = {}
    if args.parent is not None:
        params["parent"] = args.parent
    if args.mime_type:
        params["mime_type"] = args.mime_type
    if args.search:
        params["search"] = args.search
    params["per_page"] = args.per_page

    items: List[Dict[str, Any]] = []

    try:
        count = 0
        for media_item in client.iter_media(max_pages=args.max_pages, **params):
            items.append(media_item)
            count += 1
            if args.limit and count >= args.limit:
                break

    except requests.exceptions.HTTPError as e:
        print(f"[!] HTTP error: {e}", file=sys.stderr)
        resp = getattr(e, "response", None)
        if resp is not None:
            body = resp.text or ""
            if body:
                print(
                    "--- Primeros 500 caracteres de la respuesta HTTP ---",
                    file=sys.stderr,
                )
                print(body[:500], file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as e:
        print(f"[!] Error de red: {e}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"[+] Total de media items obtenidos: {len(items)}", file=sys.stderr)

    if args.show_basic:
        print_basic(items)

    if args.show_json:
        print_json(items)

    if args.analyze_patterns:
        analyze_patterns(
            items,
            min_suffixes=args.patterns_min_suffixes,
            include_wp_sizes=args.patterns_include_wp_sizes,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
