import sqlite3
from test_app import make_client, token


def test_register_filters_and_safe_bulk_actions(tmp_path):
    client = make_client(tmp_path)
    client.post('/setup', data={'csrf_token': token(client), 'name': 'Tester', 'email': 'test@example.com', 'password': 'long-password'})
    csrf = token(client, '/')
    for name in ('Checking', 'Savings'):
        client.post('/accounts', data={'csrf_token': csrf, 'name': name, 'type': 'checking', 'balance': '100'})
    con = sqlite3.connect(tmp_path / 'test.db')
    first, second = [row[0] for row in con.execute('SELECT id FROM accounts ORDER BY id')]
    con.close()
    for account, payee, amount, day in [(first, 'Cafe', '-10', '01'), (first, 'Market', '-20', '02'), (second, 'Other', '-5', '03')]:
        client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'payee': payee, 'amount': amount, 'occurred_on': f'2026-09-{day}', 'memo': 'receipt'})
    page = client.get(f'/accounts/{first}?q=Market&direction=outflow&start=2026-09-02&sort=oldest')
    assert page.status_code == 200 and b'Showing 1 of 2' in page.data
    assert b'$70.00' in page.data
    con = sqlite3.connect(tmp_path / 'test.db')
    ids = dict(con.execute('SELECT payee,id FROM transactions'))
    con.execute('UPDATE transactions SET cleared=2 WHERE id=?', (ids['Cafe'],))
    con.commit()
    con.close()
    client.post(f'/accounts/{first}/bulk', data={'csrf_token': csrf, 'action': 'clear', 'selected': [str(v) for v in ids.values()]})
    con = sqlite3.connect(tmp_path / 'test.db')
    assert dict(con.execute('SELECT payee,cleared FROM transactions')) == {'Cafe': 2, 'Market': 1, 'Other': 0}
    con.close()
    filtered = client.get(f'/accounts/{first}?status=1&category=uncategorized&q=receipt')
    assert b'Showing 1 of 2' in filtered.data
    client.post(f'/accounts/{first}/bulk', data={'csrf_token': csrf, 'action': 'delete', 'selected': [str(v) for v in ids.values()]})
    con = sqlite3.connect(tmp_path / 'test.db')
    assert {r[0] for r in con.execute('SELECT payee FROM transactions')} == {'Cafe', 'Other'}
    con.close()
