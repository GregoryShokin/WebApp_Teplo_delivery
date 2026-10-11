#!/usr/bin/env python3
"""Read Revvy's observed private API using an explicit local request plan.

Credentials: REVVY_EMAIL and REVVY_PASSWORD, or interactive prompts.
A plan is a JSON array of {method, path, body?, output}; output files must not exist.
Only known read routes are allowed. No requests/messages/settings writes are exposed.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = 'https://revvy.ru'
READ_GET = {
    '/web/aggregatedreviews/reviews-rating-filters',
    '/web/aggregatedreviews/reviews-status-filters',
    '/web/aggregatedreviews/reviews-source-filters',
    '/web/aggregatedreviews/reviews-place-filters',
    '/web/aggregatedreviews/reviews-date-filters',
    '/web/aggregatedreviews/check_is_enabled',
    '/web/aggregatedreviews/check_is_configured',
    '/web/aggregatedreviews/check_is_ready',
    '/web/adminChat/messages', '/web/adminChat/chat-info',
    '/web/places/places', '/web/journalmessages/excel',
}
READ_POST = {
    '/web/aggregatedreviews', '/web/aggregatedreviews/excel',
    '/web/adminChat/chats', '/web/adminChat/chats-by-messages',
    '/web/journalmessages/messages',
}


def validate_plan(plan):
    if not isinstance(plan, list) or not plan:
        raise ValueError('Plan must be a non-empty JSON array')
    outputs = set()
    for item in plan:
        method = item.get('method', 'GET')
        path = item['path']
        parsed = urlsplit(path)
        allowed = READ_GET if method == 'GET' else READ_POST if method == 'POST' else set()
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in allowed:
            raise ValueError(f'Not an allowed read route: {method} {parsed.path}')
        if method == 'GET' and 'body' in item:
            raise ValueError('GET must not have a body')
        if method == 'POST' and not isinstance(item.get('body'), dict):
            raise ValueError('POST requires a JSON object body')
        output = Path(item['output']).expanduser().resolve()
        if output.exists() or output in outputs:
            raise ValueError(f'Output exists or is duplicated: {output}')
        if not output.parent.is_dir():
            raise ValueError(f'Output parent does not exist: {output.parent}')
        outputs.add(output)


def request(method, path, body=None, token=None):
    headers = {'Accept-Language': 'ru-RU', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if token and urlsplit(path).path == '/web/adminChat/messages':
        node = os.environ.get('REVVY_NODE') or shutil.which('node')
        if not node:
            raise ValueError('Node.js with built-in WebSocket is required for history; set REVVY_NODE')
        result = subprocess.run([node, str(Path(__file__).with_name('revvy_history.mjs'))],
                                input=json.dumps({'token': token, 'path': path}),
                                text=True, capture_output=True, timeout=70)
        if result.returncode:
            raise ValueError(result.stderr.strip() or 'SignalR history failed')
        return 200, 'application/json; transport=signalr', result.stdout.encode()
    req = Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                  headers=headers, method=method)
    with urlopen(req, timeout=60) as response:
        return response.status, response.headers.get('Content-Type', ''), response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    if args.validate_only:
        print(json.dumps({'valid': True, 'requests': len(plan)}))
        return
    email = os.environ.get('REVVY_EMAIL') or input('Revvy email: ')
    password = os.environ.get('REVVY_PASSWORD') or getpass.getpass('Revvy password: ')
    _, _, auth = request('POST', '/api/authentication', {'name': email, 'password': password})
    token = json.loads(auth)['jwtToken']
    del password, auth
    for item in plan:
        method = item.get('method', 'GET')
        status, content_type, payload = request(method, item['path'], item.get('body'), token)
        output = Path(item['output']).expanduser().resolve()
        # Create exclusively, mode 0600: no accidental replacement or world-readable contacts.
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(payload)
        summary = {'method': method, 'path': urlsplit(item['path']).path,
                   'status': status, 'content_type': content_type, 'bytes': len(payload),
                   'output': str(output)}
        if 'json' in content_type:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                summary['fields'] = list(obj)
                for key in ('totalReviews', 'chatsCount', 'totalMessages'):
                    if key in obj:
                        summary[key] = obj[key]
                for key in ('reviews', 'chats', 'messages'):
                    if isinstance(obj.get(key), list):
                        summary[f'{key}_returned'] = len(obj[key])
            elif isinstance(obj, list):
                summary['items_returned'] = len(obj)
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except HTTPError as error:
        print(f'Revvy HTTP {error.code}; stopped. No automatic retry.', file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, OSError, URLError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
