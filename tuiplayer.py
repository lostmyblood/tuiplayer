#!/usr/bin/env python3
"""
tuiplayer - a barebones keyboard-driven TUI music player.

Search races several free public sources concurrently and uses whichever
responds first. Playback tries to start fast (with a warmed-up prefetch
session for the next track) via, in order:

  1. InnerTube direct-URL clients (limusic style) - stream URLs straight
     from YouTube's internal API, validated in mpv before use
  2. yt-dlp URL resolution
  3. a guaranteed yt-dlp -> mpv pipe fallback

Keys:
  tab         toggle focus: search box <-> results
  enter       search (search box) / play selected & prefetch next (list)
  up/down     move selection (j/k in the list)
  s           stop playback
  n           next track
  q / esc     quit (esc first clears the search box)
"""
import curses
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "0.3.0"

# ---------------------------------------------------------------- sources ---

SEARCH_SOURCES = [
    ("piped", "https://pipedapi.kavin.rocks"),
    ("piped", "https://pipedapi.adminforge.de"),
    ("piped", "https://api.piped.private.coffee"),
    ("invidious", "https://inv.nadeko.net"),
    ("invidious", "https://invidious.fdn.fr"),
    ("invidious", "https://yewtu.be"),
]


def _json(url, timeout=7):
    req = urllib.request.Request(url, headers={"User-Agent": f"tuiplayer/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _search_piped(base, q):
    data = _json(f"{base}/search?q={urllib.parse.quote_plus(q)}&filter=music_songs")
    out = []
    for it in data.get("items", []):
        out.append({
            "title": it.get("title", "").strip(),
            "channel": it.get("uploaderName", ""),
            "url": "https://www.youtube.com" + it.get("url", ""),
            "dur": it.get("duration", 0),
        })
    return out


def _search_invidious(base, q):
    data = _json(f"{base}/api/v1/search?q={urllib.parse.quote_plus(q)}&type=video")
    out = []
    for it in data:
        out.append({
            "title": it.get("title", "").strip(),
            "channel": it.get("author", ""),
            "url": f"https://www.youtube.com/watch?v={it.get('videoId')}",
            "dur": it.get("lengthSeconds", 0),
        })
    return out


def _search_ytdlp(q, limit=12):
    """Fallback: plain yt-dlp search."""
    out = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", f"ytsearch{limit}:{q}"],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(out.stdout or "{}")
    res = []
    for e in data.get("entries") or []:
        url = e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
        res.append({
            "title": (e.get("title") or "untitled").strip(),
            "channel": e.get("channel") or e.get("uploader") or "",
            "url": url,
            "dur": e.get("duration") or 0,
        })
    return res


def search(q):
    """Race all sources; first one to return results wins."""
    with ThreadPoolExecutor(max_workers=len(SEARCH_SOURCES)) as ex:
        futs = [
            ex.submit(_search_piped if kind == "piped" else _search_invidious, base, q)
            for kind, base in SEARCH_SOURCES
        ]
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception:
                continue
            if res:
                return res
    return []


# ----------------------------------------------------- stream resolution ---
# How limusic (github.com/SimoHypers/limusic) gets fast starts: it POSTs
# directly to YouTube's internal InnerTube API impersonating official
# client identities, and some of those identities return READY-TO-PLAY
# stream URLs - no yt-dlp, no signature cipher, no PoToken. One round
# trip (~0.2s) instead of yt-dlp's multi-second grind.
#
# Borrowed identities/headers from limusic's crates/innertube/clients.json.
# VISIONOS is the only one mpv can stream directly today (ANDROID_VR URLs
# 403 past the first MiB, IOS_MUSIC needs bounded ranges) but keeping the
# whole chain costs ~100ms per miss, and any hit skips yt-dlp entirely.

_INNERTUBE_URL = "https://music.youtube.com/youtubei/v1/player?prettyPrint=false"

_FAST_CLIENTS = [
    ("VISIONOS", "101", "0.1", {
        "clientName": "VISIONOS", "clientVersion": "0.1",
        "osName": "visionOS", "osVersion": "1.3.21O771",
        "deviceMake": "Apple", "deviceModel": "RealityDevice14,1",
    }, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"),
    ("ANDROID_VR_1_43_32", "28", "1.43.32", {
        "clientName": "ANDROID_VR", "clientVersion": "1.43.32",
        "osName": "Android", "osVersion": "12",
        "deviceMake": "Oculus", "deviceModel": "Quest 3",
        "androidSdkVersion": 32, "buildId": "SQ3A.220605.009.A1",
        "cronetVersion": "107.0.5284.2",
    }, "com.google.android.apps.youtube.vr.oculus/1.43.32 (Linux; U; Android 12; "
       "en_US; Quest 3; Build/SQ3A.220605.009.A1; Cronet/107.0.5284.2"),
    ("IOS_MUSIC", "26", "7.01.05", {
        "clientName": "IOS_MUSIC", "clientVersion": "7.01.05",
        "osName": "iOS", "osVersion": "18.2.22C152",
        "deviceMake": "Apple", "deviceModel": "iPhone16,2",
    }, "com.google.ios.youtubemusic/7.01.05 (iPhone16,2; U; CPU iOS 18_2 like Mac OS X;)"),
]


def _parse_video_id(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (q.get("v") or [None])[0]


def _innertube_direct(video_id):
    """Ask InnerTube /player directly; return a validated stream URL or None."""
    for name, cid, cver, client, ua in _FAST_CLIENTS:
        body = {
            "context": {
                "client": {**client, "gl": "US", "hl": "en"},
                "request": {"internalExperimentFlags": [], "useSsl": True},
                "user": {"lockedSafetyMode": False},
            },
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
        headers = {
            "content-type": "application/json", "accept": "application/json",
            "x-goog-api-format-version": "1",
            "x-youtube-client-name": cid, "x-youtube-client-version": cver,
            "x-origin": "https://music.youtube.com",
            "referer": "https://music.youtube.com/",
            "user-agent": ua, "accept-language": "en-US,en;q=0.9",
        }
        try:
            req = urllib.request.Request(_INNERTUBE_URL, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read())
            if (data.get("playabilityStatus") or {}).get("status") != "OK":
                continue
            audio = [
                f for f in (data.get("streamingData") or {}).get("adaptiveFormats", [])
                if "width" not in f and f.get("url")
                and "signatureCipher" not in f and "cipher" not in f
            ]
            if not audio:
                continue
            best = max(audio, key=lambda f: f.get("bitrate", 0))
            # Validate like limusic does: googlevideo serves some client URLs
            # only for bounded ranges - a plain GET would 403 mid-stream.
            vr = urllib.request.Request(best["url"], headers={
                "Range": "bytes=0-2047", "User-Agent": ua})
            with urllib.request.urlopen(vr, timeout=5) as r:
                if r.status not in (200, 206) or len(r.read()) == 0:
                    continue
            return best["url"], best.get("expiresInSeconds") or 21540
        except Exception:
            continue
    return None


def _ytdlp_url(url):
    """Fast yt-dlp resolution: the android player client skips the heavy
    JS-decipher path (measured 1.3s vs 3.6s with default clients)."""
    out = subprocess.run(
        ["yt-dlp", "--quiet", "--no-warnings", "--no-playlist",
         "--extractor-args", "youtube:player_client=android,default,ios",
         "-f", "ba/b", "-g", url],
        capture_output=True, text=True, timeout=45)
    first = (out.stdout or "").strip().splitlines()
    if first and first[-1].startswith("http"):
        return first[-1], 6 * 3600
    return None


def _url_streams_in_mpv(url):
    """Cheap spot check (limusic validates every candidate the same way):
    ask mpv to decode a couple of frames with null audio. Only a real
    0-exit proves googlevideo will actually serve this URL to mpv."""
    r = subprocess.run(
        ["mpv", "--no-video", "--ao=null", "--frames=48", "--really-quiet",
         "--no-terminal", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, timeout=40)
    return r.returncode == 0


_url_cache = {}  # video_id -> (expires_epoch, url)


def _resolve(item):
    """Fast path only. Cache -> InnerTube direct clients (their URLs are
    validated inside _innertube_direct). For yt-dlp-resolved URLs we do
    NOT trust direct streaming - googlevideo's serving decisions are
    per-request here, so a URL that spot-checks can still die in the
    real player; youtube falls back to the pipe below."""
    vid = _parse_video_id(item["url"])
    if vid and vid in _url_cache:
        exp, url = _url_cache[vid]
        if time.time() < exp:
            return url
    _url_cache.clear()  # drop expired entries wholesale; the dict stays tiny
    if vid:
        got = _innertube_direct(vid)      # ~0.2s per client, sub-second when it hits
    else:
        got = _ytdlp_url(item["url"])     # non-youtube sources, validated below
    if not got:
        return None
    url, expires = got
    if not _url_streams_in_mpv(url):
        return None                       # never hand mpv a URL that dies mid-stream
    if vid:
        _url_cache[vid] = (time.time() + int(expires) * 0.8, url)
    return url


# ---------------------------------------------------------------- player ---

_MPV_BASE = ["mpv", "--no-video", "--no-terminal", "--really-quiet"]


class Player:
    """Dual-session player: an active session plus a prefetch session.

    Speed strategy (limusic-style):
      1. Fast resolve: InnerTube direct clients -> CDN url (sub-second
         on friendly networks), else yt-dlp
      2. Prefetch: right after a track starts, buffer the NEXT track in
         a muted mpv session (unpaused-but-muted, because paused mpv
         does not read stdin). mpv fills its demuxer cache with the
         whole track; selecting it seeks to 0:00 and unmutes - instant.
      3. Immediate play (cold selection): the pipe `yt-dlp -> mpv`,
         tv_simply client first (fastest working extraction on this
         network), default as in-process fallback.
    """

    def __init__(self):
        # active session
        self.proc = None
        self.dl = None
        self.track = None
        self.item = None
        self.loading = False
        self.paused = False
        self._sock = None
        # prefetch session
        self.pf_proc = self.pf_dl = self.pf_sock = None
        self.pf_item = None
        self.pf_ready = False
        self._pf_gen = 0  # prefetch lifecycle counter
        self._gen = 0  # bump on stop: stale workers quit silently

    # ------------------------------------------------------------ spawning

    @staticmethod
    def _mpv_cmd(source, pause, sock, muted=False):
        extra = []
        if pause:
            extra.append("--pause")
        if muted:
            extra += ["--mute=yes", "--keep-open"]
        return _MPV_BASE + extra + [f"--input-ipc-server={sock}", source]

    @staticmethod
    def _new_sock():
        return os.path.join(
            tempfile.gettempdir(),
            f"tuiplayer-mpv-{os.getpid()}-{time.time_ns()}.sock")

    def _spawn_direct(self, url, pause, muted=False):
        sock = self._new_sock()
        proc = subprocess.Popen(self._mpv_cmd(url, pause, sock, muted),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
        return proc, None, sock

    def _spawn_pipe(self, url, pause, muted=False):
        sock = self._new_sock()
        feeder = ["yt-dlp", "-q", "--no-warnings", "--no-playlist",
                  "--extractor-args", "youtube:player_client=tv_simply,default",
                  "-f", "ba/b", "-o", "-", url]
        dl = subprocess.Popen(feeder, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
        proc = subprocess.Popen(self._mpv_cmd("-", pause, sock, muted),
                                stdin=dl.stdout, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        dl.stdout.close()  # mpv owns the pipe now
        return proc, dl, sock

    @staticmethod
    def _kill(proc, dl=None):
        for p in (proc, dl):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3)

    @staticmethod
    def _rm_sock(path):
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------------ ipc

    def _ipc(self, sock, cmd):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2)
            s.connect(sock)
            s.sendall(json.dumps({"command": cmd}).encode("utf-8") + b"\n")
            reply = json.loads((s.recv(4096) or b"{}").decode("utf-8", "replace"))
            s.close()
            return reply.get("error") == "success"
        except Exception:
            return False

    def _ipc_unpause(self, sock, tries=8):
        """Promote a prefetched (muted, playing) session to audible from 0:00."""
        for _ in range(tries):
            self._ipc(sock, ["seek", 0, "absolute", "exact"])
            if self._ipc(sock, ["set_property", "mute", False]):
                return True
            time.sleep(0.25)
        return False

    def _ipc_buffered(self, sock, tries=140):
        """demuxer-cache-duration > 0.5  ->  the stream is actually flowing."""
        for _ in range(tries):
            try:
                s = socket.socket(socket.AF_UNIX)
                s.settimeout(1)
                s.connect(sock)
                s.sendall(b'{"command": ["get_property", "demuxer-cache-duration"]}\n')
                reply = json.loads((s.recv(512) or b"{}").decode("utf-8", "replace"))
                s.close()
                if isinstance(reply.get("data"), (int, float)) and reply["data"] > 0.5:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    @staticmethod
    def _alive(sock):
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(0.5)
                s.connect(sock)
            return True
        except OSError:
            return False

    # ---------------------------------------------------------------- state

    def playing(self):
        return self.proc is not None and self.proc.poll() is None and not self.paused

    def ready(self):
        """A buffered prefetched session is waiting for the user."""
        return bool(self.pf_proc and self.pf_proc.poll() is None and self.pf_ready)

    # ------------------------------------------------------------ lifecycle

    def _start(self, item, gen, pgen, sink):
        """sink = 'active' or 'pf'; resolves then spawns into that session."""
        try:
            g = self._gen
            if sink == "active":
                self.loading = True
            direct = _resolve(item)
            if self._gen != g:
                return
            if sink == "pf" and self._pf_gen != pgen:
                return
            muted = sink == "pf"
            fn = self._spawn_direct if direct else self._spawn_pipe
            args = (direct, False, muted) if direct else (item["url"], False, muted)
            proc, dl, sock = fn(*args)
            if self._gen != g:
                self._rm_sock(sock)
                self._kill(proc, dl)
                return
            if sink == "active":
                self.proc, self.dl, self._sock = proc, dl, sock
                self.loading = False
            else:
                self.pf_proc, self.pf_dl, self.pf_sock = proc, dl, sock
                # verify bytes actually flow before advertising instant-start
                self.pf_ready = self._ipc_buffered(sock)
                if not self.pf_ready:
                    self.kill_session(pf=True)   # dead prefetch: drop it quietly
        finally:
            if sink == "active":
                self.loading = False

    def play(self, item):
        if not item:
            return
        # promotion: the item was prefetched and is fully buffered
        if (self.pf_item == item and self.pf_proc
                and self.pf_proc.poll() is None and self.pf_ready):
            self._kill(self.proc, self.dl)
            self._rm_sock(self._sock)
            self.proc, self.dl, self._sock = self.pf_proc, self.pf_dl, self.pf_sock
            self.track, self.item = item["title"], item
            self.pf_proc = self.pf_dl = self.pf_sock = None
            self.pf_item = None
            self.pf_ready = False
            if self._ipc_unpause(self._sock):
                self.paused = False
            else:
                self.stop()   # promote failed: fall back to a cold spawn
                self.track = item["title"]
                g = self._gen
                threading.Thread(target=self._start,
                                 args=(item, g, self._pf_gen, "active"),
                                 daemon=True).start()
            return
        # cold: kill everything (a different prefetch is worthless now)
        self.stop()
        self.track = item["title"]
        self.item = item
        g = self._gen
        threading.Thread(target=self._start,
                         args=(item, g, self._pf_gen, "active"),
                         daemon=True).start()

    def prefetch(self, item):
        """Buffer the given (usually next) item in a paused session."""
        if not item:
            return
        if self.item == item or self.pf_item == item:
            return
        self._pf_gen += 1
        self.kill_session(pf=True)
        self.pf_item = item
        threading.Thread(target=self._start,
                         args=(item, self._gen, self._pf_gen, "pf"),
                         daemon=True).start()

    def kill_session(self, pf=False):
        if pf:
            self._kill(self.pf_proc, self.pf_dl)
            self._rm_sock(self.pf_sock)
            self.pf_proc = self.pf_dl = self.pf_sock = None
            self.pf_item = None
            self.pf_ready = False
        else:
            self._kill(self.proc, self.dl)
            self._rm_sock(self._sock)
            self.proc = self.dl = self._sock = None

    def stop(self):
        self._gen += 1
        self.kill_session(pf=True)
        self.kill_session()
        self.track = self.item = None
        self.loading = False
        self.paused = False


player = Player()

# -------------------------------------------------------------------- tui ---

def fmt_dur(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "?"
    if s <= 0:
        return "?"
    return f"{s // 60}:{s % 60:02d}"


def clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)   # header / accent
        curses.init_pair(2, curses.COLOR_CYAN, -1)     # selection
        curses.init_pair(3, curses.COLOR_GREEN, -1)    # playing
    except curses.error:
        pass

    query, focus, results, sel = "", 0, [], 0
    status = "welcome. type something."

    def do_search():
        nonlocal results, sel, status
        q = query.strip()
        if not q:
            status = "nothing to search."
            return
        status, results, sel = f"searching '{q}'...", [], 0
        try:
            results = search(q) or _search_ytdlp(q)
        except Exception as e:
            status = f"search failed: {e}"
            return
        status = (f"{len(results)} results [{q}]"
                  if results else f"no results for '{q}'")
        if results:
            # start buffering the top result while the user browses the list
            player.prefetch(results[0])

    while True:
        h, w = stdscr.getmaxyx()
        try:
            stdscr.erase()

            # header
            stdscr.addstr(0, 0, "♪", curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(0, 2, clip(f"tuiplayer v{VERSION}", w - 4), curses.A_BOLD)
            stdscr.hline(1, 0, curses.ACS_HLINE, w)

            # search line
            cursor = "▌" if focus == 0 else " "
            stdscr.addstr(2, 0, f" search> {query}{cursor}", curses.A_BOLD if focus == 0 else curses.A_DIM)
            stdscr.hline(3, 0, curses.ACS_HLINE, w)

            # results
            room = max(0, h - 7 - 1)
            for i, r in enumerate(results[:room]):
                line = f" {r['title']}  — {r['channel']}  [{fmt_dur(r['dur'])}]"
                line = clip(line, w - 1)
                attr = 0
                if i == sel:
                    attr = curses.color_pair(2) | curses.A_REVERSE
                elif player.track == r["title"] and (player.playing() or player.ready()):
                    attr = curses.color_pair(3)
                stdscr.addstr(4 + i, 0, line, attr)

            # footer
            stdscr.hline(h - 3, 0, curses.ACS_HLINE, w)
            if player.playing():
                st = f" ▶ {player.track}"
            elif player.ready() and player.track:
                st = f" ⏸ buffered, press enter {player.track}"
            elif player.loading:
                st = f" ⋯ loading {player.track}"
            else:
                st = status
            stdscr.addstr(h - 2, 0, clip(" " + st, w - 1), curses.A_DIM)
            help_ = " tab:focus enter:play ▲▼:nav s:stop n:next q:quit "
            stdscr.addstr(h - 1, 0, clip(help_, w - 1), curses.A_BOLD)
            stdscr.refresh()
        except curses.error:
            pass

        try:
            k = stdscr.getch()
        except KeyboardInterrupt:
            break
        if k == curses.KEY_RESIZE:
            continue

        if focus == 0:  # ---- search box
            if k in (10, curses.KEY_ENTER):
                do_search()
                focus = 1 if results else 0
            elif k in (curses.KEY_DOWN, 9):
                focus = 1
            elif k == 27:
                if query:
                    query = ""
                else:
                    break
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
            elif 32 <= k < 127 or k > 127:
                query += chr(k)
        else:  # ---- result list
            if k in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(results) if results else 0
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(results) if results else 0
            elif k in (10, curses.KEY_ENTER):
                if results:
                    play_item = results[sel]
                    player.play(play_item)
                    status = f"playing #{sel + 1}/{len(results)}"
                    player.prefetch(results[(sel + 1) % len(results)]
                                    if len(results) > 1 else None)
            elif k == ord("s"):
                player.stop()
                status = "stopped."
            elif k == ord("n"):
                if results:
                    sel = (sel + 1) % len(results)
                    player.play(results[sel])
                    status = f"next ▶ {results[sel]['title']}"
                    player.prefetch(results[(sel + 1) % len(results)]
                                    if len(results) > 1 else None)
            elif k == 9 or k == curses.KEY_HOME:
                focus = 0
            elif k == 27 or k == ord("q"):
                break

    player.stop()


if __name__ == "__main__":
    if "--version" in sys.argv[1:] or "-V" in sys.argv[1:]:
        print(f"tuiplayer {VERSION}")
        sys.exit(0)
    if not shutil.which("mpv"):
        sys.exit("tuiplayer: need mpv on your PATH")
    if not shutil.which("yt-dlp"):
        sys.exit("tuiplayer: need yt-dlp on your PATH")
    curses.wrapper(main)
