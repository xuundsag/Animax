import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, lstatSync, realpathSync } from 'node:fs';
import { isAbsolute, resolve, sep } from 'node:path';
import { Readable } from 'node:stream';
import { Client } from '@lng2004/discord.js-selfbot-v13';
import { Encoders, playStream, prepareStream, Streamer, Utils } from '@dank074/discord-video-stream';

const root = resolve(process.env.ANIMAX_ROOT || process.cwd());
const secret = process.env.ANIMAX_API_KEY || '';
const pythonApi = (process.env.ANIMAX_PYTHON_API || '').replace(/\/$/, '');
const streamToken = process.env.STREAM_DISCORD_TOKEN || '';

if (!secret || !pythonApi || !streamToken) throw new Error('Required streaming-account sidecar environment is missing.');

const log = (message, error = undefined) => {
  const detail = error ? ` ${error?.stack || error}` : '';
  console.error(`[${new Date().toISOString()}] ${message}${detail}`);
};

const client = new Client({ checkUpdate: false });
const streamer = new Streamer(client);
let discordReady = false;
let activeSeries = null;
let joinedGuild = null;
let joinedChannel = null;

function json(response, status, body) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  response.end(JSON.stringify(body));
}

function authorized(request) {
  return request.headers.authorization === `Bearer ${secret}`;
}

async function readJson(request) {
  let body = '';
  for await (const part of request) {
    body += part;
    if (body.length > 128 * 1024) throw new Error('Request body is too large.');
  }
  return body ? JSON.parse(body) : {};
}

async function postPython(endpoint, payload) {
  const response = await fetch(`${pythonApi}${endpoint}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${secret}` },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(5000),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) throw new Error(result.error || `Python API HTTP ${response.status}`);
  return result;
}

function safeMediaPath(input) {
  if (typeof input !== 'string' || !isAbsolute(input)) throw new Error('Media path must be absolute.');
  const resolved = resolve(input);
  const base = resolve(root, 'downloads') + sep;
  if (!resolved.startsWith(base) || !existsSync(resolved) || lstatSync(resolved).isSymbolicLink()) {
    throw new Error('Media path is outside the ANIMAX downloads directory or is missing.');
  }
  const real = realpathSync(resolved);
  if (!real.startsWith(base)) throw new Error('Media path escapes the downloads directory.');
  return real;
}

async function* remuxToTransportStream(segment, offset, signal) {
  const file = safeMediaPath(segment.filePath);
  const tag = `${segment.kind} E${segment.episode}`;
  log(`FFMPEG_REMUX_START ${tag} file=${file} duration=${segment.duration}s`);
  const args = [
    '-hide_banner', '-loglevel', 'error', '-nostdin', '-re', '-i', file,
    '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
    '-mpegts_flags', '+resend_headers+initial_discontinuity',
    '-muxdelay', '0', '-muxpreload', '0',
    '-output_ts_offset', String(Math.max(0, offset)),
    '-f', 'mpegts', 'pipe:1',
  ];
  const child = spawn('ffmpeg', args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  const closed = once(child, 'close');
  const spawnError = once(child, 'error').then(([error]) => { throw error; });
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk) => { stderr = `${stderr}${chunk}`.slice(-12000); });
  const terminate = () => { if (child.exitCode === null) child.kill('SIGTERM'); };
  signal.addEventListener('abort', terminate, { once: true });
  let firstChunk = true;
  try {
    for await (const chunk of child.stdout) {
      if (signal.aborted) break;
      if (firstChunk) {
        firstChunk = false;
        log(`FFMPEG_REMUX_OUTPUT ${tag} first_chunk_bytes=${chunk.length}`);
      }
      yield chunk;
    }
    const [code, signalName] = await Promise.race([closed, spawnError]);
    if (!signal.aborted && code !== 0) throw new Error(`FFmpeg transport remux failed (${code ?? signalName}): ${stderr.slice(-4000)}`);
    log(`FFMPEG_REMUX_END ${tag} exit=${code ?? signalName} aborted=${signal.aborted}`);
    return { aborted: signal.aborted };
  } finally {
    signal.removeEventListener('abort', terminate);
    if (child.exitCode === null) terminate();
  }
}

class SeriesQueue {
  constructor(seriesId, signal) {
    this.seriesId = seriesId;
    this.signal = signal;
    this.items = [];
    this.waiters = [];
    this.ended = false;
    this.aborted = false;
    this.active = null;
    this.skipEpisodes = new Set();
    this.offset = 0;
  }

  push(item) {
    if (this.ended || this.aborted) throw new Error('The active stream queue is closed.');
    const waiter = this.waiters.shift();
    if (waiter) waiter(item);
    else this.items.push(item);
  }

  close() {
    this.ended = true;
    for (const waiter of this.waiters.splice(0)) waiter(null);
  }

  abort() {
    this.aborted = true;
    this.close();
    this.active?.controller?.abort(new Error('Stream stopped.'));
  }

  async take() {
    while (!this.items.length && !this.ended && !this.aborted && !this.signal.aborted) {
      const item = await new Promise((resolveItem) => this.waiters.push(resolveItem));
      if (item) return item;
    }
    return this.items.shift() || null;
  }

  skipEpisode(episode) {
    this.skipEpisodes.add(Number(episode));
    if (this.active && this.active.episode === Number(episode) && this.active.kind === 'episode') {
      this.active.controller.abort(new Error('Episode skipped.'));
    }
  }

  async report(item, skipped = false, error = '') {
    try {
      await postPython('/internal/segment-event', {
        seriesId: this.seriesId,
        episode: item.episode,
        kind: item.kind,
        skipped,
        error: String(error || '').slice(0, 1000),
      });
    } catch (exception) {
      log('Could not notify Python about a completed stream segment.', exception);
    }
  }

  async *stream() {
    try {
      while (!this.aborted && !this.signal.aborted) {
        const item = await this.take();
        if (!item) break;
        if (item.kind === 'episode' && this.skipEpisodes.has(Number(item.episode))) {
          this.skipEpisodes.delete(Number(item.episode));
          await this.report(item, true, 'Skipped before playback.');
          continue;
        }
        const controller = new AbortController();
        const active = { ...item, controller, started: performance.now() };
        this.active = active;
        let skipped = false;
        let failure = '';
        let progressPending = false;
        const reportProgress = async () => {
          if (item.kind !== 'episode' || this.active !== active || progressPending) return;
          progressPending = true;
          const duration = Math.max(0, Number(item.duration) || 0);
          const elapsed = Math.min(duration || Infinity, Math.max(0, (performance.now() - active.started) / 1000));
          try {
            await postPython('/internal/progress', {
              seriesId: this.seriesId,
              episode: item.episode,
              kind: item.kind,
              elapsed,
              duration,
            });
          } catch (error) {
            log('Could not report Go Live playback progress.', error);
          } finally {
            progressPending = false;
          }
        };
        const progressTimer = item.kind === 'episode' ? setInterval(() => { void reportProgress(); }, 2000) : null;
        try {
          try {
            if (progressTimer) void reportProgress();
            for await (const chunk of remuxToTransportStream(item, this.offset, controller.signal)) {
              if (this.aborted || this.signal.aborted) break;
              yield chunk;
            }
            skipped = controller.signal.aborted && !this.signal.aborted && !this.aborted;
          } catch (error) {
            if (controller.signal.aborted && !this.signal.aborted && !this.aborted) skipped = true;
            else if (this.signal.aborted || this.aborted) break;
            else failure = String(error?.message || error);
          }
        } finally {
          if (progressTimer) clearInterval(progressTimer);
        }
        const elapsed = Math.max(0, (performance.now() - active.started) / 1000);
        this.offset += skipped ? Math.min(Number(item.duration) || elapsed, elapsed) : (Number(item.duration) || elapsed);
        this.active = null;
        if (item.kind === 'episode') this.skipEpisodes.delete(Number(item.episode));
        await this.report(item, skipped, failure);
        if (failure) throw new Error(failure);
      }
    } finally {
      this.active?.controller?.abort(new Error('Series feed closed.'));
      this.active = null;
    }
  }
}

const VOICE_JOIN_TIMEOUT_MS = 25000;

async function joinVoice(guildId, voiceChannelId) {
  if (!discordReady) throw new Error('Discord streaming account is not ready.');
  const guild = String(guildId);
  const channel = String(voiceChannelId);
  if (
    joinedGuild === guild &&
    joinedChannel === channel &&
    streamer.voiceConnection?.status?.started
  ) return;

  log(`Connecting streaming account ${client.user?.id || 'unknown'} to voice channel ${channel} in server ${guild}.`);
  let timer;
  try {
    await Promise.race([
      streamer.joinVoice(guild, channel),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(
          `Voice connection did not become ready within ${VOICE_JOIN_TIMEOUT_MS / 1000}s. Check that the streaming account is in this server and has Connect permission; see logs/stream-bridge.log for voice/DAVE errors.`
        )), VOICE_JOIN_TIMEOUT_MS);
      }),
    ]);
    if (!streamer.voiceConnection?.status?.started) {
      throw new Error('Discord voice connection did not reach the ready state.');
    }
    joinedGuild = guild;
    joinedChannel = channel;
    log(`Streaming account connected to voice channel ${channel} in server ${guild}.`);
  } catch (error) {
    try { streamer.leaveVoice(); } catch {}
    joinedGuild = joinedChannel = null;
    log(`Voice connection failed for channel ${channel}.`, error);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function startSeries(body) {
  if (activeSeries) throw new Error('A Go Live stream is already active.');
  const guildId = String(body.guildId || '');
  const voiceChannelId = String(body.voiceChannelId || '');
  if (!/^\d+$/.test(guildId) || !/^\d+$/.test(voiceChannelId)) throw new Error('A valid server and voice channel are required.');
  await joinVoice(guildId, voiceChannelId);
  const controller = new AbortController();
  const seriesId = String(body.seriesId || `${Date.now()}`);
  const queue = new SeriesQueue(seriesId, controller.signal);
  const profile = {
    bitrate: Math.max(2500, Math.min(8000, Number(body.bitrate || 5000))),
    threads: Math.max(1, Math.min(16, Number(body.threads || 2))),
    preset: ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium'].includes(body.preset) ? body.preset : 'ultrafast',
  };
  const state = { id: seriesId, guildId, voiceChannelId, queue, controller, profile, done: false, error: '' };
  activeSeries = state;
  state.task = runSeries(state).catch((error) => {
    state.error = String(error?.stack || error);
    log(`Go Live series ${seriesId} failed.`, error);
  });
  return { seriesId };
}

async function runSeries(state) {
  let commandPromise;
  try {
    const input = Readable.from(state.queue.stream());
    const encoder = Encoders.software({
      x264: { preset: state.profile.preset, tune: 'zerolatency' },
      x265: { preset: state.profile.preset, tune: 'zerolatency' },
    });
    const prepared = prepareStream(input, {
      encoder,
      width: 1920,
      height: 1080,
      frameRate: 30,
      videoCodec: Utils.normalizeVideoCodec('H264'),
      bitrateVideo: state.profile.bitrate,
      bitrateVideoMax: Math.min(10000, Math.round(state.profile.bitrate * 1.5)),
      bitrateAudio: 128,
      includeAudio: true,
      hardwareAcceleratedDecoding: false,
      minimizeLatency: false,
      customFfmpegFlags: ['-threads:v', String(state.profile.threads)],
      logLevel: 'warning',
    }, state.controller.signal);
    commandPromise = prepared.promise;
    commandPromise.catch(() => {});
    const playing = playStream(prepared.output, streamer, {
      type: 'go-live',
      format: 'nut',
      width: 1920,
      height: 1080,
      frameRate: 30,
    }, state.controller.signal);
    await Promise.all([playing, commandPromise]);
  } catch (error) {
    if (!state.controller.signal.aborted) state.error = String(error?.stack || error);
  } finally {
    state.done = true;
    state.queue.abort();
    try {
      await postPython('/internal/series-event', { seriesId: state.id, error: state.error });
    } catch (error) {
      log(`Could not notify Python that series ${state.id} ended.`, error);
    }
    if (activeSeries === state) activeSeries = null;
  }
}

async function sendMessage(channelId, content) {
  const channel = client.channels.cache.get(String(channelId)) || await client.channels.fetch(String(channelId));
  if (!channel || typeof channel.send !== 'function') throw new Error('Text channel is unavailable.');
  const message = await channel.send(String(content || '').slice(0, 1900));
  return { messageId: String(message.id) };
}

async function editMessage(channelId, messageId, content) {
  const channel = client.channels.cache.get(String(channelId)) || await client.channels.fetch(String(channelId));
  if (!channel?.messages?.fetch) throw new Error('Text channel messages are unavailable.');
  const message = await channel.messages.fetch(String(messageId));
  if (!message || String(message.author?.id) !== String(client.user?.id)) {
    throw new Error('Refused to edit a message not owned by the streaming account.');
  }
  await message.edit(String(content || '').slice(0, 1900));
  return { messageId: String(message.id) };
}

async function handleApi(request, response) {
  if (['GET', 'POST'].includes(request.method) && request.url === '/api/health') {
    return json(response, 200, {
      ok: true,
      discordReady,
      userId: client.user?.id ? String(client.user.id) : null,
      activeSeries: Boolean(activeSeries),
    });
  }
  if (!authorized(request)) return json(response, 401, { ok: false, error: 'Unauthorized.' });
  let body;
  try { body = await readJson(request); }
  catch (error) { return json(response, 400, { ok: false, error: error.message }); }
  try {
    switch (request.url) {
      case '/api/send': {
        const result = await sendMessage(body.channelId, body.content);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/edit': {
        const result = await editMessage(body.channelId, body.messageId, body.content);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/join':
        await joinVoice(body.guildId, body.voiceChannelId);
        return json(response, 200, { ok: true });
      case '/api/series/start': {
        const result = await startSeries(body);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/series/add': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const filePath = safeMediaPath(body.filePath);
        const duration = Number(body.duration);
        if (!Number.isFinite(duration) || duration <= 0 || duration > 86400) throw new Error('Invalid media duration.');
        if (!['card', 'episode'].includes(body.kind)) throw new Error('Invalid segment kind.');
        activeSeries.queue.push({
          filePath,
          duration,
          episode: Number(body.episode),
          kind: body.kind,
        });
        log(`SEGMENT_QUEUED series=${activeSeries.id} kind=${body.kind} episode=${body.episode} duration=${duration}s file=${filePath}`);
        return json(response, 200, { ok: true });
      }
      case '/api/series/skip':
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        activeSeries.queue.skipEpisode(Number(body.episode));
        return json(response, 200, { ok: true });
      case '/api/series/end':
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        activeSeries.queue.close();
        return json(response, 200, { ok: true });
      case '/api/series/abort':
        if (activeSeries && (!body.seriesId || activeSeries.id === String(body.seriesId))) {
          const state = activeSeries;
          state.controller.abort(new Error('Stream stopped.'));
          state.queue.abort();
          await Promise.resolve(streamer.stopStream()).catch(() => {});
          await Promise.race([state.task, new Promise((resolveDone) => setTimeout(resolveDone, 4000))]);
        }
        return json(response, 200, { ok: true });
      case '/api/leave':
        if (activeSeries) throw new Error('Cannot leave voice while streaming.');
        await streamer.leaveVoice();
        joinedGuild = joinedChannel = null;
        return json(response, 200, { ok: true });
      default:
        return json(response, 404, { ok: false, error: 'Not found.' });
    }
  } catch (error) {
    log(`Local API ${request.url} failed.`, error);
    return json(response, 400, { ok: false, error: String(error?.message || error) });
  }
}

const server = createServer((request, response) => {
  handleApi(request, response).catch((error) => {
    log('HTTP request failed.', error);
    if (!response.headersSent) json(response, 500, { ok: false, error: 'Internal sidecar error.' });
    else response.destroy();
  });
});
server.listen(0, '127.0.0.1', () => {
  const port = server.address().port;
  console.log(JSON.stringify({ event: 'http-ready', port, timestamp: new Date().toISOString() }));
});

client.once('ready', () => {
  discordReady = true;
  console.log(JSON.stringify({ event: 'discord-ready', userId: client.user?.id, timestamp: new Date().toISOString() }));
});
client.on('error', (error) => log('Discord client error.', error));
process.on('unhandledRejection', (error) => log('Unhandled rejection.', error));
process.on('uncaughtException', (error) => { log('Uncaught exception.', error); process.exitCode = 1; });

await client.login(streamToken);
