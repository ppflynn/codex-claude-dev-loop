// demo-project/index.js
// A minimal math utility library — used to test AI-driven development

function add(a, b) {
  return a + b;
}

function subtract(a, b) {
  return a - b;
}

// Export for use in tests and other modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { add, subtract };
}
