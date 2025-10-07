#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Scrape mp4 assets listed on https://lofistudio.com/projects.

The site is a Next.js app that renders projects client-side. The real list of
projects (and their media URLs) lives behind the CMS exposed at
https://admin.lofistudio.com. The public site embeds that CMS base URL and an
access token inside the layout bundle, so this script extracts the credentials
from the bundle, queries the CMS directly, and collects all mp4 assets.
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://lofistudio.com"
PROJECTS_URL = f"{BASE_URL}/projects"
OUTFILE = "mp4_links.txt"
DOWNLOAD_DIR = Path("mp4_assets")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}

# Example snippet:
# let a="https://admin.lofistudio.com",i="TOKEN",n=["mov","mp4","webm"],s=18
CMS_CONFIG_PATTERN = re.compile(
    r'let a="(?P<base>https?://[^"]+)",i="(?P<token>[^"]+)",n=\[(?P<exts>[^\]]+)\],s=',
    flags=re.S,
)


def fetch_text(
    session: requests.Session, url: str, timeout: int = 20, **kwargs
) -> str:
    resp = session.get(url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.text


def discover_cms(session: requests.Session) -> Dict[str, object]:
    """Locate the CMS base URL, token, and video extensions from the layout chunk."""
    html = fetch_text(session, PROJECTS_URL)
    soup = BeautifulSoup(html, "html.parser")

    layout_src = None
    for script in soup.find_all("script"):
        src = script.get("src")
        if src and "/app/layout-" in src:
            layout_src = urljoin(BASE_URL, src)
            break

    if not layout_src:
        raise RuntimeError("Could not locate Next.js layout chunk with CMS config.")

    bundle = fetch_text(session, layout_src)
    match = CMS_CONFIG_PATTERN.search(bundle)
    if not match:
        raise RuntimeError("Failed to extract CMS credentials from layout chunk.")

    base = match.group("base").rstrip("/")
    token = match.group("token")
    exts_raw = "[" + match.group("exts") + "]"

    try:
        extensions = json.loads(exts_raw)
    except json.JSONDecodeError:
        extensions = ["mp4"]

    cleaned_exts = {
        str(ext).lower().lstrip(".") for ext in extensions if isinstance(ext, str)
    }

    return {"base": base, "token": token, "extensions": cleaned_exts}


def fetch_projects(
    session: requests.Session, cms_base: str, token: str, page_size: int = 100
) -> List[Dict[str, object]]:
    """Download all published projects from the CMS."""
    projects: List[Dict[str, object]] = []
    page = 1
    headers = {"Authorization": f"Bearer {token}"}

    while True:
        params = {
            "status": "published",
            "pagination[page]": page,
            "pagination[pageSize]": page_size,
            "populate": "*",
        }
        resp = session.get(
            f"{cms_base}/api/projects",
            params=params,
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()

        data = payload.get("data") or []
        projects.extend(data)

        pagination = (payload.get("meta") or {}).get("pagination") or {}
        if page >= pagination.get("pageCount", page):
            break
        page += 1

    return projects


def iter_media_objects(project: Dict[str, object]) -> Iterable[Dict[str, object]]:
    """Yield all media dictionaries attached to a project."""
    seen_ids = set()

    def yield_if_media(candidate):
        if not isinstance(candidate, dict):
            return
        media_id = candidate.get("documentId") or candidate.get("id")
        if media_id and media_id in seen_ids:
            return
        if media_id:
            seen_ids.add(media_id)
        yield candidate

    media = project.get("media")
    if isinstance(media, dict):
        yield from yield_if_media(media)

    for field in ("carousel_medias", "behind_scenes"):
        for item in project.get(field) or []:
            if isinstance(item, dict):
                yield from yield_if_media(item)


def collect_mp4_urls(project: Dict[str, object], cms_base: str) -> List[str]:
    """Return all mp4 URLs for the given project."""
    urls: List[str] = []
    for media in iter_media_objects(project):
        ext = (media.get("ext") or "").lstrip(".").lower()
        if ext != "mp4":
            continue

        raw_url = media.get("url")
        if not raw_url:
            continue

        absolute = urljoin(cms_base + "/", raw_url)
        urls.append(absolute)

    return urls


def filename_from_url(url: str) -> str:
    """Derive a filesystem-safe filename for the given media URL."""
    parsed = urlparse(url)
    raw_name = Path(parsed.path).name
    if not raw_name:
        raw_name = "asset.mp4"

    decoded = unquote(raw_name)
    sanitized = re.sub(r"[^\w.\-]", "_", decoded)
    sanitized = sanitized.strip("._") or "asset.mp4"

    if not sanitized.lower().endswith(".mp4"):
        sanitized += ".mp4"

    return sanitized


def resolve_destination(
    filename: str, used_names: Set[str], outdir: Path
) -> Path:
    """Ensure each download path is unique to avoid overwriting files."""
    stem = Path(filename).stem or "asset"
    ext = Path(filename).suffix or ".mp4"

    candidate = filename
    counter = 1
    while candidate in used_names or (outdir / candidate).exists():
        candidate = f"{stem}-{counter}{ext}"
        counter += 1

    used_names.add(candidate)
    return outdir / candidate


def download_mp4_assets(
    session: requests.Session, urls: Iterable[str], outdir: Path
) -> Dict[str, int]:
    """Download each MP4 to disk, skipping files that already exist."""
    outdir.mkdir(parents=True, exist_ok=True)
    used_names: Set[str] = set()
    stats = {"downloaded": 0, "skipped": 0, "failed": 0}

    for url in urls:
        filename = filename_from_url(url)
        base_destination = outdir / filename

        if filename not in used_names and base_destination.exists():
            print(f"[i]   -> Skipping existing file {base_destination}", file=sys.stderr)
            used_names.add(filename)
            stats["skipped"] += 1
            continue

        destination = resolve_destination(filename, used_names, outdir)

        if destination.exists():
            print(f"[i]   -> Skipping existing file {destination}", file=sys.stderr)
            stats["skipped"] += 1
            continue

        tmp_path = destination.with_suffix(destination.suffix + ".part")
        print(f"[i]   -> Downloading {destination.name}", file=sys.stderr)

        try:
            with session.get(url, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with tmp_path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
            tmp_path.replace(destination)
            stats["downloaded"] += 1
        except Exception as exc:  # noqa: BLE001 - surfaced to user
            if tmp_path.exists():
                tmp_path.unlink()
            print(f"[!]   -> Failed to download {url}: {exc}", file=sys.stderr)
            stats["failed"] += 1

    return stats


def main() -> int:
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        print(f"[i] Discovering CMS config from {PROJECTS_URL}", file=sys.stderr)
        cms = discover_cms(session)
    except Exception as exc:  # noqa: BLE001 - surfaced to user
        print(f"[!] Failed to discover CMS config: {exc}", file=sys.stderr)
        return 1

    cms_base = cms["base"]
    cms_token = cms["token"]
    print(f"[i] CMS base: {cms_base}", file=sys.stderr)

    try:
        projects = fetch_projects(session, cms_base, cms_token)
    except Exception as exc:  # noqa: BLE001 - surfaced to user
        print(f"[!] Failed to fetch projects: {exc}", file=sys.stderr)
        return 1

    if not projects:
        print("[!] No projects returned by CMS.", file=sys.stderr)
        return 1

    print(f"[i] Found {len(projects)} project records.", file=sys.stderr)

    all_mp4 = set()
    for idx, project in enumerate(projects, 1):
        handle = project.get("handle") or project.get("slug") or f"id-{project.get('id')}"
        title = project.get("title") or handle
        print(f"[i] ({idx}/{len(projects)}) {title} ({handle})", file=sys.stderr)

        urls = collect_mp4_urls(project, cms_base)
        if urls:
            all_mp4.update(urls)
            print(f"[i]   -> {len(urls)} mp4 found", file=sys.stderr)
        else:
            print("[i]   -> no mp4 assets", file=sys.stderr)

    if not all_mp4:
        print("[!] No mp4 URLs discovered.", file=sys.stderr)
        return 1

    results = sorted(all_mp4)
    print("\n=== MP4 LINKS ===")
    for url in results:
        print(url)

    with open(OUTFILE, "w", encoding="utf-8") as fh:
        for url in results:
            fh.write(url + "\n")

    print(f"\n[i] Saved {len(results)} mp4 URLs to {OUTFILE}", file=sys.stderr)

    print(f"[i] Downloading MP4 assets to {DOWNLOAD_DIR.resolve()}", file=sys.stderr)
    stats = download_mp4_assets(session, results, DOWNLOAD_DIR)
    print(
        "[i] Downloads complete: "
        f"{stats['downloaded']} new, {stats['skipped']} skipped, "
        f"{stats['failed']} failed",
        file=sys.stderr,
    )

    if stats["failed"] and stats["downloaded"] == 0:
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
