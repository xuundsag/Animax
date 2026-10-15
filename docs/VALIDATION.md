# Validation

## Local checks

149 tests passed on Python 3.13/Linux for v2.3.1. Media tests use real FFmpeg; Discord tests use mocks, not an account login.

Coverage includes:

- Source-rate preservation at 24000/1001, 24, 30000/1001, 30 and 60 fps.
- 1080p H.264 output, full-frame padding and single-pass enhancement.
- Actual pixel checks distinguishing transparent/outlined sub captions from opaque dub boxes.
- Quality mode without a bitrate ceiling and without automatic size-fitting recompression.
- Settings validation, restart persistence, channel access checks, secret-field rejection and atomic-save failure.
- Command-trigger deletion and reply fallback when deletion fails or is disabled.
- Refetch from exports/uploads, own-account filtering, sub-fallback recovery, latest-message ordering and no job resurrection.
- Refetch rejection of populated history and no partial commit after network failure, timeout or cancellation.
- Reset confirmation, rollback, restart recovery, symlink refusal and episode-1 download after reset.
- Journal/resume, search, attachment-only export, reply chains, cooldowns and uncertain-send handling.

Ruff lint/format checks, installer shell syntax and Python 3.10 AST syntax are checked separately. AST compatibility is not a full Python 3.10 runtime test. The GitHub Actions workflow defines Python 3.11/3.13 jobs; hosted CI has not run from this workspace.

## v2.3.1 upload recovery

38 new checks cover explicit history-permission checks (including a permission revoked after sending and a missing member-cache entry); message-ID readback; accepted-but-500 responses without resending; delayed visibility; absence followed by cloud/multipart fallback; multipart-to-cloud fallback; fallback disabled; bounded exhaustion; unchanged nonce within recovery; intent persisted before posting; cancellation; verification timeouts; disk-write failure; changed local files; conflicting sizes/captions; multiple matches; history scan limits; wrong authors; 401/403/413/429 stop conditions; legacy-report recovery; pending-cache reuse after profile changes; journal recovery without a second send; intentional force of a confirmed delivery; persisted settings; and terminal-only check/retry details.

The large-file fallback test uses a sparse 738,676,633-byte local file with mocked Discord transport. It does not send those bytes to Discord or establish that Discord accepts that file size. Current tests block Discord HTTP requests and fail if a test accidentally attempts one. Media/HLS fixture servers remain local.

The full suite passed 149 tests with 116 dependency deprecation warnings in 33.15 seconds on this run. Delayed-visibility tests replace waiting intervals with zero so they test ordering and outcomes without a wall-clock delay; the production waits are 5 and 15 seconds. No live-account upload, authenticated history read, hosted CI run or real 700 MB Discord transfer was performed. SDK internal retries and very late server visibility remain outside exactly-once guarantees.

## v2.3 status and allocation checks

New checks cover the default profile; exact `Done | 398.1 MB` formatting with backticks; per-episode sizes rather than reuse of the previous episode's size; no Done status before send confirmation; restoration of retained statuses from the journal after restart; advisory-setting explanations; and a mocked 600 MB upload that is not recompressed merely for exceeding the preferred range.

A real FFmpeg fixture contains four seconds of flat gray followed by four seconds of synthetic motion, at 1440×1080/24 fps, padded to 1920×1080. The shipped veryfast/CRF 19 profile produced:

| Equal-duration section | Encoded video packet bytes |
| --- | ---: |
| Flat gray | 7,737 |
| Synthetic motion | 2,716,080 |

The complete MP4 was 2,726,824 bytes, including container overhead. This intentionally simple fixture verifies adaptive allocation; it does not predict an anime episode's size, prove perceptual quality in fights, or establish a speedup over v2.2.2. An experimental AQ-mode-2/qcomp-0.7 profile grew the same fixture to 4,156,645 bytes and was rejected. The shipped encoder parameters remain those of v2.2.2; no new forced reference/look-ahead settings were added.

The 400-500 MB preference is not enforced by these tests or the encoder. Quality mode is also tested without an average-bitrate command, VBV ceiling or FPS override. Compatible prepared caches remain reusable even if the advisory midpoint changes.

## v2.2.2 transfer recovery

Local integration tests use actual yt-dlp and FFmpeg against a three-fragment HLS server:

- An interrupted download retains its completed first fragment, discards a stale partial second fragment and requests only fragments two and three, without a stale Range header. The resulting MP4 has the complete three-second duration.
- A transient 416 triggers one clean retry and produces a complete MP4.
- Persistent 416 stops after two downloader invocations and never reaches encoding. yt-dlp itself can make a no-Range probe after a 416, so these two invocations produce four fragment HTTP requests in this fixture.
- A 429 after the clean retry stops immediately with its Retry-After value intact.

Unit tests verify scoped cleanup, preservation of settings/history/completed media, symlink safety and no clean retry on cancellation or 403. These tests do not establish availability of the user's CDN or perform a live Discord upload.

## v2.2.1 speed check

The same 192-frame (~8-second), 1440×1080 synthetic moving-pattern fixture was encoded with burned dub captions, 1080p padding, source 24000/1001 fps, CRF 19, four threads and no bitrate ceiling. Two runs per profile used alternating order on this sandbox, with no audio.

| Profile | Run 1 | Run 2 | Median |
| --- | ---: | ---: | ---: |
| v2.2 fast + fixed refs/look-ahead | 3.948 s | 3.924 s | 3.936 s |
| v2.2.1 veryfast + preset-native analysis | 2.309 s | 2.292 s | 2.301 s |

The new profile took about 42% less elapsed time on this short fixture. This is not a full-episode or user-VPS benchmark and does not establish a return to six-minute processing. Both the preset and its overrides changed, so this does not isolate their individual contributions. Preset selection is now tested at ultrafast, superfast, veryfast and fast; the default still passes the source-FPS, caption and synthetic quality-regression checks. Identical CRF does not mean identical quality across presets.

## Original v2.2 motion fixture

These historical measurements describe the original `fast` profile, not the v2.2.1 default.

A three-second, 1440×1080, 24 fps synthetic moving-pattern source was padded to 1920×1080 and encoded with four threads. The comparison was against the same lossless fixture, including its black padding.

| Profile | SSIM | Encoded bytes |
| --- | ---: | ---: |
| Old-style ultrafast, CRF 23, 1.96 Mb/s VBV ceiling | 0.990156 | 1,133,728 |
| v2.2 fast, CRF 19, no VBV ceiling | 0.998492 | 2,244,784 |

Higher SSIM means closer pixel structure for this fixture. These numbers are not a full-episode bitrate estimate, a subjective anime-quality score, a speed benchmark or a promise of no visible artifacts. The larger output is an expected consequence of removing bitrate starvation.

## Source review

The after-mode review used miqdadbadjuber/anti-slop at commit `b1cdf87d49b1653293d58424516fd80394008345`. No new blocking findings were identified; no global plugin settings were installed.

- R-02 PASS: authored application/docs copy was scanned for prohibited punctuation.
- R-17/R-36/R-38 PASS: measurements identify their fixture and limits; no live-upload, exact-font, lossless-quality or guaranteed speed claim is made.
- R-23/R-31/R-37 PASS: caption/padding direction comes from the supplied screenshots and requirements; font license and encoding/resource trade-offs are documented.
- R-26/R-27/R-35 PASS within local scope: command callbacks and success/empty/error/cancellation paths were exercised. Actual Discord account operations remain untested.
- R-33 PASS: new command, recovery and presentation behavior is implemented in source modules; no source-patching/updater script is shipped.
- Code-comment review PASS: remaining comments describe protocol, concurrency, timing or safety constraints. No decorative comment banners were added.

Website-only navigation/theme/responsive-layout rules are not applicable. Discord owns its client UI; this review does not certify Discord's mobile layout or arbitrary terminal colors.

## Not verified

No live Discord upload, account entitlement check, full live V2.2 episode download, installed systemd service/reboot test or authenticated GitHub push was performed. Provider caption accuracy and upstream picture quality remain external limitations. Logs and generated test media are not shipped in the release ZIP.
