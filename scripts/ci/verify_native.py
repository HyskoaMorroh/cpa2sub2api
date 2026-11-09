"""Verify the built consumer and exercise only synthetic loopback sources."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def digest(path, canonical=False):
    data = Path(path).read_bytes()
    if canonical:
        data = data.replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def verify_provenance(binary, provenance_path, repo):
    sys.path.insert(0, str(repo))
    from relay.server import verify_consumer_guard
    feature = verify_consumer_guard(binary, provenance_path)
    manifest = json.loads((repo / 'third_party/patches/consumer_patch_manifest.json').read_text(encoding='utf8'))
    evidence = json.loads(provenance_path.read_text(encoding='utf8'))
    for key in ('upstream_repo', 'upstream_version', 'upstream_commit', 'feature_id',
                'automatic_output_cap', 'automatic_output_header', 'builder_image', 'original_source_hashes'):
        if evidence.get(key) != manifest[key]:
            raise ValueError('consumer manifest mismatch: ' + key)
    for name, canonical in (('build_input_hashes', False), ('build_input_canonical_lf_hashes', True)):
        expected = {name: digest(repo / name, canonical) for name in manifest['build_inputs']}
        if evidence.get(name) != expected:
            raise ValueError('consumer build inputs do not match this checkout')
    tree = evidence.get('source_tree_evidence') or {}
    if (tree.get('modified') != sorted(manifest['source_delta']['modified'])
            or tree.get('added') != sorted(manifest['source_delta']['added'])
            or type(tree.get('tracked_head_files')) is not int or tree['tracked_head_files'] <= 0
            or set(evidence.get('patched_source_hashes') or {})
            != set(manifest['source_delta']['modified'] + manifest['source_delta']['added'])):
        raise ValueError('consumer source scope mismatch')
    recipe = evidence.get('build_recipe') or {}
    if (evidence.get('cgo_enabled') != '1' or 'go1.26.10 ' not in evidence.get('go_version', '')
            or any(recipe.get(k) != v for k, v in {'cgo_enabled': '1', 'goos': 'linux',
                                                 'trimpath': True, 'buildvcs': False}.items())):
        raise ValueError('consumer build recipe mismatch')
    expected_arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if not expected_arch or evidence.get('goarch') != expected_arch:
        raise ValueError('consumer architecture mismatch')
    return dict(feature, architecture=expected_arch, build_input_count=len(manifest['build_inputs']))


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def source_model(event, protocol):
    if not protocol.startswith('gemini'):
        return event['body'].get('model')
    match = re.fullmatch(r'.*/models/([^/:]+):(?:generateContent|streamGenerateContent)', urlsplit(event['path']).path)
    return unquote(match.group(1)) if match else None


def verify_wire(binary):
    events = []
    class Origin(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            private = [k for k in self.headers if k.lower() in {
                'x-cpa2sub2api-auto-output-cap', 'idempotency-key', 'x-idempotency-key',
                'x-relay-automatic-probe', 'x-relay-exact-key', 'x-relay-generation'}]
            events.append({'path': self.path, 'body': body, 'private_headers': private})
            if (body.get('metadata') or {}).get('ci_redirect'):
                self.send_response(307)
                self.send_header('Location', 'http://127.0.0.1:%d/redirect-target' % self.server.server_port)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            data = b'{"error":{"message":"ci synthetic unavailable","type":"server_error","code":"ci_source_503"}}'
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    threading.Thread(target=source.serve_forever, daemon=True).start()
    native_port = free_port()
    base = 'http://127.0.0.1:%d' % source.server_port
    prefixes = ['d' + letter * 40 for letter in '1234']
    models = ['ci-chat-highest', 'ci-claude-highest', 'ci-codex-highest', 'ci-gemini-highest']
    groups = {}
    for protocol, prefix, model in zip(('openai-compatibility', 'claude', 'codex', 'gemini'), prefixes, models):
        groups[protocol] = [{'name': 'ci-' + protocol, 'prefix': prefix,
            'base-url': base + ('/v1' if protocol == 'openai-compatibility' else ''),
            'request-retry': 0, 'disable-cooling': True, 'proxy-url': 'direct',
            'models': [{'name': model, 'alias': model}], 'keys': [{'api-key': 'ci-synthetic-only',
                'request-retry': 0, 'websockets': False}]}]
    parameters = [({'max_tokens': 4096, 'max_completion_tokens': 4096}, 'openai'),
                  ({'max_tokens': 4096}, 'claude'), ({'max_output_tokens': 4096}, 'codex'),
                  ({'generationConfig.maxOutputTokens': 4096}, 'gemini')]
    config = {'server': {'host': '127.0.0.1', 'port': native_port, 'discovery': {'enabled': False},
                        'response-compression': {'enabled': False}},
        'access': {'api-keys': ['ci-local-client']}, 'oauth': {'auth-dir': './empty-auth'},
        'management': {'allow-remote': False, 'secret-key': '', 'disable-control-panel': True},
        'routing': {'strategy': 'round-robin', 'force-model-prefix': True},
        'retry': {'request-retry': 0, 'max-retry-credentials': 1, 'max-retry-interval': 0},
        'requests': {'payload': {'override': [{'models': [{'name': model, 'protocol': protocol}],
             'params': values} for model, (values, protocol) in zip(models, parameters)]}},
        'disable-image-generation': True, 'quota-exceeded': {'switch-project': False, 'switch-preview-model': False},
        'api-keys': groups}
    fixtures = [
        ('chat', '/v1/chat/completions', {'model': prefixes[0] + '/' + models[0],
         'messages': [{'role': 'user', 'content': 'Reply OK'}], 'max_tokens': 16, 'stream': False}, 'max_tokens'),
        ('claude', '/v1/messages', {'model': prefixes[1] + '/' + models[1],
         'messages': [{'role': 'user', 'content': 'Reply OK'}], 'max_tokens': 16, 'stream': False}, 'max_tokens'),
        ('codex', '/v1/responses', {'model': prefixes[2] + '/' + models[2],
         'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Reply OK'}]}],
         'max_output_tokens': 16, 'stream': False}, 'max_output_tokens'),
        ('gemini', '/v1beta/models/' + prefixes[3] + '/' + models[3] + ':generateContent',
         {'contents': [{'role': 'user', 'parts': [{'text': 'Reply OK'}]}],
          'generationConfig': {'maxOutputTokens': 16}}, 'maxOutputTokens')]
    for label, modality in (('gemini_image', 'IMAGE'), ('gemini_audio', 'AUDIO'), ('gemini_manual_audio', 'AUDIO')):
        fixtures.append((label, fixtures[3][1], {'contents': [{'role': 'user', 'parts': [{'text': 'Reply OK'}]}],
            'generationConfig': {'maxOutputTokens': 16, 'responseModalities': [modality]}}, 'maxOutputTokens'))
    fixtures.append(('chat_manual_redirect', fixtures[0][1], {'model': prefixes[0] + '/' + models[0],
        'messages': [{'role': 'user', 'content': 'Reply OK'}], 'metadata': {'ci_redirect': True},
        'max_tokens': 8192, 'stream': False}, 'max_tokens'))
    opener = build_opener(ProxyHandler({}))
    results = []
    child = None
    try:
        with tempfile.TemporaryDirectory(prefix='ci-native-') as workspace, tempfile.TemporaryFile() as log:
            path = Path(workspace) / 'config.yaml'
            path.write_text(json.dumps(config), encoding='utf8')
            env = {key: value for key, value in os.environ.items() if key in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR')}
            env.update(CPA2SUB2API_DIAGNOSTIC_PREFIXES=json.dumps(prefixes), NO_PROXY='127.0.0.1,localhost')
            child = subprocess.Popen([str(binary), '-config', str(path)], cwd=workspace, env=env, stdout=log, stderr=log)
            ready = False
            for _ in range(100):
                if child.poll() is not None:
                    break
                try:
                    with opener.open(Request('http://127.0.0.1:%d/v1/models' % native_port,
                                              headers={'Authorization': 'Bearer ci-local-client'}), timeout=.2) as response:
                        ready = response.status == 200
                        response.read()
                    if ready:
                        break
                except (HTTPError, URLError, TimeoutError):
                    time.sleep(.05)
            if not ready:
                raise ValueError('native consumer did not become ready')
            for label, request_path, payload, field in fixtures:
                before = len(events)
                headers = {'Authorization': 'Bearer ci-local-client', 'Content-Type': 'application/json'}
                if label not in ('gemini_manual_audio', 'chat_manual_redirect'):
                    headers['X-CPA2SUB2API-Auto-Output-Cap'] = '16'
                request = Request('http://127.0.0.1:%d%s' % (native_port, request_path),
                                  data=json.dumps(payload).encode(), method='POST', headers=headers)
                status, error = None, ''
                try:
                    with opener.open(request, timeout=10) as response:
                        status = response.status
                        response.read()
                except HTTPError as response:
                    status = response.code
                    data = response.read()
                    for code in ('automatic_probe_unsafe_profile', 'automatic_probe_unsafe_payload', 'ci_source_503'):
                        if code.encode() in data:
                            error = code
                observed = events[before:]
                caps = [(e['body'].get('generationConfig') or {}).get(field) if label.startswith('gemini')
                        else e['body'].get(field) for e in observed]
                wire_models = [source_model(e, label) for e in observed]
                results.append({'case': label, 'status': status, 'error': error,
                    'source_posts': len(observed), 'caps': caps, 'wire_models': wire_models,
                    'chat_completion_caps': [e['body'].get('max_completion_tokens') for e in observed] if label.startswith('chat') else [],
                    'response_modalities': [(e['body'].get('generationConfig') or {}).get('responseModalities') for e in observed],
                    'private_headers': sum(len(e['private_headers']) for e in observed)})
            if not all(r['source_posts'] == 1 and r['caps'] == [16] and r['wire_models'] == [models[i]]
                       for i, r in enumerate(results[:4])):
                raise ValueError('native four-protocol output cap failed')
            if results[0]['chat_completion_caps'] != [16]:
                raise ValueError('native Chat completion-token cap failed')
            if not all(r['status'] == 400 and r['error'] == 'automatic_probe_unsafe_profile'
                       and r['source_posts'] == 0 for r in results[4:6]):
                raise ValueError('native automatic unsafe modalities were not rejected')
            if (results[6]['source_posts'] != 1 or results[6]['caps'] != [4096]
                    or results[6]['response_modalities'] != [['AUDIO']] or results[6]['wire_models'] != [models[3]]):
                raise ValueError('manual modality behavior was changed')
            if (results[7]['status'] != 307 or results[7]['source_posts'] != 1
                    or results[7]['caps'] != [4096] or results[7]['chat_completion_caps'] != [4096]
                    or results[7]['wire_models'] != [models[0]]):
                raise ValueError('native diagnostic redirect was followed')
            if any(r['private_headers'] for r in results):
                raise ValueError('native private headers reached the source')
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        source.shutdown()
        source.server_close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--feature-only', action='store_true')
    args = parser.parse_args()
    proof = {'passed': False}
    try:
        proof['provenance'] = verify_provenance(args.binary.resolve(), args.provenance, args.repo.resolve())
        if not args.feature_only:
            proof['wire'] = verify_wire(args.binary.resolve())
        proof['passed'] = True
    except Exception as error:
        proof['error_type'] = type(error).__name__
        proof['reason'] = str(error) if isinstance(error, ValueError) else 'native verification failed'
    print(json.dumps(proof, sort_keys=True))
    return 0 if proof['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
