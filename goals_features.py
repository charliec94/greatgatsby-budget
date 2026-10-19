"""Explicit, owner-only purchase target imports from the local Goals app."""
import json
import os
import secrets
import sqlite3
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from flask import abort, flash, redirect, render_template, request, session, url_for

GOALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_sources(
 budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
 source TEXT NOT NULL,
 group_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
 PRIMARY KEY(budget_id,source));
CREATE TABLE IF NOT EXISTS goal_links(
 budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
 source TEXT NOT NULL, goal_id TEXT NOT NULL,
 category_id INTEGER NOT NULL UNIQUE REFERENCES categories(id) ON DELETE CASCADE,
 PRIMARY KEY(budget_id,source,goal_id));
CREATE TABLE IF NOT EXISTS goal_previews(
 token TEXT PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 source TEXT NOT NULL, expires INTEGER NOT NULL, body TEXT NOT NULL);
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Goals must respond directly; redirects are not followed.')


def source_url(value):
    value = value.strip().rstrip('/')
    u = urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
        raise ValueError('Configure GOALS_BASE_URL as an HTTP(S) origin, for example http://NAS-IP:3082.')
    _ = u.port  # Validate the port before attempting a request.
    return value


def fetch_goals(source):
    # The destination is operator configuration, never a URL supplied in a form or goal.
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    with opener.open(Request(source + '/api/goals', headers={'Accept': 'application/json'}), timeout=5) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Goals response exceeds 2 MiB.')
    result = json.loads(raw, parse_float=Decimal)
    if not isinstance(result, list) or len(result) > 1000:
        raise ValueError('Expected a Goals list of at most 1,000 records.')
    return result


def purchases(records, currency):
    rows, skipped, ids = [], 0, set()
    for g in records:
        if not isinstance(g, dict):
            raise ValueError('Invalid Goals response.')
        if g.get('type') != 'purchase' or g.get('status') == 'completed':
            skipped += 1
            continue
        gid, title = g.get('id'), g.get('title')
        if not isinstance(gid, str) or not gid or len(gid) > 100 or gid in ids:
            raise ValueError('Purchase goals need unique, stable IDs.')
        ids.add(gid)
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ValueError('Purchase titles must be 1–160 characters.')
        if g.get('status') not in ('active', 'someday'):
            raise ValueError(f'{title}: invalid purchase status.')
        if g.get('tracking') != 'savings' or g.get('unit') not in (currency, '$' if currency == 'USD' else currency):
            skipped += 1
            continue
        try:
            if isinstance(g.get('target'), bool) or not isinstance(g.get('target'), (int, float, Decimal)):
                raise ValueError()
            amount = Decimal(str(g['target']))
            if not amount.is_finite() or amount <= 0 or amount > Decimal('1000000000') or amount * 100 != (amount * 100).to_integral_value():
                raise ValueError()
            cents = int(amount * 100)
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError(f'{title}: set a positive savings target with at most two decimal places.') from None
        due = g.get('due', '')
        if not isinstance(due, str):
            raise ValueError(f'{title}: invalid due date.')
        if due:
            try:
                if date.fromisoformat(due).isoformat() != due:
                    raise ValueError()
            except ValueError:
                raise ValueError(f'{title}: invalid due date.') from None
        rows.append(dict(goal_id=gid, name=title.strip(), amount=cents, due=due or None,
                         target_type='date' if due else 'balance', status=g['status']))
    if len(rows) > 100:
        raise ValueError('Pull at most 100 eligible purchase goals at a time.')
    return rows, skipped


def register_goals_features(app, db, current_budget, login_required, sidebar_accounts):
    app.config.setdefault('GOALS_BASE_URL', os.environ.get('GOALS_BASE_URL', ''))

    def owner():
        if request.method == 'POST' and not session.get('csrf_token'):
            abort(400, 'Invalid form token')
        budget = current_budget()
        if budget['owner_id'] != session['user_id']:
            abort(403)
        return budget

    def group_for(budget_id, source):
        saved = db().execute('SELECT group_id FROM goal_sources WHERE budget_id=? AND source=?', (budget_id, source)).fetchone()
        if saved and saved['group_id']:
            rows = db().execute('SELECT * FROM categories WHERE id=? AND budget_id=?', (saved['group_id'], budget_id)).fetchall()
        else:
            rows = db().execute("SELECT * FROM categories WHERE budget_id=? AND parent_id IS NULL AND name='Imported'", (budget_id,)).fetchall()
        if len(rows) > 1 or any(r['parent_id'] is not None or r['kind'] != 'spending' or r['hidden'] for r in rows):
            raise ValueError('Imported must be a single visible spending category group. Review it in Budget first.')
        return dict(rows[0]) if rows else None

    def state(budget_id, source, gid):
        row = db().execute('''SELECT c.id,c.name,c.parent_id,c.kind,c.hidden,t.target_type,t.amount_cents,t.target_date,t.due_day
            FROM goal_links l JOIN categories c ON c.id=l.category_id LEFT JOIN category_targets t ON t.category_id=c.id
            WHERE l.budget_id=? AND l.source=? AND l.goal_id=? AND c.budget_id=?''', (budget_id, source, gid, budget_id)).fetchone()
        return dict(row) if row else None

    def page(budget, preview=None, token=None):
        return render_template('goals.html', budget=budget, accounts=sidebar_accounts(budget['id']),
                               active_view='goals', active_account_id=None, configured=bool(app.config['GOALS_BASE_URL']),
                               preview=preview, preview_token=token)

    @app.get('/goals')
    @login_required
    def goals_page():
        return page(owner())

    @app.post('/goals/preview')
    @login_required
    def goals_preview():
        budget = owner()
        try:
            source = source_url(app.config['GOALS_BASE_URL'])
            rows, skipped = purchases(fetch_goals(source), budget['currency'])
            group = group_for(budget['id'], source)
            for row in rows:
                old = state(budget['id'], source, row['goal_id'])
                if old and (not group or old['parent_id'] != group['id'] or old['kind'] != 'spending' or old['hidden']):
                    raise ValueError('A linked category was moved or archived. Restore it before pulling again.')
                row['before'] = old
                row['action'] = 'Update' if old else 'Create'
            preview = dict(rows=rows, skipped=skipped, group=group)
            token = secrets.token_urlsafe(32)
            now = int(time.time())
            db().execute('DELETE FROM goal_previews WHERE expires<? OR (budget_id=? AND user_id=?)', (now, budget['id'], session['user_id']))
            db().execute('INSERT INTO goal_previews VALUES(?,?,?,?,?,?)', (token,budget['id'],session['user_id'],source,now+900,json.dumps(preview)))
            db().commit()
            return page(budget, preview, token)
        except (ValueError, OSError):
            # Avoid displaying internal network details or remote response bodies.
            flash('Unable to preview Goals. Check the configured address, connection, and purchase goal amounts/dates. Only matching-currency savings purchases are eligible.', 'error')
            return redirect(url_for('goals_page'))

    @app.post('/goals/apply')
    @login_required
    def goals_apply():
        budget = owner()
        connection = db()
        try:
            connection.execute('BEGIN IMMEDIATE')
            saved = connection.execute('SELECT * FROM goal_previews WHERE token=? AND budget_id=? AND user_id=?',
                                       (request.form.get('preview_token',''),budget['id'],session['user_id'])).fetchone()
            if not saved or saved['expires'] < int(time.time()) or saved['source'] != source_url(app.config['GOALS_BASE_URL']):
                raise ValueError('Preview expired or was already applied. Pull again.')
            preview = json.loads(saved['body'])
            selected = request.form.getlist('goal_id')
            rows = [r for r in preview['rows'] if r['goal_id'] in selected]
            if not rows or len(selected) != len(set(selected)) or len(rows) != len(selected):
                raise ValueError('Select at least one goal from this preview.')
            group = group_for(budget['id'], saved['source'])
            if group != preview['group']:
                raise ValueError('The Imported group changed. Pull again to review the latest state.')
            for row in rows:
                if state(budget['id'],saved['source'],row['goal_id']) != row['before']:
                    raise ValueError('A linked category changed since preview. Pull again before overwriting it.')
                # A restored backup may retain categories but not integration links: never guess a match.
                if not row['before'] and group and connection.execute('SELECT 1 FROM categories WHERE parent_id=? AND name=?', (group['id'],row['name'])).fetchone():
                    raise ValueError('An unlinked category has the same name in Imported. Rename it before creating another category.')
            if not group:
                gid = connection.execute("INSERT INTO categories(budget_id,parent_id,name,assigned_cents,kind) VALUES(?,NULL,'Imported',0,'spending')", (budget['id'],)).lastrowid
            else:
                gid = group['id']
            connection.execute('INSERT INTO goal_sources VALUES(?,?,?) ON CONFLICT(budget_id,source) DO UPDATE SET group_id=excluded.group_id', (budget['id'],saved['source'],gid))
            for row in rows:
                if row['before']:
                    cid = row['before']['id']
                    connection.execute('UPDATE categories SET name=? WHERE id=?', (row['name'],cid))
                else:
                    cid = connection.execute("INSERT INTO categories(budget_id,parent_id,name,assigned_cents,kind) VALUES(?,?,?,0,'spending')", (budget['id'],gid,row['name'])).lastrowid
                    connection.execute('INSERT INTO goal_links VALUES(?,?,?,?)', (budget['id'],saved['source'],row['goal_id'],cid))
                connection.execute('''INSERT INTO category_targets(category_id,target_type,amount_cents,target_date,due_day) VALUES(?,?,?,?,NULL)
                    ON CONFLICT(category_id) DO UPDATE SET target_type=excluded.target_type,amount_cents=excluded.amount_cents,target_date=excluded.target_date''',
                                   (cid,row['target_type'],row['amount'],row['due']))
            connection.execute('DELETE FROM goal_previews WHERE token=?', (saved['token'],))
            connection.commit()
            flash(f'Applied {len(rows)} purchase targets. No money was assigned or moved.', 'success')
        except ValueError as error:
            connection.rollback()
            flash(str(error), 'error')
        except sqlite3.Error:
            connection.rollback()
            flash('Nothing was imported: database changes were rolled back. Pull again to retry.', 'error')
        return redirect(url_for('goals_page'))
