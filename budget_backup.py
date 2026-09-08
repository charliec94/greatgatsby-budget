"""Versioned, budget-scoped logical backups. Never restore SQL or authentication data."""
import json
import sqlite3
from datetime import date, datetime, timezone

TABLES = ('accounts', 'categories', 'category_assignments', 'transactions', 'transaction_splits',
          'category_activity', 'import_batches', 'import_rows', 'payee_category_rules', 'category_targets', 'saved_import_mappings')
DIRECT = {'accounts', 'categories', 'import_batches', 'payee_category_rules', 'saved_import_mappings'}
PARENTS = {
    'categories': {'parent_id': 'categories', 'credit_account_id': 'accounts'},
    'category_assignments': {'category_id': 'categories'},
    'transactions': {'account_id': 'accounts', 'category_id': 'categories'},
    'transaction_splits': {'transaction_id': 'transactions', 'category_id': 'categories'},
    'category_activity': {'transaction_id': 'transactions', 'category_id': 'categories'},
    'import_batches': {'account_id': 'accounts'}, 'import_rows': {'batch_id': 'import_batches'},
    'payee_category_rules': {'category_id': 'categories'}, 'category_targets': {'category_id': 'categories'},
    'saved_import_mappings': {'account_id': 'accounts'},
}


def snapshot(connection, budget_id):
    budget = connection.execute('SELECT * FROM budgets WHERE id=?', (budget_id,)).fetchone()
    data = {}
    for table in TABLES:
        if table in DIRECT:
            predicate = 'budget_id=?'
        elif table == 'transactions':
            predicate = 'account_id IN (SELECT id FROM accounts WHERE budget_id=?)'
        elif table in ('transaction_splits',):
            predicate = 'transaction_id IN (SELECT t.id FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.budget_id=?)'
        elif table == 'import_rows':
            predicate = 'batch_id IN (SELECT id FROM import_batches WHERE budget_id=?)'
        else:
            predicate = 'category_id IN (SELECT id FROM categories WHERE budget_id=?)'
        data[table] = [dict(row) for row in connection.execute(f'SELECT * FROM {table} WHERE {predicate}', (budget_id,))]
    return dict(format='greatgatsby-budget', version=1, created_at=datetime.now(timezone.utc).isoformat(),
                budget=dict(name=budget['name'], currency=budget['currency']), tables=data)


def validate(connection, payload):
    if not isinstance(payload, dict) or payload.get('format') != 'greatgatsby-budget' or payload.get('version') != 1:
        raise ValueError('Not a supported GreatGatsby backup.')
    tables = payload.get('tables')
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError('Backup tables are incomplete.')
    meta = payload.get('budget', {})
    if not isinstance(meta, dict) or not isinstance(meta.get('name'), str) or not meta['name'] or meta.get('currency') != 'USD':
        raise ValueError('Invalid budget metadata.')
    ids = {}
    for table in TABLES:
        rows = tables[table]
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ValueError('Backup exceeds supported row limits.')
        columns = {row['name']: row for row in connection.execute(f'PRAGMA table_info({table})')}
        ids[table] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError('Backup columns do not match this app version.')
            for name, value in row.items():
                column = columns[name]
                if value is None:
                    if column['notnull'] or name == 'id':
                        raise ValueError('Missing required value.')
                elif column['type'] == 'INTEGER':
                    if type(value) is not int or abs(value) > 2**53:
                        raise ValueError('Invalid numeric value.')
                elif not isinstance(value, str):
                    raise ValueError('Invalid text value.')
            if row['id'] <= 0 or row['id'] in ids[table]:
                raise ValueError('Duplicate or invalid record ID.')
            ids[table].add(row['id'])
            if 'occurred_on' in row:
                date.fromisoformat(row['occurred_on'])
            if table == 'accounts' and row['type'] not in ('checking', 'savings', 'cash', 'credit'):
                raise ValueError('Unknown account type.')
            if table == 'category_targets':
                if row['target_type'] not in ('monthly', 'balance', 'date') or row['amount_cents'] <= 0:
                    raise ValueError('Invalid category target.')
                if row['target_date']:
                    date.fromisoformat(row['target_date'])
            if table == 'category_assignments':
                datetime.strptime(row['month'], '%Y-%m')
            if table == 'import_batches':
                headers, raw_rows = json.loads(row['headers_json']), json.loads(row['rows_json'])
                if not isinstance(headers, list) or not headers or not all(isinstance(h, str) for h in headers):
                    raise ValueError('Invalid import headers.')
                if not isinstance(raw_rows, list) or not raw_rows or not all(isinstance(r, dict) and all(isinstance(v, str) for v in r.values()) for r in raw_rows):
                    raise ValueError('Invalid import rows.')
            if table == 'saved_import_mappings':
                if not isinstance(json.loads(row['headers_json']), list) or not isinstance(json.loads(row['mapping_json']), dict):
                    raise ValueError('Invalid saved mapping.')
    for table, relations in PARENTS.items():
        for row in tables[table]:
            for column, parent in relations.items():
                if row[column] is not None and row[column] not in ids[parent]:
                    raise ValueError('Backup contains an out-of-budget reference.')
    category_by_id = {row['id']: row for row in tables['categories']}
    for c in category_by_id.values():
        seen = set()
        node = c
        while node['parent_id'] is not None:
            if node['id'] in seen:
                raise ValueError('Category hierarchy contains a cycle.')
            seen.add(node['id'])
            node = category_by_id[node['parent_id']]
    transfers = {}
    split_totals = {}
    for s in tables['transaction_splits']:
        split_totals[s['transaction_id']] = split_totals.get(s['transaction_id'], 0) + s['amount_cents']
    for t in tables['transactions']:
        if t['cleared'] not in (0, 1, 2):
            raise ValueError('Invalid transaction status.')
        if t['id'] in split_totals and (split_totals[t['id']] != t['amount_cents'] or t['category_id'] or t['transfer_id']):
            raise ValueError('Unbalanced split transaction.')
        if t['transfer_id']:
            if t['category_id'] is not None:
                raise ValueError('Transfers cannot have spending categories.')
            transfers.setdefault(t['transfer_id'], []).append(t)
    for legs in transfers.values():
        if len(legs) != 2 or sum(t['amount_cents'] for t in legs) != 0 or legs[0]['account_id'] == legs[1]['account_id']:
            raise ValueError('Unbalanced transfer.')


def replace_budget(connection, budget_id, payload):
    """Caller owns the transaction; deferred foreign keys handle credit-envelope cycles."""
    validate(connection, payload)
    old = snapshot(connection, budget_id)
    connection.execute('PRAGMA defer_foreign_keys=ON')
    for table in reversed(TABLES):
        for row in old['tables'][table]:
            connection.execute(f'DELETE FROM {table} WHERE id=?', (row['id'],))
    for table in TABLES:
        for original in payload['tables'][table]:
            row = dict(original)
            if table in DIRECT:
                row['budget_id'] = budget_id
            columns = list(row)
            connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row[c] for c in columns))
    connection.execute('UPDATE budgets SET name=?,currency=? WHERE id=?', (payload['budget']['name'], payload['budget']['currency'], budget_id))
    if connection.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Backup relationships failed validation.')
