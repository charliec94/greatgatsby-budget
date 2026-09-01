import sqlite3
import io

from app import create_app


def make_client(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "test"})
    return app.test_client()


def token(client, path="/setup"):
    client.get(path)
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_health(tmp_path):
    assert make_client(tmp_path).get("/health").json == {"status": "ok"}


def test_setup_login_and_budget_flow(tmp_path):
    client = make_client(tmp_path)
    response = client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"}, follow_redirects=True)
    assert b"Make every dollar intentional" in response.data

    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    connection = sqlite3.connect(tmp_path / "test.db")
    account_id = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    connection.close()
    register = client.get(f"/accounts/{account_id}")
    assert b"GreatGatsby" in register.data
    assert b'class="account active"' in register.data
    assert b"$1,000.00" in register.data
    assert b"Budget" in register.data
    client.post("/categories", data={"csrf_token": csrf, "name": "Fun Money", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    group_id = connection.execute("SELECT id FROM categories WHERE name='Fun Money'").fetchone()[0]
    connection.close()
    client.post("/categories", data={"csrf_token": csrf, "name": "Eating Out", "parent_id": group_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    child_id = connection.execute("SELECT id FROM categories WHERE name='Eating Out'").fetchone()[0]
    connection.close()
    response = client.post(f"/categories/{child_id}/assignment", data={"csrf_token": csrf, "assigned": "200", "month": "2026-09"}, follow_redirects=True)
    assert b"Fun Money" in response.data
    assert b"$200.00" in response.data

    previous = client.get("/?month=2026-08")
    assert b'value="0.00"' in previous.data

    hidden = client.post(f"/categories/{child_id}/visibility", data={"csrf_token": csrf, "month": "2026-09"}, follow_redirects=True)
    assert b'aria-label="Assigned to Eating Out"' not in hidden.data
    archived = client.get("/?month=2026-09&show_hidden=1")
    assert b"Eating Out" in archived.data and b"Archived" in archived.data

    client.post("/logout", data={"csrf_token": csrf})
    response = client.post("/login", data={"csrf_token": token(client, "/login"), "email": "charlie@example.com", "password": "a-strong-password"}, follow_redirects=True)
    assert b"Checking" in response.data


def test_group_cannot_receive_assignment(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/categories", data={"csrf_token": csrf, "name": "Travel", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    group_id = connection.execute("SELECT id FROM categories WHERE name='Travel'").fetchone()[0]
    connection.close()
    response = client.post(f"/categories/{group_id}/assignment", data={"csrf_token": csrf, "assigned": "500", "month": "2026-09"})
    assert response.status_code == 400


def test_targets_progress_and_fund_underfunded_stops_at_zero(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Plan", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    group_id = connection.execute("SELECT id FROM categories WHERE name='Plan'").fetchone()[0]
    connection.close()
    for name in ("Groceries", "Spain", "Car Repair"):
        client.post("/categories", data={"csrf_token": csrf, "name": name, "parent_id": group_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    ids = dict(connection.execute("SELECT name,id FROM categories WHERE parent_id=?", (group_id,)))
    connection.close()
    client.post(f"/categories/{ids['Groceries']}/target", data={"csrf_token": csrf, "month": "2026-09", "target_type": "monthly", "amount": "600", "due_day": "15"})
    client.post(f"/categories/{ids['Spain']}/target", data={"csrf_token": csrf, "month": "2026-09", "target_type": "date", "amount": "1200", "target_date": "2027-01-01"})
    client.post(f"/categories/{ids['Car Repair']}/target", data={"csrf_token": csrf, "month": "2026-09", "target_type": "monthly", "amount": "500"})
    budget = client.get("/?month=2026-09")
    assert b"$600.00 needed" in budget.data and b"$240.00 needed" in budget.data
    funded = client.post("/targets/fund-underfunded", data={"csrf_token": csrf, "month": "2026-09"}, follow_redirects=True)
    assert b"Assigned $1,000.00" in funded.data and b"$0.00" in funded.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assignments = dict(connection.execute("SELECT category_id,assigned_cents FROM category_assignments WHERE month='2026-09'"))
    assert assignments[ids["Groceries"]] == 60000
    assert assignments[ids["Spain"]] == 24000
    assert assignments[ids["Car Repair"]] == 16000
    connection.close()
    client.post(f"/categories/{ids['Spain']}/target/delete", data={"csrf_token": csrf, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT 1 FROM category_targets WHERE category_id=?", (ids["Spain"],)).fetchone() is None
    assert connection.execute("SELECT assigned_cents FROM category_assignments WHERE category_id=? AND month='2026-09'", (ids["Spain"],)).fetchone()[0] == 24000
    connection.close()


def test_imported_credit_card_payment_matching_and_missing_side(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/accounts", data={"csrf_token": csrf, "name": "Visa", "type": "credit", "balance": "-300"})
    connection = sqlite3.connect(tmp_path / "test.db")
    checking_id = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    visa_id = connection.execute("SELECT id FROM accounts WHERE name='Visa'").fetchone()[0]
    connection.close()
    client.post("/transactions", data={"csrf_token": csrf, "account_id": checking_id, "occurred_on": "2026-09-10", "payee": "AUTOPAY VISA", "amount": "-100"})
    client.post("/transactions", data={"csrf_token": csrf, "account_id": visa_id, "occurred_on": "2026-09-11", "payee": "PAYMENT RECEIVED", "amount": "100"})
    connection = sqlite3.connect(tmp_path / "test.db")
    checking_leg = connection.execute("SELECT id FROM transactions WHERE account_id=?", (checking_id,)).fetchone()[0]
    visa_leg = connection.execute("SELECT id FROM transactions WHERE account_id=?", (visa_id,)).fetchone()[0]
    connection.close()
    inbox = client.get("/uncategorized")
    assert b"Match Visa" in inbox.data and b"Match Checking" in inbox.data
    assert inbox.data.count(b"Choose category") >= 2
    matched = client.post(f"/transactions/{checking_leg}/match-transfer", data={"csrf_token": csrf, "match_id": str(visa_leg)}, follow_redirects=True)
    assert b"linked as one transfer" in matched.data
    connection = sqlite3.connect(tmp_path / "test.db")
    links = connection.execute("SELECT DISTINCT transfer_id FROM transactions WHERE id IN (?,?)", (checking_leg, visa_leg)).fetchall()
    assert len(links) == 1 and links[0][0]
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NOT NULL").fetchone()[0] == 0
    connection.close()

    client.post("/transactions", data={"csrf_token": csrf, "account_id": checking_id, "occurred_on": "2026-10-10", "payee": "AUTOPAY VISA", "amount": "-75"})
    connection = sqlite3.connect(tmp_path / "test.db")
    lone_leg = connection.execute("SELECT id FROM transactions WHERE account_id=? AND transfer_id IS NULL", (checking_id,)).fetchone()[0]
    connection.close()
    created = client.post(f"/transactions/{lone_leg}/create-transfer", data={"csrf_token": csrf, "target_account_id": str(visa_id)}, follow_redirects=True)
    assert b"Created the matching transfer in Visa" in created.data
    connection = sqlite3.connect(tmp_path / "test.db")
    pair = connection.execute("SELECT account_id,amount_cents,transfer_id FROM transactions WHERE transfer_id=(SELECT transfer_id FROM transactions WHERE id=?) ORDER BY account_id", (lone_leg,)).fetchall()
    assert len(pair) == 2 and {row[1] for row in pair} == {-7500, 7500} and pair[0][2] == pair[1][2]
    connection.close()


def test_csv_import_mapping_review_and_commit(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Everyday", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    account_id = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    group_id = connection.execute("SELECT id FROM categories WHERE name='Everyday'").fetchone()[0]
    connection.close()
    client.post("/categories", data={"csrf_token": csrf, "name": "Groceries", "parent_id": group_id, "month": "2026-09"})
    upload = client.post("/imports/upload", data={"csrf_token": csrf, "account_id": str(account_id),
        "statement": (io.BytesIO(b"Date,Description,Amount,Memo\n09/01/2026,Market,-42.15,Food\n09/02/2026,Employer,500.00,Payday\n"), "statement.csv")},
        content_type="multipart/form-data", follow_redirects=False)
    assert upload.status_code == 302 and "/map" in upload.location
    batch_id = int(upload.location.split("/")[-2])
    mapped = client.post(f"/imports/{batch_id}/map", data={"csrf_token": csrf, "date_column": "Date", "payee_column": "Description",
        "amount_column": "Amount", "memo_column": "Memo"}, follow_redirects=True)
    assert b"Review before importing" in mapped.data and b"42.15" in mapped.data and b"$500.00" in mapped.data
    connection = sqlite3.connect(tmp_path / "test.db")
    grocery_id = connection.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()[0]
    import_rows = connection.execute("SELECT id,payee FROM import_rows ORDER BY id").fetchall()
    connection.close()
    selected = [str(row[0]) for row in import_rows]
    committed = client.post(f"/imports/{batch_id}/commit", data={"csrf_token": csrf, "selected": selected}, follow_redirects=True)
    assert b"Imported 2 transactions" in committed.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (account_id,)).fetchone()[0] == 2
    transaction_id, amount, category = connection.execute("SELECT id,amount_cents,category_id FROM transactions WHERE payee='Market'").fetchone()
    assert amount == -4215 and category is None
    connection.close()
    register = client.get(f"/accounts/{account_id}")
    assert b'aria-label="Category for Market"' in register.data
    client.post(f"/transactions/{transaction_id}/category", data={"csrf_token": csrf, "category_id": str(grocery_id)})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT category_id FROM transactions WHERE id=?", (transaction_id,)).fetchone()[0] == grocery_id
    connection.close()
    client.post("/transactions", data={"csrf_token": csrf, "account_id": account_id, "occurred_on": "2026-09-03",
        "payee": "Market", "amount": "-12.00", "category_id": ""})
    connection = sqlite3.connect(tmp_path / "test.db")
    new_transaction_id = connection.execute("SELECT id FROM transactions WHERE payee='Market' ORDER BY id DESC").fetchone()[0]
    connection.close()
    inbox = client.get("/uncategorized")
    assert b"Everyday" in inbox.data and b"Groceries" in inbox.data
    assert b"Uncategorized <b>2</b>" in inbox.data
    assigned = client.post("/uncategorized/assign", data={"csrf_token": csrf, "selected": str(new_transaction_id),
        "category_id": str(grocery_id)}, follow_redirects=True)
    assert b"Categorized 1 transaction" in assigned.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT category_id FROM transactions WHERE id=?", (new_transaction_id,)).fetchone()[0] == grocery_id
    connection.close()


def test_move_money_move_category_and_delete(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Fun Money", "month": "2026-09"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Goals", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    fun_id, goals_id = [row[0] for row in connection.execute("SELECT id FROM categories ORDER BY id")]
    connection.close()
    client.post("/categories", data={"csrf_token": csrf, "name": "Eating Out", "parent_id": fun_id, "month": "2026-09"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Spain", "parent_id": goals_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    eating_id, spain_id = [row[0] for row in connection.execute("SELECT id FROM categories WHERE parent_id IS NOT NULL ORDER BY id")]
    connection.close()
    client.post(f"/categories/{eating_id}/assignment", data={"csrf_token": csrf, "assigned": "300", "month": "2026-09"})
    client.post(f"/categories/{spain_id}/assignment", data={"csrf_token": csrf, "assigned": "100", "month": "2026-09"})
    moved = client.post("/money/move", data={"csrf_token": csrf, "source_id": eating_id, "target_id": spain_id, "amount": "50", "month": "2026-09"}, follow_redirects=True)
    assert b"$600.00" in moved.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assignments = dict(connection.execute("SELECT category_id,assigned_cents FROM category_assignments WHERE month='2026-09'"))
    assert assignments == {eating_id: 25000, spain_id: 15000}
    connection.close()

    client.post(f"/categories/{eating_id}/move", data={"csrf_token": csrf, "target_id": goals_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT parent_id FROM categories WHERE id=?", (eating_id,)).fetchone()[0] == goals_id
    connection.close()
    client.post(f"/categories/{spain_id}/delete", data={"csrf_token": csrf, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT 1 FROM categories WHERE id=?", (spain_id,)).fetchone() is None
    connection.close()


def test_envelope_and_credit_card_invariants(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/accounts", data={"csrf_token": csrf, "name": "Visa", "type": "credit", "balance": "-200"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Everyday", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    connection.row_factory = sqlite3.Row
    checking = connection.execute("SELECT * FROM accounts WHERE name='Checking'").fetchone()
    visa = connection.execute("SELECT * FROM accounts WHERE name='Visa'").fetchone()
    group_id = connection.execute("SELECT id FROM categories WHERE name='Everyday'").fetchone()[0]
    connection.close()
    client.post("/categories", data={"csrf_token": csrf, "name": "Groceries", "parent_id": group_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    grocery_id = connection.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()[0]
    payment_id = connection.execute("SELECT id FROM categories WHERE credit_account_id=?", (visa["id"],)).fetchone()[0]
    connection.close()
    client.post(f"/categories/{grocery_id}/assignment", data={"csrf_token": csrf, "assigned": "300", "month": "2026-09"})

    card_purchase = client.post("/transactions", data={"csrf_token": csrf, "account_id": visa["id"], "category_id": grocery_id,
        "occurred_on": "2026-09-10", "payee": "Market", "amount": "-50"}, follow_redirects=True)
    assert b"$700.00" in card_purchase.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT SUM(amount_cents) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 5000
    assert connection.execute("SELECT starting_balance_cents+COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE account_id=accounts.id),0) FROM accounts WHERE id=?", (visa["id"],)).fetchone()[0] == -25000
    card_transaction_id = connection.execute("SELECT id FROM transactions WHERE account_id=? AND payee='Market'", (visa["id"],)).fetchone()[0]
    connection.close()

    register = client.get(f"/accounts/{visa['id']}")
    assert register.status_code == 200 and b"Possible duplicate" not in register.data
    client.post(f"/transactions/{card_transaction_id}/edit", data={"csrf_token": csrf, "occurred_on": "2026-09-10",
        "payee": "Market", "category_id": grocery_id, "amount": "-40", "cleared": "0"})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT SUM(amount_cents) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 4000
    connection.close()
    client.post(f"/transactions/{card_transaction_id}/delete", data={"csrf_token": csrf})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT COUNT(*) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 0
    connection.close()
    client.post("/transactions", data={"csrf_token": csrf, "account_id": visa["id"], "category_id": grocery_id,
        "occurred_on": "2026-09-10", "payee": "Market", "amount": "-50"})

    payment = client.post("/transfers", data={"csrf_token": csrf, "source_account_id": checking["id"], "target_account_id": visa["id"],
        "occurred_on": "2026-09-15", "amount": "50"}, follow_redirects=True)
    assert b"$700.00" in payment.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT SUM(amount_cents) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 0
    balances = dict(connection.execute("SELECT name,starting_balance_cents+COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE account_id=accounts.id),0) FROM accounts"))
    assert balances == {"Checking": 95000, "Visa": -20000}
    assert connection.execute("SELECT COUNT(DISTINCT transfer_id) FROM transactions WHERE transfer_id IS NOT NULL").fetchone()[0] == 1
    transfer_leg = connection.execute("SELECT id FROM transactions WHERE transfer_id IS NOT NULL LIMIT 1").fetchone()[0]
    connection.close()
    client.post(f"/transactions/{transfer_leg}/delete", data={"csrf_token": csrf})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE transfer_id IS NOT NULL").fetchone()[0] == 0
    assert connection.execute("SELECT SUM(amount_cents) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 5000
    connection.close()


def test_unfunded_card_spending_does_not_create_payment_cash(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/accounts", data={"csrf_token": csrf, "name": "Visa", "type": "credit", "balance": "0"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Everyday", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    visa_id = connection.execute("SELECT id FROM accounts WHERE name='Visa'").fetchone()[0]
    group_id = connection.execute("SELECT id FROM categories WHERE name='Everyday'").fetchone()[0]
    connection.close()
    client.post("/categories", data={"csrf_token": csrf, "name": "Groceries", "parent_id": group_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    grocery_id = connection.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()[0]
    payment_id = connection.execute("SELECT id FROM categories WHERE credit_account_id=?", (visa_id,)).fetchone()[0]
    connection.close()
    client.post(f"/categories/{grocery_id}/assignment", data={"csrf_token": csrf, "assigned": "10", "month": "2026-09"})
    response = client.post("/transactions", data={"csrf_token": csrf, "account_id": visa_id, "category_id": grocery_id,
        "occurred_on": "2026-09-10", "payee": "Market", "amount": "-20"}, follow_redirects=True)
    assert b"$990.00" in response.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT SUM(amount_cents) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 1000
    connection.close()


def test_duplicate_warning_and_reconciliation_lock(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "100"})
    connection = sqlite3.connect(tmp_path / "test.db")
    account_id = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    connection.close()
    payload = {"csrf_token": csrf, "account_id": account_id, "occurred_on": "2026-09-10", "payee": "Cafe", "amount": "-10"}
    client.post("/transactions", data=payload)
    client.post("/transactions", data=payload)
    register = client.get(f"/accounts/{account_id}")
    assert b"Possible duplicate" in register.data
    connection = sqlite3.connect(tmp_path / "test.db")
    transaction_ids = [row[0] for row in connection.execute("SELECT id FROM transactions ORDER BY id")]
    connection.close()
    for transaction_id in transaction_ids:
        client.post(f"/transactions/{transaction_id}/status", data={"csrf_token": csrf})
    reconciled = client.post(f"/accounts/{account_id}/reconcile", data={"csrf_token": csrf, "statement_balance": "80"}, follow_redirects=True)
    assert b"Account reconciled" in reconciled.data
    client.post(f"/transactions/{transaction_ids[0]}/delete", data={"csrf_token": csrf})
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE cleared=2").fetchone()[0] == 2
    connection.close()


def test_split_cash_and_credit_transactions_preserve_envelope_invariants(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "1000"})
    client.post("/accounts", data={"csrf_token": csrf, "name": "Visa", "type": "credit", "balance": "0"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Everyday", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    checking_id = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    visa_id = connection.execute("SELECT id FROM accounts WHERE name='Visa'").fetchone()[0]
    group_id = connection.execute("SELECT id FROM categories WHERE name='Everyday'").fetchone()[0]
    connection.close()
    for name in ("Groceries", "Household"):
        client.post("/categories", data={"csrf_token": csrf, "name": name, "parent_id": group_id, "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    grocery_id = connection.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()[0]
    household_id = connection.execute("SELECT id FROM categories WHERE name='Household'").fetchone()[0]
    payment_id = connection.execute("SELECT id FROM categories WHERE credit_account_id=?", (visa_id,)).fetchone()[0]
    connection.close()
    client.post(f"/categories/{grocery_id}/assignment", data={"csrf_token": csrf, "assigned": "100", "month": "2026-09"})
    client.post(f"/categories/{household_id}/assignment", data={"csrf_token": csrf, "assigned": "50", "month": "2026-09"})

    client.post("/transactions", data={"csrf_token": csrf, "account_id": checking_id, "occurred_on": "2026-09-05", "payee": "Superstore", "amount": "-60"})
    connection = sqlite3.connect(tmp_path / "test.db")
    cash_transaction = connection.execute("SELECT id FROM transactions WHERE account_id=?", (checking_id,)).fetchone()[0]
    connection.close()
    cash_split = client.post(f"/transactions/{cash_transaction}/split", data={"csrf_token": csrf,
        "split_category_id": [str(grocery_id), str(household_id)], "split_amount": ["40", "20"], "split_memo": ["Food", "Supplies"]}, follow_redirects=True)
    assert b"Transaction split updated" in cash_split.data
    dashboard = client.get("/?month=2026-09")
    assert b"$850.00" in dashboard.data

    client.post("/transactions", data={"csrf_token": csrf, "account_id": visa_id, "occurred_on": "2026-09-10", "payee": "Warehouse", "amount": "-80"})
    connection = sqlite3.connect(tmp_path / "test.db")
    card_transaction = connection.execute("SELECT id FROM transactions WHERE account_id=?", (visa_id,)).fetchone()[0]
    connection.close()
    card_split = client.post(f"/transactions/{card_transaction}/split", data={"csrf_token": csrf,
        "split_category_id": [str(grocery_id), str(household_id)], "split_amount": ["50", "30"], "split_memo": ["Food", "Home"]}, follow_redirects=True)
    assert b"Transaction split updated" in card_split.data
    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT SUM(amount_cents) FROM transaction_splits WHERE transaction_id=?", (card_transaction,)).fetchone()[0] == -8000
    assert connection.execute("SELECT SUM(amount_cents) FROM category_activity WHERE category_id=?", (payment_id,)).fetchone()[0] == 8000
    connection.close()
    dashboard = client.get("/?month=2026-09")
    assert b"$850.00" in dashboard.data
