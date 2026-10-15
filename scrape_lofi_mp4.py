#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Scrape animated wallpaper assets listed on https://studio.lofigirl.com/projects.

The site is a Next.js app that streams project data into the HTML. This script
extracts the main project videos from that payload and downloads them locally.
Gallery and behind-the-scenes clips can still be collected with --all-assets.
The old CMS discovery path is kept as a fallback for older deployments.
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://studio.lofigirl.com"
PROJECTS_URL = f"{BASE_URL}/projects"
OUTFILE = "wallpaper_links.txt"
METADATA_OUTFILE = "wallpaper_metadata.json"
DOWNLOAD_DIR = Path("wallpaper_assets")
VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}
EXTRA_PROJECT_SLUGS = ["halloween-lofi-2023"]
MAX_DETAIL_PROJECTS = 200
NON_PROJECT_SLUGS = {
    "collab",
    "compilation",
    "lofi-world",
    "music",
    "radio",
}
NON_WALLPAPER_VIDEO_HINTS = {
    "behind",
    "bts",
    "reels",
    "rough",
    "short",
    "teaser",
    "test",
}

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
VIDEO_URL_PATTERN = re.compile(
    r"https?:\\?/\\?/[^\\\"'<>\s]+?\.(?:mp4|webm|mov)(?:\?[^\\\"'<>\s]*)?",
    flags=re.I,
)
PROJECT_VIDEO_PATTERN = re.compile(
    r'\\"project_video\\":(?P<value>null|\{.*?\})(?:,\\"gallery\\")',
    flags=re.S,
)
ESCAPED_MEDIA_URL_PATTERN = re.compile(
    r'\\"url\\":\\"(?P<url>https?:\\?/\\?/[^\\"]+?\.(?:mp4|webm|mov)(?:\?[^\\"]*)?)\\"',
    flags=re.I,
)
PROJECT_START_PATTERN = re.compile(
    r'\{\\"id\\":\\"(?P<id>[^\\"]+)\\",\\"title\\":\\"(?:\\\\.|[^\\])*\\"'
    r',\\"url\\":'
)
ESCAPED_FIELD_PATTERN_TEMPLATE = r'\\"{}\\":\\"(?P<value>(?:\\\\.|[^\\])*)\\"'
ESCAPED_NULLABLE_FIELD_PATTERN_TEMPLATE = (
    r'\\"{}\\":(?P<value>null|\\"(?:\\\\.|[^\\])*\\"|(?:\$\d+))'
)
ESCAPED_IMAGE_URL_PATTERN = re.compile(
    r'\\"url\\":\\"(?P<url>https?:\\?/\\?/[^\\"]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\\"]*)?)\\"',
    flags=re.I,
)
ESCAPED_TITLE_PATTERN = re.compile(
    r'\\"title\\":\\"(?P<title>(?:\\\\.|[^\\])*)\\"',
    flags=re.S,
)


def fetch_text(
    session: requests.Session, url: str, timeout: int = 20, **kwargs
) -> str:
    resp = session.get(url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def discover_cms(session: requests.Session) -> Dict[str, object]:
    """Locate the CMS base URL, token, and video extensions from the layout chunk."""
    html = fetch_text(session, PROJECTS_URL)
    soup = BeautifulSoup(html, "html.parser")

    script_srcs = []
    for script in soup.find_all("script"):
        src = script.get("src")
        if src:
            script_srcs.append(urljoin(BASE_URL, src))

    if not script_srcs:
        raise RuntimeError("Could not locate Next.js script chunks.")

    match = None
    for script_src in script_srcs:
        bundle = fetch_text(session, script_src)
        match = CMS_CONFIG_PATTERN.search(bundle)
        if match:
            break

    if not match:
        raise RuntimeError("Failed to extract CMS credentials from script chunks.")

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


def normalize_media_url(raw_url: str) -> str:
    """Decode a media URL embedded in JSON or HTML."""
    return html.unescape(raw_url).replace("\\/", "/")


def decode_escaped_json_string(token: str) -> str:
    """Decode a string from the escaped JSON embedded in the HTML payload."""
    return json.loads('"' + token + '"')


def decode_nullable_escaped_json_string(token: str) -> str:
    """Decode a nullable escaped JSON string token."""
    if token == "null" or token.startswith("$"):
        return ""
    return decode_escaped_json_string(token[2:-2])


def is_video_url(url: str) -> bool:
    """Return whether the URL points to a supported video extension."""
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower().lstrip(".")
    return ext in VIDEO_EXTENSIONS


def find_escaped_field(segment: str, field: str) -> str:
    pattern = re.compile(ESCAPED_FIELD_PATTERN_TEMPLATE.format(re.escape(field)), re.S)
    match = pattern.search(segment)
    if not match:
        return ""
    return decode_escaped_json_string(match.group("value"))


def find_last_escaped_field(segment: str, field: str) -> str:
    pattern = re.compile(ESCAPED_FIELD_PATTERN_TEMPLATE.format(re.escape(field)), re.S)
    matches = list(pattern.finditer(segment))
    if not matches:
        return ""
    return decode_escaped_json_string(matches[-1].group("value"))


def find_nullable_escaped_field(segment: str, field: str) -> str:
    pattern = re.compile(
        ESCAPED_NULLABLE_FIELD_PATTERN_TEMPLATE.format(re.escape(field)), re.S
    )
    match = pattern.search(segment)
    if not match:
        return ""
    return decode_nullable_escaped_json_string(match.group("value"))


def find_project_description(segment: str) -> str:
    match = re.search(
        r'\\"description\\":(?P<value>null|\\"(?P<text>.*?)\\"),\\"tags\\"',
        segment,
        flags=re.S,
    )
    if not match:
        return ""
    if match.group("value") == "null":
        return ""
    return decode_escaped_json_string(match.group("text"))


def iter_project_segments(page_html: str) -> Iterable[str]:
    starts = [match.start() for match in PROJECT_START_PATTERN.finditer(page_html)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(page_html)
        yield page_html[start:end]


def discover_project_slugs_from_html(page_html: str) -> List[str]:
    slugs = []
    seen = set()
    for segment in iter_project_segments(page_html):
        if '\\"release_date\\":' not in segment:
            continue
        slug = find_last_escaped_field(segment, "slug")
        if slug and slug not in NON_PROJECT_SLUGS and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def collect_wallpaper_records_from_html(
    page_html: str, wanted_slug: str = ""
) -> List[Dict[str, object]]:
    """Extract main project videos and metadata from a Next.js payload."""
    records = []
    seen_urls = set()

    for segment in iter_project_segments(page_html):
        if '\\"featured\\":' not in segment or '\\"release_date\\":' not in segment:
            continue

        project_video_index = segment.find('\\"project_video\\":')
        gallery_index = segment.find(',\\"gallery\\"', project_video_index)
        if project_video_index < 0 or gallery_index < 0:
            continue

        video = segment[project_video_index:gallery_index]
        video_urls = []
        url_match = ESCAPED_MEDIA_URL_PATTERN.search(video)
        if url_match:
            video_urls.append(normalize_media_url(url_match.group("url")))
        elif '\\"project_video\\":null' in video:
            for raw_url in VIDEO_URL_PATTERN.findall(segment):
                url = normalize_media_url(raw_url)
                filename = filename_from_url(url).lower()
                if any(hint in filename for hint in NON_WALLPAPER_VIDEO_HINTS):
                    continue
                video_urls.append(url)

        slug = find_last_escaped_field(segment, "slug")
        if wanted_slug and slug != wanted_slug:
            continue

        cover_index = segment.find('\\"cover_image\\":')
        cover_end = segment.find(',\\"project_video\\"', cover_index)
        cover = segment[cover_index:cover_end] if cover_index >= 0 and cover_end >= 0 else ""
        cover_url = ""
        if cover and '\\"cover_image\\":null' not in cover:
            cover_match = ESCAPED_IMAGE_URL_PATTERN.search(cover)
            if cover_match:
                cover_url = normalize_media_url(cover_match.group("url"))

        tags_index = segment.find('\\"tags\\":[')
        tags_end = segment.find('],\\"cover_image\\"', tags_index)
        tags_segment = segment[tags_index:tags_end] if tags_index >= 0 and tags_end >= 0 else ""
        tags = [
            decode_escaped_json_string(tag_match.group("title"))
            for tag_match in ESCAPED_TITLE_PATTERN.finditer(tags_segment)
        ]

        for url in video_urls:
            if not is_video_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)

            records.append(
                {
                    "id": find_escaped_field(segment, "id"),
                    "title": find_escaped_field(segment, "title"),
                    "description": find_project_description(segment),
                    "slug": slug,
                    "tags": tags,
                    "url": url,
                    "filename": filename_from_url(url),
                    "cover_url": cover_url,
                    "project_url": f"{PROJECTS_URL}/{slug}" if slug else PROJECTS_URL,
                }
            )

    return sorted(records, key=lambda item: str(item["url"]))


def collect_wallpaper_records_from_page(session: requests.Session) -> List[Dict[str, object]]:
    """Extract main project videos and metadata from the current projects page."""
    page_html = fetch_text(session, PROJECTS_URL)
    records = collect_wallpaper_records_from_html(page_html)
    seen_urls = {str(record["url"]) for record in records}
    pending_slugs = discover_project_slugs_from_html(page_html)
    for slug in EXTRA_PROJECT_SLUGS:
        if slug not in pending_slugs:
            pending_slugs.append(slug)
    seen_slugs = set()

    while pending_slugs and len(seen_slugs) < MAX_DETAIL_PROJECTS:
        slug = pending_slugs.pop(0)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        try:
            detail_html = fetch_text(session, f"{PROJECTS_URL}/{slug}")
        except Exception as exc:  # noqa: BLE001 - keep the main scrape useful
            print(f"[!] Failed to fetch extra project {slug}: {exc}", file=sys.stderr)
            continue

        for discovered_slug in discover_project_slugs_from_html(detail_html):
            if discovered_slug not in seen_slugs and discovered_slug not in pending_slugs:
                pending_slugs.append(discovered_slug)

        for record in collect_wallpaper_records_from_html(detail_html, wanted_slug=slug):
            if str(record["url"]) in seen_urls:
                continue
            print(f"[i]   -> Deep project video found: {record['filename']}", file=sys.stderr)
            records.append(record)
            seen_urls.add(str(record["url"]))

    return sorted(records, key=lambda item: str(item["url"]))


def collect_wallpaper_urls_from_page(session: requests.Session) -> List[str]:
    """Extract main project videos from the current Next.js page payload."""
    page_html = fetch_text(session, PROJECTS_URL)
    urls = set()

    for match in PROJECT_VIDEO_PATTERN.finditer(page_html):
        value = match.group("value")
        if value == "null":
            continue

        url_match = ESCAPED_MEDIA_URL_PATTERN.search(value)
        if not url_match:
            continue

        normalized = normalize_media_url(url_match.group("url"))
        if is_video_url(normalized):
            urls.add(normalized)

    return sorted(urls)


def collect_all_video_urls_from_page(session: requests.Session) -> List[str]:
    """Extract every video asset URL embedded in the current Next.js page payload."""
    page_html = fetch_text(session, PROJECTS_URL)
    urls = set()

    for raw_url in VIDEO_URL_PATTERN.findall(page_html):
        normalized = normalize_media_url(raw_url)
        if is_video_url(normalized):
            urls.add(normalized)

    return sorted(urls)


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


def iter_media_objects(
    project: Dict[str, object], include_extras: bool = False
) -> Iterable[Dict[str, object]]:
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

    project_video = project.get("project_video")
    if isinstance(project_video, dict):
        yield from yield_if_media(project_video)

    if not include_extras:
        return

    for field in ("carousel_medias", "behind_scenes"):
        for item in project.get(field) or []:
            if isinstance(item, dict):
                yield from yield_if_media(item)


def collect_video_urls(
    project: Dict[str, object], cms_base: str, include_extras: bool = False
) -> List[str]:
    """Return all known video URLs for the given project."""
    urls: List[str] = []
    for media in iter_media_objects(project, include_extras=include_extras):
        ext = (media.get("ext") or "").lstrip(".").lower()
        if ext not in VIDEO_EXTENSIONS:
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

    if not Path(sanitized).suffix:
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


def download_video_assets(
    session: requests.Session, urls: Iterable[str], outdir: Path
) -> Dict[str, int]:
    """Download each video to disk, skipping files that already exist."""
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Lofi Studio animated wallpaper videos."
    )
    parser.add_argument(
        "--all-assets",
        action="store_true",
        help="download every video found in the page, including gallery/BTS clips",
    )
    parser.add_argument(
        "--outfile",
        default=OUTFILE,
        help=f"write discovered URLs to this file (default: {OUTFILE})",
    )
    parser.add_argument(
        "--metadata-out",
        default=METADATA_OUTFILE,
        help=f"write discovered wallpaper metadata to this file (default: {METADATA_OUTFILE})",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=DOWNLOAD_DIR,
        help=f"download files into this directory (default: {DOWNLOAD_DIR})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = requests.Session()
    session.headers.update(HEADERS)

    asset_kind = "all video assets" if args.all_assets else "wallpaper videos"
    print(f"[i] Extracting {asset_kind} from {PROJECTS_URL}", file=sys.stderr)
    try:
        if args.all_assets:
            results = collect_all_video_urls_from_page(session)
            records = []
        else:
            records = collect_wallpaper_records_from_page(session)
            results = [str(record["url"]) for record in records]
    except Exception as exc:  # noqa: BLE001 - surfaced to user
        print(f"[!] Failed to extract video URLs from page: {exc}", file=sys.stderr)
        results = []
        records = []

    if results:
        print(f"[i] Found {len(results)} {asset_kind} in page payload.", file=sys.stderr)
    else:
        print("[i] No embedded video URLs found; trying legacy CMS path.", file=sys.stderr)
        results = collect_video_urls_from_legacy_cms(
            session, include_extras=args.all_assets
        )

    if not results:
        print("[!] No video URLs discovered.", file=sys.stderr)
        return 1

    print("\n=== VIDEO LINKS ===")
    for url in results:
        print(url)

    with open(args.outfile, "w", encoding="utf-8") as fh:
        for url in results:
            fh.write(url + "\n")

    print(f"\n[i] Saved {len(results)} video URLs to {args.outfile}", file=sys.stderr)
    if records:
        with open(args.metadata_out, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(
            f"[i] Saved {len(records)} wallpaper metadata records to {args.metadata_out}",
            file=sys.stderr,
        )

    print(f"[i] Downloading video assets to {args.download_dir.resolve()}", file=sys.stderr)
    stats = download_video_assets(session, results, args.download_dir)
    print(
        "[i] Downloads complete: "
        f"{stats['downloaded']} new, {stats['skipped']} skipped, "
        f"{stats['failed']} failed",
        file=sys.stderr,
    )

    if stats["failed"] and stats["downloaded"] == 0:
        return 1

    return 0


def collect_video_urls_from_legacy_cms(
    session: requests.Session, include_extras: bool = False
) -> List[str]:
    """Collect video URLs using the older public-CMS credentials flow."""
    try:
        print(f"[i] Discovering CMS config from {PROJECTS_URL}", file=sys.stderr)
        cms = discover_cms(session)
    except Exception as exc:  # noqa: BLE001 - surfaced to user
        print(f"[!] Failed to discover CMS config: {exc}", file=sys.stderr)
        return []

    cms_base = cms["base"]
    cms_token = cms["token"]
    print(f"[i] CMS base: {cms_base}", file=sys.stderr)

    try:
        projects = fetch_projects(session, cms_base, cms_token)
    except Exception as exc:  # noqa: BLE001 - surfaced to user
        print(f"[!] Failed to fetch projects: {exc}", file=sys.stderr)
        return []

    if not projects:
        print("[!] No projects returned by CMS.", file=sys.stderr)
        return []

    print(f"[i] Found {len(projects)} project records.", file=sys.stderr)

    all_videos = set()
    for idx, project in enumerate(projects, 1):
        handle = project.get("handle") or project.get("slug") or f"id-{project.get('id')}"
        title = project.get("title") or handle
        print(f"[i] ({idx}/{len(projects)}) {title} ({handle})", file=sys.stderr)

        urls = collect_video_urls(project, cms_base, include_extras=include_extras)
        if urls:
            all_videos.update(urls)
            print(f"[i]   -> {len(urls)} video assets found", file=sys.stderr)
        else:
            print("[i]   -> no video assets", file=sys.stderr)

    return sorted(all_videos)


if __name__ == "__main__":
    raise SystemExit(main())
