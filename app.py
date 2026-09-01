import os
import secrets
import sqlite3
import csv
import io
import json
import math
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("APP_DATABASE", str(Path("data/greatgatsby.db").resolve())),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    def db():
        if "db" not in g:
            Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def init_db():
        db().executescript(SCHEMA)
        needs_credit_rebuild = False
        columns = {row["name"] for row in db().execute("PRAGMA table_info(categories)")}
        if "hidden" not in columns:
            db().execute("ALTER TABLE categories ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        if "kind" not in columns:
            db().execute("ALTER TABLE categories ADD COLUMN kind TEXT NOT NULL DEFAULT 'spending'")
            needs_credit_rebuild = True
        if "credit_account_id" not in columns:
            db().execute("ALTER TABLE categories ADD COLUMN credit_account_id INTEGER REFERENCES accounts(id)")
        account_columns = {row["name"] for row in db().execute("PRAGMA table_info(accounts)")}
        if "starting_balance_cents" not in account_columns:
            db().execute("ALTER TABLE accounts ADD COLUMN starting_balance_cents INTEGER NOT NULL DEFAULT 0")
            db().execute(
                """UPDATE accounts SET starting_balance_cents=balance_cents-
                   COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE account_id=accounts.id),0)"""
            )
        transaction_columns = {row["name"] for row in db().execute("PRAGMA table_info(transactions)")}
        if "transfer_id" not in transaction_columns:
            db().execute("ALTER TABLE transactions ADD COLUMN transfer_id TEXT")
            needs_credit_rebuild = True
        db().execute(
            """INSERT OR IGNORE INTO category_assignments(category_id,month,assigned_cents)
               SELECT id, strftime('%Y-%m','now'), assigned_cents FROM categories
               WHERE parent_id IS NOT NULL AND assigned_cents != 0"""
        )
        for budget in db().execute("SELECT id FROM budgets").fetchall():
            credit_accounts = db().execute("SELECT id,name FROM accounts WHERE budget_id=? AND type='credit'", (budget["id"],)).fetchall()
            if not credit_accounts:
                continue
            group = db().execute("SELECT id FROM categories WHERE budget_id=? AND kind='credit_group'", (budget["id"],)).fetchone()
            if not group:
                cursor = db().execute(
                    "INSERT INTO categories(budget_id,parent_id,name,assigned_cents,kind) VALUES(?,NULL,'Credit Card Payments',0,'credit_group')",
                    (budget["id"],),
                )
                group_id = cursor.lastrowid
            else:
                group_id = group["id"]
            for account in credit_accounts:
                if not db().execute("SELECT 1 FROM categories WHERE credit_account_id=?", (account["id"],)).fetchone():
                    db().execute(
                        "INSERT INTO categories(budget_id,parent_id,name,assigned_cents,kind,credit_account_id) VALUES(?,?,?,0,'credit_payment',?)",
                        (budget["id"], group_id, f"{account['name']} Payment", account["id"]),
                    )
            if needs_credit_rebuild:
                rebuild_credit_activity(budget["id"])
        db().commit()

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def protect_csrf():
        init_db()
        if request.method == "POST" and not secrets.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            abort(400, "Invalid form token")

    def login_required(view):
        @wraps(view)
        def wrapped(**kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            return view(**kwargs)
        return wrapped

    def current_budget():
        return db().execute(
            "SELECT * FROM budgets WHERE owner_id = ? ORDER BY id LIMIT 1",
            (session["user_id"],),
        ).fetchone()

    @app.context_processor
    def sidebar_counts():
        if not session.get("user_id"):
            return {"uncategorized_count": 0}
        budget = current_budget()
        if not budget:
            return {"uncategorized_count": 0}
        count = db().execute(
            """SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE a.budget_id=? AND t.category_id IS NULL AND t.transfer_id IS NULL
               AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)""",
            (budget["id"],),
        ).fetchone()[0]
        return {"uncategorized_count": count}

    def remember_payee_category(budget_id, payee, category_id):
        if not category_id or not payee.strip():
            return
        db().execute(
            """INSERT INTO payee_category_rules(budget_id,payee_key,display_name,category_id) VALUES(?,?,?,?)
               ON CONFLICT(budget_id,payee_key) DO UPDATE SET display_name=excluded.display_name,category_id=excluded.category_id""",
            (budget_id, " ".join(payee.lower().split()), payee.strip(), category_id),
        )

    def selected_month(value):
        try:
            chosen = datetime.strptime(value or "", "%Y-%m").date().replace(day=1)
        except ValueError:
            chosen = date.today().replace(day=1)
        previous = (chosen - timedelta(days=1)).replace(day=1)
        following = date(chosen.year + (chosen.month == 12), 1 if chosen.month == 12 else chosen.month + 1, 1)
        return chosen, previous, following

    def sidebar_accounts(budget_id):
        return db().execute(
            """SELECT a.*,a.starting_balance_cents+COALESCE((SELECT SUM(t.amount_cents) FROM transactions t WHERE t.account_id=a.id),0) computed_balance
               FROM accounts a WHERE a.budget_id=? ORDER BY a.type='credit',a.name""", (budget_id,)
        ).fetchall()

    def parse_import_date(value):
        value = (value or "").strip()
        for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y"):
            try:
                return datetime.strptime(value, pattern).date().isoformat()
            except ValueError:
                pass
        raise ValueError(f"Unrecognized date: {value}")

    def parse_import_money(value):
        cleaned = (value or "").strip().replace("$", "").replace(",", "")
        if not cleaned:
            return 0
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = f"-{cleaned[1:-1]}"
        return money(cleaned)

    def target_metrics(target_type, target_amount, target_date, assigned, carried, activity, month):
        available = carried + assigned + activity
        if not target_type or not target_amount:
            return 0, 0
        if target_type == "monthly":
            needed = max(target_amount - assigned, 0)
            progress_value = assigned
        elif target_type == "balance":
            needed = max(target_amount - available, 0)
            progress_value = available
        else:
            try:
                deadline = date.fromisoformat(target_date).replace(day=1)
            except (TypeError, ValueError):
                deadline = month
            months = max((deadline.year - month.year) * 12 + deadline.month - month.month + 1, 1)
            monthly_plan = math.ceil(max(target_amount - (carried + activity), 0) / months)
            needed = max(monthly_plan - assigned, 0)
            progress_value = available
        progress = min(max(round(progress_value * 100 / target_amount), 0), 100)
        return needed, progress

    def category_available(category_id, through_date):
        month_key = through_date.strftime("%Y-%m")
        cutoff = through_date.isoformat()
        return db().execute(
            """SELECT COALESCE((SELECT SUM(assigned_cents) FROM category_assignments
                                 WHERE category_id=? AND month<=?),0)
                      + COALESCE((SELECT SUM(amount_cents) FROM transactions
                                  WHERE category_id=? AND occurred_on<=?),0)
                      + COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
                                  WHERE s.category_id=? AND t.occurred_on<=?),0)
                      + COALESCE((SELECT SUM(amount_cents) FROM category_activity
                                  WHERE category_id=? AND occurred_on<=?),0)""",
            (category_id, month_key, category_id, cutoff, category_id, cutoff, category_id, cutoff),
        ).fetchone()[0]

    def payment_category(account_id):
        return db().execute("SELECT * FROM categories WHERE credit_account_id=? AND kind='credit_payment'", (account_id,)).fetchone()

    def category_available_before(category_id, occurred_on, transaction_id):
        month_key = occurred_on[:7]
        return db().execute(
            """SELECT COALESCE((SELECT SUM(assigned_cents) FROM category_assignments
                                 WHERE category_id=? AND month<=?),0)
                      + COALESCE((SELECT SUM(amount_cents) FROM transactions
                                  WHERE category_id=? AND (occurred_on<? OR (occurred_on=? AND id<?))),0)
                      + COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
                                  WHERE s.category_id=? AND (t.occurred_on<? OR (t.occurred_on=? AND t.id<?))),0)
                      + COALESCE((SELECT SUM(amount_cents) FROM category_activity
                                  WHERE category_id=? AND (occurred_on<? OR (occurred_on=? AND COALESCE(transaction_id,0)<?))),0)""",
            (category_id, month_key, category_id, occurred_on, occurred_on, transaction_id,
             category_id, occurred_on, occurred_on, transaction_id,
             category_id, occurred_on, occurred_on, transaction_id),
        ).fetchone()[0]

    def rebuild_credit_activity(budget_id):
        db().execute("DELETE FROM category_activity WHERE category_id IN (SELECT id FROM categories WHERE budget_id=?)", (budget_id,))
        transactions = db().execute(
            """SELECT t.*,a.type account_type FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE a.budget_id=? ORDER BY t.occurred_on,t.id""",
            (budget_id,),
        ).fetchall()
        for transaction in transactions:
            if transaction["account_type"] != "credit":
                continue
            payment = payment_category(transaction["account_id"])
            if not payment:
                continue
            splits = db().execute("SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id", (transaction["id"],)).fetchall()
            if transaction["transfer_id"] and transaction["amount_cents"] > 0:
                available = max(category_available_before(payment["id"], transaction["occurred_on"], transaction["id"]), 0)
                amount = -min(transaction["amount_cents"], available)
                kind = "card_payment"
            elif splits and transaction["amount_cents"] < 0:
                reserved_by_category = {}
                for split in splits:
                    available = max(category_available_before(split["category_id"], transaction["occurred_on"], transaction["id"])
                                    - reserved_by_category.get(split["category_id"], 0), 0)
                    reserved = min(-split["amount_cents"], available)
                    reserved_by_category[split["category_id"]] = reserved_by_category.get(split["category_id"], 0) + reserved
                    if reserved:
                        db().execute(
                            "INSERT INTO category_activity(category_id,transaction_id,occurred_on,amount_cents,kind) VALUES(?,?,?,?,?)",
                            (payment["id"], transaction["id"], transaction["occurred_on"], reserved, "card_reservation"),
                        )
                continue
            elif splits and transaction["amount_cents"] > 0:
                available = max(category_available_before(payment["id"], transaction["occurred_on"], transaction["id"]), 0)
                amount = -min(transaction["amount_cents"], available)
                kind = "card_refund"
            elif transaction["category_id"] and transaction["amount_cents"] < 0:
                available = max(category_available_before(transaction["category_id"], transaction["occurred_on"], transaction["id"]), 0)
                amount = min(-transaction["amount_cents"], available)
                kind = "card_reservation"
            elif transaction["category_id"] and transaction["amount_cents"] > 0:
                available = max(category_available_before(payment["id"], transaction["occurred_on"], transaction["id"]), 0)
                amount = -min(transaction["amount_cents"], available)
                kind = "card_refund"
            else:
                continue
            if amount:
                db().execute(
                    "INSERT INTO category_activity(category_id,transaction_id,occurred_on,amount_cents,kind) VALUES(?,?,?,?,?)",
                    (payment["id"], transaction["id"], transaction["occurred_on"], amount, kind),
                )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/setup", methods=("GET", "POST"))
    def setup():
        if db().execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return redirect(url_for("login"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or "@" not in email or len(password) < 10:
                flash("Enter a name, valid email, and password of at least 10 characters.", "error")
            else:
                cursor = db().execute(
                    "INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                    (name, email, generate_password_hash(password), "owner"),
                )
                db().execute(
                    "INSERT INTO budgets(owner_id,name,currency) VALUES(?,?,?)",
                    (cursor.lastrowid, "My GreatGatsby Budget", "USD"),
                )
                db().commit()
                session.clear()
                session["user_id"] = cursor.lastrowid
                return redirect(url_for("dashboard"))
        return render_template("setup.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if not db().execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return redirect(url_for("setup"))
        if request.method == "POST":
            user = db().execute("SELECT * FROM users WHERE email = ?", (request.form.get("email", "").strip().lower(),)).fetchone()
            if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                flash("Incorrect email or password.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        budget = current_budget()
        month, previous_month, next_month = selected_month(request.args.get("month"))
        show_hidden = request.args.get("show_hidden") == "1"
        accounts = db().execute(
            """SELECT a.*, a.starting_balance_cents+
               COALESCE((SELECT SUM(t.amount_cents) FROM transactions t WHERE t.account_id=a.id),0) computed_balance
               FROM accounts a WHERE a.budget_id=? ORDER BY a.type='credit',a.name""",
            (budget["id"],),
        ).fetchall()
        rows = db().execute(
            """SELECT c.*, p.name parent_name,ct.target_type,ct.amount_cents target_amount,ct.target_date,ct.due_day,
               COALESCE(ca.assigned_cents,0) assigned,
               COALESCE((SELECT SUM(amount_cents) FROM transactions t
                         WHERE t.category_id=c.id AND t.occurred_on>=? AND t.occurred_on<?),0)
               + COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions st ON st.id=s.transaction_id
                           WHERE s.category_id=c.id AND st.occurred_on>=? AND st.occurred_on<?),0)
               + COALESCE((SELECT SUM(amount_cents) FROM category_activity x
                           WHERE x.category_id=c.id AND x.occurred_on>=? AND x.occurred_on<?),0) activity
               ,COALESCE((SELECT SUM(ca2.assigned_cents) FROM category_assignments ca2
                          WHERE ca2.category_id=c.id AND ca2.month<?),0)
                + COALESCE((SELECT SUM(t2.amount_cents) FROM transactions t2
                            WHERE t2.category_id=c.id AND t2.occurred_on<?),0)
                + COALESCE((SELECT SUM(s2.amount_cents) FROM transaction_splits s2 JOIN transactions st2 ON st2.id=s2.transaction_id
                            WHERE s2.category_id=c.id AND st2.occurred_on<?),0)
                + COALESCE((SELECT SUM(x2.amount_cents) FROM category_activity x2
                            WHERE x2.category_id=c.id AND x2.occurred_on<?),0) carried
               FROM categories c LEFT JOIN categories p ON p.id=c.parent_id
               LEFT JOIN category_targets ct ON ct.category_id=c.id
               LEFT JOIN category_assignments ca ON ca.category_id=c.id AND ca.month=?
               WHERE c.budget_id=? ORDER BY c.id""",
            (month.isoformat(), next_month.isoformat(), month.isoformat(), next_month.isoformat(),
             month.isoformat(), next_month.isoformat(), month.strftime("%Y-%m"), month.isoformat(), month.isoformat(),
             month.isoformat(), month.strftime("%Y-%m"), budget["id"]),
        ).fetchall()
        category_rows = [dict(row) for row in rows]
        for row in category_rows:
            row["available"] = row["carried"] + row["assigned"] + row["activity"]
            row["underfunded"], row["target_progress"] = target_metrics(
                row["target_type"], row["target_amount"], row["target_date"], row["assigned"], row["carried"], row["activity"], month
            )
            row["funding_status"] = "overspent" if row["available"] < 0 else "underfunded" if row["underfunded"] else "funded" if row["target_type"] else ""
        children = {}
        for row in category_rows:
            if row["parent_id"]:
                children.setdefault(row["parent_id"], []).append(row)
        groups = []
        for group in (row for row in category_rows if row["parent_id"] is None):
            all_children = children.get(group["id"], [])
            group["assigned"] = sum(child["assigned"] for child in all_children)
            group["activity"] = sum(child["activity"] for child in all_children)
            group["carried"] = sum(child["carried"] for child in all_children)
            group["available"] = group["carried"] + group["assigned"] + group["activity"]
            group["children"] = [child for child in all_children if show_hidden or not child["hidden"]]
            if show_hidden or not group["hidden"]:
                groups.append(group)
        assignable_categories = [row for row in category_rows if row["parent_id"] and not row["hidden"]]
        spending_categories = [row for row in assignable_categories if row["kind"] == "spending"]
        transactions = db().execute(
            """SELECT t.*, a.name account_name, c.name category_name,
               (SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id=t.id) split_count FROM transactions t
               JOIN accounts a ON a.id=t.account_id LEFT JOIN categories c ON c.id=t.category_id
               WHERE a.budget_id=? ORDER BY t.occurred_on DESC,t.id DESC LIMIT 8""",
            (budget["id"],),
        ).fetchall()
        assigned = sum(row["assigned"] for row in category_rows if row["parent_id"])
        activity = sum(row["activity"] for row in category_rows if row["parent_id"])
        category_balances = [row["carried"] + row["assigned"] + row["activity"] for row in category_rows if row["parent_id"]]
        available = sum(max(balance, 0) for balance in category_balances)
        cash_balance = db().execute(
            """SELECT COALESCE(SUM(a.starting_balance_cents + COALESCE((SELECT SUM(t.amount_cents)
                       FROM transactions t WHERE t.account_id=a.id AND t.occurred_on<?),0)),0)
               FROM accounts a WHERE a.budget_id=? AND a.type!='credit'""",
            (next_month.isoformat(), budget["id"]),
        ).fetchone()[0]
        ready_to_assign = cash_balance - available
        for row in category_rows:
            row["quick_fund"] = min(row["underfunded"], max(ready_to_assign, 0))
        return render_template("dashboard.html", budget=budget, accounts=accounts, groups=groups,
                               assignable_categories=assignable_categories, spending_categories=spending_categories,
                               transactions=transactions,
                               assigned=assigned, activity=activity, available=available,
                               ready_to_assign=ready_to_assign, today=date.today(), month=month,
                               previous_month=previous_month, next_month=next_month,
                               show_hidden=show_hidden, hidden_count=sum(row["hidden"] for row in category_rows),
                               underfunded=sum(row["underfunded"] for row in category_rows if row["parent_id"]),
                               active_view="budget", active_account_id=None)

    @app.get("/accounts/<int:account_id>")
    @login_required
    def account_register(account_id):
        budget = current_budget()
        account = db().execute("SELECT * FROM accounts WHERE id=? AND budget_id=?", (account_id, budget["id"])).fetchone()
        if not account:
            abort(404)
        rows = db().execute(
            """SELECT t.*,c.name category_name,(SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id=t.id) split_count,
               (SELECT COUNT(*) FROM transactions d WHERE d.account_id=t.account_id AND d.id!=t.id
                AND d.occurred_on=t.occurred_on AND d.amount_cents=t.amount_cents AND lower(d.payee)=lower(t.payee)) duplicate_count
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               WHERE t.account_id=? ORDER BY t.occurred_on,t.id""",
            (account_id,),
        ).fetchall()
        running = account["starting_balance_cents"]
        transactions = []
        for row in rows:
            running += row["amount_cents"]
            item = dict(row)
            item["running_balance"] = running
            transactions.append(item)
        transactions.reverse()
        split_rows = db().execute(
            """SELECT s.*,c.name category_name,p.name parent_name FROM transaction_splits s
               JOIN transactions t ON t.id=s.transaction_id JOIN categories c ON c.id=s.category_id
               JOIN categories p ON p.id=c.parent_id WHERE t.account_id=? ORDER BY s.id""",
            (account_id,),
        ).fetchall()
        splits = {}
        for split in split_rows:
            splits.setdefault(split["transaction_id"], []).append(split)
        categories = db().execute(
            """SELECT c.id,c.name,p.name parent_name FROM categories c JOIN categories p ON p.id=c.parent_id
               WHERE c.budget_id=? AND c.kind='spending' AND c.hidden=0 ORDER BY p.name,c.name""",
            (budget["id"],),
        ).fetchall()
        accounts = db().execute(
            """SELECT a.*, a.starting_balance_cents+
               COALESCE((SELECT SUM(t.amount_cents) FROM transactions t WHERE t.account_id=a.id),0) computed_balance
               FROM accounts a WHERE a.budget_id=? ORDER BY a.type='credit',a.name""",
            (budget["id"],),
        ).fetchall()
        cleared_balance = account["starting_balance_cents"] + db().execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE account_id=? AND cleared>=1", (account_id,)
        ).fetchone()[0]
        current_balance = account["starting_balance_cents"] + sum(row["amount_cents"] for row in rows)
        return render_template("account.html", budget=budget, account=account, transactions=transactions, categories=categories,
                               accounts=accounts, splits=splits, cleared_balance=cleared_balance, current_balance=current_balance,
                               today=date.today(), active_view="account", active_account_id=account_id)

    @app.get("/imports")
    @login_required
    def imports():
        budget = current_budget()
        batches = db().execute("SELECT b.*,a.name account_name FROM import_batches b JOIN accounts a ON a.id=b.account_id WHERE b.budget_id=? ORDER BY b.id DESC LIMIT 10", (budget["id"],)).fetchall()
        return render_template("imports.html", budget=budget, accounts=sidebar_accounts(budget["id"]), batches=batches,
                               active_view="imports", active_account_id=None)

    @app.get("/uncategorized")
    @login_required
    def uncategorized():
        budget = current_budget()
        raw_rows = db().execute(
            """SELECT t.*,a.name account_name,a.type account_type,r.category_id suggested_category_id,c.name suggested_category_name,p.name suggested_parent_name
               FROM transactions t JOIN accounts a ON a.id=t.account_id
               LEFT JOIN payee_category_rules r ON r.budget_id=a.budget_id AND r.payee_key=lower(trim(t.payee))
               LEFT JOIN categories c ON c.id=r.category_id LEFT JOIN categories p ON p.id=c.parent_id
               WHERE a.budget_id=? AND t.category_id IS NULL AND t.transfer_id IS NULL
               AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)
               ORDER BY t.occurred_on DESC,t.id DESC""", (budget["id"],)
        ).fetchall()
        rows = [dict(row) for row in raw_rows]
        for row in rows:
            candidates = [candidate for candidate in rows if candidate["id"] != row["id"]
                          and candidate["account_id"] != row["account_id"]
                          and candidate["amount_cents"] == -row["amount_cents"]
                          and abs((date.fromisoformat(candidate["occurred_on"]) - date.fromisoformat(row["occurred_on"])).days) <= 3]
            candidates.sort(key=lambda candidate: (0 if "credit" in (row["account_type"], candidate["account_type"]) else 1,
                                                   abs((date.fromisoformat(candidate["occurred_on"]) - date.fromisoformat(row["occurred_on"])).days)))
            row["transfer_match"] = candidates[0] if candidates else None
        categories = db().execute("""SELECT c.id,c.name,p.name parent_name FROM categories c JOIN categories p ON p.id=c.parent_id
            WHERE c.budget_id=? AND c.kind='spending' AND c.hidden=0 ORDER BY p.name,c.name""", (budget["id"],)).fetchall()
        return render_template("uncategorized.html", budget=budget, accounts=sidebar_accounts(budget["id"]), rows=rows,
                               categories=categories, active_view="uncategorized", active_account_id=None)

    @app.post("/uncategorized/assign")
    @login_required
    def assign_uncategorized():
        budget = current_budget()
        category_id = request.form.get("category_id", type=int)
        selected = {int(value) for value in request.form.getlist("selected") if value.isdigit()}
        if not category_id or not db().execute("SELECT 1 FROM categories WHERE id=? AND budget_id=? AND parent_id IS NOT NULL AND kind='spending'", (category_id, budget["id"])).fetchone():
            flash("Choose a valid category.", "error")
            return redirect(url_for("uncategorized"))
        updated = 0
        for transaction_id in selected:
            transaction = db().execute("""SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id
                WHERE t.id=? AND a.budget_id=? AND t.category_id IS NULL AND t.transfer_id IS NULL AND t.cleared!=2
                AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)""", (transaction_id, budget["id"])).fetchone()
            if transaction:
                db().execute("UPDATE transactions SET category_id=? WHERE id=?", (category_id, transaction_id))
                remember_payee_category(budget["id"], transaction["payee"], category_id)
                updated += 1
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash(f"Categorized {updated} transaction{'s' if updated != 1 else ''}.", "success")
        return redirect(url_for("uncategorized"))

    @app.post("/transactions/<int:transaction_id>/match-transfer")
    @login_required
    def match_transfer(transaction_id):
        budget = current_budget()
        match_id = request.form.get("match_id", type=int)
        legs = db().execute(
            """SELECT t.*,a.name account_name,a.type account_type FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE t.id IN (?,?) AND a.budget_id=? AND t.category_id IS NULL AND t.transfer_id IS NULL AND t.cleared!=2
               AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)""",
            (transaction_id, match_id, budget["id"]),
        ).fetchall()
        if len(legs) != 2 or legs[0]["account_id"] == legs[1]["account_id"] or legs[0]["amount_cents"] != -legs[1]["amount_cents"]:
            flash("Those transactions cannot be matched as a transfer.", "error")
            return redirect(url_for("uncategorized"))
        transfer_id = secrets.token_urlsafe(12)
        by_id = {leg["id"]: leg for leg in legs}
        first, second = by_id[transaction_id], by_id[match_id]
        db().execute("UPDATE transactions SET transfer_id=?,payee=? WHERE id=?", (transfer_id, f"Transfer to {second['account_name']}" if first["amount_cents"] < 0 else f"Transfer from {second['account_name']}", first["id"]))
        db().execute("UPDATE transactions SET transfer_id=?,payee=? WHERE id=?", (transfer_id, f"Transfer to {first['account_name']}" if second["amount_cents"] < 0 else f"Transfer from {first['account_name']}", second["id"]))
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash("The two transactions are now linked as one transfer.", "success")
        return redirect(url_for("uncategorized"))

    @app.post("/transactions/<int:transaction_id>/create-transfer")
    @login_required
    def create_transfer_from_transaction(transaction_id):
        budget = current_budget()
        transaction = db().execute("""SELECT t.*,a.name account_name FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE t.id=? AND a.budget_id=? AND t.category_id IS NULL AND t.transfer_id IS NULL AND t.cleared!=2
            AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)""", (transaction_id, budget["id"])).fetchone()
        target_id = request.form.get("target_account_id", type=int)
        target = db().execute("SELECT * FROM accounts WHERE id=? AND budget_id=?", (target_id, budget["id"])).fetchone()
        if not transaction or not target or target["id"] == transaction["account_id"]:
            flash("Choose a different account for the other side of the transfer.", "error")
            return redirect(url_for("uncategorized"))
        transfer_id = secrets.token_urlsafe(12)
        db().execute("UPDATE transactions SET transfer_id=?,payee=? WHERE id=?", (transfer_id,
                     f"Transfer to {target['name']}" if transaction["amount_cents"] < 0 else f"Transfer from {target['name']}", transaction_id))
        db().execute("INSERT INTO transactions(account_id,category_id,occurred_on,payee,memo,amount_cents,cleared,transfer_id) VALUES(?,NULL,?,?,?,?,0,?)",
                     (target["id"], transaction["occurred_on"], f"Transfer from {transaction['account_name']}" if transaction["amount_cents"] < 0 else f"Transfer to {transaction['account_name']}",
                      transaction["memo"], -transaction["amount_cents"], transfer_id))
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash(f"Created the matching transfer in {target['name']}.", "success")
        return redirect(url_for("uncategorized"))

    @app.post("/imports/upload")
    @login_required
    def upload_import():
        budget = current_budget()
        account_id = request.form.get("account_id", type=int)
        account = db().execute("SELECT id FROM accounts WHERE id=? AND budget_id=?", (account_id, budget["id"])).fetchone()
        uploaded = request.files.get("statement")
        if not account or not uploaded or not uploaded.filename.lower().endswith(".csv"):
            flash("Choose an account and a CSV statement.", "error")
            return redirect(url_for("imports"))
        try:
            text = uploaded.read().decode("utf-8-sig")
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            headers = [header.strip() for header in (reader.fieldnames or []) if header and header.strip()]
            rows = [{str(key).strip(): (value or "").strip() for key, value in row.items() if key} for row in reader]
        except (UnicodeDecodeError, csv.Error):
            flash("That CSV could not be read. Export it as a UTF-8 CSV and try again.", "error")
            return redirect(url_for("imports"))
        if not headers or not rows:
            flash("The CSV contains no transaction rows.", "error")
            return redirect(url_for("imports"))
        cursor = db().execute("INSERT INTO import_batches(budget_id,account_id,filename,headers_json,rows_json,status) VALUES(?,?,?,?,?,'mapping')",
                              (budget["id"], account_id, uploaded.filename[:200], json.dumps(headers), json.dumps(rows[:5000])))
        db().commit()
        return redirect(url_for("map_import", batch_id=cursor.lastrowid))

    @app.route("/imports/<int:batch_id>/map", methods=("GET", "POST"))
    @login_required
    def map_import(batch_id):
        budget = current_budget()
        batch = db().execute("SELECT * FROM import_batches WHERE id=? AND budget_id=?", (batch_id, budget["id"])).fetchone()
        if not batch:
            abort(404)
        headers = json.loads(batch["headers_json"])
        source_rows = json.loads(batch["rows_json"])
        if request.method == "POST":
            date_column, payee_column = request.form.get("date_column"), request.form.get("payee_column")
            amount_column, outflow_column, inflow_column = request.form.get("amount_column"), request.form.get("outflow_column"), request.form.get("inflow_column")
            memo_column = request.form.get("memo_column")
            if date_column not in headers or payee_column not in headers or not any(column in headers for column in (amount_column, outflow_column, inflow_column)):
                flash("Map the date, payee, and either amount or inflow/outflow columns.", "error")
            else:
                parsed, errors = [], []
                for number, row in enumerate(source_rows, start=2):
                    try:
                        amount = parse_import_money(row.get(amount_column)) if amount_column in headers else parse_import_money(row.get(inflow_column)) - abs(parse_import_money(row.get(outflow_column)))
                        parsed.append((batch_id, parse_import_date(row.get(date_column)), (row.get(payee_column) or "Unknown")[:200],
                                       (row.get(memo_column) or "")[:500] if memo_column in headers else "", amount))
                    except (ValueError, TypeError):
                        errors.append(str(number))
                if errors:
                    flash(f"Could not read the date or amount on CSV row(s): {', '.join(errors[:8])}.", "error")
                else:
                    db().execute("DELETE FROM import_rows WHERE batch_id=?", (batch_id,))
                    db().executemany("INSERT INTO import_rows(batch_id,occurred_on,payee,memo,amount_cents) VALUES(?,?,?,?,?)", parsed)
                    db().execute("UPDATE import_batches SET status='review' WHERE id=?", (batch_id,))
                    db().commit()
                    return redirect(url_for("review_import", batch_id=batch_id))
        name_sets = {"date": ("date", "transaction date", "posted date"), "payee": ("payee", "description", "merchant", "name"), "amount": ("amount", "transaction amount"),
                     "outflow": ("outflow", "debit", "withdrawal"), "inflow": ("inflow", "credit", "deposit"), "memo": ("memo", "notes")}
        guesses = {field: next((header for header in headers if header.lower() in names), "") for field, names in name_sets.items()}
        return render_template("import_map.html", budget=budget, accounts=sidebar_accounts(budget["id"]), batch=batch, headers=headers,
                               sample=source_rows[0], guesses=guesses, active_view="imports", active_account_id=None)

    @app.get("/imports/<int:batch_id>/review")
    @login_required
    def review_import(batch_id):
        budget = current_budget()
        batch = db().execute("SELECT b.*,a.name account_name FROM import_batches b JOIN accounts a ON a.id=b.account_id WHERE b.id=? AND b.budget_id=?", (batch_id, budget["id"])).fetchone()
        if not batch:
            abort(404)
        rows = db().execute("""SELECT r.*,EXISTS(SELECT 1 FROM transactions t WHERE t.account_id=b.account_id AND t.occurred_on=r.occurred_on
            AND t.amount_cents=r.amount_cents AND lower(t.payee)=lower(r.payee)) duplicate,pr.category_id suggested_category_id,
            c.name suggested_category_name,p.name suggested_parent_name FROM import_rows r JOIN import_batches b ON b.id=r.batch_id
            LEFT JOIN payee_category_rules pr ON pr.budget_id=b.budget_id AND pr.payee_key=lower(trim(r.payee))
            LEFT JOIN categories c ON c.id=pr.category_id LEFT JOIN categories p ON p.id=c.parent_id
            WHERE r.batch_id=? ORDER BY r.occurred_on,r.id""", (batch_id,)).fetchall()
        categories = db().execute("""SELECT c.id,c.name,p.name parent_name FROM categories c JOIN categories p ON p.id=c.parent_id
            WHERE c.budget_id=? AND c.kind='spending' AND c.hidden=0 ORDER BY p.name,c.name""", (budget["id"],)).fetchall()
        return render_template("import_review.html", budget=budget, accounts=sidebar_accounts(budget["id"]), batch=batch,
                               rows=rows, categories=categories, active_view="imports", active_account_id=None)

    @app.post("/imports/<int:batch_id>/commit")
    @login_required
    def commit_import(batch_id):
        budget = current_budget()
        batch = db().execute("SELECT * FROM import_batches WHERE id=? AND budget_id=?", (batch_id, budget["id"])).fetchone()
        if not batch:
            abort(404)
        if batch["status"] == "imported":
            flash("This statement has already been imported.", "error")
            return redirect(url_for("account_register", account_id=batch["account_id"]))
        selected = {int(value) for value in request.form.getlist("selected") if value.isdigit()}
        imported = 0
        for row in db().execute("SELECT * FROM import_rows WHERE batch_id=?", (batch_id,)).fetchall():
            if row["id"] not in selected:
                continue
            category_id = request.form.get(f"category_{row['id']}", type=int)
            valid = category_id and db().execute("SELECT 1 FROM categories WHERE id=? AND budget_id=? AND parent_id IS NOT NULL AND kind='spending'", (category_id, budget["id"])).fetchone()
            db().execute("INSERT INTO transactions(account_id,category_id,occurred_on,payee,memo,amount_cents,cleared) VALUES(?,?,?,?,?,?,0)",
                         (batch["account_id"], category_id if valid else None, row["occurred_on"], row["payee"], row["memo"], row["amount_cents"]))
            if valid:
                remember_payee_category(budget["id"], row["payee"], category_id)
            imported += 1
        db().execute("UPDATE import_batches SET status='imported' WHERE id=?", (batch_id,))
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash(f"Imported {imported} transaction{'s' if imported != 1 else ''}.", "success")
        return redirect(url_for("account_register", account_id=batch["account_id"]))

    @app.post("/accounts")
    @login_required
    def add_account():
        budget = current_budget()
        name = request.form.get("name", "").strip()
        if not name:
            flash("Account name is required.", "error")
        else:
            account_type = request.form.get("type", "checking")
            balance = money(request.form.get("balance", "0"))
            db().execute("INSERT INTO accounts(budget_id,name,type,balance_cents,starting_balance_cents) VALUES(?,?,?,?,?)",
                         (budget["id"], name, account_type, balance, balance))
            db().commit()
            flash(f"Added {name}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/categories")
    @login_required
    def add_category():
        budget = current_budget()
        name = request.form.get("name", "").strip()
        parent_id = request.form.get("parent_id") or None
        if not name:
            flash("Category name is required.", "error")
        else:
            if parent_id:
                parent = db().execute("SELECT id FROM categories WHERE id=? AND budget_id=? AND parent_id IS NULL AND kind='spending'", (parent_id, budget["id"])).fetchone()
                if not parent:
                    abort(400, "Invalid category group")
            db().execute("INSERT INTO categories(budget_id,parent_id,name,assigned_cents) VALUES(?,?,?,0)",
                         (budget["id"], parent_id, name))
            db().commit()
            flash(f"Added {name}. Assign money from its monthly budget row." if parent_id else f"Added category group {name}.", "success")
        return redirect(url_for("dashboard", month=request.form.get("month")))

    @app.post("/categories/<int:category_id>/assignment")
    @login_required
    def set_assignment(category_id):
        budget = current_budget()
        category = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=?", (category_id, budget["id"])).fetchone()
        if not category or category["parent_id"] is None:
            abort(400, "Money can only be assigned to subcategories")
        month, _, _ = selected_month(request.form.get("month"))
        db().execute(
            """INSERT INTO category_assignments(category_id,month,assigned_cents) VALUES(?,?,?)
               ON CONFLICT(category_id,month) DO UPDATE SET assigned_cents=excluded.assigned_cents""",
            (category_id, month.strftime("%Y-%m"), money(request.form.get("assigned", "0"))),
        )
        db().commit()
        flash(f"Updated {category['name']} for {month.strftime('%B %Y')}.", "success")
        return redirect(url_for("dashboard", month=month.strftime("%Y-%m"), show_hidden=request.form.get("show_hidden") or None))

    @app.post("/categories/<int:category_id>/target")
    @login_required
    def set_category_target(category_id):
        budget = current_budget()
        category = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=? AND parent_id IS NOT NULL AND kind='spending'", (category_id, budget["id"])).fetchone()
        if not category:
            abort(404)
        target_type = request.form.get("target_type")
        amount = money(request.form.get("amount", "0"))
        target_date = request.form.get("target_date") or None
        due_day = request.form.get("due_day", type=int)
        if target_type not in ("monthly", "balance", "date") or amount <= 0 or (target_type == "date" and not target_date):
            flash("Choose a target type and enter a valid amount and date.", "error")
        else:
            if target_date:
                datetime.strptime(target_date, "%Y-%m-%d")
            due_day = due_day if due_day and 1 <= due_day <= 31 else None
            db().execute("""INSERT INTO category_targets(category_id,target_type,amount_cents,target_date,due_day) VALUES(?,?,?,?,?)
                ON CONFLICT(category_id) DO UPDATE SET target_type=excluded.target_type,amount_cents=excluded.amount_cents,
                target_date=excluded.target_date,due_day=excluded.due_day""", (category_id, target_type, amount, target_date, due_day))
            db().commit()
            flash(f"Target saved for {category['name']}.", "success")
        return redirect(url_for("dashboard", month=request.form.get("month")))

    @app.post("/categories/<int:category_id>/target/delete")
    @login_required
    def delete_category_target(category_id):
        budget = current_budget()
        db().execute("DELETE FROM category_targets WHERE category_id=? AND category_id IN (SELECT id FROM categories WHERE budget_id=?)", (category_id, budget["id"]))
        db().commit()
        flash("Target removed. The category and its money were not changed.", "success")
        return redirect(url_for("dashboard", month=request.form.get("month")))

    @app.post("/targets/fund-underfunded")
    @login_required
    def fund_underfunded():
        budget = current_budget()
        month, _, next_month = selected_month(request.form.get("month"))
        rows = db().execute("""SELECT c.id,ct.target_type,ct.amount_cents target_amount,ct.target_date,
            COALESCE((SELECT assigned_cents FROM category_assignments WHERE category_id=c.id AND month=?),0) assigned,
            COALESCE((SELECT SUM(assigned_cents) FROM category_assignments WHERE category_id=c.id AND month<?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on<?),0)
            +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id WHERE s.category_id=c.id AND t.occurred_on<?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on<?),0) carried,
            COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on>=? AND occurred_on<?),0)
            +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id WHERE s.category_id=c.id AND t.occurred_on>=? AND t.occurred_on<?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on>=? AND occurred_on<?),0) activity
            FROM categories c JOIN category_targets ct ON ct.category_id=c.id WHERE c.budget_id=? AND c.hidden=0 ORDER BY c.id""",
            (month.strftime("%Y-%m"), month.strftime("%Y-%m"), month.isoformat(), month.isoformat(), month.isoformat(),
             month.isoformat(), next_month.isoformat(), month.isoformat(), next_month.isoformat(), month.isoformat(), next_month.isoformat(), budget["id"])).fetchall()
        balances = db().execute("""SELECT c.id,COALESCE((SELECT SUM(assigned_cents) FROM category_assignments WHERE category_id=c.id AND month<=?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on<?),0)
            +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id WHERE s.category_id=c.id AND t.occurred_on<?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on<?),0) balance
            FROM categories c WHERE c.budget_id=? AND c.parent_id IS NOT NULL""", (month.strftime("%Y-%m"), next_month.isoformat(), next_month.isoformat(), next_month.isoformat(), budget["id"])).fetchall()
        available = sum(max(row["balance"], 0) for row in balances)
        cash = db().execute("""SELECT COALESCE(SUM(a.starting_balance_cents+COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE account_id=a.id AND occurred_on<?),0)),0)
            FROM accounts a WHERE a.budget_id=? AND a.type!='credit'""", (next_month.isoformat(), budget["id"])).fetchone()[0]
        remaining = max(cash - available, 0)
        funded = 0
        for row in rows:
            needed, _ = target_metrics(row["target_type"], row["target_amount"], row["target_date"], row["assigned"], row["carried"], row["activity"], month)
            addition = min(needed, remaining)
            if addition:
                db().execute("""INSERT INTO category_assignments(category_id,month,assigned_cents) VALUES(?,?,?)
                    ON CONFLICT(category_id,month) DO UPDATE SET assigned_cents=assigned_cents+excluded.assigned_cents""", (row["id"], month.strftime("%Y-%m"), addition))
                funded += addition
                remaining -= addition
        db().commit()
        flash(f"Assigned ${funded/100:,.2f} toward underfunded targets.", "success")
        return redirect(url_for("dashboard", month=month.strftime("%Y-%m")))

    @app.post("/categories/<int:category_id>/visibility")
    @login_required
    def category_visibility(category_id):
        budget = current_budget()
        category = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=?", (category_id, budget["id"])).fetchone()
        if not category:
            abort(404)
        hidden = 0 if category["hidden"] else 1
        db().execute("UPDATE categories SET hidden=? WHERE id=?", (hidden, category_id))
        db().commit()
        flash(f"{'Hidden' if hidden else 'Restored'} {category['name']}.", "success")
        return redirect(url_for("dashboard", month=request.form.get("month"), show_hidden=request.form.get("show_hidden") or None))

    @app.post("/categories/<int:category_id>/move")
    @login_required
    def move_category(category_id):
        budget = current_budget()
        category = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=?", (category_id, budget["id"])).fetchone()
        target_id = request.form.get("target_id")
        target = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=? AND parent_id IS NULL AND kind='spending'", (target_id, budget["id"])).fetchone()
        if not category or category["parent_id"] is None or not target:
            abort(400, "Only subcategories can be moved into category groups")
        db().execute("UPDATE categories SET parent_id=? WHERE id=?", (target["id"], category_id))
        db().commit()
        flash(f"Moved {category['name']} to {target['name']}.", "success")
        return redirect(url_for("dashboard", month=request.form.get("month"), show_hidden=request.form.get("show_hidden") or None))

    @app.post("/categories/<int:category_id>/delete")
    @login_required
    def delete_category(category_id):
        budget = current_budget()
        category = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=?", (category_id, budget["id"])).fetchone()
        if not category:
            abort(404)
        if category["parent_id"] is None and db().execute("SELECT 1 FROM categories WHERE parent_id=? LIMIT 1", (category_id,)).fetchone():
            flash("Move, archive, or delete this group's subcategories first.", "error")
        elif (db().execute("SELECT 1 FROM transactions WHERE category_id=? LIMIT 1", (category_id,)).fetchone()
              or db().execute("SELECT 1 FROM transaction_splits WHERE category_id=? LIMIT 1", (category_id,)).fetchone()):
            flash("This category has transactions and cannot be deleted. Archive it instead.", "error")
        else:
            db().execute("DELETE FROM category_assignments WHERE category_id=?", (category_id,))
            db().execute("DELETE FROM categories WHERE id=?", (category_id,))
            db().commit()
            flash(f"Deleted {category['name']}.", "success")
        return redirect(url_for("dashboard", month=request.form.get("month"), show_hidden=request.form.get("show_hidden") or None))

    @app.post("/money/move")
    @login_required
    def move_money():
        budget = current_budget()
        month, _, _ = selected_month(request.form.get("month"))
        source_id, target_id = request.form.get("source_id"), request.form.get("target_id")
        amount = money(request.form.get("amount", "0"))
        categories = db().execute(
            "SELECT id,name FROM categories WHERE budget_id=? AND parent_id IS NOT NULL AND id IN (?,?)",
            (budget["id"], source_id, target_id),
        ).fetchall()
        by_id = {str(row["id"]): row for row in categories}
        if amount <= 0 or source_id == target_id or source_id not in by_id or target_id not in by_id:
            flash("Choose two different subcategories and enter a positive amount.", "error")
            return redirect(url_for("dashboard", month=month.strftime("%Y-%m")))
        source_available = db().execute(
            """SELECT COALESCE((SELECT SUM(assigned_cents) FROM category_assignments WHERE category_id=? AND month<=?),0)
               + COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=? AND occurred_on<?),0)
               + COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=? AND occurred_on<?),0)""",
            (source_id, month.strftime("%Y-%m"), source_id,
             date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1).isoformat(),
             source_id, date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1).isoformat()),
        ).fetchone()[0]
        if source_available < amount:
            flash(f"{by_id[source_id]['name']} does not have enough available money.", "error")
            return redirect(url_for("dashboard", month=month.strftime("%Y-%m")))
        for category_id, adjustment in ((source_id, -amount), (target_id, amount)):
            db().execute(
                """INSERT INTO category_assignments(category_id,month,assigned_cents) VALUES(?,?,?)
                   ON CONFLICT(category_id,month) DO UPDATE SET assigned_cents=assigned_cents+excluded.assigned_cents""",
                (category_id, month.strftime("%Y-%m"), adjustment),
            )
        db().commit()
        flash(f"Moved ${amount/100:,.2f} from {by_id[source_id]['name']} to {by_id[target_id]['name']}.", "success")
        return redirect(url_for("dashboard", month=month.strftime("%Y-%m")))

    @app.post("/transactions")
    @login_required
    def add_transaction():
        amount = money(request.form.get("amount", "0"))
        account_id = request.form.get("account_id")
        payee = request.form.get("payee", "").strip()
        if not account_id or not payee or amount == 0:
            flash("Account, payee, and a non-zero amount are required.", "error")
        else:
            budget = current_budget()
            account = db().execute("SELECT * FROM accounts WHERE id=? AND budget_id=?", (account_id, budget["id"])).fetchone()
            category_id = request.form.get("category_id") or None
            category = db().execute("SELECT * FROM categories WHERE id=? AND budget_id=? AND parent_id IS NOT NULL AND kind='spending'", (category_id, budget["id"])).fetchone() if category_id else None
            if not account or (category_id and not category):
                abort(400, "Invalid account or category")
            occurred_on = request.form.get("occurred_on") or date.today().isoformat()
            datetime.strptime(occurred_on, "%Y-%m-%d")
            db().execute("INSERT INTO transactions(account_id,category_id,occurred_on,payee,memo,amount_cents,cleared) VALUES(?,?,?,?,?,?,?)",
                         (account_id, category_id, occurred_on, payee, request.form.get("memo", "").strip(), amount, 0))
            rebuild_credit_activity(budget["id"])
            db().commit()
            flash("Transaction added.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/transfers")
    @login_required
    def add_transfer():
        budget = current_budget()
        source_id, target_id = request.form.get("source_account_id"), request.form.get("target_account_id")
        amount = money(request.form.get("amount", "0"))
        occurred_on = request.form.get("occurred_on") or date.today().isoformat()
        accounts = db().execute("SELECT * FROM accounts WHERE budget_id=? AND id IN (?,?)", (budget["id"], source_id, target_id)).fetchall()
        by_id = {str(row["id"]): row for row in accounts}
        if amount <= 0 or source_id == target_id or source_id not in by_id or target_id not in by_id:
            flash("Choose two different accounts and enter a positive transfer amount.", "error")
            return redirect(url_for("dashboard"))
        transfer_id = secrets.token_urlsafe(12)
        source = db().execute(
            "INSERT INTO transactions(account_id,category_id,occurred_on,payee,memo,amount_cents,cleared,transfer_id) VALUES(?,NULL,?,?,?,?,0,?)",
            (source_id, occurred_on, f"Transfer to {by_id[target_id]['name']}", request.form.get("memo", "").strip(), -amount, transfer_id),
        )
        target = db().execute(
            "INSERT INTO transactions(account_id,category_id,occurred_on,payee,memo,amount_cents,cleared,transfer_id) VALUES(?,NULL,?,?,?,?,0,?)",
            (target_id, occurred_on, f"Transfer from {by_id[source_id]['name']}", request.form.get("memo", "").strip(), amount, transfer_id),
        )
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash(f"Transferred ${amount/100:,.2f} from {by_id[source_id]['name']} to {by_id[target_id]['name']}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/transactions/<int:transaction_id>/edit")
    @login_required
    def edit_transaction(transaction_id):
        budget = current_budget()
        transaction = db().execute(
            """SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE t.id=? AND a.budget_id=?""",
            (transaction_id, budget["id"]),
        ).fetchone()
        if not transaction:
            abort(404)
        if transaction["cleared"] == 2:
            flash("Reconciled transactions are locked. Unreconcile the account before editing them.", "error")
            return redirect(url_for("account_register", account_id=transaction["account_id"]))
        occurred_on = request.form.get("occurred_on") or transaction["occurred_on"]
        datetime.strptime(occurred_on, "%Y-%m-%d")
        status = min(max(int(request.form.get("cleared", transaction["cleared"])), 0), 2)
        if transaction["transfer_id"]:
            amount = abs(money(request.form.get("amount", "0")))
            if not amount:
                flash("Transfer amount must be greater than zero.", "error")
            else:
                legs = db().execute("SELECT * FROM transactions WHERE transfer_id=?", (transaction["transfer_id"],)).fetchall()
                for leg in legs:
                    signed_amount = -amount if leg["amount_cents"] < 0 else amount
                    db().execute("UPDATE transactions SET occurred_on=?,memo=?,amount_cents=?,cleared=? WHERE id=?",
                                 (occurred_on, request.form.get("memo", "").strip(), signed_amount, status, leg["id"]))
                rebuild_credit_activity(budget["id"])
                db().commit()
                flash("Transfer updated.", "success")
        else:
            amount = money(request.form.get("amount", "0"))
            split = db().execute("SELECT COUNT(*) count,COALESCE(SUM(amount_cents),0) total FROM transaction_splits WHERE transaction_id=?", (transaction_id,)).fetchone()
            category_id = None if split["count"] else request.form.get("category_id") or None
            if amount == 0:
                flash("Transaction amount cannot be zero.", "error")
            elif split["count"] and amount != split["total"]:
                flash("Edit the split lines before changing the total transaction amount.", "error")
            elif category_id and not db().execute(
                "SELECT 1 FROM categories WHERE id=? AND budget_id=? AND kind='spending' AND parent_id IS NOT NULL",
                (category_id, budget["id"]),
            ).fetchone():
                abort(400, "Invalid category")
            else:
                db().execute(
                    "UPDATE transactions SET occurred_on=?,payee=?,memo=?,amount_cents=?,category_id=?,cleared=? WHERE id=?",
                    (occurred_on, request.form.get("payee", "").strip(), request.form.get("memo", "").strip(),
                     amount, category_id, status, transaction_id),
                )
                if category_id:
                    remember_payee_category(budget["id"], request.form.get("payee", "").strip(), int(category_id))
                rebuild_credit_activity(budget["id"])
                db().commit()
                flash("Transaction updated and budget balances recalculated.", "success")
        return redirect(url_for("account_register", account_id=transaction["account_id"]))

    @app.post("/transactions/<int:transaction_id>/category")
    @login_required
    def set_transaction_category(transaction_id):
        budget = current_budget()
        transaction = db().execute(
            """SELECT t.*,(SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id=t.id) split_count
               FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.id=? AND a.budget_id=?""",
            (transaction_id, budget["id"]),
        ).fetchone()
        if not transaction:
            abort(404)
        if transaction["cleared"] == 2 or transaction["transfer_id"] or transaction["split_count"]:
            flash("That transaction cannot be categorized directly.", "error")
            return redirect(url_for("account_register", account_id=transaction["account_id"]))
        category_id = request.form.get("category_id", type=int)
        if category_id and not db().execute(
            "SELECT 1 FROM categories WHERE id=? AND budget_id=? AND kind='spending' AND parent_id IS NOT NULL",
            (category_id, budget["id"]),
        ).fetchone():
            abort(400, "Invalid category")
        db().execute("UPDATE transactions SET category_id=? WHERE id=?", (category_id, transaction_id))
        if category_id:
            remember_payee_category(budget["id"], transaction["payee"], category_id)
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash("Transaction category updated.", "success")
        return redirect(url_for("account_register", account_id=transaction["account_id"]))

    @app.post("/transactions/<int:transaction_id>/delete")
    @login_required
    def delete_transaction(transaction_id):
        budget = current_budget()
        transaction = db().execute(
            """SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE t.id=? AND a.budget_id=?""",
            (transaction_id, budget["id"]),
        ).fetchone()
        if not transaction:
            abort(404)
        if transaction["cleared"] == 2:
            flash("Reconciled transactions are locked and cannot be deleted.", "error")
            return redirect(url_for("account_register", account_id=transaction["account_id"]))
        if transaction["transfer_id"]:
            db().execute("DELETE FROM category_activity WHERE transaction_id IN (SELECT id FROM transactions WHERE transfer_id=?)", (transaction["transfer_id"],))
            db().execute("DELETE FROM transactions WHERE transfer_id=?", (transaction["transfer_id"],))
            message = "Transfer deleted from both accounts."
        else:
            db().execute("DELETE FROM category_activity WHERE transaction_id=?", (transaction_id,))
            db().execute("DELETE FROM transaction_splits WHERE transaction_id=?", (transaction_id,))
            db().execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
            message = "Transaction deleted."
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash(message, "success")
        return redirect(url_for("account_register", account_id=transaction["account_id"]))

    @app.post("/transactions/<int:transaction_id>/split")
    @login_required
    def split_transaction(transaction_id):
        budget = current_budget()
        transaction = db().execute(
            """SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE t.id=? AND a.budget_id=?""",
            (transaction_id, budget["id"]),
        ).fetchone()
        if not transaction:
            abort(404)
        if transaction["transfer_id"] or transaction["cleared"] == 2:
            flash("Transfers and reconciled transactions cannot be split.", "error")
            return redirect(url_for("account_register", account_id=transaction["account_id"]))
        category_ids = request.form.getlist("split_category_id")
        amounts = request.form.getlist("split_amount")
        memos = request.form.getlist("split_memo")
        lines = []
        sign = -1 if transaction["amount_cents"] < 0 else 1
        for category_id, amount_value, memo in zip(category_ids, amounts, memos):
            amount = abs(money(amount_value)) * sign
            if category_id and amount:
                lines.append((category_id, amount, memo.strip()))
        if len(lines) < 2 or sum(line[1] for line in lines) != transaction["amount_cents"]:
            flash("A split needs at least two lines whose amounts exactly equal the transaction total.", "error")
            return redirect(url_for("account_register", account_id=transaction["account_id"]))
        valid_ids = {str(row["id"]) for row in db().execute(
            "SELECT id FROM categories WHERE budget_id=? AND parent_id IS NOT NULL AND kind='spending'",
            (budget["id"],),
        ).fetchall()}
        if any(category_id not in valid_ids for category_id, _, _ in lines):
            abort(400, "Invalid split category")
        db().execute("DELETE FROM transaction_splits WHERE transaction_id=?", (transaction_id,))
        for category_id, amount, memo in lines:
            db().execute("INSERT INTO transaction_splits(transaction_id,category_id,memo,amount_cents) VALUES(?,?,?,?)",
                         (transaction_id, category_id, memo, amount))
        db().execute("UPDATE transactions SET category_id=NULL WHERE id=?", (transaction_id,))
        rebuild_credit_activity(budget["id"])
        db().commit()
        flash("Transaction split updated and envelope balances recalculated.", "success")
        return redirect(url_for("account_register", account_id=transaction["account_id"]))

    @app.post("/transactions/<int:transaction_id>/status")
    @login_required
    def transaction_status(transaction_id):
        budget = current_budget()
        transaction = db().execute(
            """SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE t.id=? AND a.budget_id=?""",
            (transaction_id, budget["id"]),
        ).fetchone()
        if not transaction:
            abort(404)
        if transaction["cleared"] == 2:
            flash("Reconciled transactions are locked.", "error")
            return redirect(url_for("account_register", account_id=transaction["account_id"]))
        status = 0 if transaction["cleared"] else 1
        if transaction["transfer_id"]:
            db().execute("UPDATE transactions SET cleared=? WHERE transfer_id=?", (status, transaction["transfer_id"]))
        else:
            db().execute("UPDATE transactions SET cleared=? WHERE id=?", (status, transaction_id))
        db().commit()
        return redirect(url_for("account_register", account_id=transaction["account_id"]))

    @app.post("/accounts/<int:account_id>/reconcile")
    @login_required
    def reconcile_account(account_id):
        budget = current_budget()
        account = db().execute("SELECT * FROM accounts WHERE id=? AND budget_id=?", (account_id, budget["id"])).fetchone()
        if not account:
            abort(404)
        statement_balance = money(request.form.get("statement_balance", "0"))
        cleared_balance = account["starting_balance_cents"] + db().execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE account_id=? AND cleared>=1", (account_id,)
        ).fetchone()[0]
        difference = statement_balance - cleared_balance
        if difference:
            flash(f"Not reconciled: the statement differs from cleared transactions by ${difference/100:,.2f}.", "error")
        else:
            db().execute("UPDATE transactions SET cleared=2 WHERE account_id=? AND cleared=1", (account_id,))
            db().commit()
            flash("Account reconciled. Cleared transactions are now locked as reconciled.", "success")
        return redirect(url_for("account_register", account_id=account_id))

    app.get_db = db
    return app


def money(value):
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return 0


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budgets(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, currency TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budgets(id), name TEXT NOT NULL, type TEXT NOT NULL, balance_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budgets(id), parent_id INTEGER REFERENCES categories(id), name TEXT NOT NULL, assigned_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS category_assignments(id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id), month TEXT NOT NULL, assigned_cents INTEGER NOT NULL DEFAULT 0, UNIQUE(category_id,month));
CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), category_id INTEGER REFERENCES categories(id), occurred_on TEXT NOT NULL, payee TEXT NOT NULL, memo TEXT NOT NULL DEFAULT '', amount_cents INTEGER NOT NULL, cleared INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS category_activity(id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id), transaction_id INTEGER REFERENCES transactions(id), occurred_on TEXT NOT NULL, amount_cents INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transaction_splits(id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES transactions(id), category_id INTEGER NOT NULL REFERENCES categories(id), memo TEXT NOT NULL DEFAULT '', amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS import_batches(id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budgets(id), account_id INTEGER NOT NULL REFERENCES accounts(id), filename TEXT NOT NULL, headers_json TEXT NOT NULL, rows_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'mapping', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS import_rows(id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE, occurred_on TEXT NOT NULL, payee TEXT NOT NULL, memo TEXT NOT NULL DEFAULT '', amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payee_category_rules(id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budgets(id), payee_key TEXT NOT NULL, display_name TEXT NOT NULL, category_id INTEGER NOT NULL REFERENCES categories(id), UNIQUE(budget_id,payee_key));
CREATE TABLE IF NOT EXISTS category_targets(id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL UNIQUE REFERENCES categories(id) ON DELETE CASCADE, target_type TEXT NOT NULL, amount_cents INTEGER NOT NULL, target_date TEXT, due_day INTEGER);
"""

app = create_app()
