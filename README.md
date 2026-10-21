# ANIMAX 2.4.3

ANIMAX resolves an anime by MyAnimeList (MAL) ID or title, prepares episodes locally, and plays them as one continuous **Discord Go Live** session. It keeps one episode active and one next-episode preparation slot—no worker pool, episode uploads, or alternate launcher.

> **Account warning:** Go Live is implemented through a self-bot (a normal user account running automation). Discord prohibits self-bots and may terminate accounts that use them. This project does not make that use safe or supported. Read the [Discord self-bot policy](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots) and proceed at your own risk.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Commands](#commands)
- [Settings](#settings)
- [How playback works](#how-playback-works)
- [Troubleshooting](#troubleshooting)
- [Security and third-party components](#security-and-third-party-components)

## Features

- `.stream` accepts a MAL ID or a title; title searches use AniList/Jikan results and fuzzy matching to choose the closest MAL-linked entry.
- Starts Go Live in the **command author’s current voice channel**. The command must be posted in a server text channel, and its author must already be in the intended voice channel.
- Plays an inclusive episode range without requiring someone to enqueue each episode manually.
- Optional AniSkip opening/ending removal (`Yes`); clips without usable timings, API failures, and missing entries play without cuts.
- Burns available English captions into the image with the selected bundled font. If the provider has no usable English track, ANIMAX reports that and continues without captions.
- Inserts a 1080p title card before each episode (for example, “Starting Episode 7 — One Piece”).
- While episode N is playing, prepares **only episode N+1**. The next segment is added to the same Go Live feed; playback does not use a new launcher or separate Discord session per episode.
- CPU-only encoding: no GPU or hardware encoder is required. The outgoing video profile is fixed at 1920×1080, 16:9, H.264, 30 fps.
- One active Go Live series per ANIMAX process. `.skip`, `.stop`, and `.status` control the session.

## Requirements

- Python 3.10 or newer (3.11+ recommended).
- Node.js **22.4.0 or newer** and npm. The pinned current Go Live backend is `@dank074/discord-video-stream` 7.0.0 with the `@lng2004/discord.js-selfbot-v13` 3.7.2 client fork. This backend is selected for current DAVE-capable voice/video support; the older v6/selfbot package combination is not used.
- A recent FFmpeg and FFprobe available on `PATH`, built with:
  - the CPU `libx264` and AAC encoders;
  - the `subtitles`/libass filter and `azmq` (libzmq) filter;
  - MPEG-TS support.
- Playwright Chromium for observing the provider’s normal embedded player.
- A CPU with enough capacity to download and encode the next episode while the current episode is streamed. ANIMAX does not assume a GPU; a slower CPU may cause a pause at an episode boundary.
- Access to Discord, AniList/Jikan, AniSkip, the media provider, and its CDN.

The Go Live library uses native WebRTC/AV dependencies. `npm ci` may need a working C/C++ build toolchain on platforms where compatible native binaries are not available.

## Installation

1. Install Python, Node.js/npm, and an FFmpeg build meeting the requirements above. Confirm that `python`, `node`, `npm`, `ffmpeg`, and `ffprobe` resolve from your shell.
2. From the ANIMAX directory, create the private environment file and enter your token locally:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and replace `YOUR_USER_TOKEN_HERE` with your own token. Do **not** paste it into chat, commit it, or share log files that may contain account data.
3. Install the Python and Node dependencies and Chromium:

   ```bash
   python -m venv .venv
   # Windows PowerShell: .venv\Scripts\Activate.ps1
   # macOS/Linux:       source .venv/bin/activate
   pip install -r requirements.txt
   npm ci --omit=dev
   python -m playwright install chromium
   ```

   On first launch, `python bot.py` checks the Python/Node/FFmpeg runtime and can install missing project dependencies and Chromium. Node.js and FFmpeg themselves must already be installed.
4. Start ANIMAX using its **only runtime launcher**:

   ```bash
   python bot.py
   ```

   Do not start the Node sidecar separately. The Python entry point starts and supervises the local, loopback-only Node Go Live sidecar.

The Node stream-feed smoke test can be run after dependencies are installed with `npm run check` and `npm run test:feed`. Python checks use `pytest -q` after installing `requirements-dev.txt`.

## Usage

Join the voice channel where you want the stream to appear, then post a command in a server text channel:

```text
.stream One Piece 1 100 Yes
```

The title is fuzzy-matched to its best MAL-linked result. A MAL ID works too:

```text
.stream 21 1 12 No
.stream "Attack on Titan" 1 25 Yes dub
```

- `Yes` asks AniSkip for opening/ending intervals; `No` (or omitting the option) keeps the whole episode. The option is case-insensitive.
- Audio defaults to `sub`. Add `sub` or `dub` as the final option to choose another mode.
- The range is inclusive and limited to 100 episodes per command.
- The command author’s **current** voice channel is used; ANIMAX does not choose a random channel or move users.

## Commands

| Command | Purpose |
|---|---|
| `.stream <MAL ID or title> <start> <end> [Yes\|No] [sub\|dub]` | Match the anime, join the author’s current voice channel, and play the range. |
| `.skip` | Skip the current episode. If no episode is active yet, cancel the episode currently being prepared and advance. |
| `.stop` | Stop the active series and leave voice. |
| `.status` | Show the current phase and the single next-episode slot. |
| `.settings` | Show runtime settings. Use `.settings <name>` to inspect one setting or `.settings <name> <value>` to change and save it. |
| `.help` | Show command syntax and a concise safety notice. |

Only the author who started the active series can skip or stop it. A new series cannot start until the current session has ended.

## Settings

Edit `.env` before startup, or use `.settings` in Discord. Chat changes are written to the ignored `settings.json` file and take precedence over environment defaults on the next launch. Stream output remains fixed at 1080p/16:9.

| Setting | Default | Meaning |
|---|---:|---|
| `CAPTION_FONT` | `liberation` | `liberation`, `dejavu`, `dejavu-serif`, or `dejavu-mono`; bundled font files are in `assets/fonts/`. |
| `DUB_FALLBACK` | `true` | Fall back to sub only when dub playback or English dub captions are positively unavailable. |
| `VIDEO_PRESET` | `ultrafast` | CPU x264 preset for episode preparation and live streaming. Slower presets may exceed real time. |
| `ENCODE_THREADS` | `2` | Per-encode CPU thread limit. |
| `STREAM_BITRATE_KBPS` | `5000` | Average outgoing video bitrate; valid range 2500–8000 kbps. |
| `MAX_TEMP_GIB` | `8` | Temporary/cache budget under `downloads/`. |
| `KEEP_STREAM_CACHE` | `false` | Keep prepared episodes for reuse; when off, a prepared file is removed after it has played. |
| `REQUEST_INTERVAL` | `0.25` | Minimum interval between media-provider requests. |
| `RESOLVE_TIMEOUT` | `60` | Time allowed to observe the provider player and locate its stream. |
| `OPERATION_TIMEOUT` | `7200` | Maximum time allowed for one download/encode operation, in seconds. |
| `HEADLESS` | `true` | Run the provider-observation browser without a visible window. |
| `COMPRESS_ALL` | `false` | Preserve source quality below the safety limit unless enabled. |
| `LOSSLESS` | `false` | When enabled, lossless preparation takes precedence over `COMPRESS_ALL`. |
| `VIDEO_CRF` | `19` | CRF used when compression is required. |
| `COMPRESSION_MODE` | `quality` | `quality` or `size` behavior when a safe-size encode is required. |
| `FRAME_RATE` | `source` | Preparation frame rate; Go Live output is normalized to 30 fps. |

The configured temporary-storage limit covers the current episode and the one prefetch slot together. Keeping the stream cache can make later runs faster, but the configured disk limit still applies. The old worker, upload, route, and job settings are not used by v2.4.3.

## How playback works

1. A name search returns candidates from AniList/Jikan; RapidFuzz ranks the title and selects the best MAL-linked match. A numeric argument is treated as a MAL ID.
2. ANIMAX joins the command author’s voice channel through the Node sidecar and resolves the requested episode using the provider’s normal player page.
3. It downloads the source and prepares a whole MP4: 16:9 padding/scaling, selected-font caption burn-in when a usable track exists, and optional AniSkip trims. The trim is applied to video and audio together. AniSkip errors or missing timings fail open to the full episode.
4. A title card and episode are fed into one continuous CPU H.264 Go Live stream. During episode N, one background task downloads and prepares N+1. ANIMAX waits at the boundary if that preparation is not ready; it does not queue multiple future episodes.
5. Unless `KEEP_STREAM_CACHE=true`, the prepared episode is removed after its Go Live segment has been consumed. `downloads/` also contains private runtime files and is excluded from version control.

## Troubleshooting

- **Node version error:** install Node.js 22.4.0 or newer, reopen the shell, and check `node --version` before running `python bot.py`.
- **Native package or sidecar error:** from the project directory, run `npm ci --omit=dev`; inspect `logs/stream-bridge.log`. Make sure the `@dank074/discord-video-stream` and `@lng2004/discord.js-selfbot-v13` versions match `package-lock.json`.
- **FFmpeg capability error:** confirm `ffmpeg -encoders` lists `libx264` and AAC and `ffmpeg -filters` lists `subtitles` and `azmq`. Hardware acceleration is not required.
- **No Go Live start:** ensure the command author is in the intended voice channel and that the account can connect and speak there. Discord-side account restrictions, DAVE negotiation, network conditions, and client updates can still prevent streaming.
- **Title mismatch:** try the MAL ID or a more distinctive title. If metadata APIs are unavailable, retry later; numeric MAL IDs do not depend on fuzzy matching.
- **Episode/language unavailable:** verify the provider has mapped that episode and audio mode. Dub fallback is not triggered by generic timeouts or rate limits.
- **No captions:** the episode may have no usable English WebVTT track. ANIMAX reports the absence and continues without captions; font settings do not create missing subtitle data.
- **Skip did nothing:** AniSkip may not have timings for that MAL ID/episode, or its API may be unavailable. In either case the episode plays uncut.
- **Episode boundary pause:** the next episode may still be downloading or encoding. Check available CPU, free disk space, provider speed, `ENCODE_THREADS`, and the `ultrafast` preset.
- **Logs:** `logs/bot.log` and `logs/stream-bridge.log` are local diagnostics. Keep `.env`, account tokens, and private logs confidential.

## Security and third-party components

- **Self-bot risk:** Discord states that automating normal user accounts is prohibited and may result in account termination. This software is provided at the user’s risk; no promise of account safety, voice compatibility, or live reliability is made. See [Discord’s voice connection/DAVE documentation](https://docs.discord.com/developers/topics/voice-connections).
- The Node API binds to `127.0.0.1` and uses a per-run random bearer secret. The Python and Node processes share the token only through the local process environment; it is not printed in the startup banner.
- Respect the media provider’s terms, copyright law, and AniSkip’s API conditions. ANIMAX does not bypass DRM or provider access controls.
- Third-party components: [discord-video-stream](https://github.com/Discord-RE/Discord-video-stream), [the DAVE-capable selfbot client fork](https://www.npmjs.com/package/@lng2004/discord.js-selfbot-v13), [AniSkip](https://api.aniskip.com/api-docs), [AniList](https://anilist.co/), and [Jikan](https://jikan.moe/). Bundled DejaVu and Liberation fonts include their license files in `assets/fonts/`.
- No separate ANIMAX project license file is included in this release; check the repository’s terms before redistribution.
