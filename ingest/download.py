#!/usr/bin/env python3
"""Download the Houston 311 extracts listed on the city's index page.

Links are scraped from the index page, not hardcoded. Each file is saved as
data/raw/<stem>_<YYYYMMDDTHHMM>.txt and recorded in data/raw/manifest.tsv with its
SHA-256, size and the server's Last-Modified.

Idempotent: a URL whose Last-Modified and size match a manifest entry whose
file still exists is skipped. Resumable: transfers go to a .part file that
curl continues with -C -.

Usage: download.py [--only REGEX] [--keep N]
  --only 'MTD|2026'  download a subset (the daily refresh)
  --keep 2           afterwards delete all but the newest N snapshots per source
                     (manifest rows are kept for provenance)
"""
import argparse
import csv
import datetime as dt
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

INDEX_URL = "https://311.houstontx.gov/servicerequestdata.html"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MANIFEST = RAW / "manifest.tsv"
FIELDS = ["url", "file", "bytes", "last_modified", "sha256", "downloaded_at"]


def curl(*args: str) -> str:
    # City servers sit behind a WAF that is friendlier to curl than to Python clients.
    return subprocess.run(["curl", "-sSfL", "--retry", "5", "--retry-delay", "10", *args],
                          check=True, capture_output=True, text=True).stdout


def scrape_links() -> list[str]:
    html = curl(INDEX_URL)
    urls = re.findall(r'href="(https://hfdapp\.houstontx\.gov/311/[^"]+\.txt)(?:\?[^"]*)?"', html)
    return list(dict.fromkeys(urls))  # dedupe, keep order


def is_header_only(url: str) -> bool:
    """True if the extract is a real, empty one: banner + column header, no data rows.
    Happens to MTD right after the city's job crosses midnight UTC on the last day of
    a month (it then extracts the new, still-empty month)."""
    body = curl(f"{url}?nocache=true")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    hdr = [i for i, ln in enumerate(lines) if ln.startswith("365 Case Number|")]
    return "Extract date-time" in body and len(hdr) == 1 and hdr[0] == len(lines) - 1


def head(url: str) -> tuple[int, str] | None:
    """(size, last_modified), or None if the extract is legitimately empty."""
    # Under load the server sometimes answers HEAD with a tiny placeholder; retry until sane.
    for attempt in range(6):
        hdrs = curl("-I", url)
        m_size = re.search(r"(?im)^content-length:\s*(\d+)", hdrs)
        m_lm = re.search(r"(?im)^last-modified:\s*(.+?)\s*$", hdrs)
        if m_size and m_lm and int(m_size.group(1)) > 1_000_000:
            return int(m_size.group(1)), m_lm.group(1)
        if m_size and int(m_size.group(1)) < 10_000 and is_header_only(url):
            return None
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"no plausible HEAD response for {url}")


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with MANIFEST.open() as f:
        return list(csv.DictReader(f, delimiter="\t"))


def append_manifest(row: dict) -> None:
    new = not MANIFEST.exists()
    with MANIFEST.open("a", newline="") as f:
        w = csv.DictWriter(f, FIELDS, delimiter="\t")
        if new:
            w.writeheader()
        w.writerow(row)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="regex; download only URLs matching it")
    ap.add_argument("--keep", type=int, help="keep only the newest N snapshots per source")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    urls = scrape_links()
    if args.only:
        urls = [u for u in urls if re.search(args.only, u)]
    print(f"{len(urls)} files listed on index page", flush=True)

    # minute resolution: the city regenerates MTD/YTD several times a day
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M")
    failures = 0
    for url in urls:
        stem = Path(url).stem
        h = head(url)
        if h is None:
            print(f"skip  {stem} (extract is header-only, no rows yet)", flush=True)
            continue
        size, lm = h
        have = [m for m in manifest if m["url"] == url and m["last_modified"] == lm
                and int(m["bytes"]) == size and (RAW / m["file"]).exists()]
        if have:
            print(f"skip  {stem} (unchanged, {have[-1]['file']})", flush=True)
            continue

        dest = RAW / f"{stem}_{stamp}.txt"
        part = dest.with_suffix(".txt.part")
        print(f"fetch {stem} ({size / 1e6:.0f} MB)", flush=True)
        # The city server resets long transfers (curl exit 56); resume from the .part file.
        for attempt in range(1, 11):
            rc = subprocess.run(["curl", "-sSfL", "--retry", "5", "--retry-all-errors",
                                 "--retry-delay", "10", "-C", "-", "-o", str(part),
                                 f"{url}?nocache=true"]).returncode
            if rc == 0:
                break
            print(f"  curl exit {rc}, attempt {attempt}; resuming at "
                  f"{part.stat().st_size if part.exists() else 0:,} bytes", flush=True)
            time.sleep(15)
        else:
            print(f"  giving up on {stem}", file=sys.stderr, flush=True)
            failures += 1
            continue
        if part.stat().st_size != size:
            print(f"  size mismatch: got {part.stat().st_size}, expected {size}; "
                  "file may have changed mid-download, rerun", file=sys.stderr)
            failures += 1
            continue
        part.rename(dest)
        row = {"url": url, "file": dest.name, "bytes": size, "last_modified": lm,
               "sha256": sha256(dest), "downloaded_at": dt.datetime.now().isoformat(timespec="seconds")}
        append_manifest(row)
        manifest.append({k: str(v) for k, v in row.items()})
        print(f"  ok  sha256={row['sha256'][:16]}…", flush=True)
    if args.keep:
        prune(args.keep)
    return 1 if failures else 0


def prune(keep: int) -> None:
    by_url = {}
    for m in load_manifest():
        by_url.setdefault(m["url"], []).append(m["file"])
    for files in by_url.values():
        present = list(dict.fromkeys(f for f in files if (RAW / f).exists()))
        for f in present[:-keep]:
            (RAW / f).unlink()
            print(f"prune {f}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
