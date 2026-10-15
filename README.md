# Lofi Girl Wallpaper Engine Generator

End-to-end pipeline for turning the public Lofi Studio project videos into local Wallpaper Engine video projects and Steam Workshop upload descriptors.

The pipeline:
- scrapes `project_video` wallpapers from `https://studio.lofigirl.com/projects`
- saves real project metadata such as title, description, slug, tags and source URL
- downloads only desktop-style animated wallpaper candidates
- validates videos with `ffprobe` and prunes corrupt/non-desktop files
- prepares Wallpaper Engine `project.json` folders with previews
- downloads SteamCMD into `tools/steamcmd/`
- stages SteamCMD VDF files and a batch upload script

## Requirements

- Python 3.10+
- `requests` and `beautifulsoup4`
- `ffmpeg` / `ffprobe` available on `PATH`
- Wallpaper Engine installed through Steam

Install Python dependencies:

```powershell
python -m pip install --upgrade requests beautifulsoup4
```

## One Command Pipeline

Run the full scrape -> validate -> stage flow:

```powershell
python wallpaper_engine_pipeline.py run
```

This writes:
- `wallpaper_assets/`: downloaded valid videos
- `wallpaper_links.txt`: valid wallpaper URLs
- `wallpaper_metadata.json`: title/description/source metadata from the site
- `wallpaper_engine_build/manifest.json`: local validated manifest
- `wallpaper_engine_build/workshop/projects/`: one Wallpaper Engine project per wallpaper
- `wallpaper_engine_build/workshop/vdf/`: one SteamCMD descriptor per wallpaper
- `wallpaper_engine_build/workshop/upload-all.ps1`: sequential uploader
- `tools/steamcmd/steamcmd.exe`: local SteamCMD downloaded from Valve

Generated Workshop items default to `visibility = 2` (private).

## Upload To Steam Workshop

After staging, upload all private Workshop items:

```powershell
python wallpaper_engine_pipeline.py --steam-user YOUR_STEAM_USERNAME run --upload
```

For this repo, there is also a shortcut configured for `jappoman`:

```powershell
.\publish-workshop.ps1
```

This shortcut stages the projects, then uploads missing Workshop items and updates already published items when their staged title, description, preview, or project content changed. Existing `publishedfileid` values are preserved, so rerunning the shortcut updates existing items instead of creating duplicates. Successful upload signatures are stored in `wallpaper_engine_build/workshop/upload-state.json`.

The uploader waits between items (`600s` by default) and retries Steam rate-limit failures with a minimum 10-minute backoff. Steam does not publish a fixed cooldown value; this conservative delay avoids immediate repeated attempts. To publish or update only one wallpaper for a quick test:

```powershell
.\publish-workshop.ps1 -Only bossa
```

To stage/upload as public instead of private:

```powershell
.\publish-workshop.ps1 -Public
```

Or use the generated PowerShell script directly:

```powershell
.\wallpaper_engine_build\workshop\upload-all.ps1 -SteamUser YOUR_STEAM_USERNAME -SteamCmd ".\tools\steamcmd\steamcmd.exe"
```

SteamCMD may prompt for Steam Guard. The shortcut waits between new items (`600s` by default) to reduce friction with Steam rate limits.

The Bossa item uses the project's dedicated `Bossa_Lofi.webp` cover as its Workshop preview and converts it to a compressed JPEG during staging.

## Useful Commands

Download/update SteamCMD only:

```powershell
python wallpaper_engine_pipeline.py ensure-steamcmd
```

Scrape only:

```powershell
python scrape_lofi_mp4.py
```

Build local manifest only:

```powershell
python wallpaper_engine_pipeline.py --prune-invalid build
```

List staged wallpapers:

```powershell
python wallpaper_engine_pipeline.py list
```

Apply a local wallpaper through Wallpaper Engine:

```powershell
python wallpaper_engine_pipeline.py apply bossa
python wallpaper_engine_pipeline.py apply --random
```

Prepare Workshop projects without uploading:

```powershell
python wallpaper_engine_pipeline.py prepare-workshop
```

## Visibility And Rights

The generated VDF files are private by default. Change visibility to public only if you have the rights or permission to publish the source media on Steam Workshop.

Steam visibility values:
- `0`: Public
- `1`: Friends-only
- `2`: Private
- `3`: Unlisted

Example public staging:

```powershell
python wallpaper_engine_pipeline.py --visibility 0 prepare-workshop
```

## Notes

- The scraper prefers metadata embedded in the current Next.js page payload and keeps a legacy CMS fallback for older deployments.
- Three currently exposed CDN files fail `ffprobe` validation and are automatically pruned from the staged set.
- VDF text is made ASCII-safe for SteamCMD; local Wallpaper Engine `project.json` files keep UTF-8 descriptions.
