const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function estimator() {
  const source = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
  const context = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const downloadTimeEstimates ='), source.indexOf('const downloadSpeedHistory =')), context);
  return context.downloadRemainingTime;
}
const MiB = 1024 ** 2;
const task = { task_id: 'model', status: 'downloading', total_bytes: 100 * MiB, downloaded_bytes: 20 * MiB, speed_mbps: 2 };

test('ETA uses the downloader MiB/s units', () => {
  assert.equal(estimator()(task, 1000), 'Temps restant estimé : ~40 s');
});

test('byte samples estimate native bundles between files with zero reported speed', () => {
  const estimate = estimator();
  estimate({ ...task, speed_mbps: 0 }, 1000);
  assert.equal(estimate({ ...task, downloaded_bytes: 30 * MiB, speed_mbps: 0 }, 2000), 'Temps restant estimé : ~7 s');
});

test('unknown size and missing speed never show a fabricated duration', () => {
  assert.match(estimator()({ ...task, total_bytes: 0 }), /calcul en cours/);
  assert.match(estimator()({ ...task, speed_mbps: 0 }), /en attente du débit/);
});

test('a stalled transfer stops showing its old estimate', () => {
  const estimate = estimator();
  estimate(task, 1000);
  assert.match(estimate({ ...task, speed_mbps: 0 }, 17000), /en attente du débit/);
});

test('hour estimates stay readable', () => {
  assert.equal(estimator()({ ...task, total_bytes: 7220 * MiB }, 1000), 'Temps restant estimé : ~1 h 0 min');
});

test('finalization and terminal states have no remaining download duration', () => {
  const estimate = estimator();
  assert.equal(estimate({ ...task, downloaded_bytes: task.total_bytes }), 'Finalisation…');
  for (const status of ['queued', 'cancelled', 'error', 'completed']) assert.equal(estimate({ ...task, status }), '');
});
