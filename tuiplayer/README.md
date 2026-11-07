# tuiplayer

A barebones, keyboard-driven music player for the terminal.
Search races several free public sources (Piped / Invidious instances,
with a yt-dlp fallback) and plays the first response through `mpv`.

## Install (Arch Linux)

```
makepkg -si
```

Runtime dependencies: `python`, `mpv`, `yt-dlp` (installed automatically).

## Run

```
tuiplayer
```

Type a query in the search box and press enter. Then:

| key        | action                                        |
| ---------- | --------------------------------------------- |
| `tab`      | toggle focus: search box <-> result list      |
| `enter`    | search (search box) / play selection (list)   |
| `up/down`  | move selection (`j`/`k` in the list)          |
| `s`        | stop playback                                 |
| `n`        | next track                                    |
| `q`, `esc` | quit (`esc` first clears the search box)      |

## How it stays fast

- Sources are raced concurrently; the first to answer wins.
- Stream URLs are resolved via YouTube's InnerTube API using a handful of
  direct clients (limusic-style) and validated in `mpv` before use; if
  none works, `yt-dlp` resolves, and the guaranteed fallback is a
  `yt-dlp -> mpv` pipe.
- After a track starts, the next track is buffered in a muted mpv side
  session; selecting it later starts playing in well under a second
  instead of a full resolve+buffer wait.

## Notes

- No configuration file: sources and behavior are constants in
  `/usr/bin/tuiplayer`, single-file, standard library only.
- Playback depends on YouTube availability from your network; on some
  networks addresses can be bot-gated and the pipe fallback does the
  heavy lifting (a few seconds to start cold).
