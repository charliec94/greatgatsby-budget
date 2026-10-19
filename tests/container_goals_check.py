"""CI-only synthetic data check, copied into a disposable test container."""
import sqlite3
import sys
from app import create_app
import goals_features

app=create_app()
client=app.test_client()
client.get('/setup' if sys.argv[1]=='seed' else '/login')
with client.session_transaction() as s:
    csrf=s['csrf_token']
if sys.argv[1]=='seed':
    response=client.post('/setup',data={'csrf_token':csrf,'name':'CI Owner','email':'ci@example.test','password':'synthetic-ci-password'})
else:
    response=client.post('/login',data={'csrf_token':csrf,'email':'ci@example.test','password':'synthetic-ci-password'})
assert response.status_code==302
client.get('/goals')
with client.session_transaction() as s:
    assert s.get('user_id')
    csrf=s['csrf_token']
goals_features.fetch_goals=lambda source:[dict(id='ci-purchase',title='CI purchase',type='purchase',status='active',tracking='savings',unit='$',target=500,due='2027-03-01')]
assert client.post('/goals/preview',data={'csrf_token':csrf}).status_code==200
with sqlite3.connect(app.config['DATABASE']) as connection:
    token=connection.execute('SELECT token FROM goal_previews').fetchone()[0]
response=client.post('/goals/apply',data={'csrf_token':csrf,'preview_token':token,'goal_id':'ci-purchase'},follow_redirects=True)
assert b'Applied 1 purchase targets' in response.data
with sqlite3.connect(app.config['DATABASE']) as connection:
    assert connection.execute('SELECT count(*) FROM goal_links').fetchone()[0]==1
    assert connection.execute("SELECT count(*) FROM categories WHERE name='CI purchase'").fetchone()[0]==1
    assert connection.execute('SELECT amount_cents,target_date FROM category_targets').fetchone()==(50000,'2027-03-01')
