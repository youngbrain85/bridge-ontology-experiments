"""Concurrent model workers, isolated credentials, and read-only result summaries."""
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading

from local_common import APP_ID, VERSION, ID_PATTERN, UserError, inside, safe_id, now, read_json, write_json, normalized_key, clear_secrets
from credential_store import CredentialStore

TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cached_tokens', 'reasoning_tokens')
COUNT_KEYS = ('scheduled', 'attempted', 'finished', 'json_completed', 'failures', 'remaining', 'api_calls', 'continuations')
CONDITION_LABELS = {'A': 'A — No added knowledge', 'B': 'B — Prose knowledge', 'C': 'C — Structured ontology'}


def read_condition_document(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate condition metadata property')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('Nonfinite condition metadata value')

    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'),
                          object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (ValueError, OSError):
        return None


def condition_info(row=None):
    row = row if isinstance(row, dict) else {}
    condition = row.get('condition')
    valid = (isinstance(condition, str) and condition in CONDITION_LABELS
             and isinstance(row.get('run_id'), str) and ID_PATTERN.fullmatch(row['run_id'])
             and isinstance(row.get('case_id'), str) and ID_PATTERN.fullmatch(row['case_id'])
             and type(row.get('repetition')) is int and row['repetition'] > 0)
    if not valid:
        return {'condition': None, 'condition_label': 'Condition unavailable', 'run_id': None, 'case_id': None, 'repetition': None}
    return {'condition': condition, 'condition_label': CONDITION_LABELS[condition],
            **{key: row[key] for key in ('run_id', 'case_id', 'repetition')}}


class Application:
    def __init__(self, package, app_dir=None, jobs_dir=None, credential_store=None):
        self.package = Path(package).resolve()
        self.app_dir = Path(app_dir or Path(__file__).parent).resolve()
        if not (self.package / 'experiment.py').is_file():
            raise ValueError('Experiment package not found')
        sys.path.insert(0, str(self.package))
        self.providers = importlib.import_module('providers')
        self.protocol = importlib.import_module('batch_protocol')
        self.jobs_dir = Path(jobs_dir or self.app_dir / 'runtime/jobs').resolve()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.credential_store = credential_store if credential_store is not None else CredentialStore(self.package / 'webapp/runtime/credentials')
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs = {}
        self.preparation = None
        self.integrity = {}
        self.processes = {}
        self.shutting_down = False

    def is_busy(self):
        return any(j['status'] in ('running', 'stopping') for j in self.jobs.values())

    def exp_path(self, ident):
        exp = inside(self.package, 'experiments/' + safe_id(ident))
        if not (exp / 'manifest.json').is_file():
            raise UserError('Frozen experiment not found.', 404)
        return exp

    def read_batch(self, ident):
        safe_id(ident)
        try:
            batch = self.protocol.read_batch(self.package, ident)
        except FileNotFoundError:
            raise UserError('Prepared batch not found.', 404)
        except (ValueError, KeyError):
            raise UserError('Batch settings have changed or are corrupted. Prepare a new batch.', 409)
        return {**batch, 'path': str(inside(self.package, 'batches/' + ident + '.json'))}

    def batches(self):
        rows = []
        folder = inside(self.package, 'batches')
        for file in sorted(folder.glob('*.json')):
            if ID_PATTERN.fullmatch(file.stem):
                try:
                    rows.append(self.read_batch(file.stem))
                except UserError:
                    pass
        return rows

    def input_preview(self):
        """Expose only the configured, contained prompt resources for the next batch."""
        unavailable = {'available': False, 'input_protocol_version': None, 'engine_version': None,
                       'cases': [], 'common_instruction': '', 'technical': {},
                       'error': 'Cannot read the new batch inputs. Check the input configuration and files, then refresh.'}
        try:
            cfg = read_json(inside(self.package, 'config.json'))
            if not isinstance(cfg, dict) or not isinstance(cfg.get('cases'), list) or not cfg['cases']:
                return unavailable

            def resource(name, text=False):
                if not isinstance(name, str) or not name:
                    raise ValueError('Missing input resource')
                path = inside(self.package, name)
                # Do not expose runtime files, saved credentials, or arbitrary package files.
                path.relative_to(inside(self.package, 'resources'))
                if not path.is_file():
                    raise ValueError('Missing input resource')
                if not text:
                    return None
                if path.suffix.lower() not in ('.md', '.txt', '.json') or path.stat().st_size > 1024 * 1024:
                    raise ValueError('Invalid prompt resource')
                return path.read_text(encoding='utf-8-sig')

            cases, seen = [], set()
            for case in cfg['cases']:
                if not isinstance(case, dict):
                    raise ValueError('Invalid case')
                ident = safe_id(case.get('case_id'))
                images = case.get('images')
                if ident in seen or not isinstance(images, list) or not images:
                    raise ValueError('Invalid case inputs')
                seen.add(ident)
                scope = resource(case.get('scope'), text=True)
                for name in images:
                    resource(name)
                cases.append({'case_id': ident, 'scope': scope, 'image_count': len(images)})
            version = cfg.get('input_protocol_version')
            engine_version = cfg.get('version')
            return {'available': True,
                    'input_protocol_version': version if isinstance(version, str) and len(version) <= 120 else None,
                    'engine_version': engine_version if isinstance(engine_version, str) and len(engine_version) <= 120 else None,
                    'cases': cases, 'common_instruction': resource(cfg.get('common_instruction'), text=True),
                    'technical': {key: resource(cfg.get(key), text=True) for key in ('output_schema', 'geometry_contract')},
                    'error': None}
        except (OSError, ValueError, TypeError, UserError):
            return unavailable

    def bootstrap(self):
        batches = self.batches()
        preview = self.input_preview()
        return {'csrf_token': self.csrf_token, 'version': VERSION, 'package_root': str(self.package),
                'catalog': self.providers.catalog(), 'batches': batches, 'credentials': self.credential_store.status(),
                'default_batch': batches[-1]['id'] if batches else None, 'review_mode': 'conditions_visible',
                'new_input': preview,
                'defaults': {'repetitions': 1, 'max_output_tokens': 128000, 'auto_continue_limit': 2,
                             'case_ids': [c['case_id'] for c in preview['cases']], 'max_models': 9}}

    def review_mapping(self, exp):
        """Read the existing randomized mapping; never create, relabel, or rewrite it."""
        saved = read_condition_document(inside(exp, 'admin/review_key.json'))
        schedule = read_condition_document(inside(exp, 'schedule.json'))
        if not isinstance(saved, dict) or not isinstance(schedule, dict):
            return {}
        rows, runs = saved.get('slots'), schedule.get('runs')
        if not isinstance(rows, list) or not isinstance(runs, list) or len(rows) != len(runs):
            return {}
        expected = {}
        for run in runs:
            info = condition_info(run)
            if info['condition'] is None or info['run_id'] in expected:
                return {}
            expected[info['run_id']] = info
        mapping, seen_runs = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                return {}
            ident = row.get('review_id')
            info = condition_info(row)
            if (not isinstance(ident, str) or not re.fullmatch(r'S[0-9]{3,}', ident)
                    or ident in mapping or info['condition'] is None or info['run_id'] in seen_runs
                    or expected.get(info['run_id']) != info):
                return {}
            seen_runs.add(info['run_id'])
            mapping[ident] = {'review_id': ident, **info}
        return mapping if seen_runs == set(expected) else {}

    def review_state(self, ident, mapping=None):
        exp = self.exp_path(ident)
        mapping = self.review_mapping(exp) if mapping is None else mapping
        review = inside(exp, 'review')
        slots = []
        if review.is_dir():
            for folder in sorted(review.iterdir()):
                if not folder.is_dir() or not re.fullmatch(r'S[0-9]{3,}', folder.name):
                    continue
                summary = read_json(inside(exp, 'review/' + folder.name + '/run_summary.json'), {})
                files = {ext: '/files/' + ident + '/' + folder.name + '/model.' + ext
                         for ext in ('glb', 'dxf', 'obj') if inside(exp, 'review/' + folder.name + '/model.' + ext).is_file()}
                slots.append({'review_id': folder.name, 'execution_status': summary.get('execution_status', 'unknown'),
                              'conversion_status': summary.get('conversion_status', 'not_attempted'), 'files': files,
                              **condition_info(mapping.get(folder.name))})
        return {'available': bool(slots), 'slots': slots}

    def model_status(self, ident):
        exp = self.exp_path(ident)
        cfg = read_json(exp / 'config.json')
        schedule = read_condition_document(exp / 'schedule.json')['runs']
        mapping = self.review_mapping(exp)
        review_ids = {row['run_id']: ident for ident, row in mapping.items()}
        job = next((j for j in reversed(list(self.jobs.values())) if j['experiment_id'] == ident), None)
        busy = bool(job and job['status'] in ('running', 'stopping'))
        locked = (exp / 'run.lock').exists()
        progress = {k: 0 for k in COUNT_KEYS}
        progress.update({k: None for k in TOKEN_KEYS})
        progress.update({'scheduled': len(schedule), 'active_run': None})
        runs = []
        for row in schedule:
            folder = inside(exp, 'runs/' + safe_id(row['run_id']))
            attempted = (folder / 'attempt.json').exists()
            result = read_json(folder / 'result.json')
            turns = list((folder / 'turns').glob('*/attempt.json'))
            calls = len(turns) if turns else (result or {}).get('api_turns', int(attempted))
            continuations = max(0, calls - 1)
            progress['attempted'] += int(attempted)
            progress['api_calls'] += calls
            progress['continuations'] += continuations
            if result:
                status = result.get('status', 'unknown')
                progress['finished'] += 1
                progress['json_completed'] += int(status == 'completed')
                progress['failures'] += int(status != 'completed')
                usage = result.get('usage') or {}
                token_values = {'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
                                'cached_tokens': (usage.get('input_tokens_details') or {}).get('cached_tokens'),
                                'reasoning_tokens': (usage.get('output_tokens_details') or {}).get('reasoning_tokens')}
                for key, value in token_values.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        progress[key] = (progress[key] or 0) + value
            elif attempted:
                status = 'running' if busy or locked else 'interrupted_outcome_unknown'
                if status == 'running':
                    progress['active_run'] = row['run_id']
            else:
                status = 'pending'
            runs.append({**condition_info(row), 'run_id': row['run_id'], 'review_id': review_ids.get(row['run_id']),
                         'status': status, 'api_calls': calls, 'continuations': continuations,
                         'http_status': (result or {}).get('http_status'), 'elapsed_seconds': (result or {}).get('elapsed_seconds')})
        progress['remaining'] = len(schedule) - progress['attempted']
        return {'experiment': {'id': ident, 'provider': cfg['provider'], 'model': cfg['model'],
                              'reasoning_effort': cfg['reasoning_effort'], 'thinking_budget_tokens': cfg.get('thinking_budget_tokens'),
                              'repetitions': cfg['repetitions'], 'max_output_tokens': cfg['max_output_tokens'],
                              'auto_continue_limit': cfg['auto_continue_limit'], 'total_calls': len(schedule),
                              'case_ids': [c['case_id'] for c in cfg['cases']], 'path': str(exp)},
                'job': dict(job) if job else None, 'busy': busy, 'locked': locked,
                'integrity': self.integrity.get(ident), 'progress': progress, 'runs': runs,
                'review': self.review_state(ident, mapping), 'protocol_deviation': (exp / 'admin/protocol_violation.json').exists()}

    def status(self, ident=None):
        with self.lock:
            batch = self.read_batch(ident) if ident else None
            models = [self.model_status(row['experiment_id']) for row in batch['models']] if batch else []
            totals = {key: sum(m['progress'][key] for m in models) for key in COUNT_KEYS}
            for key in TOKEN_KEYS:
                values = [m['progress'][key] for m in models if m['progress'][key] is not None]
                totals[key] = sum(values) if values else None
            return {'batch': batch, 'review_mode': 'conditions_visible', 'preparation': dict(self.preparation) if self.preparation else None,
                    'busy': self.is_busy(), 'models': models, 'totals': totals,
                    'jobs': [dict(j) for j in self.jobs.values() if not ident or j['batch_id'] == ident]}

    def require_idle(self):
        if self.shutting_down:
            raise UserError('The server is shutting down.', 409)
        if self.is_busy():
            raise UserError('Wait for the current task to finish before starting another.', 409)

    def save_credentials(self, body):
        with self.lock:
            self.require_idle()
            keys = body.get('keys')
            if not isinstance(keys, dict) or not keys or set(keys) - {'openai', 'anthropic'}:
                raise UserError('Enter the OpenAI or Claude API key to save.')
            validated = {}
            try:
                validated = {provider: normalized_key(value) for provider, value in keys.items()}
                self.credential_store.save_many(validated)
            finally:
                validated.clear()
            return {'credentials': self.credential_store.status()}

    def delete_credentials(self, body):
        with self.lock:
            self.require_idle()
            provider = body.get('provider')
            if not isinstance(provider, str) or provider not in ('openai', 'anthropic'):
                raise UserError('Select the provider whose saved API key you want to delete.')
            self.credential_store.delete(provider)
            return {'credentials': self.credential_store.status()}

    def prepare(self, body):
        with self.lock:
            self.require_idle()
            ident = safe_id(body.get('batch_id'))
            if len(ident) > 70:
                raise UserError('Batch names must contain no more than 70 characters.')
            if any(inside(self.package, 'batches/' + ident + ext).exists() for ext in ('.json', '.reserved')):
                raise UserError('This batch name is already in use. Enter a new name.', 409)
            models = body.get('models')
            if not isinstance(models, list) or not 1 <= len(models) <= 9:
                raise UserError('Select between 1 and 9 models.')
            for key, minimum, maximum in (('repetitions', 1, 100), ('max_output_tokens', 1, 128000), ('auto_continue_limit', 0, 5)):
                if type(body.get(key)) is not int or not minimum <= body[key] <= maximum:
                    raise UserError('Check the repetition count, maximum output, and automatic follow-up limit.')
            selections, seen = [], set()
            for row in models:
                if not isinstance(row, dict) or set(row) != {'provider', 'model', 'reasoning_effort', 'thinking_budget_tokens'}:
                    raise UserError('Invalid model selection format.')
                marker = (row['provider'], row['model'])
                if not all(isinstance(v, str) for v in marker) or marker in seen:
                    raise UserError('The same model cannot be selected more than once.')
                seen.add(marker)
                try:
                    self.providers.validate_model_settings({**row, 'max_output_tokens': body['max_output_tokens']})
                except (ValueError, TypeError, KeyError):
                    raise UserError('Check the selected model reasoning option, output limit, and thinking token budget.')
                selections.append(dict(row))
            request = {k: body[k] for k in ('repetitions', 'max_output_tokens', 'auto_continue_limit')}
            request['models'] = selections
            reservation = inside(self.package, 'batches/' + ident + '.reserved')
            reservation.parent.mkdir(parents=True, exist_ok=True)
            with reservation.open('x', encoding='utf-8') as file:
                file.write(now())
            job = self.launch('prepare', ident, None, request)
            self.preparation = job
            return {'job_id': job['id'], 'batch_id': ident}

    def operate(self, action, body):
        with self.lock:
            self.require_idle()
            ident = safe_id(body.get('batch_id'))
            batch = self.read_batch(ident)
            models = [self.model_status(row['experiment_id']) for row in batch['models']]
            if action == 'run':
                models = [m for m in models if m['progress']['remaining'] > 0 and not m['protocol_deviation']]
            elif action == 'review':
                models = [m for m in models if m['progress']['attempted'] > 0]
            if not models:
                raise UserError('No model slots are available for this action.', 409)
            if any(m['locked'] for m in models):
                raise UserError('An execution lock exists. Check for another running process.', 409)
            requests = []
            if action == 'run':
                if any(not (m['integrity'] or {}).get('passed') for m in models):
                    raise UserError('All models must pass the input check first.', 409)
                keys = body.get('keys')
                if not isinstance(keys, dict) or set(keys) - {'openai', 'anthropic'}:
                    raise UserError('Check the OpenAI and Claude API key fields.')
                limit = body.get('max_new_slots', 0)
                if type(limit) is not int or not 0 <= limit <= 100000:
                    raise UserError('The new-slot limit per model must be a nonnegative integer.')
                # Resolve and validate all required keys before saving or launching any model.
                credentials, supplied = {}, {}
                try:
                    for provider in sorted({m['experiment']['provider'] for m in models}):
                        if provider in keys:
                            supplied[provider] = normalized_key(keys[provider])
                            credentials[provider] = supplied[provider]
                        else:
                            stored = self.credential_store.get(provider)
                            if stored is None:
                                label = 'OpenAI' if provider == 'openai' else 'Claude · Anthropic'
                                raise UserError(label + ' API key must be entered and saved once.')
                            credentials[provider] = normalized_key(stored)
                            stored = None
                    if supplied:
                        self.credential_store.save_many(supplied)
                    for model in models:
                        requests.append({'api_key': credentials[model['experiment']['provider']], 'max_new_slots': limit})
                finally:
                    credentials.clear()
                    supplied.clear()
            else:
                requests = [{} for _ in models]
            jobs = [self.launch(action, ident, model['experiment']['id'], request)
                    for model, request in zip(models, requests)]
            result = {'job_ids': [j['id'] for j in jobs], 'batch_id': ident}
            if action == 'run':
                result['credentials'] = self.credential_store.status()
            return result

    def launch(self, action, batch_id, experiment_id, request):
        job_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + secrets.token_hex(5)
        directory = inside(self.jobs_dir, job_id)
        directory.mkdir()
        sources = ['server.py', 'app_service.py', 'local_common.py', 'credential_store.py', 'worker.py', 'static/index.html', 'static/app.css', 'static/app.js', 'static/model-viewer.js']
        job = {'id': job_id, 'action': action, 'batch_id': batch_id, 'experiment_id': experiment_id, 'status': 'running',
               'started_utc': now(), 'finished_utc': None, 'message': 'Task started.', 'events': [], 'app_version': VERSION,
               'app_code_sha256': {name: hashlib.sha256((self.app_dir / name).read_bytes()).hexdigest() for name in sources if (self.app_dir / name).is_file()},
               'batch_protocol_sha256': hashlib.sha256((self.package / 'batch_protocol.py').read_bytes()).hexdigest()}
        self.jobs[job_id] = job
        write_json(directory / 'job.json', job)
        threading.Thread(target=self._run_worker, args=(job, request, directory), daemon=True).start()
        return job

    def _run_worker(self, job, request, directory):
        process = None
        try:
            env = {k: v for k, v in os.environ.items() if not any(token in k.upper() for token in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'))}
            env.update({'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
            args = [sys.executable, '-B', '-X', 'utf8', str(self.app_dir / 'worker.py'), '--package', str(self.package),
                    '--action', job['action'], '--batch', job['batch_id'], '--job-dir', str(directory)]
            if job['experiment_id']:
                args += ['--experiment', job['experiment_id']]
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with self.lock:
                self.processes[job['id']] = process
            process.stdin.write(json.dumps(request, ensure_ascii=False).encode('utf-8'))
            process.stdin.close()
            clear_secrets(request)
            for line in process.stdout:
                try:
                    event = json.loads(line.decode('utf-8'))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get('event') not in ('call_started', 'call_finished', 'turn_started', 'turn_finished', 'auto_continuation', 'conversion_started', 'worker_finished'):
                    continue
                safe_event = {key: event[key] for key in ('event', 'run_id', 'status', 'turn') if key in event and (event[key] is None or isinstance(event[key], (str, int)))}
                with self.lock:
                    job['events'] = (job['events'] + [safe_event])[-100:]
                    if event['event'] == 'conversion_started':
                        job['message'] = 'Processing generated results with the shared converter.'
                    elif event['event'] == 'auto_continuation':
                        job['message'] = 'Sending the fixed clarification reply to continue with the model recommendation.'
                    elif event['event'] in ('call_started', 'turn_started'):
                        job['message'] = ('Sending the fixed clarification reply to continue with the model recommendation.'
                                          if event.get('turn', 0) else 'Waiting for this model independent API response.')
            code = process.wait()
            final = read_json(directory / 'result.json', {'ok': False, 'error': 'The worker process exited. Original execution records are preserved.'})
            success = code == 0 and final.get('ok') is True
            result = final.get('result') or {}
            phase = result.get('status', '')
            paused = phase.startswith('paused') or phase.startswith('stopped')
            with self.lock:
                job['status'] = 'paused' if success and paused else 'completed' if success else 'failed'
                job['finished_utc'] = now()
                job['message'] = ('Stopped with remaining slots preserved. Check the generated review files.' if paused else 'Task completed.') if success else final.get('error', 'The task could not be completed.')
                if result.get('conversion_error'):
                    job['message'] += ' Some 3D conversions could not be completed. Use the conversion button to check their status.'
                if job['action'] == 'check':
                    self.integrity[job['experiment_id']] = {'passed': success, 'checked_utc': now(), 'message': job['message']}
                write_json(directory / 'job.json', job)
        except Exception:
            clear_secrets(request)
            if process is not None and process.poll() is None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                process.wait()
            with self.lock:
                job['status'] = 'failed'
                job['message'] = 'Could not start the worker process. Check Python and the package location.'
                job['finished_utc'] = now()
                write_json(directory / 'job.json', job)
                if job['action'] == 'check':
                    self.integrity[job['experiment_id']] = {'passed': False, 'checked_utc': now(), 'message': job['message']}
        finally:
            clear_secrets(request)
            with self.lock:
                self.processes.pop(job['id'], None)

    def stop(self, body):
        with self.lock:
            ident = safe_id(body.get('batch_id'))
            self.read_batch(ident)
            exp_id = safe_id(body['experiment_id']) if 'experiment_id' in body else None
            matched = [j for j in self.jobs.values() if j['batch_id'] == ident and j['action'] == 'run'
                       and j['status'] in ('running', 'stopping') and (exp_id is None or j['experiment_id'] == exp_id)]
            if not matched:
                raise UserError('No API generation task is available to stop.', 409)
            for job in matched:
                inside(self.jobs_dir, job['id'] + '/stop.requested').write_text(now(), encoding='ascii')
                job['status'] = 'stopping'
                job['message'] = 'The current response will be preserved, then execution will stop before the next API request.'
            return {'status': 'stopping'}

    def mapping(self, ident):
        batch = self.read_batch(ident)
        slots = []
        for model in batch['models']:
            exp = self.exp_path(model['experiment_id'])
            for row in self.review_mapping(exp).values():
                slots.append({**{k: model[k] for k in ('experiment_id', 'model', 'provider')},
                              **{k: row.get(k) for k in ('review_id', 'condition', 'condition_label', 'repetition', 'case_id', 'run_id')}})
        if not slots:
            raise UserError('Cannot verify the mapping between existing review IDs and execution conditions.', 404)
        return {'slots': slots}
