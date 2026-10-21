import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, rmSync, createWriteStream } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { Encoders, prepareStream, Utils } from '@dank074/discord-video-stream';
import { buildRemuxArgs } from '../animax/stream_remux.mjs';

const work = mkdtempSync(join(tmpdir(), 'animax-feed-'));

function run(program, args) {
  const result = spawnSync(program, args, { encoding: 'utf8', timeout: 60000 });
  if (result.error || result.status !== 0) {
    throw new Error(`${program} failed: ${result.error || result.stderr}`);
  }
}

async function* runRemux(args, tag) {
  const child = spawn('ffmpeg', args, { stdio: ['ignore', 'pipe', 'pipe'] });
  const closed = once(child, 'close');
  let errorText = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (text) => { errorText = `${errorText}${text}`.slice(-4000); });
  for await (const chunk of child.stdout) yield chunk;
  const [code, signal] = await closed;
  if (code !== 0) throw new Error(`${tag} remux failed (${code ?? signal}): ${errorText}`);
}

async function* transportStream(files, duration) {
  let offset = 0;
  for (let index = 0; index < files.length; index += 1) {
    yield* runRemux(buildRemuxArgs({
      file: files[index],
      kind: 'episode',
      offset,
      volume: 1,
    }), `episode ${index + 1}`);
    offset += duration;
    if (index === 0) {
      yield* runRemux(buildRemuxArgs({
        kind: 'pause',
        offset,
        volume: 1,
        pauseDuration: 1,
      }), 'pause slate');
      offset += 1;
    }
  }
}

try {
  const files = [];
  for (let index = 0; index < 3; index += 1) {
    const file = join(work, `episode-${index + 1}.mp4`);
    run('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-y',
      '-f', 'lavfi', '-i', `testsrc2=size=320x180:rate=24:duration=2`,
      '-f', 'lavfi', '-i', `sine=frequency=${440 + index * 100}:duration=2`,
      '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
      '-c:a', 'aac', file,
    ]);
    files.push(file);
  }

  const prepared = prepareStream(Readable.from(transportStream(files, 2)), {
    encoder: Encoders.software({ x264: { preset: 'ultrafast', tune: 'zerolatency' } }),
    width: 1920,
    height: 1080,
    frameRate: 30,
    videoCodec: Utils.normalizeVideoCodec('H264'),
    bitrateVideo: 4000,
    bitrateVideoMax: 6000,
    bitrateAudio: 128,
    includeAudio: true,
    hardwareAcceleratedDecoding: false,
    minimizeLatency: false,
    customFfmpegFlags: ['-threads:v', '2'],
    logLevel: 'error',
  });
  const output = join(work, 'continuous.nut');
  await Promise.all([
    prepared.promise,
    pipeline(prepared.output, createWriteStream(output)),
  ]);
  const probe = spawnSync('ffprobe', [
    '-v', 'error', '-show_entries', 'format=duration',
    '-show_entries', 'stream=codec_type,width,height', '-of', 'json', output,
  ], { encoding: 'utf8', timeout: 30000 });
  if (probe.status !== 0) throw new Error(`ffprobe failed: ${probe.stderr}`);
  const info = JSON.parse(probe.stdout);
  const video = info.streams.find((stream) => stream.codec_type === 'video');
  const duration = Number(info.format.duration);
  if (video?.width !== 1920 || video?.height !== 1080 || duration < 6.5 || duration > 7.5) {
    throw new Error(`Unexpected continuous feed: ${JSON.stringify({ video, duration })}`);
  }
  console.log(`Continuous feed with pause slate passed: ${video.width}x${video.height}, ${duration.toFixed(2)}s.`);
} finally {
  rmSync(work, { recursive: true, force: true });
}
