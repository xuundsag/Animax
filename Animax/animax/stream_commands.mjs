export const STREAM_COMMAND_NAMES = [
  'help',
  'stream',
  'pause',
  'resume',
  'skip',
  'forward',
  'backward',
  'seek',
  'skipto',
  'volume',
  'autopause',
  'stop',
];

export const STREAM_HELP = `\`\`\`\n${STREAM_COMMAND_NAMES.join(', ')}\n\`\`\``;

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
  if (!match) throw new Error('Use a relative seek such as `10s` or `2m`.');
  const amount = Number(match[1]);
  const unit = match[2] || 's';
  const seconds = amount * (unit.startsWith('m') ? 60 : 1);
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 7200) {
    throw new Error('Seek time must be greater than zero and no more than 120 minutes.');
  }
  return seconds;
}

export function parseAbsoluteSeekSeconds(value) {
  const text = String(value || '').trim();
  if (/^\d+$/.test(text)) {
    const minutes = Number(text);
    if (!Number.isSafeInteger(minutes) || minutes > 1440) {
      throw new Error('Use `?seek <minutes>` or `?seek <minutes>:<seconds>` (up to 24 hours).');
    }
    return minutes * 60;
  }
  const parts = text.split(':');
  if (parts.length !== 2 && parts.length !== 3) {
    throw new Error('Use `?seek <minutes>` or `?seek <minutes>:<seconds>`, for example `6` or `6:07`.');
  }
  if (parts.some((part) => !/^\d+$/.test(part))) {
    throw new Error('Seek time must use whole numbers in `minutes:seconds` format.');
  }
  const values = parts.map(Number);
  let seconds;
  if (values.length === 2) {
    const [minutes, remainder] = values;
    if (remainder > 59) throw new Error('Seconds in `minutes:seconds` must be from 0 to 59.');
    seconds = minutes * 60 + remainder;
  } else {
    const [hours, minutes, remainder] = values;
    if (minutes > 59 || remainder > 59) {
      throw new Error('Use `hours:minutes:seconds` with minutes and seconds from 0 to 59.');
    }
    seconds = hours * 3600 + minutes * 60 + remainder;
  }
  if (!Number.isSafeInteger(seconds) || seconds < 0 || seconds > 86400) {
    throw new Error('Seek time must be within a 24-hour episode.');
  }
  return seconds;
}
