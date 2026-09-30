"""Scheduled transactions, Auto-Assign, money history, and focused budget views."""
import calendar
import json
from datetime import date, datetime, timedelta, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for


PLANNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_transactions(id INTEGER PRIMARY KEY,budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,payee TEXT NOT NULL,memo TEXT NOT NULL DEFAULT '',amount_cents INTEGER NOT NULL,frequency TEXT NOT NULL,next_due TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS scheduled_occurrences(id INTEGER PRIMARY KEY,schedule_id INTEGER NOT NULL REFERENCES scheduled_transactions(id) ON DELETE CASCADE,due_on TEXT NOT NULL,transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,UNIQUE(schedule_id,due_on));
CREATE TABLE IF NOT EXISTS account_reconciliations(id INTEGER PRIMARY KEY,budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,statement_date TEXT NOT NULL,statement_balance_cents INTEGER NOT NULL,cleared_balance_cents INTEGER NOT NULL,adjustment_cents INTEGER NOT NULL DEFAULT 0,adjustment_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS money_moves(id INTEGER PRIMARY KEY,budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,month TEXT NOT NULL,description TEXT NOT NULL,changes_json TEXT NOT NULL,undone_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS focused_views(id INTEGER PRIMARY KEY,budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,name TEXT NOT NULL,category_ids_json TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


def log_money_move(connection, budget_id, user_id, month, description, changes):
    changes = [dict(category_id=int(c["category_id"]), delta=int(c["delta"])) for c in changes if c["delta"]]
    if changes:
        connection.execute("INSERT INTO money_moves(budget_id,user_id,month,description,changes_json) VALUES(?,?,?,?,?)",
                           (budget_id, user_id, month, description[:200], json.dumps(changes)))


def _next_date(value, frequency):
    if frequency == "weekly":
        return value + timedelta(days=7)
    if frequency == "biweekly":
        return value + timedelta(days=14)
    year = value.year + (value.month == 12)
    month = 1 if value.month == 12 else value.month + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _month(value):
    try:
        return datetime.strptime(value or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        return date.today().replace(day=1)


def register_planning_features(app, db, current_budget, login_required, sidebar_accounts, money, target_metrics, rebuild_credit_activity):
    def context(view="planning"):
        budget = current_budget()
        return dict(budget=budget, accounts=sidebar_accounts(budget["id"]), active_view=view, active_account_id=None)

    def post_due(budget_id, through=None):
        through = through or date.today()
        schedules = db().execute("SELECT * FROM scheduled_transactions WHERE budget_id=? AND active=1 AND next_due<=? ORDER BY next_due,id",
                                 (budget_id, through.isoformat())).fetchall()
        posted = 0
        for schedule in schedules:
            due = date.fromisoformat(schedule["next_due"])
            count = 0
            while due <= through and count < 120:
                exists = db().execute("SELECT 1 FROM scheduled_occurrences WHERE schedule_id=? AND due_on=?",
                                      (schedule["id"], due.isoformat())).fetchone()
                if not exists:
                    ready = 1 if schedule["amount_cents"] > 0 and schedule["category_id"] is None else 0
                    cursor = db().execute(
                        "INSERT INTO transactions(account_id,category_id,occurred_on,payee,memo,amount_cents,cleared,ready_to_assign) VALUES(?,?,?,?,?,?,0,?)",
                        (schedule["account_id"], schedule["category_id"], due.isoformat(), schedule["payee"], schedule["memo"], schedule["amount_cents"], ready))
                    db().execute("INSERT INTO scheduled_occurrences(schedule_id,due_on,transaction_id) VALUES(?,?,?)",
                                 (schedule["id"], due.isoformat(), cursor.lastrowid))
                    posted += 1
                due = _next_date(due, schedule["frequency"])
                count += 1
            db().execute("UPDATE scheduled_transactions SET next_due=? WHERE id=?", (due.isoformat(), schedule["id"]))
        if posted:
            rebuild_credit_activity(budget_id)
            db().commit()
        return posted

    @app.before_request
    def materialize_scheduled_transactions():
        if session.get("user_id") and request.endpoint not in ("static", "logout"):
            budget = current_budget()
            if budget:
                post_due(budget["id"])

    @app.get("/planning")
    @login_required
    def planning():
        budget = current_budget()
        schedules = db().execute("""SELECT s.*,a.name account_name,c.name category_name,p.name parent_name
            FROM scheduled_transactions s JOIN accounts a ON a.id=s.account_id
            LEFT JOIN categories c ON c.id=s.category_id LEFT JOIN categories p ON p.id=c.parent_id
            WHERE s.budget_id=? ORDER BY s.active DESC,s.next_due,s.id""", (budget["id"],)).fetchall()
        categories = db().execute("""SELECT c.id,c.name,p.name parent_name FROM categories c JOIN categories p ON p.id=c.parent_id
            WHERE c.budget_id=? AND c.kind='spending' AND c.hidden=0 ORDER BY p.name,c.name""", (budget["id"],)).fetchall()
        views = db().execute("SELECT * FROM focused_views WHERE budget_id=? AND user_id=? ORDER BY name,id",
                             (budget["id"], session["user_id"])).fetchall()
        moves = db().execute("""SELECT m.*,u.name user_name FROM money_moves m LEFT JOIN users u ON u.id=m.user_id
            WHERE m.budget_id=? ORDER BY m.id DESC LIMIT 30""", (budget["id"],)).fetchall()
        return render_template("planning.html", schedules=schedules, categories=categories, views=views, moves=moves,
                               today=date.today().isoformat(), **context())

    @app.post("/scheduled")
    @login_required
    def create_schedule():
        budget = current_budget()
        account_id = request.form.get("account_id", type=int)
        category_id = request.form.get("category_id", type=int)
        account = db().execute("SELECT 1 FROM accounts WHERE id=? AND budget_id=?", (account_id, budget["id"])).fetchone()
        category = db().execute("SELECT 1 FROM categories WHERE id=? AND budget_id=? AND parent_id IS NOT NULL AND kind='spending'",
                                (category_id, budget["id"])).fetchone() if category_id else None
        payee = request.form.get("payee", "").strip()
        direction, amount, frequency = request.form.get("direction"), abs(money(request.form.get("amount", "0"))), request.form.get("frequency")
        try:
            next_due = date.fromisoformat(request.form.get("next_due", ""))
        except ValueError:
            next_due = None
        if not account or (category_id and not category) or not payee or not amount or direction not in ("outflow", "inflow") or frequency not in ("weekly", "biweekly", "monthly") or not next_due:
            flash("Complete every scheduled transaction field with valid values.", "error")
        else:
            signed = amount if direction == "inflow" else -amount
            db().execute("""INSERT INTO scheduled_transactions(budget_id,account_id,category_id,payee,memo,amount_cents,frequency,next_due)
                VALUES(?,?,?,?,?,?,?,?)""", (budget["id"], account_id, category_id, payee[:200],
                request.form.get("memo", "").strip(), signed, frequency, next_due.isoformat()))
            db().commit()
            post_due(budget["id"])
            flash("Scheduled transaction created.", "success")
        return redirect(url_for("planning"))

    @app.post("/scheduled/<int:schedule_id>/toggle")
    @login_required
    def toggle_schedule(schedule_id):
        budget = current_budget()
        db().execute("UPDATE scheduled_transactions SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND budget_id=?",
                     (schedule_id, budget["id"]))
        db().commit()
        return redirect(url_for("planning"))

    @app.post("/scheduled/<int:schedule_id>/delete")
    @login_required
    def delete_schedule(schedule_id):
        budget = current_budget()
        db().execute("DELETE FROM scheduled_transactions WHERE id=? AND budget_id=?", (schedule_id, budget["id"]))
        db().commit()
        flash("Schedule removed. Existing transactions were kept.", "success")
        return redirect(url_for("planning"))

    def proposals(budget_id, month):
        next_month = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
        rows = db().execute("""SELECT c.id,c.name,p.name parent_name,ct.target_type,ct.amount_cents target_amount,ct.target_date,ct.due_day,
            COALESCE((SELECT assigned_cents FROM category_assignments WHERE category_id=c.id AND month=?),0) assigned,
            COALESCE((SELECT SUM(assigned_cents) FROM category_assignments WHERE category_id=c.id AND month<?),0)
             +COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on<?),0)
             +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id WHERE s.category_id=c.id AND t.occurred_on<?),0)
             +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on<?),0) carried,
            COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on>=? AND occurred_on<?),0)
             +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id WHERE s.category_id=c.id AND t.occurred_on>=? AND t.occurred_on<?),0)
             +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on>=? AND occurred_on<?),0) activity
            FROM categories c JOIN categories p ON p.id=c.parent_id LEFT JOIN category_targets ct ON ct.category_id=c.id
            WHERE c.budget_id=? AND c.hidden=0 AND c.kind='spending' ORDER BY c.id""",
            (month.strftime("%Y-%m"), month.strftime("%Y-%m"), month.isoformat(), month.isoformat(), month.isoformat(),
             month.isoformat(), next_month.isoformat(), month.isoformat(), next_month.isoformat(),
             month.isoformat(), next_month.isoformat(), budget_id)).fetchall()
        scheduled = {}
        for schedule in db().execute("""SELECT * FROM scheduled_transactions
            WHERE budget_id=? AND active=1 AND category_id IS NOT NULL AND amount_cents<0""", (budget_id,)).fetchall():
            due, total, first_due = date.fromisoformat(schedule["next_due"]), 0, None
            while due < next_month:
                if due >= month:
                    total += abs(schedule["amount_cents"])
                    first_due = first_due or due
                due = _next_date(due, schedule["frequency"])
            if total:
                value = scheduled.setdefault(schedule["category_id"], {"amount": 0, "due": first_due})
                value["amount"] += total
                value["due"] = min(value["due"], first_due)
        raw = []
        for row in rows:
            available = row["carried"] + row["assigned"] + row["activity"]
            target_need, _ = target_metrics(row["target_type"], row["target_amount"], row["target_date"],
                                            row["assigned"], row["carried"], row["activity"], month)
            upcoming = scheduled.get(row["id"], {"amount": 0, "due": None})
            schedule_need = max(upcoming["amount"] - max(row["assigned"], 0), 0)
            overspent = max(-available, 0)
            needed = max(overspent, target_need, schedule_need)
            if not needed:
                continue
            if overspent:
                reason, priority = "Cover overspending", (0, "")
            elif schedule_need:
                reason, priority = "Upcoming scheduled transaction", (1, upcoming["due"].isoformat())
            else:
                reason, priority = "Target", (2, f"{row['due_day'] or 99:02d}")
            raw.append(dict(category_id=row["id"], name=row["name"], parent_name=row["parent_name"],
                            needed=needed, reason=reason, priority=priority))
        raw.sort(key=lambda item: (item["priority"], item["parent_name"], item["name"]))
        balances = db().execute("""SELECT c.id,COALESCE((SELECT SUM(assigned_cents) FROM category_assignments WHERE category_id=c.id AND month<=?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE category_id=c.id AND occurred_on<?),0)
            +COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id WHERE s.category_id=c.id AND t.occurred_on<?),0)
            +COALESCE((SELECT SUM(amount_cents) FROM category_activity WHERE category_id=c.id AND occurred_on<?),0) balance
            FROM categories c WHERE c.budget_id=? AND c.parent_id IS NOT NULL""",
            (month.strftime("%Y-%m"), next_month.isoformat(), next_month.isoformat(), next_month.isoformat(), budget_id)).fetchall()
        available = sum(max(row["balance"], 0) for row in balances)
        cash = db().execute("""SELECT COALESCE(SUM(a.starting_balance_cents+COALESCE((SELECT SUM(amount_cents) FROM transactions
            WHERE account_id=a.id AND occurred_on<?),0)),0) FROM accounts a WHERE a.budget_id=? AND a.type!='credit'""",
            (next_month.isoformat(), budget_id)).fetchone()[0]
        ready = max(cash - available, 0)
        result, remaining = [], ready
        for item in raw:
            amount = min(item["needed"], remaining)
            if amount:
                item["amount"] = amount
                result.append(item)
                remaining -= amount
        return result, ready

    @app.route("/auto-assign", methods=("GET", "POST"))
    @login_required
    def auto_assign():
        budget, month = current_budget(), _month(request.values.get("month"))
        plan, ready = proposals(budget["id"], month)
        if request.method == "POST":
            changes = []
            for item in plan:
                db().execute("""INSERT INTO category_assignments(category_id,month,assigned_cents) VALUES(?,?,?)
                    ON CONFLICT(category_id,month) DO UPDATE SET assigned_cents=assigned_cents+excluded.assigned_cents""",
                    (item["category_id"], month.strftime("%Y-%m"), item["amount"]))
                changes.append({"category_id": item["category_id"], "delta": item["amount"]})
            log_money_move(db(), budget["id"], session["user_id"], month.strftime("%Y-%m"),
                           f"Auto-Assign for {month.strftime('%B %Y')}", changes)
            db().commit()
            flash(f"Auto-Assign gave USD {sum(c['delta'] for c in changes)/100:,.2f} a job.", "success")
            return redirect(url_for("dashboard", month=month.strftime("%Y-%m")))
        return render_template("auto_assign.html", plan=plan, ready=ready, month=month, **context())

    @app.post("/money-moves/<int:move_id>/undo")
    @login_required
    def undo_money_move(move_id):
        budget = current_budget()
        move = db().execute("SELECT * FROM money_moves WHERE id=? AND budget_id=?", (move_id, budget["id"])).fetchone()
        if not move:
            abort(404)
        if move["undone_at"]:
            flash("That money move has already been undone.", "error")
            return redirect(url_for("planning"))
        changes = json.loads(move["changes_json"])
        valid_ids = {r["id"] for r in db().execute("SELECT id FROM categories WHERE budget_id=?", (budget["id"],)).fetchall()}
        if any(c["category_id"] not in valid_ids for c in changes):
            flash("That move cannot be undone because a category no longer exists.", "error")
            return redirect(url_for("planning"))
        for change in changes:
            db().execute("""INSERT INTO category_assignments(category_id,month,assigned_cents) VALUES(?,?,?)
                ON CONFLICT(category_id,month) DO UPDATE SET assigned_cents=assigned_cents-excluded.assigned_cents""",
                (change["category_id"], move["month"], change["delta"]))
        db().execute("UPDATE money_moves SET undone_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), move_id))
        db().commit()
        flash("Money move undone with a reversing assignment.", "success")
        return redirect(url_for("planning"))

    @app.post("/focused-views")
    @login_required
    def create_focused_view():
        budget = current_budget()
        name = request.form.get("name", "").strip()
        requested = {int(v) for v in request.form.getlist("category_id") if v.isdigit()}
        valid = {r["id"] for r in db().execute("SELECT id FROM categories WHERE budget_id=? AND parent_id IS NOT NULL",
                                               (budget["id"],)).fetchall()}
        chosen = sorted(requested.intersection(valid))
        if not name or not chosen:
            flash("Give the view a name and choose at least one category.", "error")
        else:
            db().execute("INSERT INTO focused_views(budget_id,user_id,name,category_ids_json) VALUES(?,?,?,?)",
                         (budget["id"], session["user_id"], name[:80], json.dumps(chosen)))
            db().commit()
            flash("Focused View created.", "success")
        return redirect(url_for("planning"))

    @app.post("/focused-views/<int:view_id>/delete")
    @login_required
    def delete_focused_view(view_id):
        budget = current_budget()
        db().execute("DELETE FROM focused_views WHERE id=? AND budget_id=? AND user_id=?",
                     (view_id, budget["id"], session["user_id"]))
        db().commit()
        return redirect(url_for("planning"))
