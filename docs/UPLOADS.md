# Upload verification and recovery

v2.3.1 verifies **every newly uploaded episode**, not only files over 500 MB. It does not recompress videos, remove captions or change frame rate during upload recovery.

## Delivery sequence

1. Check View Channel/Read Message History permissions for guild channels and read the newest destination message ID before the initial upload. This becomes the boundary for later history checks. Missing history permission can make Discord return an empty list, so an empty API response alone is not treated as permission proof. If access cannot be established, no new file is sent.
2. Persist an upload intent containing account, channel, filename, exact byte size, caption, correlation nonce and history boundary. Intent files use atomic replacement, private file permissions and fsync.
3. Upload using the selected method. Before posting, persist that the outcome may be uncertain if the process stops.
4. After a successful API response, fetch the returned message ID and check its author, channel and attachment metadata. A matching message found in destination history can also confirm delivery after a lost response.
5. Only then commit the message ID to `anime.json` and finalize `Done | actual size`. Cache cleanup follows the journal commit.

A candidate must belong to this account/channel and contain exactly one attachment with the expected filename and byte size. The correlation nonce or original caption must also match. Conflicting metadata or multiple matching messages stop recovery for review; the bot never deletes potential duplicate messages automatically.

## Transient failure fallback

After a transient server/network error, checks run immediately and after waits of 5 and 15 seconds, subject to the verification and overall deadlines. History scans are limited to 500 post-boundary messages. A limit hit, read error or timeout is **not** evidence of absence.

If a match is found, ANIMAX saves it without sending again. If all readable checks find no match, it may reopen the same cached video and retry. With fallback enabled, cloud switches to multipart, or multipart switches to cloud when available. If only multipart is available, it can retry that same method.

The default is two application-level send attempts per invocation, with at most three configurable. The SDK can perform its own HTTP retries inside one invocation; the configured count is not a raw network-request count. Both application attempts share the original upload deadline. No new download or encode is performed between them.

The same nonce is retained for recovery attempts. It is used for correlation, **not as a promise of server-side deduplication**. A server can make a message visible after all absence checks; exactly-once delivery cannot be guaranteed. After exhaustion, earlier ambiguous posting outcomes remain flagged for recovery instead of being treated as definitely never sent.

## Stops instead of blind retries

- HTTP 401, 403, 413 and 429 do not trigger an automatic alternate-method retry.
- Unreadable, incomplete or conflicting history does not authorize a resend.
- If a send returned a message ID but that message cannot be verified, the bot pauses rather than assuming it is safe to send again. This also avoids blindly replacing moderated/deleted messages.
- A changed cached file, missing/corrupt intent, cancellation or a confirmation deadline stops further sending as appropriate. State and the video are kept.
- Upload limits and account permissions remain enforced. A 700 MB attachment can still fail on Discord, and transport fallback cannot override account limits or an outage.

After a pause, inspect the terminal/report. An explicit `.resume` can start a new bounded recovery cycle, checking the prior intent first. It cannot bypass unknown or conflicting verification. No background retry loop runs forever, and the episode range does not advance past an unconfirmed upload.

## Existing pending episodes

Each episode keeps `upload-state.json` in its download work directory. The human-readable report remains `logs/upload-MAL-LANGUAGE-EP.json`. Keep both logs and downloads during installation.

For older reports without a nonce/history boundary, recovery uses the recorded UTC start time, current account, original destination, filename, exact file size and caption. This is weaker than a modern anchored intent, so conflicts or a capped history scan require review.

Pending-upload recovery may use the original prepared cache even if current encoding preferences changed. Its identity is checked before any resend; other episodes still use current settings. If the matching video/report is unavailable, recovery stops instead of downloading and encoding a replacement for an unresolved send.

An explicit force command for an already confirmed delivery still intentionally permits another copy. Pending/uncertain sends are checked first even when force is requested. Use `.resume` for recovery rather than repeatedly forcing a range.

## Discord versus terminal

Discord's episode status stays `Uploading` while checks and retries happen. It becomes `Done` with the actual size after verified delivery and a successful journal commit. If recovery stops, it becomes `Paused`, without HTTP codes, check steps or log paths in the message. Detailed reasons are printed to the terminal and written to logs.

The existing status-retention policy is unchanged: configured destination channels retain successful statuses; channel 0 deletes them only after verified/journaled delivery.

## Settings

| Command | Environment key | Default |
| --- | --- | --- |
| `.settings upload_mode auto` | `UPLOAD_MODE` | `auto` |
| `.settings upload_attempts 2` | `UPLOAD_ATTEMPTS` | `2` (1-3) |
| `.settings upload_fallback on` | `UPLOAD_FALLBACK` | `true` |
| `.settings upload_check_timeout 60` | `UPLOAD_CHECK_TIMEOUT` | `60` seconds (30-300) |
| `.settings upload_timeout 1800` | `UPLOAD_TIMEOUT` | `1800` seconds |

The verification setting bounds one verification sequence; the whole operation is also bounded by `upload_timeout`. Settings changes require the bot to be idle. Turning fallback off retains the chosen transport while keeping verification and the attempt cap.

The implementation is tested with mocked Discord responses, including delayed visibility, accepted messages followed by HTTP 500, absent messages, rate limits and large sparse files. It has not been verified against a live Discord account or a real 700 MB Discord upload.
