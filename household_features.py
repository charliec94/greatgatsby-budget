"""Local reporting, budget-only backups, and optional household email invitations."""
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from budget_backup import snapshot, validate, replace_budget, TABLES

FEATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_members(budget_id INTEGER NOT NULL REFERENCES budgets(id), user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(budget_id,user_id));
CREATE TABLE IF NOT EXISTS household_invites(id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budgets(id), email TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, revoked INTEGER NOT NULL DEFAULT 0);
"""


def register_features(app, db, current_budget, login_required, sidebar_accounts):
    for key, default in {"APP_BASE_URL": "", "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "SMTP_FROM": ""}.items():
        app.config.setdefault(key, os.environ.get(key, default))

    def owner_required(view):
        @wraps(view)
        @login_required
        def wrapped(**kwargs):
            if current_budget()["owner_id"] != session["user_id"]:
                abort(403)
            return view(**kwargs)
        return wrapped

    def context(view):
        budget = current_budget()
        return dict(budget=budget, accounts=sidebar_accounts(budget["id"]), active_view=view, active_account_id=None)

    def signed_in(view):
        @wraps(view)
        def wrapped(**kwargs):
            if not session.get('user_id') or not db().execute('SELECT 1 FROM users WHERE id=?', (session['user_id'],)).fetchone():
                return redirect(url_for('login'))
            return view(**kwargs)
        return wrapped

    @app.get('/budgets')
    @signed_in
    def budgets_page():
        budget = current_budget()
        return render_template('budgets.html', budget=budget, accounts=sidebar_accounts(budget['id']) if budget else [],
                               active_view='budgets', active_account_id=None)

    @app.post('/budgets/create')
    @signed_in
    def create_budget():
        name = request.form.get('name', '').strip()
        if not name or len(name) > 100:
            flash('Enter a budget name of 1–100 characters.', 'error')
            return redirect(url_for('budgets_page'))
        cursor = db().execute('INSERT INTO budgets(owner_id,name,currency) VALUES(?,?,?)', (session['user_id'], name, 'USD'))
        db().commit()
        session['budget_id'] = cursor.lastrowid
        return redirect(url_for('dashboard'))

    @app.post('/budgets/<int:budget_id>/switch')
    @signed_in
    def switch_budget(budget_id):
        allowed = db().execute('''SELECT 1 FROM budgets b WHERE b.id=? AND (b.owner_id=? OR EXISTS
            (SELECT 1 FROM budget_members m WHERE m.budget_id=b.id AND m.user_id=?))''',
            (budget_id, session['user_id'], session['user_id'])).fetchone()
        if not allowed:
            abort(404)
        session['budget_id'] = budget_id
        return redirect(url_for('dashboard'))

    @app.post('/budgets/<int:budget_id>/delete')
    @signed_in
    def delete_budget(budget_id):
        budget = db().execute('SELECT * FROM budgets WHERE id=? AND owner_id=?', (budget_id, session['user_id'])).fetchone()
        if not budget:
            abort(403)
        owner = db().execute('SELECT password_hash FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if request.form.get('confirm_name') != budget['name'] or not check_password_hash(owner['password_hash'], request.form.get('password', '')):
            flash('Enter the exact budget name and your password to confirm deletion.', 'error')
            return redirect(url_for('budgets_page'))
        connection = db()
        try:
            connection.execute('BEGIN IMMEDIATE')
            recovery = snapshot(connection, budget_id)
            directory = Path(app.config['DATABASE']).resolve().parent / 'backups'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'deleted-budget-{budget_id}-{secrets.token_hex(8)}.json'
            with path.open('x', encoding='utf-8') as output:
                json.dump(recovery, output)
            connection.execute('PRAGMA defer_foreign_keys=ON')
            for table in reversed(TABLES):
                for row in recovery['tables'][table]:
                    connection.execute(f'DELETE FROM {table} WHERE id=?', (row['id'],))
            connection.execute('DELETE FROM household_invites WHERE budget_id=?', (budget_id,))
            connection.execute('DELETE FROM budget_members WHERE budget_id=?', (budget_id,))
            connection.execute('DELETE FROM budgets WHERE id=?', (budget_id,))
            connection.commit()
        except (sqlite3.Error, OSError):
            connection.rollback()
            flash('Budget was not deleted: a safe recovery copy or database update could not be completed.', 'error')
            return redirect(url_for('budgets_page'))
        if session.get('budget_id') == budget_id:
            session.pop('budget_id', None)
        flash('Budget deleted. Financial data can be recovered from data/backups; invitations and memberships were removed.', 'success')
        return redirect(url_for('budgets_page'))

    @app.after_request
    def protect_private_pages(response):
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/reports')
    @login_required
    def reports():
        ctx = context('reports')
        today = date.today()
        try:
            start = date.fromisoformat(request.args.get('start') or today.replace(month=1, day=1).isoformat())
            end = date.fromisoformat(request.args.get('end') or today.isoformat())
            if start > end or (end - start).days > 3660:
                raise ValueError()
        except ValueError:
            abort(400, 'Choose a valid date range of at most ten years.')
        account_id = request.args.get('account_id', type=int)
        if account_id and not any(a['id'] == account_id for a in ctx['accounts']):
            abort(400, 'Invalid account')
        transactions = db().execute('''SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.budget_id=? AND t.occurred_on BETWEEN ? AND ? AND (? IS NULL OR a.id=?)
            AND t.transfer_id IS NULL ORDER BY t.occurred_on,t.id''',
            (ctx['budget']['id'], start.isoformat(), end.isoformat(), account_id, account_id)).fetchall()
        categories = {c['id']: dict(c) for c in db().execute('''SELECT c.*,p.name group_name FROM categories c
            LEFT JOIN categories p ON p.id=c.parent_id WHERE c.budget_id=?''', (ctx['budget']['id'],))}
        splits = defaultdict(list)
        for s in db().execute('''SELECT s.* FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
            JOIN accounts a ON a.id=t.account_id WHERE a.budget_id=?''', (ctx['budget']['id'],)):
            splits[s['transaction_id']].append(s)
        totals = defaultdict(int)
        monthly = {}
        month = start.replace(day=1)
        while month <= end:
            monthly[month.strftime('%Y-%m')] = dict(income=0, spending=0, refunds=0)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        for t in transactions:
            summary = monthly[t['occurred_on'][:7]]
            for line in splits.get(t['id']) or [t]:
                amount = line['amount_cents']
                if amount < 0:
                    totals[line['category_id']] += -amount
                    summary['spending'] += -amount
                elif line['category_id']:
                    totals[line['category_id']] -= amount
                    summary['refunds'] += amount
                else:
                    summary['income'] += amount
        spending = sum(m['spending'] for m in monthly.values())
        refunds = sum(m['refunds'] for m in monthly.values())
        income = sum(m['income'] for m in monthly.values())
        breakdown = []
        for category_id, net in totals.items():
            c = categories.get(category_id, {})
            breakdown.append(dict(name=c.get('name', 'Uncategorized'), group=c.get('group_name') or 'Uncategorized',
                                  net=net, percent=max(net, 0) * 100 / max(sum(max(v, 0) for v in totals.values()), 1)))
        breakdown.sort(key=lambda row: (row['group'], -row['net']))
        groups = defaultdict(int)
        for row in breakdown:
            groups[row['group']] += row['net']
        return render_template('reports.html', **ctx, start=start, end=end, account_id=account_id,
                               income=income, spending=spending, refunds=refunds, breakdown=breakdown,
                               groups=dict(groups), monthly=monthly, net=income-spending+refunds)

    @app.get('/backups')
    @owner_required
    def backups():
        return render_template('backups.html', **context('backups'))

    @app.post('/backups/download')
    @owner_required
    def download_backup():
        connection = db()
        connection.execute('BEGIN')
        try:
            payload = snapshot(connection, current_budget()['id'])
        finally:
            connection.rollback()
        return Response(json.dumps(payload), mimetype='application/json', headers={
            'Content-Disposition': f'attachment; filename="greatgatsby-{date.today().isoformat()}.json"'})

    @app.post('/backups/restore')
    @owner_required
    def restore_backup():
        user = db().execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if request.form.get('confirm') != 'RESTORE' or not check_password_hash(user['password_hash'], request.form.get('password', '')):
            flash('Enter RESTORE and your owner password to confirm replacement.', 'error')
            return redirect(url_for('backups'))
        upload = request.files.get('backup')
        if not upload:
            abort(400, 'Choose a backup file.')
        connection = db()
        budget_id = current_budget()['id']
        try:
            payload = json.loads(upload.read())
            validate(connection, payload)
            staging = sqlite3.connect(':memory:')
            staging.row_factory = sqlite3.Row
            try:
                connection.backup(staging)
                staging.execute('PRAGMA foreign_keys=ON')
                staging.execute('BEGIN')
                replace_budget(staging, budget_id, payload)
                staging.commit()
            finally:
                staging.close()
            connection.execute('BEGIN IMMEDIATE')
            recovery = snapshot(connection, budget_id)
            directory = Path(app.config['DATABASE']).resolve().parent / 'backups'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'pre-restore-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")}-{secrets.token_hex(4)}.json'
            with path.open('x', encoding='utf-8') as output:
                json.dump(recovery, output)
            replace_budget(connection, budget_id, payload)
            connection.commit()
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError, UnicodeError):
            connection.rollback()
            flash('Restore failed validation or could not be safely completed. Existing budget data was not replaced.', 'error')
            return redirect(url_for('backups'))
        flash('Budget restored. A pre-restore recovery copy was saved in the data/backups folder. Household logins were not changed.', 'success')
        return redirect(url_for('backups'))

    def smtp_ready():
        return bool(app.config['SMTP_USERNAME'] and app.config['SMTP_PASSWORD'] and app.config['APP_BASE_URL'])

    def household_page(invite_link=None):
        ctx = context('household')
        members = db().execute('''SELECT u.id,u.name,u.email FROM budget_members m JOIN users u ON u.id=m.user_id
            WHERE m.budget_id=? ORDER BY u.name''', (ctx['budget']['id'],)).fetchall()
        invites = db().execute('SELECT * FROM household_invites WHERE budget_id=? ORDER BY id DESC', (ctx['budget']['id'],)).fetchall()
        return render_template('household.html', **ctx, members=members, invites=invites, invite_link=invite_link, smtp_ready=smtp_ready())

    @app.get('/household')
    @owner_required
    def household():
        return household_page()

    @app.post('/household/invite')
    @owner_required
    def invite_household():
        email = request.form.get('email', '').strip().lower()
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email) or len(email) > 254:
            flash('Enter a valid email address.', 'error')
            return redirect(url_for('household'))
        token = secrets.token_urlsafe(32)
        budget = current_budget()
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db().execute('UPDATE household_invites SET revoked=1 WHERE budget_id=? AND email=? AND used_at IS NULL', (budget['id'], email))
        db().execute('INSERT INTO household_invites(budget_id,email,token_hash,expires_at) VALUES(?,?,?,?)',
                     (budget['id'], email, hashlib.sha256(token.encode()).hexdigest(), expires))
        db().commit()
        path = url_for('accept_invite', token=token)
        base = app.config['APP_BASE_URL'].rstrip('/')
        link = base + path if base else request.url_root.rstrip('/') + path
        if request.form.get('send_email') == '1':
            if not smtp_ready() or urlsplit(base).scheme not in ('http', 'https'):
                flash('Email is not configured. Copy the invitation link instead.', 'error')
            else:
                try:
                    message = EmailMessage()
                    message['Subject'] = 'GreatGatsby Budget household invitation'
                    message['From'] = app.config['SMTP_FROM'] or app.config['SMTP_USERNAME']
                    message['To'] = email
                    message.set_content(f'You have been invited to a shared GreatGatsby Budget.\n\n{link}\n\nThis single-use link expires in seven days. Connect to the household network before opening it.\nIf unexpected, ignore this message.')
                    with smtplib.SMTP(app.config['SMTP_HOST'], int(app.config['SMTP_PORT']), timeout=15) as smtp:
                        smtp.ehlo()
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                        smtp.login(app.config['SMTP_USERNAME'], app.config['SMTP_PASSWORD'])
                        smtp.send_message(message)
                    flash('Invitation email sent.', 'success')
                except (OSError, smtplib.SMTPException, ValueError):
                    flash('Email delivery failed. The invitation is still valid; copy the link below.', 'error')
        return household_page(link)

    @app.post('/household/invites/<int:invite_id>/revoke')
    @owner_required
    def revoke_invite(invite_id):
        db().execute('UPDATE household_invites SET revoked=1 WHERE id=? AND budget_id=?', (invite_id, current_budget()['id']))
        db().commit()
        return redirect(url_for('household'))

    @app.post('/household/members/<int:user_id>/remove')
    @owner_required
    def remove_member(user_id):
        db().execute('DELETE FROM budget_members WHERE budget_id=? AND user_id=?', (current_budget()['id'], user_id))
        db().commit()
        return redirect(url_for('household'))

    @app.route('/join/<token>', methods=['GET', 'POST'])
    def accept_invite(token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        if request.method == 'POST':
            db().execute('BEGIN IMMEDIATE')
        invite = db().execute('SELECT * FROM household_invites WHERE token_hash=?', (digest,)).fetchone()
        if not invite or invite['used_at'] or invite['revoked'] or datetime.fromisoformat(invite['expires_at']) <= datetime.now(timezone.utc):
            db().rollback()
            abort(410, 'This invitation has expired or is no longer valid.')
        existing = db().execute('SELECT * FROM users WHERE lower(email)=?', (invite['email'],)).fetchone()
        if request.method == 'POST':
            if existing:
                if not check_password_hash(existing['password_hash'], request.form.get('password', '')):
                    db().rollback()
                    flash('Enter the existing account password to accept this invitation.', 'error')
                    return render_template('join.html', invite=invite, existing=True)
                user_id = existing['id']
            else:
                name = request.form.get('name', '').strip()
                password = request.form.get('password', '')
                if not name or len(password) < 10:
                    db().rollback()
                    flash('Enter a name and a password of at least 10 characters.', 'error')
                    return render_template('join.html', invite=invite, existing=False)
                cursor = db().execute('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',
                                      (name[:100], invite['email'], generate_password_hash(password), 'member'))
                user_id = cursor.lastrowid
            db().execute('INSERT OR IGNORE INTO budget_members(budget_id,user_id) VALUES(?,?)', (invite['budget_id'], user_id))
            db().execute('UPDATE household_invites SET used_at=? WHERE id=?', (datetime.now(timezone.utc).isoformat(), invite['id']))
            db().commit()
            session.clear()
            session['user_id'] = user_id
            session['budget_id'] = invite['budget_id']
            session['user_name'] = existing['name'] if existing else name
            return redirect(url_for('dashboard'))
        return render_template('join.html', invite=invite, existing=bool(existing))
