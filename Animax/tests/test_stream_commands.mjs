import assert from 'node:assert/strict';
import {
  STREAM_COMMAND_NAMES,
  STREAM_HELP,
  parseAbsoluteSeekSeconds,
  parseSeekSeconds,
  parseStreamCommand,
} from '../animax/stream_commands.mjs';

assert.deepEqual(parseStreamCommand('?stream "One Piece" 1 12 dub'), {
  name: 'stream',
  args: '"One Piece" 1 12 dub',
});
assert.deepEqual(parseStreamCommand('  ?FORWARD 10s'), {
  name: 'forward',
  args: '10s',
});
assert.deepEqual(parseStreamCommand('?seek 6:7'), {
  name: 'seek',
  args: '6:7',
});
assert.equal(parseStreamCommand('.stream One Piece 1 3'), null);
assert.equal(parseStreamCommand('?'), null);
assert.equal(parseSeekSeconds('10s'), 10);
assert.equal(parseSeekSeconds('2m'), 120);
assert.equal(parseSeekSeconds('1.5 minutes'), 90);
assert.throws(() => parseSeekSeconds('later'), /relative seek/i);
assert.throws(() => parseSeekSeconds('121m'), /120 minutes/i);
assert.equal(parseAbsoluteSeekSeconds('6'), 360);
assert.equal(parseAbsoluteSeekSeconds('6:7'), 367);
assert.equal(parseAbsoluteSeekSeconds('1:02:03'), 3723);
assert.throws(() => parseAbsoluteSeekSeconds('6:60'), /seconds.*0 to 59/i);
assert.throws(() => parseAbsoluteSeekSeconds('6:7:60'), /minutes and seconds/i);
assert.deepEqual(STREAM_COMMAND_NAMES, [
  'help', 'stream', 'pause', 'resume', 'skip', 'forward', 'backward', 'seek',
  'skipto', 'volume', 'autopause', 'stop',
]);
assert.equal(
  STREAM_HELP,
  '```\nhelp, stream, pause, resume, skip, forward, backward, seek, skipto, volume, autopause, stop\n```',
);
assert.doesNotMatch(STREAM_HELP, /usage|default|quality|—/i);
console.log('Stream command parser smoke passed.');
