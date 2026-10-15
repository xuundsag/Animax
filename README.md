<div align="center">

# ANIMAX

**Whole episodes. Source frame rates. One file per upload.**

[Quick start](#quick-start) · [Commands](#commands) · [Encoding](docs/ENCODING.md) · [Operations](docs/OPERATIONS.md)

</div>

ANIMAX downloads inclusive MAL episode ranges and sends one complete MP4 per episode to Discord. Version **2.3.1** adds destination verification and bounded upload recovery for every episode. It keeps 1080p, x264 **veryfast / CRF 19**, source FPS, and an advisory **400-500 MB** size preference. Verified uploads finish with the actual file size:

**One Piece** | `E3` | Done | `398.1 MB`

## Encoding policy

- Native x264 adaptive quantization and temporal allocation compress still frames efficiently and allow motion to use more bits.
- No duration-based bitrate ceiling in the default quality mode. The preferred size is not a hard cap, an automatic recompression trigger or a bitrate input.
- CRF 19 cannot promise 400-500 MB for every episode. Long, noisy or action-heavy sources can be larger. Files below the range are not padded with unnecessary data.
- Source FPS is preserved, including 23.976 fps. No forced 24 fps conversion.
- Full-frame 1080p canvas with aspect-ratio padding, not cropping or stretching.
- Outlined sub captions without a filled background; opaque boxes for dub captions.

Encoding remains lossy and cannot restore missing source detail. The default keeps the preset's native reference-frame/look-ahead behavior rather than adding slower overrides. See [encoding trade-offs](docs/ENCODING.md).

## Quick start

Python **3.11+** is recommended. On Ubuntu or Debian, extract the release ZIP and enter its directory:

```bash
bash install.sh
```

Edit `.env` locally with your account token and destination:

```dotenv
DISCORD_TOKEN=your_account_token
CHANNEL_ID=0
```

Choose one execution mode:

```bash
# Foreground
.venv/bin/python bot.py

# Survives SSH logout and starts at boot
sudo python3 manage.py service install

# Without systemd; survives logout, not a reboot
python3 manage.py service detach
```

## Existing installations

Stop the bot before replacing application files. Keep `.env`, runtime JSON files and `downloads/`. Do not run two copies with the same account. Existing preferences are preserved, so replacing the code does not undo old size-mode or preset overrides.

To select the v2.3 profile, run these while the bot is idle:

```text
.settings quality 1080
.settings preset veryfast
.settings crf 19
.settings threads 4
.settings fps source
.settings compress quality
.settings target_size 450
.settings reencode off
```

If you previously set `max_size` to 400, restore it to your actual allowed upload cap, for example `.settings max_size 1000` when your account supports 1,000 MB. `upload_limit` is a separate safety guard; it must also match the account's allowance. Neither guard is the preferred 400-500 MB range.

Matching prepared videos are reused. Normal encoding changes can invalidate a cache, but pending-upload recovery reuses the original prepared bytes after checking their saved upload identity. This release does not shrink an existing 700 MB file. Keep `logs/` as well as `downloads/`: older pending sends may need their upload report. Use `.resume` rather than repeatedly forcing E3.

## Commands

```text
download, status, cancel, resume, history, search, getlist, reset, refetch, settings, help
```

| Command | Example |
| :--- | :--- |
| Download an inclusive range | `.download 21 dub 1 100` |
| Inspect or stop work | `.status` · `.cancel` |
| Resume saved work | `.resume` |
| Search uploaded episodes | `.search One piece episode 1` |
| Export history | `.getlist` |
| Recover missing/empty history | `.refetch #channel` or `.refetch 123456789012345678` |
| Change a setting | `.settings quality 1080` |
| Clear local history/cache, after confirmation | `.reset` |

Command triggers are deleted by default. If deletion fails or is disabled, responses reply to the trigger without pinging its author. `.getlist` sends only the JSON attachment. Video captions have no download hyperlink. Episodes are sent sequentially with reply chains.

`Done` is shown only after the destination message/attachment are read back and the message ID is committed to history. Retained status messages show the actual file size, not `100%`. With channel 0, successful automatic statuses still follow the existing deletion policy.

### Upload recovery

All episode sizes, including files above 500 MB, use the same verification. A transient error such as HTTP 500 triggers delayed history checks. If the upload is found, it is recorded without a resend. If readable checks find no matching message, the cached file may be retried through the alternate cloud/multipart method. Default: two application-level attempts in total.

Checking, retries and detailed errors appear only in the terminal/logs. Discord stays at `Uploading`, then shows `Done` after verified success or `Paused` if recovery cannot safely finish. Inaccessible history, conflicting matches, a previously acknowledged but missing message, permissions errors, rate limits and file-size rejection never authorize a blind resend. [Recovery policy and settings](docs/UPLOADS.md)

These safeguards cannot guarantee exactly-once delivery or make Discord errors impossible. Late visibility and the SDK's internal HTTP retries remain external limitations.

## Project layout

```text
animax/          application, media pipeline and command handlers
assets/fonts/    caption fonts and their license
config/          example data files
docs/            encoding, operations and validation notes
tests/           regression and media tests (source checkout)
.github/         CI workflow (source checkout)
bot.py           application entry point
manage.py        service, diagnostics and migration commands
```

Runtime credentials, settings, histories, logs, caches and ZIPs are excluded from Git. Tests and CI are excluded from the deployment ZIP.

## Development

From a source checkout with FFmpeg installed:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use real FFmpeg/yt-dlp for local media fixtures and mocks for Discord. [Validation details](docs/VALIDATION.md)

> ANIMAX uses `discord.py-self`. Discord prohibits automated user accounts and may terminate them; an official bot account is the compliant alternative. Only download or redistribute media you are authorized to share. Keep your token private. Live Discord uploads and account entitlement have not been verified for this release.
