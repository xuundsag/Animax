# ANIMAX 2.4.3

ANIMAX keeps its existing download, upload, history, routing, recovery, and maintenance commands. Version 2.4.3 adds `.stream` as a separate, optional Go Live feature—it does **not** replace `.download` or the other commands.

> **Self-bot warning:** ANIMAX automates normal Discord user accounts. Discord prohibits self-bots and may terminate accounts that use them. The Python client uses `DISCORD_TOKEN` for commands and downloads. Optional `.stream` uses a separate `STREAM_DISCORD_TOKEN` belonging to a different streaming user. Both accounts are subject to Discord's self-bot rules. Use at your own risk.

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
- Keeps existing history search, export, reset, route-refetch, and settings commands.
- Adds `.stream` for a continuous 1080p/16:9 CPU-only Go Live series. It matches titles against local `anime.json`, fetches the recorded Discord upload messages, downloads their video attachments, and plays the episodes in order without altering the uploaded media.
- While streaming episode N, prepares only episode N+1. Stream preparation and Go Live use an ultrafast CPU preset to reduce time-to-live; download encoding keeps its configured preset. Download/upload jobs and Go Live playback are kept separate: start one after the other, not concurrently.
- **No download worker pool:** saved download jobs run one at a time. They remain resumable and cancellable; the configurable worker-count setting was removed.

## Requirements

### Existing download/upload commands

- Python 3.10+ (3.11+ recommended), pip, and the packages in `requirements.txt`.
- FFmpeg and FFprobe on `PATH`, plus Chromium and its system libraries for the provider player resolver.
- A Discord account with access to the command, destination, and log channels.

### Optional `.stream` command

- Node.js **22.4.0+** and npm. Go Live dependencies are pinned in `package-lock.json` and installed lazily the first time `.stream` is used. Node does not need to be started separately.
- A separate streaming user account with access to the server and voice channel. Put its token only in `STREAM_DISCORD_TOKEN`; using the main account for both tokens is rejected. The main command account owns and edits the progress message.
- An FFmpeg build with CPU `libx264`, AAC, `drawtext`, and MPEG-TS support.
- Enough CPU and free disk to prepare the next episode while the current episode plays. No GPU is required. A slow CPU or provider may cause a pause at an episode boundary.

The Go Live library uses native WebRTC/AV dependencies. On systems without compatible prebuilt binaries, installing them may require a C/C++ toolchain.

## Install and start

1. Install Python, pip, FFmpeg/FFprobe, and Chromium system libraries. For `.stream`, also install Node.js 22.4+ and npm.
2. In the project directory, create and edit the private environment file:

   ```bash
   cp .env.example .env
   ```

   Set `DISCORD_TOKEN` locally. To use `.stream`, also set `STREAM_DISCORD_TOKEN` to a token belonging to a different user account. The main account handles commands; the streaming account joins voice and plays Go Live. Never paste either token into chat or commit `.env`.
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

   **Do not launch a second bot or the Node sidecar manually.** The Python entry point supervises the optional sidecar when `.stream` is first requested. Without Node installed, the existing download/upload commands can still be used; `.stream` will report that Node is required.

For development checks, install `requirements-dev.txt`, then run `pytest -q`. The synthetic Go Live feed test is `npm run test:feed` after Node dependencies have been installed.

## Commands

| Command | Purpose |
|---|---|
| `.download MAL_ID dub\|sub START END [resume\|force] [#channel]` | Download, prepare, and upload an inclusive episode range. First use in a server needs the destination channel mention at the end. |
| `.stream <MAL ID or anime name> <start> <end> [sub\|dub]` | Stream uploaded Discord attachments recorded in `anime.json` in the command author’s current voice channel. |
| `.skip` | Skip the current Go Live episode, or cancel its preparation if playback has not started. |
| `.stop` | Stop Go Live and leave voice. Only the person who started that stream can control it. |
| `.status` | Show the serialized download queue and current Go Live episode elapsed time/percentage. |
| `.cancel [JOB_ID\|all]` | Cancel download jobs. Use `.stop` to stop Go Live. |
| `.resume [JOB_ID\|all]` / `.retry` | Resume interrupted or failed downloads using saved destinations and upload checks. |
| `.history MAL_ID` | Show local delivery totals for an anime. |
| `.search QUERY` | Find previously uploaded episodes in `anime.json`; this is not a source-catalog search. |
| `.getlist` | Send the local `anime.json` file. It contains account/channel/message metadata; share carefully. |
| `.reset` | Request confirmation before clearing local history, saved jobs, and downloaded cache. `.reset cancel` aborts the request. |
| `.refetch [#channel\|CHANNEL_ID]` | Rebuild missing/empty history from readable channel history. A failed scan leaves the existing history unchanged. |
| `.settings [FIELD [VALUE]]` | List, inspect, or change validated settings. Settings can only be changed while idle. |
| `.help [command]` / `.mphelp` | List commands or show one command’s syntax. |

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

Only one saved download job runs at a time; additional jobs remain queued in `anime.json`. `.cancel JOB_ID` or `.cancel all` controls downloads, while `.resume`/`.retry` can restart interrupted or failed jobs.

## Go Live streaming

Join the voice channel where you want the stream to appear, then run `.stream` in a server text channel. The episode range must already be present in `anime.json` as uploaded Discord messages with video attachments.

```text
.stream One Piece 1 100 dub
.stream 21 1 12 sub
.stream "Attack on Titan" 1 25
```

- A numeric query is a MAL ID. Anime names are fuzzy-matched **only against titles stored in local `anime.json`**; `.stream` does not search an external catalog or resolve another media source.
- For each requested episode, ANIMAX finds its recorded upload message, fetches that message through the main Python Discord account, downloads the current video attachment from Discord CDN, validates it with FFprobe/FFmpeg, and streams the local file. Missing records, messages, or attachments stop the series with a clear error; episodes are not silently replaced by an external source.
- The command author (the main account) must be in the intended voice channel. The distinct account in `STREAM_DISCORD_TOKEN` joins that channel and handles Go Live playback. `DISCORD_TOKEN` stays in Python and is never passed to the Node sidecar.
- Add `sub` or `dub` to select a tracked upload language. If omitted, each episode prefers a recorded dub upload, then sub. The stored video attachment is played as uploaded.
- The command account creates and edits one progress message from the start. It shows title, current episode/range, stage, elapsed/total time, percentage, and Discord download bytes while an attachment is downloading. Status edits happen about every five seconds.
- Console output is timestamped and reports sidecar startup/login, saved message lookup, attachment download progress, FFmpeg validation/remux, voice connection, and playback state. Details are also retained in `logs/bot.log` and `logs/stream-bridge.log`.
- A short title card precedes each episode. The title card and source files are fed through one continuous 1920×1080, 16:9, H.264 Go Live session at 30 fps. While episode N plays, only N+1 is downloaded and validated.
- Download/upload jobs and `.stream` are intentionally mutually exclusive to avoid competing CPU and disk workloads. The downloaded attachment is cached under `downloads/stream-cache/` only when `keep_stream_cache` is enabled.

The Go Live stream is a separate feature from `.download`: it does not create upload posts or alter anime history. The Node sidecar is started automatically by the sole launcher, `python bot.py`, on first use of `.stream`.

## Settings

Use `.settings` to list settings, `.settings FIELD` to inspect one, and `.settings FIELD VALUE` to change it while idle. Changes are stored in the ignored `settings.json` file and take precedence over environment defaults on the next launch.

Important settings include:

- `quality`, `subtitles`, `caption_font`, and `dub_fallback` for downloaded episodes.
- `compressall`, `lossless`, `crf`, `compress`, `target_size`, and `max_size` for safe output-size and quality behavior.
- `threads`, `preset`, `fps`, `upload_limit`, and `disk_gib` for local processing, upload, and storage.
- `stream_bitrate` and `keep_stream_cache` for the optional Go Live feature. Go Live output itself stays fixed at 1080p/16:9/30 fps.

The former worker-count setting is ignored when loading older `settings.json` files and is no longer configurable. Download jobs are serialized.

## Local data and maintenance

- `anime.json` stores job state, per-anime routes, upload messages, and local delivery history. Use `.getlist` carefully: this file contains account/channel/message metadata.
- `downloads/` holds downloaded and prepared videos, resume fragments, and the separate Go Live cache. `logs/` contains local diagnostics. Both are excluded from version control.
- `.refetch` only imports data from channel history the account can read; scan errors do not partially replace the journal.
- `.reset` clears only local history/jobs and downloads after an explicit confirmation. It does not delete Discord messages, `.env`, settings, or cooldown information.

## Troubleshooting

- **Python import/dependency error:** start from the project root, verify `animax/` is present, and run `pip install -r requirements.txt` with the same Python used for `python bot.py`.
- **Download or upload fails:** inspect `logs/bot.log` and the episode-specific logs. Check destination permissions, free disk, source availability, and rate-limit cooldowns before retrying.
- **First `.stream` says Node is missing:** install Node.js 22.4+ and npm, reopen the shell, confirm `node --version`, then use `.stream` again. The existing download commands do not require Node.
- **Streaming account not ready or same-account warning:** set `STREAM_DISCORD_TOKEN` to a valid token for a distinct streaming user account. The sidecar verifies its logged-in user ID differs from the main `DISCORD_TOKEN` account before accepting stream requests.
- **Stream appears stuck at Starting or Connecting:** check the timestamped terminal lines and `logs/bot.log` plus `logs/stream-bridge.log`. The command’s existing progress message stays visible through startup, upload lookup, CDN download, FFmpeg validation, voice connection, and playback. Voice negotiation is bounded to 25 seconds.
- **No matching episode or attachment:** `.stream` uses only uploaded messages recorded in `anime.json`. Verify the MAL ID, language, episode number, saved channel/message IDs, and that the main account can read the channel and fetch its message history.
- **Native package or Go Live startup error:** inspect the timestamped Node output and `logs/stream-bridge.log`; confirm the pinned packages in `package-lock.json`, CPU FFmpeg `libx264`/AAC/drawtext/MPEG-TS support, and the Node version. The bridge is not tested against every Discord session or host.
- **No Go Live start:** verify the command author is in the intended voice channel and that the distinct streaming account can connect there. DAVE negotiation, account restrictions, network conditions, and client changes can still prevent streaming.
- **Episode boundary pause:** the next Discord attachment may still be downloading or validating. Check CPU, free disk, and the timestamped download/probe logs.

## Security and third-party components

- Discord prohibits automating normal user accounts and may terminate accounts that do so. The Python command client and optional Node Go Live sidecar use distinct user-account tokens; no live Discord playback or account-safety guarantee is claimed.
- Keep `.env`, `anime.json`, private logs, and exported `.getlist` attachments confidential. The local stream bridge binds to loopback and uses a per-run random bearer secret.
- Respect copyright law, Discord rules, and the terms for any source used by the download commands. `.stream` downloads only attachments already recorded in `anime.json`; it does not bypass DRM or resolve an external stream source.
- Third-party components include [discord.py-self](https://github.com/dolfies/discord.py-self), [discord-video-stream](https://github.com/Discord-RE/Discord-video-stream), the [DAVE-capable selfbot client fork](https://www.npmjs.com/package/@lng2004/discord.js-selfbot-v13), [AniList](https://anilist.co/), and [Jikan](https://jikan.moe/). Bundled fonts include their license files in `assets/fonts/`.
- No ANIMAX project license file is included in this release; check the repository’s terms before redistribution.
