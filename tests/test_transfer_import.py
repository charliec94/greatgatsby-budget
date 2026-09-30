import io
import sqlite3

from test_app import make_client, token


def upload_amount_csv(client, csrf, account_id, filename, amount, payee='AUTOPAY'):
    upload = client.post('/imports/upload', data={'csrf_token': csrf, 'account_id': str(account_id),
        'statement': (io.BytesIO(f'Date,Description,Amount\n09/15/2026,{payee},{amount}\n'.encode()), filename)},
        content_type='multipart/form-data')
    batch_id = int(upload.location.split('/')[-2])
    client.post(f'/imports/{batch_id}/map', data={'csrf_token': csrf, 'date_column': 'Date',
        'payee_column': 'Description', 'amount_column': 'Amount'})
    connection = sqlite3.connect(client.application.config['DATABASE'])
    row_id = connection.execute('SELECT id FROM import_rows WHERE batch_id=?', (batch_id,)).fetchone()[0]
    connection.close()
    return batch_id, row_id


def test_credit_card_import_links_payment_instead_of_ready_to_assign(tmp_path):
    client = make_client(tmp_path)
    client.post('/setup', data={'csrf_token': token(client), 'name': 'Charlie', 'email': 'charlie@example.com', 'password': 'a-strong-password'})
    csrf = token(client, '/')
    client.post('/accounts', data={'csrf_token': csrf, 'name': 'Checking', 'type': 'checking', 'balance': '1000'})
    client.post('/accounts', data={'csrf_token': csrf, 'name': 'Visa', 'type': 'credit', 'balance': '-500'})
    connection = sqlite3.connect(tmp_path / 'test.db')
    checking = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    visa = connection.execute("SELECT id FROM accounts WHERE name='Visa'").fetchone()[0]
    connection.close()
    client.post('/transactions', data={'csrf_token': csrf, 'account_id': checking, 'payee': 'VISA AUTOPAY',
        'amount': '-100', 'occurred_on': '2026-09-14', 'category_id': ''})
    connection = sqlite3.connect(tmp_path / 'test.db')
    checking_leg = connection.execute("SELECT id FROM transactions WHERE payee='VISA AUTOPAY'").fetchone()[0]
    connection.close()

    batch, row = upload_amount_csv(client, csrf, visa, 'visa.csv', '100.00')
    review = client.get(f'/imports/{batch}/review')
    assert b'Link transfer with Checking' in review.data
    assert b'value="ready_to_assign"' not in review.data
    saved = client.post(f'/imports/{batch}/autosave', data={'csrf_token': csrf, 'row_id': row,
        'selected': '1', 'category': f'transfer:{checking_leg}'})
    assert saved.status_code == 204
    committed = client.post(f'/imports/{batch}/commit', data={'csrf_token': csrf, 'selected': str(row),
        f'category_{row}': f'transfer:{checking_leg}'}, follow_redirects=True)
    assert b'Imported 1 transaction' in committed.data
    connection = sqlite3.connect(tmp_path / 'test.db')
    legs = connection.execute("SELECT account_id,amount_cents,transfer_id,ready_to_assign FROM transactions ORDER BY account_id").fetchall()
    assert len(legs) == 2 and sum(leg[1] for leg in legs) == 0
    assert legs[0][2] and legs[0][2] == legs[1][2]
    assert all(leg[3] == 0 for leg in legs)
    connection.close()


def test_import_review_flags_transfer_waiting_for_other_draft(tmp_path):
    client = make_client(tmp_path)
    client.post('/setup', data={'csrf_token': token(client), 'name': 'Charlie', 'email': 'charlie@example.com', 'password': 'a-strong-password'})
    csrf = token(client, '/')
    client.post('/accounts', data={'csrf_token': csrf, 'name': 'Checking', 'type': 'checking', 'balance': '0'})
    client.post('/accounts', data={'csrf_token': csrf, 'name': 'Savings', 'type': 'savings', 'balance': '0'})
    connection = sqlite3.connect(tmp_path / 'test.db')
    checking = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    savings = connection.execute("SELECT id FROM accounts WHERE name='Savings'").fetchone()[0]
    connection.close()
    upload_amount_csv(client, csrf, checking, 'checking.csv', '-50.00', 'Transfer to savings')
    savings_batch, _row = upload_amount_csv(client, csrf, savings, 'savings.csv', '50.00', 'Transfer from checking')
    review = client.get(f'/imports/{savings_batch}/review')
    assert b'Possible transfer waiting for Checking to be imported' in review.data
