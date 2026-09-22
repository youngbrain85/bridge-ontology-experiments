"""Loopback-only web application for parallel independent model experiments."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import urllib.parse
import webbrowser
from app_service import Application
from local_common import APP_ID, VERSION, UserError, safe_id, inside, read_json, write_json, now, clear_secrets

STATIC_FILES = {'index.html', 'app.css', 'app.js', 'model-viewer.js', 'THIRD_PARTY_NOTICES.txt'}

def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON property')
        result[key] = value
    return result

def invalid_constant(value):
    raise ValueError('Nonfinite JSON value')

def create_server(app, port=0):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            pass

        def send_body(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def guard_host(self):
            hosts = self.headers.get_all('Host') or []
            allowed = {'127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)}
            if len(hosts) != 1 or hosts[0] not in allowed:
                raise UserError('This host is not allowed.', 403)

        def parsed(self):
            url = urllib.parse.urlsplit(self.path)
            if url.scheme or url.netloc:
                raise UserError('Only relative URLs are allowed.', 400)
            return urllib.parse.unquote(url.path), urllib.parse.parse_qs(url.query, keep_blank_values=True)

        def query_id(self, query, key, optional=False):
            if optional and not query:
                return None
            if set(query) != {key} or len(query[key]) != 1:
                raise UserError('Select a valid batch or experiment.')
            return safe_id(query[key][0])

        def do_GET(self):
            try:
                self.guard_host()
                path, query = self.parsed()
                if path == '/api/health':
                    return self.send_body(200, {'status': 'ok', 'app': APP_ID, 'version': VERSION, 'package_root': str(app.package)})
                if path == '/api/bootstrap':
                    return self.send_body(200, app.bootstrap())
                if path == '/api/status':
                    return self.send_body(200, app.status(self.query_id(query, 'batch_id', optional=True)))
                if path == '/api/source':
                    exp = app.exp_path(self.query_id(query, 'experiment_id'))
                    cfg = read_json(exp / 'config.json')
                    file = inside(exp, cfg['cases'][0]['images'][0])
                    return self.send_body(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'image/png')
                match = re.fullmatch(r'/files/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/(S[0-9]{3,})/model\.(glb|dxf|obj)', path)
                if match:
                    ident, slot, ext = match.groups()
                    exp = app.exp_path(ident)
                    file = inside(exp, 'review/' + slot + '/model.' + ext)
                    if not file.is_file():
                        raise UserError('Review file not found.', 404)
                    return self.send_body(200, file.read_bytes(), {'glb': 'model/gltf-binary', 'dxf': 'application/dxf', 'obj': 'text/plain; charset=utf-8'}[ext])
                name = 'index.html' if path == '/' else path.lstrip('/')
                if name not in STATIC_FILES:
                    raise UserError('Page not found.', 404)
                file = inside(app.app_dir, 'static/' + name)
                if not file.is_file():
                    raise UserError('Application file not found.', 404)
                content_type = {'js': 'text/javascript; charset=utf-8', 'html': 'text/html; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain; charset=utf-8')
                return self.send_body(200, file.read_bytes(), content_type)
            except UserError as exc:
                self.send_body(exc.status, {'error': exc.message})
            except Exception:
                self.send_body(500, {'error': 'Cannot read the data. Check the batch and experiment files.'})

        def do_POST(self):
            body = {}
            try:
                self.guard_host()
                origin = 'http://' + self.headers['Host']
                if self.headers.get_all('Origin') != [origin] or self.headers.get_all('X-CSRF-Token') != [app.csrf_token]:
                    raise UserError('Refresh the application page and try again.', 403)
                if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/json':
                    raise UserError('Only JSON requests are allowed.', 415)
                lengths = self.headers.get_all('Content-Length') or []
                if len(lengths) != 1:
                    raise UserError('Invalid request length.')
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise UserError('Invalid request length.')
                if not 0 < length <= 16384:
                    raise UserError('The request exceeds the allowed size.', 413)
                try:
                    body = json.loads(self.rfile.read(length), object_pairs_hook=strict_object, parse_constant=invalid_constant)
                except (ValueError, UnicodeError):
                    raise UserError('A valid JSON request is required.')
                if not isinstance(body, dict):
                    raise UserError('A JSON object is required.')
                path, query = self.parsed()
                if query:
                    raise UserError('Query parameters are not allowed for POST requests.')
                allowed = {'/api/prepare': {'batch_id', 'models', 'repetitions', 'max_output_tokens', 'auto_continue_limit'},
                           '/api/check': {'batch_id'}, '/api/run': {'batch_id', 'keys', 'max_new_slots'}, '/api/review': {'batch_id'},
                           '/api/stop': {'batch_id', 'experiment_id'}, '/api/mapping': {'batch_id'}, '/api/shutdown': set(),
                           '/api/credentials/save': {'keys'}, '/api/credentials/delete': {'provider'}}
                if path not in allowed:
                    raise UserError('Task not found.', 404)
                if set(body) - allowed[path]:
                    raise UserError('The request contains unknown fields.')
                if path == '/api/prepare':
                    return self.send_body(202, app.prepare(body))
                if path == '/api/credentials/save':
                    return self.send_body(200, app.save_credentials(body))
                if path == '/api/credentials/delete':
                    return self.send_body(200, app.delete_credentials(body))
                if path in ('/api/check', '/api/run', '/api/review'):
                    return self.send_body(202, app.operate(path.split('/')[-1], body))
                if path == '/api/stop':
                    return self.send_body(200, app.stop(body))
                if path == '/api/mapping':
                    return self.send_body(200, app.mapping(safe_id(body.get('batch_id'))))
                with app.lock:
                    app.require_idle()
                    app.shutting_down = True
                self.send_body(200, {'status': 'shutting_down'})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            except UserError as exc:
                self.send_body(exc.status, {'error': exc.message})
            except Exception:
                self.send_body(500, {'error': 'Could not start the task. Refresh the application.'})
            finally:
                clear_secrets(body)

        def do_OPTIONS(self):
            self.send_body(403, {'error': 'Cross-origin requests are not allowed.'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', default=str(Path(__file__).parent.parent))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    app = Application(args.package)
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = create_server(app, port)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit('No local port available.')
    url = 'http://127.0.0.1:' + str(server.server_port)
    write_json(app.app_dir / 'runtime/server.json', {'url': url, 'pid': os.getpid(), 'package': str(app.package), 'version': VERSION, 'started_utc': now()})
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
