# Scrape LOFI MP4 Assets

`scrape_lofi_mp4.py` is a standalone utility that discovers and downloads the MP4 assets published on [lofistudio.com/projects](https://lofistudio.com/projects). The public site is rendered client-side, so the script locates the underlying CMS credentials embedded in the Next.js layout bundle, calls the CMS directly, and gathers every MP4 file it exposes.

## How It Works
- Fetches the `/projects` page, parses the layout chunk, and extracts the CMS base URL, access token, and allowed video extensions.
- Paginates through the `/api/projects` endpoint exposed by the CMS, collecting every published project payload.
- Scans each project for media entries, normalises the asset URLs, and filters them down to `.mp4` files.
- Prints the full list of MP4 URLs to stdout, saves them to `mp4_links.txt`, and downloads each file into `mp4_assets/`, avoiding existing files.

Progress messages are emitted to stderr, so piping stdout will still capture a clean list of URLs.

## Requirements
- Python 3.10 or newer.
- `requests` and `beautifulsoup4` Python packages.

Install the dependencies with:

```powershell
python -m pip install --upgrade requests beautifulsoup4
```

## Usage

```powershell
python scrape_lofi_mp4.py
```

The script behaves as follows:
- Prints the discovered CMS base URL and enumerates each project.
- Outputs a sorted list of MP4 URLs under an `=== MP4 LINKS ===` banner.
- Writes the same URLs to `mp4_links.txt`.
- Downloads every MP4 into `mp4_assets/`, skipping files that already exist and appending counters when duplicate filenames appear.

Running the script again is safe: previously downloaded files are skipped, and the URL list is regenerated.

## Customisation
You can tweak behaviour by editing the constants near the top of `scrape_lofi_mp4.py`:
- `OUTFILE`: change the destination file for the URL list.
- `DOWNLOAD_DIR`: select a different download directory.
- `HEADERS`: override the HTTP headers sent with each request.

## Notes
- The script relies on credentials embedded in the public site; if the layout bundle changes format, `discover_cms` may need updating.
- Respect the site's terms of service before downloading assets in bulk.

