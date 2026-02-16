#!/usr/bin/env python3
"""
Manga Downloader — Interactive terminal application.

Monitors mangas on mangapill.com, detects new chapters,
downloads images, and generates EPUBs organized by manga/chapter.

Usage:
    python3 manga_downloader.py            # Interactive menu
    python3 manga_downloader.py --download # Download new chapters and exit
    python3 manga_downloader.py --all      # Re-download all chapters and exit
    python3 manga_downloader.py --reset    # Reset download history and exit
"""

import os
import re
import json
import time
import sys
import threading
import argparse
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed

import cloudscraper
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from ebooklib import epub
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.live import Live
from rich import box

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
BASE_URL = "https://mangapill.com"
MANGAS_FILE = "mangas.txt"
STATE_FILE = "downloaded_chapters.json"
OUTPUT_DIR = "downloads"
MAX_RETRIES = 3
NEW_FOLDER_PREFIX = "[NEW] - "

# ── Parallelism and rate limiting control ──
MAX_CONCURRENT_IMAGES = 4      # images downloading simultaneously per chapter
MAX_CONCURRENT_CHAPTERS = 2    # chapters processing simultaneously per manga
MAX_CONCURRENT_MANGAS = 2      # mangas processing simultaneously
GLOBAL_MAX_CONNECTIONS = 6     # global limit of simultaneous connections to the site
MIN_REQUEST_INTERVAL = 0.3     # minimum interval (seconds) between requests

# ── Rich console ──
console = Console()


def clear_screen():
    """Clear visible screen and scrollback buffer to prevent visual artifacts."""
    console.clear()
    # ESC[3J clears the scrollback buffer (supported by most modern terminals)
    print("\033[3J", end="", flush=True)


# ──────────────────────────────────────────────
# Rate Limiter
# ──────────────────────────────────────────────
class RateLimiter:
    """
    Controls the request rate to the server.
    Ensures a minimum interval between requests and limits simultaneous connections.
    """

    def __init__(self, max_concurrent: int, min_interval: float):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request_time = 0.0

    def acquire(self):
        """Acquire permission to make a request. Returns False if cancelled."""
        # Try to acquire semaphore with short polling to stay cancel-responsive
        while not self._semaphore.acquire(timeout=0.1):
            if _cancel_event.is_set():
                return False
        if _cancel_event.is_set():
            self._semaphore.release()
            return False
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                remaining = self._min_interval - elapsed
                # Sleep in small chunks to stay cancel-responsive
                while remaining > 0 and not _cancel_event.is_set():
                    time.sleep(min(remaining, 0.1))
                    remaining -= 0.1
            self._last_request_time = time.monotonic()
        return True

    def release(self):
        """Release permission after the request."""
        self._semaphore.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


# Global rate limiter — shared by all threads
rate_limiter = RateLimiter(GLOBAL_MAX_CONNECTIONS, MIN_REQUEST_INTERVAL)

# Lock for thread-safe state saving (RLock allows re-entrant acquisition)
state_lock = threading.RLock()

# Cancellation signal — set on Ctrl+C to gracefully stop downloads
_cancel_event = threading.Event()

# Pause/resume signals — for "P + Enter" pause during downloads
_pause_event = threading.Event()
_resume_event = threading.Event()
_resume_event.set()  # initially unblocked


class DownloadTracker:
    """
    Thread-safe progress tracker for the Live download dashboard.
    Worker threads update progress; the main thread reads it to render.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._mangas: dict[str, dict] = {}
        self._order: list[str] = []

    def add_manga(self, url: str, title: str, status: str = "checking"):
        """Register a manga in the tracker."""
        with self._lock:
            self._mangas[url] = {
                "title": title,
                "status": status,
                "total_ch": 0,
                "done_ch": 0,
                "current_ch": "",
                "total_img": 0,
                "done_img": 0,
            }
            if url not in self._order:
                self._order.append(url)

    def update(self, url: str, **kwargs):
        """Update fields for a manga (e.g. status, done_ch, current_ch, ...)."""
        with self._lock:
            if url in self._mangas:
                self._mangas[url].update(kwargs)

    def increment(self, url: str, field: str, amount: int = 1):
        """Thread-safe increment of a numeric field."""
        with self._lock:
            if url in self._mangas:
                self._mangas[url][field] += amount

    def get_totals(self) -> tuple[int, int]:
        """Return (total_done_chapters, total_chapters) across all mangas."""
        with self._lock:
            done = sum(m["done_ch"] for m in self._mangas.values())
            total = sum(m["total_ch"] for m in self._mangas.values())
            return done, total

    def _make_bar(self, done: int, total: int, width: int = 20) -> str:
        """Build a text-based progress bar."""
        if total <= 0:
            return "[dim]" + "━" * width + "[/]"
        filled = int(width * done / total)
        return "━" * filled + "[dim]" + "━" * (width - filled) + "[/]"

    def build_panel(self, show_hint: bool = True) -> Panel:
        """Render the dashboard panel from current state."""
        from rich.text import Text

        with self._lock:
            table = Table(
                show_header=False,
                show_edge=False,
                box=None,
                pad_edge=False,
                padding=(0, 1),
                expand=False,
            )
            table.add_column("title", no_wrap=True, max_width=30, overflow="ellipsis")
            table.add_column("bar", no_wrap=True, min_width=20)
            table.add_column("count", no_wrap=True, justify="right", min_width=8)
            table.add_column("status", no_wrap=True, min_width=18)

            for url in self._order:
                m = self._mangas[url]
                title = Text(m["title"], style="bold")
                bar = Text.from_markup(self._make_bar(m["done_ch"], m["total_ch"]))

                if m["total_ch"] > 0:
                    count = Text.from_markup(f"[dim]{m['done_ch']}/{m['total_ch']} ch[/]")
                else:
                    count = Text("")

                status = m["status"]
                if status == "up_to_date":
                    status_text = Text("up to date", style="green")
                elif status == "checking":
                    status_text = Text("checking...", style="dim")
                elif status == "downloading":
                    img_part = ""
                    if m["total_img"] > 0:
                        img_part = f"  {m['done_img']}/{m['total_img']} img"
                    status_text = Text.from_markup(
                        f"[cyan]Ch. {m['current_ch']}[/][dim]{img_part}[/]"
                    )
                elif status == "done":
                    status_text = Text("done", style="green bold")
                elif status == "waiting":
                    status_text = Text("waiting...", style="dim")
                elif status == "error":
                    status_text = Text("error", style="red")
                else:
                    status_text = Text(status, style="dim")

                table.add_row(title, bar, count, status_text)

            if not self._order:
                table.add_row(
                    Text("Checking mangas...", style="dim"),
                    Text(""), Text(""), Text(""),
                )

        subtitle = "[dim]P + Enter to pause · Ctrl+C to stop[/]" if show_hint else None
        return Panel(
            table,
            title="[bold]  Downloading  [/]",
            subtitle=subtitle,
            border_style="cyan",
            padding=(1, 2),
            expand=False,
        )


def _input_listener():
    """
    Daemon thread that listens for 'p' + Enter to signal a pause.
    The main thread (inside the Live loop) handles the prompt.
    """
    while not _cancel_event.is_set():
        try:
            line = input()
        except (EOFError, KeyboardInterrupt, OSError):
            return

        if line.strip().lower() == "p" and not _pause_event.is_set():
            _resume_event.clear()
            _pause_event.set()


# ──────────────────────────────────────────────
# Scraper
# ──────────────────────────────────────────────
def create_scraper():
    """Create a scraper that bypasses Cloudflare protection."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
    return scraper


# Pool of scrapers for use in threads (cloudscraper is not thread-safe)
_scraper_local = threading.local()


def get_scraper():
    """Return a per-thread scraper (thread-local)."""
    if not hasattr(_scraper_local, "scraper"):
        _scraper_local.scraper = create_scraper()
    return _scraper_local.scraper


# ──────────────────────────────────────────────
# State management
# ──────────────────────────────────────────────
def load_state() -> dict:
    """Load the state of already downloaded chapters."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Save the state of already downloaded chapters (thread-safe)."""
    with state_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)


# ──────────────────────────────────────────────
# File management
# ──────────────────────────────────────────────
def read_mangas_file() -> list[str]:
    """Read URLs from mangas.txt."""
    if not os.path.exists(MANGAS_FILE):
        return []
    urls = []
    with open(MANGAS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def write_mangas_file(urls: list[str]):
    """Write URLs to mangas.txt."""
    with open(MANGAS_FILE, "w", encoding="utf-8") as f:
        f.write("# List of manga URLs to monitor (one per line)\n")
        f.write("# Lines starting with # are ignored\n")
        for url in urls:
            f.write(url + "\n")


def get_epubs() -> list[dict]:
    """List all downloaded EPUBs."""
    epubs = []
    downloads_dir = Path(OUTPUT_DIR)
    if not downloads_dir.exists():
        return epubs

    for manga_dir in sorted(downloads_dir.iterdir()):
        if not manga_dir.is_dir():
            continue
        for epub_file in sorted(manga_dir.iterdir()):
            if epub_file.suffix.lower() == ".epub":
                stat = epub_file.stat()
                epubs.append({
                    "manga": manga_dir.name,
                    "filename": epub_file.name,
                    "path": str(epub_file.relative_to(downloads_dir)),
                    "size_mb": round(stat.st_size / (1024 * 1024), 1),
                })
    return epubs


def get_manga_state() -> list[dict]:
    """Return the list of mangas with state information."""
    urls = read_mangas_file()
    state = load_state()
    mangas = []
    for url in urls:
        info = state.get(url, {})
        title = info.get("title", "")
        safe_title = sanitize_filename(title) if title else ""
        # Check both base and "[NEW] - " prefixed folders
        epub_count = 0
        for dir_name in [safe_title, f"{NEW_FOLDER_PREFIX}{safe_title}"]:
            epub_dir = Path(OUTPUT_DIR) / dir_name
            if epub_dir.exists():
                epub_count += len(list(epub_dir.glob("*.epub")))
        mangas.append({
            "url": url,
            "title": title or "(not checked yet)",
            "epub_count": epub_count,
        })
    return mangas


# ──────────────────────────────────────────────
# Utility functions
# ──────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    """Remove invalid characters for file names."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def format_chapter_num(chapter_num: str, pad: int = 4) -> str:
    """
    Zero-pad chapter numbers for proper alphabetical sorting.
    '7' → '0007', '83' → '0083', '12.5' → '0012.5'
    """
    try:
        num = float(chapter_num)
        if num == int(num):
            return str(int(num)).zfill(pad)
        # Decimal chapter (e.g. 12.5) — pad the integer part
        int_part, dec_part = chapter_num.split(".", 1)
        return f"{int_part.zfill(pad)}.{dec_part}"
    except (ValueError, AttributeError):
        return chapter_num


def parse_chapter_num(num_str: str) -> float:
    """Convert chapter string to float for comparison."""
    try:
        return float(num_str)
    except (ValueError, TypeError):
        return 0.0


def get_manga_dir(safe_title: str) -> str:
    """Return the manga download directory, preferring the '[NEW] - ' prefixed variant if it exists."""
    new_dir = os.path.join(OUTPUT_DIR, f"{NEW_FOLDER_PREFIX}{safe_title}")
    if os.path.exists(new_dir):
        return new_dir
    return os.path.join(OUTPUT_DIR, safe_title)


def cleanup_new_folders():
    """
    Remove '[NEW] - ' prefix from download folders that only contain
    the cover image (no EPUBs). Called before downloads to clean up
    folders whose EPUBs have already been transferred.
    """
    downloads_dir = Path(OUTPUT_DIR)
    if not downloads_dir.exists():
        return

    prefix_len = len(NEW_FOLDER_PREFIX)

    for folder in sorted(downloads_dir.iterdir()):
        if not folder.is_dir() or not folder.name.startswith(NEW_FOLDER_PREFIX):
            continue

        has_epub = any(f.suffix.lower() == ".epub" for f in folder.iterdir())
        if has_epub:
            continue

        base_name = folder.name[prefix_len:]
        target = downloads_dir / base_name

        if target.exists():
            for item in list(folder.iterdir()):
                dest = target / item.name
                if not dest.exists():
                    item.rename(dest)
                elif item.is_file():
                    item.unlink()
            try:
                folder.rmdir()
            except OSError:
                pass
        else:
            folder.rename(target)

        console.print(f"  [dim]cleaned up:[/] {base_name}")


def mark_folder_new(safe_title: str):
    """Add '[NEW] - ' prefix to a manga folder if it doesn't already have it."""
    downloads_dir = Path(OUTPUT_DIR)
    base_folder = downloads_dir / safe_title
    new_folder = downloads_dir / f"{NEW_FOLDER_PREFIX}{safe_title}"

    if base_folder.exists() and not new_folder.exists():
        base_folder.rename(new_folder)


# ──────────────────────────────────────────────
# Scraping functions
# ──────────────────────────────────────────────
def fetch_page(url: str) -> BeautifulSoup | None:
    """Make an HTTP request with rate limiting and return the parsed BeautifulSoup."""
    scraper = get_scraper()
    for attempt in range(MAX_RETRIES):
        if _cancel_event.is_set():
            return None
        try:
            with rate_limiter:
                response = scraper.get(url, timeout=(10, 20))
            if _cancel_event.is_set():
                return None
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except Exception:
            if _cancel_event.is_set():
                return None
            if attempt < MAX_RETRIES - 1:
                _cancel_aware_sleep(2 * (attempt + 1))
    return None


def get_manga_info(manga_url: str) -> tuple[str, list[dict], str | None] | None:
    """
    Access the manga page and extract the title, chapter list, and cover image URL.
    Returns (title, [{"number": "12", "url": "https://..."}], cover_url)
    """
    soup = fetch_page(manga_url)

    if not soup:
        return None

    # Extract manga title — mangapill uses <h1 class="font-bold ...">
    title_tag = soup.find("h1", class_=re.compile(r"font-bold"))
    if not title_tag:
        title_tag = soup.find("h1")
    if not title_tag:
        title_tag = soup.find("title")

    manga_title = title_tag.get_text(strip=True) if title_tag else "Unknown Manga"

    # Clean title (remove suffixes like " Manga - Mangapill")
    manga_title = re.sub(r"\s*[-|].*Mangapill.*$", "", manga_title, flags=re.IGNORECASE)
    manga_title = re.sub(r"\s*Manga\s*$", "", manga_title, flags=re.IGNORECASE)
    manga_title = manga_title.strip()

    # Extract cover image URL
    cover_url = None
    cover_img = soup.find("img", alt=re.compile(re.escape(manga_title[:20]), re.IGNORECASE))
    if cover_img:
        cover_url = cover_img.get("data-src") or cover_img.get("src")
    if not cover_url:
        # Fallback: og:image meta tag
        og_img = soup.find("meta", property="og:image")
        if og_img:
            cover_url = og_img.get("content")

    # Extract chapter list
    # mangapill lists chapters inside <div id="chapters"> with <a href="/chapters/..."> links
    chapters = []
    chapters_container = soup.find("div", id="chapters")

    if chapters_container:
        chapter_links = chapters_container.find_all("a", href=re.compile(r"/chapters/"))
    else:
        chapter_links = soup.find_all("a", href=re.compile(r"/chapters/"))

    for link in chapter_links:
        href = link.get("href", "")
        chapter_url = urljoin(BASE_URL, href)

        chapter_text = link.get_text(strip=True)
        chapter_num_match = re.search(r"chapter[- ]?([\d.]+)", chapter_text, re.IGNORECASE)
        if not chapter_num_match:
            chapter_num_match = re.search(r"chapter[- ]?([\d.]+)", href, re.IGNORECASE)

        if chapter_num_match:
            chapter_number = chapter_num_match.group(1)
        else:
            num_match = re.search(r"([\d.]+)", chapter_text)
            chapter_number = num_match.group(1) if num_match else "0"

        chapters.append({"number": chapter_number, "url": chapter_url})

    # Remove duplicates
    seen = set()
    unique_chapters = []
    for ch in chapters:
        if ch["url"] not in seen:
            seen.add(ch["url"])
            unique_chapters.append(ch)

    # Sort by chapter number
    def sort_key(ch):
        try:
            return float(ch["number"])
        except ValueError:
            return 0

    unique_chapters.sort(key=sort_key)

    return manga_title, unique_chapters, cover_url


def get_chapter_images(chapter_url: str) -> list[str]:
    """
    Access a chapter page and extract image URLs.
    mangapill uses <chapter-page> tags containing <img class="js-page" data-src="...">.
    """
    soup = fetch_page(chapter_url)
    if not soup:
        return []

    image_urls = []

    # Main strategy: <img class="js-page">
    imgs = soup.find_all("img", class_="js-page")

    if not imgs:
        # Fallback 1: <chapter-page> > <img>
        chapter_pages = soup.find_all("chapter-page")
        for cp in chapter_pages:
            imgs.extend(cp.find_all("img"))

    if not imgs:
        # Fallback 2: any <img data-src> from CDN
        all_imgs = soup.find_all("img", attrs={"data-src": True})
        imgs = [
            img for img in all_imgs
            if "mangap" in (img.get("data-src", "") or "").lower()
            or "cdn" in (img.get("data-src", "") or "").lower()
        ]

    for img in imgs:
        src = img.get("data-src") or img.get("src") or ""
        if not src:
            continue
        full_url = urljoin(chapter_url, src)
        image_urls.append(full_url)

    return image_urls


# ──────────────────────────────────────────────
# Download functions
# ──────────────────────────────────────────────
def _cancel_aware_sleep(seconds: float):
    """Sleep in small chunks, returning early if cancel is set."""
    remaining = seconds
    while remaining > 0 and not _cancel_event.is_set():
        time.sleep(min(remaining, 0.2))
        remaining -= 0.2


def download_single_image(index: int, url: str) -> tuple[int, Image.Image | None]:
    """
    Download a single image (executed in a thread).
    Returns (index, image) to maintain page order.
    """
    scraper = get_scraper()
    for attempt in range(MAX_RETRIES):
        _resume_event.wait()
        if _cancel_event.is_set():
            return (index, None)
        try:
            with rate_limiter:
                response = scraper.get(
                    url, timeout=10, headers={"Referer": BASE_URL + "/"}
                )
            if _cancel_event.is_set():
                return (index, None)
            response.raise_for_status()

            img = Image.open(BytesIO(response.content))
            # Convert to RGB if necessary (RGBA/P don't work in PDF)
            if img.mode in ("RGBA", "P", "LA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img, mask=img.split()[-1] if "A" in img.mode else None
                )
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            return (index, img)
        except Exception as e:
            if _cancel_event.is_set():
                return (index, None)
            if attempt < MAX_RETRIES - 1:
                _cancel_aware_sleep(1.5 * (attempt + 1))
    return (index, None)


def download_cover_image(cover_url: str, manga_title: str) -> bytes | None:
    """
    Download the manga cover image and cache it on disk.
    Returns the cover image bytes, or None if download fails.
    """
    safe_title = sanitize_filename(manga_title)
    cover_dir = get_manga_dir(safe_title)
    cover_path = os.path.join(cover_dir, "cover.jpg")

    # Use cached cover if available
    if os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            return f.read()

    if not cover_url:
        return None

    try:
        scraper = get_scraper()
        with rate_limiter:
            resp = scraper.get(
                cover_url,
                timeout=(10, 30),
                headers={"Referer": BASE_URL + "/"},
            )
        if resp.status_code == 200:
            img = Image.open(BytesIO(resp.content))
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=90)
            cover_bytes = buf.getvalue()
            # Cache to disk
            os.makedirs(cover_dir, exist_ok=True)
            with open(cover_path, "wb") as f:
                f.write(cover_bytes)
            return cover_bytes
    except Exception:
        pass

    return None


def stamp_cover_with_chapter(cover_data: bytes, chapter_num: str) -> bytes:
    """
    Overlay the chapter number as a badge on the cover image.
    Returns the modified cover as JPEG bytes.
    """
    img = Image.open(BytesIO(cover_data)).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Scale font size relative to the image width
    font_size = max(28, img.width // 8)
    try:
        # Try common system fonts (macOS → Linux → Windows → fallback)
        for font_path in [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
        ]:
            if os.path.exists(font_path):
                font = ImageFont.truetype(font_path, font_size)
                break
        else:
            font = ImageFont.load_default(size=font_size)
    except Exception:
        font = ImageFont.load_default(size=font_size)

    text = chapter_num.lstrip("0") or "0"

    # Measure text
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Badge dimensions and position (bottom-right corner)
    pad_x = int(text_w * 0.5)
    pad_y = int(text_h * 0.4)
    badge_w = text_w + pad_x * 2
    badge_h = text_h + pad_y * 2
    margin = int(img.width * 0.04)
    x = img.width - badge_w - margin
    y = img.height - badge_h - margin

    # Draw rounded rectangle badge with semi-transparent dark background
    badge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    badge_draw = ImageDraw.Draw(badge)
    radius = int(badge_h * 0.3)
    badge_draw.rounded_rectangle(
        [x, y, x + badge_w, y + badge_h],
        radius=radius,
        fill=(0, 0, 0, 200),
    )

    # Composite badge onto cover
    img = Image.alpha_composite(img.convert("RGBA"), badge).convert("RGB")

    # Draw text on top of the badge
    draw = ImageDraw.Draw(img)
    text_x = x + (badge_w - text_w) // 2
    text_y = y + (badge_h - text_h) // 2 - int(text_h * 0.05)
    draw.text((text_x, text_y), text, fill=(255, 255, 255), font=font)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ──────────────────────────────────────────────
# EPUB generation
# ──────────────────────────────────────────────
def images_to_epub(
    images: list[Image.Image],
    output_path: str,
    title: str,
    chapter_num: str,
    cover_data: bytes | None = None,
) -> bool:
    """
    Convert a list of PIL images into an EPUB file.
    Includes cover image and series metadata for Kindle grouping.
    """
    if not images:
        return False

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    padded_num = format_chapter_num(chapter_num)

    book = epub.EpubBook()
    book.set_identifier(f"manga-{sanitize_filename(title)}-ch{chapter_num}")
    book.set_title(f"Chapter {padded_num} - {title}")
    book.set_language("en")

    # Use manga title as author — Kindle groups books by author,
    # so all chapters of the same manga appear together
    book.add_author(title)

    # ── Series metadata ──
    try:
        series_index = str(float(chapter_num))
    except ValueError:
        series_index = chapter_num

    # Calibre series metadata (for Calibre users)
    book.add_metadata(None, "meta", "", {"name": "calibre:series", "content": title})
    book.add_metadata(None, "meta", "", {"name": "calibre:series_index", "content": series_index})

    # EPUB 3 belongs-to-collection (standard metadata)
    book.add_metadata(None, "meta", title, {"property": "belongs-to-collection", "id": "series-id"})
    book.add_metadata(None, "meta", "series", {"property": "collection-type", "refines": "#series-id"})
    book.add_metadata(None, "meta", series_index, {"property": "group-position", "refines": "#series-id"})

    book.add_metadata("DC", "subject", f"Manga: {title}")

    # ── Cover image with chapter number badge ──
    if cover_data:
        try:
            stamped_cover = stamp_cover_with_chapter(cover_data, chapter_num)
        except Exception:
            stamped_cover = cover_data
        book.set_cover("images/cover.jpg", stamped_cover)

    # Add CSS for full-page images
    style = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=b"""
body { margin: 0; padding: 0; }
.page { width: 100%; height: 100vh; display: flex; align-items: center; justify-content: center; }
.page img { max-width: 100%; max-height: 100%; object-fit: contain; }
""",
    )
    book.add_item(style)

    spine = ["nav"]
    toc = []

    for i, img in enumerate(images):
        # Save image to bytes
        img_bytes = BytesIO()
        img_format = "JPEG"
        img_ext = "jpg"
        img.save(img_bytes, format=img_format, quality=85)
        img_bytes.seek(0)

        # Add image to EPUB
        img_item = epub.EpubItem(
            uid=f"img{i+1:04d}",
            file_name=f"images/page_{i+1:04d}.{img_ext}",
            media_type="image/jpeg",
            content=img_bytes.read(),
        )
        book.add_item(img_item)

        # Create XHTML page wrapping the image
        page = epub.EpubHtml(
            title=f"Page {i+1}",
            file_name=f"page_{i+1:04d}.xhtml",
            lang="en",
        )
        page.content = f"""<html xmlns="http://www.w3.org/1999/xhtml">
<head><link rel="stylesheet" type="text/css" href="style/default.css"/></head>
<body><div class="page"><img src="images/page_{i+1:04d}.{img_ext}" alt="Page {i+1}"/></div></body>
</html>"""
        page.add_item(style)
        book.add_item(page)
        spine.append(page)

        if i == 0:
            toc.append(epub.Link(f"page_0001.xhtml", f"Chapter {padded_num}", f"ch{chapter_num}"))

    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(output_path, book)
    return True


# ──────────────────────────────────────────────
# Chapter & manga processing
# ──────────────────────────────────────────────
def download_chapter(
    manga_title: str,
    chapter: dict,
    cover_data: bytes | None = None,
    tracker: DownloadTracker | None = None,
    manga_url: str | None = None,
) -> bool:
    """
    Download all images from a chapter in parallel and generate the EPUB.
    Includes cover image and series metadata when cover_data is provided.
    Updates tracker with image-level progress when provided.
    Returns True if successful.
    """
    chapter_num = chapter["number"]
    chapter_url = chapter["url"]
    padded_num = format_chapter_num(chapter_num)

    safe_title = sanitize_filename(manga_title)
    filename = f"Chapter {padded_num} - {safe_title}.epub"
    output_dir = get_manga_dir(safe_title)
    output_path = os.path.join(output_dir, filename)

    # Check if EPUB already exists (check old naming formats too)
    old_filename_unpadded = f"{safe_title} - Chapter {chapter_num}.epub"
    old_filename_padded = f"{safe_title} - Chapter {padded_num}.epub"
    old_path_unpadded = os.path.join(output_dir, old_filename_unpadded)
    old_path_padded = os.path.join(output_dir, old_filename_padded)
    if os.path.exists(output_path):
        return True
    # Rename old format files if they exist
    for old_name, old_path in [(old_filename_padded, old_path_padded), (old_filename_unpadded, old_path_unpadded)]:
        if old_name != filename and os.path.exists(old_path):
            os.rename(old_path, output_path)
            return True

    # Get image URLs
    image_urls = get_chapter_images(chapter_url)

    if not image_urls:
        if tracker and manga_url:
            tracker.update(manga_url, status="error")
        return False

    # Update tracker with image progress start
    if tracker and manga_url:
        tracker.update(manga_url, current_ch=chapter_num, total_img=len(image_urls), done_img=0)

    # ── Parallel image download ──
    results: dict[int, Image.Image | None] = {}
    img_done = 0
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_IMAGES)
    futures = {
        executor.submit(download_single_image, i, url): i
        for i, url in enumerate(image_urls)
    }
    for future in as_completed(futures):
        _resume_event.wait()
        if _cancel_event.is_set():
            break
        idx, img = future.result()
        results[idx] = img
        img_done += 1
        if tracker and manga_url:
            tracker.update(manga_url, done_img=img_done)
    executor.shutdown(wait=False, cancel_futures=True)

    # Build image list in correct order
    images = []
    for i in range(len(image_urls)):
        img = results.get(i)
        if img:
            images.append(img)

    if not images:
        if tracker and manga_url:
            tracker.update(manga_url, status="error")
        return False

    success = images_to_epub(images, output_path, manga_title, chapter_num, cover_data=cover_data)

    if not success and tracker and manga_url:
        tracker.update(manga_url, status="error")

    # Free memory
    for img in images:
        img.close()

    return success


def process_manga(
    manga_url: str,
    state: dict,
    start_from: float | None = None,
    tracker: DownloadTracker | None = None,
):
    """
    Process a manga: check for new chapters and download missing ones.
    Chapters are downloaded in parallel (limited by MAX_CONCURRENT_CHAPTERS).
    Optionally filter by start_from chapter number.
    Updates tracker with progress for the Live dashboard.
    """
    if tracker:
        tracker.update(manga_url, status="checking")

    result = get_manga_info(manga_url)
    if not result:
        if tracker:
            tracker.update(manga_url, status="error")
        return

    manga_title, chapters, cover_url = result

    if tracker:
        tracker.update(manga_url, title=manga_title)

    if not chapters:
        if tracker:
            tracker.update(manga_url, status="up_to_date")
        return

    # Download and cache cover image for EPUB metadata
    cover_data = download_cover_image(cover_url, manga_title)

    # Check which chapters have already been downloaded
    manga_key = manga_url
    with state_lock:
        downloaded = set(state.get(manga_key, {}).get("chapters", []))

    new_chapters = [ch for ch in chapters if ch["url"] not in downloaded]

    # Filter by starting chapter if specified
    if start_from is not None:
        new_chapters = [
            ch for ch in new_chapters
            if parse_chapter_num(ch["number"]) >= start_from
        ]

    if not new_chapters:
        if tracker:
            tracker.update(manga_url, status="up_to_date")
        return

    if tracker:
        tracker.update(manga_url, total_ch=len(new_chapters), status="downloading")

    # ── Parallel chapter download ──
    success_count = 0

    def _download_and_track(chapter):
        _resume_event.wait()
        if _cancel_event.is_set():
            return chapter, False
        return chapter, download_chapter(
            manga_title, chapter, cover_data=cover_data,
            tracker=tracker, manga_url=manga_url,
        )

    try:
        executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHAPTERS)
        futures = [
            executor.submit(_download_and_track, ch) for ch in new_chapters
        ]
        for future in as_completed(futures):
            _resume_event.wait()
            if _cancel_event.is_set():
                break
            chapter, success = future.result()
            if success:
                with state_lock:
                    if manga_key not in state:
                        state[manga_key] = {"title": manga_title, "chapters": []}
                    state[manga_key]["chapters"].append(chapter["url"])
                    save_state(state)
                success_count += 1
                if tracker:
                    tracker.increment(manga_url, "done_ch")
        executor.shutdown(wait=False, cancel_futures=True)
    except KeyboardInterrupt:
        _cancel_event.set()
        _resume_event.set()

    # Update tracker final status
    if tracker:
        if _cancel_event.is_set():
            tracker.update(manga_url, status="done")
        else:
            tracker.update(manga_url, status="done")

    # Mark folder with "[NEW] - " prefix if new chapters were downloaded
    if success_count > 0:
        safe_title = sanitize_filename(manga_title)
        mark_folder_new(safe_title)


# ──────────────────────────────────────────────
# Download runner
# ──────────────────────────────────────────────
def _show_pause_prompt(tracker: DownloadTracker):
    """Show the pause prompt (called from the main thread after Live is stopped)."""
    done, total = tracker.get_totals()
    remaining = total - done

    pause_content = (
        "\n"
        f"[dim]Downloaded[/]   [green]{done} chapter{'s' if done != 1 else ''}[/]\n"
        f"[dim]Remaining[/]    [cyan]{remaining} chapter{'s' if remaining != 1 else ''}[/]\n"
        "\n"
        "[cyan bold]c[/]  Continue downloading\n"
        "[red bold]s[/]  Stop and save progress\n"
    )

    clear_screen()
    console.print()
    console.print(
        Panel(
            pause_content,
            title="[yellow bold]  ⏸  Paused  [/]",
            border_style="yellow",
            padding=(0, 3),
            expand=False,
        )
    )

    try:
        choice = Prompt.ask(
            "  [yellow]>[/]",
            choices=["c", "s"],
            default="c",
            show_choices=False,
        )
    except (EOFError, KeyboardInterrupt):
        choice = "s"

    if choice == "s":
        _cancel_event.set()
        _resume_event.set()
    else:
        _pause_event.clear()
        _resume_event.set()


def run_download(urls: list[str], download_all: bool = False, start_from: float | None = None):
    """Execute the download for given URLs using a Live dashboard."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cleanup_new_folders()
    _cancel_event.clear()
    _pause_event.clear()
    _resume_event.set()
    state = load_state()

    if download_all:
        for url in urls:
            if url in state:
                state[url]["chapters"] = []

    # Build tracker and register all mangas with placeholder titles
    tracker = DownloadTracker()
    for url in urls:
        # Extract a human-readable name from the URL as placeholder
        slug = url.rstrip("/").split("/")[-1].replace("-", " ").title()
        existing_title = state.get(url, {}).get("title", slug)
        tracker.add_manga(url, existing_title, status="waiting")

    # Start input listener for pause support (simplified: only sets events)
    if sys.stdin.isatty():
        listener = threading.Thread(target=_input_listener, daemon=True)
        listener.start()

    clear_screen()

    try:
        with Live(tracker.build_panel(), refresh_per_second=4, console=console) as live:
            executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_MANGAS)
            futures = {
                executor.submit(process_manga, url, state, start_from, tracker): url
                for url in urls
            }
            pending = set(futures.keys())

            while pending:
                # Handle pause — stop Live, show prompt, then resume Live
                if _pause_event.is_set():
                    live.stop()
                    _show_pause_prompt(tracker)
                    if not _cancel_event.is_set():
                        live.start()

                if _cancel_event.is_set():
                    break

                done_futures, pending = concurrent.futures.wait(
                    pending, timeout=0.05,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                # React to pause/cancel immediately after waking up
                if _pause_event.is_set() or _cancel_event.is_set():
                    continue

                for f in done_futures:
                    try:
                        f.result()
                    except Exception:
                        pass  # errors already handled in process_manga

                live.update(tracker.build_panel())

            executor.shutdown(wait=False, cancel_futures=True)

    except KeyboardInterrupt:
        _cancel_event.set()
        _resume_event.set()

    clear_screen()
    console.print()
    if _cancel_event.is_set():
        console.print(
            Panel.fit(
                f"[yellow bold]Download interrupted[/]  [dim]partial results saved to {os.path.abspath(OUTPUT_DIR)}[/]",
                border_style="yellow",
                padding=(0, 3),
            )
        )
    else:
        console.print(
            Panel.fit(
                f"[green bold]Done[/]  [dim]EPUBs saved to {os.path.abspath(OUTPUT_DIR)}[/]",
                border_style="green",
                padding=(0, 3),
            )
        )


# ──────────────────────────────────────────────
# Interactive menu — helpers
# ──────────────────────────────────────────────
def _wait_enter():
    """Wait for user to press Enter."""
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")


def _select_manga(prompt_text: str = "Select manga") -> dict | None:
    """Show numbered list of mangas and let user select one."""
    mangas = get_manga_state()
    if not mangas:
        console.print("[yellow]No mangas being monitored.[/]")
        return None

    table = Table(
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )
    table.add_column("#", style="cyan", justify="center", width=4)
    table.add_column("Title", style="white")
    table.add_column("EPUBs", style="green", justify="center", width=8)

    for i, m in enumerate(mangas, 1):
        table.add_row(str(i), m["title"], str(m["epub_count"]))

    console.print(table)
    console.print()

    choice = Prompt.ask(
        f"[cyan]{prompt_text}[/] [dim](number or 'c' to cancel)[/]",
        default="c",
    )

    if choice.lower() == "c":
        return None

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(mangas):
            return mangas[idx]
    except ValueError:
        pass

    console.print("[red]Invalid selection.[/]")
    return None


# ──────────────────────────────────────────────
# Interactive menu — actions
# ──────────────────────────────────────────────
def menu_list_mangas():
    """[1] List all monitored mangas."""
    clear_screen()
    mangas = get_manga_state()
    if not mangas:
        console.print("\n  [yellow]Nothing here yet.[/]")
        console.print("  [dim]Use option 2 to start tracking a manga.[/]")
        _wait_enter()
        return

    console.print()

    table = Table(
        title="[bold]Your Collection[/]",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 2),
        title_style="bold white",
    )
    table.add_column("#", style="cyan bold", justify="right", width=4)
    table.add_column("Title", style="bold white", min_width=20)
    table.add_column("EPUBs", style="green", justify="center", width=8)
    table.add_column("URL", style="dim", max_width=55, overflow="ellipsis")

    for i, m in enumerate(mangas, 1):
        table.add_row(str(i), m["title"], str(m["epub_count"]), m["url"])

    console.print(table)
    _wait_enter()


def menu_add_manga():
    """[2] Add a new manga URL."""
    clear_screen()
    url = Prompt.ask("[cyan]Manga URL[/] (mangapill.com/manga/...)")
    url = url.strip()

    if not url:
        console.print("[red]URL is required.[/]")
        _wait_enter()
        return

    if "mangapill.com/manga/" not in url:
        console.print("[red]Invalid URL. Use a URL from mangapill.com/manga/...[/]")
        _wait_enter()
        return

    urls = read_mangas_file()
    if url in urls:
        console.print("[yellow]This manga is already in the list.[/]")
        _wait_enter()
        return

    start_from_str = Prompt.ask(
        "[cyan]Start from chapter[/] [dim](leave empty for all chapters)[/]",
        default="",
    )

    start_from = None
    if start_from_str.strip():
        try:
            start_from = float(start_from_str)
        except ValueError:
            console.print("[red]Invalid chapter number.[/]")
            _wait_enter()
            return

    urls.append(url)
    write_mangas_file(urls)

    console.print("[dim]Fetching manga info...[/]")
    result = get_manga_info(url)

    if result:
        manga_title, chapters, _cover_url = result
        state = load_state()
        skipped_urls = []

        if start_from is not None:
            skipped_urls = [
                ch["url"] for ch in chapters
                if parse_chapter_num(ch["number"]) < start_from
            ]

        state[url] = {"title": manga_title, "chapters": skipped_urls}
        save_state(state)

        console.print(f"\n  [green]Added:[/] [bold]{manga_title}[/] [dim]· {len(chapters)} chapters available[/]")
        if skipped_urls:
            console.print(f"  [dim]{len(skipped_urls)} chapters before ch. {start_from} marked as read[/]")
    else:
        console.print("  [green]Added.[/] [dim]Could not fetch title yet.[/]")

    _wait_enter()


def menu_remove_manga():
    """[3] Remove a manga."""
    clear_screen()
    manga = _select_manga("Remove manga")
    if not manga:
        _wait_enter()
        return

    if not Confirm.ask(f"Remove [bold]{manga['title']}[/]?", default=False):
        console.print("[dim]Cancelled.[/]")
        _wait_enter()
        return

    urls = read_mangas_file()
    if manga["url"] in urls:
        urls.remove(manga["url"])
        write_mangas_file(urls)

    state = load_state()
    if manga["url"] in state:
        del state[manga["url"]]
        save_state(state)

    console.print(f"  [green]Removed:[/] [bold]{manga['title']}[/]")
    _wait_enter()


def menu_download_new():
    """[4] Download new chapters."""
    clear_screen()
    mangas = get_manga_state()
    if not mangas:
        console.print("[yellow]No mangas being monitored.[/]")
        _wait_enter()
        return

    console.print(
        Panel.fit(
            "[cyan bold]a[/]  All mangas at once\n"
            "[cyan bold]s[/]  Select a specific one",
            title="[bold]Download[/]",
            border_style="cyan",
            padding=(1, 3),
        )
    )
    console.print()

    choice = Prompt.ask("Choice", choices=["a", "s"], default="a")

    urls_to_download = []
    start_from = None

    if choice == "s":
        manga = _select_manga("Download manga")
        if not manga:
            _wait_enter()
            return
        urls_to_download = [manga["url"]]

        start_from_str = Prompt.ask(
            "[cyan]Start from chapter[/] [dim](leave empty for all new)[/]",
            default="",
        )
        if start_from_str.strip():
            try:
                start_from = float(start_from_str)
            except ValueError:
                console.print("[red]Invalid chapter number.[/]")
                _wait_enter()
                return
    else:
        urls_to_download = [m["url"] for m in mangas]

    run_download(urls_to_download, download_all=False, start_from=start_from)
    _wait_enter()


def menu_download_all():
    """[5] Re-download all chapters."""
    clear_screen()
    if not Confirm.ask(
        "[yellow]Re-download ALL chapters?[/] This ignores download history.",
        default=False,
    ):
        console.print("[dim]Cancelled.[/]")
        _wait_enter()
        return

    mangas = get_manga_state()
    if not mangas:
        console.print("[yellow]No mangas being monitored.[/]")
        _wait_enter()
        return

    urls = [m["url"] for m in mangas]
    run_download(urls, download_all=True)
    _wait_enter()


def menu_list_epubs():
    """[6] List downloaded EPUBs."""
    clear_screen()
    epubs = get_epubs()
    if not epubs:
        console.print("\n[yellow]No EPUBs downloaded yet.[/]")
        _wait_enter()
        return

    # Group by manga
    grouped: dict[str, list[dict]] = {}
    for ep in epubs:
        grouped.setdefault(ep["manga"], []).append(ep)

    console.print()

    table = Table(
        title="[bold]Your Library[/]",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 2),
        title_style="bold white",
        caption=f"[dim]{len(epubs)} files across {len(grouped)} series[/]",
    )
    table.add_column("#", style="cyan bold", justify="right", width=4)
    table.add_column("Manga", style="bold white", min_width=15)
    table.add_column("File", style="white")
    table.add_column("Size", style="dim", justify="right", width=10)

    idx = 1
    for manga_name, files in sorted(grouped.items()):
        for j, ep in enumerate(files):
            manga_col = manga_name if j == 0 else ""
            table.add_row(str(idx), manga_col, ep["filename"], f"{ep['size_mb']} MB")
            idx += 1

    console.print(table)
    _wait_enter()


def menu_delete_epub():
    """[7] Delete an EPUB."""
    clear_screen()
    epubs = get_epubs()
    if not epubs:
        console.print("\n[yellow]No EPUBs to delete.[/]")
        _wait_enter()
        return

    # Group by manga for display
    grouped: dict[str, list[dict]] = {}
    for ep in epubs:
        grouped.setdefault(ep["manga"], []).append(ep)

    table = Table(
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )
    table.add_column("#", style="cyan", justify="center", width=4)
    table.add_column("Manga", style="bold white")
    table.add_column("File", style="white")
    table.add_column("Size", style="green", justify="right", width=10)

    idx = 1
    for manga_name, files in sorted(grouped.items()):
        for j, ep in enumerate(files):
            manga_col = manga_name if j == 0 else ""
            table.add_row(str(idx), manga_col, ep["filename"], f"{ep['size_mb']} MB")
            idx += 1

    console.print()
    console.print(table)
    console.print()

    choice = Prompt.ask(
        "[cyan]Delete EPUB[/] [dim](number or 'c' to cancel)[/]",
        default="c",
    )

    if choice.lower() == "c":
        _wait_enter()
        return

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(epubs):
            ep = epubs[idx]
            full_path = Path(OUTPUT_DIR) / ep["path"]

            if Confirm.ask(f"Delete [bold]{ep['filename']}[/]?", default=False):
                os.remove(full_path)
                console.print(f"  [green]Deleted:[/] {ep['filename']}")
            else:
                console.print("[dim]Cancelled.[/]")
        else:
            console.print("[red]Invalid selection.[/]")
    except ValueError:
        console.print("[red]Invalid selection.[/]")

    _wait_enter()


def menu_check_info():
    """[8] Check manga info."""
    clear_screen()

    url = Prompt.ask(
        "[cyan]Manga URL[/] [dim](or leave empty to select from list)[/]",
        default="",
    )

    if not url.strip():
        manga = _select_manga("Check manga")
        if not manga:
            _wait_enter()
            return
        url = manga["url"]

    console.print("[dim]Fetching manga info...[/]")
    result = get_manga_info(url)

    if not result:
        console.print("[red]Could not access the manga.[/]")
        _wait_enter()
        return

    title, chapters, _cover_url = result
    state = load_state()
    downloaded = set(state.get(url, {}).get("chapters", []))

    downloaded_count = len([c for c in chapters if c["url"] in downloaded])
    new_count = len([c for c in chapters if c["url"] not in downloaded])

    info_content = (
        f"[dim]Total chapters[/]    {len(chapters)}\n"
        f"[dim]Downloaded[/]        [green]{downloaded_count}[/]\n"
        f"[dim]Pending[/]           [yellow]{new_count}[/]\n"
        f"[dim]URL[/]               [dim]{url}[/]"
    )

    console.print()
    console.print(
        Panel(
            info_content,
            title=f"[bold]{title}[/]",
            border_style="cyan",
            padding=(1, 3),
            expand=False,
        )
    )
    _wait_enter()


def menu_reset_history():
    """[9] Reset download history."""
    clear_screen()
    if not Confirm.ask(
        "[yellow]Reset ALL download history?[/] EPUBs on disk won't be deleted.",
        default=False,
    ):
        console.print("[dim]Cancelled.[/]")
        _wait_enter()
        return

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        console.print("  [green]History cleared.[/]")
    else:
        console.print("  [dim]Nothing to clear.[/]")

    _wait_enter()


# ──────────────────────────────────────────────
# Interactive menu — main loop
# ──────────────────────────────────────────────
MENU_ACTIONS = {
    "1": menu_list_mangas,
    "2": menu_add_manga,
    "3": menu_remove_manga,
    "4": menu_download_new,
    "5": menu_download_all,
    "6": menu_list_epubs,
    "7": menu_delete_epub,
    "8": menu_check_info,
    "9": menu_reset_history,
}


def interactive_menu():
    """Main interactive menu loop."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    while True:
        clear_screen()
        menu_content = (
            "\n"
            "[dim]Mangas[/]\n"
            "  [cyan bold]1[/]  Browse your collection\n"
            "  [cyan bold]2[/]  Track a new manga\n"
            "  [cyan bold]3[/]  Stop tracking a manga\n"
            "\n"
            "[dim]Downloads[/]\n"
            "  [cyan bold]4[/]  Fetch latest chapters\n"
            "  [cyan bold]5[/]  Re-download everything\n"
            "\n"
            "[dim]Library[/]\n"
            "  [cyan bold]6[/]  Browse your EPUBs\n"
            "  [cyan bold]7[/]  Delete an EPUB\n"
            "\n"
            "[dim]Tools[/]\n"
            "  [cyan bold]8[/]  Inspect manga details\n"
            "  [cyan bold]9[/]  Wipe download history\n"
            "  [cyan bold]0[/]  [dim]Quit[/]\n"
        )

        console.print()
        console.print(
            Panel(
                menu_content,
                title="[bold white]  Manga Downloader[/] [dim]for Kindle  [/]",
                border_style="cyan",
                padding=(0, 3),
                expand=False,
            )
        )

        choice = Prompt.ask(
            "  [cyan]>[/]",
            choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
            default="0",
            show_choices=False,
        )

        if choice == "0":
            clear_screen()
            break

        action = MENU_ACTIONS.get(choice)
        if action:
            action()


# ──────────────────────────────────────────────
# CLI mode (non-interactive)
# ──────────────────────────────────────────────
def run_cli_download(download_all: bool = False):
    """Run download in non-interactive CLI mode."""
    mode = "re-downloading everything" if download_all else "fetching new chapters"
    cli_content = (
        f"[bold]{mode}[/]\n"
        f"[dim]{MAX_CONCURRENT_IMAGES} images | "
        f"{MAX_CONCURRENT_CHAPTERS} chapters | "
        f"{MAX_CONCURRENT_MANGAS} mangas | "
        f"{GLOBAL_MAX_CONNECTIONS} max connections[/]"
    )
    console.print(
        Panel(
            cli_content,
            title="[bold white]  Manga Downloader[/] [dim]for Kindle  [/]",
            border_style="cyan",
            padding=(1, 3),
            expand=False,
        )
    )

    urls = read_mangas_file()
    if not urls:
        console.print(f"\n  [red]No URLs found in[/] [bold]{MANGAS_FILE}[/]")
        console.print(
            "  [dim]Create the file with one manga URL per line, "
            "or use the interactive menu.[/]"
        )
        sys.exit(1)

    console.print(f"  [dim]{len(urls)} manga(s) to check[/]")
    run_download(urls, download_all=download_all)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main():
    """Main entry point with argparse."""
    parser = argparse.ArgumentParser(
        description="Manga Downloader for Kindle — download manga chapters as EPUBs",
    )
    parser.add_argument(
        "--download", "-d",
        action="store_true",
        help="Download new chapters and exit (non-interactive)",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Re-download all chapters (ignores history)",
    )
    parser.add_argument(
        "--reset", "-r",
        action="store_true",
        help="Reset download history",
    )

    args = parser.parse_args()

    if args.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            console.print("[green]History cleared.[/]")
        else:
            console.print("[dim]Nothing to clear.[/]")
        if not args.download and not args.all:
            return

    if args.download or args.all:
        run_cli_download(download_all=args.all)
    else:
        interactive_menu()


if __name__ == "__main__":
    main()
