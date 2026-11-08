#!/usr/bin/env python3
"""
ytgrab - lightweight YouTube transcript + metadata grabber.

Give it a video, channel, playlist, or search URL (or a text file with one link
per line). For a video it saves that video's transcript; for a channel/playlist/
search it saves the N newest / first N / top N videos.
Each video produces two files in the output folder:

    <date>_<title>_[<id>].txt    transcript (with a short header)
    <date>_<title>_[<id>].json   metadata (title, channel, views, tags, ...)

No video/audio is ever downloaded and no ffmpeg is needed. Videos are processed
one at a time with a short pause between them to stay light on CPU, memory,
bandwidth and YouTube's rate limits. Already-saved videos are skipped, so you
can re-run with a bigger -n to continue where you left off.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlsplit

try:
    import yt_dlp
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing dependency ({exc.name}). Run ./install.sh or: pip install yt-dlp youtube-transcript-api")

DEFAULT_OUT = os.environ.get("YTGRAB_DIR", str(Path.home() / "yt_transcripts"))
DEFAULT_COUNT = 10

# Channel URLs without a tab (e.g. youtube.com/@name) list tabs, not videos;
# we point them at /videos so "N videos" means the N newest uploads.
CHANNEL_RE = re.compile(
    r"^(https?://)?(www\.|m\.)?youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)"
    r"(/(?P<tab>[^/?#]+))?/?([?#].*)?$",
    re.IGNORECASE,
)
PLAYLIST_RE = re.compile(r"youtube\.com/playlist\?", re.IGNORECASE)
SEARCH_RE = re.compile(r"youtube\.com/(results|search)", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"[\w-]{11}")
SEARCH_CAP = 100  # search results never end, so 'all' means this many for a search
BLOCK_COOLDOWN = 30  # seconds to wait after YouTube rate-limits a transcript request
BLOCK_LIMIT = 3      # stop the run after this many rate-limited videos in a row
LINK_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/|$)", re.IGNORECASE)


class Blocked(Exception):
    """YouTube keeps rate-limiting us; stop instead of making it worse."""


def is_block(exc):
    return (type(exc).__name__ in ("IpBlocked", "RequestBlocked")
            or getattr(exc, "status", None) == 429)


# --------------------------------------------------------------------------- utils

class _YdlLogger:
    """Silence yt-dlp unless --verbose: ytgrab reports failures itself, and the
    usual warnings (no JS runtime, retries, Python 3.10 notice) don't matter here."""

    def __init__(self, verbose):
        self.verbose = verbose

    def _out(self, msg):
        if self.verbose and not msg.startswith("[debug]"):
            print(f"  {msg}", file=sys.stderr)

    debug = info = warning = error = _out


def safe_name(text, max_len=90):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(text or "untitled"))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    return (text[:max_len].rstrip() or "untitled")


def hms(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def iso_date(yyyymmdd):
    if yyyymmdd and len(yyyymmdd) == 8:
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return None


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (str(default) if default is not None else "")


def parse_count(value):
    value = str(value).strip().lower()
    if value in ("all", "0", "*"):
        return 0
    n = int(value)
    if n < 0:
        raise ValueError
    return n


def search_url(query):
    return "https://www.youtube.com/results?search_query=" + quote_plus(query)


def normalize_search(url):
    """Real search URLs (results?search_query=...) are kept as-is so filters like
    &sp=... still apply. Looser forms like youtube.com/search="funny cats" or
    youtube.com/search?q=funny+cats are rewritten into a real search URL."""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    if qs.get("search_query"):
        return url
    query = (qs.get("q") or qs.get("search") or [""])[0]
    if not query:
        m = re.search(r"search[=/](.+)$", unquote_plus(url))
        query = m.group(1) if m else ""
    query = query.strip().strip("\"'").strip()
    return search_url(query) if query else None


def classify(url):
    """Return (kind, normalized_url) where kind is video|channel|playlist|search|invalid."""
    if url.lower().startswith("search:"):
        query = url.split(":", 1)[1].strip().strip("\"'").strip()
        return ("search", search_url(query)) if query else ("invalid", url)
    if not LINK_RE.match(url):
        return "invalid", url
    m = CHANNEL_RE.match(url)
    if m:
        if not m.group("tab"):
            base = url.split("?")[0].split("#")[0].rstrip("/")
            url = base + "/videos"
        return "channel", url
    if PLAYLIST_RE.search(url):
        return "playlist", url
    if SEARCH_RE.search(url):
        norm = normalize_search(url)
        return ("search", norm) if norm else ("invalid", url)
    return "video", url


def read_links_file(path):
    """One link per line. Blank lines and lines starting with # are ignored.
    Handles Windows line endings / BOM (files made in Notepad)."""
    with open(os.path.expanduser(path), encoding="utf-8-sig") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def expand_inputs(items, files):
    """Turn CLI args + -f files into a de-duplicated list of links. An argument
    that is an existing file path is read as a links file too."""
    out = []
    for path in files:
        out += read_links_file(path)
    for item in items:
        item = item.strip()
        if item and os.path.isfile(os.path.expanduser(item)):
            out += read_links_file(item)
        elif item:
            out.append(item)
    return list(dict.fromkeys(out))


def lang_label(transcript):
    name = transcript["language"]
    if transcript["is_generated"] and "auto" not in name.lower():
        name += " (auto-generated)"
    return name


def lang_match(code, wanted):
    code, wanted = code.lower(), wanted.lower()
    return code == wanted or code.startswith(wanted + "-")


# ---------------------------------------------------------------------- transcripts

def transcript_via_api(api, video_id, langs):
    """Primary method: youtube-transcript-api (one small request, no JS)."""
    listing = api.list(video_id)
    try:
        tr = listing.find_transcript(langs)  # prefers manual over auto, in lang order
    except Exception:
        tr = next(iter(listing), None)       # fall back to whatever exists
        if tr is None:
            return None
    fetched = tr.fetch()
    segments = [{"start": round(s.start, 2), "duration": round(s.duration, 2), "text": s.text}
                for s in fetched]
    return {
        "language": tr.language,
        "language_code": tr.language_code,
        "is_generated": tr.is_generated,
        "source": "youtube-transcript-api",
        "segments": segments,
    }


def transcript_via_ytdlp(ydl, info, langs):
    """Fallback: caption tracks yt-dlp already found while reading metadata."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    manual = {k: v for k, v in manual.items() if k != "live_chat"}

    def pick(tracks_by_lang, generated):
        for want in langs:
            # auto captions: prefer the original (non-translated) track
            keys = sorted(tracks_by_lang, key=lambda k: (not k.endswith("-orig"), k))
            for key in keys:
                if lang_match(key, want):
                    return key, tracks_by_lang[key], generated
        return None

    choice = pick(manual, False) or pick(auto, True)
    if not choice and manual:
        key = next(iter(manual))
        choice = (key, manual[key], False)
    if not choice:
        orig = [k for k in auto if k.endswith("-orig")]
        if orig:
            choice = (orig[0], auto[orig[0]], True)
    if not choice:
        return None

    key, tracks, generated = choice
    track = next((t for t in tracks if t.get("ext") == "json3"), None)
    if not track:
        return None
    data = json.loads(ydl.urlopen(track["url"]).read())
    segments = []
    for ev in data.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in ev.get("segs") or []).strip()
        if text:
            segments.append({"start": round(ev.get("tStartMs", 0) / 1000, 2),
                             "duration": round(ev.get("dDurationMs", 0) / 1000, 2),
                             "text": text})
    if not segments:
        return None
    return {
        "language": track.get("name") or key,
        "language_code": key.replace("-orig", ""),
        "is_generated": generated,
        "source": "yt-dlp",
        "segments": segments,
    }


def format_transcript(segments, timestamps):
    if timestamps:
        return "\n".join(f"[{hms(s['start'])}] {s['text']}" for s in segments) + "\n"
    # Plain text: join caption lines into paragraphs (~45s each, or at long pauses).
    paragraphs, current, para_start, prev_end = [], [], 0.0, 0.0
    for s in segments:
        text = " ".join(s["text"].split())
        if not text:
            continue
        long_pause = current and s["start"] - prev_end > 3.0
        too_long = current and s["start"] - para_start > 45
        if long_pause or too_long:
            paragraphs.append(" ".join(current))
            current, para_start = [], s["start"]
        if not current:
            para_start = s["start"]
        current.append(text)
        prev_end = s["start"] + s["duration"]
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) + "\n"


# ------------------------------------------------------------------------ metadata

META_KEYS = [
    "id", "title", "webpage_url", "channel", "channel_id", "channel_url", "uploader",
    "uploader_id", "channel_follower_count", "upload_date", "timestamp", "duration",
    "duration_string", "view_count", "like_count", "comment_count", "categories", "tags",
    "description", "chapters", "language", "live_status", "was_live", "age_limit",
    "availability", "thumbnail",
]


def build_metadata(info, transcript, source):
    meta = {k: info.get(k) for k in META_KEYS}
    meta["upload_date"] = iso_date(info.get("upload_date")) or info.get("upload_date")
    meta["chapters"] = [
        {"title": c.get("title"), "start": c.get("start_time"), "end": c.get("end_time")}
        for c in (info.get("chapters") or [])
    ] or None
    if transcript:
        text = " ".join(s["text"] for s in transcript["segments"])
        meta["transcript"] = {
            "language": transcript["language"],
            "language_code": transcript["language_code"],
            "is_generated": transcript["is_generated"],
            "fetched_with": transcript["source"],
            "segments": len(transcript["segments"]),
            "words": len(text.split()),
        }
    else:
        meta["transcript"] = None
    meta["grabbed_from"] = source
    meta["grabbed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return meta


# --------------------------------------------------------------------------- core

class Grabber:
    def __init__(self, args):
        self.args = args
        self.out_root = Path(args.output).expanduser().resolve()
        self.langs = [l.strip() for l in args.lang.split(",") if l.strip()] or ["en"]
        base = {
            "quiet": True,
            "no_warnings": not args.verbose,
            "skip_download": True,
            "logger": _YdlLogger(args.verbose),
            "noplaylist": True,          # watch?v=X&list=Y -> just the video
            "retries": 3,
            "socket_timeout": 20,
            # don't fetch stream manifests we never use
            "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
        }
        if args.cookies:
            base["cookiefile"] = os.path.expanduser(args.cookies)
        self.ydl = yt_dlp.YoutubeDL(base)
        self.api = YouTubeTranscriptApi()
        self.stats = {"saved": 0, "skipped": 0, "no_transcript": 0, "blocked": 0, "failed": 0}
        self._last_fetch = float("-inf")
        self._blocks_in_row = 0

    def close(self):
        self.ydl.close()

    def throttle(self):
        """Keep at least --sleep seconds between videos, whatever the input mix."""
        wait = self.args.sleep - (time.monotonic() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        self._last_fetch = time.monotonic()

    # ---- listing

    def list_videos(self, url, limit):
        """Flat listing: one request per ~100 videos, no per-video page loads."""
        opts = {"extract_flat": "in_playlist", "noplaylist": False}
        if limit:
            opts["playlistend"] = limit
        with yt_dlp.YoutubeDL({**self.ydl.params, **opts}) as ydl:
            info = ydl.extract_info(url, download=False)
        # keep only videos (searches can mix in channels/playlists)
        entries = [e for e in (info.get("entries") or [])
                   if e and VIDEO_ID_RE.fullmatch(str(e.get("id") or ""))]
        if limit:
            entries = entries[:limit]
        return info, entries

    # ---- per video

    def existing(self, folder, video_id):
        """A video counts as done once its transcript .txt exists. Videos saved
        with metadata only (no transcript, e.g. temporarily blocked) are retried."""
        if not folder.exists():
            return None
        for p in folder.glob(f"*[[]{video_id}[]].txt"):
            return p
        return None

    def grab_video(self, video_url, folder, source, label=""):
        args = self.args
        vid_hint = video_url.rsplit("=", 1)[-1] if "watch?v=" in video_url else None
        if vid_hint and not args.overwrite and folder and self.existing(folder, vid_hint):
            print(f"{label}skip  {vid_hint} (already saved)")
            self.stats["skipped"] += 1
            return

        self.throttle()
        try:
            info = self.ydl.extract_info(video_url, download=False)
        except Exception as exc:
            print(f"{label}FAIL  {video_url}: {str(exc).splitlines()[0]}")
            self.stats["failed"] += 1
            return

        vid = info["id"]
        if folder is None:  # single video: group by channel
            folder = self.out_root if args.flat else self.out_root / safe_name(info.get("channel") or info.get("uploader") or "Unknown channel")
        if not args.overwrite and self.existing(folder, vid):
            print(f"{label}skip  {vid} (already saved)")
            self.stats["skipped"] += 1
            return

        transcript, err, blocked = None, None, False
        try:
            transcript = transcript_via_api(self.api, vid, self.langs)
        except Exception as exc:
            err, blocked = type(exc).__name__, is_block(exc)
        if transcript is None:
            try:
                transcript = transcript_via_ytdlp(self.ydl, info, self.langs)
            except Exception as exc:
                err = (err + " / " if err else "") + type(exc).__name__
                blocked = blocked or is_block(exc)
        if transcript:
            blocked = False

        folder.mkdir(parents=True, exist_ok=True)
        date = iso_date(info.get("upload_date")) or "nodate"
        stem = f"{date}_{safe_name(info.get('title'))}_[{vid}]"
        if not transcript and self.existing(folder, vid):
            # --overwrite run that failed to get a transcript: keep the old files intact
            print(f"{label}{'block' if blocked else 'nosub'} {vid}  kept previously saved files  ({err or 'no captions'})")
            self.stats["blocked" if blocked else "no_transcript"] += 1
            self._after_block_check(blocked)
            return
        meta = build_metadata(info, transcript, source)

        if transcript:
            header = [
                f"Title:    {info.get('title')}",
                f"Channel:  {info.get('channel') or info.get('uploader')}",
                f"URL:      {info.get('webpage_url')}",
                f"Uploaded: {iso_date(info.get('upload_date')) or 'unknown'}",
                f"Duration: {hms(info.get('duration'))}",
                f"Language: {lang_label(transcript)}",
                "-" * 60,
                "",
                "",
            ]
            body = format_transcript(transcript["segments"], args.timestamps)
            (folder / f"{stem}.txt").write_text("\n".join(header) + body, encoding="utf-8")
            self.stats["saved"] += 1
            status = "ok   "
        elif blocked:
            self.stats["blocked"] += 1
            status = "block"
        else:
            self.stats["no_transcript"] += 1
            status = "nosub"
        (folder / f"{stem}.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        tag = f"[{transcript['language_code']}{' auto' if transcript['is_generated'] else ''}]  " if transcript else ""
        extra = "  (rate-limited by YouTube)" if blocked else (f"  ({err})" if not transcript and err else "")
        print(f"{label}{status} {vid}  {tag}{info.get('title')}{extra}")
        self._after_block_check(blocked)

    def _after_block_check(self, blocked):
        if not blocked:
            self._blocks_in_row = 0
            return
        self._blocks_in_row += 1
        if self._blocks_in_row >= BLOCK_LIMIT:
            raise Blocked()
        print(f"      YouTube is rate-limiting; cooling down {BLOCK_COOLDOWN}s...")
        time.sleep(BLOCK_COOLDOWN)

    # ---- entry point per URL

    def run(self, url, limit):
        kind, url = classify(url)
        if kind == "invalid":
            print(f"FAIL  don't understand {url!r} (expected a YouTube link or 'search: words')")
            self.stats["failed"] += 1
            return
        if kind == "video":
            self.grab_video(url, None, {"type": "video", "url": url})
            return
        if kind == "search" and not limit:
            limit = SEARCH_CAP
            print(f"Note: searches never end; 'all' means the top {SEARCH_CAP} results.")

        print(f"Listing {kind}: {url}")
        try:
            info, entries = self.list_videos(url, limit)
        except Exception as exc:
            print(f"FAIL  could not list {url}: {str(exc).splitlines()[0]}")
            self.stats["failed"] += 1
            return

        if kind == "channel":
            name = info.get("channel") or info.get("uploader") or info.get("title")
        elif kind == "search":
            name = f"Search - {info.get('title') or info.get('id')}"
        else:
            name = info.get("title")
        folder = self.out_root if self.args.flat else self.out_root / safe_name(name or kind)
        source = {"type": kind, "url": url, "title": info.get("title"), "id": info.get("id")}
        total = len(entries)
        print(f"{name}: {total} video(s) -> {folder}")

        for i, entry in enumerate(entries, 1):
            vurl = f"https://www.youtube.com/watch?v={entry['id']}"
            self.grab_video(vurl, folder, source, label=f"[{i}/{total}] ")


# ---------------------------------------------------------------------------- CLI

def main():
    p = argparse.ArgumentParser(
        prog="ytgrab",
        description="Save YouTube transcripts + metadata for a video, channel, playlist, or search.",
        epilog="Run with no arguments for interactive prompts. "
               "Links file: one link per line; blank lines and lines starting with # are ignored.",
    )
    p.add_argument("urls", nargs="*",
                   help="video/channel/playlist/search URL(s), 'search: some words', or a links file")
    p.add_argument("-f", "--file", action="append", default=[],
                   help="text file with one link per line (can be given more than once)")
    p.add_argument("-o", "--output", help=f"output folder (default: $YTGRAB_DIR or {DEFAULT_OUT})")
    p.add_argument("-n", "--count", help=f"videos to grab per channel/playlist/search; 'all' for every video (default: ask, or {DEFAULT_COUNT})")
    p.add_argument("-l", "--lang", default="en", help="preferred transcript languages, comma separated (default: en)")
    p.add_argument("-t", "--timestamps", action="store_true", help="write one timestamped line per caption instead of paragraphs")
    p.add_argument("-s", "--sleep", type=float, default=1.5, help="seconds to pause between videos (default: 1.5)")
    p.add_argument("--flat", action="store_true", help="save directly in the output folder (no channel/playlist subfolder)")
    p.add_argument("--overwrite", action="store_true", help="re-grab videos that were already saved")
    p.add_argument("--cookies", help="cookies.txt (Netscape format) for age-restricted/members videos")
    p.add_argument("-v", "--verbose", action="store_true", help="show yt-dlp messages")
    args = p.parse_args()

    interactive = sys.stdin.isatty()
    raw = args.urls
    if not raw and not args.file:
        if not interactive:
            p.error("no URL or links file given")
        raw = [ask("YouTube URL, 'search: some words', or path to a links file")]
    for path in args.file:
        if not os.path.isfile(os.path.expanduser(path)):
            p.error(f"links file not found: {path}")
    try:
        urls = expand_inputs(raw, args.file)
    except (OSError, UnicodeDecodeError) as exc:
        p.error(f"could not read links file: {exc}")
    if not urls:
        p.error("no links given (links file empty?)")

    if not args.output:
        args.output = ask("Save to folder", DEFAULT_OUT) if interactive else DEFAULT_OUT

    needs_count = any(classify(u)[0] in ("channel", "playlist", "search") for u in urls)
    if args.count is None:
        args.count = ask("How many videos per channel/playlist/search ('all' for every one)", DEFAULT_COUNT) \
            if (needs_count and interactive) else DEFAULT_COUNT
    try:
        limit = parse_count(args.count)
    except ValueError:
        p.error(f"invalid count: {args.count!r} (use a number or 'all')")

    g = Grabber(args)
    print(f"Output: {g.out_root}")
    if len(urls) > 1:
        print(f"{len(urls)} links to process")
    try:
        for n, u in enumerate(urls, 1):
            if len(urls) > 1:
                print(f"\n=== link {n}/{len(urls)}: {u}")
            g.run(u, limit)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Blocked:
        print(f"\nSTOPPED: YouTube is blocking transcript requests from this IP "
              f"({BLOCK_LIMIT} videos in a row got HTTP 429 / IpBlocked).\n"
              "Everything saved so far is kept. Re-run the same command later (blocks usually\n"
              "lift within a few hours) and it continues where it left off. A bigger pause,\n"
              "e.g. -s 5, makes this less likely.")
    finally:
        g.close()

    s = g.stats
    print(f"\nDone. saved={s['saved']}  no_transcript={s['no_transcript']}  blocked={s['blocked']}  "
          f"skipped={s['skipped']}  failed={s['failed']}")
    sys.exit(2 if s["blocked"] else 1 if s["failed"] else 0)


if __name__ == "__main__":
    main()
