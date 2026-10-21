export const STREAM_COMMAND_NAMES = [
  'help',
  'stream',
  'pause',
  'resume',
  'skip',
  'forward',
  'backward',
  'skipto',
  'volume',
  'autopause',
  'stop',
];

export const STREAM_HELP = [
  '**Commands**',
  '',
  '```',
  STREAM_COMMAND_NAMES.join(', '),
  '```',
  '',
  '`?stream <MAL ID or title> <start> <end> [sub|dub]` — play uploaded episodes from anime.json.',
  '`?pause` / `?resume` — pause or resume playback.',
  '`?skip` — skip the current episode. `?skipto <episode>` — jump within the requested range.',
  '`?forward <time>` / `?backward <time>` — seek by seconds or minutes, e.g. `10s` or `2m`.',
  '`?volume [0-100]` — set or check volume; default is 50%.',
  '`?autopause on|off` — pause when only the streaming account remains; resume when someone returns. Default: off.',
  '`?stop` — stop playback and leave voice. Only the stream starter can control a session.',
].join('\n');

export function parseStreamCommand(content) {
  const text = String(content || '').trimStart();
  if (!text.startsWith('?')) return null;
  const body = text.slice(1).trim();
  if (!body) return null;
  const match = body.match(/^(\S+)(?:\s+([\s\S]*))?$/);
  if (!match) return null;
  return {
    name: match[1].toLowerCase(),
    args: String(match[2] || '').trim(),
  };
}

export function parseSeekSeconds(value) {
  const text = String(value || '').trim().toLowerCase();
  const match = text.match(/^(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)?$/);
  if (!match) throw new Error('Use a seek time such as `10s` or `2m`.');
  const amount = Number(match[1]);
  const unit = match[2] || 's';
  const seconds = amount * (unit.startsWith('m') ? 60 : 1);
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 7200) {
    throw new Error('Seek time must be greater than zero and no more than 120 minutes.');
  }
  return seconds;
}
