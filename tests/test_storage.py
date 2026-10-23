import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub import storage
from hub.main import create_app

HEADERS = {'x-pi-hub': '1', 'origin': 'http://127.0.0.1:8840'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / 'home'
    cache = home / '.cache/pip/http-v2'
    (cache / 'a').mkdir(parents=True)
    (cache / 'a/blob').write_bytes(os.urandom(2 * 1024 * 1024))
    (cache / 'top').write_bytes(b'y' * 100)
    outside = root / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    monkeypatch.setattr(storage, 'HOME', home)
    monkeypatch.setattr(storage, 'ROOTS', (home,))
    monkeypatch.setattr(storage, 'CACHES', {'pip-http': (cache, 'Python downloads')})
    asked = []

    def ask(system, user):
        asked.append(user)
        return 'Advice'
    data = root / 'data'
    data.mkdir()
    return {'root': root, 'home': home, 'cache': cache, 'outside': outside, 'asked': asked,
            'storage': storage.Storage(data / 'storage.json', ask=ask)}


def wait(s):
    for _ in range(250):
        if not s.operation.locked():
            return s.status()
        time.sleep(0.02)
    raise AssertionError('job did not finish')


def test_tree_rejects_traversal_outside_roots_and_symlinks(env):
    (env['home'] / 'link').symlink_to(env['outside'])
    for bad in ('', 'relative', str(env['outside']), str(env['home'] / '../outside'), str(env['home'] / 'link'), None, 'a\x00b'):
        with pytest.raises(ValueError):
            storage.tree(bad)


def test_tree_lists_without_following_links(env):
    (env['home'] / 'link').symlink_to(env['outside'])
    rows = {Path(r['path']).name: r for r in storage.tree(str(env['home']))['rows']}
    assert rows['.cache']['kind'] == 'folder'
    assert rows['link']['kind'] == 'link'
    assert 'keep' not in rows


def test_preview_rejects_unknown_ids(env):
    for bad in (None, [], ['npm'], ['../etc'], [1], 'pip-http'):
        with pytest.raises(ValueError):
            env['storage'].preview(bad)


def test_clean_needs_confirmation_and_token_is_single_use(env, monkeypatch):
    s = env['storage']
    monkeypatch.setattr(s, 'start', lambda action: None)
    token = s.preview(['pip-http'])['token']
    with pytest.raises(ValueError):
        s.clean(token, 'yes')
    event = s.clean(token, 'DELETE CACHES')
    assert event['items'] == ['Python downloads']
    assert env['cache'].is_dir() and list(env['cache'].iterdir()) == []
    assert s.status()['history'][0]['items'] == ['Python downloads']
    with pytest.raises(ValueError):
        s.clean(token, 'DELETE CACHES')


def test_expired_or_changed_preview_deletes_nothing(env):
    s = env['storage']
    token = s.preview(['pip-http'])['token']
    s.previews[token]['expires'] = time.time() - 1
    with pytest.raises(ValueError):
        s.clean(token, 'DELETE CACHES')
    token = s.preview(['pip-http'])['token']
    (env['cache'] / 'new').write_text('arrived later')
    with pytest.raises(ValueError):
        s.clean(token, 'DELETE CACHES')
    assert (env['cache'] / 'top').exists()


def test_clear_cache_is_allowlisted_and_symlink_safe(env):
    with pytest.raises(ValueError):
        storage.clear_cache(env['outside'])
    (env['cache'] / 'escape').symlink_to(env['outside'])
    storage.clear_cache(env['cache'])
    assert (env['outside'] / 'keep').exists()
    assert not (env['cache'] / 'escape').exists()
    link = env['home'] / 'linked'
    link.symlink_to(env['outside'])
    with pytest.raises(ValueError):
        storage.cache_snapshot(link)


def test_scan_persists_suggests_cache_and_sends_metadata_only(env):
    s = env['storage']
    (env['home'] / 'notes.txt').write_text('TOP-SECRET-CONTENT')
    s.start('scan')
    st = wait(s)
    assert st['error'] == ''
    assert [c['id'] for c in st['scan']['caches']] == ['pip-http']
    assert any(x.get('cache_id') == 'pip-http' for x in st['scan']['suggestions'])
    assert st['advice']['text'] == 'Advice'  # first scan asks once
    assert 'TOP-SECRET-CONTENT' not in env['asked'][0]
    assert json.loads(env['asked'][0])['cleanup_allowlist'][0]['id'] == 'pip-http'
    assert s.path.stat().st_mode & 0o777 == 0o600
    assert storage.Storage(s.path).status()['scan']['at'] == st['scan']['at']
    s.start('scan')
    wait(s)
    assert len(env['asked']) == 1  # automatic advice at most once a day


def test_failed_auto_advice_keeps_scan(env):
    s = env['storage']
    s.ask = lambda system, user: (_ for _ in ()).throw(ValueError('not logged in'))
    s.start('scan')
    st = wait(s)
    assert st['scan'] and 'Scan finished' in st['error']


def test_one_operation_at_a_time(env):
    s = env['storage']
    s.operation.acquire()
    try:
        with pytest.raises(ValueError):
            s.start('scan')
        with pytest.raises(ValueError):
            s.preview(['pip-http'])
    finally:
        s.operation.release()


def test_claude_call_has_no_tools_and_no_api_key(monkeypatch):
    seen = {}

    class Result:
        returncode, stderr, stdout = 0, '', json.dumps({'result': 'ok'})

    def run(args, **kw):
        seen.update(args=args, **kw)
        return Result()
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    monkeypatch.setattr(storage, 'claude_path', lambda: '/usr/bin/claude')
    monkeypatch.setattr(storage.subprocess, 'run', run)
    assert storage.ask_claude('sys', 'user') == 'ok'
    assert seen['args'][seen['args'].index('--tools') + 1] == ''
    assert '--strict-mcp-config' in seen['args']
    assert 'ANTHROPIC_API_KEY' not in seen['env']


def test_api_routes_and_guards(env, tmp_path):
    app = create_app(tmp_path / 'data', background=False)
    app.state.storage.__dict__.update(env['storage'].__dict__)
    with TestClient(app, base_url='http://127.0.0.1:8840') as client:
        assert client.get('/api/storage', headers={'host': 'evil.example'}).status_code == 400
        assert client.get('/api/storage').json()['confirmation'] == 'DELETE CACHES'
        assert client.get('/api/storage/tree', params={'path': str(env['outside'])}).status_code == 400
        assert client.get('/api/storage/tree', params={'path': str(env['home'])}).status_code == 200
        assert client.post('/api/storage/scan', headers={'x-pi-hub': '1', 'origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/storage/nope', headers=HEADERS).status_code == 404
        assert client.post('/api/storage/cleanup/preview', json={'ids': ['../x']}, headers=HEADERS).status_code == 409
        token = client.post('/api/storage/cleanup/preview', json={'ids': ['pip-http']}, headers=HEADERS).json()['token']
        assert client.post('/api/storage/cleanup', json={'token': token, 'confirmation': 'no'}, headers=HEADERS).status_code == 409
        r = client.post('/api/storage/cleanup', json={'token': token, 'confirmation': 'DELETE CACHES'}, headers=HEADERS)
        assert r.status_code == 200 and r.json()['items'] == ['Python downloads']
        wait(app.state.storage)
        actions = [e['action'] for e in client.get('/api/activity').json()]
        assert 'storage cleanup' in actions
