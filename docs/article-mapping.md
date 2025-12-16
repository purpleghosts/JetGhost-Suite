# Article mapping: scripts ↔ sections

This repo supports the writeup “CMS Media Timeleaks in Jetpack, WordPress and beyond”.
Below is how each script maps to the article sections.

## 1) Reproduction / “wtf moment”

- `tools/poc/img-exfil.py`
  - Minimal “sitemap image URL declared vs live HTML reference” PoC.
  - Best for demonstrating the core mismatch without vendor logic.

## 2) Jetpack image sitemap model (attachment-driven)

- `tools/jetghost/jetghost.py`
  - The operational auditor: enumerates image sitemaps and diffs against live HTML.
  - Output format matches the post’s example lines:
    `IMAGE <post_url> <image_url>`

## 3) JetGhost and observed impact (scanning + triage)

- `tools/jetghost/jetghost.py`
  - Full audit: find actual “sitemap-only” ghost media.

- `tools/jetpack/jetpack-detect.py`
  - Pre-filter: identify Jetpack/WP.com sitemap sources at scale.

- `tools/jetpack/jetpack-leak.py`
  - Very fast pre-filter: flag targets with strong image-sitemap/leak fingerprints
    (does not diff HTML; intended to shortlist targets for JetGhost).

## 6) WordPress media lifecycle amplifiers

### 6.1 Predictable versioning (name collisions / variants)

- `tools/wp/wp_media_explorer.py` (pattern analysis)
  - Shows numeric collision suffixes (-1, -2, -3) and WordPress size suffixes (-300x200).
  - Helps explain why older variants become easier to guess.

- `tools/patterns/leakloom.py`
  - Tech-agnostic “pattern enumerator” and suggestion engine for counterpart URLs.

### 6.3 WP-JSON media enumeration

- `tools/wp/wp_media_explorer.py`
  - Enumerates `/wp-json/wp/v2/media` and outputs metadata and source URLs.

## 7) The bigger picture: timeleaks beyond WordPress

### 7.2 Attacks on the past

- `tools/patterns/leakloom.py`
  - Designed to detect and exploit *predictability* in naming/versioning.
  - Suggests likely missing/previous variants and can HEAD-check existence.

## Legacy names

- `attic/ghostpress.py`, `attic/ghostpress.bak`
  - Historical internal iterations kept for reference.
  - JetGhost is the maintained “public” name and entry point.
