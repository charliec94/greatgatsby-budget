import sqlite3
from datetime import date

from test_household_features import owner


def test_scheduled_transaction_posts_once_and_advances(tmp_path):
    client, csrf, account, category = owner(tmp_path)
    today = date.today().isoformat()
    response = client.post('/scheduled', data={'csrf_token': csrf, 'account_id': account, 'payee': 'Paycheck',
        'direction': 'inflow', 'amount': '250', 'frequency': 'monthly', 'next_due': today, 'category_id': ''}, follow_redirects=True)
    assert b'Scheduled transaction created' in response.data
    connection = sqlite3.connect(tmp_path / 'test.db')
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE payee='Paycheck'").fetchone()[0] == 1
    assert connection.execute("SELECT ready_to_assign FROM transactions WHERE payee='Paycheck'").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM scheduled_occurrences").fetchone()[0] == 1
    assert connection.execute("SELECT next_due FROM scheduled_transactions").fetchone()[0] > today
    connection.close()
    client.get('/planning')
    connection = sqlite3.connect(tmp_path / 'test.db')
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE payee='Paycheck'").fetchone()[0] == 1
    connection.close()
    backup = client.post('/backups/download', data={'csrf_token': csrf}).json
    assert backup['version'] == 2 and len(backup['tables']['scheduled_transactions']) == 1
    assert len(backup['tables']['scheduled_occurrences']) == 1


def test_auto_assign_preview_move_history_undo_and_focused_view(tmp_path):
    client, csrf, _account, category = owner(tmp_path)
    month = date.today().strftime('%Y-%m')
    client.post(f'/categories/{category}/target', data={'csrf_token': csrf, 'target_type': 'monthly', 'amount': '300', 'month': month})
    preview = client.get(f'/auto-assign?month={month}')
    assert b'Auto-Assign preview' in preview.data and b'Target' in preview.data
    applied = client.post('/auto-assign', data={'csrf_token': csrf, 'month': month}, follow_redirects=True)
    assert b'Auto-Assign gave USD 300.00 a job' in applied.data
    connection = sqlite3.connect(tmp_path / 'test.db')
    group = connection.execute('SELECT parent_id FROM categories WHERE id=?', (category,)).fetchone()[0]
    connection.close()
    client.post('/categories', data={'csrf_token': csrf, 'name': 'Utilities', 'parent_id': group, 'month': month})
    connection = sqlite3.connect(tmp_path / 'test.db')
    utilities = connection.execute("SELECT id FROM categories WHERE name='Utilities'").fetchone()[0]
    connection.close()
    client.post('/money/move', data={'csrf_token': csrf, 'source_id': category, 'target_id': utilities, 'amount': '50', 'month': month})
    connection = sqlite3.connect(tmp_path / 'test.db')
    move_id = connection.execute('SELECT id FROM money_moves ORDER BY id DESC').fetchone()[0]
    before = dict(connection.execute('SELECT category_id,assigned_cents FROM category_assignments WHERE month=?', (month,)))
    connection.close()
    assert before[category] == 25000 and before[utilities] == 5000
    client.post(f'/money-moves/{move_id}/undo', data={'csrf_token': csrf})
    connection = sqlite3.connect(tmp_path / 'test.db')
    after = dict(connection.execute('SELECT category_id,assigned_cents FROM category_assignments WHERE month=?', (month,)))
    assert after[category] == 30000 and after[utilities] == 0
    assert connection.execute('SELECT undone_at FROM money_moves WHERE id=?', (move_id,)).fetchone()[0]
    connection.close()
    client.post('/focused-views', data={'csrf_token': csrf, 'name': 'Only utilities', 'category_id': str(utilities)})
    connection = sqlite3.connect(tmp_path / 'test.db')
    view_id = connection.execute("SELECT id FROM focused_views WHERE name='Only utilities'").fetchone()[0]
    connection.close()
    focused = client.get(f'/?month={month}&view=custom-{view_id}')
    assert b'Utilities' in focused.data and b'Only utilities' in focused.data
    assert b'Food</span>' not in focused.data


def test_reconciliation_history_and_optional_adjustment(tmp_path):
    client, csrf, account, _category = owner(tmp_path)
    today = date.today().isoformat()
    client.post('/transactions', data={'csrf_token': csrf, 'account_id': account, 'payee': 'Deposit',
        'amount': '100', 'occurred_on': today, 'cleared': '1'})
    mismatch = client.post(f'/accounts/{account}/reconcile', data={'csrf_token': csrf, 'statement_date': today,
        'statement_balance': '1150'}, follow_redirects=True)
    assert b'Not reconciled' in mismatch.data
    connection = sqlite3.connect(tmp_path / 'test.db')
    assert connection.execute('SELECT COUNT(*) FROM account_reconciliations').fetchone()[0] == 0
    connection.close()
    fixed = client.post(f'/accounts/{account}/reconcile', data={'csrf_token': csrf, 'statement_date': today,
        'statement_balance': '1150', 'create_adjustment': '1'}, follow_redirects=True)
    assert b'balance adjustment was created' in fixed.data and b'Recent reconciliations' in fixed.data
    connection = sqlite3.connect(tmp_path / 'test.db')
    assert connection.execute('SELECT adjustment_cents FROM account_reconciliations').fetchone()[0] == 5000
    assert connection.execute("SELECT amount_cents,cleared FROM transactions WHERE payee='Reconciliation Balance Adjustment'").fetchone() == (5000, 2)
    batch = connection.execute("""INSERT INTO import_batches(budget_id,account_id,filename,headers_json,rows_json,status)
        VALUES((SELECT budget_id FROM accounts WHERE id=?),?,'older.csv','[\"Date\",\"Payee\",\"Amount\"]','[{}]','review')""",
        (account, account)).lastrowid
    row = connection.execute("INSERT INTO import_rows(batch_id,occurred_on,payee,memo,amount_cents) VALUES(?,?,?,?,?)",
                             (batch, today, 'Older bank row', '', -100)).lastrowid
    connection.commit()
    connection.close()
    review = client.get(f'/imports/{batch}/review')
    assert b'account was already reconciled' in review.data
    assert f'name="selected" value="{row}" checked'.encode() not in review.data
