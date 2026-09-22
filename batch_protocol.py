"""Freeze independent model experiments with a common, auditable batch definition."""
import copy
from pathlib import Path

import experiment as engine
import providers
from frozen_runtime import check_in_process


def batch_path(package, ident):
    return engine.contained(package, 'batches/' + engine.safe_id(ident) + '.json')


def common_fingerprint(exp):
    exp = Path(exp)
    cfg = engine.read_json(exp / 'config.json')
    common = {k: cfg[k] for k in ('repetitions', 'max_output_tokens', 'randomization_seed',
                                   'timeout_seconds', 'auto_continue_limit', 'study_role')}
    if 'input_protocol_version' in cfg:
        common['input_protocol_version'] = cfg['input_protocol_version']
    common['files'] = {p.relative_to(exp / 'frozen').as_posix(): engine.sha(p)
                       for p in sorted((exp / 'frozen').rglob('*')) if p.is_file()}
    return engine.digest(engine.dumps(common).encode('utf-8'))


def prepare_batch(package, ident, request, job_dir):
    package, job_dir = Path(package), Path(job_dir)
    destination = batch_path(package, ident)
    if destination.exists():
        raise FileExistsError('Batch already exists')
    base = engine.read_json(package / 'config.json')
    for key in ('knowledge_source', 'common_instruction', 'output_schema', 'geometry_contract', 'backend_dir'):
        base[key] = str(engine.resolve_input(package, base[key]))
    for case in base['cases']:
        for key in ('scope', 'text', 'provenance'):
            case[key] = str(engine.resolve_input(package, case[key]))
        case['images'] = [str(engine.resolve_input(package, name)) for name in case['images']]
    for key in ('repetitions', 'max_output_tokens', 'auto_continue_limit'):
        base[key] = request[key]
    planned = []
    for index, selection in enumerate(request['models'], 1):
        cfg = copy.deepcopy(base)
        cfg.update({k: selection.get(k) for k in ('provider', 'model', 'reasoning_effort', 'thinking_budget_tokens')})
        cfg['version'] = engine.VERSION
        cfg['model_version_note'] = 'Documentation-listed model; account access unverified. Returned IDs are recorded; aliases do not prove immutable weights.'
        engine.validate_config(cfg)
        exp_id = engine.safe_id(ident + '_m%02d' % index)
        exp = engine.contained(package, 'experiments/' + exp_id)
        if exp.exists():
            raise FileExistsError('Experiment already exists')
        planned.append((exp_id, exp, cfg))
    fingerprints, rows = set(), []
    for exp_id, exp, cfg in planned:
        config_path = job_dir / (exp_id + '_config.json')
        engine.write_json(config_path, cfg)
        engine.prepare(config_path, exp)
        engine.verify_frozen(exp)
        fingerprints.add(common_fingerprint(exp))
        rows.append({'experiment_id': exp_id, **{k: cfg[k] for k in ('provider', 'model', 'reasoning_effort', 'thinking_budget_tokens')}})
    if len(fingerprints) != 1:
        raise ValueError('Models do not share identical frozen inputs and common settings')
    count = len(base['cases']) * 3 * base['repetitions'] * len(rows)
    batch = {'version': '0.2.0', 'engine_version': engine.VERSION, 'id': ident, 'created_utc': engine.now(),
             'input_protocol_version': base.get('input_protocol_version', engine.VERSION),
             'study_role': base['study_role'], 'case_ids': [c['case_id'] for c in base['cases']],
             'repetitions': base['repetitions'], 'max_output_tokens': base['max_output_tokens'],
             'auto_continue_limit': base['auto_continue_limit'], 'model_count': len(rows), 'models': rows,
             'total_slots': count, 'max_api_calls': count * (1 + base['auto_continue_limit']),
             'common_fingerprint': next(iter(fingerprints)),
             'comparison_note': 'Provider/model/reasoning differ across model experiments; A/B/C differ only in additional knowledge. Equal effort labels are not equal compute.',
             'clarification_note': 'Same continuation ceiling, response-dependent realized calls. No history crosses slots.'}
    engine.write_json(destination, batch)
    destination.with_suffix('.sha256').write_text(engine.sha(destination) + '\n', encoding='ascii')
    verify_batch(package, ident)
    return {'status': 'prepared', 'batch_id': ident, 'model_count': len(rows), 'total_slots': count}


def read_batch(package, ident):
    file = batch_path(package, ident)
    if engine.sha(file) != file.with_suffix('.sha256').read_text(encoding='ascii').strip():
        raise ValueError('Batch manifest changed')
    batch = engine.read_json(file)
    if batch['id'] != ident or not 1 <= len(batch['models']) <= 9:
        raise ValueError('Invalid batch manifest')
    return batch


def verify_batch(package, ident):
    batch = read_batch(package, ident)
    for row in batch['models']:
        exp = engine.contained(package, 'experiments/' + engine.safe_id(row['experiment_id']))
        cfg = engine.read_json(exp / 'config.json')
        if any(cfg[k] != row[k] for k in ('provider', 'model', 'reasoning_effort', 'thinking_budget_tokens')):
            raise ValueError('Batch model settings changed')
        recorded = engine.read_json(exp / 'manifest.json')['files']
        if all(engine.sha(Path(engine.__file__).parent / name) == recorded.get('software/' + name)
               for name in engine.CODE_FILES):
            engine.verify_frozen(exp)
        else:
            check_in_process(exp)
        if common_fingerprint(exp) != batch['common_fingerprint']:
            raise ValueError('Batch common inputs changed')
    return {'passed': True, 'models': len(batch['models'])}
