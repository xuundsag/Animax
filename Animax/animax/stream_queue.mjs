export function volumeGain(level) {
  const value = Number(level);
  if (!Number.isInteger(value) || value < 1 || value > 50) {
    throw new Error('Volume level must be from 1 to 50.');
  }
  return value / 2;
}

export class SeriesQueue {
  constructor(seriesId, signal, {
    volume = 1, autopause = false, remux, postPython, logger = () => {},
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
    this.volume = Number(volume);
    volumeGain(this.volume);
    this.autopauseEnabled = Boolean(autopause);
    this.manualPaused = false;
    this.autoPaused = false;
    this.paused = false;
    this.hasAudience = true;
  }

  setPausedState() {
    const next = this.manualPaused || this.autoPaused;
    if (next === this.paused) return;
    const now = performance.now();
    const active = this.active;
    if (active) {
      if (next && active.pausedAt === null) {
        const position = this.position(active, now);
        active.nextSeek = position;
        active.restartFrom = position;
        active.restartRequested = true;
        active.controller?.abort(new Error('Playback paused.'));
        active.pausedAt = now;
      } else if (!next && active.pausedAt !== null) {
        active.pausedMs += now - active.pausedAt;
        active.pausedAt = null;
        active.pauseController?.abort(new Error('Playback resumed.'));
      }
    }
    this.paused = next;
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
    const result = { paused: this.paused, enabled: this.autopauseEnabled };
    const position = this.activePosition();
    if (position !== null) result.position = position;
    return result;
  }

  activePosition() {
    if (!this.active || this.active.kind !== 'episode') return null;
    const position = this.position(this.active);
    return Number.isFinite(position) ? position : null;
  }

  pause() {
    this.manualPaused = true;
    this.setPausedState();
    const result = { paused: this.paused };
    const position = this.activePosition();
    if (position !== null) result.position = position;
    return result;
  }

  resume() {
    this.manualPaused = false;
    if (this.autopauseEnabled) this.autoPaused = !this.hasAudience;
    else this.autoPaused = false;
    this.setPausedState();
    const result = { paused: this.paused };
    const position = this.activePosition();
    if (position !== null) result.position = position;
    return result;
  }

  position(active, now = performance.now()) {
    const pausedNow = active.pausedAt === null ? 0 : Math.max(0, now - active.pausedAt);
    const elapsed = active.runStartedAt > 0
      ? Math.max(0, (now - active.runStartedAt - active.pausedMs - pausedNow) / 1000)
      : 0;
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
  }

  abort() {
    this.aborted = true;
    this.close();
    this.active?.controller?.abort(new Error('Stream stopped.'));
    this.active?.pauseController?.abort(new Error('Stream stopped.'));
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
      this.active.pauseController?.abort(new Error('Episode skipped.'));
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
      this.active.pauseController?.abort(new Error(`Jumping to E${target}.`));
    }
    return { target };
  }

  seek(seconds, { absolute = false } = {}) {
    const active = this.active;
    if (!active || active.kind !== 'episode') throw new Error('Seeking is available only during an episode.');
    const value = Number(seconds);
    if (!Number.isFinite(value)) throw new Error('Seek position must be a finite number of seconds.');
    const current = this.position(active);
    const desired = absolute ? value : current + value;
    const target = Math.max(0, Math.min(Number(active.duration) - 0.25, desired));
    if (!Number.isFinite(target)) throw new Error('The current episode duration is unavailable.');
    if (this.paused) {
      active.sourcePosition = target;
      active.nextSeek = null;
      active.restartFrom = null;
      active.restartRequested = false;
    } else {
      active.restartFrom = current;
      active.nextSeek = target;
      active.restartRequested = true;
      if (active.controller) active.controller.abort(new Error('Playback position changed.'));
      else active.sourcePosition = target;
    }
    return { position: target };
  }

  setVolume(volume) {
    const value = Number(volume);
    volumeGain(value);
    this.volume = value;
    const active = this.active;
    if (active?.kind === 'episode' && !this.paused) {
      const current = this.position(active);
      active.restartFrom = current;
      active.nextSeek = current;
      active.restartRequested = true;
      if (active.controller) active.controller.abort(new Error('Stream volume changed.'));
      else active.sourcePosition = active.nextSeek;
    }
    return {
      volume: this.volume,
      message: `Stream volume set to Level ${this.volume} (${this.volume * 100}% of default).`,
    };
  }

  async *pauseFeed(active) {
    const controller = new AbortController();
    const startedAt = performance.now();
    active.pauseController = controller;
    try {
      const pauseSegment = { ...active, kind: 'pause' };
      for await (const chunk of this.remux(
        pauseSegment,
        this.offset,
        controller.signal,
        0,
        this.volume,
      )) {
        if (this.aborted || this.signal.aborted || !this.paused) break;
        yield chunk;
      }
    } catch (error) {
      if (!controller.signal.aborted && !this.aborted && !this.signal.aborted) throw error;
    } finally {
      this.offset += Math.max(0, (performance.now() - startedAt) / 1000);
      if (active.pauseController === controller) active.pauseController = null;
    }
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
          pauseController: null,
          sourcePosition: Math.max(
            0,
            Math.min(
              Math.max(0, (Number(item.duration) || 0) - 0.25),
              Number(item.startSeconds) || 0,
            ),
          ),
          nextSeek: null,
          restartFrom: null,
          runStartedAt: 0,
          pausedMs: 0,
          pausedAt: this.paused ? performance.now() : null,
          restartRequested: false,
          skipRequested: false,
        };
        this.active = active;
        let segmentSkipped = false;
        let failure = '';
        let complete = false;
        while (!complete && !this.aborted && !this.signal.aborted) {
          if (active.skipRequested) {
            segmentSkipped = true;
            complete = true;
            break;
          }
          if (active.restartRequested && active.controller === null && active.nextSeek !== null) {
            active.sourcePosition = active.nextSeek;
            active.nextSeek = null;
            active.restartFrom = null;
            active.restartRequested = false;
          }
          if (this.paused) {
            for await (const chunk of this.pauseFeed(active)) yield chunk;
            if (this.aborted || this.signal.aborted) break;
            continue;
          }
          active.controller = new AbortController();
          active.runStartedAt = performance.now();
          active.pausedMs = 0;
          active.pausedAt = null;
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
              if (this.aborted || this.signal.aborted || active.controller.signal.aborted) break;
              yield chunk;
            }
          } catch (error) {
            if (active.controller.signal.aborted && !active.skipRequested && !this.aborted && !this.signal.aborted) {
              // A seek, pause, or volume update intentionally restarts this episode.
            } else if (active.skipRequested || this.aborted || this.signal.aborted) {
              // The control action or shutdown owns the termination.
            } else {
              runFailure = String(error?.message || error);
            }
          } finally {
            if (progressTimer) clearInterval(progressTimer);
          }
          active.controller = null;
          const endPosition = this.position(active);
          const stoppedAt = active.restartFrom ?? endPosition;
          const runElapsed = Math.max(0, stoppedAt - runStart);
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
            active.restartFrom = null;
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
      this.active?.pauseController?.abort(new Error('Series feed closed.'));
      this.active = null;
    }
  }
}
