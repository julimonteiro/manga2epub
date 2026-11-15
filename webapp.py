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
import hmac
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import Counter
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import manga_downloader as md
import recommender

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
MANGA_URL_RE = re.compile(r"^https://mangapill\.com/manga/\d+/[\w-]+$")

TOKEN_FILE = ROOT / ".webapp_token"
PROGRESS_FILE = ROOT / "reading_progress.json"
PUBLIC_PATHS = {"/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png", "/icon-maskable.png", "/sw.js"}
COOKIE = "mt"


def load_token() -> str:
    """Secret required for requests that arrive through the Cloudflare tunnel."""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return token


TOKEN = load_token()


def load_progress() -> dict:
    try:
        return json.loads(PROGRESS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_progress(data: dict):
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(PROGRESS_FILE)


def download_status() -> dict:
    """Live progress of the running download (empty when nothing is downloading)."""
    for u in [u for u, p in queued.items() if p.poll() is not None]:
        del queued[u]
    busy = md.run_lock_busy()
    if not busy and not queued:
        return {"active": False, "mangas": []}
    try:
        snap = json.loads((ROOT / md.STATUS_FILE).read_text()) if busy else {}
    except (OSError, ValueError):
        snap = {}
    state = md.load_state()
    snap.setdefault("mangas", [])
    listed = {m["url"] for m in snap["mangas"]}
    for u in queued:  # added from the app and still waiting for the running download to finish
        if u != ALL and u not in listed:
            snap["mangas"].append({"url": u, "title": title_of(u, state), "status": "waiting"})
    mangas = []
    for m in snap.get("mangas", []):
        if m["status"] in ("waiting", "checking", "downloading", "done", "error") and (
            m["status"] != "waiting" or m["url"] in queued or len(snap["mangas"]) <= 6
        ):
            title = m.get("title") or title_of(m["url"], state)
            mangas.append({
                "url": m["url"], "title": title, "status": m["status"],
                "chapter": m.get("current_ch", ""), "done_ch": m.get("done_ch", 0), "total_ch": m.get("total_ch", 0),
                "done_img": m.get("done_img", 0), "total_img": m.get("total_img", 0),
            })
    order = {"downloading": 0, "checking": 1, "waiting": 2, "error": 3, "done": 4}
    mangas.sort(key=lambda m: order.get(m["status"], 9))
    return {"active": True, "mangas": mangas}


def recent_updates(since: float) -> list[dict]:
    """Mangas that got new chapters after `since` (unix time), newest first."""
    try:
        log = json.loads((ROOT / md.NEW_LOG_FILE).read_text())
    except (OSError, ValueError):
        return []
    listed = set(md.read_mangas_file())
    state = md.load_state()
    groups: dict[str, dict] = {}
    for e in log:
        if e["ts"] <= since or e["url"] not in listed:
            continue
        g = groups.setdefault(e["url"], {"url": e["url"], "title": title_of(e["url"], state), "chapters": [], "ts": 0})
        g["chapters"].append(e["chapter"])
        g["ts"] = max(g["ts"], e["ts"])
    out = sorted(groups.values(), key=lambda g: -g["ts"])
    for g in out:
        g["chapters"].sort(key=md.parse_chapter_num)
        g["has_cover"] = find_cover(g["title"]) is not None
    return out


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


_json_cache: dict[str, tuple[float, object]] = {}


def cached_json(name: str, default):
    """Parse a JSON file only when it changed on disk (the lists are polled every few seconds)."""
    path = ROOT / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    hit = _json_cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return default
    _json_cache[name] = (mtime, data)
    return data


def chapter_key(name: str) -> tuple[float, str]:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return (float(m.group(1)) if m else 0, name)


def pace_window(log: list[dict], now: float) -> float:
    """Days the reading pace is measured over: up to 30, less while the log is still young."""
    first = min((e["ts"] for e in log), default=now)
    return max(3.0, min(30.0, (now - first) / 86400))


def list_mangas() -> list[dict]:
    state = md.load_state()
    meta = md.load_meta()
    progress = load_progress()
    now = time.time()
    read_log = cached_json(md.READ_LOG_FILE, [])
    new_log = cached_json(md.NEW_LOG_FILE, [])
    window = pace_window(read_log, now)
    read_n: Counter = Counter()
    last_read: dict[str, float] = {}
    for e in read_log:
        last_read[e["url"]] = max(last_read.get(e["url"], 0), e["ts"])
        if e["ts"] >= now - window * 86400:
            read_n[e["url"]] += 1
    # releases: days that brought new chapters (a backfill of many chapters in one go counts once)
    new_days: dict[str, set] = {}
    last_new: dict[str, float] = {}
    new_7d: Counter = Counter()
    for e in new_log:
        last_new[e["url"]] = max(last_new.get(e["url"], 0), e["ts"])
        if e["ts"] >= now - 7 * 86400:
            new_7d[e["url"]] += 1
        if e["ts"] >= now - 30 * 86400:
            new_days.setdefault(e["url"], set()).add(date.fromtimestamp(e["ts"]))
    queue = meta["queue"]
    mangas = []
    for url in md.read_mangas_file():
        title = title_of(url, state)
        m = meta["mangas"].get(url, {})
        files = chapter_paths(url, state)
        ordered = sorted(files, key=chapter_key)
        prog = progress.get(url, {})
        started = next(({"file": f, "page": prog[f]["page"], "total": prog[f]["total"]} for f in ordered
                        if f in prog and prog[f]["page"] < prog[f]["total"] - 1), None)
        unread = len(files)
        read_rate = read_n.get(url, 0) / window
        rel_rate = len(new_days.get(url, ())) / 30
        eta = 0.0 if unread == 0 else (unread / (read_rate - rel_rate) if read_rate - rel_rate > 0.01 else None)
        mangas.append({
            "url": url,
            "title": title,
            "chapters": unread,  # EPUBs currently on disk = unread
            "has_cover": find_cover(title) is not None,
            "paused": bool(m.get("paused")),
            "completed": bool(m.get("completed")),
            "status": m.get("status", ""),
            "genres": m.get("genres", []),
            "latest": m.get("latest"),
            "total": m.get("total"),
            "error": m.get("error"),
            "fails": m.get("fails", 0),
            "checked": m.get("checked"),
            "queue": queue.index(url) if url in queue else -1,
            "next": ordered[0] if ordered else None,
            "started": started,
            "last_read": last_read.get(url),
            "last_new": last_new.get(url),
            "new_7d": new_7d.get(url, 0),
            "read_rate": round(read_rate, 3),
            "release_rate": round(rel_rate, 3),
            "eta_days": None if eta is None else round(eta, 1),
        })
    return mangas


def stats() -> dict:
    now = time.time()
    log = cached_json(md.READ_LOG_FILE, [])
    today = date.today()
    per_day = Counter(date.fromtimestamp(e["ts"]) for e in log)
    days = [{"d": (today - timedelta(n)).isoformat(), "n": per_day.get(today - timedelta(n), 0)} for n in range(83, -1, -1)]
    cursor = today if per_day.get(today) else today - timedelta(1)
    streak = 0
    while per_day.get(cursor):
        streak += 1
        cursor -= timedelta(1)
    best = 0
    run = 0
    for d in sorted(per_day):
        run = run + 1 if (d - timedelta(1)) in per_day else 1
        best = max(best, run)
    weeks = [sum(per_day.get(today - timedelta(w * 7 + k), 0) for k in range(7)) for w in range(7, -1, -1)]
    top = Counter()
    urls: dict[str, str] = {}
    for e in log:
        if e["ts"] >= now - 30 * 86400:
            top[e["title"]] += 1
            urls[e["title"]] = e["url"]
    return {
        "now": now,
        "total": len(log),
        "since": min((e["ts"] for e in log), default=None),
        "days": days,
        "streak": streak,
        "best_streak": best,
        "this_week": weeks[-1],
        "last_week": weeks[-2],
        "weeks": weeks,
        "active_days": len(per_day),
        "best_day": max(per_day.values(), default=0),
        "top": [{"title": t, "n": n, "url": urls[t], "has_cover": find_cover(t) is not None} for t, n in top.most_common(5)],
    }


def chapter_paths(url: str, state: dict | None = None) -> dict[str, Path]:
    """EPUB files on disk for one manga, keyed by file name."""
    safe = md.sanitize_filename(title_of(url, state if state is not None else md.load_state()))
    files = {}
    for folder in (safe, f"{md.NEW_FOLDER_PREFIX}{safe}"):
        base = Path(md.OUTPUT_DIR) / folder
        if base.is_dir():
            for f in base.glob("*.epub"):
                files[f.name] = f
    return files


def list_chapter_files(url: str) -> list[dict]:
    def key(f):
        m = re.search(r"(\d+(?:\.\d+)?)", f.name)
        return (float(m.group(1)) if m else 0, f.name)
    return [
        {"name": f.stem, "file": f.name, "size": f.stat().st_size}
        for f in sorted(chapter_paths(url).values(), key=key)
    ]


def chapter_pages(path: Path) -> list[str]:
    """Page image names inside an EPUB, in reading order (cover excluded)."""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.search(r"/page_\d+\.(jpe?g|png|webp)$", n, re.I)]
    return sorted(names)


def read_page(path: Path, index: int) -> tuple[bytes, str]:
    pages = chapter_pages(path)
    if not 0 <= index < len(pages):
        raise ValueError("Page not found.")
    with zipfile.ZipFile(path) as z:
        data = z.read(pages[index])
    ext = pages[index].rsplit(".", 1)[-1].lower()
    return data, {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")


def delete_read_chapter(url: str, file: str) -> dict:
    """Delete the EPUB of a chapter that was fully read. The download history keeps the
    chapter, so the downloader does not fetch it again."""
    with data_lock:
        state = md.load_state()
        path = chapter_paths(url, state).get(file)
        if not path:
            raise ValueError("Chapter not found.")
        path.unlink()
        progress = load_progress()
        if progress.get(url, {}).pop(file, None) is not None:
            save_progress(progress)
        md.log_read_chapter(url, title_of(url, state), Path(file).stem)
    return {"deleted": file}


def set_flag(url: str, key: str, value: bool) -> dict:
    if url not in md.read_mangas_file():
        raise ValueError("Manga not in the list.")

    def apply(meta):
        m = meta["mangas"].setdefault(url, {})
        if value:
            m[key] = True
        else:
            m.pop(key, None)
        if key in ("paused", "completed") and value and url in meta["queue"]:
            meta["queue"].remove(url)
    md.modify_meta(apply)
    return {"ok": True}


def edit_queue(url: str, action: str) -> dict:
    """The "Up next" list: the mangas you want to read first, in order. The downloader checks them first too."""
    if url not in md.read_mangas_file():
        raise ValueError("Manga not in the list.")

    def apply(meta):
        q = [u for u in meta["queue"] if u in set(md.read_mangas_file())]
        if action == "add" and url not in q:
            q.append(url)
        elif action == "remove" and url in q:
            q.remove(url)
        elif action in ("up", "down", "top") and url in q:
            i = q.index(url)
            q.remove(url)
            q.insert(0 if action == "top" else max(0, i - 1) if action == "up" else min(len(q), i + 1), url)
        meta["queue"] = q
    md.modify_meta(apply)
    return {"ok": True}


def backfill_meta():
    """One-time (and for new mangas): learn status, genres and chapter counts for mangas never checked."""
    time.sleep(3)
    for url in md.read_mangas_file():
        if url in md.load_meta()["mangas"] and md.load_meta()["mangas"][url].get("genres") is not None and md.load_meta()["mangas"][url].get("total"):
            continue
        try:
            info = md.get_manga_info(url)
        except Exception:
            continue
        if info:
            title, chapters, _ = info
            nums = [md.parse_chapter_num(c["number"]) for c in chapters]
            md.record_check(url, title=title, total=len(chapters), latest=max(nums) if nums else None, details=md.LAST_DETAILS.get(url))


ALL = "*"  # key in `queued` for a full check of the library
queued: dict[str, subprocess.Popen] = {}  # downloads started by this app that may still be waiting their turn


def start_download(url: str, from_chapter: float | None):
    """Run a background download for one manga, or for the whole library with url=ALL
    (waits if another download is running)."""
    cmd = [sys.executable, "manga_downloader.py", "--download"]
    if url != ALL:
        cmd += ["--only", url]
    if from_chapter is not None:
        cmd += ["--from", str(from_chapter)]
    os.makedirs("logs", exist_ok=True)
    log = open(os.path.join("logs", "app.log"), "a")
    # Each download waits for the download lock, so several added mangas simply download one after another.
    queued[url] = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


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
    """Add to the library right away. The download is queued and runs when the downloader is free."""
    with data_lock:
        if url in md.read_mangas_file():
            raise ValueError("This manga is already in the list.")
    info = md.get_manga_info(url)
    if not info:
        raise ValueError("Could not read this manga. Check the link.")
    title, chapters, cover_url = info
    md.download_cover_image(cover_url, title)
    with data_lock:
        urls = md.read_mangas_file()
        if url in urls:
            raise ValueError("This manga is already in the list.")
        if mode == "new":
            # Skip everything that exists now; only future chapters get downloaded.
            md.modify_state(lambda st: st.__setitem__(url, {"title": title, "chapters": [c["url"] for c in chapters]}))
        else:
            md.modify_state(lambda st: st.setdefault(url, {"title": title, "chapters": []}))
        md.write_mangas_file(urls + [url])

    if mode != "new":
        start_download(url, from_chapter if mode == "from" else None)
    return {"title": title, "queued": mode != "new"}


def remove_manga(url: str) -> dict:
    with data_lock:
        if url in {m["url"] for m in download_status()["mangas"] if m["status"] in ("downloading", "checking")} | set(queued):
            raise RuntimeError("This manga is downloading right now. Try again when it finishes.")
        urls = md.read_mangas_file()
        if url not in urls:
            raise ValueError("Manga not in the list.")
        state = md.load_state()
        safe = md.sanitize_filename(title_of(url, state))

        read_title = title_of(url, state) if len(state.get(url, {}).get("chapters", [])) > len(chapter_paths(url, state)) else None
        md.write_mangas_file([u for u in urls if u != url])
        md.modify_state(lambda st: st.pop(url, None))
        md.modify_meta(lambda mt: (mt["mangas"].pop(url, None), mt["queue"].remove(url) if url in mt["queue"] else None))

        if safe and safe not in (".", ".."):
            for base, name in (
                (md.OUTPUT_DIR, safe),
                (md.OUTPUT_DIR, f"{md.NEW_FOLDER_PREFIX}{safe}"),
                (md.MIHON_DIR, safe),
            ):
                folder = (Path(base) / name).resolve()
                if folder.is_dir() and Path(base).resolve() in folder.parents:
                    shutil.rmtree(folder)
    if read_title:  # something was read: keep it in the read history so suggestions learn from it
        threading.Thread(target=recommender.history_add_title, args=(read_title,), daemon=True).start()
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

    def _is_remote(self) -> bool:
        # cloudflared connects from 127.0.0.1, so the client IP cannot tell local from remote;
        # anything that went through Cloudflare carries these headers.
        return any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "Forwarded"))

    def _cookie_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _authorized(self) -> bool:
        if not self._is_remote():
            return True  # home network: no login
        supplied = self.headers.get("X-Manga-Token") or self._cookie_token()
        return hmac.compare_digest(supplied.encode(), TOKEN.encode())

    def _locked(self):
        if self.path.startswith("/api/"):
            return self._json(401, {"error": "Unauthorized"})
        body = b"<!doctype html><meta name=viewport content='width=device-width'><body style='font:16px system-ui;padding:3em;text-align:center'>Locked. Open the app to sign in."
        self._send(401, body, "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" and self._is_remote() and parse_qs(parsed.query).get("token"):
            # one-time sign-in link for a browser: store the token as a cookie, then hide it from the URL
            if hmac.compare_digest(parse_qs(parsed.query)["token"][0].encode(), TOKEN.encode()):
                self.send_response(302)
                self.send_header("Set-Cookie", f"{COOKIE}={TOKEN}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Strict")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
        if parsed.path not in PUBLIC_PATHS and not self._authorized():
            return self._locked()
        if parsed.path == "/api/ping":
            self._json(200, {"ok": True})
        elif parsed.path == "/api/status":
            self._json(200, download_status())
        elif parsed.path == "/api/updates":
            try:
                since = float(parse_qs(parsed.query).get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            self._json(200, {"now": time.time(), "updates": recent_updates(since)})
        elif parsed.path == "/api/history":
            self._json(200, {"items": recommender.load_history()})
        elif parsed.path == "/api/history/search":
            q = parse_qs(parsed.query).get("q", [""])[0].strip()
            try:
                self._json(200, {"items": recommender.search_anilist(q) if len(q) >= 2 else []})
            except Exception:
                self._json(502, {"error": "Could not reach AniList."})
        elif parsed.path == "/api/recommendations":
            self._json(200, recommender.recommendations())
        elif parsed.path == "/api/progress":
            url = parse_qs(parsed.query).get("url", [""])[0]
            self._json(200, {"progress": load_progress().get(url, {})})
        elif parsed.path == "/":
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/manifest.webmanifest":
            self._send(200, (WEB_DIR / "manifest.webmanifest").read_bytes(), "application/manifest+json")
        elif parsed.path == "/sw.js":
            self._send(200, (WEB_DIR / "sw.js").read_bytes(), "text/javascript; charset=utf-8")
        elif parsed.path in ("/icon-192.png", "/icon-512.png", "/icon-maskable.png"):
            self._send(200, (WEB_DIR / parsed.path[1:]).read_bytes(), "image/png")
        elif parsed.path == "/icon.svg":
            self._send(200, (WEB_DIR / "icon.svg").read_bytes(), "image/svg+xml")
        elif parsed.path == "/api/mangas":
            meta = md.load_meta()
            self._json(200, {"mangas": list_mangas(), "downloading": md.run_lock_busy(), "run": meta.get("run"), "now": time.time()})
        elif parsed.path == "/api/stats":
            self._json(200, stats())
        elif parsed.path == "/api/chapters":
            url = parse_qs(parsed.query).get("url", [""])[0]
            if url not in md.read_mangas_file():
                return self._json(404, {"error": "Manga not in the list."})
            self._json(200, {"chapters": list_chapter_files(url)})
        elif parsed.path in ("/api/pages", "/page"):
            query = parse_qs(parsed.query)
            url = query.get("url", [""])[0]
            path = chapter_paths(url).get(query.get("file", [""])[0]) if url in md.read_mangas_file() else None
            if not path:
                return self._json(404, {"error": "Chapter not found."})
            try:
                if parsed.path == "/api/pages":
                    return self._json(200, {"pages": len(chapter_pages(path))})
                data, ctype = read_page(path, int(query.get("i", ["-1"])[0]))
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except (ValueError, zipfile.BadZipFile) as e:
                self._json(404, {"error": str(e) or "Invalid EPUB."})
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
        if not self._authorized():
            return self._locked()
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
            if self.path in ("/api/history/add", "/api/history/remove"):
                try:
                    media_id = int(body.get("id"))
                except (TypeError, ValueError):
                    raise ValueError("Invalid manga.")
                if self.path.endswith("/add"):
                    try:
                        recommender.history_add(media_id, str(body.get("status", "read")))
                    except Exception:
                        raise ValueError("Could not reach AniList.")
                else:
                    recommender.history_remove(media_id)
                return self._json(200, {"ok": True})
            if self.path == "/api/recommend/resolve":
                names = [str(n) for n in body.get("names", []) if n][:6]
                found = recommender.resolve_mangapill(names) if names else None
                if not found:
                    raise ValueError("Could not find this manga on mangapill.")
                return self._json(200, {"url": found})
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
            if self.path == "/api/progress":
                file, page, total = str(body.get("file", "")), body.get("page"), body.get("total")
                if url not in md.read_mangas_file() or file not in chapter_paths(url):
                    raise ValueError("Chapter not found.")
                if not (isinstance(page, int) and isinstance(total, int) and 0 <= page < total):
                    raise ValueError("Invalid progress.")
                with data_lock:
                    progress = load_progress()
                    progress.setdefault(url, {})[file] = {"page": page, "total": total}
                    save_progress(progress)
                return self._json(200, {"ok": True})
            if self.path == "/api/chapter/read":
                if url not in md.read_mangas_file():
                    raise ValueError("Manga not in the list.")
                files = [str(f) for f in body.get("files", [])] or [str(body.get("file", ""))]
                for f in files:
                    delete_read_chapter(url, f)
                return self._json(200, {"deleted": files})
            if self.path == "/api/manga/pause":
                return self._json(200, set_flag(url, "paused", bool(body.get("value"))))
            if self.path == "/api/manga/complete":
                return self._json(200, set_flag(url, "completed", bool(body.get("value"))))
            if self.path == "/api/manga/queue":
                action = str(body.get("action", ""))
                if action not in ("add", "remove", "up", "down", "top"):
                    raise ValueError("Invalid action.")
                return self._json(200, edit_queue(url, action))
            if self.path == "/api/manga/retry":
                if url not in md.read_mangas_file():
                    raise ValueError("Manga not in the list.")
                set_flag(url, "completed", False)
                if url not in queued:
                    start_download(url, None)
                return self._json(200, {"queued": True})
            if self.path == "/api/check":
                if ALL not in queued:
                    start_download(ALL, None)
                return self._json(200, {"queued": True})
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
    threading.Thread(target=backfill_meta, daemon=True).start()
    print(f"Remote access token stored in {TOKEN_FILE.name}")
    print(f"Manga Kindle web app on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
