# ANIMAX 2.4.3

ANIMAX keeps its existing download, upload, history, routing, recovery, and maintenance commands. The separate `STREAM_DISCORD_TOKEN` account now handles Go Live through `?` commands; `.download` and the other main-account commands remain unchanged.

> **Self-bot warning:** ANIMAX automates normal Discord user accounts. Discord prohibits self-bots and may terminate accounts that use them. The Python client uses `DISCORD_TOKEN` for main commands and downloads. A distinct `STREAM_DISCORD_TOKEN` account listens for stream commands and handles Go Live. Both accounts are subject to Discord's self-bot rules. Use at your own risk.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install and start](#install-and-start)
- [Commands](#commands)
- [Download and upload](#download-and-upload)
- [Go Live streaming](#go-live-streaming)
- [Settings](#settings)
- [Local data and maintenance](#local-data-and-maintenance)
- [Troubleshooting](#troubleshooting)
- [Security and third-party components](#security-and-third-party-components)

## Features

- Downloads inclusive episode ranges by MyAnimeList (MAL) ID, prepares whole MP4 files, and uploads them to Discord.
- Remembers per-anime upload destinations and keeps local delivery history, job state, and recovery information in `anime.json`.
- Resumes interrupted jobs, verifies uncertain uploads before retrying, and preserves successful episode records.
- Provides title-aware episode captions, subtitle handling, safe-size compression, configurable fonts and CPU encoding.
- Keeps history search, export, reset, route-refetch, and settings commands on the main `.` prefix.
- Adds a separate `?` command set on the streaming account. `?stream` matches local `anime.json` titles, fetches the recorded Discord messages, downloads their current video attachments, and plays the uploaded videos continuously in Go Live.
- Plays each full attachment without OP/ED cuts. While episode N plays, ANIMAX prepares only N+1.
- `.download` and `?stream` may run at the same time. Download encoding uses 4 threads by default; Go Live encoding uses 2 threads and an ultrafast CPU preset. The download may take longer while both encoders are active; no GPU is required.
- **No download worker pool:** saved download jobs run one at a time. They remain resumable and cancellable; the configurable worker-count setting was removed.

## Requirements

### Existing download/upload commands

- Python 3.10+ (3.11+ recommended), pip, and the packages in `requirements.txt`.
- FFmpeg and FFprobe on `PATH`, plus Chromium and its system libraries for the provider player resolver.
- The main Discord account must have access to the command, destination, and upload-history channels.

### Optional stream-token `?` commands

- Node.js **22.4.0+** and npm. Go Live dependencies are pinned in `package-lock.json`; the Node sidecar starts automatically when `STREAM_DISCORD_TOKEN` is configured.
- A separate streaming user account with access to the server, text channel, and voice channel. Put its token only in `STREAM_DISCORD_TOKEN`; using the main account for both tokens is rejected.
- An FFmpeg build with CPU `libx264`, AAC, `drawtext`, and MPEG-TS support.
- Enough CPU and free disk to run download encoding and Go Live together. Both can remain active, but heavy CPU or disk pressure can slow the download or cause stream buffering.

The Go Live library uses native WebRTC/AV dependencies. On systems without compatible prebuilt binaries, installing them may require a C/C++ toolchain.

## Install and start

1. Install Python, pip, FFmpeg/FFprobe, and Chromium system libraries. For Go Live, also install Node.js 22.4+ and npm.
2. In the project directory, create and edit the private environment file:

   ```bash
   cp .env.example .env
   ```

   Set `DISCORD_TOKEN` locally. To enable `?` stream commands, also set `STREAM_DISCORD_TOKEN` to a token belonging to a different user account. Never paste either token into chat or commit `.env`.
3. Install Python packages and Chromium:

   ```bash
   python -m venv .venv
   # Windows PowerShell: .venv\Scripts\Activate.ps1
   # macOS/Linux:       source .venv/bin/activate
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

4. Start ANIMAX with its only runtime launcher:

   ```bash
   python bot.py
   ```

   **Do not launch a second bot or the Node sidecar manually.** The Python entry point supervises the separate streaming account and `?` command listener. Without a valid `STREAM_DISCORD_TOKEN`, the existing main-account commands still work; Go Live commands are disabled.

For development checks, install `requirements-dev.txt`, then run `pytest -q`. After `npm ci`, run `npm run check`, `npm run test:commands`, `npm run test:queue`, and `npm run test:feed` for the Go Live sidecar.

## Commands

The main account keeps the `.` prefix for downloads and existing utility commands. The streaming account listens for the following `?` commands; any server member can start a stream, but only that stream's starter can control it.

| Command | Purpose |
|---|---|
| `.download MAL_ID dub\|sub START END [resume\|force] [#channel]` | Download, prepare, and upload an inclusive episode range. |
| `?help` | Show the stream-token command list and usage. |
| `?stream <MAL ID or anime name> <start> <end> [sub\|dub]` | Play recorded Discord uploads in the command author's current voice channel. |
| `?pause` / `?resume` | Pause or resume Go Live playback. |
| `?skip` | Skip the current episode. |
| `?forward 10s` / `?backward 2m` | Seek forward or backward in the current episode by seconds or minutes. |
| `?skipto <episode>` | Jump to an episode within the requested range. |
| `?volume [0-100]` | Check or change volume. Default is 50%; maximum is 100%. |
| `?autopause on\|off` | Pause when only the streaming account remains in voice; resume when someone returns. Default is off. |
| `?stop` | Stop the stream and leave voice. |
| `.status` | Show the serialized download queue and any active Go Live stream. |
| `.cancel [JOB_ID\|all]` | Cancel download jobs. Stream controls use the `?` prefix. |
| `.resume [JOB_ID\|all]` / `.retry` | Resume interrupted or failed downloads using saved destinations and upload checks. |
| `.history MAL_ID` | Show local delivery totals for an anime. |
| `.search QUERY` | Find previously uploaded episodes in `anime.json`; this is not a source-catalog search. |
| `.getlist` | Send the local `anime.json` file. It contains account/channel/message metadata; share carefully. |
| `.reset` | Request confirmation before clearing local history, saved jobs, and downloaded cache. `.reset cancel` aborts the request. |
| `.refetch [#channel\|CHANNEL_ID]` | Rebuild missing/empty history from readable channel history. A failed scan leaves the existing history unchanged. |
| `.settings [FIELD [VALUE]]` | List, inspect, or change validated settings. Settings can only be changed while idle. |
| `.help [command]` / `.mphelp` | List main-account commands or show one command's syntax. |

## Download and upload

Example first download in a server:

```text
.download 21 dub 1 10 #one-piece
```

The destination is saved for that anime and reused on later runs, so a later command can be:

```text
.download 21 dub 11 20
```

Use `resume` (the default) to skip confirmed episodes and recover uncertain uploads. Use `force` only when you intentionally want to resend already-confirmed episodes. ANIMAX uploads whole episodes; it does not split a video. The safe prepared-file limit is 950 MB. If a source or prepared output needs compression, the configured compression and lossless rules apply. See `.settings` and the matching entries in `.env.example` for quality, subtitle, font, retry, upload, storage, and CPU settings.

Only one saved download job runs at a time; additional jobs remain queued in `anime.json`. `.cancel JOB_ID` or `.cancel all` controls downloads, while `.resume`/`.retry` can restart interrupted or failed jobs. A download can continue while Go Live is active, using its normal 4-thread default alongside the 2-thread stream encoder.

## Go Live streaming

The separate streaming account listens for `?` commands. Go Live does **not** replace `.download`; downloads and streaming remain independent features and can run together. Join a voice channel, then run `?stream` in a server text channel. The person who issues `?stream` must be in the voice channel where they want Go Live to appear. The requested episodes must already be recorded in local `anime.json` as uploaded Discord messages with video attachments.

```text
?stream One Piece 1 100 dub
?stream 21 1 12 sub
?stream "Attack on Titan" 1 25
```

- `?help` displays the stream-token commands. Any server member may start a stream; only the member who started it may pause, resume, skip, seek, change volume, configure autopause, jump episodes, or stop it.
- A numeric query is a MAL ID. Anime names are fuzzy-matched **only against titles in local `anime.json`**; `?stream` does not search an external catalog or resolve another media source.
- For each episode, ANIMAX selects its recorded upload message, fetches that message through the main Python account, downloads the current video attachment from Discord CDN, validates it with FFprobe/FFmpeg, and plays the local file. Missing records, messages, or attachments stop the series with a clear error; episodes are not replaced by an external source.
- `DISCORD_TOKEN` remains in Python and is never passed to the Node sidecar. The distinct `STREAM_DISCORD_TOKEN` account receives `?` commands, creates/edits the stream progress message, joins voice, and handles Go Live playback.
- Add `sub` or `dub` to choose a recorded upload language. If omitted, each episode prefers an available dub, then sub. The full stored video attachment is played as uploaded.
- The stream progress message shows title, episode/range, current stage, and elapsed/total timing. It does not show a timing percentage. During attachment download it can also show downloaded bytes. Status edits happen about every five seconds.
- `?forward 10s` and `?backward 2m` seek within the current episode. Volume defaults to 50%; use `?volume 0`–`?volume 100` to adjust it. `?autopause on` pauses when no other member remains in the voice channel and automatically resumes when someone returns; it is off by default.
- Go Live output is fixed at 1920×1080, 16:9, H.264, 30 fps. There is no stream-quality command. The stream encoder uses 2 CPU threads; downloads use 4 by default. Both may run at once, but system load can slow downloads or affect playback.
- A short title card precedes each episode. While episode N plays, only N+1 is downloaded and validated. The downloaded attachment is cached under `downloads/stream-cache/` only when `keep_stream_cache` is enabled.
- Console output is timestamped and reports listener startup/login, saved message lookup, attachment download progress, FFmpeg validation/remux, voice connection, playback, and controls. Details are also retained in `logs/bot.log` and `logs/stream-bridge.log`.

Go Live does not create upload posts or alter anime history. The sidecar starts automatically under the sole launcher, `python bot.py`, when a distinct streaming token is configured.

## Settings

Use `.settings` to list settings, `.settings FIELD` to inspect one, and `.settings FIELD VALUE` to change it while idle. Changes are stored in the ignored `settings.json` file and take precedence over environment defaults on the next launch.

Important settings include:

- `quality`, `subtitles`, `caption_font`, and `dub_fallback` for downloaded episodes.
- `compressall`, `lossless`, `crf`, `compress`, `target_size`, and `max_size` for safe output-size and quality behavior.
- `threads`, `preset`, `fps`, `upload_limit`, and `disk_gib` for local processing, upload, and storage. Download encoding defaults to 4 threads.
- `stream_bitrate` and `keep_stream_cache` for Go Live. Stream output stays fixed at 1080p/16:9/30 fps and uses 2 encoder threads.

The former worker-count setting is ignored when loading older `settings.json` files and is no longer configurable. Download jobs are serialized.

## Local data and maintenance

- `anime.json` stores job state, per-anime routes, upload messages, and local delivery history. Use `.getlist` carefully: this file contains account/channel/message metadata.
- `downloads/` holds downloaded and prepared videos, resume fragments, and the separate Go Live cache. `logs/` contains local diagnostics. Both are excluded from version control.
- `.refetch` only imports data from channel history the account can read; scan errors do not partially replace the journal.
- `.reset` clears only local history/jobs and downloads after an explicit confirmation. It does not delete Discord messages, `.env`, settings, or cooldown information.

## Troubleshooting

- **Python import/dependency error:** start from the project root, verify `animax/` is present, and run `pip install -r requirements.txt` with the same Python used for `python bot.py`.
- **Download or upload fails:** inspect `logs/bot.log` and the episode-specific logs. Check destination permissions, free disk, source availability, and rate-limit cooldowns before retrying.
- **`?` commands do not respond:** check that `STREAM_DISCORD_TOKEN` is set to a distinct account, Node.js 22.4+ and npm are installed, and startup output shows the streaming account logged in. Node dependencies install automatically under `python bot.py`.
- **Streaming account not ready or same-account warning:** configure a valid `STREAM_DISCORD_TOKEN` for a different user. The sidecar checks the logged-in user ID against the main `DISCORD_TOKEN` account.
- **Stream appears stuck at Starting or Connecting:** check timestamped terminal output and `logs/bot.log` plus `logs/stream-bridge.log`. The streaming account's status message remains visible through lookup, CDN download, FFmpeg validation, voice connection, and playback. Voice negotiation is bounded to 25 seconds.
- **No matching episode or attachment:** `?stream` uses only uploads recorded in `anime.json`. Verify the MAL ID, language, episode number, saved channel/message IDs, and that the main account can fetch that message.
- **Native package or Go Live startup error:** inspect timestamped Node output and `logs/stream-bridge.log`; confirm the pinned packages, CPU FFmpeg `libx264`/AAC/drawtext/MPEG-TS support, and Node version. The bridge is not tested against every Discord session or host.
- **No Go Live start:** verify the requester is in the intended voice channel and that the streaming account can connect there. DAVE negotiation, account restrictions, network conditions, and client changes can still prevent streaming.
- **Episode boundary pause or buffering:** the next attachment may still be downloading or validating. Check CPU and disk pressure, especially while download encoding and the 2-thread stream encoder run together.

## Security and third-party components

- Discord prohibits automating normal user accounts and may terminate accounts that do so. The Python command client and optional Node Go Live sidecar use distinct user-account tokens; no authenticated live playback or account-safety guarantee is claimed.
- Keep `.env`, `anime.json`, private logs, and exported `.getlist` attachments confidential. The local stream bridge binds to loopback and uses a per-run random bearer secret.
- Respect copyright law, Discord rules, and the terms for any source used by the download commands. `?stream` downloads only attachments already recorded in `anime.json`; it does not bypass DRM or resolve an external stream source.
- Third-party components include [discord.py-self](https://github.com/dolfies/discord.py-self), [discord-video-stream](https://github.com/Discord-RE/Discord-video-stream), the [DAVE-capable selfbot client fork](https://www.npmjs.com/package/@lng2004/discord.js-selfbot-v13), [AniList](https://anilist.co/), and [Jikan](https://jikan.moe/). Bundled fonts include their license files in `assets/fonts/`.
- No ANIMAX project license file is included in this release; check the repository's terms before redistribution.
