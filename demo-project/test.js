// demo-project/test.js
// Minimal test runner — no external dependencies required

var math = require('./index.js');

var passed = 0;
var failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  PASS: ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL: ' + name);
    console.log('    ' + e.message);
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      (message || '') + '\n      expected: ' + expected + '\n      actual:   ' + actual
    );
  }
}

// --- Tests ---
console.log('Running tests...\n');

test('add(2, 3) === 5', function () {
  assertEqual(math.add(2, 3), 5);
});

test('add(-1, 1) === 0', function () {
  assertEqual(math.add(-1, 1), 0);
});

test('subtract(5, 3) === 2', function () {
  assertEqual(math.subtract(5, 3), 2);
});

test('subtract(0, 5) === -5', function () {
  assertEqual(math.subtract(0, 5), -5);
});

// --- Summary ---
console.log('\n' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  process.exit(1);
}
