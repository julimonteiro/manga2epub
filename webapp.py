#!/usr/bin/env python3
"""
Manga Kindle — small web app to manage the monitored mangas from the phone.

Lists the mangas with their covers, adds a manga from a mangapill.com link and
removes one (list entry, download history, EPUBs and the Mihon copy).
Listens on the local network (no login), so anyone on the same Wi-Fi can use it.
Use --host 127.0.0.1 to keep it on this machine only.

Usage:
    python3 webapp.py [--host 0.0.0.0] [--port 8766]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import manga_downloader as md

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
MANGA_URL_RE = re.compile(r"^https://mangapill\.com/manga/\d+/[\w-]+$")

# Serialises changes to mangas.txt and the download history made by this app.
data_lock = threading.Lock()


def slug_title(url: str) -> str:
    return url.rstrip("/").split("/")[-1].replace("-", " ").title()


def title_of(url: str, state: dict) -> str:
    return state.get(url, {}).get("title") or slug_title(url)


def find_cover(title: str) -> Path | None:
    safe = md.sanitize_filename(title)
    for folder in (safe, f"{md.NEW_FOLDER_PREFIX}{safe}"):
        path = Path(md.OUTPUT_DIR) / folder / "cover.jpg"
        if path.exists():
            return path
    return None


def list_mangas() -> list[dict]:
    state = md.load_state()
    mangas = []
    for url in md.read_mangas_file():
        title = title_of(url, state)
        mangas.append({
            "url": url,
            "title": title,
            "chapters": len(state.get(url, {}).get("chapters", [])),
            "has_cover": find_cover(title) is not None,
        })
    return mangas


def start_download(url: str, from_chapter: float | None):
    """Run a background download for one manga (waits if another download is running)."""
    cmd = [sys.executable, "manga_downloader.py", "--download", "--only", url]
    if from_chapter is not None:
        cmd += ["--from", str(from_chapter)]
    os.makedirs("logs", exist_ok=True)
    log = open(os.path.join("logs", "app.log"), "a")
    subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def preview(url: str) -> dict:
    info = md.get_manga_info(url)
    if not info:
        raise ValueError("Could not read this manga. Check the link.")
    title, chapters, cover_url = info
    md.download_cover_image(cover_url, title)  # caches cover.jpg for the card
    numbers = [md.parse_chapter_num(c["number"]) for c in chapters]
    return {
        "title": title,
        "chapters": len(chapters),
        "latest": max(numbers) if numbers else None,
    }


def add_manga(url: str, mode: str, from_chapter: float | None) -> dict:
    with data_lock:
        urls = md.read_mangas_file()
        if url in urls:
            raise ValueError("This manga is already in the list.")
        info = md.get_manga_info(url)
        if not info:
            raise ValueError("Could not read this manga. Check the link.")
        title, chapters, cover_url = info
        md.download_cover_image(cover_url, title)

        if md.run_lock_busy():
            raise RuntimeError("A download is running. Try again in a few minutes.")
        state = md.load_state()
        if mode == "new":
            # Skip everything that exists now; only future chapters get downloaded.
            state[url] = {"title": title, "chapters": [c["url"] for c in chapters]}
        else:
            state.setdefault(url, {"title": title, "chapters": []})
        md.save_state(state)
        md.write_mangas_file(urls + [url])

    if mode != "new":
        start_download(url, from_chapter if mode == "from" else None)
    return {"title": title}


def remove_manga(url: str) -> dict:
    with data_lock:
        if md.run_lock_busy():
            raise RuntimeError("A download is running. Try again in a few minutes.")
        urls = md.read_mangas_file()
        if url not in urls:
            raise ValueError("Manga not in the list.")
        state = md.load_state()
        safe = md.sanitize_filename(title_of(url, state))

        md.write_mangas_file([u for u in urls if u != url])
        state.pop(url, None)
        md.save_state(state)

        if safe and safe not in (".", ".."):
            for base, name in (
                (md.OUTPUT_DIR, safe),
                (md.OUTPUT_DIR, f"{md.NEW_FOLDER_PREFIX}{safe}"),
                (md.MIHON_DIR, safe),
            ):
                folder = (Path(base) / name).resolve()
                if folder.is_dir() and Path(base).resolve() in folder.parents:
                    shutil.rmtree(folder)
    return {"removed": safe}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload):
        self._send(code, json.dumps(payload).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/manifest.webmanifest":
            self._send(200, (WEB_DIR / "manifest.webmanifest").read_bytes(), "application/manifest+json")
        elif parsed.path == "/icon.svg":
            self._send(200, (WEB_DIR / "icon.svg").read_bytes(), "image/svg+xml")
        elif parsed.path == "/api/mangas":
            self._json(200, {"mangas": list_mangas(), "downloading": md.run_lock_busy()})
        elif parsed.path == "/cover":
            query = parse_qs(parsed.query)
            url = query.get("url", [""])[0]
            # "title" lets the add dialog show a cover for a manga that is not in the list yet.
            cover = find_cover(query.get("title", [""])[0] or title_of(url, md.load_state()))
            if cover:
                self._send(200, cover.read_bytes(), "image/jpeg")
            else:
                self._send(404, b"")
        else:
            self._send(404, b"")

    def do_POST(self):
        # JSON content type forces a CORS preflight, so other websites cannot call this blindly.
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._json(415, {"error": "Expected JSON"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "Invalid JSON"})

        url = str(body.get("url", "")).strip().split("?")[0].rstrip("/")
        try:
            if self.path == "/api/preview":
                if not MANGA_URL_RE.match(url):
                    raise ValueError("Use a link like https://mangapill.com/manga/1234/name")
                return self._json(200, preview(url))
            if self.path == "/api/add":
                if not MANGA_URL_RE.match(url):
                    raise ValueError("Use a link like https://mangapill.com/manga/1234/name")
                mode = body.get("mode", "all")
                if mode not in ("all", "from", "new"):
                    raise ValueError("Invalid mode")
                try:
                    from_chapter = float(body["from"]) if mode == "from" else None
                except (KeyError, ValueError):
                    raise ValueError("Enter the chapter number to start from.")
                return self._json(200, add_manga(url, mode, from_chapter))
            if self.path == "/api/remove":
                return self._json(200, remove_manga(url))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except RuntimeError as e:
            return self._json(409, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"Unexpected error: {e}"})
        self._json(404, {"error": "Not found"})


def main():
    parser = argparse.ArgumentParser(description="Manga Kindle web app")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    os.chdir(ROOT)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Manga Kindle web app on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
