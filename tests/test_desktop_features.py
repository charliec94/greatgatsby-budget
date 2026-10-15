import sqlite3

from test_household_features import owner


def test_month_review_reconciliation_search_and_about(tmp_path):
    client, csrf, account, category = owner(tmp_path)
    client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'category_id': category,
        'occurred_on': '2026-10-05', 'payee': 'Desktop Market', 'amount': '-25'})
    month = client.get('/month-review?month=2026-10')
    assert month.status_code == 200
    assert b'October 2026 review' in month.data and b'Cash overspending' in month.data

    reconciliation = client.get('/reconciliation')
    assert reconciliation.status_code == 200
    assert b'Reconciliation dashboard' in reconciliation.data and b'Never' in reconciliation.data

    search = client.get('/search?q=Desktop+Market')
    assert search.status_code == 200
    assert b'Desktop Market' in search.data and b'Transaction' in search.data

    about = client.get('/about')
    assert about.status_code == 200
    assert b'GreatGatsby Budget 0.2.0' in about.data and b'Transactions' in about.data
    connection = sqlite3.connect(tmp_path / 'test.db')
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE payee='Desktop Market'").fetchone()[0] == 1
    connection.close()
