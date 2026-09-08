import json
import sqlite3
from test_household_features import owner


def test_budget_creation_switch_isolation_and_deletion(tmp_path):
    client, csrf, account, category = owner(tmp_path)
    with client.session_transaction() as session:
        first_id = session['budget_id']
    created = client.post('/budgets/create', data={'csrf_token': csrf, 'name': 'Travel plan'}, follow_redirects=True)
    assert created.status_code == 200 and b'Travel plan' in created.data
    with client.session_transaction() as session:
        second_id = session['budget_id']
    assert first_id != second_id
    assert client.get(f'/accounts/{account}').status_code == 404
    assert client.post(f'/categories/{category}/assignment', data={'csrf_token': csrf, 'assigned': '50'}).status_code == 400
    assert client.post('/budgets/9999/switch', data={'csrf_token': csrf}).status_code == 404
    client.post(f'/budgets/{first_id}/switch', data={'csrf_token': csrf})
    assert client.get(f'/accounts/{account}').status_code == 200
    denied = client.post(f'/budgets/{first_id}/delete', data={'csrf_token': csrf, 'confirm_name': 'Wrong', 'password': 'long-password'}, follow_redirects=True)
    assert b'exact budget name' in denied.data
    deleted = client.post(f'/budgets/{first_id}/delete', data={'csrf_token': csrf, 'confirm_name': 'My GreatGatsby Budget', 'password': 'long-password'}, follow_redirects=True)
    assert b'Budget deleted' in deleted.data
    con = sqlite3.connect(tmp_path / 'test.db')
    assert con.execute('SELECT name FROM budgets').fetchall() == [('Travel plan',)]
    assert con.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0
    assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    con.close()
    recovery = list((tmp_path / 'backups').glob('deleted-budget-*.json'))
    assert len(recovery) == 1
    assert json.loads(recovery[0].read_text())['tables']['accounts'][0]['id'] == account
    client.post(f'/budgets/{second_id}/delete', data={'csrf_token': csrf, 'confirm_name': 'Travel plan', 'password': 'long-password'})
    assert b'A fresh start' in client.get('/budgets').data
    assert client.get('/').location.endswith('/budgets')
    assert client.post('/budgets/create', data={'csrf_token': csrf, 'name': 'New start'}, follow_redirects=True).status_code == 200


def test_requested_ui_actions_and_member_deletion_protection(tmp_path):
    client, csrf, account, _ = owner(tmp_path)
    dashboard = client.get('/').data
    assert b'id="transaction-dialog"' not in dashboard
    assert b'Explain target types' in dashboard and b'Assign monthly' in dashboard
    sidebar = dashboard.split(b'<aside')[1].split(b'</aside>')[0]
    assert b'Transfer money' not in sidebar and b'class="nav-icon"' in sidebar
    assert b'dark-ui.css' in dashboard
    assert b'id="transfer-dialog"' in client.get(f'/accounts/{account}').data
    con = sqlite3.connect(tmp_path / 'test.db')
    budget_id = con.execute('SELECT id FROM budgets').fetchone()[0]
    con.execute("INSERT INTO users(name,email,password_hash,role) VALUES('Member','member@example.com','unused','member')")
    member_id = con.execute("SELECT id FROM users WHERE email='member@example.com'").fetchone()[0]
    con.execute('INSERT INTO budget_members(budget_id,user_id) VALUES(?,?)', (budget_id, member_id))
    con.commit()
    con.close()
    member = client.application.test_client()
    with member.session_transaction() as session:
        session['user_id'] = member_id
        session['csrf_token'] = 'member-csrf'
    assert b'Delete this budget' not in member.get('/').data
    assert member.post(f'/budgets/{budget_id}/delete', data={'csrf_token': 'member-csrf', 'confirm_name': 'My GreatGatsby Budget', 'password': 'long-password'}).status_code == 403
