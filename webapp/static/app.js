// Vanilla local UI. Credentials stay out of browser storage; the local backend encrypts saved keys.
const $ = (id) => document.getElementById(id);
const state = {
  bootstrap: null, batchId: null, status: null, connected: false, pending: false,
  polling: false, awaitingJobs: new Set(), pendingBatch: null, timer: null,
  globalStatus: null, selectionRevision: 0, bootstrapRevision: 0, pollQueued: false, lastStatusAt: null,
  catalog: new Map(), catalogSignature: '', stopped: new Set(), conditionSignature: '',
  panels: [], viewerModule: null,
  credentials: { openai: { saved: false, available: false, error: null }, anthropic: { saved: false, available: false, error: null } },
};
const providers = { openai: 'OpenAI', anthropic: 'Anthropic · Claude' };
const statusNames = {
  running: 'Running', stopping: 'Stopping after current request', completed: 'Completed', paused: 'Paused', failed: 'Failed',
  pending: 'Pending', not_attempted: 'Unattempted', missing_result: 'No result', error: 'Error',
  provider_incomplete: 'Incomplete response', invalid_model_json: 'JSON parsing failed', refused: 'Generation refused',
  invalid_api_response: 'Invalid API response', http_error: 'HTTP error', transport_error: 'Transport error',
  timeout: 'Timed out', interrupted_outcome_unknown: 'Interrupted · outcome unknown',
  protocol_deviation: 'Protocol deviation', unknown: 'Status needs attention', skipped: 'Skipped',
  clarification_requested: 'Clarification requested', clarification_limit_reached: 'Continuation limit reached',
  stopped_before_continuation: 'Stopped before continuation', transport_error_outcome_unknown: 'Transport interrupted · outcome unknown',
  unexpected_tool_or_output: 'Unexpected output', provider_failed: 'Provider failed',
};
const conversionNames = { ok: '3D conversion complete', partial: 'Partial geometry conversion', failed: '3D conversion failed', empty: 'No geometry', not_attempted: 'No conversion input' };
const actionNames = { prepare: 'Prepare batch', check: 'Integrity check', run: 'API generation and shared conversion', review: 'Shared conversion' };
const n = (value) => value == null || (typeof value === 'string' && !value.trim()) || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('en-US');
const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
const profileKey = (provider, model) => `${provider}\u0000${model}`;
const statusName = (value) => statusNames[value] || value || 'Pending';
const conversionName = (value) => conversionNames[value] || value || 'Conversion status needs attention';
const modelStatuses = () => Array.isArray(state.status?.models) ? state.status.models : [];
const selectedProfiles = () => [...state.catalog.values()].filter((entry) => entry.selected);
const activeJob = (job) => job && ['running', 'stopping'].includes(job.status);
const busy = () => state.pending || Boolean(state.globalStatus?.busy) || Boolean(state.status?.busy) || state.awaitingJobs.size > 0;
const text = (id, value) => { $(id).textContent = value == null ? '' : String(value); };
function el(tag, className = '', content) {
  const node = document.createElement(tag); if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content); return node;
}
function showError(message) { text('error-text', message || 'The request could not be processed.'); $('error-notice').hidden = false; }
function clearError() { $('error-notice').hidden = true; text('error-text', ''); }
function message(value) { text('info-notice', value); $('info-notice').hidden = !value; }
function connected(value) { state.connected = value; text('connection-status', value ? 'Server connected' : 'Server connection needs attention'); $('connection-status').classList.toggle('disconnected', !value); renderFreshness(); if (!value) { renderProgress(modelStatuses()); renderActiveBatchNotice(); } }
function staleStatus() { return Boolean(state.lastStatusAt && (!state.connected || Date.now() - state.lastStatusAt > 8000)); }
function renderFreshness() {
  const stale = staleStatus();
  $('stale-status').hidden = !stale;
  if (stale) {
    const checked = new Date(state.lastStatusAt).toLocaleTimeString('en-US', { hour12: false });
    text('stale-status', `Status updates are interrupted or delayed. Showing the last status received at ${checked}. The execution state has not been confirmed since then.`);
    text('last-updated', `${checked} · Last known status`);
  }
}
function globalActiveJobs() { return (state.globalStatus?.jobs || state.status?.jobs || []).filter(activeJob); }
function renderActiveBatchNotice() {
  const allJobs = globalActiveJobs(), current = allJobs.filter(job => job.batch_id === state.batchId);
  const jobs = allJobs.filter(job => job.batch_id && job.batch_id !== state.batchId);
  const ids = [...new Set(jobs.map(job => job.batch_id))];
  $('other-busy').hidden = !ids.length && !current.length;
  const fragment = document.createDocumentFragment();
  if (current.length) {
    const runningModels = current.every(job => job.action === 'run');
    fragment.append(el('span', '', `${n(current.length)} ${runningModels ? 'model runs' : 'jobs'} are active in this batch.${staleStatus() ? ' This is the last known status.' : ''}`));
    const link = el('a', 'active-batch-link current-batch-progress-link', 'View progress ↓'); link.href = '#execution-title';
    link.addEventListener('click', event => { event.preventDefault(); location.hash = 'execution-title'; requestAnimationFrame(() => $('execution-title').scrollIntoView({ block: 'start', behavior: 'instant' })); });
    fragment.append(link);
  }
  if (ids.length) fragment.append(el('span', '', `${n(jobs.length)} jobs are active in other batches. Switch batches to view their status.${staleStatus() ? ' This is the last known status.' : ''}`));
  for (const id of ids) {
    const link = el('a', 'active-batch-link', `View running batch · ${id}`);
    const url = new URL(location.href); url.searchParams.set('batch', id); url.hash = 'experiment';
    link.href = url.href;
    link.addEventListener('click', event => { event.preventDefault(); location.hash = 'experiment'; openBatch(id); });
    fragment.append(link);
  }
  $('other-busy').replaceChildren(fragment);
}
function elapsedLabel(start, end) {
  const beginning = Date.parse(start), ending = typeof end === 'number' ? end : Date.parse(end);
  if (!Number.isFinite(beginning) || !Number.isFinite(ending)) return 'Unknown';
  const seconds = Math.max(0, Math.floor((ending - beginning) / 1000));
  return `${Math.floor(seconds / 3600) ? `${Math.floor(seconds / 3600)}h ` : ''}${Math.floor(seconds / 60) % 60}m ${seconds % 60}s`;
}
function modelRuntime(model) {
  const job = model.job;
  if (!job || job.action !== 'run') return null;
  const section = el('div', 'model-runtime'); const live = activeJob(job);
  const last = Array.isArray(job.events) ? job.events[job.events.length - 1] : null;
  const active = model.progress?.active_run;
  const runId = typeof active === 'object' ? active?.run_id : active;
  const latestRunId = last && ['call_started', 'turn_started', 'auto_continuation'].includes(last.event) ? last.run_id : null;
  const row = (model.runs || []).find(item => item.run_id === (latestRunId || runId));
  const waiting = live && ['call_started', 'turn_started'].includes(last?.event);
  const stage = !live ? `Model run: ${statusName(job.status)}` : last?.event === 'conversion_started' ? 'Running shared 3D conversion'
    : waiting ? 'Waiting for API response' : last?.event === 'auto_continuation' ? 'Preparing the fixed follow-up request'
      : ['call_finished', 'turn_finished'].includes(last?.event) ? 'Processing received response' : 'Model job in progress';
  section.append(el('strong', 'model-runtime-stage', `${stage}${staleStatus() ? ' · Last known status' : ''}`));
  if (live && row && last?.event !== 'conversion_started') section.append(el('p', 'model-runtime-condition', `Condition ${conditionCode(row) || 'Unknown'} · Case ${row.case_id || 'Unknown'} · Repetition ${n(row.repetition)} · ${row.run_id}`));
  const end = live ? (staleStatus() ? state.lastStatusAt : Date.now()) : job.finished_utc;
  if (job.started_utc) section.append(el('p', 'model-runtime-elapsed', `Model run elapsed: ${elapsedLabel(job.started_utc, end)}${staleStatus() ? ' · At last status update' : ''}`));
  if (waiting) section.append(el('p', 'model-runtime-note', 'Request progress and token usage are unavailable until the response completes. The bar below counts processed slots.'));
  return section;
}
function badgeClass(value) {
  if (['completed', 'ok', 'passed'].includes(value)) return 'success';
  if (['running', 'stopping'].includes(value)) return 'running';
  if (['paused', 'partial', 'empty', 'skipped'].includes(value)) return 'warning';
  if (['pending', 'not_attempted', 'missing_result', null, undefined].includes(value)) return 'neutral';
  return 'failed';
}
function setBadge(id, label, kind = 'neutral') { text(id, label); $(id).className = `badge ${kind}`; }
function modelLabel(experiment) {
  return state.catalog.get(profileKey(experiment.provider, experiment.model))?.profile.label || experiment.model || experiment.id;
}
function reasoningUI(profile) {
  if (profile.provider === 'openai') return {
    label: 'OpenAI reasoning effort', field: 'reasoning.effort',
    note: 'Choose reasoning effort. This does not directly set a token budget or thinking duration.',
  };
  if (profile.thinking_mode === 'manual') return {
    label: 'Claude extended thinking', field: 'thinking.type',
    note: 'Enable or disable extended thinking. When enabled, set its target token budget. This differs from an effort level.',
  };
  return {
    label: 'Claude effort · adaptive thinking', field: 'output_config.effort',
    note: 'Uses adaptive thinking. Effort controls work spent on thinking and answering; it is not a fixed token budget.',
  };
}
const reasoningOptionLabel = (value) => ({ none: 'No reasoning · none', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Maximum', disabled: 'Off · disabled', enabled: 'On · enabled' }[value] || value);
function reasoningSummary(experiment) {
  const effort = experiment.reasoning_effort;
  if (experiment.provider === 'openai') return `OpenAI reasoning effort: ${effort}`;
  if (experiment.provider === 'anthropic') {
    if (['enabled', 'disabled'].includes(effort)) return `Claude extended thinking: ${reasoningOptionLabel(effort)}${effort === 'enabled' ? ` · Target budget: ${n(experiment.thinking_budget_tokens)} tokens` : ''}`;
    return `Claude effort: ${effort}`;
  }
  return `Setting: ${effort || 'Not recorded'}`;
}
function inputVersion(input) {
  if (input?.input_protocol_version) return `Input ${input.input_protocol_version}`;
  return `Input version not recorded${input?.engine_version ? ` · Engine ${input.engine_version}` : ''}`;
}
function renderInputPreview(preview) {
  const available = preview?.available === true;
  const cases = Array.isArray(preview?.cases) ? preview.cases : [];
  const summary = available ? `${cases.map(item => `${item.case_id} · ${n(item.image_count)} images`).join(' / ')} · ${inputVersion(preview)}` : 'New batch input needs attention';
  text('input-preview-meta', summary); text('review-new-input-summary', summary);
  setBadge('input-preview-badge', available ? 'Applies to new batches' : 'Input needs attention', available ? 'success' : 'warning');
  $('input-preview-body').hidden = !available; $('input-preview-error').hidden = available;
  text('input-preview-error', available ? '' : preview?.error || 'Could not load the new batch input. Refresh this page.');
  const fragment = document.createDocumentFragment();
  for (const item of cases) {
    const section = el('section', 'case-scope');
    section.append(el('h4', '', `Target ${item.case_id} · ${n(item.image_count)} drawing images`), el('pre', 'prompt-text', item.scope || ''));
    fragment.append(section);
  }
  $('input-cases').replaceChildren(fragment);
  text('input-common-instruction', preview?.common_instruction || '');
  text('input-geometry-contract', preview?.technical?.geometry_contract || '');
  text('input-output-schema', preview?.technical?.output_schema || '');
}
function dateId() {
  const date = new Date(); const pad = (value) => String(value).padStart(2, '0');
  return `batch_${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

async function api(path, { method = 'GET', body } = {}) {
  const options = { method, cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (method === 'POST') {
    if (!state.bootstrap?.csrf_token) throw new Error('Refresh the local app and try again.');
    options.headers['Content-Type'] = 'application/json'; options.headers['X-CSRF-Token'] = state.bootstrap.csrf_token;
    options.body = JSON.stringify(body ?? {});
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Could not connect to the local server. Restart the local app and refresh this page.'); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error('Could not read the server response. Restart the local app and refresh this page.'); }
  if (!response.ok) throw new Error(typeof result?.error === 'string' ? result.error : `The request could not be processed. HTTP ${response.status}`);
  return result;
}
function rememberJobs(result) {
  for (const id of result.job_ids || (result.job_id ? [result.job_id] : [])) state.awaitingJobs.add(id);
}
function updateCredentials(credentials) {
  if (!credentials || typeof credentials !== 'object') return;
  for (const provider of ['openai', 'anthropic']) {
    const metadata = credentials[provider];
    if (!metadata || typeof metadata !== 'object') continue;
    // Only metadata is accepted here. Saved secret values are never returned to inputs.
    state.credentials[provider] = { saved: metadata.saved === true, available: metadata.available === true, error: typeof metadata.error === 'string' ? metadata.error : null };
  }
}
const credentialAvailable = (provider) => state.credentials[provider]?.available === true;
function acknowledgeJobs(data) {
  const jobs = [...(data.jobs || []), data.preparation, ...((data.models || []).map((model) => model.job))].filter(Boolean);
  for (const job of jobs) state.awaitingJobs.delete(job.id);
  for (const id of [...state.stopped]) if (!jobs.some((job) => job.experiment_id === id && activeJob(job) && job.action === 'run')) state.stopped.delete(id);
}

function renderCatalog(profiles) {
  const signature = JSON.stringify(profiles);
  if (signature === state.catalogSignature) return;
  const old = state.catalog; state.catalog = new Map(); state.catalogSignature = signature;
  const fragment = document.createDocumentFragment(); const picked = new Set();
  profiles.forEach((profile, index) => {
    const key = profileKey(profile.provider, profile.model); const previous = old.get(key);
    const defaultSelection = !old.size && index === 0;
    if (defaultSelection) picked.add(profile.provider);
    const options = Array.isArray(profile.reasoning_options) ? profile.reasoning_options : [];
    const entry = {
      profile, selected: previous?.selected ?? defaultSelection,
      reasoning: options.includes(previous?.reasoning) ? previous.reasoning : options.includes(profile.default_reasoning) ? profile.default_reasoning : options[0] || '',
      budget: previous?.budget ?? profile.default_thinking_budget ?? '', refs: {},
    };
    const card = el('article', 'model-card'); const label = el('label', 'model-card-head');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.id = `model-check-${index}`; checkbox.checked = entry.selected;
    const title = el('span', 'model-card-title'); title.append(el('span', 'provider-caption', providers[profile.provider] || profile.provider), el('strong', '', profile.label || profile.model), el('small', '', profile.model));
    label.htmlFor = checkbox.id; label.append(checkbox, title);
    const ui = reasoningUI(profile);
    const config = el('div', 'model-config'); const reasoningLabel = el('label', '', ui.label);
    const select = document.createElement('select'); select.id = `model-reasoning-${index}`; reasoningLabel.htmlFor = select.id;
    for (const option of options) { const node = document.createElement('option'); node.value = option; node.textContent = reasoningOptionLabel(option); select.append(node); }
    select.value = entry.reasoning;
    const note = el('p', 'model-reasoning-note', ui.note); note.id = `model-reasoning-note-${index}`; select.setAttribute('aria-describedby', note.id);
    const budgetField = el('div', 'thinking-field'); const budgetLabel = el('label', '', 'Thinking token budget');
    const budget = document.createElement('input'); budget.type = 'number'; budget.min = '1024'; budget.step = '1'; budget.value = entry.budget; budget.id = `model-budget-${index}`; budgetLabel.htmlFor = budget.id;
    const budgetNote = el('p', 'model-reasoning-note', 'Enter at least 1,024 and less than the shared maximum output. This is a target budget; actual usage may vary.');
    budgetNote.id = `model-budget-note-${index}`; budget.setAttribute('aria-describedby', budgetNote.id);
    budgetField.append(budgetLabel, budget, budgetNote); config.append(reasoningLabel, select, note, budgetField);
    const wire = el('details', 'model-wire'); const wireValue = el('code');
    wire.append(el('summary', '', 'Settings recorded in the API request'), wireValue); config.append(wire);
    config.append(el('p', 'model-support-note', `Documented output limit: ${n(profile.max_output_tokens)} tokens`));
    card.append(label, config); entry.refs = { card, checkbox, select, budget, budgetField, wireValue };
    checkbox.addEventListener('change', () => { entry.selected = checkbox.checked; renderControls(); });
    select.addEventListener('change', () => { entry.reasoning = select.value; renderControls(); });
    budget.addEventListener('input', () => { entry.budget = budget.value; renderControls(); });
    state.catalog.set(key, entry); fragment.append(card);
  });
  $('model-grid').replaceChildren(fragment); $('catalog-empty').hidden = profiles.length > 0;
  if (!profiles.length) { $('catalog-empty').replaceChildren(el('strong', '', 'No model profiles are available.'), el('p', '', 'Check the server model catalog, then refresh.')); }
}

function prepareValidation() {
  if (!state.bootstrap) return '';
  if (state.bootstrap.new_input?.available === false) return 'Check the new batch input before preparing it. Existing frozen batches remain available to view and run.';
  const selected = selectedProfiles(); const maxModels = count(state.bootstrap.defaults?.max_models) || 9;
  const reps = Number($('repetitions').value), output = Number($('max-output').value), limit = Number($('continue-limit').value);
  if (selected.length < 1 || selected.length > maxModels) return `Select between 1 and ${maxModels} models.`;
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,69}$/.test($('new-batch-id').value.trim())) return 'A batch ID must start with a letter or digit and contain up to 70 letters, digits, underscores, or hyphens.';
  if ((state.bootstrap.batches || []).some((batch) => batch.id === $('new-batch-id').value.trim())) return 'This batch ID already exists. Choose a new ID.';
  if (!Number.isInteger(reps) || reps < 1 || reps > 100) return 'Repetitions per condition must be an integer from 1 to 100.';
  if (!Number.isInteger(output) || output < 1 || output > 128000) return 'Maximum output must be an integer from 1 to 128,000.';
  if (!Number.isInteger(limit) || limit < 0 || limit > 5) return 'The continuation limit must be from 0 to 5.';
  for (const entry of selected) {
    if (!entry.profile.reasoning_options.includes(entry.reasoning)) return `Select a supported reasoning setting for ${entry.profile.label || entry.profile.model}.`;
    if (output > entry.profile.max_output_tokens) return `${entry.profile.label || entry.profile.model} supports up to ${n(entry.profile.max_output_tokens)} output tokens. Lower the shared limit.`;
    if (entry.profile.thinking_mode === 'manual' && entry.reasoning === 'enabled' && (!Number.isInteger(Number(entry.budget)) || Number(entry.budget) < 1024 || Number(entry.budget) >= output)) return `The thinking budget for ${entry.profile.label || entry.profile.model} must be an integer of at least 1,024 and below the shared maximum output.`;
  }
  return '';
}
function runEstimates() {
  const cap = Number($('max-new-slots').value); let initial = 0; let maximum = 0;
  for (const model of modelStatuses()) {
    if (model.protocol_deviation) continue;
    const slots = cap > 0 ? Math.min(count(model.progress?.remaining), cap) : count(model.progress?.remaining);
    initial += slots; maximum += slots * (1 + count(model.experiment?.auto_continue_limit ?? state.status?.batch?.auto_continue_limit));
  }
  return { initial, maximum };
}
function neededProviders() { return new Set(modelStatuses().filter((model) => count(model.progress?.remaining) > 0 && !model.protocol_deviation).map((model) => model.experiment.provider)); }
function renderControls() {
  const unavailable = !state.connected || !state.bootstrap || busy();
  const entries = selectedProfiles(); const maxModels = count(state.bootstrap?.defaults?.max_models) || 9;
  for (const entry of state.catalog.values()) {
    const manual = entry.profile.thinking_mode === 'manual' && entry.reasoning === 'enabled';
    entry.refs.card.classList.toggle('selected', entry.selected);
    entry.refs.checkbox.disabled = unavailable || (!entry.selected && entries.length >= maxModels);
    entry.refs.select.disabled = unavailable || !entry.selected;
    entry.refs.budgetField.hidden = !manual;
    entry.refs.budget.disabled = unavailable || !entry.selected || !manual;
    if (manual) entry.refs.budget.max = String(Math.max(1024, Number($('max-output').value) - 1));
    const field = reasoningUI(entry.profile).field;
    entry.refs.wireValue.textContent = `${entry.profile.thinking_mode === 'adaptive' ? 'thinking.type = adaptive\n' : ''}${field} = ${entry.reasoning}${manual ? `\nthinking.budget_tokens = ${entry.budget}` : ''}`;
  }
  for (const id of ['repetitions', 'max-output', 'continue-limit', 'new-batch-id']) $(id).disabled = unavailable;
  const reps = Number($('repetitions').value), limit = Number($('continue-limit').value), cases = state.bootstrap?.defaults?.case_ids?.length || 0;
  const planned = entries.length * cases * (Number.isInteger(reps) && reps > 0 ? reps : 0) * 3;
  text('selected-count', `${entries.length} / ${maxModels} selected`);
  text('prepare-estimate', `${n(entries.length)} models · ${n(planned)} initial slots`);
  text('prepare-max-estimate', `Up to ${n(planned * (1 + (Number.isInteger(limit) ? limit : 0)))} calls including continuations · Preparation only`);
  const outputCap = entries.length ? Math.min(...entries.map((entry) => entry.profile.max_output_tokens)) : 128000;
  $('max-output').max = String(Math.min(128000, outputCap));
  text('output-cap-note', `Shared limit for selected models: ${n(outputCap)} tokens`);
  const validation = prepareValidation(); text('prepare-validation', validation);
  $('prepare-button').disabled = unavailable || Boolean(validation);
  $('batch-select').disabled = !state.connected || !state.bootstrap || state.pending || !(state.bootstrap?.batches?.length);
  $('refresh-button').disabled = state.pending;
  const hasBatch = Boolean(state.batchId && state.status?.batch);
  const models = modelStatuses(), unfinished = models.filter((model) => count(model.progress?.remaining) > 0 && !model.protocol_deviation);
  const excluded = models.filter((model) => model.protocol_deviation && count(model.progress?.remaining) > 0);
  const passed = unfinished.length > 0 && unfinished.every((model) => model.integrity?.passed === true);
  const blocked = unfinished.some((model) => model.locked);
  const checked = unfinished.filter((model) => model.integrity?.passed).length;
  $('check-button').disabled = unavailable || !hasBatch;
  text('check-summary', hasBatch ? `${n(checked)} / ${n(unfinished.length)} resumable models passed${excluded.length ? ` · ${n(excluded.length)} models excluded for protocol deviations` : ''}` : 'The integrity check must pass in this server session before a paid run.');
  const required = neededProviders();
  for (const provider of ['openai', 'anthropic']) {
    $(`key-field-${provider}`).hidden = false;
    const input = $(`key-${provider}`);
    const credential = state.credentials[provider];
    input.disabled = unavailable;
    input.placeholder = credential.available ? 'Reuse saved key · Enter only to replace it' : `Enter and save your ${provider === 'openai' ? 'OpenAI' : 'Anthropic'} API key`;
    document.querySelector(`[data-show-key="${provider}"]`).disabled = unavailable;
    $(`save-key-${provider}`).disabled = unavailable || !input.value.trim();
    text(`save-key-${provider}`, credential.saved ? 'Replace saved key' : 'Save key');
    $(`delete-key-${provider}`).hidden = !credential.saved;
    $(`delete-key-${provider}`).disabled = unavailable;
    setBadge(`key-status-${provider}`, credential.available ? 'Encrypted · Ready to reuse' : credential.saved ? 'Could not read saved key' : 'No saved key', credential.available ? 'success' : credential.saved ? 'failed' : 'neutral');
    text(`key-error-${provider}`, credential.error || '');
    $(`key-error-${provider}`).hidden = !credential.error;
    text(`key-use-${provider}`, required.has(provider) ? 'Required for this batch' : 'Can be saved before preparing a batch');
  }
  $('no-keys').hidden = false;
  text('no-keys', !hasBatch ? 'You can save keys before preparing a batch. Saved keys are reused automatically in later runs.'
    : required.size ? `Keys needed for this batch: ${[...required].map(provider => providers[provider]).join(', ')}. Reuse saved keys or enter new ones.`
    : 'No unattempted models are currently runnable. You can manage keys for a future batch here.');
  $('max-new-slots').disabled = unavailable || !unfinished.length || blocked;
  const estimates = runEstimates();
  text('run-initial-estimate', `Initial slots for this run: up to ${n(estimates.initial)}`);
  text('run-maximum-estimate', `Maximum calls including continuations: ${n(estimates.maximum)}`);
  text('run-button', `Start concurrent paid run · ${n(estimates.initial)} slots / up to ${n(estimates.maximum)} calls`);
  const validCap = Number.isInteger(Number($('max-new-slots').value)) && Number($('max-new-slots').value) >= 0 && Number($('max-new-slots').value) <= 100000;
  const keysReady = [...required].every((provider) => $(`key-${provider}`)?.value.trim() || credentialAvailable(provider));
  $('run-button').disabled = unavailable || !hasBatch || !passed || blocked || !estimates.initial || !keysReady || !validCap;
  let hint = 'Prepare a batch and pass the integrity check first.';
  if (hasBatch && busy()) hint = 'Wait for the active jobs to finish.';
  else if (hasBatch && blocked) hint = 'Check the execution locks for models to resume.';
  else if (hasBatch && !unfinished.length) hint = excluded.length ? 'Models with protocol deviations need a new batch. No models can currently resume.' : 'All slots have already been attempted. Review the generated results.';
  else if (hasBatch && !passed) hint = 'All unfinished models must pass the input integrity check.';
  else if (hasBatch && !keysReady) hint = 'Enter the required provider keys or resolve saved-key read errors.';
  else if (hasBatch) hint = excluded.length ? 'Models with protocol deviations are excluded. Their records are preserved.' : 'Provider requests start only when you select the paid-run button.';
  if (excluded.length && unfinished.length && !hint.toLowerCase().includes('protocol deviation')) hint += ' Models with protocol deviations are excluded.';
  text('run-hint', hint);
  const running = models.filter((model) => model.busy && model.job?.action === 'run' && activeJob(model.job));
  $('stop-all-button').hidden = !running.length;
  $('stop-all-button').disabled = state.pending || running.every((model) => state.stopped.has(model.experiment.id) || model.job.status === 'stopping');
  const attempted = models.some((model) => count(model.progress?.attempted) > 0);
  $('review-button').disabled = unavailable || !hasBatch || !attempted;
  $('review-rebuild-button').disabled = $('review-button').disabled;
  document.querySelectorAll('[data-stop-model]').forEach((button) => button.disabled = state.pending || state.stopped.has(button.dataset.stopModel));
}

function renderProgress(models) {
  $('models-progress-empty').hidden = models.length > 0;
  const fragment = document.createDocumentFragment();
  for (const model of models) {
    const experiment = model.experiment, progress = model.progress || {}, job = model.job;
    const card = el('article', 'model-progress'); const head = el('div', 'model-progress-head'); const title = el('div');
    title.append(el('strong', '', modelLabel(experiment)), el('p', '', reasoningSummary(experiment)));
    const stage = model.protocol_deviation ? 'protocol_deviation' : job?.status || (count(progress.attempted) ? 'paused' : 'pending');
    head.append(title, el('span', `badge ${badgeClass(stage)}`, statusName(stage))); card.append(head);
    const runtime = modelRuntime(model); if (runtime) card.append(runtime);
    const integrityText = model.integrity?.passed === true ? 'Integrity check passed' : model.integrity?.passed === false ? `Integrity check failed${model.integrity.message ? `: ${model.integrity.message}` : ''}` : 'Integrity check required in this session';
    card.append(el('p', `model-integrity${model.integrity?.passed === false ? ' error-text' : ''}`, integrityText));
    const labels = el('div', 'model-progress-label'); labels.append(el('span', '', `Processed slots: ${n(progress.finished)} / ${n(progress.scheduled)}`), el('span', '', `Unattempted: ${n(progress.remaining)}`));
    const bar = document.createElement('progress'); bar.max = Math.max(1, count(progress.scheduled)); bar.value = Math.min(count(progress.finished), count(progress.scheduled)); bar.setAttribute('aria-label', `${modelLabel(experiment)} slot progress`);
    const counts = el('div', 'model-counts');
    for (const [label, value] of [['Provider API calls', progress.api_calls], ['Automatic continuations', progress.continuations], ['Generation or parsing failures', progress.failures]]) { const item = el('div', '', label); item.append(el('strong', '', n(value))); counts.append(item); }
    card.append(labels, bar, counts, el('p', 'model-tokens', `Tokens: input ${n(progress.input_tokens)} · output ${n(progress.output_tokens)} · cached ${n(progress.cached_tokens)} · reasoning ${n(progress.reasoning_tokens)}`));
    if (job?.message) card.append(el('p', 'model-message', job.message));
    if (model.locked && !model.busy) card.append(el('p', 'model-message', 'An execution lock remains. Check the corresponding process.'));
    if (model.protocol_deviation) card.append(el('p', 'model-message', 'A protocol deviation is recorded. Preserve the original and prepare a new batch.'));
    const footer = el('div', 'model-footer'); const active = progress.active_run;
    footer.append(el('span', '', active ? `Processing ${typeof active === 'object' ? active.run_id || 'current slot' : active}` : `JSON completed: ${n(progress.json_completed)}`));
    if (model.busy && job?.action === 'run' && activeJob(job)) {
      const stop = el('button', 'button stop', 'Stop this model'); stop.type = 'button'; stop.dataset.stopModel = experiment.id;
      stop.disabled = state.pending || state.stopped.has(experiment.id) || job.status === 'stopping';
      stop.addEventListener('click', () => stopModels(experiment.id)); footer.append(stop);
    }
    card.append(footer); fragment.append(card);
  }
  $('models-progress').replaceChildren(fragment);
}
function renderStatus(data) {
  state.status = data; acknowledgeJobs(data); const batch = data.batch, totals = data.totals || {};
  text('sidebar-batch', batch?.id || 'None yet');
  text('sidebar-summary', batch ? `${n(batch.model_count)} models · ${n(batch.total_slots)} slots · Up to ${n(batch.auto_continue_limit)} continuations` : 'Choose generation settings to prepare a new batch.');
  setBadge('study-role', batch ? batch.study_role === 'held_out' ? 'Held-out drawing evaluation' : 'Development pilot' : 'No selection');
  text('batch-summary', batch ? `Frozen cases: ${(batch.case_ids || []).join(', ')} · ${inputVersion(batch)} · ${n(batch.model_count)} models` : 'Prepared batches will appear here.');
  text('active-batch-description', batch ? `${batch.id} · Up to ${n(batch.max_output_tokens)} output tokens · Up to ${n(batch.auto_continue_limit)} clarification continuations per slot` : 'First, prepare a batch with the selected models and shared settings.');
  text('active-batch-input', batch ? `Cases: ${(batch.case_ids || []).join(', ')} · ${inputVersion(batch)} · ${n(batch.repetitions)} repetitions per condition. This batch uses the request and drawings frozen at preparation.` : 'Prepare or select a batch to see its target and input version.');
  for (const [id, key] of [['stat-attempted', 'attempted'], ['stat-scheduled', 'scheduled'], ['stat-api-calls', 'api_calls'], ['stat-continuations', 'continuations'], ['stat-json', 'json_completed']]) text(id, n(batch ? totals[key] : null));
  for (const key of ['input', 'output', 'cached', 'reasoning']) text(`usage-${key}`, n(batch ? totals[`${key}_tokens`] : null));
  text('last-updated', `${new Date().toLocaleTimeString('en-US', { hour12: false })} · Status updates every 2 seconds`);
  const runningJobs = (data.jobs || []).filter(activeJob); const preparing = activeJob(data.preparation);
  renderActiveBatchNotice(); renderFreshness();
  if (preparing) { setBadge('batch-job-badge', 'Preparing batch', 'running'); text('job-message', data.preparation.message || 'Freezing the new input and model settings. No provider API calls are made.'); }
  else if (runningJobs.length) { setBadge('batch-job-badge', `${n(runningJobs.length)} active jobs`, 'running'); text('job-message', 'Each model uses only its own experiment inputs and results. A failure does not stop other models.'); }
  else if (batch && count(totals.scheduled) > 0 && count(totals.remaining) === 0) { setBadge('batch-job-badge', 'All slots attempted', 'success'); text('job-message', 'All scheduled slots have been attempted. Review API, parsing, and geometry-conversion status separately.'); }
  else { setBadge('batch-job-badge', batch ? 'Ready to run' : 'Awaiting preparation'); text('job-message', batch ? 'Check integrity before starting the paid run. Existing attempts and failures are preserved.' : 'Select a prepared batch to see its execution status.'); }
  renderProgress(modelStatuses());
  const slots = modelStatuses().flatMap((model) => model.review?.slots || []), glbs = slots.filter((slot) => slot.files?.glb).length;
  text('review-count', slots.length ? `${n(slots.length)} review slots · ${n(glbs)} GLB files · Attempting every slot does not mean every 3D conversion succeeded.` : 'No review artifacts yet.');
  renderConditionTable(); updatePanels(); renderControls();
}

function clearProviderKey(provider) {
  $(`key-${provider}`).value = ''; $(`key-${provider}`).type = 'password';
  const button = document.querySelector(`[data-show-key="${provider}"]`); button.textContent = 'Show'; button.setAttribute('aria-pressed', 'false'); button.setAttribute('aria-label', `Show ${providers[provider]} API key`);
}
function clearKeys() { for (const provider of ['openai', 'anthropic']) clearProviderKey(provider); }
async function saveCredential(provider) {
  if (busy() || $(`save-key-${provider}`).disabled || !state.connected) return;
  const keys = { [provider]: $(`key-${provider}`).value.trim() };
  clearProviderKey(provider); state.pending = true; clearError(); message(''); renderControls();
  try {
    const result = await api('/api/credentials/save', { method: 'POST', body: { keys } });
    updateCredentials(result.credentials);
    message(`The ${providers[provider]} key is encrypted and saved on this PC for automatic reuse. No provider API calls were made.`);
  } catch (error) { showError(error.message); }
  finally { delete keys[provider]; state.pending = false; renderControls(); }
}
async function deleteCredential(provider) {
  if (busy() || $(`delete-key-${provider}`).disabled || !state.connected || !state.credentials[provider]?.saved) return;
  clearProviderKey(provider); state.pending = true; clearError(); message(''); renderControls();
  try {
    const result = await api('/api/credentials/delete', { method: 'POST', body: { provider } });
    updateCredentials(result.credentials);
    message(`The saved ${providers[provider]} key was deleted from this PC. This does not revoke the key in your provider account.`);
  } catch (error) { showError(error.message); }
  finally { state.pending = false; renderControls(); }
}
function selectBatch(id, updateUrl = false) {
  state.selectionRevision++;
  state.batchId = id || null; state.status = null; state.conditionSignature = ''; state.stopped.clear();
  if (updateUrl) { const url = new URL(location.href); if (id) url.searchParams.set('batch', id); else url.searchParams.delete('batch'); history.replaceState(null, '', url); }
  clearKeys(); $('max-new-slots').value = '0'; renderConditionTable();
  for (const panel of state.panels) resetPanel(panel, true);
  renderProgress([]); text('active-batch-description', id ? `${id} · Loading status.` : 'Select a batch.');
  clearError(); renderActiveBatchNotice(); renderControls();
}
async function openBatch(id) {
  try {
    if (!(state.bootstrap?.batches || []).some(batch => batch.id === id)) await bootstrap(id);
    if ((state.bootstrap?.batches || []).some(batch => batch.id === id)) { $('batch-select').value = id; selectBatch(id, true); await poll({ reportError: true }); }
  } catch (error) { connected(false); showError(error.message); renderControls(); }
}
async function bootstrap(preferredId) {
  const revision = ++state.bootstrapRevision, selection = state.selectionRevision;
  const [data, global] = await Promise.all([api('/api/bootstrap'), api('/api/status')]);
  if (revision !== state.bootstrapRevision) return state.bootstrap;
  const first = !state.bootstrap; state.bootstrap = data; state.globalStatus = global; connected(true);
  acknowledgeJobs(global);
  updateCredentials(data.credentials);
  text('package-path', data.package_root); text('app-version', `LOCAL · ${data.version || 'v0.2'}`);
  renderCatalog(Array.isArray(data.catalog) ? data.catalog : []);
  renderInputPreview(data.new_input);
  if (first) {
    $('repetitions').value = data.defaults?.repetitions ?? 1; $('max-output').value = data.defaults?.max_output_tokens ?? 128000;
    $('continue-limit').value = data.defaults?.auto_continue_limit ?? 2; $('new-batch-id').value = dateId();
  }
  const batches = Array.isArray(data.batches) ? data.batches : []; const fragment = document.createDocumentFragment();
  if (!batches.length) { const option = document.createElement('option'); option.value = ''; option.textContent = 'No prepared batches yet'; fragment.append(option); }
  for (const batch of batches) { const option = document.createElement('option'); option.value = batch.id; option.textContent = `${batch.id} · ${(batch.case_ids || []).join(', ')} · ${n(batch.model_count)} models`; fragment.append(option); }
  $('batch-select').replaceChildren(fragment);
  const explicit = selection !== state.selectionRevision ? state.batchId : preferredId || new URLSearchParams(location.search).get('batch');
  const active = globalActiveJobs().slice().sort((a, b) => String(b.started_utc || '').localeCompare(String(a.started_utc || ''))).find(job => batches.some(batch => batch.id === job.batch_id))?.batch_id;
  const selected = batches.some((batch) => batch.id === explicit) ? explicit : active || data.default_batch || batches[0]?.id || null;
  $('batch-select').value = selected || '';
  if (selected !== state.batchId) selectBatch(selected);
  renderControls(); return data;
}
async function poll({ reportError = false } = {}) {
  if (!state.bootstrap) return;
  if (state.polling) { state.pollQueued = true; return; }
  state.polling = true; const batchId = state.batchId, selection = state.selectionRevision;
  try {
    const [global, data] = await Promise.all([api('/api/status'), batchId ? api(`/api/status?batch_id=${encodeURIComponent(batchId)}`) : api('/api/status')]);
    if (selection !== state.selectionRevision) return;
    state.globalStatus = global; state.lastStatusAt = Date.now(); acknowledgeJobs(global);
    connected(true); renderStatus(data);
    if (state.pendingBatch && !activeJob(data.preparation) && !state.awaitingJobs.size && !data.busy) {
      const createdId = state.pendingBatch; state.pendingBatch = null;
      const updated = await bootstrap(createdId);
      if (updated.batches?.some((batch) => batch.id === createdId)) {
        $('new-batch-id').value = dateId();
        message('Input and settings are frozen for each model. Check batch integrity and the required saved keys. No provider API calls have been made.');
        const nextSelection = state.selectionRevision;
        const next = await api(`/api/status?batch_id=${encodeURIComponent(state.batchId)}`);
        if (nextSelection === state.selectionRevision) { state.lastStatusAt = Date.now(); renderStatus(next); }
      } else showError(data.preparation?.message || 'Batch preparation failed. Check the error and prepare again with a new batch ID.');
    }
  } catch (error) { if (selection === state.selectionRevision) { connected(false); renderProgress(modelStatuses()); renderActiveBatchNotice(); renderControls(); if (reportError) showError(error.message); } }
  finally { state.polling = false; if (state.pollQueued || selection !== state.selectionRevision) { state.pollQueued = false; queueMicrotask(() => poll()); } }
}
async function action(name) {
  if (busy() || !state.batchId) return;
  state.pending = true; clearError(); message(''); renderControls();
  try {
    const result = await api(`/api/${name}`, { method: 'POST', body: { batch_id: state.batchId } }); rememberJobs(result);
    message(`${actionNames[name]} request accepted. Check progress for each model.`); await poll({ reportError: true });
  } catch (error) { showError(error.message); }
  finally { state.pending = false; renderControls(); }
}
async function stopModels(experimentId) {
  if (state.pending || !state.batchId) return;
  state.pending = true; clearError(); renderControls();
  try {
    const body = { batch_id: state.batchId }; if (experimentId) body.experiment_id = experimentId;
    await api('/api/stop', { method: 'POST', body });
    for (const model of modelStatuses()) if (!experimentId || model.experiment.id === experimentId) state.stopped.add(model.experiment.id);
    message('Execution will stop after the current request, before any further paid request, including automatic continuations.'); await poll({ reportError: true });
  } catch (error) { showError(error.message); }
  finally { state.pending = false; renderControls(); }
}

function safeFileUrl(value) {
  if (typeof value !== 'string') return null;
  try { const url = new URL(value, location.href); return url.origin === location.origin && url.pathname.startsWith('/files/') ? url.href : null; }
  catch { return null; }
}
function conditionLabel(row) {
  if (!['A', 'B', 'C'].includes(row?.condition) || typeof row.condition_label !== 'string' || !row.condition_label.trim()) return 'Condition unknown';
  return row.condition_label;
}
function conditionCode(row) {
  return conditionLabel(row) === 'Condition unknown' ? null : row.condition;
}
function renderConditionTable() {
  const models = modelStatuses();
  const signature = JSON.stringify(models.map(model => [model.experiment.id, model.experiment.model, model.runs, model.review?.slots]));
  if (signature === state.conditionSignature) return;
  state.conditionSignature = signature;
  const fragment = document.createDocumentFragment(); let total = 0, reviewCount = 0, unknown = 0;
  for (const model of models) {
    const slots = Array.isArray(model.review?.slots) ? model.review.slots : [];
    const represented = new Set(slots.map(slot => slot.run_id).filter(Boolean));
    const rows = [...slots, ...(Array.isArray(model.runs) ? model.runs : []).filter(run => !represented.has(run.run_id))];
    for (const row of rows) {
      total++; if (row.review_id) reviewCount++;
      const label = conditionLabel(row), tr = document.createElement('tr');
      if (label === 'Condition unknown') unknown++;
      const condition = el('td', 'condition-cell'); condition.append(el('span', `condition-label${label === 'Condition unknown' ? ' unknown-condition' : ''}`, label));
      const stages = [statusName(row.execution_status || row.status), row.conversion_status ? conversionName(row.conversion_status) : null].filter(Boolean).join(' · ');
      tr.append(el('td', '', modelLabel(model.experiment)), el('td', 'review-id-cell', row.review_id || 'No review ID'), condition, el('td', '', row.repetition ?? '—'), el('td', '', row.case_id || '—'), el('td', '', stages), el('td', 'run-id-cell', row.run_id || '—'));
      fragment.append(tr);
    }
  }
  $('mapping-body').replaceChildren(fragment); $('mapping-table-wrap').hidden = !total;
  $('conditions-empty').hidden = total > 0;
  text('conditions-summary', total ? `${n(total)} entries · ${n(reviewCount)} review IDs${unknown ? ` · ${n(unknown)} conditions unknown` : ''} · Updates with status` : 'Select a batch to see its frozen conditions and existing results.');
}
function makePanels() {
  for (const [index, side] of ['left', 'right'].entries()) {
    const node = $('viewer-template').content.firstElementChild.cloneNode(true); const refs = {};
    node.querySelectorAll('[data-part]').forEach((part) => { refs[part.dataset.part] = part; part.id = `${side}-${part.dataset.part}`; });
    refs.letter.textContent = index === 0 ? 'L' : 'R'; refs.title.textContent = index === 0 ? 'Left result' : 'Right result';
    node.setAttribute('aria-labelledby', refs.title.id); refs['model-label'].htmlFor = refs.model.id; refs['slot-label'].htmlFor = refs.slot.id;
    refs.views.setAttribute('aria-label', `${refs.title.textContent}: viewing direction`); refs.viewer.setAttribute('aria-label', `${refs.title.textContent}: 3D display`);
    const panel = { side, index, refs, viewer: null, generation: 0, modelId: null, slotId: null, modelsSignature: '', slotsSignature: '', renderedKey: '' };
    refs.model.addEventListener('change', () => { panel.modelId = refs.model.value; panel.slotId = null; panel.slotsSignature = ''; panel.renderedKey = ''; updatePanel(panel); });
    refs.slot.addEventListener('change', () => { panel.slotId = refs.slot.value; panel.renderedKey = ''; updatePanel(panel); });
    refs.views.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => { panel.viewer?.setView(button.dataset.view); refs.views.querySelectorAll('[data-view]').forEach((other) => other.classList.toggle('active', other === button)); }));
    state.panels.push(panel); $('comparison-grid').append(node);
  }
}
function resetPanel(panel, dispose = false) {
  panel.generation += 1;
  if (dispose) { panel.viewer?.dispose(); panel.viewer = null; panel.refs.viewer.replaceChildren(); panel.modelId = null; panel.slotId = null; panel.modelsSignature = ''; panel.slotsSignature = ''; }
  else panel.viewer?.clear();
  panel.renderedKey = ''; panel.refs.empty.hidden = false;
  panel.refs['empty-title'].textContent = 'No 3D model yet.';
  panel.refs['empty-text'].textContent = 'Converted results will appear here.';
  panel.refs.status.textContent = 'Select a generated result.'; panel.refs.downloads.replaceChildren();
  panel.refs.condition.textContent = 'Select a result to see its condition.'; panel.refs.details.textContent = '';
  panel.refs.condition.classList.remove('unknown-condition');
  panel.refs.views.querySelectorAll('[data-view]').forEach((button) => { button.disabled = true; button.classList.remove('active'); });
}
function updatePanels() { for (const panel of state.panels) updatePanel(panel); }
function updatePanel(panel) {
  const models = modelStatuses(); const modelsSignature = JSON.stringify(models.map((model) => model.experiment));
  if (modelsSignature !== panel.modelsSignature) {
    panel.modelsSignature = modelsSignature; const fragment = document.createDocumentFragment();
    if (!models.length) { const option = document.createElement('option'); option.value = ''; option.textContent = 'No models'; fragment.append(option); }
    for (const model of models) { const option = document.createElement('option'); option.value = model.experiment.id; option.textContent = `${modelLabel(model.experiment)} · ${reasoningSummary(model.experiment)}`; fragment.append(option); }
    panel.refs.model.replaceChildren(fragment); panel.refs.model.disabled = !models.length;
    if (!models.some((model) => model.experiment.id === panel.modelId)) { panel.modelId = models[panel.index]?.experiment.id || models[0]?.experiment.id || null; panel.slotId = null; }
    panel.refs.model.value = panel.modelId || '';
  }
  const model = models.find((item) => item.experiment.id === panel.modelId); const slots = model?.review?.slots || [];
  const slotsSignature = JSON.stringify([panel.modelId, slots]);
  if (slotsSignature !== panel.slotsSignature) {
    panel.slotsSignature = slotsSignature; const fragment = document.createDocumentFragment();
    if (!slots.length) { const option = document.createElement('option'); option.value = ''; option.textContent = 'No review results'; fragment.append(option); }
    for (const slot of slots) { const option = document.createElement('option'); option.value = slot.review_id; option.textContent = `${slot.review_id} · ${conditionLabel(slot)} · ${conversionName(slot.conversion_status)}`; option.title = option.textContent; fragment.append(option); }
    panel.refs.slot.replaceChildren(fragment); panel.refs.slot.disabled = !slots.length;
    if (!slots.some((slot) => slot.review_id === panel.slotId)) panel.slotId = slots.find((slot) => safeFileUrl(slot.files?.glb))?.review_id || slots[0]?.review_id || null;
    panel.refs.slot.value = panel.slotId || '';
  }
  if (location.hash !== '#review') return;
  const slot = slots.find((item) => item.review_id === panel.slotId);
  const key = JSON.stringify([panel.modelId, slot || null]);
  if (key !== panel.renderedKey) { panel.renderedKey = key; displaySlot(panel, slot); }
}
async function displaySlot(panel, slot) {
  const generation = ++panel.generation; panel.viewer?.clear(); panel.refs.views.querySelectorAll('[data-view]').forEach((button) => button.disabled = true);
  panel.refs.downloads.replaceChildren();
  if (!slot) {
    panel.refs.empty.hidden = false; panel.refs['empty-title'].textContent = 'No 3D model yet.';
    panel.refs['empty-text'].textContent = 'Results become available after generation and shared conversion finish.';
    panel.refs.status.textContent = 'No review results are available.'; panel.refs.condition.textContent = 'Select a result to see its condition.'; panel.refs.condition.classList.remove('unknown-condition'); panel.refs.details.textContent = ''; return;
  }
  const label = conditionLabel(slot), code = conditionCode(slot);
  panel.refs.condition.textContent = `${slot.review_id} · ${label}`;
  panel.refs.condition.classList.toggle('unknown-condition', label === 'Condition unknown');
  panel.refs.details.textContent = `Case ${slot.case_id || '—'} · Repetition ${slot.repetition ?? '—'} · Run ${slot.run_id || '—'}`;
  panel.refs.status.textContent = `${code || 'Condition unknown'} · ${conversionName(slot.conversion_status)} · ${statusName(slot.execution_status)}`;
  for (const extension of ['glb', 'dxf', 'obj']) {
    const url = safeFileUrl(slot.files?.[extension]); if (!url) continue;
    const link = el('a', 'download-link', `${code ? `${code} · ` : ''}${extension.toUpperCase()} ↓`); link.href = url; link.download = `${panel.modelId}_${slot.review_id}${code ? `_${code}` : ''}.${extension}`; panel.refs.downloads.append(link);
  }
  const glb = safeFileUrl(slot.files?.glb);
  if (!glb) { panel.refs.empty.hidden = false; panel.refs['empty-title'].textContent = 'No GLB file is available to display.'; panel.refs['empty-text'].textContent = `${conversionName(slot.conversion_status)}. Failed slots remain in the list.`; return; }
  panel.refs.empty.hidden = false; panel.refs['empty-title'].textContent = 'Loading the generated model.'; panel.refs['empty-text'].textContent = 'Reading the GLB file for the selected result.';
  try {
    if (!state.viewerModule) state.viewerModule = import('./model-viewer.js').catch((error) => { state.viewerModule = null; throw error; });
    const module = await state.viewerModule; if (generation !== panel.generation) return;
    if (!panel.viewer) panel.viewer = module.createModelViewer(panel.refs.viewer);
    panel.refs.empty.hidden = true; const result = await panel.viewer.load(glb); if (generation !== panel.generation) return;
    panel.refs.views.querySelectorAll('[data-view]').forEach((button) => { button.disabled = result?.status !== 'loaded'; button.classList.toggle('active', result?.status === 'loaded' && button.dataset.view === 'iso'); });
  } catch (error) {
    if (generation !== panel.generation) return; panel.refs.empty.hidden = false; panel.refs['empty-title'].textContent = 'The viewer could not open this model.';
    panel.refs['empty-text'].textContent = error instanceof Error ? error.message : 'Download the file to inspect it in another viewer.';
  }
}

function route() {
  const review = location.hash === '#review'; $('experiment-view').hidden = review; $('review-view').hidden = !review;
  for (const name of ['experiment', 'review']) { const active = (name === 'review') === review; $(`nav-${name}`).classList.toggle('active', active); if (active) $(`nav-${name}`).setAttribute('aria-current', 'page'); else $(`nav-${name}`).removeAttribute('aria-current'); }
  text('page-title', review ? 'Compare generated results and evaluate them manually.' : 'Compare models using the same drawings.');
  text('page-description', review ? 'Check the A/B/C condition beside each review ID and compare the generated results.' : 'Choose models and reasoning settings, generate independently, and review the 3D results.');
  if (review) { updatePanels(); requestAnimationFrame(() => state.panels.forEach((panel) => panel.viewer?.resize())); }
  else if (location.hash === '#api-keys') requestAnimationFrame(() => $('api-keys').scrollIntoView({ block: 'start', behavior: 'instant' }));
  else if (location.hash === '#new-experiment') requestAnimationFrame(() => { $('new-experiment').focus({ preventScroll: true }); $('new-experiment').scrollIntoView({ block: 'start', behavior: 'instant' }); });
}

$('dismiss-error').addEventListener('click', clearError);
$('refresh-button').addEventListener('click', async () => { clearError(); try { await bootstrap(state.batchId); state.panels.forEach((panel) => panel.renderedKey = ''); await poll({ reportError: true }); } catch (error) { connected(false); showError(error.message); renderControls(); } });
$('batch-select').addEventListener('change', async (event) => { selectBatch(event.target.value, true); await poll({ reportError: true }); });
for (const id of ['repetitions', 'max-output', 'continue-limit', 'new-batch-id', 'max-new-slots']) $(id).addEventListener('input', renderControls);
$('prepare-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy() || !state.bootstrap) return; const error = prepareValidation(); if (error) { text('prepare-validation', error); return; }
  const payload = {
    batch_id: $('new-batch-id').value.trim(), repetitions: Number($('repetitions').value), max_output_tokens: Number($('max-output').value), auto_continue_limit: Number($('continue-limit').value),
    models: selectedProfiles().map((entry) => ({ provider: entry.profile.provider, model: entry.profile.model, reasoning_effort: entry.reasoning, thinking_budget_tokens: entry.profile.thinking_mode === 'manual' && entry.reasoning === 'enabled' ? Number(entry.budget) : null })),
  };
  state.pending = true; clearError(); message(''); renderControls();
  try { const result = await api('/api/prepare', { method: 'POST', body: payload }); rememberJobs(result); state.pendingBatch = result.batch_id || payload.batch_id; message('Freezing model inputs and settings in a new batch. No provider API calls are made.'); await poll({ reportError: true }); }
  catch (error) { showError(error.message); }
  finally { state.pending = false; renderControls(); }
});
$('check-button').addEventListener('click', () => action('check'));
for (const id of ['review-button', 'review-rebuild-button']) $(id).addEventListener('click', () => action('review'));
$('stop-all-button').addEventListener('click', () => stopModels());
$('run-form').addEventListener('submit', (event) => event.preventDefault());
for (const provider of ['openai', 'anthropic']) {
  $(`key-${provider}`).addEventListener('input', renderControls);
  $(`key-${provider}`).addEventListener('keydown', (event) => { if (event.key === 'Enter') event.preventDefault(); });
  $(`save-key-${provider}`).addEventListener('click', () => saveCredential(provider));
  $(`delete-key-${provider}`).addEventListener('click', () => deleteCredential(provider));
}
document.querySelectorAll('[data-show-key]').forEach((button) => button.addEventListener('click', () => {
  const input = $(`key-${button.dataset.showKey}`); const show = input.type === 'password'; input.type = show ? 'text' : 'password'; button.textContent = show ? 'Hide' : 'Show'; button.setAttribute('aria-pressed', String(show)); button.setAttribute('aria-label', `${show ? 'Hide' : 'Show'} ${providers[button.dataset.showKey]} API key`);
}));
$('run-button').addEventListener('click', async () => {
  if (busy() || $('run-button').disabled || !state.batchId) return;
  const keys = {};
  for (const provider of neededProviders()) {
    const value = $(`key-${provider}`).value.trim();
    if (value) keys[provider] = value; // Omitted providers use the encrypted local store.
  }
  const payload = { batch_id: state.batchId, keys, max_new_slots: Number($('max-new-slots').value) };
  // Clear before submission and never restore on an error. Enter in key fields does not run.
  clearKeys(); state.pending = true; clearError(); message(''); renderControls();
  try { const result = await api('/api/run', { method: 'POST', body: payload }); updateCredentials(result.credentials); rememberJobs(result); message('Concurrent paid runs have been requested. Newly entered keys are saved encrypted. API calls and automatic continuations are recorded separately.'); await poll({ reportError: true }); }
  catch (error) { showError(error.message); }
  finally { for (const provider of Object.keys(keys)) delete keys[provider]; state.pending = false; renderControls(); }
});
window.addEventListener('hashchange', route);
window.addEventListener('resize', () => state.panels.forEach((panel) => panel.viewer?.resize()));
window.addEventListener('pagehide', () => { clearKeys(); state.panels.forEach((panel) => panel.viewer?.dispose()); if (state.timer) clearInterval(state.timer); });
async function initialize() {
  makePanels(); route();
  try { await bootstrap(); await poll({ reportError: true }); }
  catch (error) { connected(false); showError(error.message); renderControls(); }
  route();
  state.timer = setInterval(() => { renderFreshness(); if (staleStatus()) { renderProgress(modelStatuses()); renderActiveBatchNotice(); } poll(); }, 2000); document.documentElement.dataset.ready = 'true';
}
initialize();
