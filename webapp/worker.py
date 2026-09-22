"""One isolated model process. Credential input is stdin only."""
import argparse
import importlib
import json
from pathlib import Path
import sys

def safe_error(exc):
    value = str(exc).lower()
    known = [('frozen files changed', 'Frozen inputs have changed. Prepare a new batch.'),
             ('runner code differs', 'Runner code differs from the frozen copy.'),
             ('manifest changed', 'Frozen batch or experiment settings have changed.'),
             ('runtime changed', 'The Python or conversion environment has changed.'),
             ('lock exists', 'A lock from another execution exists.'),
             ('protocol deviation', 'An experiment protocol deviation occurred, such as a returned model change.'),
             ('api key', 'Check the provider API key.'),
             ('cannot mix', 'Mock results and live experiments cannot share a folder.')]
    if isinstance(exc, FileExistsError):
        return 'This name is already in use. Enter a new batch name.'
    for token, message in known:
        if token in value:
            return message
    return 'The task could not be completed. Check model settings, inputs, and the runtime environment. Original records are preserved.'

def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)

def export_review(engine, exp):
    engine.verify_frozen(exp)
    runtime = exp / 'admin/execution_runtime.json'
    if runtime.exists() and engine.read_json(runtime) != engine.runtime_info():
        raise ValueError('Runtime changed')
    exporter = importlib.import_module('review_export')
    report = exporter.export_review(exp, exp / 'software/backend', exp / 'frozen/shared/common_output.schema.json')
    return {k: report[k] for k in ('status', 'review_slot_count', 'available_geometry_count')}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--action', choices=['prepare', 'check', 'run', 'review'], required=True)
    parser.add_argument('--batch', required=True)
    parser.add_argument('--experiment')
    parser.add_argument('--job-dir', required=True)
    args = parser.parse_args()
    job_dir = Path(args.job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    request, key, result = {}, '', None
    try:
        package = Path(args.package).resolve()
        sys.path.insert(0, str(package))
        engine = importlib.import_module('experiment')
        protocol = importlib.import_module('batch_protocol')
        engine.safe_id(args.batch)
        request = json.loads(sys.stdin.read(16385))
        if not isinstance(request, dict):
            raise ValueError('Invalid request')
        if args.action == 'prepare':
            result = protocol.prepare_batch(package, args.batch, request, job_dir)
        else:
            batch = protocol.read_batch(package, args.batch)
            if args.experiment not in [row['experiment_id'] for row in batch['models']]:
                raise ValueError('Experiment not part of batch')
            exp = engine.contained(package, 'experiments/' + engine.safe_id(args.experiment))
            from frozen_runtime import load_in_worker
            engine = load_in_worker(exp)
            if protocol.common_fingerprint(exp) != batch['common_fingerprint']:
                raise ValueError('Batch common inputs changed')
            row = next(row for row in batch['models'] if row['experiment_id'] == args.experiment)
            cfg = engine.read_json(exp / 'config.json')
            if any(cfg[k] != row[k] for k in ('provider', 'model', 'reasoning_effort', 'thinking_budget_tokens')):
                raise ValueError('Batch manifest changed')
            if args.action == 'check':
                result = {'status': 'checked', 'passed': True}
            elif args.action == 'review':
                emit({'event': 'conversion_started'})
                result = export_review(engine, exp)
            else:
                key = engine.normalized_api_key(request.pop('api_key', ''))
                limit = request.get('max_new_slots', 0)
                if type(limit) is not int or not 0 <= limit <= 100000:
                    raise ValueError('Invalid slot limit')
                result = engine.run_batch(exp, api_key=key, max_new_calls=limit or None, stop_file=job_dir / 'stop.requested')
                key = ''
                request.clear()
                try:
                    emit({'event': 'conversion_started'})
                    result['review'] = export_review(engine, exp)
                except Exception:
                    result['conversion_error'] = True
        final = {'ok': True, 'result': result}
    except Exception as exc:
        final = {'ok': False, 'error': safe_error(exc)}
    finally:
        key = ''
        if isinstance(request, dict):
            request.clear()
    (job_dir / 'result.json').write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding='utf-8')
    emit({'event': 'worker_finished', 'status': (result or {}).get('status'), 'ok': final['ok']})
    return 0 if final['ok'] else 1

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
