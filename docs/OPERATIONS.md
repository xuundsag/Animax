# Operations

## Settings

`.settings` lists all configurable fields and their current values in a code block. `.settings quality` shows valid choices; `.settings quality 1080` validates, saves and applies the value. Invalid values show the valid choices/range without changing configuration. Channel changes also check access and upload permissions.

Settings changes are blocked during downloads, reset and refetch. Listing/querying values still works. Saved preferences live in private `settings.json` and override `.env` on restart. Tokens are never accepted or displayed through this command. Reset does not remove settings.

## Recover history

```text
.refetch #channel
.refetch 123456789012345678
```

Refetch only runs when the on-disk `anime.json` is missing or empty. It refuses to overwrite populated history or a malformed nonempty JSON file. Blank files and `{}` are treated as empty at startup.

The command scans readable channel history, newest first, using Discord's paginated history API. It accepts only messages authored by the logged-in account:

- The newest valid, useful `anime.json` attachment can restore that account's uploaded records across its recorded destinations. Snapshots up to 20 MiB are supported.
- Recognizable video filenames such as `MAL-21_EP-0001_dub.mp4` rebuild or supplement records from the requested channel.
- A `SUB (dub unavailable)` caption preserves the original dub request with actual sub audio.
- Pending/running jobs are not imported, so restoring history does not restart old jobs.

The scan does not infer MAL IDs from arbitrary renamed videos. Deleted messages, unreadable channels, exports from other accounts and unknown filename formats cannot be recovered this way. Records restored from a snapshot can point to videos deleted since that snapshot; other destinations are not revalidated.

Nothing is committed until the scan succeeds. Cancellation, timeout or connection failure leaves the previous journal untouched. The default deadline is 600 seconds; `.settings refetch_timeout 1200` allows a longer scan. `.cancel` stops refetch. Discord's own rate-limit handling still applies.

## Reset

`.reset` asks for a one-use confirmation code in the same channel, valid for 60 seconds. `.reset cancel` aborts before confirmation. Running jobs/refetch must stop first.

Confirmation clears anime history, saved jobs, legacy state and the entire downloaded-video cache. It keeps `.env`, `settings.json`, title overrides, existing Discord messages, logs and cooldowns. A subsequent range beginning at episode 1 downloads and uploads it again, so duplicates are intentional after reset.

Reset uses staged file moves and a journal commit. Startup rolls back an interrupted pre-commit reset; after commit it finishes cache deletion without restoring history. There is no interactive undo after commit.

## Command messages

By default, the account's command trigger is deleted before the response. If deletion fails or `delete_triggers` is off, the response references the trigger without a mention notification. Unrecognized commands from unrelated prefix text are not deleted automatically.

`.getlist` sends an attachment without accompanying text. `.help` lists command names without the old footer; `.help NAME` remains available. Video messages reply to the nearest earlier uploaded episode of the same anime/account/destination, not to the command. New video captions have no download hyperlink.

With nonzero `channel`, video uploads go there and automatic episode statuses remain in the command channel. With channel 0, a successful episode's automatic status is removed only after its accepted ID is saved. Failed/cancelled/uncertain statuses stay.

Enhancing displays elapsed minutes:seconds, updated at `status_interval` rather than every second. Uploads do not display an invented byte percentage.

After a confirmed send and successful journal commit, the episode status reads `Done` with the actual decimal file size, for example `398.1 MB`, instead of `Uploaded` and `100%`. Status cleanup after a restart uses the committed size, even if the video cache has already been removed. Legacy records without a size show `size unavailable`, not an invented number. Failed or ambiguous sends are never labeled Done. The existing channel-dependent retention/deletion policy is unchanged.

## Retry and upload safety

`.download MAL_ID dub|sub START END [resume|force]` uses inclusive bounds. Normal mode skips recorded uploads. `.resume`/`.retry` resumes the latest saved job. Force intentionally permits duplicates.

Dub-to-sub fallback requires positive provider evidence of an unavailable source. A timeout, rate limit, caption failure or Discord error does not authorize switching audio. Fallbacks are labeled.

CDN cooldowns persist through restart and reset. No episode hopping, proxy rotation or fragment-skipping is used to evade limits. Do not run parallel downloaders to bypass a cooldown.

The video message and attachment are verified before the ID is committed and status/cache cleanup runs. Transient send failures use bounded destination checks and cached-file transport fallback; a found upload is recorded without resending. Inconclusive checks pause the job. Verification/retry/error details stay in the terminal/logs; Discord displays only Uploading, Done with size, or Paused for this flow. Exactly-once delivery cannot be guaranteed across a remote API and a local crash. See [upload recovery](UPLOADS.md) for settings, stop conditions and recovery of older pending reports.

## Interrupted downloads and HTTP 416

An `exit=-15` process log means the downloader received SIGTERM. Cancellation, shutdown or another process can cause it; that line alone does not identify the sender. A later `HTTP Error 416: Range Not Satisfiable` means the CDN rejected a requested byte range. Repeating the same failed fragment after interruption can indicate stale local resume state, though provider/manifest problems can also cause 416.

Starting with v2.2.2:

- Before each transfer, orphaned per-fragment files are removed. The assembled `.part` and `.ytdl` completed-fragment checkpoint remain, so a normal interrupted HLS transfer resumes at the next unfinished fragment without reusing its stale byte offset.
- A failed download with HTTP 416 clears only `source.*` transfer files, the source fingerprint and download progress in that episode's work directory. It retries from the beginning once within the original download timeout budget.
- Persistent 416 returns to the existing bounded episode retry loop, which resolves a fresh URL. There is at most one extra clean retry per resolved transfer, not an unlimited loop.
- HTTP 429 still stops immediately and follows the persisted cooldown. Cancellation and unrelated failures do not trigger a clean retry. Fragments are never skipped to produce a supposedly complete episode.

This recovery does not clear `anime.json`, saved settings, other episodes or prepared `episode.mp4` files. Do not use `.reset` to repair this error. Install the new application code while the bot is stopped, keep private configuration/history/downloads, restart, and use `.resume` if the job does not resume automatically. Existing `fast` preferences remain unchanged; switch to `.settings preset veryfast` while idle if you also need the speed fix.

Python 3.10's deprecation warning is separate from HTTP 416. Python 3.11+ is recommended, but replacing application files does not upgrade the interpreter or its virtual environment.

## Services and diagnostics

```bash
sudo python3 manage.py service status
sudo python3 manage.py service restart
sudo python3 manage.py service logs
python3 manage.py service status-detached
python3 manage.py service stop-detached
.venv/bin/python manage.py doctor
```

`manage.py doctor --source MAL_ID dub|sub EP` checks browser/manifest access, not a complete episode upload. Avoid repeated source checks during cooldowns.

Logs live under `logs/`; upload JSON reports separate sending, confirmation and uncertain outcomes. Media logs include exit codes. Detailed logs may contain signed URLs, so redact them before sharing.

For a fresh source directory, keep your private configuration and history separately and copy them deliberately while the old process is stopped, or use `manage.py migrate --from /path/to/old/project`. Migration copies private data, not program code. There is no release updater. Do not start two installations using the same account/destination at once.
