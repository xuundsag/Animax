# Encoding

## v2.3 profile

| Option | Default |
| --- | --- |
| Video | H.264, 1080p canvas, yuv420p |
| Encoder | x264 `veryfast`, CRF 19 |
| CPU threads | 4 decoder/encoder threads; 1 filter thread |
| Frame rate | Source timestamps/FPS |
| Audio | AAC, 128 kb/s |
| Compression policy | Quality, without a duration-based bitrate ceiling |
| Preferred size | 400-500 decimal MB; midpoint 450 MB |

The preferred range is advisory. Fixed CRF controls quality, not total bytes. Episode duration, source noise/detail and motion determine size; a 700 MB output can still be valid under this policy. The software does not secretly increase CRF, reduce resolution/FPS, strip captions or run another encode to hit 500 MB.

`target_size` defaults to 450. In quality mode, it defines a reporting band of target minus/plus 50 MB. `SIZE_PREFERENCE` logs the actual size and whether it is below, within or above the band. **This setting does not change the encoder's bitrate or CRF in quality mode.**

## Still frames and action

The native veryfast preset retains x264's adaptive quantization and macroblock-tree allocation. Inter-frame prediction encodes unchanged regions cheaply; motion and changing detail can use more bits. There is no fixed per-scene allocation that reserves a large share for static scenes.

Quality mode adds no `-b:v`, `-maxrate` or `-bufsize`. It keeps the preset's reference-frame, B-frame and look-ahead choices. Only `sync-lookahead=0` is overridden to avoid that extra queue. No animation tune, smoothing, denoising or sharpening filter is added.

A local static-to-motion fixture confirmed that motion received far more encoded packet bytes. This verifies adaptive allocation for that fixture, not subjective quality in every anime. Stronger action-biased AQ/qcomp settings were evaluated but not shipped: they increased fixture size substantially. There is no claim that v2.3 universally makes files smaller than v2.2.2 at the same settings.

CRF 19 is still lossy. Already-blocky source video cannot be repaired by re-encoding. Absolute freedom from blur, blocking or banding is not guaranteed. Choosing a slower preset can improve compression efficiency but costs CPU time; v2.3 keeps veryfast as requested.

## Upload guards and optional size mode

`max_size` and `upload_limit` are safety limits, not quality targets. Their defaults remain 1,000 decimal MB. Set them to an allowance actually supported by the account. Files exceeding the smaller guard are retained and refused, not split or silently recompressed in quality mode, even if `reencode` was previously enabled.

The older `.settings compress size` mode remains available for explicit use. It applies a duration-based bitrate budget from `target_size`, and `reencode on` permits a bounded size-fitting pass. This is not the v2.3 default and can sacrifice action detail. Do not select it when you want an unconstrained CRF 19 encode.

No mode splits or truncates episodes. Files smaller than the preferred range are not inflated.

## Frame rate, geometry and captions

`fps source` adds no FPS filter or output `-r`. Timestamp passthrough avoids an implicit CFR conversion. 24000/1001 is approximately 23.976 fps, not 23 or exactly 24. Source 30/60 fps stays 30/60, which can increase work and size compared with a 24 fps conversion.

The full source display aspect, including sample aspect ratio and rotation, fits inside a 16:9 canvas. A 4:3 1080p picture receives side bars without cropping or stretching. Bicubic scaling does not create real detail in a low-resolution source.

Sub videos use white outlined captions with no filled background. Dub captions retain opaque black boxes. Provider wording/timestamps are kept; no translation is generated. Hardcoded upstream caption backgrounds cannot be removed. Dub always burns an advertised English track when available; burn errors never silently switch to soft captions.

Inspect a saved video with:

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,avg_frame_rate \
  -of default=noprint_wrappers=1 episode.mp4
```

## Resource use and caching

Chromium closes before downloading/encoding. Native HLS uses one fragment worker. Uploads stream from disk rather than buffering entire videos in Python memory. Thread settings are not a strict whole-process CPU/RAM quota. No specific VPS completion time is promised.

`ENCODE_START` and `ENCODE_DONE` log settings and elapsed encode time. The default encode pipeline is unchanged from v2.2.2, so matching prepared caches remain compatible. The advisory size midpoint does not invalidate a quality-mode cache. Changing quality, CRF, preset, FPS or compression mode can require a new encode. Saved settings override `.env`; see the README for selecting the profile on an existing installation.

## Common settings

| Command field | Environment key | Default |
| --- | --- | --- |
| `quality` | `OUTPUT_HEIGHT` | `1080` |
| `compress` | `COMPRESSION_MODE` | `quality` |
| `preset` | `VIDEO_PRESET` | `veryfast` |
| `crf` | `VIDEO_CRF` | `19` |
| `threads` | `ENCODE_THREADS` | `4` |
| `fps` | `FRAME_RATE` | `source` |
| `target_size` | `TARGET_SIZE_MB` | `450` |
| `max_size` | `MAX_OUTPUT_MB` | `1000` |
| `upload_limit` | `UPLOAD_LIMIT_MB` | `1000` |
| `reencode` | `ALLOW_SIZE_REENCODE` | `off` |

`.settings` lists every configurable field. `.settings FIELD` shows valid values. Invalid values do not change the saved configuration.
