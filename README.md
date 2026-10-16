# ANIMAX

Downloads whole MP4 episodes by MAL ID. Workers handle anime concurrently; episodes stay sequential per anime, uploads are serialized, and jobs/routes are saved in `anime.json`.

Use `.download MAL_ID dub|sub START END [resume|force] [#channel]` for an inclusive range. First use in a server requires the destination channel mention last, e.g. `.download 21 dub 1 10 #one-piece`. Later runs reuse the saved channel and ignore a supplied mention. ANIMAX never creates channels.

The safe output limit is 950 MB. By default, smaller inputs use CRF 0 through the required padding/caption pass; larger or oversized output is compressed only as needed. `.settings compressall true|false` controls all-video compression. `.settings lossless true` forces CRF 0 and overrides compressall; oversized lossless files are not compressed and may be refused. Choose caption fonts with `.settings caption_font`.

## Requirements

- Python 3.10+ and pip (3.11+ recommended).
- FFmpeg, ffprobe and Chromium system libraries; Chromium downloads on first start.
- A Discord account with message, attachment and history permissions. Uses `discord.py-self`; automated user accounts can be banned by Discord.

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
```

Set `DISCORD_TOKEN`, `LOG_CHANNEL_ID` (`0` uses the command channel) and `ENCODE_THREADS` in `.env`, then run `python bot.py`.

## Structure

```text
bot.py              entry point
animax/             workers, routing, media and uploads
assets/fonts/       bundled caption fonts and licenses
config/             example JSON files
requirements.txt    Python dependencies
.env.example        configuration template
anime.json          saved jobs, channels and history (runtime)
downloads/          episode cache (runtime)
logs/               process and upload logs (runtime)
```
