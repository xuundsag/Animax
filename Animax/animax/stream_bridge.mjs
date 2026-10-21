import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, lstatSync, realpathSync } from 'node:fs';
import { isAbsolute, resolve, sep } from 'node:path';
import { Readable } from 'node:stream';
import { STREAM_COMMAND_NAMES, STREAM_HELP, parseAbsoluteSeekSeconds, parseSeekSeconds, parseStreamCommand } from './stream_commands.mjs';
import { SeriesQueue, volumeGain } from './stream_queue.mjs';
import { buildRemuxArgs } from './stream_remux.mjs';
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

async function* remuxToTransportStream(segment, offset, signal, seekSeconds = 0, volume = 1) {
  const isPause = segment.kind === 'pause';
  const file = isPause ? null : safeMediaPath(segment.filePath);
  const tag = `${segment.kind} E${segment.episode}`;
  const audioVolume = volumeGain(volume);
  const action = isPause ? 'PAUSE_FEED' : 'REMUX';
  log(`FFMPEG_${action}_START ${tag} file=${file || 'synthetic-black'} duration=${segment.duration}s seek=${seekSeconds}s volume_level=${volume} gain=${audioVolume.toFixed(2)}x`);
  const args = buildRemuxArgs({
    file,
    kind: segment.kind,
    offset,
    seekSeconds,
    volume,
  });
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
        log(`FFMPEG_${action}_OUTPUT ${tag} first_chunk_bytes=${chunk.length}`);
      }
      yield chunk;
    }
    const [code, signalName] = await Promise.race([closed, spawnError]);
    if (!signal.aborted && code !== 0) throw new Error(`FFmpeg transport remux failed (${code ?? signalName}): ${stderr.slice(-4000)}`);
    log(`FFMPEG_${action}_END ${tag} exit=${code ?? signalName} aborted=${signal.aborted}`);
    return { aborted: signal.aborted };
  } finally {
    signal.removeEventListener('abort', terminate);
    if (child.exitCode === null) terminate();
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

function audiencePresent(state) {
  const guild = client.guilds.cache.get(String(state.guildId));
  const channel = client.channels.cache.get(String(state.voiceChannelId)) || guild?.channels.cache.get(String(state.voiceChannelId));
  const members = channel?.members;
  if (!members || typeof members.values !== 'function') return null;
  const selfId = String(client.user?.id || '');
  return [...members.values()].some((member) => String(member.id) !== selfId);
}

function refreshAutoPause(state) {
  if (!state || !state.queue.autopauseEnabled) return;
  const hasAudience = audiencePresent(state);
  if (hasAudience === null) return;
  const wasPaused = state.queue.paused;
  state.queue.setAudience(hasAudience);
  if (wasPaused !== state.queue.paused) {
    log(`AUTOPAUSE series=${state.id} paused=${state.queue.paused} audience=${hasAudience}`);
    if (state.queue.active?.kind === 'episode') {
      void state.queue.reportProgress(state.queue.active);
    }
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
  const volume = Number(body.volume ?? 1);
  volumeGain(volume);
  const autopause = Boolean(body.autopause);
  const queue = new SeriesQueue(seriesId, controller.signal, {
    volume, autopause, remux: remuxToTransportStream, postPython, logger: log,
  });
  const profile = {
    bitrate: Math.max(2500, Math.min(8000, Number(body.bitrate || 5000))),
    threads: Math.max(1, Math.min(16, Number(body.threads || 2))),
    preset: ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium'].includes(body.preset) ? body.preset : 'ultrafast',
  };
  const state = { id: seriesId, guildId, voiceChannelId, queue, controller, profile, done: false, error: '', autoPauseTimer: null };
  activeSeries = state;
  refreshAutoPause(state);
  state.autoPauseTimer = setInterval(() => refreshAutoPause(state), 5000);
  state.autoPauseTimer.unref?.();
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
    if (state.autoPauseTimer) clearInterval(state.autoPauseTimer);
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

async function memberVoiceChannelId(message) {
  let member = message.member;
  let channelId = member?.voice?.channelId || member?.voice?.channel?.id || null;
  if (!channelId && message.guild?.members?.fetch) {
    try {
      member = await message.guild.members.fetch(String(message.author.id));
      channelId = member?.voice?.channelId || member?.voice?.channel?.id || null;
    } catch (error) {
      log(`Could not fetch command author's voice state for ${message.author.id}.`, error);
    }
  }
  return channelId ? String(channelId) : null;
}


async function handleStreamCommand(message) {
  const parsed = parseStreamCommand(message?.content);
  if (!parsed) return;
  if (message.author?.bot && String(message.author.id) !== String(client.user?.id)) return;
  const { name, args } = parsed;
  if (!STREAM_COMMAND_NAMES.includes(name)) {
    await message.channel.send('Unknown stream command. Use `?help`.');
    return;
  }
  log(`COMMAND name=${name} author=${message.author.id} guild=${message.guild?.id || 'DM'} channel=${message.channel?.id || 'unknown'}`);
  if (name === 'help') {
    await message.channel.send(STREAM_HELP);
    return;
  }
  if (!message.guild || !message.channel?.send) {
    await message.channel.send('Use stream commands in a server text channel.');
    return;
  }
  const guildId = String(message.guild.id);
  const authorId = String(message.author.id);
  const channelId = String(message.channel.id);

  if (name === 'stream') {
    if (!args) {
      await message.channel.send('Usage: `?stream <MAL ID or anime title> <start> <end> [sub|dub]`.');
      return;
    }
    let statusMessage;
    try {
      statusMessage = await message.channel.send('**Go Live** | `Starting` | 0:00 / --:--');
      const voiceChannelId = await memberVoiceChannelId(message);
      const result = await postPython('/stream/start', {
        guildId,
        voiceChannelId,
        channelId,
        authorId,
        raw: args,
        progressMessageId: String(statusMessage.id),
      });
      log(`STREAM_REQUEST accepted author=${authorId} guild=${guildId} message=${statusMessage.id}`);
      return result;
    } catch (error) {
      const text = `**Go Live** | \`Failed\` | 0:00 / --:--\n${String(error?.message || error).slice(0, 1200)}`;
      if (statusMessage) {
        await statusMessage.edit(text.slice(0, 1900)).catch(() => {});
      } else {
        await message.channel.send(text.slice(0, 1900)).catch(() => {});
      }
      log(`STREAM_REQUEST failed author=${authorId}.`, error);
      return null;
    }
  }

  let value = null;
  try {
    const voiceChannelId = await memberVoiceChannelId(message);
    if (!voiceChannelId) throw new Error('Join the voice channel where the stream is playing to control it.');
    if (name === 'forward' || name === 'backward') {
      value = parseSeekSeconds(args);
    } else if (name === 'seek') {
      value = parseAbsoluteSeekSeconds(args);
    } else if (name === 'skipto') {
      if (!/^\d+$/.test(args)) throw new Error('Usage: `?skipto <episode number>`.');
      value = Number(args);
      if (!Number.isSafeInteger(value) || value < 1) throw new Error('Episode number must be positive.');
    } else if (name === 'volume') {
      if (args) {
        if (!/^\d{1,2}$/.test(args)) throw new Error('Usage: `?volume [1-50]`.');
        value = Number(args);
        if (value < 1 || value > 50) throw new Error('Volume level must be from 1 to 50.');
      }
    } else if (name === 'autopause') {
      if (args) {
        const option = args.toLowerCase();
        if (!['on', 'off'].includes(option)) throw new Error('Usage: `?autopause on|off`.');
        value = option === 'on';
      }
    } else if (args) {
      throw new Error(`Usage: \`?${name}\` takes no arguments.`);
    }
    const result = await postPython('/stream/control', {
      guildId,
      voiceChannelId,
      authorId,
      command: name,
      value,
    });
    await message.channel.send(String(result.message || 'Stream command completed.'));
  } catch (error) {
    await message.channel.send(String(error?.message || error).slice(0, 1800)).catch(() => {});
    log(`COMMAND failed name=${name} author=${authorId}.`, error);
  }
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
          startSeconds: Math.max(0, Number(body.startSeconds) || 0),
        });
        log(`SEGMENT_QUEUED series=${activeSeries.id} kind=${body.kind} episode=${body.episode} duration=${duration}s file=${filePath}`);
        return json(response, 200, { ok: true });
      }
      case '/api/series/skip':
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        activeSeries.queue.skipEpisode(Number(body.episode));
        return json(response, 200, { ok: true });
      case '/api/series/pause': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const result = activeSeries.queue.pause();
        log(`PAUSE series=${activeSeries.id}`);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/series/resume': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const result = activeSeries.queue.resume();
        log(`RESUME series=${activeSeries.id} paused=${result.paused}`);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/series/seek': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const result = activeSeries.queue.seek(Number(body.seconds), { absolute: body.absolute === true });
        const position = Math.floor(result.position);
        const hours = Math.floor(position / 3600);
        const minutes = Math.floor((position % 3600) / 60);
        const seconds = position % 60;
        const timing = hours > 0
          ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
          : `${minutes}:${String(seconds).padStart(2, '0')}`;
        log(`SEEK series=${activeSeries.id} absolute=${body.absolute === true} position=${result.position.toFixed(2)}s`);
        return json(response, 200, { ok: true, ...result, message: `Seeked to ${timing}.` });
      }
      case '/api/series/skipto': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const result = await activeSeries.queue.skipTo(Number(body.episode));
        log(`SKIP_TO series=${activeSeries.id} episode=${result.target}`);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/series/volume': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const result = activeSeries.queue.setVolume(Number(body.volume));
        log(`VOLUME series=${activeSeries.id} value=${result.volume}`);
        return json(response, 200, { ok: true, ...result });
      }
      case '/api/series/autopause': {
        if (!activeSeries || activeSeries.id !== String(body.seriesId)) throw new Error('No matching active stream.');
        const hasAudience = audiencePresent(activeSeries);
        const result = activeSeries.queue.setAutopause(Boolean(body.enabled), hasAudience ?? true);
        log(`AUTOPAUSE_CONFIG series=${activeSeries.id} enabled=${result.enabled} paused=${result.paused}`);
        return json(response, 200, { ok: true, ...result });
      }
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
client.on('messageCreate', (message) => {
  void handleStreamCommand(message).catch((error) => log('Stream command handler failed.', error));
});
client.on('voiceStateUpdate', (oldState, newState) => {
  const guildId = String(newState?.guild?.id || oldState?.guild?.id || '');
  const state = activeSeries;
  if (!state || guildId !== String(state.guildId) || !state.queue.autopauseEnabled) return;
  const timer = setTimeout(() => refreshAutoPause(state), 300);
  timer.unref?.();
});
process.on('unhandledRejection', (error) => log('Unhandled rejection.', error));
process.on('uncaughtException', (error) => { log('Uncaught exception.', error); process.exitCode = 1; });

await client.login(streamToken);
