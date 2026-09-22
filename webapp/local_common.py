from datetime import datetime, timezone
import json
from pathlib import Path
import re

VERSION = '0.2.5'
APP_ID = 'bridge-ontology-multi-model-app'
ID_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')


def now():
    return datetime.now(timezone.utc).isoformat()


class UserError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)


def safe_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise UserError('Names must start with an ASCII letter or digit and contain at most 80 ASCII letters, digits, underscores, or hyphens.')
    return value


def inside(base, relative):
    base = Path(base).resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise UserError('This file path is not allowed.', 403)
    return target


def read_json(file, default=None):
    try:
        return json.loads(Path(file).read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        return default


def write_json(file, value):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(file)


def clear_secrets(body):
    if isinstance(body, dict):
        for value in list(body.values()):
            if isinstance(value, dict):
                clear_secrets(value)
        body.clear()


def normalized_key(value):
    if not isinstance(value, str) or not value.strip():
        raise UserError('Enter the API key for the provider to run.')
    value = value.strip()
    if len(value) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise UserError('The API key contains whitespace or invalid characters.')
    return value
