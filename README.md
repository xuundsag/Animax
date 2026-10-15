# ANIMAX

Anime downloader from anikoto api for discord enjoy !
## Requirements

- Python 3.10+ and pip (3.11+ recommended).
- FFmpeg and ffprobe on PATH; Chromium system libraries. Chromium is downloaded on first start.
- Packages on requirements.txt

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set `DISCORD_TOKEN` and configure other options .

```bash
python bot.py
```
**use pm2 to run this bot in background**
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
