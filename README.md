# Manga Downloader

A tool to monitor mangas on [mangapill.com](https://mangapill.com), detect new chapters, download images, and generate organized EPUBs with cover art and series metadata.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Terminal](https://img.shields.io/badge/Interface-Terminal-green?logo=gnometerminal&logoColor=white)

---

## Features

### Download & Conversion
- **Manga monitoring** — add URLs from mangapill.com and track new chapters automatically
- **Automatic download** — downloads chapter images and generates ready-to-read EPUBs
- **EPUB generation** — each chapter becomes a standalone EPUB file with full-page images
- **Cover art** — automatically downloads and caches the manga cover image, embedded in every EPUB
- **Series metadata** — EPUBs include Calibre series metadata and EPUB 3 `belongs-to-collection`, so chapters are grouped together on e-readers and Calibre
- **Smart chapter numbering** — zero-padded chapter numbers (e.g. `Chapter 0007 - Manga Name.epub`) for correct alphabetical sorting
- **Start from chapter** — when adding a manga, choose a starting chapter to skip already-read chapters

### Interactive Terminal Menu
- **Rich terminal UI** — beautiful, colorful interface powered by [Rich](https://github.com/Textualize/rich)
- **Manga management** — add, remove, and view monitored mangas
- **Download controls** — download new chapters, re-download all, or download a specific manga from a chosen chapter
- **EPUB browser** — view downloaded EPUBs grouped by manga
- **Delete EPUBs** — select and delete individual EPUB files
- **Manga info** — check chapter count, download status, and new chapters available

### Performance
- **Smart parallelism** — parallel downloads with rate limiting to avoid overloading the site
- **Cloudflare bypass** — uses `cloudscraper` to bypass Cloudflare protection automatically
- **Thread-local scrapers** — each thread has its own scraper instance for thread safety
- **Global rate limiter** — controls both concurrent connections and minimum interval between requests
- **Retry with backoff** — automatic retries with exponential backoff on failed requests

### Download Dashboard
- **Live dashboard** — in-place updating panel (powered by `rich.live.Live`) shows all mangas with progress bars, chapter counts, and current image download status, with no terminal scrolling
- **Pause / resume** — press `P + Enter` during downloads to pause, view progress summary, and choose to continue or stop
- **Graceful interruption** — `Ctrl+C` stops downloads and saves partial progress

### New Chapter Tracking
- **`[NEW]` folder prefix** — manga folders are prefixed with `[NEW] - ` when new chapters are downloaded, making it easy to spot updates at a glance
- **Automatic cleanup** — the `[NEW] - ` prefix is removed from folders that only contain `cover.jpg` (i.e. EPUBs have been moved to a Kindle or elsewhere)

### CLI Mode
- **Non-interactive mode** — works via terminal flags for automation (cron, scripts, etc.)
- **Download modes** — download only new chapters, re-download all, or reset history

---

## Installation Guide

### Prerequisites

- **Python 3.10+** installed ([download](https://www.python.org/downloads/))
- **pip** (Python package manager, already included with Python 3.10+)

### 1. Clone or copy the project

```bash
# Via Git
git clone <repository-url> manga-kindle
cd manga-kindle

# Or copy the project folder to your computer
```

### 2. Install dependencies

```bash
make install
# or
python3 -m pip install -r requirements.txt
```

> **Windows:** use `python` instead of `python3`.

### 3. Run the interactive menu

```bash
make run
# or
python3 manga_downloader.py
```

---

## Interactive Menu

```bash
python3 manga_downloader.py
```

The interactive menu provides all management features:

| Option | Action |
|---|---|
| `[1]` List mangas | View all monitored mangas with EPUB counts |
| `[2]` Add manga | Add a new manga URL (optionally set starting chapter) |
| `[3]` Remove manga | Remove a manga from the monitoring list |
| `[4]` Download new chapters | Download new chapters (all mangas or specific one) |
| `[5]` Re-download all | Re-download all chapters (ignores history) |
| `[6]` List EPUBs | View all downloaded EPUBs grouped by manga |
| `[7]` Delete EPUB | Select and delete an EPUB file |
| `[8]` Check manga info | View chapter count and download status |
| `[9]` Reset history | Reset download history (EPUBs on disk are kept) |
| `[0]` Exit | Exit the application |

---

## Non-interactive CLI

For automation (cron jobs, scripts, etc.):

### Download only new chapters

```bash
make download
# or
python3 manga_downloader.py --download
```

### Re-download all chapters (ignores history)

```bash
make download-all
# or
python3 manga_downloader.py --download --all
```

### Reset the download history

```bash
make reset
# or
python3 manga_downloader.py --reset
```

### Clean all downloads and state

```bash
make clean
```

---

## Project Structure

```
manga-kindle/
├── manga_downloader.py       # All logic: scraping, download, EPUB, and terminal UI
├── mangas.txt                # List of manga URLs to monitor
├── requirements.txt          # Python dependencies
├── .python-version           # Python version for pyenv
├── Makefile                  # Shortcut commands (make run, make download, etc.)
├── manga_kindle.spec         # PyInstaller spec for standalone executable
├── downloaded_chapters.json  # State of already downloaded chapters (auto-generated)
└── downloads/                # Generated EPUBs (auto-generated)
    ├── [NEW] - Manga Name/   # Folder with newly downloaded chapters
    │   ├── cover.jpg
    │   ├── Chapter 0001 - Manga Name.epub
    │   └── ...
    └── Manga Name/            # Folder after EPUBs have been moved
        └── cover.jpg
```

---

## Configuration

### Parallelism Settings

Adjustable at the top of `manga_downloader.py`:

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_IMAGES` | 4 | Images downloading simultaneously per chapter |
| `MAX_CONCURRENT_CHAPTERS` | 2 | Chapters processing simultaneously per manga |
| `MAX_CONCURRENT_MANGAS` | 2 | Mangas processing simultaneously |
| `GLOBAL_MAX_CONNECTIONS` | 6 | Global limit of simultaneous connections |
| `MIN_REQUEST_INTERVAL` | 0.3s | Minimum interval between requests |

> Don't increase these values too much to avoid being blocked by the site.

### EPUB Settings

| Setting | Description |
|---|---|
| Cover image | Automatically downloaded and cached in `downloads/<manga>/cover.jpg` |
| Series metadata | Calibre series + EPUB 3 `belongs-to-collection` for e-reader grouping |
| Author field | Set to the manga title so e-readers group all chapters together |
| Image quality | JPEG at 85% quality for a good balance of size and readability |
| Naming format | `Chapter XXXX - Manga Name.epub` (chapter number first for correct sorting) |

---

## Common Issues

### `pip install` gives a permission error

```bash
python3 -m pip install --user -r requirements.txt
```

### SSL / connection error

The site uses Cloudflare. `cloudscraper` bypasses this automatically, but if it persists, try updating:

```bash
python3 -m pip install --upgrade cloudscraper
```

### No chapters found

The site may have changed its HTML structure. Open an issue or check if the manga URL is valid by accessing it in your browser.

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP requests |
| `beautifulsoup4` | HTML parsing and scraping |
| `cloudscraper` | Cloudflare bypass |
| `Pillow` | Image processing |
| `EbookLib` | EPUB generation |
| `rich` | Terminal UI, live dashboard, tables, and panels |