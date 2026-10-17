import os
import tempfile
import unittest

import app as platform

class PlatformFeatureTests(unittest.TestCase):
    def setUp(self):
        handle, self.database = tempfile.mkstemp(suffix='.sqlite3')
        os.close(handle)
        os.unlink(self.database)
        platform.DB = self.database
        platform.DB_URL = ''
        platform.initialize()
        platform.app.config.update(TESTING=True)
        self.client = platform.app.test_client()

    def tearDown(self):
        if os.path.exists(self.database): os.unlink(self.database)

    def csrf(self, client=None):
        client = client or self.client
        with client.session_transaction() as state:
            return state.get('csrf')

    def login(self, username='admin', password='ChangeMe123!', client=None):
        client = client or self.client
        response = client.post('/login', data={'username': username, 'password': password})
        self.assertEqual(response.status_code, 302)
        return client

    def test_login_and_admin_role_controls(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.login('sales', 'SalesDemo123!')
        denied = self.client.get('/team')
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.get('/leads')
        self.assertEqual(allowed.status_code, 200)

    def test_api_bearer_token_and_lead_endpoints(self):
        token_response = self.client.post('/api/v1/auth/token', json={'username':'admin','password':'ChangeMe123!'})
        self.assertEqual(token_response.status_code, 200)
        token = token_response.get_json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        created = self.client.post('/api/v1/leads', json={'name':'API Test Lead','company':'API Client','budget':22000,'engagement':80}, headers=headers)
        self.assertEqual(created.status_code, 201)
        lead_id = created.get_json()['id']
        fetched = self.client.get(f'/api/v1/leads/{lead_id}', headers=headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.get_json()['data']['name'], 'API Test Lead')
        unauthenticated = self.client.get('/api/v1/leads')
        self.assertEqual(unauthenticated.status_code, 401)

    def test_web_lead_to_won_opportunity_flow_and_csrf(self):
        self.login()
        token = self.csrf()
        created = self.client.post('/leads/create', data={'_csrf':token,'name':'Workflow Test','company':'Workflow Co','source':'Referral','budget':'40000','engagement':'82'})
        self.assertEqual(created.status_code, 302)
        with platform.app.app_context():
            lead_id = platform.db().execute("SELECT id FROM leads WHERE name='Workflow Test'").fetchone()['id']
        token = self.csrf()
        self.client.post(f'/leads/{lead_id}/qualify', data={'_csrf':token,'budget':'40000','engagement':'82','source':'Referral'})
        token = self.csrf()
        self.client.post(f'/leads/{lead_id}/opportunity', data={'_csrf':token,'name':'Workflow Deal','amount':'40000','stage':'Discovery','probability':'30'})
        with platform.app.app_context():
            opp_id = platform.db().execute("SELECT id FROM opportunities WHERE name='Workflow Deal'").fetchone()['id']
        token = self.csrf()
        response = self.client.post(f'/opportunities/{opp_id}/update', data={'_csrf':token,'stage':'Closed Won','amount':'40000','probability':'30'})
        self.assertEqual(response.status_code, 302)
        with platform.app.app_context():
            status = platform.db().execute('SELECT status FROM opportunities WHERE id=?',(opp_id,)).fetchone()['status']
        self.assertEqual(status, 'Won')


    def test_authenticated_pages_and_manager_access(self):
        self.login()
        for path in ('/','/leads','/opportunities','/activities','/team','/catalog','/reports','/audit','/users','/account/password'):
            with self.subTest(path=path): self.assertEqual(self.client.get(path).status_code,200)
        with platform.app.app_context():
            platform.db().execute("INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)",('manager-test','Test Manager',platform.generate_password_hash('ManagerPass123!'),'manager'))
            platform.db().commit()
        manager_client=platform.app.test_client()
        manager_client.post('/login',data={'username':'manager-test','password':'ManagerPass123!'})
        self.assertEqual(manager_client.get('/reports').status_code,200)
        self.assertEqual(manager_client.get('/catalog').status_code,403)
        self.assertEqual(manager_client.get('/users').status_code,403)


    def test_postgres_adapter_translates_placeholders_and_identity_columns(self):
        class Description:
            def __init__(self,name): self.name=name
        class Cursor:
            def __init__(self): self.description=None; self.commands=[]; self.rows=[]
            def execute(self,sql,params=()):
                self.commands.append((sql,params))
                if 'RETURNING id' in sql: self.rows=[(73,)]
                elif sql.startswith('SELECT label'): self.description=[Description('label')]; self.rows=[('PostgreSQL adapter',)]
            def fetchone(self): return self.rows.pop(0) if self.rows else None
            def fetchall(self): rows,self.rows=self.rows,[]; return rows
            def executemany(self,sql,rows): self.commands.append((sql,list(rows)))
        class Connection:
            def __init__(self): self.cursors=[]
            def cursor(self): cursor=Cursor();self.cursors.append(cursor);return cursor
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        raw=Connection(); pg=platform.PgConnection(raw)
        inserted=pg.execute('INSERT INTO leads(name) VALUES(?)',('Example',))
        self.assertEqual(inserted.lastrowid,73)
        self.assertIn('VALUES(%s) RETURNING id',raw.cursors[0].commands[0][0])
        row=pg.execute('SELECT label FROM demo WHERE id=?',(1,)).fetchone()
        self.assertEqual(row['label'],'PostgreSQL adapter')
        self.assertEqual(raw.cursors[1].commands[0][1],(1,))
        translated=pg.execute('UPDATE leads SET status="Qualified", updated_at=CURRENT_TIMESTAMP WHERE id=?',(1,))
        self.assertIn("status='Qualified'",raw.cursors[2].commands[0][0])
        self.assertIn('updated_at=CURRENT_TIMESTAMP::text',raw.cursors[2].commands[0][0])
        pg.executescript('CREATE TABLE sample(id INTEGER PRIMARY KEY, name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);')
        self.assertIn('id BIGSERIAL PRIMARY KEY',raw.cursors[3].commands[0][0])
        self.assertIn('CURRENT_TIMESTAMP::text',raw.cursors[3].commands[0][0])


    def test_account_password_change_and_admin_reset(self):
        self.login()
        token=self.csrf()
        changed=self.client.post('/account/password',data={'_csrf':token,'current_password':'ChangeMe123!','new_password':'UniqueAdminPass456!'})
        self.assertEqual(changed.status_code,200)
        user=platform.app.test_client()
        self.assertEqual(user.post('/login',data={'username':'admin','password':'UniqueAdminPass456!'}).status_code,302)
        sales_token=self.client.post('/api/v1/auth/token',json={'username':'sales','password':'SalesDemo123!'}).get_json()['access_token']
        sales=platform.app.test_client()
        sales.post('/login',data={'username':'sales','password':'SalesDemo123!'})
        token=self.csrf()
        reset=self.client.post('/users/2/password',data={'_csrf':token,'password':'UniqueSalesPass456!'})
        self.assertEqual(reset.status_code,302)
        self.assertEqual(self.client.get('/api/v1/leads',headers={'Authorization':f'Bearer {sales_token}'}).status_code,401)
        self.assertEqual(sales.get('/leads').status_code,302)
        self.assertEqual(sales.post('/login',data={'username':'sales','password':'UniqueSalesPass456!'}).status_code,302)

    def test_browser_mutation_rejects_missing_csrf(self):
        self.login()
        self.client.post('/leads/create',data={'name':'No CSRF Lead'})
        with platform.app.app_context():
            self.assertEqual(platform.db().execute("SELECT COUNT(*) FROM leads WHERE name='No CSRF Lead'").fetchone()[0],0)


    def test_registration_validation_duplicates_and_logout_for_both_roles(self):
        self.login()
        token=self.csrf()
        created=self.client.post('/users',data={'_csrf':token,'name':'New Manager','username':'new-manager','password':'ThisPassword123!','role':'manager'})
        self.assertEqual(created.status_code,302)
        token=self.csrf()
        short=self.client.post('/users',data={'_csrf':token,'name':'Short','username':'short-pass','password':'tiny','role':'sales'})
        self.assertEqual(short.status_code,302)
        token=self.csrf()
        duplicate=self.client.post('/users',data={'_csrf':token,'name':'Duplicate','username':'new-manager','password':'AnotherPassword123!','role':'sales'})
        self.assertEqual(duplicate.status_code,302)
        with platform.app.app_context():
            self.assertEqual(platform.db().execute("SELECT COUNT(*) FROM users WHERE username='new-manager'").fetchone()[0],1)
            self.assertEqual(platform.db().execute("SELECT COUNT(*) FROM users WHERE username='short-pass'").fetchone()[0],0)
        token=self.csrf(); self.assertEqual(self.client.post('/logout',data={'_csrf':token}).status_code,302)
        self.assertEqual(self.client.get('/').status_code,302)
        sales=platform.app.test_client(); sales.post('/login',data={'username':'sales','password':'SalesDemo123!'})
        token=self.csrf(sales); sales.post('/logout',data={'_csrf':token})
        self.assertEqual(sales.get('/leads').status_code,302)

    def test_dashboard_lead_search_filter_score_assignment_and_status(self):
        self.login()
        dash=self.client.get('/').data
        self.assertIn(b'Total leads',dash); self.assertIn(b'Won revenue',dash); self.assertIn(b'Open pipeline',dash)
        token=self.csrf()
        response=self.client.post('/leads/create',data={'_csrf':token,'name':'Filter Target','company':'Searchable Ltd','source':'Referral','product_id':'1','territory_id':'1','budget':'90000','engagement':'100'})
        self.assertEqual(response.status_code,302)
        with platform.app.app_context():
            lead=platform.db().execute("SELECT * FROM leads WHERE name='Filter Target'").fetchone()
            self.assertGreaterEqual(lead['score'],75); self.assertEqual(lead['priority'],'High'); self.assertIsNotNone(lead['rep_id']); lead_id=lead['id']
        self.assertIn(b'Filter Target',self.client.get('/leads?q=Searchable').data)
        self.assertNotIn(b'Filter Target',self.client.get('/leads?status=Converted').data)
        token=self.csrf(); self.client.post(f'/leads/{lead_id}/status',data={'_csrf':token,'status':'Contacted'})
        with platform.app.app_context(): self.assertEqual(platform.db().execute('SELECT status FROM leads WHERE id=?',(lead_id,)).fetchone()['status'],'Contacted')

    def test_opportunity_amount_stages_lost_and_followup_lifecycle(self):
        self.login()
        with platform.app.app_context(): lead_id=platform.db().execute("SELECT id FROM leads WHERE name='Ethan Brooks'").fetchone()['id']
        token=self.csrf(); self.client.post(f'/leads/{lead_id}/qualify',data={'_csrf':token,'budget':'38000','engagement':'75','source':'Website'})
        token=self.csrf(); self.client.post(f'/leads/{lead_id}/opportunity',data={'_csrf':token,'name':'Loss Test Deal','amount':'42000','stage':'Discovery','probability':'35'})
        with platform.app.app_context(): opp_id=platform.db().execute("SELECT id FROM opportunities WHERE name='Loss Test Deal'").fetchone()['id']
        token=self.csrf(); self.client.post(f'/opportunities/{opp_id}/update',data={'_csrf':token,'stage':'Proposal','amount':'45000','probability':'55'})
        with platform.app.app_context():
            opp=platform.db().execute('SELECT * FROM opportunities WHERE id=?',(opp_id,)).fetchone(); self.assertEqual(opp['amount'],45000); self.assertEqual(opp['stage'],'Proposal')
        token=self.csrf(); self.client.post(f'/opportunities/{opp_id}/update',data={'_csrf':token,'stage':'Closed Lost','amount':'45000','probability':'55'})
        with platform.app.app_context(): self.assertEqual(platform.db().execute('SELECT status FROM opportunities WHERE id=?',(opp_id,)).fetchone()['status'],'Lost')
        token=self.csrf(); self.client.post(f'/leads/{lead_id}/activity',data={'_csrf':token,'type':'Call','due_date':'2020-01-01','notes':'Overdue test action'})
        with platform.app.app_context(): activity=platform.db().execute("SELECT id FROM activities WHERE notes='Overdue test action'").fetchone()['id']
        self.assertIn(b'overdue',self.client.get('/activities').data)
        token=self.csrf(); self.client.post(f'/activity/{activity}/complete',data={'_csrf':token})
        with platform.app.app_context(): self.assertEqual(platform.db().execute('SELECT completed FROM activities WHERE id=?',(activity,)).fetchone()['completed'],1)

    def test_team_edit_capacity_catalog_analytics_and_audit(self):
        self.login()
        token=self.csrf(); self.client.post('/team/create',data={'_csrf':token,'name':'Capacity Tester','email':'tester@example.test','territory_id':'1','capacity':'5','skills':'Cloud Suite'})
        with platform.app.app_context(): rep_id=platform.db().execute("SELECT id FROM reps WHERE name='Capacity Tester'").fetchone()['id']
        token=self.csrf(); self.client.post(f'/team/{rep_id}/edit',data={'_csrf':token,'name':'Capacity Updated','email':'updated@example.test','territory_id':'2','capacity':'2','skills':'Analytics Pro'})
        token=self.csrf(); self.client.post(f'/team/{rep_id}/toggle',data={'_csrf':token})
        token=self.csrf(); self.client.post('/catalog/product',data={'_csrf':token,'name':'QA Product','category':'QA','value':'12345'})
        token=self.csrf(); self.client.post('/catalog/territory',data={'_csrf':token,'name':'QA Territory','region':'Test region'})
        with platform.app.app_context():
            rep=platform.db().execute('SELECT * FROM reps WHERE id=?',(rep_id,)).fetchone()
            self.assertEqual((rep['name'],rep['capacity'],rep['active'],rep['territory_id']),('Capacity Updated',2,0,2))
            self.assertIsNotNone(platform.db().execute("SELECT id FROM products WHERE name='QA Product'").fetchone())
            self.assertIsNotNone(platform.db().execute("SELECT id FROM territories WHERE name='QA Territory'").fetchone())
        self.assertIn(b'Capacity Updated',self.client.get('/team').data)
        self.assertIn(b'QA Product',self.client.get('/catalog').data)
        self.assertIn(b'QA Territory',self.client.get('/reports').data)
        audit=self.client.get('/audit').data
        self.assertIn(b'Profile updated',audit); self.assertIn(b'QA Product',audit); self.assertIn(b'QA Territory',audit)

    def test_login_rejects_bad_password_and_api_health_is_public(self):
        bad = self.client.post('/login', data={'username':'admin','password':'incorrect'})
        self.assertEqual(bad.status_code, 200)
        self.assertIn(b'incorrect', bad.data)
        self.assertEqual(self.client.get('/api/v1/health').status_code, 200)

if __name__ == '__main__': unittest.main()


