export class SeriesQueue {
  constructor(seriesId, signal, {
    volume = 50, autopause = false, remux, postPython, logger = () => {},
  } = {}) {
    if (typeof remux !== 'function' || typeof postPython !== 'function') {
      throw new TypeError('SeriesQueue requires remux and Python API functions.');
    }
    this.remux = remux;
    this.postPython = postPython;
    this.logger = logger;
    this.seriesId = seriesId;
    this.signal = signal;
    this.items = [];
    this.waiters = [];
    this.ended = false;
    this.aborted = false;
    this.active = null;
    this.skipEpisodes = new Set();
    this.skipToTarget = null;
    this.offset = 0;
    this.volume = Math.max(0, Math.min(100, Number(volume) || 0));
    this.autopauseEnabled = Boolean(autopause);
    this.manualPaused = false;
    this.autoPaused = false;
    this.paused = false;
    this.hasAudience = true;
    this.resumeWaiters = [];
  }

  wake() {
    for (const waiter of this.resumeWaiters.splice(0)) waiter();
  }

  setPausedState() {
    const next = this.manualPaused || this.autoPaused;
    if (next === this.paused) {
      this.wake();
      return;
    }
    this.paused = next;
    const now = performance.now();
    if (this.active) {
      if (next && this.active.pausedAt === null) this.active.pausedAt = now;
      else if (!next && this.active.pausedAt !== null) {
        this.active.pausedMs += now - this.active.pausedAt;
        this.active.pausedAt = null;
      }
    }
    this.wake();
  }

  setAudience(hasAudience) {
    this.hasAudience = Boolean(hasAudience);
    if (this.autopauseEnabled) {
      this.autoPaused = !this.hasAudience;
      this.setPausedState();
    }
  }

  setAutopause(enabled, hasAudience = this.hasAudience) {
    this.autopauseEnabled = Boolean(enabled);
    this.hasAudience = Boolean(hasAudience);
    this.autoPaused = this.autopauseEnabled && !this.hasAudience;
    this.setPausedState();
    return { paused: this.paused, enabled: this.autopauseEnabled };
  }

  pause() {
    this.manualPaused = true;
    this.setPausedState();
    return { paused: this.paused };
  }

  resume() {
    this.manualPaused = false;
    if (this.autopauseEnabled) this.autoPaused = !this.hasAudience;
    else this.autoPaused = false;
    this.setPausedState();
    return { paused: this.paused };
  }

  async waitUntilRunning(active) {
    while (
      this.paused && !this.aborted && !this.signal.aborted &&
      !active.controller.signal.aborted
    ) {
      await new Promise((resolveResume) => this.resumeWaiters.push(resolveResume));
    }
  }

  position(active, now = performance.now()) {
    const pausedNow = active.pausedAt === null ? 0 : Math.max(0, now - active.pausedAt);
    const elapsed = Math.max(0, (now - active.runStartedAt - active.pausedMs - pausedNow) / 1000);
    return Math.min(Number(active.duration) || Infinity, active.sourcePosition + elapsed);
  }

  push(item) {
    if (this.ended || this.aborted) throw new Error('The active stream queue is closed.');
    const episode = Number(item.episode);
    if (this.skipToTarget !== null && episode !== this.skipToTarget) {
      if (item.kind === 'episode') {
        void this.report(item, true, `Skipped while jumping to E${this.skipToTarget}.`);
      }
      return;
    }
    const waiter = this.waiters.shift();
    if (waiter) waiter(item);
    else this.items.push(item);
    if (item.kind === 'episode' && episode === this.skipToTarget) this.skipToTarget = null;
  }

  close() {
    this.ended = true;
    for (const waiter of this.waiters.splice(0)) waiter(null);
    this.wake();
  }

  abort() {
    this.aborted = true;
    this.close();
    this.active?.controller?.abort(new Error('Stream stopped.'));
    this.wake();
  }

  async take() {
    while (!this.items.length && !this.ended && !this.aborted && !this.signal.aborted) {
      const item = await new Promise((resolveItem) => this.waiters.push(resolveItem));
      if (item) return item;
    }
    const item = this.items.shift() || null;
    if (item?.kind === 'episode' && Number(item.episode) === this.skipToTarget) {
      this.skipToTarget = null;
    }
    return item;
  }

  skipEpisode(episode) {
    this.skipEpisodes.add(Number(episode));
    if (this.active && this.active.episode === Number(episode) && this.active.kind === 'episode') {
      this.active.skipRequested = true;
      this.active.controller?.abort(new Error('Episode skipped.'));
      this.wake();
    }
  }

  async skipTo(episode) {
    const target = Number(episode);
    if (this.active?.kind === 'episode' && Number(this.active.episode) === target) {
      this.skipToTarget = null;
      return { target, current: true };
    }
    this.skipToTarget = target;
    const dropped = [];
    this.items = this.items.filter((item) => {
      if (Number(item.episode) === target) return true;
      if (item.kind === 'episode') dropped.push(item);
      return false;
    });
    for (const item of dropped) {
      await this.report(item, true, `Skipped while jumping to E${target}.`);
    }
    if (this.active && Number(this.active.episode) !== target) {
      this.active.skipRequested = true;
      this.active.skipToTarget = target;
      this.active.controller?.abort(new Error(`Jumping to E${target}.`));
      this.wake();
    }
    return { target };
  }

  seek(deltaSeconds) {
    const active = this.active;
    if (!active || active.kind !== 'episode') throw new Error('Seeking is available only during an episode.');
    const current = this.position(active);
    const target = Math.max(0, Math.min(Number(active.duration) - 0.25, current + Number(deltaSeconds)));
    if (!Number.isFinite(target)) throw new Error('The current episode duration is unavailable.');
    active.nextSeek = target;
    active.restartRequested = true;
    active.controller?.abort(new Error('Playback position changed.'));
    this.wake();
    return { position: target };
  }

  setVolume(volume) {
    const value = Number(volume);
    if (!Number.isInteger(value) || value < 0 || value > 100) throw new Error('Volume must be from 0 to 100.');
    this.volume = value;
    const active = this.active;
    if (active?.kind === 'episode') {
      active.nextSeek = this.position(active);
      active.restartRequested = true;
      active.controller?.abort(new Error('Stream volume changed.'));
      this.wake();
    }
    return { volume: this.volume, message: `Stream volume set to ${this.volume}%.` };
  }

  async report(item, skipped = false, error = '') {
    try {
      await this.postPython('/internal/segment-event', {
        seriesId: this.seriesId,
        episode: item.episode,
        kind: item.kind,
        skipped,
        error: String(error || '').slice(0, 1000),
      });
    } catch (exception) {
      this.logger('Could not notify Python about a completed stream segment.', exception);
    }
  }

  async reportProgress(active) {
    if (active.kind !== 'episode' || this.active !== active) return;
    try {
      await this.postPython('/internal/progress', {
        seriesId: this.seriesId,
        episode: active.episode,
        kind: active.kind,
        elapsed: this.position(active),
        duration: Number(active.duration) || 0,
        paused: this.paused,
      });
    } catch (error) {
      this.logger('Could not report Go Live playback progress.', error);
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
        const active = {
          ...item,
          controller: null,
          sourcePosition: 0,
          nextSeek: null,
          runStartedAt: 0,
          pausedMs: 0,
          pausedAt: null,
          restartRequested: false,
          skipRequested: false,
        };
        this.active = active;
        let segmentSkipped = false;
        let failure = '';
        let complete = false;
        while (!complete && !this.aborted && !this.signal.aborted) {
          active.controller = new AbortController();
          active.runStartedAt = performance.now();
          active.pausedMs = 0;
          active.pausedAt = this.paused ? performance.now() : null;
          const runStart = active.sourcePosition;
          let runFailure = '';
          const progressTimer = item.kind === 'episode'
            ? setInterval(() => { void this.reportProgress(active); }, 2000)
            : null;
          try {
            if (progressTimer) void this.reportProgress(active);
            for await (const chunk of this.remux(
              item,
              this.offset,
              active.controller.signal,
              runStart,
              this.volume,
            )) {
              if (this.aborted || this.signal.aborted) break;
              await this.waitUntilRunning(active);
              if (active.controller.signal.aborted) break;
              yield chunk;
            }
          } catch (error) {
            if (active.controller.signal.aborted && !active.skipRequested && !this.aborted && !this.signal.aborted) {
              // A seek or volume update intentionally restarts this segment.
            } else if (active.skipRequested || this.aborted || this.signal.aborted) {
              // The control action or shutdown owns the termination.
            } else {
              runFailure = String(error?.message || error);
            }
          } finally {
            if (progressTimer) clearInterval(progressTimer);
          }
          const endPosition = this.position(active);
          const runElapsed = Math.max(0, endPosition - runStart);
          if (runFailure) {
            failure = runFailure;
            complete = true;
          } else if (active.skipRequested) {
            segmentSkipped = true;
            complete = true;
          } else if (active.restartRequested && !this.aborted && !this.signal.aborted) {
            this.offset += runElapsed;
            active.sourcePosition = active.nextSeek ?? endPosition;
            active.nextSeek = null;
            active.restartRequested = false;
          } else {
            this.offset += runElapsed;
            complete = true;
          }
        }
        this.active = null;
        if (item.kind === 'episode') this.skipEpisodes.delete(Number(item.episode));
        await this.report(item, segmentSkipped, failure);
        if (failure) throw new Error(failure);
      }
    } finally {
      this.active?.controller?.abort(new Error('Series feed closed.'));
      this.active = null;
      this.wake();
    }
  }
}
