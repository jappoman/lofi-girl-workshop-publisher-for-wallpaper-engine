#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Prepare and apply downloaded Lofi videos as Wallpaper Engine wallpapers."""

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List

ASSETS_DIR = Path("wallpaper_assets")
LINKS_FILE = Path("wallpaper_links.txt")
METADATA_FILE = Path("wallpaper_metadata.json")
WORKSHOP_OVERRIDES_FILE = Path("workshop_overrides.json")
BUILD_DIR = Path("wallpaper_engine_build")
MANIFEST_FILE = BUILD_DIR / "manifest.json"
OPEN_SCRIPT = BUILD_DIR / "open-wallpaper.ps1"
WORKSHOP_APP_ID = "431960"
WORKSHOP_DIR = BUILD_DIR / "workshop"
WORKSHOP_PROJECTS_DIR = WORKSHOP_DIR / "projects"
WORKSHOP_VDFS_DIR = WORKSHOP_DIR / "vdf"
WORKSHOP_PREVIEWS_DIR = WORKSHOP_DIR / "previews"
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
STEAMCMD_URL = "https://client-update.steamstatic.com/installer/steamcmd.zip"
DEFAULT_WALLPAPER_ENGINE_EXE = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe"
)
DEFAULT_STEAMCMD_DIR = Path("tools") / "steamcmd"
DEFAULT_STEAMCMD_EXE = DEFAULT_STEAMCMD_DIR / "steamcmd.exe"


def output_is_current(output_path: Path, source_path: Path) -> bool:
    """Return whether a generated artifact can be reused for this source."""
    try:
        return output_path.is_file() and output_path.stat().st_mtime >= source_path.stat().st_mtime
    except OSError:
        return False


def run_ffprobe(path: Path) -> Dict[str, object]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    payload = json.loads(proc.stdout or "{}")
    stream = (payload.get("streams") or [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": float(stream.get("duration") or 0),
    }


def iter_video_files(assets_dir: Path) -> Iterable[Path]:
    if not assets_dir.exists():
        return []
    return sorted(
        path
        for path in assets_dir.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )


def slug_from_path(path: Path) -> str:
    return path.stem.lower().replace(" ", "_")


def load_metadata(metadata_path: Path) -> Dict[str, Dict[str, object]]:
    if not metadata_path.exists():
        return {}
    records = json.loads(metadata_path.read_text(encoding="utf-8"))
    return {
        str(record.get("filename") or ""): record
        for record in records
        if record.get("filename")
    }


def title_from_filename(path: Path) -> str:
    text = path.stem.replace("_", " ").replace("-", " ")
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    return text.title()


def repair_text_encoding(value: object) -> str:
    text = str(value or "").strip()
    if any(marker in text for marker in ("â", "ð", "Â")):
        try:
            text = text.encode("latin1").decode("utf-8")
        except UnicodeError:
            pass
    return text


def clean_text(value: object) -> str:
    text = repair_text_encoding(value)
    text = text.replace("\\n", "\n")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_plain_text(value: object) -> str:
    text = clean_text(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalized_title_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def workshop_title_for(item: Dict[str, object]) -> str:
    title = clean_plain_text(item.get("title") or title_from_filename(Path(str(item["filename"]))))
    filename_title = title_from_filename(Path(str(item["filename"])))
    title_key = normalized_title_key(title)
    filename_key = normalized_title_key(filename_title)

    if filename_key and filename_key not in title_key:
        return f"Lofi Girl - {filename_title}: {title}"
    return f"Lofi Girl - {title}"


def collect_wallpapers(
    assets_dir: Path,
    min_width: int,
    min_aspect_ratio: float,
    max_aspect_ratio: float,
    metadata_path: Path,
    prune_invalid: bool = False,
) -> List[Dict[str, object]]:
    wallpapers = []
    skipped = []
    metadata_by_filename = load_metadata(metadata_path)

    for path in iter_video_files(assets_dir):
        try:
            meta = run_ffprobe(path)
        except Exception as exc:  # noqa: BLE001 - keep scanning other files
            skipped.append(
                {
                    "filename": path.name,
                    "path": str(path.resolve()),
                    "width": 0,
                    "height": 0,
                    "reason": f"ffprobe failed: {exc}",
                }
            )
            if prune_invalid:
                path.unlink()
            continue

        width = int(meta["width"])
        height = int(meta["height"])
        aspect_ratio = round(width / height, 4) if height else 0
        is_desktop = (
            width >= height
            and width >= min_width
            and min_aspect_ratio <= aspect_ratio <= max_aspect_ratio
        )

        metadata = metadata_by_filename.get(path.name, {})
        record = {
            "id": slug_from_path(path),
            "title": clean_plain_text(metadata.get("title") or title_from_filename(path)),
            "description": clean_text(metadata.get("description")),
            "slug": clean_plain_text(metadata.get("slug") or slug_from_path(path)),
            "tags": [clean_plain_text(tag) for tag in metadata.get("tags") or []],
            "url": str(metadata.get("url") or ""),
            "cover_url": str(metadata.get("cover_url") or ""),
            "project_url": str(metadata.get("project_url") or ""),
            "path": str(path.resolve()),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "width": width,
            "height": height,
            "duration": round(float(meta["duration"]), 3),
            "aspect_ratio": aspect_ratio,
        }

        if is_desktop:
            wallpapers.append(record)
        else:
            record["reason"] = "not landscape desktop-aspect video"
            skipped.append(record)
            if prune_invalid:
                path.unlink()

    if skipped:
        print("[i] Skipped non-desktop candidates:", file=sys.stderr)
        for item in skipped:
            print(
                f"[i]   -> {item['filename']} ({item['width']}x{item['height']}): "
                f"{item['reason']}",
                file=sys.stderr,
            )

    return wallpapers


def sync_source_files(wallpapers: List[Dict[str, object]], metadata_path: Path, links_path: Path) -> None:
    valid_filenames = {str(item["filename"]) for item in wallpapers}

    if metadata_path.exists():
        records = json.loads(metadata_path.read_text(encoding="utf-8"))
        records = [
            record for record in records if str(record.get("filename") or "") in valid_filenames
        ]
        metadata_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    urls = [str(item.get("url") or "") for item in wallpapers if item.get("url")]
    if urls:
        links_path.write_text("\n".join(sorted(urls)) + "\n", encoding="utf-8")


def write_open_script(
    open_script_path: Path, manifest_path: Path, wallpaper_engine_exe: Path
) -> None:
    script = f"""param(
    [string]$Name,
    [switch]$Random,
    [switch]$List,
    [int]$Monitor = -1
)

$ManifestPath = "{manifest_path.resolve()}"
$WallpaperEngine = "{wallpaper_engine_exe}"
$Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json

if ($List) {{
    $Manifest.wallpapers | Select-Object id,title,width,height,duration,path | Format-Table -AutoSize
    exit 0
}}

if ($Random) {{
    $Selected = $Manifest.wallpapers | Get-Random
}} elseif ($Name) {{
    $Selected = $Manifest.wallpapers | Where-Object {{ $_.id -eq $Name -or $_.filename -eq $Name }} | Select-Object -First 1
}} else {{
    $Selected = $Manifest.wallpapers | Select-Object -First 1
}}

if ($null -eq $Selected) {{
    throw "Wallpaper not found. Use -List to see available IDs."
}}

$ProjectPath = Join-Path (Split-Path -Parent $ManifestPath) ("workshop\\projects\\" + $Selected.id)
if (Test-Path -LiteralPath $ProjectPath) {{
    $WallpaperPath = $ProjectPath
}} else {{
    $WallpaperPath = $Selected.path
}}
$Args = @("-control", "openWallpaper", "-file", $WallpaperPath)
if ($Monitor -ge 0) {{
    $Args += @("-monitor", "$Monitor")
}}

& $WallpaperEngine @Args
"""
    open_script_path.write_text(script, encoding="utf-8")


def build(args: argparse.Namespace) -> int:
    wallpapers = collect_wallpapers(
        args.assets_dir,
        args.min_width,
        args.min_aspect_ratio,
        args.max_aspect_ratio,
        args.metadata,
        prune_invalid=args.prune_invalid,
    )
    if not wallpapers:
        print("[!] No desktop wallpaper candidates found.", file=sys.stderr)
        return 1

    args.build_dir.mkdir(parents=True, exist_ok=True)
    if args.prune_invalid:
        sync_source_files(wallpapers, args.metadata, args.links_file)

    open_script = args.build_dir / OPEN_SCRIPT.name
    manifest = {
        "source_dir": str(args.assets_dir.resolve()),
        "wallpaper_engine_exe": str(args.wallpaper_engine_exe),
        "count": len(wallpapers),
        "wallpapers": wallpapers,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_open_script(open_script, args.manifest, args.wallpaper_engine_exe)

    print(f"[i] Built manifest: {args.manifest.resolve()}")
    print(f"[i] Built opener:   {open_script.resolve()}")
    print(f"[i] Wallpapers:     {len(wallpapers)}")
    return 0


def vdf_escape(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("\\", "\\\\").replace('"', '\\"')


def steamcmd_text(value: object) -> str:
    text = str(value)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def write_vdf(path: Path, fields: Dict[str, object]) -> None:
    lines = ['"workshopitem"', "{"]
    for key, value in fields.items():
        lines.append(f'\t"{key}"\t\t"{vdf_escape(steamcmd_text(value))}"')
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_vdf_field(path: Path, field: str, default: str = "") -> str:
    if not path.exists():
        return default
    pattern = re.compile(rf'"{re.escape(field)}"\s+"([^"]*)"')
    match = pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else default


def generate_video_frame(video_path: Path, output_path: Path, vf: str = "") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    meta = run_ffprobe(video_path)
    duration = float(meta.get("duration") or 0)
    seek_seconds = max(1.0, min(8.0, duration * 0.2))
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{seek_seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
    ]
    if vf:
        command.extend(["-vf", vf])
    command.extend(["-q:v", "2", str(output_path)])
    subprocess.run(command, check=True)


def generate_preview(video_path: Path, preview_path: Path) -> None:
    generate_video_frame(
        video_path,
        preview_path,
        "crop='min(iw,ih)':'min(iw,ih)'",
    )


def generate_screenshot(video_path: Path, screenshot_path: Path) -> None:
    generate_video_frame(video_path, screenshot_path)


def download_cover_preview(url: str, output_path: Path) -> None:
    """Download a project cover and convert it to Steam's JPEG preview format."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="lofi-cover-", suffix=".source", dir=output_path.parent, delete=False
    ) as handle:
        source_path = Path(handle.name)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Lofi Wallpaper Engine pipeline)"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            source_path.write_bytes(response.read())
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
                "-vf",
                "crop='min(iw,ih)':'min(iw,ih)'",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(output_path),
            ],
            check=True,
        )
    finally:
        source_path.unlink(missing_ok=True)


def project_json_for(item: Dict[str, object]) -> Dict[str, object]:
    description = clean_text(
        item.get("description")
        or (
        f"{item['title']} animated wallpaper for Wallpaper Engine."
        )
    )

    return {
        "contentrating": "Everyone",
        "description": description,
        "file": item["filename"],
        "general": {
            "properties": {
                "schemecolor": {
                    "order": 0,
                    "text": "ui_browse_properties_scheme_color",
                    "type": "color",
                    "value": "0.12 0.12 0.12",
                }
            }
        },
        "preview": "preview.jpg",
        "ratingsex": "none",
        "ratingviolence": "none",
        "snapshotformat": -1,
        "snapshotoverlay": "",
        "tags": ["Unspecified"],
        "title": workshop_title_for(item),
        "type": "video",
        "version": 1,
    }


def stage_workshop_video(source_video: Path, project_dir: Path, item: Dict[str, object]) -> Path:
    """Stage a Workshop-compatible MP4 and return its project-relative path.

    Wallpaper Engine can open several formats locally, but Workshop video
    wallpapers should be MP4 files. Keeping this conversion at the packaging
    boundary also leaves the downloaded source assets untouched.
    """
    project_video = project_dir / f"{Path(str(item['filename'])).stem}.mp4"
    if output_is_current(project_video, source_video):
        print(f"[i]   -> Reusing cached conversion {project_video}")
        return project_video

    if source_video.suffix.lower() == ".mp4":
        shutil.copy2(source_video, project_video)
        return project_video

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_video),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(project_video),
        ],
        check=True,
    )
    return project_video


def load_workshop_overrides(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    overrides: Dict[str, Dict[str, str]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            overrides[str(key)] = {str(k): str(v) for k, v in value.items()}
    return overrides


def workshop_override_for(
    item: Dict[str, object], overrides: Dict[str, Dict[str, str]]
) -> Dict[str, str]:
    keys = [
        str(item.get("id") or ""),
        str(item.get("filename") or ""),
        str(item.get("slug") or ""),
    ]
    for key in keys:
        if key in overrides:
            return overrides[key]
    return {}


def prepare_workshop(args: argparse.Namespace) -> int:
    if not args.manifest.exists():
        build(args)

    manifest = load_manifest(args.manifest)
    wallpapers = list(manifest.get("wallpapers", []))
    if not wallpapers:
        print("[!] Manifest does not contain wallpapers.", file=sys.stderr)
        return 1

    args.workshop_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = args.workshop_dir / WORKSHOP_PROJECTS_DIR.name
    vdfs_dir = args.workshop_dir / WORKSHOP_VDFS_DIR.name
    previews_dir = args.workshop_dir / WORKSHOP_PREVIEWS_DIR.name
    projects_dir.mkdir(parents=True, exist_ok=True)
    vdfs_dir.mkdir(parents=True, exist_ok=True)
    previews_dir.mkdir(parents=True, exist_ok=True)
    overrides = load_workshop_overrides(args.workshop_overrides)

    for item in wallpapers:
        item_override = workshop_override_for(item, overrides)
        project_dir = projects_dir / item["id"]
        project_dir.mkdir(parents=True, exist_ok=True)

        source_video = Path(item["path"])
        project_video = stage_workshop_video(source_video, project_dir, item)

        preview_file = project_dir / "preview.jpg"
        preview_url = item_override.get("preview_url", "")
        if output_is_current(preview_file, source_video):
            print(f"[i]   -> Reusing cached preview {preview_file}")
        else:
            try:
                if preview_url:
                    download_cover_preview(preview_url, preview_file)
                else:
                    generate_preview(source_video, preview_file)
            except Exception as exc:  # noqa: BLE001 - video frame is a safe fallback
                print(f"[!] Cover download failed for {item['id']}: {exc}; using video frame.")
                generate_preview(source_video, preview_file)
        screenshot_file = project_dir / "screenshot.jpg"
        if not output_is_current(screenshot_file, source_video):
            generate_screenshot(source_video, screenshot_file)
        else:
            print(f"[i]   -> Reusing cached screenshot {screenshot_file}")

        project_json = project_json_for(item)
        project_json["file"] = project_video.name
        if item_override.get("title"):
            project_json["title"] = item_override["title"]
        if item_override.get("description"):
            project_json["description"] = item_override["description"]
        (project_dir / "project.json").write_text(
            json.dumps(project_json, indent=2), encoding="utf-8"
        )

        shared_preview = previews_dir / f"{item['id']}.jpg"
        shutil.copy2(preview_file, shared_preview)

        vdf_path = vdfs_dir / f"{item['id']}.vdf"
        published_file_id = (
            item_override.get("publishedfileid")
            or read_vdf_field(vdf_path, "publishedfileid", "0")
        )
        write_vdf(
            vdf_path,
            {
                "appid": WORKSHOP_APP_ID,
                "publishedfileid": published_file_id or "0",
                "contentfolder": str(project_dir.resolve()),
                "previewfile": str(shared_preview.resolve()),
                "visibility": str(args.visibility),
                "title": project_json["title"],
                "description": workshop_description_for(item, project_json["description"]),
                "changenote": "Initial upload",
            },
        )

    write_upload_script(args.workshop_dir, args.steamcmd_exe)
    print(f"[i] Prepared Workshop projects: {projects_dir.resolve()}")
    print(f"[i] Prepared VDF files:         {vdfs_dir.resolve()}")
    print(f"[i] Upload script:              {(args.workshop_dir / 'upload-all.ps1').resolve()}")
    print(f"[i] Items staged:               {len(wallpapers)}")
    return 0


def workshop_description_for(item: Dict[str, object], description: str) -> str:
    summary = clean_plain_text(steamcmd_text(description))
    if summary and summary[-1] not in ".!?":
        summary += "."
    parts = [summary]
    tags = item.get("tags") or []
    if tags:
        clean_tags = [clean_plain_text(steamcmd_text(tag)) for tag in tags]
        parts.append("Tags: " + ", ".join(tag for tag in clean_tags if tag) + ".")
    return " ".join(part for part in parts if part)


def ensure_steamcmd(args: argparse.Namespace) -> int:
    if args.steamcmd_exe.exists():
        print(f"[i] SteamCMD already available: {args.steamcmd_exe.resolve()}")
        return 0

    steamcmd_dir = args.steamcmd_exe.parent
    steamcmd_dir.mkdir(parents=True, exist_ok=True)
    zip_path = steamcmd_dir / "steamcmd.zip"

    print(f"[i] Downloading SteamCMD from {STEAMCMD_URL}")
    urllib.request.urlretrieve(STEAMCMD_URL, zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(steamcmd_dir)
    zip_path.unlink()

    if not args.steamcmd_exe.exists():
        raise FileNotFoundError(f"SteamCMD extraction did not create {args.steamcmd_exe}")

    print(f"[i] SteamCMD installed: {args.steamcmd_exe.resolve()}")
    return 0


def write_upload_script(workshop_dir: Path, steamcmd_exe: Path) -> None:
    vdfs_dir = workshop_dir / WORKSHOP_VDFS_DIR.name
    script = f"""param(
    [Parameter(Mandatory=$true)]
    [string]$SteamUser,
    [int]$DelaySeconds = 600,
    [string]$SteamCmd = "{steamcmd_exe}"
)

if (-not (Test-Path -LiteralPath $SteamCmd)) {{
    throw "SteamCMD not found at $SteamCmd. Install it or pass -SteamCmd."
}}

$Vdfs = Get-ChildItem -LiteralPath "{vdfs_dir.resolve()}" -Filter "*.vdf" | Sort-Object Name
foreach ($Vdf in $Vdfs) {{
    Write-Host "Uploading $($Vdf.Name)..."
    & $SteamCmd +login $SteamUser +workshop_build_item $Vdf.FullName +quit
    if ($LASTEXITCODE -ne 0) {{
        throw "SteamCMD failed for $($Vdf.FullName)"
    }}
    if ($DelaySeconds -gt 0 -and $Vdf.FullName -ne $Vdfs[-1].FullName) {{
        Start-Sleep -Seconds $DelaySeconds
    }}
}}
"""
    (workshop_dir / "upload-all.ps1").write_text(script, encoding="utf-8")


def upload_workshop(args: argparse.Namespace) -> int:
    ensure_steamcmd(args)
    if not args.steam_user:
        raise RuntimeError("Steam username required. Pass --steam-user.")

    script_path = args.workshop_dir / "upload-all.ps1"
    if not script_path.exists():
        prepare_workshop(args)

    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-SteamUser",
        args.steam_user,
        "-DelaySeconds",
        str(args.delay_seconds),
        "-SteamCmd",
        str(args.steamcmd_exe.resolve()),
    ]
    subprocess.run(command, check=True)
    return 0


def run_scraper(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "scrape_lofi_mp4.py",
        "--outfile",
        str(args.links_file),
        "--metadata-out",
        str(args.metadata),
        "--download-dir",
        str(args.assets_dir),
    ]
    subprocess.run(command, check=True)


def run_pipeline(args: argparse.Namespace) -> int:
    run_scraper(args)
    ensure_steamcmd(args)

    args.prune_invalid = True
    build_result = build(args)
    if build_result:
        return build_result

    prepare_result = prepare_workshop(args)
    if prepare_result:
        return prepare_result

    if args.upload:
        return upload_workshop(args)

    print("[i] Pipeline staged. Re-run with --upload --steam-user <user> to publish.")
    return 0


def load_manifest(manifest_path: Path) -> Dict[str, object]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def list_wallpapers(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    for item in manifest.get("wallpapers", []):
        print(
            f"{item['id']}\t{item['width']}x{item['height']}\t"
            f"{item['duration']}s\t{item['filename']}"
        )
    return 0


def select_wallpaper(manifest: Dict[str, object], name: str | None, random_pick: bool):
    wallpapers = list(manifest.get("wallpapers", []))
    if not wallpapers:
        raise RuntimeError("Manifest does not contain wallpapers.")

    if random_pick:
        return random.choice(wallpapers)

    if name:
        for item in wallpapers:
            if name in {item["id"], item["filename"], item["title"]}:
                return item
        raise RuntimeError(f"Wallpaper not found: {name}")

    return wallpapers[0]


def apply_wallpaper(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    wallpaper_engine_exe = Path(manifest["wallpaper_engine_exe"])
    if not wallpaper_engine_exe.exists():
        raise FileNotFoundError(f"Wallpaper Engine executable not found: {wallpaper_engine_exe}")

    selected = select_wallpaper(manifest, args.name, args.random)
    staged_project = args.manifest.parent / "workshop" / "projects" / str(selected["id"])
    wallpaper_path = staged_project if staged_project.exists() else Path(str(selected["path"]))
    command = [
        str(wallpaper_engine_exe),
        "-control",
        "openWallpaper",
        "-file",
        str(wallpaper_path),
    ]
    if args.monitor >= 0:
        command.extend(["-monitor", str(args.monitor)])

    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    if args.dry_run:
        return 0

    subprocess.run(command, check=True)
    print(f"[i] Applied wallpaper: {selected['filename']}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a Wallpaper Engine pipeline.")
    parser.set_defaults(func=build)
    parser.add_argument("--assets-dir", type=Path, default=ASSETS_DIR)
    parser.add_argument("--links-file", type=Path, default=LINKS_FILE)
    parser.add_argument("--metadata", type=Path, default=METADATA_FILE)
    parser.add_argument("--workshop-overrides", type=Path, default=WORKSHOP_OVERRIDES_FILE)
    parser.add_argument("--build-dir", type=Path, default=BUILD_DIR)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--min-width", type=int, default=1280)
    parser.add_argument("--min-aspect-ratio", type=float, default=1.3)
    parser.add_argument("--max-aspect-ratio", type=float, default=3.6)
    parser.add_argument(
        "--prune-invalid",
        action="store_true",
        help="delete corrupt or non-desktop files while building",
    )
    parser.add_argument(
        "--wallpaper-engine-exe",
        type=Path,
        default=DEFAULT_WALLPAPER_ENGINE_EXE,
    )
    parser.add_argument("--steamcmd-exe", type=Path, default=DEFAULT_STEAMCMD_EXE)
    parser.add_argument("--workshop-dir", type=Path, default=WORKSHOP_DIR)
    parser.add_argument(
        "--visibility",
        type=int,
        default=2,
        choices=[0, 1, 2, 3],
        help="Steam visibility: 0 public, 1 friends-only, 2 private, 3 unlisted",
    )
    parser.add_argument("--steam-user")
    parser.add_argument("--delay-seconds", type=int, default=600)

    subparsers = parser.add_subparsers(dest="command")

    build_parser = subparsers.add_parser("build")
    build_parser.set_defaults(func=build)

    list_parser = subparsers.add_parser("list")
    list_parser.set_defaults(func=list_wallpapers)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("name", nargs="?")
    apply_parser.add_argument("--random", action="store_true")
    apply_parser.add_argument("--monitor", type=int, default=-1)
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.set_defaults(func=apply_wallpaper)

    steamcmd_parser = subparsers.add_parser("ensure-steamcmd")
    steamcmd_parser.set_defaults(func=ensure_steamcmd)

    workshop_parser = subparsers.add_parser("prepare-workshop")
    workshop_parser.set_defaults(func=prepare_workshop)

    upload_parser = subparsers.add_parser("upload-workshop")
    upload_parser.set_defaults(func=upload_workshop)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--upload", action="store_true")
    run_parser.set_defaults(func=run_pipeline)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - command-line utility
        print(f"[!] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
