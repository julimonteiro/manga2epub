# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install        # Install Python dependencies
make run            # Start interactive terminal menu
make download       # Download new chapters (non-interactive)
make download-all   # Re-download all chapters (ignores history)
make reset          # Reset download history
make clean          # Remove all downloads and state (downloads/ + downloaded_chapters.json)
make clean-build    # Remove build artifacts (build/, dist/, __pycache__)
make build          # Build standalone executable via PyInstaller
```

Direct invocations:
```bash
python3 manga_downloader.py                  # Interactive menu
python3 manga_downloader.py --download       # Non-interactive: new chapters only
python3 manga_downloader.py --download --all # Non-interactive: re-download all
python3 manga_downloader.py --reset          # Reset history
```

## Architecture

The entire application lives in a single file: `manga_downloader.py`. There are no modules or packages.

**Key data files:**
- `mangas.txt` — one mangapill.com URL per line (lines starting with `#` ignored)
- `downloaded_chapters.json` — state tracking which chapters have been downloaded (keyed by URL)
- `downloads/<Manga Name>/` — output EPUBs + `cover.jpg` per manga; prefixed with `[NEW] - ` when new chapters are present

**Internal structure of `manga_downloader.py` (top-to-bottom):**
1. Configuration constants (`BASE_URL`, parallelism settings, paths)
2. `RateLimiter` — semaphore + interval enforcement shared across all threads
3. `DownloadTracker` — thread-safe progress state feeding the Rich Live dashboard
4. Global threading events: `_cancel_event` (Ctrl+C), `_pause_event` / `_resume_event` (P+Enter pause)
5. Scraper helpers — `cloudscraper` instances are thread-local (`_scraper_local`) because cloudscraper is not thread-safe
6. State management (`load_state` / `save_state` with `state_lock` RLock)
7. File management helpers (`read_mangas_file`, `write_mangas_file`, `get_epubs`)
8. Scraping functions — parse mangapill.com HTML with BeautifulSoup
9. Download pipeline — parallel images → Pillow processing → EbookLib EPUB assembly
10. Interactive menu (`main_menu`) and non-interactive CLI (`main`)

**Parallelism model:** Three-level concurrency controlled by constants at the top of the file: `MAX_CONCURRENT_MANGAS` → `MAX_CONCURRENT_CHAPTERS` → `MAX_CONCURRENT_IMAGES`. All share a `GLOBAL_MAX_CONNECTIONS` semaphore via the global `rate_limiter`.

## Conventions

- All code, comments, docstrings, commit messages, and documentation must be in **English**.
- No emojis anywhere in code, docs, or commits.
- Python 3.10+ required (`.python-version` controls pyenv version).
- `requirements.txt` must list only packages actually imported. Update it when adding/removing imports.
- `README.md` must reflect the current state of the app. Update it when features, menu options, CLI flags, dependencies, or configuration defaults change. Do not document planned features.
