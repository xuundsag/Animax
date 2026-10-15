# ANIMAX

Downloads anime by MAL ID and sends one complete video per episode. Different anime share a worker queue; episodes stay sequential within each anime. Uploads run **one at a time**, including verification and retries. Anime channels are created automatically, worker progress goes to the log channel, and jobs/history are saved in `anime.json`. Use `.help` in Discord for commands.

## Requirements

- Python 3.10+ and pip (3.11+ recommended).
- FFmpeg and ffprobe on PATH; Chromium system libraries. Chromium is downloaded on first start.
- A Discord account with messaging, attachment, history and channel-creation permissions. Uses `discord.py-self`; automated user accounts can be banned by Discord. Share only authorized media.

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set `DISCORD_TOKEN`, `LOG_CHANNEL_ID` (`0` uses the command channel), and `ENCODE_THREADS` (shared across workers).

```bash
python bot.py
```

## Structure

```text
bot.py              entry point
animax/             workers, routing, media and uploads
assets/fonts/       caption fonts
config/             example JSON files
requirements.txt    Python dependencies
.env.example        configuration template
anime.json          saved jobs, channels and history (runtime)
downloads/          episode cache (runtime)
logs/               process and upload logs (runtime)
```
