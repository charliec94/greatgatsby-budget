import io
import json
import re
import sqlite3
from flask import template_rendered
from test_app import make_client, token


def owner(tmp_path):
    client = make_client(tmp_path)
    client.post('/setup', data={'csrf_token': token(client), 'name': 'Owner', 'email': 'owner@example.com', 'password': 'long-password'})
    csrf = token(client, '/')
    client.post('/accounts', data={'csrf_token': csrf, 'name': 'Checking', 'type': 'checking', 'balance': '1000'})
    client.post('/categories', data={'csrf_token': csrf, 'name': 'Bills'})
    con = sqlite3.connect(tmp_path / 'test.db')
    account = con.execute('SELECT id FROM accounts').fetchone()[0]
    group = con.execute('SELECT id FROM categories').fetchone()[0]
    con.close()
    client.post('/categories', data={'csrf_token': csrf, 'name': 'Food', 'parent_id': group})
    con = sqlite3.connect(tmp_path / 'test.db')
    category = con.execute('SELECT id FROM categories WHERE parent_id IS NOT NULL').fetchone()[0]
    con.close()
    return client, csrf, account, category


def test_reports_exclude_transfers_and_include_splits_refunds(tmp_path):
    client, csrf, account, category = owner(tmp_path)
    for name, amount, cat in [('Pay', '500', ''), ('Grocer', '-50', category), ('Refund', '10', category)]:
        client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'payee': name, 'amount': amount, 'category_id': cat, 'occurred_on': '2026-09-01'})
    client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'payee': 'Split', 'outflow': '50', 'is_split': '1',
        'split_category_id': [str(category), str(category)], 'split_amount': ['13', '37'], 'occurred_on': '2026-09-02'})
    client.post('/accounts', data={'csrf_token': csrf, 'name': 'Visa', 'type': 'credit', 'balance': '-100'})
    con = sqlite3.connect(tmp_path / 'test.db')
    card = con.execute("SELECT id FROM accounts WHERE name='Visa'").fetchone()[0]
    con.close()
    client.post('/transfers', data={'csrf_token': csrf, 'source_account_id': account, 'target_account_id': card, 'amount': '100', 'occurred_on': '2026-09-03'})
    rendered = []
    def capture(sender, template, context, **extra):
        rendered.append(context)
    with template_rendered.connected_to(capture, client.application):
        response = client.get('/reports?start=2026-09-01&end=2026-09-30')
    assert response.status_code == 200
    ctx = rendered[-1]
    assert (ctx['income'], ctx['spending'], ctx['refunds'], ctx['net']) == (50000, 10000, 1000, 41000)
    assert ctx['groups']['Bills'] == 9000
    assert client.get('/reports?start=2026-10-01&end=2026-09-01').status_code == 400
    assert client.get('/reports?account_id=9999').status_code == 400


def test_backup_restore_roundtrip_validation_and_auth_exclusion(tmp_path):
    client, csrf, account, category = owner(tmp_path)
    client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'payee': 'Original', 'amount': '-15', 'category_id': category})
    response = client.post('/backups/download', data={'csrf_token': csrf})
    assert response.status_code == 200
    backup = response.json
    assert 'users' not in backup['tables'] and 'long-password' not in response.get_data(as_text=True)
    client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'payee': 'Later', 'amount': '-20'})
    def restore(payload, password='long-password'):
        return client.post('/backups/restore', data={'csrf_token': csrf, 'confirm': 'RESTORE', 'password': password,
            'backup': (io.BytesIO(json.dumps(payload).encode()), 'backup.json')}, content_type='multipart/form-data', follow_redirects=True)
    assert b'owner password' in restore(backup, 'wrong').data
    damaged = json.loads(json.dumps(backup))
    damaged['tables']['transactions'][0]['category_id'] = 99999
    assert b'Restore failed' in restore(damaged).data
    con = sqlite3.connect(tmp_path / 'test.db')
    assert con.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 2
    con.close()
    result = restore(backup)
    assert b'Budget restored' in result.data
    con = sqlite3.connect(tmp_path / 'test.db')
    assert con.execute('SELECT payee FROM transactions').fetchall() == [('Original',)]
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    con.close()
    recovery = list((tmp_path / 'backups').glob('*.json'))
    assert len(recovery) == 1
    assert len(json.loads(recovery[0].read_text())['tables']['transactions']) == 2


def test_invites_single_use_owner_permissions_and_revocation(tmp_path):
    client, csrf, _, _ = owner(tmp_path)
    response = client.post('/household/invite', data={'csrf_token': csrf, 'email': 'member@example.com'})
    invite_token = re.search(r'/join/([A-Za-z0-9_-]+)', response.get_data(as_text=True)).group(1)
    con = sqlite3.connect(tmp_path / 'test.db')
    assert con.execute('SELECT token_hash FROM household_invites').fetchone()[0] != invite_token
    con.close()
    member = client.application.test_client()
    member_csrf = token(member, '/join/' + invite_token)
    joined = member.post('/join/' + invite_token, data={'csrf_token': member_csrf, 'name': 'Member', 'password': 'member-password'}, follow_redirects=True)
    assert joined.status_code == 200 and b'Make every dollar intentional' in joined.data
    assert member.get('/join/' + invite_token).status_code == 410
    assert member.get('/reports').status_code == 200
    for page in ('/household', '/backups'):
        assert member.get(page).status_code == 403
    member_csrf = token(member, '/')
    assert member.post('/backups/download', data={'csrf_token': member_csrf}).status_code == 403
    assert member.post('/household/invite', data={'csrf_token': member_csrf, 'email': 'other@example.com'}).status_code == 403
    con = sqlite3.connect(tmp_path / 'test.db')
    member_id = con.execute("SELECT id FROM users WHERE email='member@example.com'").fetchone()[0]
    con.close()
    client.post(f'/household/members/{member_id}/remove', data={'csrf_token': csrf})
    assert member.get('/reports').status_code == 302
    assert member.get('/').status_code == 302


def test_optional_smtp_tls_and_failure_fallback(tmp_path, monkeypatch):
    client, csrf, _, _ = owner(tmp_path)
    calls = []
    class FakeSMTP:
        def __init__(self, host, port, timeout): calls.append((host, port))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): pass
        def starttls(self, context): calls.append('tls')
        def login(self, user, password): calls.append('login')
        def send_message(self, message):
            assert message['To'] == 'member@example.com'
            assert 'http://nas.local:8081/join/' in message.get_content()
            calls.append('sent')
    monkeypatch.setattr('household_features.smtplib.SMTP', FakeSMTP)
    client.post('/household/invite', data={'csrf_token': csrf, 'email': 'member@example.com', 'send_email': '1'})
    assert calls == []
    client.application.config.update(APP_BASE_URL='http://nas.local:8081', SMTP_USERNAME='sender@gmail.com', SMTP_PASSWORD='fake-test-only')
    response = client.post('/household/invite', data={'csrf_token': csrf, 'email': 'member@example.com', 'send_email': '1'})
    assert b'Invitation email sent' in response.data
    assert calls == [('smtp.gmail.com', 587), 'tls', 'login', 'sent']
    def fail(*args, **kwargs): raise OSError('offline')
    monkeypatch.setattr('household_features.smtplib.SMTP', fail)
    response = client.post('/household/invite', data={'csrf_token': csrf, 'email': 'member@example.com', 'send_email': '1'})
    assert b'Email delivery failed' in response.data and b'/join/' in response.data


def test_expired_revoked_invites_and_existing_database_migration(tmp_path):
    client, csrf, account, _ = owner(tmp_path)
    response = client.post('/household/invite', data={'csrf_token': csrf, 'email': 'guest@example.com'})
    raw = re.search(r'/join/([A-Za-z0-9_-]+)', response.get_data(as_text=True)).group(1)
    con = sqlite3.connect(tmp_path / 'test.db')
    invite_id = con.execute('SELECT id FROM household_invites').fetchone()[0]
    con.close()
    client.post(f'/household/invites/{invite_id}/revoke', data={'csrf_token': csrf})
    assert client.application.test_client().get('/join/' + raw).status_code == 410
    response = client.post('/household/invite', data={'csrf_token': csrf, 'email': 'guest@example.com'})
    raw = re.search(r'/join/([A-Za-z0-9_-]+)', response.get_data(as_text=True)).group(1)
    con = sqlite3.connect(tmp_path / 'test.db')
    con.execute("UPDATE household_invites SET expires_at='2000-01-01T00:00:00+00:00'")
    con.commit()
    con.close()
    assert client.application.test_client().get('/join/' + raw).status_code == 410
    con = sqlite3.connect(tmp_path / 'test.db')
    con.execute('DROP TABLE household_invites')
    con.execute('DROP TABLE budget_members')
    con.commit()
    con.close()
    assert client.get('/').status_code == 200
    con = sqlite3.connect(tmp_path / 'test.db')
    assert con.execute('SELECT COUNT(*) FROM household_invites').fetchone()[0] == 0
    assert con.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0] == 'Checking'
    con.close()
