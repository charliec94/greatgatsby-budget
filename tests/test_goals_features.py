import json
import sqlite3
import time
from copy import deepcopy

import pytest
from app import create_app
import goals_features as goals


def purchase(gid='jacket', **changes):
    return dict(id=gid, title='Motorcycle jacket' if gid=='jacket' else 'Shoes', type='purchase', status='active', tracking='savings', unit='$', target=500, due='2027-03-01', **changes)


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = create_app({'TESTING':True, 'SECRET_KEY':'test', 'DATABASE':str(tmp_path/'budget.db'), 'GOALS_BASE_URL':'http://goals:3000'})
    client=app.test_client()
    client.get('/setup')
    with client.session_transaction() as s:
        csrf=s['csrf_token']
    client.post('/setup',data={'csrf_token':csrf,'name':'Owner','email':'owner@example.test','password':'long-test-password'})
    client.get('/')
    with client.session_transaction() as s:
        csrf=s['csrf_token']
    records=[purchase(),purchase('shoes')]
    monkeypatch.setattr(goals,'fetch_goals',lambda source:deepcopy(records))
    def db():
        c=sqlite3.connect(tmp_path/'budget.db');c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
    return app,client,csrf,records,db


def preview(env):
    app,client,csrf,records,db=env
    response=client.post('/goals/preview',data={'csrf_token':csrf})
    assert response.status_code==200
    with db() as c:
        row=c.execute('SELECT * FROM goal_previews').fetchone()
    return row['token'],response


def apply(env, token, ids=('jacket','shoes')):
    return env[1].post('/goals/apply',data={'csrf_token':env[2],'preview_token':token,'goal_id':list(ids)},follow_redirects=True)


def test_preview_select_update_and_restart(env):
    app,client,csrf,records,db=env
    assert b'Pull from Goals' in client.get('/').data
    token,response=preview(env)
    assert b'500.00' in response.data and b'Motorcycle jacket' in response.data
    with db() as c:
        assert c.execute('SELECT count(*) FROM goal_links').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM categories WHERE name='Imported'").fetchone()[0]==0
    assert b'Applied 1 purchase targets' in apply(env,token,('jacket',)).data
    with db() as c:
        cid=c.execute('SELECT category_id FROM goal_links').fetchone()[0]
        c.execute("INSERT INTO category_assignments(category_id,month,assigned_cents) VALUES(?,'2026-10',12000)",(cid,))
        c.commit()
    records[0].update(title='Safer jacket',target=650.25,due='2027-04-30')
    token,response=preview(env)
    assert b'Currently: Motorcycle jacket' in response.data
    assert b'Applied 2 purchase targets' in apply(env,token).data
    assert b'already applied' in apply(env,token).data
    # Same database in a recreated app keeps stable category links.
    restarted=create_app(dict(app.config))
    rc=restarted.test_client()
    with client.session_transaction() as old, rc.session_transaction() as new:
        new.update(dict(old))
    with db() as c:
        assert c.execute('SELECT count(*) FROM goal_links').fetchone()[0]==2
        assert c.execute("SELECT count(*) FROM categories WHERE name='Imported'").fetchone()[0]==1
        assert c.execute('SELECT category_id FROM goal_links WHERE goal_id=?',('jacket',)).fetchone()[0]==cid
        assert c.execute('SELECT amount_cents,target_date FROM category_targets WHERE category_id=?',(cid,)).fetchone()[:]==(65025,'2027-04-30')
        assert c.execute('SELECT assigned_cents FROM category_assignments WHERE category_id=?',(cid,)).fetchone()[0]==12000
        assert c.execute('SELECT count(*) FROM transactions').fetchone()[0]==0
    assert rc.post('/goals/preview',data={'csrf_token':csrf}).status_code==200
    records[0]['due']=''
    token,_=preview(env);apply(env,token,('jacket',))
    with db() as c:
        assert c.execute('SELECT target_type,target_date FROM category_targets WHERE category_id=?',(cid,)).fetchone()[:]==('balance',None)


def test_validation_and_eligibility(env):
    records=env[3]
    records.extend([dict(purchase('done'),status='completed'),dict(purchase('personal'),type='personal'),dict(purchase('number'),tracking='number'),dict(purchase('foreign'),unit='EUR')])
    token,response=preview(env)
    assert b'2 eligible goals; 4 skipped' in response.data
    for changes in [dict(target=0),dict(target=-1),dict(target='500'),dict(target=True),dict(target=1.001),dict(target=float('nan')),dict(due='2027-02-29'),dict(status='invalid')]:
        original=deepcopy(records[0]);records[0].update(changes)
        assert env[1].post('/goals/preview',data={'csrf_token':env[2]}).status_code==302
        records[0]=original
    assert b'Select at least one' in apply(env,token,()).data
    assert b'Select at least one' in apply(env,token,('unknown',)).data
    with env[4]() as c:
        assert c.execute('SELECT count(*) FROM goal_links').fetchone()[0]==0


def test_stale_expired_and_other_budget_previews(env):
    token,_=preview(env)
    with env[4]() as c:
        c.execute('UPDATE goal_previews SET expires=0');c.commit()
    assert b'expired' in apply(env,token).data
    token,_=preview(env)
    env[1].post('/budgets/create',data={'csrf_token':env[2],'name':'Other'})
    assert b'expired' in apply(env,token).data
    with env[4]() as c:
        assert c.execute('SELECT count(*) FROM goal_links').fetchone()[0]==0


def test_local_edits_and_failed_insert_roll_back(env):
    token,_=preview(env);apply(env,token)
    token,_=preview(env)
    with env[4]() as c:
        c.execute("UPDATE categories SET name='Local change' WHERE id=(SELECT category_id FROM goal_links WHERE goal_id='jacket')");c.commit()
    assert b'changed since preview' in apply(env,token).data
    # New source IDs force inserts, then inject a failure on the second target.
    env[3][:]=[dict(purchase('new1'),title='First'),dict(purchase('new2'),title='Second')]
    token,_=preview(env)
    with env[4]() as c:
        c.execute("CREATE TRIGGER fail_goal BEFORE INSERT ON category_targets WHEN NEW.category_id=(SELECT id FROM categories WHERE name='Second') BEGIN SELECT RAISE(ABORT,'test failure'); END")
        c.commit()
    assert b'rolled back' in apply(env,token,('new1','new2')).data
    with env[4]() as c:
        assert c.execute("SELECT count(*) FROM categories WHERE name IN ('First','Second')").fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM goal_links').fetchone()[0]==2
        assert c.execute('SELECT 1 FROM goal_previews WHERE token=?',(token,)).fetchone()


def test_owner_csrf_and_source_configuration(env,monkeypatch):
    app,client,csrf,records,db=env
    assert app.test_client().get('/goals').status_code==302
    assert client.post('/goals/preview',data={}).status_code==400
    with db() as c:
        c.execute("INSERT INTO users(name,email,password_hash,role) VALUES('Member','member@example.test','not-used','member')")
        uid=c.execute("SELECT id FROM users WHERE email='member@example.test'").fetchone()[0]
        c.execute('INSERT INTO budget_members(budget_id,user_id) VALUES(1,?)',(uid,));c.commit()
    member=app.test_client()
    with member.session_transaction() as s:
        s.update(user_id=uid,budget_id=1,csrf_token='member')
    assert member.get('/goals').status_code==403
    assert member.post('/goals/preview',data={'csrf_token':'member'}).status_code==403
    assert member.post('/goals/apply',data={'csrf_token':'member'}).status_code==403
    for url in ['', 'file:///data/secret','http://user:password@host','http://host/path','http://host/?token=secret','http://host:bad']:
        app.config['GOALS_BASE_URL']=url
        assert client.post('/goals/preview',data={'csrf_token':csrf}).status_code==302
    app.config['GOALS_BASE_URL']='http://goals:3000'
    monkeypatch.setattr(goals,'fetch_goals',lambda source:(_ for _ in ()).throw(OSError('sensitive details')))
    response=client.post('/goals/preview',data={'csrf_token':csrf},follow_redirects=True)
    assert b'Unable to preview' in response.data and b'sensitive details' not in response.data


def test_restore_deletion_and_unlinked_name_conflict(env):
    from budget_backup import snapshot, replace_budget
    token,_=preview(env);apply(env,token)
    with env[4]() as c:
        payload=snapshot(c,1)
        replace_budget(c,1,payload);c.commit()
        assert c.execute('SELECT count(*) FROM goal_links').fetchone()[0]==0
    token,_=preview(env)
    assert b'unlinked category has the same name' in apply(env,token).data
    with env[4]() as c:
        assert c.execute("SELECT count(*) FROM categories WHERE name='Motorcycle jacket'").fetchone()[0]==1


def test_remote_text_is_escaped(env):
    env[3][0]['title']='<img src=x onerror=alert(1)>'
    _,response=preview(env)
    assert b'&lt;img' in response.data and b'<img src=x' not in response.data
