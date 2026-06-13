import * as path from 'path';
import { globSync } from 'glob';

export function run(): Promise<void> {
  return new Promise((resolve, reject) => {
    const Mocha = require('mocha');
    const mocha = new Mocha({
      ui: 'tdd',
      color: true,
      timeout: 10000,
    });

    const testsRoot = path.resolve(__dirname, '..');

    try {
      const files = globSync('**/**.test.js', { cwd: testsRoot });
      files.forEach((f: string) => mocha.addFile(path.resolve(testsRoot, f)));

      mocha.run((failures: number) => {
        if (failures > 0) {
          reject(new Error(`${failures} tests failed.`));
        } else {
          resolve();
        }
      });
    } catch (err) {
      reject(err);
    }
  });
}

// Entry point when this file is executed directly (e.g. via node ./out/test/runTest.js)
run().then(
  () => process.exit(0),
  () => process.exit(1)
);
