"""Desktop planning, reconciliation, search, and installation information."""
from datetime import date, datetime, timedelta

from flask import abort, render_template, request


APP_VERSION = "0.2.0"


def _month(value):
    try:
        return datetime.strptime(value or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        return date.today().replace(day=1)


def _next_month(month):
    return date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)


def register_desktop_features(app, db, current_budget, login_required, sidebar_accounts):
    def context(view):
        budget = current_budget()
        return dict(budget=budget, accounts=sidebar_accounts(budget["id"]), active_view=view, active_account_id=None)

    @app.get("/month-review")
    @login_required
    def month_review():
        budget = current_budget()
        month = _month(request.args.get("month"))
        following = _next_month(month)
        rows = db().execute("""SELECT c.id,c.name,p.name parent_name,
            COALESCE((SELECT SUM(assigned_cents) FROM category_assignments WHERE category_id=c.id AND month<=?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on<?),0)
            +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
                       WHERE s.category_id=c.id AND t.occurred_on<?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on<?),0) balance,
            COALESCE((SELECT SUM(-t.amount_cents) FROM transactions t JOIN accounts a ON a.id=t.account_id
                       WHERE t.category_id=c.id AND t.occurred_on>=? AND t.occurred_on<? AND t.amount_cents<0 AND a.type='credit'),0)
            +COALESCE((SELECT SUM(-s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
                       JOIN accounts a ON a.id=t.account_id WHERE s.category_id=c.id AND t.occurred_on>=? AND t.occurred_on<?
                       AND s.amount_cents<0 AND a.type='credit'),0) credit_spending
            FROM categories c JOIN categories p ON p.id=c.parent_id
            WHERE c.budget_id=? AND c.kind='spending' ORDER BY p.name,c.name""",
            (month.strftime("%Y-%m"), following.isoformat(), following.isoformat(), following.isoformat(),
             month.isoformat(), following.isoformat(), month.isoformat(), following.isoformat(), budget["id"])).fetchall()
        carryovers, cash_overspending, card_overspending = [], [], []
        for raw in rows:
            row = dict(raw)
            if row["balance"] > 0:
                carryovers.append(row)
            elif row["balance"] < 0:
                deficit = -row["balance"]
                card = min(deficit, row["credit_spending"])
                cash = deficit - card
                if card:
                    card_overspending.append({**row, "amount": card})
                if cash:
                    cash_overspending.append({**row, "amount": cash})
        future_assigned = db().execute("""SELECT COALESCE(SUM(ca.assigned_cents),0) FROM category_assignments ca
            JOIN categories c ON c.id=ca.category_id WHERE c.budget_id=? AND ca.month>?""",
            (budget["id"], month.strftime("%Y-%m"))).fetchone()[0]
        uncategorized = db().execute("""SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.budget_id=? AND t.occurred_on>=? AND t.occurred_on<? AND t.category_id IS NULL
            AND t.transfer_id IS NULL AND t.ready_to_assign=0
            AND NOT EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)""",
            (budget["id"], month.isoformat(), following.isoformat())).fetchone()[0]
        uncleared = db().execute("""SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.budget_id=? AND t.occurred_on>=? AND t.occurred_on<? AND t.cleared=0""",
            (budget["id"], month.isoformat(), following.isoformat())).fetchone()[0]
        return render_template("month_review.html", month=month, following=following, carryovers=carryovers,
            cash_overspending=cash_overspending, card_overspending=card_overspending,
            future_assigned=future_assigned, uncategorized=uncategorized, uncleared=uncleared, **context("month_review"))

    @app.get("/reconciliation")
    @login_required
    def reconciliation_dashboard():
        budget = current_budget()
        accounts = db().execute("""SELECT a.*,
            a.starting_balance_cents+COALESCE((SELECT SUM(t.amount_cents) FROM transactions t WHERE t.account_id=a.id),0) working_balance,
            a.starting_balance_cents+COALESCE((SELECT SUM(t.amount_cents) FROM transactions t WHERE t.account_id=a.id AND t.cleared IN (1,2)),0) cleared_balance,
            (SELECT MAX(statement_date) FROM account_reconciliations r WHERE r.account_id=a.id) last_reconciled,
            (SELECT COUNT(*) FROM transactions t WHERE t.account_id=a.id AND t.cleared=0) uncleared_count,
            (SELECT COUNT(*) FROM transactions t WHERE t.account_id=a.id AND t.pending_transfer=1) pending_count
            FROM accounts a WHERE a.budget_id=? ORDER BY a.closed,a.type='credit',a.name""", (budget["id"],)).fetchall()
        today = date.today()
        items = []
        for raw in accounts:
            row = dict(raw)
            row["days_since"] = (today-date.fromisoformat(row["last_reconciled"])).days if row["last_reconciled"] else None
            row["status"] = "attention" if row["pending_count"] or row["uncleared_count"] or row["days_since"] is None or row["days_since"] > 45 else "current"
            items.append(row)
        return render_template("reconciliation.html", reconciliation_accounts=items, today=today, **context("reconciliation"))

    @app.get("/search")
    @login_required
    def global_search():
        budget = current_budget()
        query = request.args.get("q", "").strip()
        results = []
        if query:
            like = f"%{query}%"
            for row in db().execute("SELECT id,name,type FROM accounts WHERE budget_id=? AND name LIKE ? ORDER BY name LIMIT 10", (budget["id"], like)):
                results.append(dict(kind="Account", title=row["name"], detail=row["type"].title(), url=f"/accounts/{row['id']}"))
            for row in db().execute("""SELECT c.id,c.name,p.name parent_name FROM categories c LEFT JOIN categories p ON p.id=c.parent_id
                WHERE c.budget_id=? AND c.name LIKE ? ORDER BY c.name LIMIT 15""", (budget["id"], like)):
                results.append(dict(kind="Category", title=row["name"], detail=row["parent_name"] or "Category group", url="/"))
            for row in db().execute("""SELECT t.id,t.account_id,t.occurred_on,t.payee,t.memo,t.amount_cents,a.name account_name
                FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.budget_id=?
                AND (t.payee LIKE ? OR t.memo LIKE ?) ORDER BY t.occurred_on DESC,t.id DESC LIMIT 30""", (budget["id"], like, like)):
                results.append(dict(kind="Transaction", title=row["payee"], detail=f"{row['occurred_on']} · {row['account_name']} · ${row['amount_cents']/100:,.2f}", url=f"/accounts/{row['account_id']}?q={query}"))
        return render_template("search.html", query=query, results=results, **context("search"))

    @app.get("/about")
    @login_required
    def about():
        connection = db()
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("accounts", "categories", "transactions", "import_batches")}
        return render_template("about.html", version=APP_VERSION, counts=counts,
                               database_path=app.config["DATABASE"], **context("about"))
