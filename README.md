# JetGhost

JetGhost is a small toolset for finding **CMS media timeleaks**, that is to say cases where media files (screenshots, diagrams, slide images, videos, PDFs, etc.) escape their intended editorial timeline.

The main focus is **WordPress + Jetpack / WordPress.com image sitemaps**, where **attachments** can keep being listed (and therefore trivially discoverable) even after you remove or replace them in the live post HTML.

## Repository layout

- **`tools/jetghost/jetghost.py`** — main auditor: diffs sitemap-declared media vs live HTML
- **`tools/jetpack/jetpack-detect.py`** — fast vendor / Jetpack/WP.com sitemap fingerprinting (bulk)
- **`tools/jetpack/jetpack-leak.py`** — fast leak fingerprinting (bulk pre-filter)
- **`tools/wp/wp_media_explorer.py`** — enumerates `/wp-json/wp/v2/media` and analyzes filename collision / size patterns
- **`tools/patterns/leakloom.py`** — tech-agnostic detector for predictable media naming/versioning patterns
- **`tools/poc/img-exfil.py`** — minimal PoC: compares `<image:loc>` entries vs live HTML
- **`attic/`** — legacy scripts kept for historical context (do not use for production)
- **`docs/`** — write-up mapping and notes

## Install

Python 3.10+ is recommended.

```bash
pip install -r requirements.txt
```

## JetGhost usage

### Basic scan (sitemap discovery + image/video checks)

```bash
python tools/jetghost/jetghost.py https://example.com
```

Output is one leak per line:

```text
IMAGE\thttps://example.com/post/\thttps://example.com/wp-content/uploads/2025/06/original.png
```

### Only print leaks (machine-friendly)

```bash
python tools/jetghost/jetghost.py https://example.com --brief
```

### Only check images (skip videos/attachments)

```bash
python tools/jetghost/jetghost.py https://example.com --leaks images
```

### Advisory workflows (Jetpack/WP.com only)

- Exit with code `4` if the sitemap is not Jetpack/WP.com:

```bash
python tools/jetghost/jetghost.py https://example.com --jetpack-only
```

- “Assert Jetpack leak”: exit `1` *only if* the site is Jetpack/WP.com **and** at least one leak is found:

```bash
python tools/jetghost/jetghost.py https://example.com --assert-jetpack-leak
```

### Core attachment mode (orphan public attachments)

JetGhost can also flag **public attachment URLs** present in WordPress Core attachment sitemaps that do not appear in the HTML of *any* current post.

```bash
python tools/jetghost/jetghost.py https://example.com --leaks attachments
```

Optional `--verify-head` will HEAD-check and require an `image/*` or `video/*` content-type.

## Bulk helpers

### Detect Jetpack/WP.com sitemaps at scale

Input file: one sitemap URL per line.

```bash
python tools/jetpack/jetpack-detect.py -i sitemaps.txt -t 32 -T 6
```

### Pre-filter likely Jetpack image-sitemap leaks

```bash
python tools/jetpack/jetpack-leak.py -i sitemaps.txt -t 32 -T 6 --max-kb 256
```

## WordPress REST media enumeration

If a site exposes the REST API media catalog to unauthenticated users, you can enumerate and analyze filename patterns:

```bash
python tools/wp/wp_media_explorer.py https://example.com --analyze-patterns
```

## Predictable naming / versioning patterns (CMS-agnostic)

LeakLoom finds “guessable patterns” (numeric suffixes, redaction suffixes, ranges) and can suggest likely counterparts:

```bash
python tools/patterns/leakloom.py --sitemap https://example.com/sitemap.xml --crawl-from-sitemap --suggest --check
```

## Documentation

- See **`docs/article-mapping.md`** for a “which tool supports which section” mapping.

## Ethics & safety

Use these tools only on systems you own or where you have explicit permission to test.

When scanning at scale, be respectful: rate-limit, avoid excessive concurrency, and follow the target’s policies.

## License

MIT (see `LICENSE`).
