// Offline browser checks. All HTTP responses are intercepted; no server or API keys are used.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const staticRoot = path.join(root, 'webapp/static');
const origin = 'http://127.0.0.1:55839';
const fixtures = [
  { provider: 'openai', model: 'test-openai', label: 'Test OpenAI', reasoning_options: ['none', 'low', 'medium', 'high'], default_reasoning: 'high', max_output_tokens: 128000 },
  { provider: 'anthropic', model: 'test-adaptive', label: 'Test Claude adaptive', thinking_mode: 'adaptive', reasoning_options: ['low', 'medium', 'high', 'max'], default_reasoning: 'high', max_output_tokens: 128000 },
  { provider: 'anthropic', model: 'test-manual', label: 'Test Claude manual', thinking_mode: 'manual', reasoning_options: ['disabled', 'enabled'], default_reasoning: 'enabled', default_thinking_budget: 4096, max_output_tokens: 64000 },
];
const credentials = Object.fromEntries(['openai', 'anthropic'].map(name => [name, { saved: false, available: false, error: null }]));
const conditions = { A: 'A — No added knowledge', B: 'B — Prose knowledge', C: 'C — Structured ontology' };
const batch = { id: 'synthetic_ui', model_count: 2, total_slots: 6, repetitions: 1, case_ids: ['SYNTHETIC_UI'], max_output_tokens: 32768, auto_continue_limit: 2, study_role: 'development_pilot', input_protocol_version: 'synthetic_ui_v1' };
const models = fixtures.slice(0, 2).map((profile, i) => {
  const id = `${batch.id}_m0${i + 1}`;
  const runs = Object.entries(conditions).map(([condition, condition_label], index) => ({ condition, condition_label, case_id: 'SYNTHETIC_UI', repetition: 1, run_id: `r000${index + 1}`, review_id: `S00${index + 1}`, execution_status: 'completed', conversion_status: 'ok', files: { glb: `/files/${id}/S00${index + 1}/model.glb` } }));
  return { experiment: { ...profile, id, reasoning_effort: 'high' }, progress: { scheduled: 3, attempted: 1, finished: 1, remaining: 2, api_calls: 1, continuations: 0, json_completed: 1, failures: 0, input_tokens: 12345, output_tokens: 6789 }, integrity: { passed: true }, busy: false, locked: false, runs, review: { available: true, slots: runs } };
});
const status = { batch, models, jobs: [], busy: false, totals: { scheduled: 6, attempted: 2, finished: 2, remaining: 4, api_calls: 2, continuations: 0, json_completed: 2 } };
const bootstrap = { version: 'TEST', csrf_token: 'SYNTHETIC_TEST_TOKEN', package_root: 'SYNTHETIC_TEST_PACKAGE', credentials, catalog: fixtures, batches: [batch], default_batch: batch.id, defaults: { case_ids: ['SYNTHETIC_UI'], repetitions: 1, max_output_tokens: 32768, auto_continue_limit: 2, max_models: 9 }, new_input: { available: true, input_protocol_version: 'synthetic_ui_v1', cases: [{ case_id: 'SYNTHETIC_UI', image_count: 1, scope: 'A synthetic plate and tube for UI verification.' }], common_instruction: 'Create a 3D model from the synthetic drawing.', technical: { geometry_contract: 'Synthetic geometry contract.', output_schema: '{}' } } };

function triangleGlb() {
  const bin = Buffer.alloc(36);
  [0, 0, 0, 1, 0, 0, 0, 1, 0].forEach((value, index) => bin.writeFloatLE(value, index * 4));
  const doc = { asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }], meshes: [{ primitives: [{ attributes: { POSITION: 0 } }] }], accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', min: [0, 0, 0], max: [1, 1, 0] }], bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: 36 }], buffers: [{ byteLength: 36 }] };
  let json = Buffer.from(JSON.stringify(doc));
  json = Buffer.concat([json, Buffer.alloc((4 - json.length % 4) % 4, 32)]);
  const header = Buffer.alloc(20), binHeader = Buffer.alloc(8);
  header.writeUInt32LE(0x46546c67, 0); header.writeUInt32LE(2, 4); header.writeUInt32LE(28 + json.length + bin.length, 8);
  header.writeUInt32LE(json.length, 12); header.writeUInt32LE(0x4e4f534a, 16);
  binHeader.writeUInt32LE(bin.length, 0); binHeader.writeUInt32LE(0x004e4942, 4);
  return Buffer.concat([header, json, binHeader, bin]);
}
const syntheticGlb = triangleGlb();
const violations = [], pageErrors = [], prepared = [];
const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH || (process.platform === 'win32' && fs.existsSync(edge) ? edge : undefined);
const browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on('pageerror', error => pageErrors.push(error.message));
await page.route('**/*', async route => {
  const req = route.request(), url = new URL(req.url());
  const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  if (url.origin !== origin) { violations.push(`External ${url.origin}`); return route.abort(); }
  if (req.method() === 'POST' && url.pathname === '/api/prepare') {
    prepared.push(req.postDataJSON());
    return json({ job_id: 'synthetic_prepare', batch_id: prepared.at(-1).batch_id });
  }
  if (req.method() !== 'GET') { violations.push(`${req.method()} ${url.pathname}`); return route.abort(); }
  if (url.pathname === '/api/bootstrap') return json(bootstrap);
  if (url.pathname === '/api/status') return json(status);
  if (/^\/files\/synthetic_ui_m0[12]\/S00[123]\/model\.glb$/.test(url.pathname)) return route.fulfill({ contentType: 'model/gltf-binary', body: syntheticGlb });
  if (url.pathname === '/files/synthetic_invalid/S001/model.glb') return route.fulfill({ contentType: 'model/gltf-binary', body: Buffer.from('Invalid synthetic GLB') });
  if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204, body: '' });
  const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  if (['index.html', 'app.js', 'app.css', 'model-viewer.js'].includes(name)) return route.fulfill({ contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(path.join(staticRoot, name)) });
  violations.push(`Unexpected GET ${url.pathname}`); return route.abort();
});
try {
  await page.goto(`${origin}/#experiment`);
  await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
  assert.equal(await page.locator('html').getAttribute('lang'), 'en');
  assert.equal(await page.locator('.model-card').count(), 3);
  assert.equal(await page.locator('#run-button').isDisabled(), true, 'No saved or entered keys must block the paid run');
  assert.match(await page.locator('#run-hint').textContent(), /required provider keys/);
  assert.equal(await page.locator('#mapping-body tr').count(), 6);
  assert.equal(/\p{Script=Hangul}/u.test(await page.locator('body').innerText()), false);
  for (let i = 0; i < fixtures.length; i++) {
    const card = page.locator('.model-card').nth(i);
    assert.deepEqual(await card.locator('select option').evaluateAll(nodes => nodes.map(node => node.value)), fixtures[i].reasoning_options);
  }
  assert.deepEqual(await page.locator('.model-config > label').allTextContents(), ['OpenAI reasoning effort', 'Claude effort · adaptive thinking', 'Claude extended thinking']);
  await page.locator('.model-card').nth(1).locator('input[type=checkbox]').check();
  await page.locator('.model-card').nth(2).locator('input[type=checkbox]').check();
  await page.locator('.model-card').nth(0).locator('select').selectOption('none');
  await page.locator('.model-card').nth(1).locator('select').selectOption('max');
  const manual = page.locator('.model-card').nth(2);
  await manual.locator('select').selectOption('disabled');
  assert.equal(await manual.locator('.thinking-field').isVisible(), false);
  await manual.locator('select').selectOption('enabled');
  assert.equal(await manual.locator('.thinking-field').isVisible(), true);
  await manual.locator('input[type=number]').fill('32768');
  assert.equal(await page.locator('#prepare-button').isDisabled(), true);
  assert.match(await page.locator('#prepare-validation').textContent(), /thinking budget.*below the shared maximum output/);
  await manual.locator('input[type=number]').fill('4096');
  assert.equal(await page.locator('#prepare-button').isDisabled(), false);
  await page.locator('#nav-review').click();
  await page.waitForFunction(() => [...document.querySelectorAll('.local-model-viewer')].length === 2 && [...document.querySelectorAll('.local-model-viewer')].every(item => item.dataset.viewerState === 'loaded'));
  assert.match(await page.locator('.comparison-panel').first().innerText(), /A — No added knowledge/);
  assert.equal(await page.locator('[data-view=front]').first().isDisabled(), false);
  await page.locator('[data-view=front]').first().click();
  assert.equal(await page.locator('[data-view=front]').first().evaluate(button => button.classList.contains('active')), true);
  const errorMessage = await page.evaluate(async () => {
    const { createModelViewer } = await import('/model-viewer.js');
    const container = document.createElement('div'); container.style.cssText = 'width:400px;height:400px'; document.body.append(container);
    const viewer = createModelViewer(container);
    const result = await viewer.load('/files/synthetic_invalid/S001/model.glb');
    const output = { result, message: container.querySelector('[role=status]').textContent };
    viewer.dispose(); container.remove(); return output;
  });
  assert.equal(errorMessage.result.status, 'failed');
  assert.match(errorMessage.message, /not a valid GLB 2.0/);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'No page-wide mobile overflow');
  await page.locator('#nav-experiment').click();
  await page.locator('#new-batch-id').fill('synthetic_prepared');
  await page.locator('#prepare-button').click();
  await page.waitForFunction(() => document.getElementById('info-notice').textContent.includes('Freezing model inputs'));
  assert.equal(prepared.length, 1);
  assert.deepEqual(prepared[0].models, [
    { provider: 'openai', model: 'test-openai', reasoning_effort: 'none', thinking_budget_tokens: null },
    { provider: 'anthropic', model: 'test-adaptive', reasoning_effort: 'max', thinking_budget_tokens: null },
    { provider: 'anthropic', model: 'test-manual', reasoning_effort: 'enabled', thinking_budget_tokens: 4096 },
  ]);
  assert.equal(prepared[0].max_output_tokens, 32768);
  assert.equal(prepared[0].auto_continue_limit, 2);
  assert.deepEqual(violations, []);
  assert.deepEqual(pageErrors, []);
  console.log('PASS: English UI, native reasoning controls, exact mocked preparation payload, paid-run guard, A/B/C mapping, GLB rendering, English errors, mobile layout. No live requests.');
} finally {
  await browser.close();
}
