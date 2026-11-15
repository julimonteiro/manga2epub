#!/usr/bin/env python3
"""
Manga recommendations from what is in the library.

Hybrid approach, the same two ideas the open-source recommenders use:
  * collaborative signal: AniList's community "recommendations" (users vote that
    "if you liked A, read B"), pulled for every manga in the library. This is what
    timschneeb/mitsukeru does with an AniList profile.
  * content signal: TF-IDF weighted tags/genres compared with cosine similarity
    (as in csiv23/MangaMatch and the MangaDex "similar" projects), against a taste
    profile built from the library.
Each library manga counts proportionally to how much of it was read (chapters
downloaded), so the series you follow the most steer the result.

Data comes from the public AniList GraphQL API (no key). Results are cached in
recs_cache.json and rebuilt in the background when the library changes.
"""

import difflib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import manga_downloader as md

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "recs_cache.json"
HISTORY_FILE = ROOT / "read_history.json"  # mangas read before / elsewhere; feeds the taste profile and hides them from suggestions
HIST_WEIGHT = 1.8
# A dropped manga says little about its genre: liking other rom-coms is fine. Only what is
# *specific* to it counts against a suggestion, and it never adds up across dropped mangas.
DROP_TRAITS = 0.30      # tags the dropped manga has that your liked mangas do not
DROP_AUTHOR = 0.12      # same author / artist
DROP_PAIRED = 0.06      # the community pairs it directly with the dropped manga
CACHE_VERSION = 2
ANILIST = "https://graphql.anilist.co"
META_TTL = 14 * 86400   # AniList data per library manga
RESULT_TTL = 12 * 3600  # recommendation list
MAX_RESULTS = 30
FRESH_DAYS = 14         # unread chapters newer than this still count as "following"

# tags that nearly every series has; they only add noise to the taste profile
GENERIC_TAGS = {"Heterosexual", "Male Protagonist", "Female Protagonist", "Primarily Teen Cast", "Primarily Adult Cast",
                "Primarily Female Cast", "Primarily Male Cast", "Ensemble Cast", "Full Color", "Cast Main Cast"}

_FIELDS = "id title{romaji english} synonyms format genres isAdult popularity averageScore coverImage{large} siteUrl tags{name rank isMediaSpoiler} staff(perPage:4){nodes{id}}"
SEARCH_Q = "query($s:String){Page(perPage:6){media(search:$s,type:MANGA,sort:SEARCH_MATCH){%s}}}" % _FIELDS
RECS_Q = (
    "query($id:Int){Media(id:$id){recommendations(sort:RATING_DESC,perPage:25){nodes{rating "
    "mediaRecommendation{%s type}}}}}" % _FIELDS
)

_lock = threading.Lock()
_building = False
_error = ""


def _post(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(ANILIST, body, {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "manga-kindle/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "30")) + 1)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2)
    raise RuntimeError("AniList is rate limiting requests")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _names(m: dict) -> list[str]:
    t = m.get("title") or {}
    return [x for x in [t.get("romaji"), t.get("english"), *(m.get("synonyms") or [])] if x]


def _slim(m: dict) -> dict:
    t = m.get("title") or {}
    return {
        "id": m["id"],
        "title": t.get("romaji") or t.get("english") or "?",
        "alt": t.get("english") or "",
        "names": _names(m)[:6],
        "genres": m.get("genres") or [],
        "tags": [{"n": x["name"], "r": x["rank"]} for x in (m.get("tags") or []) if not x.get("isMediaSpoiler") and x["rank"] >= 30 and x["name"] not in GENERIC_TAGS],
        "score": m.get("averageScore") or 0,
        "pop": m.get("popularity") or 0,
        "cover": (m.get("coverImage") or {}).get("large", ""),
        "url": m.get("siteUrl", ""),
        "adult": bool(m.get("isAdult")),
        "staff": [n["id"] for n in ((m.get("staff") or {}).get("nodes") or [])],
    }


def _match(title: str, candidates: list[dict]) -> dict | None:
    """The AniList entry that best matches a library title (spin-offs and light novels rank lower)."""
    want = _norm(title)
    best, best_s = None, 0.0
    for m in candidates:
        if m.get("format") not in ("MANGA", "ONE_SHOT", None):
            continue
        s = max((difflib.SequenceMatcher(None, want, _norm(n)).ratio() for n in _names(m)), default=0)
        s += min(math.log10(1 + (m.get("popularity") or 0)) / 40, 0.1)  # tie-break: the main series is the popular one
        if s > best_s:
            best, best_s = m, s
    return best if best_s >= 0.62 else None


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(CACHE_FILE)


def _library() -> list[dict]:
    """Mangas that are actually being followed.

    A chapter that was read gets its EPUB deleted, so EPUBs still on disk are unread. A manga
    with chapters that have been sitting unread for a while was dropped (or never started) and
    does not shape the taste profile; chapters that just arrived do not count against it.
    Weight grows with the chapters read (downloaded minus the ones still waiting)."""
    state = md.load_state()
    now = time.time()
    out = []
    for url in md.read_mangas_file():
        title = state.get(url, {}).get("title") or url.rstrip("/").split("/")[-1].replace("-", " ").title()
        unread = list(chapter_files(title))
        stale = sum(1 for f in unread if now - f.stat().st_mtime > FRESH_DAYS * 86400)
        read = len(state.get(url, {}).get("chapters", [])) - len(unread)
        out.append({
            "url": url, "title": title, "weight": 1 + math.log1p(max(read, 0)),
            "active": stale == 0 and read > 0,
        })
    return out


def chapter_files(title: str):
    safe = md.sanitize_filename(title)
    for folder in (safe, f"{md.NEW_FOLDER_PREFIX}{safe}"):
        base = Path(md.OUTPUT_DIR) / folder
        if base.is_dir():
            yield from base.glob("*.epub")


def _signature(lib: list[dict]) -> str:
    hist = ",".join(f'{h["id"]}{h.get("status", "read")[0]}' for h in load_history())
    return "|".join(sorted(m["url"] + ("+" if m["active"] else "-") for m in lib)) + "#" + hist


# ── read history ──
def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save_history(items: list[dict]):
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    tmp.replace(HISTORY_FILE)


def history_add(media_id: int, status: str = "read") -> dict:
    """Mark an AniList manga as read, or as dropped (never suggested, and similar ones are pushed down)."""
    status = "dropped" if status == "dropped" else "read"
    with _lock:
        items = load_history()
        for h in items:
            if h["id"] == media_id:
                if h.get("status", "read") != status:
                    h["status"] = status
                    _save_history(items)
                return h
    data = _post("query($id:Int){Media(id:$id,type:MANGA){%s}}" % _FIELDS, {"id": media_id})["Media"]
    entry = {**_slim(data), "added": time.time(), "status": status}
    with _lock:
        items = load_history()
        if not any(h["id"] == media_id for h in items):
            items.insert(0, entry)
            _save_history(items)
    return entry


def history_remove(media_id: int):
    with _lock:
        _save_history([h for h in load_history() if h["id"] != media_id])


def history_add_title(title: str):
    """Best effort: remember a manga by title (used when a read manga leaves the library)."""
    try:
        found = _match(title, _post(SEARCH_Q, {"s": title})["Page"]["media"])
        if found:
            history_add(found["id"])
    except Exception:
        pass


def search_anilist(q: str) -> list[dict]:
    """Manga search for the 'I already read this' picker."""
    out = []
    for m in _post(SEARCH_Q, {"s": q})["Page"]["media"]:
        if m.get("format") in ("MANGA", "ONE_SHOT", None) and not m.get("isAdult"):
            x = _slim(m)
            out.append({"id": x["id"], "title": x["title"], "alt": x["alt"], "cover": x["cover"], "genres": x["genres"][:3]})
    return out


def _vector(m: dict, idf: dict) -> dict:
    v = {}
    for g in m["genres"]:
        v["g:" + g] = 1.0 * idf.get("g:" + g, 1.0)
    for t in m["tags"]:
        v["t:" + t["n"]] = (t["r"] / 100) * idf.get("t:" + t["n"], 1.0)
    return v


def _cosine(a: dict, b: dict) -> float:
    dot = sum(x * b.get(k, 0) for k, x in a.items())
    na = math.sqrt(sum(x * x for x in a.values()))
    nb = math.sqrt(sum(x * x for x in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def build() -> dict:
    """Fetch AniList data for the library and compute the recommendation list."""
    lib = _library()
    cache = _load_cache()
    if cache.get("v") != CACHE_VERSION:  # data shape changed: refetch
        cache = {}
    metas = cache.get("metas", {})
    now = time.time()

    # 1) library manga -> AniList entry + its community recommendations
    known = {}
    for m in lib:
        entry = metas.get(m["url"])
        if not entry or now - entry.get("ts", 0) > META_TTL or entry.get("title") != m["title"]:
            entry = {"ts": now, "title": m["title"], "media": None, "recs": []}
            found = _match(m["title"], _post(SEARCH_Q, {"s": m["title"]})["Page"]["media"])
            time.sleep(0.7)
            if found:
                entry["media"] = _slim(found)
                nodes = _post(RECS_Q, {"id": found["id"]})["Media"]["recommendations"]["nodes"]
                time.sleep(0.7)
                for n in nodes:
                    r = n.get("mediaRecommendation")
                    if r and n["rating"] and n["rating"] > 0 and r.get("type") == "MANGA":
                        entry["recs"].append({"rating": n["rating"], **_slim(r)})
            metas[m["url"]] = entry
        known[m["url"]] = entry
    metas = {u: e for u, e in metas.items() if u in known}

    # 1b) manga marked as read: same treatment as followed ones, with a fixed weight
    hmetas = cache.get("hmetas", {})
    hist = load_history()
    refreshed = False
    for h in hist:
        if h.get("status") == "dropped" and "staff" not in h:  # added before authors were tracked
            h.update({k: v for k, v in _slim(_post("query($id:Int){Media(id:$id,type:MANGA){%s}}" % _FIELDS, {"id": h["id"]})["Media"]).items()})
            refreshed = True
            time.sleep(0.7)
    if refreshed:
        with _lock:
            _save_history(hist)
    for h in hist:
        k = str(h["id"])
        if k not in hmetas or now - hmetas[k].get("ts", 0) > META_TTL:
            nodes = _post(RECS_Q, {"id": h["id"]})["Media"]["recommendations"]["nodes"]
            time.sleep(0.7)
            hmetas[k] = {"ts": now, "recs": [
                {"rating": n["rating"], **_slim(n["mediaRecommendation"])} for n in nodes
                if n.get("mediaRecommendation") and n["rating"] and n["rating"] > 0 and n["mediaRecommendation"].get("type") == "MANGA"]}
    hmetas = {k: v for k, v in hmetas.items() if k in {str(h["id"]) for h in hist}}
    for h in hist:
        known["h:%d" % h["id"]] = {"media": h, "recs": hmetas[str(h["id"])]["recs"]}

    mine = {e["media"]["id"] for e in known.values() if e["media"]}  # includes the read history
    weights = {m["url"]: m["weight"] for m in lib if m["active"]}  # dropped / backlogged mangas do not steer the taste
    weights.update({"h:%d" % h["id"]: HIST_WEIGHT for h in hist if h.get("status", "read") == "read"})
    dropped = [known["h:%d" % h["id"]] for h in hist if h.get("status") == "dropped"]

    # 2) candidates: everything the community recommends for the library, minus what is already in it
    cands: dict[int, dict] = {}
    for url, e in known.items():
        if not e["media"] or url not in weights:
            continue
        for r in e["recs"]:
            if r["id"] in mine or r["adult"]:
                continue
            c = cands.setdefault(r["id"], {**r, "collab": 0.0, "from": {}})
            c["collab"] += weights[url] * math.log1p(r["rating"])
            c["from"][e["media"]["title"]] = c["from"].get(e["media"]["title"], 0) + r["rating"]

    # 3) content side: TF-IDF over library + candidates, taste profile = weighted library vectors
    docs = [e["media"] for u, e in known.items() if e["media"] and u in weights] + list(cands.values()) + [d["media"] for d in dropped]
    df: dict[str, int] = {}
    for d in docs:
        for k in {"g:" + g for g in d["genres"]} | {"t:" + t["n"] for t in d["tags"]}:
            df[k] = df.get(k, 0) + 1
    idf = {k: math.log((1 + len(docs)) / (1 + n)) + 1 for k, n in df.items()}
    profile: dict[str, float] = {}
    for url, e in known.items():
        if e["media"] and url in weights:
            for k, x in _vector(e["media"], idf).items():
                profile[k] = profile.get(k, 0) + x * weights[url]
    top_tags = [k[2:] for k, _ in sorted(profile.items(), key=lambda kv: -kv[1]) if k.startswith("t:")][:6]

    # dropped manga as a narrow negative: only what sets it apart from what you like counts, per manga
    liked_max = max(profile.values(), default=1) or 1
    drops = []
    for d in dropped:
        dv = _vector(d["media"], idf)
        dn = math.sqrt(sum(x * x for x in dv.values())) or 1
        # a trait that your liked mangas share (the genre, the tone) is cancelled out; the rest is what put you off
        traits = {k: x * (1 - min(profile.get(k, 0) / liked_max, 1)) ** 2 for k, x in dv.items()}
        drops.append({
            "traits": traits, "norm": dn, "staff": set(d["media"].get("staff", [])),
            "paired": {r["id"]: math.log1p(r["rating"]) for r in d["recs"]},
        })
    max_paired = max((v for d in drops for v in d["paired"].values()), default=1) or 1

    max_collab = max((c["collab"] for c in cands.values()), default=1) or 1
    results = []
    for c in cands.values():
        content = _cosine(_vector(c, idf), profile)
        final = 0.6 * (c["collab"] / max_collab) + 0.4 * content
        if drops:
            cv = _vector(c, idf)
            cn = math.sqrt(sum(x * x for x in cv.values())) or 1
            worst = 0.0
            for d in drops:  # the strongest single dropped manga decides, they do not pile up
                traits = sum(x * d["traits"].get(k, 0) for k, x in cv.items()) / (cn * d["norm"])
                author = 1.0 if d["staff"] & set(c["staff"]) else 0.0
                paired = d["paired"].get(c["id"], 0) / max_paired
                worst = max(worst, DROP_TRAITS * traits + DROP_AUTHOR * author + DROP_PAIRED * paired)
            final -= worst
        # light nudge towards titles people actually rate well, without drowning niche picks
        final *= 0.9 + 0.1 * (c["score"] / 100)
        shared = [t for t in top_tags if t in {x["n"] for x in c["tags"]} or t in c["genres"]][:3]
        reasons = [t for t, _ in sorted(c["from"].items(), key=lambda kv: -kv[1])[:2]]
        results.append({
            "id": c["id"], "title": c["title"], "alt": c["alt"], "names": c["names"], "cover": c["cover"], "url": c["url"],
            "genres": c["genres"][:3], "score": c["score"], "match": round(min(final / 0.7, 1) * 100),
            "because": reasons, "shared": shared, "_f": final,
        })
    results.sort(key=lambda r: -r["_f"])
    for r in results:
        del r["_f"]

    out = {
        "built": now, "sig": _signature(lib), "items": results[:MAX_RESULTS],
        "matched": sum(1 for u, e in known.items() if e["media"] and u in weights), "library": len(weights) - len(hist), "taste": top_tags,
        "metas": metas, "hmetas": hmetas, "v": CACHE_VERSION,
    }
    _save_cache(out)
    return out


def _build_bg():
    global _building, _error
    try:
        build()
        _error = ""
    except Exception as e:  # network down, AniList hiccup: keep the old list
        _error = str(e)
    finally:
        _building = False


def recommendations() -> dict:
    """Cached recommendations; starts a background rebuild when they are stale."""
    global _building
    lib = _library()
    cache = _load_cache()
    fresh = cache and cache.get("sig") == _signature(lib) and time.time() - cache.get("built", 0) < RESULT_TTL
    with _lock:
        if not fresh and not _building and (lib or load_history()):
            _building = True
            threading.Thread(target=_build_bg, daemon=True).start()
        building = _building
    read_ids = {h["id"] for h in load_history()}
    return {
        "building": building, "error": _error, "items": [i for i in cache.get("items", []) if i["id"] not in read_ids],
        "history": sum(1 for h in load_history() if h.get("status", "read") == "read"),
        "dropped": sum(1 for h in load_history() if h.get("status") == "dropped"),
        "matched": cache.get("matched", 0), "library": sum(1 for m in lib if m["active"]), "taste": cache.get("taste", []),
    }


def _title_score(text: str, name: str) -> float:
    """mangapill lists "Main title Alt title,Other alt", so a name inside the text counts as a match."""
    t, n = _norm(text), _norm(name)
    if len(n) >= 4 and (t == n or t.startswith(n + " ")):
        return 1.0
    if len(n) >= 6 and f" {n} " in f" {t} ":
        return 0.85
    return difflib.SequenceMatcher(None, t, n).ratio()


def resolve_mangapill(names: list[str]) -> str | None:
    """Find the mangapill.com page of a recommended manga so it can be added to the library."""
    best, best_s = None, 0.0
    for q in names[:3]:
        soup = md.fetch_page("https://mangapill.com/search?q=" + urllib.request.quote(q) + "&type=&status=")
        if not soup:
            continue
        for a in soup.select('a[href^="/manga/"]'):
            text = a.get_text(" ", strip=True)
            if not text:
                continue
            s = max(_title_score(text, n) for n in names)
            if s > best_s:
                best, best_s = "https://mangapill.com" + a["href"], s
        if best_s >= 0.9:
            break
    return best if best_s >= 0.8 else None


if __name__ == "__main__":
    r = build()
    print(r["matched"], "/", r["library"], "followed mangas matched; taste:", r["taste"])
    for m in _library():
        print("  ", "FOLLOW" if m["active"] else "skip  ", m["title"])
    for i in r["items"][:15]:
        print(f'{i["match"]:3d}% {i["title"]}  <- {", ".join(i["because"])}  [{", ".join(i["shared"])}]')
