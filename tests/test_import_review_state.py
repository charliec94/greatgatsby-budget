import io
import sqlite3

from test_app import make_client, token


def prepared_import(tmp_path):
    client = make_client(tmp_path)
    client.post("/setup", data={"csrf_token": token(client), "name": "Charlie", "email": "charlie@example.com", "password": "a-strong-password"})
    csrf = token(client, "/")
    client.post("/accounts", data={"csrf_token": csrf, "name": "Checking", "type": "checking", "balance": "0"})
    client.post("/categories", data={"csrf_token": csrf, "name": "Everyday", "month": "2026-09"})
    connection = sqlite3.connect(tmp_path / "test.db")
    account_id = connection.execute("SELECT id FROM accounts WHERE name='Checking'").fetchone()[0]
    group_id = connection.execute("SELECT id FROM categories WHERE name='Everyday'").fetchone()[0]
    connection.close()
    client.post("/categories", data={"csrf_token": csrf, "name": "Groceries", "parent_id": group_id, "month": "2026-09"})
    upload = client.post("/imports/upload", data={"csrf_token": csrf, "account_id": str(account_id),
        "statement": (io.BytesIO(b"Date,Description,Amount\n09/01/2026,Market,-42.15\n09/02/2026,Employer,500.00\n"), "statement.csv")},
        content_type="multipart/form-data")
    batch_id = int(upload.location.split("/")[-2])
    client.post(f"/imports/{batch_id}/map", data={"csrf_token": csrf, "date_column": "Date", "payee_column": "Description", "amount_column": "Amount"})
    connection = sqlite3.connect(tmp_path / "test.db")
    rows = dict(connection.execute("SELECT payee,id FROM import_rows WHERE batch_id=?", (batch_id,)).fetchall())
    grocery_id = connection.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()[0]
    connection.close()
    return client, csrf, account_id, group_id, grocery_id, batch_id, rows


def test_import_review_autosaves_ready_to_assign_and_allows_partial_commit(tmp_path):
    client, csrf, account_id, group_id, grocery_id, batch_id, rows = prepared_import(tmp_path)
    review = client.get(f"/imports/{batch_id}/review")
    assert b'Ready to Assign</option>' in review.data
    assert b'value="ready_to_assign" selected' in review.data

    saved = client.post(f"/imports/{batch_id}/autosave", data={"csrf_token": csrf, "row_id": rows["Market"],
        "selected": "1", "category": str(grocery_id)})
    assert saved.status_code == 204
    saved = client.post(f"/imports/{batch_id}/autosave", data={"csrf_token": csrf, "row_id": rows["Employer"],
        "selected": "0", "category": "ready_to_assign"})
    assert saved.status_code == 204

    client.post("/categories", data={"csrf_token": csrf, "name": "New after review", "parent_id": group_id, "month": "2026-09"})
    reopened = client.get(f"/imports/{batch_id}/review")
    assert f'name="category_{rows["Market"]}"'.encode() in reopened.data
    assert f'value="{grocery_id}" selected'.encode() in reopened.data
    assert f'name="selected" value="{rows["Employer"]}" checked'.encode() not in reopened.data
    assert b"New after review" in reopened.data

    first = client.post(f"/imports/{batch_id}/commit", data={"csrf_token": csrf, "selected": str(rows["Market"]),
        f"category_{rows['Market']}": str(grocery_id), f"category_{rows['Employer']}": "ready_to_assign"}, follow_redirects=True)
    assert b"Imported 1 transaction" in first.data
    assert b"Already imported" in first.data
    assert b"This statement has already been imported" not in first.data

    second = client.post(f"/imports/{batch_id}/commit", data={"csrf_token": csrf, "selected": str(rows["Employer"]),
        f"category_{rows['Employer']}": "ready_to_assign"}, follow_redirects=True)
    assert b"Imported 1 transaction" in second.data
    assert b"This statement has already been imported" not in second.data

    connection = sqlite3.connect(tmp_path / "test.db")
    assert connection.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (account_id,)).fetchone()[0] == 2
    assert connection.execute("SELECT ready_to_assign,category_id FROM transactions WHERE payee='Employer'").fetchone() == (1, None)
    assert connection.execute("SELECT category_id FROM transactions WHERE payee='Market'").fetchone()[0] == grocery_id
    assert connection.execute("SELECT status FROM import_batches WHERE id=?", (batch_id,)).fetchone()[0] == "imported"
    assert connection.execute("SELECT COUNT(*) FROM import_rows WHERE batch_id=? AND imported=1", (batch_id,)).fetchone()[0] == 2
    connection.close()

    register = client.get(f"/accounts/{account_id}")
    assert b"Ready to Assign" in register.data
    inbox = client.get("/uncategorized")
    assert b"Employer" not in inbox.data


def test_autosave_rejects_ready_to_assign_for_outflow(tmp_path):
    client, csrf, _account_id, _group_id, _grocery_id, batch_id, rows = prepared_import(tmp_path)
    response = client.post(f"/imports/{batch_id}/autosave", data={"csrf_token": csrf, "row_id": rows["Market"],
        "selected": "1", "category": "ready_to_assign"})
    assert response.status_code == 400
