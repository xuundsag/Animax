import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { SeriesQueue, volumeGain } from '../animax/stream_queue.mjs';

const posted = [];
const postPython = async (endpoint, payload) => {
  posted.push({ endpoint, payload });
  return { ok: true };
};
const remuxCalls = [];
async function* remux(item, offset, signal, seekSeconds, volume) {
  remuxCalls.push({ item, offset, seekSeconds, volume });
  if (!signal.aborted) yield Buffer.from(`${item.kind}:${item.episode}`);
}
const newQueue = (options = {}) => new SeriesQueue(
  'series-test',
  new AbortController().signal,
  { remux, postPython, logger: () => {}, ...options },
);
const segment = (kind, episode, startSeconds = 0) => ({
  kind,
  episode,
  duration: 60,
  startSeconds,
  filePath: `/downloads/${kind}-${episode}.mp4`,
});

assert.equal(volumeGain(1), 0.5, 'level 1 preserves the old 50% output gain');
assert.equal(volumeGain(2), 1);
assert.equal(volumeGain(50), 25);
assert.throws(() => volumeGain(0), /1 to 50/);
assert.throws(() => volumeGain(51), /1 to 50/);

// Manual pause and audience-driven autopause compose without overriding each other.
{
  const queue = newQueue();
  assert.deepEqual(queue.pause(), { paused: true });
  assert.deepEqual(queue.setAutopause(true, false), { paused: true, enabled: true });
  queue.setAudience(true);
  assert.equal(queue.paused, true, 'manual pause remains while the audience returns');
  assert.deepEqual(queue.resume(), { paused: false });
  queue.setAudience(false);
  assert.equal(queue.paused, true, 'autopause pauses after the audience leaves');
  assert.deepEqual(queue.setAutopause(false), { paused: false, enabled: false });

  queue.resume();
  assert.equal(queue.paused, false);
}

// Seeking and volume changes restart the current episode at the calculated source time.
{
  const queue = newQueue();
  const controller = new AbortController();
  const active = {
    kind: 'episode',
    episode: 4,
    duration: 120,
    sourcePosition: 5,
    runStartedAt: performance.now() - 5000,
    pausedMs: 0,
    pausedAt: null,
    controller,
  };
  queue.active = active;
  const current = queue.position(active);
  const result = queue.seek(10);
  assert.ok(Math.abs(result.position - (current + 10)) < 0.2);
  assert.equal(active.restartRequested, true);
  assert.equal(controller.signal.aborted, true);

  const volumeController = new AbortController();
  const volumeActive = {
    ...active,
    controller: volumeController,
    sourcePosition: 30,
    runStartedAt: performance.now(),
    restartRequested: false,
  };
  queue.active = volumeActive;
  assert.equal(queue.setVolume(40).volume, 40);
  assert.ok(volumeActive.nextSeek >= 30);
  assert.equal(volumeActive.restartRequested, true);
  assert.equal(volumeController.signal.aborted, true);
  assert.throws(() => queue.setVolume(0), /1 to 50/);
  assert.throws(() => queue.setVolume(51), /1 to 50/);
}

// skipto preserves a queued target, clears its gate when it is taken, and does not poison later episodes.
{
  const queue = newQueue();
  queue.items = [segment('episode', 1), segment('episode', 2), segment('episode', 3)];
  assert.deepEqual(await queue.skipTo(2), { target: 2 });
  assert.equal(queue.skipToTarget, 2);
  const target = await queue.take();
  assert.equal(target.episode, 2);
  assert.equal(queue.skipToTarget, null);
  queue.push(segment('episode', 3));
  assert.deepEqual(queue.items.map((item) => item.episode), [3]);
}

// skipto the currently playing episode is a no-op and leaves the next episode queued.
{
  const queue = newQueue();
  const controller = new AbortController();
  queue.active = { kind: 'episode', episode: 2, controller };
  queue.items = [segment('episode', 3)];
  assert.deepEqual(await queue.skipTo(2), { target: 2, current: true });
  assert.equal(queue.skipToTarget, null);
  assert.equal(controller.signal.aborted, false);
  assert.deepEqual(queue.items.map((item) => item.episode), [3]);
}

// A jump away from the current episode aborts it and admits only the requested episode.
{
  const queue = newQueue();
  const controller = new AbortController();
  queue.active = { kind: 'episode', episode: 1, controller };
  assert.deepEqual(await queue.skipTo(4), { target: 4 });
  assert.equal(controller.signal.aborted, true);
  queue.push(segment('episode', 2));
  queue.push(segment('episode', 4));
  assert.deepEqual(queue.items.map((item) => item.episode), [4]);
  assert.equal(queue.skipToTarget, null);
}

// The generator forwards whole segments and reports progress/completion to Python.
{
  posted.length = 0;
  remuxCalls.length = 0;
  const queue = newQueue({ volume: 1 });
  queue.push(segment('card', 1));
  queue.push(segment('episode', 1, 12));
  queue.close();
  const chunks = [];
  for await (const chunk of queue.stream()) chunks.push(chunk.toString());
  assert.deepEqual(chunks, ['card:1', 'episode:1']);
  assert.equal(remuxCalls[1].volume, 1);
  assert.equal(remuxCalls[1].seekSeconds, 12);
  assert.ok(posted.some(({ endpoint, payload }) => endpoint === '/internal/progress' && payload.episode === 1));
  assert.ok(posted.some(({ endpoint, payload }) => endpoint === '/internal/segment-event' && payload.episode === 1 && !payload.skipped));
}

// Pause feeds a keepalive slate instead of starving the live encoder, and resume
// reopens the media at the exact saved source position without ending the queue.
{
  const calls = [];
  const timedRemux = async function* (item, _offset, signal, seekSeconds) {
    calls.push({ kind: item.kind, seekSeconds });
    if (item.kind === 'pause') {
      while (!signal.aborted) {
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 10));
        if (!signal.aborted) yield Buffer.from('pause');
      }
      return;
    }
    for (let index = 0; index < 100 && !signal.aborted; index += 1) {
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 10));
      if (!signal.aborted) yield Buffer.from(`episode:${seekSeconds}`);
    }
  };
  const queue = new SeriesQueue('pause-test', new AbortController().signal, {
    remux: timedRemux,
    postPython,
    logger: () => {},
  });
  queue.push({ ...segment('episode', 1, 10), duration: 1000 });
  const stream = queue.stream();
  assert.equal((await stream.next()).value.toString(), 'episode:10');
  queue.pause();
  assert.equal((await stream.next()).value.toString(), 'pause');
  const heldPosition = queue.activePosition();
  await new Promise((resolveDelay) => setTimeout(resolveDelay, 80));
  assert.ok(Math.abs(queue.activePosition() - heldPosition) < 0.05);
  assert.equal((await stream.next()).value.toString(), 'pause');
  queue.resume();
  assert.match((await stream.next()).value.toString(), /^episode:/);
  assert.ok(calls.some((call) => call.kind === 'episode' && call.seekSeconds >= 10));
  const forward = queue.seek(5);
  await stream.next();
  assert.ok(Math.abs(calls.at(-1).seekSeconds - forward.position) < 0.1);
  const backward = queue.seek(-3);
  await stream.next();
  assert.ok(Math.abs(calls.at(-1).seekSeconds - backward.position) < 0.1);
  const absolute = queue.seek(367, { absolute: true });
  await stream.next();
  assert.equal(absolute.position, 367);
  assert.ok(Math.abs(calls.at(-1).seekSeconds - 367) < 0.1);
  queue.abort();
  await stream.next();
}

console.log('Stream queue control smoke passed.');
