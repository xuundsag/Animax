import { volumeGain } from './stream_queue.mjs';

const TS_OUTPUT = [
  '-mpegts_flags', '+resend_headers+initial_discontinuity',
  '-muxdelay', '0',
  '-muxpreload', '0',
];

export function buildRemuxArgs({
  file,
  kind = 'episode',
  offset = 0,
  seekSeconds = 0,
  volume = 1,
  pauseDuration = null,
} = {}) {
  const audioVolume = volumeGain(volume);
  const base = ['-hide_banner', '-loglevel', 'error', '-nostdin'];
  const timestampOffset = ['-output_ts_offset', String(Math.max(0, Number(offset) || 0))];
  const audioFilter = [
    '-af',
    `volume=${audioVolume.toFixed(2)},alimiter=limit=0.98:level=false:attack=5:release=50`,
  ];
  const duration = pauseDuration === null
    ? []
    : ['-t', String(Math.max(0.1, Number(pauseDuration) || 0.1))];
  if (kind === 'pause') {
    return [
      ...base,
      '-re', '-f', 'lavfi', '-i', 'color=c=black:s=1920x1080:r=30',
      '-re', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
      '-map', '0:v:0', '-map', '1:a:0',
      '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
      '-threads:v', '1', '-pix_fmt', 'yuv420p', '-r', '30', '-g', '60',
      '-c:a', 'aac', '-b:a', '128k',
      ...audioFilter,
      ...duration,
      ...TS_OUTPUT,
      ...timestampOffset,
      '-f', 'mpegts', 'pipe:1',
    ];
  }
  if (typeof file !== 'string' || !file) throw new TypeError('A media file is required for remuxing.');
  const seek = Number(seekSeconds);
  const seekArgs = Number.isFinite(seek) && seek > 0 ? ['-ss', String(seek)] : [];
  return [
    ...base,
    ...seekArgs,
    '-re', '-i', file,
    '-map', '0:v:0', '-map', '0:a:0?',
    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
    ...audioFilter,
    ...TS_OUTPUT,
    ...timestampOffset,
    '-f', 'mpegts', 'pipe:1',
  ];
}
