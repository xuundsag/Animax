import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { SeriesQueue } from '../animax/stream_queue.mjs';

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
const segment = (kind, episode) => ({
  kind,
  episode,
  duration: 60,
  filePath: `/downloads/${kind}-${episode}.mp4`,
});

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

  const waiting = queue.waitUntilRunning({ controller: new AbortController() });
  await Promise.resolve();
  queue.resume();
  await waiting;
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
  assert.equal(queue.setVolume(75).volume, 75);
  assert.ok(volumeActive.nextSeek >= 30);
  assert.equal(volumeActive.restartRequested, true);
  assert.equal(volumeController.signal.aborted, true);
  assert.throws(() => queue.setVolume(101), /0 to 100/);
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
  const queue = newQueue({ volume: 50 });
  queue.push(segment('card', 1));
  queue.push(segment('episode', 1));
  queue.close();
  const chunks = [];
  for await (const chunk of queue.stream()) chunks.push(chunk.toString());
  assert.deepEqual(chunks, ['card:1', 'episode:1']);
  assert.equal(remuxCalls[1].volume, 50);
  assert.ok(posted.some(({ endpoint, payload }) => endpoint === '/internal/progress' && payload.episode === 1));
  assert.ok(posted.some(({ endpoint, payload }) => endpoint === '/internal/segment-event' && payload.episode === 1 && !payload.skipped));
}

console.log('Stream queue control smoke passed.');
