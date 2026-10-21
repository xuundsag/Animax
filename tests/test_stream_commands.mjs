import assert from 'node:assert/strict';
import {
  STREAM_COMMAND_NAMES,
  STREAM_HELP,
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
assert.equal(parseStreamCommand('.stream One Piece 1 3'), null);
assert.equal(parseStreamCommand('?'), null);
assert.equal(parseSeekSeconds('10s'), 10);
assert.equal(parseSeekSeconds('2m'), 120);
assert.equal(parseSeekSeconds('1.5 minutes'), 90);
assert.throws(() => parseSeekSeconds('later'), /seek time/i);
assert.throws(() => parseSeekSeconds('121m'), /120 minutes/i);
assert.deepEqual(STREAM_COMMAND_NAMES, [
  'help', 'stream', 'pause', 'resume', 'skip', 'forward', 'backward',
  'skipto', 'volume', 'autopause', 'stop',
]);
assert.match(STREAM_HELP, /\*\*Commands\*\*/);
assert.match(STREAM_HELP, /default is 50%/);
assert.match(STREAM_HELP, /Default: off/);
assert.doesNotMatch(STREAM_HELP, /quality/i);
console.log('Stream command parser smoke passed.');
