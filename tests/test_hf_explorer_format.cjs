const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
const start = source.indexOf('function validateHfExplorerResponse(');
const end = source.indexOf('async function loadHfExplorerResults(', start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const validate = context.validateHfExplorerResponse;

test('OpenVINO rejects a legacy server returning Ministral GGUF', () => {
  assert.throws(() => validate({ models: [{ filename: 'Ministral-3B.gguf' }] }, 'openvino'), /Redémarrez ETU-AI/);
});

test('OpenVINO rejects GGUF even when response format claims OpenVINO', () => {
  assert.throws(() => validate({ model_format: 'openvino', models: [{ format: 'gguf', filename: 'Ministral-3B.gguf' }] }, 'openvino'), /autre format/);
  assert.throws(() => validate({ model_format: 'openvino', models: [{ format: 'openvino', filename: 'Ministral-3B.gguf' }] }, 'openvino'), /autre format/);
});

test('OpenVINO accepts an actual IR export and an empty search', () => {
  validate({ model_format: 'openvino', models: [{ format: 'openvino', filename: 'openvino_model.xml' }] }, 'openvino');
  validate({ model_format: 'openvino', models: [] }, 'openvino');
});

test('ONNX rejects OpenVINO exports', () => {
  assert.throws(() => validate({ model_format: 'onnx', models: [{ format: 'openvino', filename: 'model.xml' }] }, 'onnx'), /autre format/);
});

test('automatic NPU accepts both native formats', () => {
  validate({ model_format: 'npu', models: [{ format: 'openvino', filename: 'model.xml' }, { format: 'onnx', filename: 'onnx/model.onnx' }] }, 'npu');
});

test('GGUF searches stay compatible with older servers', () => {
  validate({ models: [{ filename: 'Ministral-3B.gguf' }] }, 'gguf');
});
