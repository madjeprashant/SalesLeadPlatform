from flask import Flask, render_template, request, redirect, url_for, flash, g, session, jsonify
import sqlite3, os, re, secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'local-development-key-change-me')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', 'true' if os.environ.get('APP_ENV') == 'production' else 'false').lower() == 'true')
BASE = os.path.dirname(os.path.abspath(__file__))
DB_URL = os.environ.get('DATABASE_URL', '').strip()
DB = os.path.join(BASE, 'saleslead.db')
API_TOKEN_TTL = int(os.environ.get('API_TOKEN_TTL', '3600'))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS reps(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, territory_id INTEGER, skills TEXT DEFAULT '', capacity INTEGER DEFAULT 20, active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS territories(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, region TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT DEFAULT '', value REAL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, name TEXT NOT NULL, company TEXT DEFAULT '', email TEXT DEFAULT '', phone TEXT DEFAULT '', source TEXT DEFAULT 'Website', product_id INTEGER, territory_id INTEGER, rep_id INTEGER, status TEXT DEFAULT 'New', score INTEGER DEFAULT 0, priority TEXT DEFAULT 'Medium', budget REAL DEFAULT 0, engagement INTEGER DEFAULT 50, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, type TEXT NOT NULL, due_date TEXT, notes TEXT DEFAULT '', completed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS opportunities(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, name TEXT NOT NULL, rep_id INTEGER, amount REAL DEFAULT 0, stage TEXT DEFAULT 'Discovery', probability INTEGER DEFAULT 20, close_date TEXT, status TEXT DEFAULT 'Open', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'sales', active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
'''

class HybridRow(tuple):
    def __new__(cls, values, names):
        obj = super().__new__(cls, values)
        obj.names = names
        return obj
    def __getitem__(self, key):
        return super().__getitem__(self.names.index(key)) if isinstance(key, str) else super().__getitem__(key)
    def keys(self): return self.names

class PgCursor:
    def __init__(self, cursor, lastrowid=None): self.cursor=cursor; self.lastrowid=lastrowid
    def fetchone(self):
        row=self.cursor.fetchone()
        return HybridRow(row, [c.name for c in self.cursor.description]) if row is not None and self.cursor.description else row
    def fetchall(self):
        if not self.cursor.description: return []
        names=[c.name for c in self.cursor.description]
        return [HybridRow(row,names) for row in self.cursor.fetchall()]

class PgConnection:
    def __init__(self, conn): self.conn=conn
    def execute(self, sql, params=()):
        statement=re.sub(r'\?', '%s', sql)
        statement=re.sub(r'"([^"]*)"', r"'\1'", statement)
        statement=statement.replace('updated_at=CURRENT_TIMESTAMP','updated_at=CURRENT_TIMESTAMP::text')
        cursor=self.conn.cursor()
        returning=bool(re.match(r'\s*INSERT\s+INTO', statement, re.I)) and ' RETURNING ' not in statement.upper()
        if returning: statement += ' RETURNING id'
        try:
            cursor.execute(statement, params)
        except Exception as exc:
            if exc.__class__.__module__.startswith('psycopg') and ('Integrity' in exc.__class__.__name__ or 'Violation' in exc.__class__.__name__): raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        inserted_id=cursor.fetchone()[0] if returning else None
        return PgCursor(cursor, inserted_id)
    def executemany(self, sql, rows):
        cursor=self.conn.cursor()
        try: cursor.executemany(re.sub(r'\?', '%s', sql), rows)
        except Exception as exc:
            if exc.__class__.__module__.startswith('psycopg') and ('Integrity' in exc.__class__.__name__ or 'Violation' in exc.__class__.__name__): raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        return PgCursor(cursor)
    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip(): self.execute(statement.replace('id INTEGER PRIMARY KEY','id BIGSERIAL PRIMARY KEY').replace('TEXT DEFAULT CURRENT_TIMESTAMP','TEXT DEFAULT (CURRENT_TIMESTAMP::text)'))
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

def connect_db():
    if DB_URL.startswith(('postgres://','postgresql://')):
        import psycopg
        return PgConnection(psycopg.connect(DB_URL))
    conn=sqlite3.connect(DB)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def db():
    if 'db' not in g:
        g.db = connect_db()
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    conn = g.pop('db', None)
    if conn: conn.close()

def log(entity, entity_id, action, detail=''):
    db().execute('INSERT INTO audit(entity,entity_id,action,detail) VALUES(?,?,?,?)', (entity,entity_id,action,detail))

def score_lead(budget, engagement, source, product_id):
    source_points = {'Referral':18,'Partner':16,'Event':12,'Campaign':10,'Website':8,'Customer inquiry':10}
    raw = int(min(float(budget or 0)/5000, 35) + int(engagement or 0)*.35 + source_points.get(source, 5) + (8 if product_id else 0))
    return min(raw, 100)

def initialize():
    conn = connect_db()
    conn.executescript(SCHEMA)
    if DB_URL.startswith(('postgres://','postgresql://')):
        columns={row[0] for row in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'").fetchall()}
    else:
        columns={row[1] for row in conn.execute('PRAGMA table_info(users)').fetchall()}
    if 'session_version' not in columns: conn.execute('ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')
    if conn.execute('SELECT COUNT(*) FROM territories').fetchone()[0] == 0:
        conn.executemany('INSERT INTO territories(name,region) VALUES(?,?)', [('North America','United States & Canada'),('EMEA','Europe, Middle East & Africa'),('APAC','Asia Pacific')])
        conn.executemany('INSERT INTO products(name,category,value) VALUES(?,?,?)', [('Cloud Suite','Software',24000),('Analytics Pro','Analytics',18000),('Security One','Security',32000),('Growth Services','Consulting',12000)])
        conn.executemany('INSERT INTO reps(name,email,territory_id,skills,capacity) VALUES(?,?,?,?,?)', [('Avery Chen','avery@northstar.test',1,'Cloud Suite, Analytics Pro',18),('Jordan Patel','jordan@northstar.test',1,'Security One, Cloud Suite',16),('Morgan Lee','morgan@northstar.test',2,'Analytics Pro, Growth Services',20),('Riley Okafor','riley@northstar.test',2,'Security One, Growth Services',15),('Samira Das','samira@northstar.test',3,'Cloud Suite, Analytics Pro, Security One',18)])
        products = dict(conn.execute('SELECT name,id FROM products').fetchall())
        territories = dict(conn.execute('SELECT name,id FROM territories').fetchall())
        samples = [
          ('Nina Kapoor','BrightPath Health','nina@brightpath.test','+1 415 555 0182','Referral','Cloud Suite','North America',1,'Qualified',86,72000,92,'Multi-site team evaluating a Q4 rollout.'),
          ('Ethan Brooks','Cedar & Co','ethan@cedar.test','+1 212 555 0147','Website','Analytics Pro','North America',2,'Contacted',67,38000,72,'Requested a product walkthrough.'),
          ('Priya Nair','Orbit Logistics','priya@orbit.test','+44 20 5555 0113','Event','Security One','EMEA',3,'New',78,95000,84,'Met the team at FutureOps.'),
          ('Luca Moretti','Mosaic Retail','luca@mosaic.test','+39 02 555 0142','Partner','Growth Services','EMEA',4,'Proposal',82,54000,89,'Partner intro; proposal in review.'),
          ('Hana Sato','BluePeak Systems','hana@bluepeak.test','+81 3 5550 0188','Campaign','Cloud Suite','APAC',5,'New',61,44000,65,'Downloaded the cloud migration guide.'),
          ('Noah Wilson','Fieldstone Group','noah@fieldstone.test','+1 312 555 0190','Customer inquiry','Analytics Pro','North America',1,'Working',73,61000,76,'Expansion opportunity from existing account.'),
          ('Amara Mensah','Kiteworks','amara@kiteworks.test','+971 4 555 0122','Referral','Security One','EMEA',4,'New',91,112000,96,'Urgent security review requested.'),
          ('Oliver Grant','Juniper Labs','oliver@juniper.test','+61 2 5550 0155','Website','Growth Services','APAC',5,'Contacted',55,24000,58,'Asked for services overview.'),
          ('Isabella Cruz','Copperline Finance','isabella@copperline.test','+1 646 555 0104','Campaign','Security One','North America',2,'Working',69,83000,70,'Stakeholder group forming this month.'),
          ('Arjun Mehta','Saffron Media','arjun@saffron.test','+91 22 5550 0125','Partner','Analytics Pro','APAC',5,'Qualified',77,50000,81,'Qualified for discovery workshop.'),
          ('Eva Schneider','Nordlicht GmbH','eva@nordlicht.test','+49 30 5550 0171','Event','Cloud Suite','EMEA',3,'New',72,67000,74,'Interested in regional data hosting.'),
          ('Mia Johnson','Harbor & Pine','mia@harbor.test','+1 206 555 0160','Website','Growth Services','North America',1,'New',48,19000,46,'Early-stage inquiry; follow up next week.')]
        for s in samples:
            score=score_lead(s[10],s[11],s[4],products[s[5]])
            conn.execute('INSERT INTO leads(name,company,email,phone,source,product_id,territory_id,rep_id,status,score,priority,budget,engagement,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(s[0],s[1],s[2],s[3],s[4],products[s[5]],territories[s[6]],s[7],s[8],score,'High' if score>=75 else 'Medium' if score>=50 else 'Low',s[10],s[11],s[12]))
        today=date.today()
        conn.executemany('INSERT INTO activities(lead_id,type,due_date,notes,completed) VALUES(?,?,?,?,?)',[(1,'Call',(today+timedelta(days=1)).isoformat(),'Confirm rollout timeline',0),(2,'Meeting',(today-timedelta(days=1)).isoformat(),'Product walkthrough',0),(3,'Email',today.isoformat(),'Send security overview',0),(4,'Meeting',(today+timedelta(days=2)).isoformat(),'Proposal review',0),(5,'Call',(today+timedelta(days=3)).isoformat(),'Introductory call',0)])
        for lead_id,name,rep,amount,stage,prob,days,status in [(1,'BrightPath Health rollout',1,72000,'Discovery',30,60,'Open'),(4,'Mosaic Retail services',4,54000,'Proposal',70,35,'Open'),(6,'Fieldstone analytics expansion',1,61000,'Negotiation',85,18,'Open'),(10,'Saffron Media analytics',5,50000,'Qualification',20,75,'Open'),(7,'Kiteworks security program',4,112000,'Closed Won',100,-4,'Won')]:
            conn.execute('INSERT INTO opportunities(lead_id,name,rep_id,amount,stage,probability,close_date,status) VALUES(?,?,?,?,?,?,?,?)',(lead_id,name,rep,amount,stage,prob,(today+timedelta(days=days)).isoformat(),status))
        conn.commit()
    if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        conn.executemany('INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)', [(os.environ.get('ADMIN_USERNAME','admin'), 'Workspace Administrator', generate_password_hash(os.environ.get('ADMIN_PASSWORD','ChangeMe123!')), 'admin'), ('sales', 'Sales Representative', generate_password_hash(os.environ.get('SALES_PASSWORD','SalesDemo123!')), 'sales')])
    conn.commit()
    conn.close()

@app.context_processor
def globals_for_templates():
    return {'today':date.today().isoformat(), 'money':lambda n: '${:,.0f}'.format(n or 0), 'csrf_token':csrf_token}

def csrf_token():
    if 'csrf' not in session: session['csrf']=secrets.token_urlsafe(32)
    return session['csrf']

def api_serializer(): return URLSafeTimedSerializer(app.secret_key, salt='northstar-api-token-v1')

@app.before_request
def authenticate_and_authorize():
    endpoint=request.endpoint or ''
    if endpoint in ('login','register','static','health','api_health','api_token'): return
    if request.path.startswith('/api/v1/'):
        auth=request.headers.get('Authorization','')
        if auth.startswith('Bearer '):
            try:
                payload=api_serializer().loads(auth[7:], max_age=API_TOKEN_TTL)
                uid=payload['uid'] if isinstance(payload,dict) else payload
                g.user=db().execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone()
                if g.user and isinstance(payload,dict) and payload.get('sv')!=g.user['session_version']: g.user=None
            except (BadSignature,SignatureExpired):
                g.user=None
        if not getattr(g,'user',None): return jsonify(error='Authentication required'),401
    else:
        uid=session.get('user_id')
        g.user=db().execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone() if uid else None
        if g.user and session.get('session_version') != g.user['session_version']:
            session.clear(); g.user=None
        if not g.user: return redirect(url_for('login', next=request.url))
        if request.method=='POST' and session.get('csrf') and request.form.get('_csrf') != session['csrf']:
            flash('Your session token expired. Please retry.','error')
            return redirect(request.referrer or url_for('dashboard'))
    role=g.user['role']
    if request.path.startswith(('/team','/catalog','/users')) and role!='admin':
        if request.path.startswith('/api/'): return jsonify(error='Administrator role required'),403
        return render_template('403.html'),403
    if request.path.startswith(('/reports','/audit')) and role not in ('admin','manager'):
        return (jsonify(error='Manager role required'),403) if request.path.startswith('/api/') else (render_template('403.html'),403)

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        user=db().execute('SELECT * FROM users WHERE username=? AND active=1',(request.form.get('username','').strip(),)).fetchone()
        if user and check_password_hash(user['password_hash'],request.form.get('password','')):
            session.clear(); session['user_id']=user['id']; session['session_version']=user['session_version']; csrf_token()
            target=request.args.get('next') or url_for('dashboard'); parts=urlsplit(target)
            if parts.scheme or parts.netloc or not target.startswith('/') or target.startswith('//') or '\\' in target: target=url_for('dashboard')
            return redirect(target)
        flash('Username or password is incorrect.','error')
    return render_template('login.html')

@app.post('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        token = request.form.get('_csrf', '')
        if not session.get('csrf') or not secrets.compare_digest(token, session['csrf']):
            flash('Your session token expired. Please retry.', 'error')
            return redirect(url_for('register'))

        username = request.form.get('username', '').strip()
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not name or not username:
            flash('Full name and username are required.', 'error')
        elif not re.fullmatch(r'[A-Za-z0-9_.-]{3,50}', username):
            flash('Username must be 3-50 characters and use only letters, numbers, dots, underscores or hyphens.', 'error')
        elif len(password) < 12:
            flash('Choose a password with at least 12 characters.', 'error')
        elif password != confirm_password:
            flash('Passwords do not match.', 'error')
        elif db().execute('SELECT id FROM users WHERE LOWER(username)=LOWER(?)', (username,)).fetchone():
            flash('That username is already in use. Please choose another.', 'error')
        else:
            d = db()
            d.execute(
                'INSERT INTO users(username,name,password_hash,role,active) VALUES(?,?,?,?,1)',
                (username, name, generate_password_hash(password), 'sales')
            )
            d.commit()
            flash('Account created successfully. You can sign in now.', 'success')
            return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/users', methods=['GET','POST'])
def users():
    d=db()
    if request.method=='POST':
        username=request.form.get('username','').strip(); name=request.form.get('name','').strip(); password=request.form.get('password',''); role=request.form.get('role','sales')
        if len(password)<12 or role not in ('admin','manager','sales') or not username or not name:
            flash('Provide a name, username, a 12-character password and a valid role.','error')
        else:
            d.execute('INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)',(username,name,generate_password_hash(password),role)); d.commit(); flash('User account created.','success')
        return redirect(url_for('users'))
    return render_template('users.html',page='users',users=d.execute('SELECT id,username,name,role,active,created_at FROM users ORDER BY id').fetchall())

@app.route('/account/password', methods=['GET','POST'])
def change_password():
    if request.method=='POST':
        d=db(); user=d.execute('SELECT * FROM users WHERE id=?',(g.user['id'],)).fetchone(); current=request.form.get('current_password',''); new=request.form.get('new_password','')
        if not check_password_hash(user['password_hash'],current): flash('Current password is incorrect.','error')
        elif len(new)<12: flash('Choose a password with at least 12 characters.','error')
        else:
            d.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',(generate_password_hash(new),g.user['id'])); d.commit(); session.clear(); session['user_id']=g.user['id']; session['session_version']=g.user['session_version']+1; csrf_token(); flash('Password changed successfully.','success')
    return render_template('account.html',page='account')

@app.post('/users/<int:user_id>/password')
def reset_user_password(user_id):
    password=request.form.get('password','')
    if len(password)<12: flash('Temporary password must contain at least 12 characters.','error')
    else:
        d=db(); user=d.execute('SELECT username FROM users WHERE id=?',(user_id,)).fetchone()
        if not user: flash('User not found.','error')
        else:
            d.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',(generate_password_hash(password),user_id)); d.commit(); flash(f"Password reset for {user['username']}. Existing sessions and tokens are revoked.",'success')
    return redirect(url_for('users'))

@app.post('/users/<int:user_id>/toggle')
def toggle_user(user_id):
    if user_id==g.user['id']: flash('You cannot disable your own account.','error')
    else:
        d=db(); d.execute('UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(user_id,)); d.commit(); flash('Account access updated.','success')
    return redirect(url_for('users'))

@app.post('/api/v1/auth/token')
def api_token():
    data=request.get_json(silent=True) or {}; user=db().execute('SELECT * FROM users WHERE username=? AND active=1',(data.get('username',''),)).fetchone()
    if not user or not check_password_hash(user['password_hash'],data.get('password','')): return jsonify(error='Invalid credentials'),401
    return jsonify(access_token=api_serializer().dumps({'uid':user['id'],'sv':user['session_version']}),token_type='Bearer',expires_in=API_TOKEN_TTL,role=user['role'])

@app.get('/api/v1/health')
def health(): return jsonify(status='ok',service='northstar-sales-platform')

@app.get('/api/v1/leads')
def api_leads():
    rows=db().execute('SELECT l.*,r.name rep,p.name product,t.name territory FROM leads l LEFT JOIN reps r ON r.id=l.rep_id LEFT JOIN products p ON p.id=l.product_id LEFT JOIN territories t ON t.id=l.territory_id ORDER BY l.id DESC LIMIT 500').fetchall()
    return jsonify(data=[dict(row) for row in rows])

@app.post('/api/v1/leads')
def api_create_lead():
    data=request.get_json(silent=True) or {}
    if not data.get('name'): return jsonify(error='name is required'),400
    source=data.get('source','Website'); product_id=data.get('product_id') or None; budget=float(data.get('budget') or 0); engagement=int(data.get('engagement',50)); score=score_lead(budget,engagement,source,product_id)
    d=db(); cur=d.execute('INSERT INTO leads(name,company,email,phone,source,product_id,territory_id,status,score,priority,budget,engagement,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(data['name'],data.get('company',''),data.get('email',''),data.get('phone',''),source,product_id,data.get('territory_id') or None,'New',score,'High' if score>=75 else 'Medium' if score>=50 else 'Low',budget,engagement,data.get('notes','')))
    lid=cur.lastrowid; log('Lead',lid,'Created via API',f'score {score}'); assigned=assign_lead(lid,False); d.commit()
    return jsonify(id=lid,score=score,priority='High' if score>=75 else 'Medium' if score>=50 else 'Low',assigned_to=assigned['name'] if assigned else None),201

@app.get('/api/v1/leads/<int:lead_id>')
def api_get_lead(lead_id):
    row=db().execute('SELECT l.*,r.name rep,p.name product,t.name territory FROM leads l LEFT JOIN reps r ON r.id=l.rep_id LEFT JOIN products p ON p.id=l.product_id LEFT JOIN territories t ON t.id=l.territory_id WHERE l.id=?',(lead_id,)).fetchone()
    return (jsonify(data=dict(row)),200) if row else (jsonify(error='Lead not found'),404)

@app.patch('/api/v1/leads/<int:lead_id>')
def api_update_lead(lead_id):
    data=request.get_json(silent=True) or {}; allowed={'status','notes','budget','engagement'}; updates={k:v for k,v in data.items() if k in allowed}
    if not updates: return jsonify(error='No supported fields supplied'),400
    if 'status' in updates and updates['status'] not in ('New','Contacted','Working','Qualified','Opportunity','Converted','Disqualified'): return jsonify(error='Invalid status'),400
    d=db(); row=d.execute('SELECT id FROM leads WHERE id=?',(lead_id,)).fetchone()
    if not row: return jsonify(error='Lead not found'),404
    assignments=','.join(f'{key}=?' for key in updates); d.execute(f'UPDATE leads SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?',(*updates.values(),lead_id)); log('Lead',lead_id,'Updated via API',', '.join(updates)); d.commit()
    return jsonify(id=lead_id,updated=list(updates))

@app.get('/api/v1/opportunities')
def api_opportunities():
    rows=db().execute('SELECT o.*,l.name lead_name,r.name rep FROM opportunities o JOIN leads l ON l.id=o.lead_id LEFT JOIN reps r ON r.id=o.rep_id ORDER BY o.id DESC LIMIT 500').fetchall()
    return jsonify(data=[dict(row) for row in rows])

@app.route('/')
def dashboard():
    d=db()
    stats={'leads':d.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 'open':d.execute("SELECT COUNT(*) FROM leads WHERE status NOT IN ('Converted','Disqualified')").fetchone()[0], 'pipeline':d.execute("SELECT COALESCE(SUM(amount),0) FROM opportunities WHERE status='Open'").fetchone()[0], 'won':d.execute("SELECT COALESCE(SUM(amount),0) FROM opportunities WHERE status='Won'").fetchone()[0], 'conversion':d.execute("SELECT ROUND(100.0*SUM(CASE WHEN status='Converted' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0)) FROM leads").fetchone()[0] or 0}
    leads=d.execute('SELECT l.*,r.name rep,p.name product,t.name territory FROM leads l LEFT JOIN reps r ON r.id=l.rep_id LEFT JOIN products p ON p.id=l.product_id LEFT JOIN territories t ON t.id=l.territory_id ORDER BY l.score DESC LIMIT 6').fetchall()
    acts=d.execute("SELECT a.*,l.name lead_name,l.company FROM activities a JOIN leads l ON l.id=a.lead_id WHERE a.completed=0 ORDER BY a.due_date LIMIT 5").fetchall()
    stages=d.execute("SELECT stage,COUNT(*) count,COALESCE(SUM(amount),0) amount FROM opportunities WHERE status='Open' GROUP BY stage ORDER BY CASE stage WHEN 'Qualification' THEN 1 WHEN 'Discovery' THEN 2 WHEN 'Proposal' THEN 3 WHEN 'Negotiation' THEN 4 ELSE 5 END").fetchall()
    return render_template('dashboard.html', page='dashboard',stats=stats,leads=leads,activities=acts,stages=stages)

@app.route('/leads')
def leads():
    d=db(); q=request.args.get('q','').strip(); status=request.args.get('status',''); sql='SELECT l.*,r.name rep,p.name product,t.name territory FROM leads l LEFT JOIN reps r ON r.id=l.rep_id LEFT JOIN products p ON p.id=l.product_id LEFT JOIN territories t ON t.id=l.territory_id WHERE 1=1'; args=[]
    if q: sql+=' AND (l.name LIKE ? OR l.company LIKE ? OR l.email LIKE ?)'; args += [f'%{q}%']*3
    if status: sql+=' AND l.status=?'; args.append(status)
    rows=d.execute(sql+' ORDER BY l.created_at DESC',args).fetchall()
    return render_template('index.html',page='leads',leads=rows,reps=d.execute('SELECT * FROM reps WHERE active=1').fetchall(),products=d.execute('SELECT * FROM products').fetchall(),territories=d.execute('SELECT * FROM territories').fetchall(),q=q,status=status)

@app.post('/leads/create')
def create_lead():
    d=db(); f=request.form; score=score_lead(f.get('budget',0),f.get('engagement',50),f.get('source'),f.get('product_id'))
    cur=d.execute('INSERT INTO leads(name,company,email,phone,source,product_id,territory_id,status,score,priority,budget,engagement,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(f['name'],f.get('company',''),f.get('email',''),f.get('phone',''),f.get('source','Website'),f.get('product_id') or None,f.get('territory_id') or None,'New',score,'High' if score>=75 else 'Medium' if score>=50 else 'Low',float(f.get('budget') or 0),int(f.get('engagement') or 50),f.get('notes','')))
    log('Lead',cur.lastrowid,'Created',f"Lead registered from {f.get('source','Website')}; score {score}")
    assign_lead(cur.lastrowid,False); d.commit(); flash(f'Lead added and scored {score}/100.','success'); return redirect(url_for('leads'))

def assign_lead(lead_id, commit=True):
    d=db(); lead=d.execute('SELECT * FROM leads WHERE id=?',(lead_id,)).fetchone()
    if not lead: return None
    reps=d.execute("SELECT r.*,t.name territory,(SELECT COUNT(*) FROM leads l WHERE l.rep_id=r.id AND l.status NOT IN ('Converted','Disqualified')) load FROM reps r LEFT JOIN territories t ON t.id=r.territory_id WHERE r.active=1").fetchall()
    eligible=[]
    for r in reps:
        if lead['territory_id'] and r['territory_id'] != lead['territory_id']: continue
        if r['load'] >= r['capacity']: continue
        skill=(r['skills'] or '').lower(); product=d.execute('SELECT name FROM products WHERE id=?',(lead['product_id'],)).fetchone()
        expertise=20 if product and product['name'].lower() in skill else 0
        capacity=max(0,30-(r['load']/max(r['capacity'],1))*30)
        eligible.append((expertise+capacity,r))
    if not eligible:
        log('Lead',lead_id,'Assignment pending','No active representative matched territory, skills and capacity')
        if commit:d.commit()
        return None
    rep=max(eligible,key=lambda x:(x[0],-x[1]['load']))[1]
    old=lead['rep_id']; d.execute('UPDATE leads SET rep_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(rep['id'],lead_id))
    if old!=rep['id']: log('Lead',lead_id,'Assigned',f"{rep['name']} selected by territory, expertise and workload")
    if commit:d.commit()
    return rep

@app.post('/leads/<int:lead_id>/assign')
def reassign(lead_id):
    rep=assign_lead(lead_id); flash(('Assigned to '+rep['name']+'.') if rep else 'No eligible representative is available. Review territory, workload and skills.','success' if rep else 'error'); return redirect(request.referrer or url_for('leads'))

@app.post('/leads/<int:lead_id>/status')
def update_status(lead_id):
    status=request.form['status']; db().execute('UPDATE leads SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,lead_id)); log('Lead',lead_id,'Status changed',status); db().commit(); flash('Lead status updated.','success'); return redirect(request.referrer or url_for('leads'))

@app.post('/leads/<int:lead_id>/qualify')
def qualify(lead_id):
    f=request.form; score=score_lead(f.get('budget',0),f.get('engagement',50),f.get('source'),f.get('product_id'))
    db().execute('UPDATE leads SET budget=?,engagement=?,score=?,priority=?,status="Qualified",updated_at=CURRENT_TIMESTAMP WHERE id=?',(float(f.get('budget') or 0),int(f.get('engagement') or 50),score,'High' if score>=75 else 'Medium' if score>=50 else 'Low',lead_id)); log('Lead',lead_id,'Qualified',f'Qualification complete; score {score}'); db().commit(); flash(f'Lead qualified. New score: {score}/100.','success'); return redirect(url_for('leads'))

@app.post('/leads/<int:lead_id>/activity')
def add_activity(lead_id):
    f=request.form; cur=db().execute('INSERT INTO activities(lead_id,type,due_date,notes) VALUES(?,?,?,?)',(lead_id,f['type'],f.get('due_date') or None,f.get('notes',''))); log('Lead',lead_id,'Follow-up scheduled',f"{f['type']} on {f.get('due_date','')}"); db().commit(); flash('Follow-up saved.','success'); return redirect(url_for('leads'))

@app.post('/activity/<int:activity_id>/complete')
def complete_activity(activity_id):
    d=db(); a=d.execute('SELECT lead_id FROM activities WHERE id=?',(activity_id,)).fetchone(); d.execute('UPDATE activities SET completed=1 WHERE id=?',(activity_id,));
    if a: log('Lead',a['lead_id'],'Follow-up completed',f'Activity #{activity_id}')
    d.commit(); flash('Follow-up completed.','success'); return redirect(request.referrer or url_for('dashboard'))

@app.post('/leads/<int:lead_id>/opportunity')
def create_opportunity(lead_id):
    d=db(); lead=d.execute('SELECT * FROM leads WHERE id=?',(lead_id,)).fetchone(); f=request.form
    if not lead: flash('Lead not found.','error'); return redirect(url_for('leads'))
    cur=d.execute('INSERT INTO opportunities(lead_id,name,rep_id,amount,stage,probability,close_date) VALUES(?,?,?,?,?,?,?)',(lead_id,f.get('name') or lead['company']+' opportunity',lead['rep_id'],float(f.get('amount') or lead['budget'] or 0),f.get('stage','Qualification'),int(f.get('probability') or 20),f.get('close_date') or None)); d.execute('UPDATE leads SET status="Opportunity",updated_at=CURRENT_TIMESTAMP WHERE id=?',(lead_id,)); log('Opportunity',cur.lastrowid,'Created',f"Opportunity created from lead #{lead_id}"); log('Lead',lead_id,'Converted to opportunity',f"Opportunity #{cur.lastrowid}"); d.commit(); flash('Opportunity created from qualified lead.','success'); return redirect(url_for('opportunities'))

@app.route('/opportunities')
def opportunities():
    rows=db().execute('SELECT o.*,l.name lead_name,l.company,r.name rep FROM opportunities o JOIN leads l ON l.id=o.lead_id LEFT JOIN reps r ON r.id=o.rep_id ORDER BY CASE o.status WHEN "Open" THEN 0 ELSE 1 END,o.close_date').fetchall()
    return render_template('index.html',page='opportunities',opportunities=rows)

@app.post('/opportunities/<int:opp_id>/update')
def update_opportunity(opp_id):
    f=request.form; d=db(); o=d.execute('SELECT * FROM opportunities WHERE id=?',(opp_id,)).fetchone(); stage=f.get('stage',o['stage']); status='Won' if stage=='Closed Won' else 'Lost' if stage=='Closed Lost' else 'Open'; amount=float(f.get('amount') or o['amount']); prob=100 if status=='Won' else 0 if status=='Lost' else int(f.get('probability') or o['probability']); d.execute('UPDATE opportunities SET stage=?,status=?,amount=?,probability=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(stage,status,amount,prob,opp_id))
    if status in ('Won','Lost'):
        d.execute('UPDATE leads SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',('Converted' if status=='Won' else 'Disqualified',o['lead_id']))
    log('Opportunity',opp_id,'Updated',f'{stage}; {status}; ${amount:,.0f}'); d.commit(); flash('Opportunity updated.','success'); return redirect(url_for('opportunities'))

@app.route('/team')
def team():
    d=db(); reps=d.execute('SELECT r.*,t.name territory,(SELECT COUNT(*) FROM leads l WHERE l.rep_id=r.id AND l.status NOT IN ("Converted","Disqualified")) load,(SELECT COALESCE(SUM(o.amount),0) FROM opportunities o WHERE o.rep_id=r.id AND o.status="Open") pipeline FROM reps r LEFT JOIN territories t ON t.id=r.territory_id ORDER BY r.active DESC,r.name').fetchall()
    return render_template('index.html',page='team',reps=reps,territories=d.execute('SELECT * FROM territories').fetchall(),products=d.execute('SELECT * FROM products').fetchall())

@app.post('/team/create')
def create_rep():
    f=request.form; cur=db().execute('INSERT INTO reps(name,email,territory_id,skills,capacity) VALUES(?,?,?,?,?)',(f['name'],f.get('email',''),f.get('territory_id') or None,f.get('skills',''),int(f.get('capacity') or 20))); log('Representative',cur.lastrowid,'Created',f['name']); db().commit(); flash('Representative added.','success'); return redirect(url_for('team'))

@app.post('/team/<int:rep_id>/edit')
def edit_rep(rep_id):
    f=request.form; capacity=int(f.get('capacity') or 0)
    if capacity<1 or not f.get('name','').strip():
        flash('Name is required and capacity must be at least 1.','error')
        return redirect(url_for('team'))
    d=db(); rep=d.execute('SELECT id FROM reps WHERE id=?',(rep_id,)).fetchone()
    if not rep: flash('Representative not found.','error'); return redirect(url_for('team'))
    d.execute('UPDATE reps SET name=?,email=?,territory_id=?,skills=?,capacity=? WHERE id=?',(f['name'].strip(),f.get('email','').strip(),f.get('territory_id') or None,f.get('skills',''),capacity,rep_id))
    log('Representative',rep_id,'Profile updated',f"Capacity {capacity}; territory {f.get('territory_id') or 'all'}")
    d.commit(); flash('Representative profile updated.','success'); return redirect(url_for('team'))

@app.post('/team/<int:rep_id>/toggle')
def toggle_rep(rep_id):
    d=db(); d.execute('UPDATE reps SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(rep_id,)); log('Representative',rep_id,'Availability changed','Active state toggled'); d.commit(); flash('Availability updated.','success'); return redirect(url_for('team'))

@app.route('/catalog')
def catalog():
    d=db(); return render_template('index.html',page='catalog',products=d.execute('SELECT * FROM products ORDER BY name').fetchall(),territories=d.execute('SELECT * FROM territories ORDER BY name').fetchall())

@app.post('/catalog/product')
def add_product():
    f=request.form; cur=db().execute('INSERT INTO products(name,category,value) VALUES(?,?,?)',(f['name'],f.get('category',''),float(f.get('value') or 0))); log('Product',cur.lastrowid,'Created',f['name']); db().commit(); flash('Product added.','success'); return redirect(url_for('catalog'))

@app.post('/catalog/territory')
def add_territory():
    f=request.form; cur=db().execute('INSERT INTO territories(name,region) VALUES(?,?)',(f['name'],f.get('region',''))); log('Territory',cur.lastrowid,'Created',f['name']); db().commit(); flash('Territory added.','success'); return redirect(url_for('catalog'))

@app.route('/activities')
def activities():
    rows=db().execute('SELECT a.*,l.name lead_name,l.company,r.name rep FROM activities a JOIN leads l ON l.id=a.lead_id LEFT JOIN reps r ON r.id=l.rep_id ORDER BY a.completed,a.due_date').fetchall()
    return render_template('index.html',page='activities',activities=rows)

@app.route('/reports')
def reports():
    d=db(); reps=d.execute("SELECT r.name,t.name territory,COUNT(DISTINCT l.id) leads,ROUND(100.0*SUM(CASE WHEN l.status='Converted' THEN 1 ELSE 0 END)/NULLIF(COUNT(DISTINCT l.id),0)) conversion,COALESCE(SUM(CASE WHEN o.status='Open' THEN o.amount ELSE 0 END),0) pipeline FROM reps r LEFT JOIN territories t ON t.id=r.territory_id LEFT JOIN leads l ON l.rep_id=r.id LEFT JOIN opportunities o ON o.rep_id=r.id GROUP BY r.id, r.name, t.id, t.name ORDER BY pipeline DESC").fetchall()
    territories=d.execute('SELECT t.name,COUNT(l.id) leads,COALESCE(SUM(CASE WHEN l.status="Converted" THEN 1 ELSE 0 END),0) won,COALESCE(SUM(CASE WHEN l.status NOT IN ("Converted","Disqualified") THEN 1 ELSE 0 END),0) active FROM territories t LEFT JOIN leads l ON l.territory_id=t.id GROUP BY t.id ORDER BY leads DESC').fetchall()
    return render_template('index.html',page='reports',reps_report=reps,territory_report=territories)

@app.route('/audit')
def audit():
    rows=db().execute('SELECT * FROM audit ORDER BY created_at DESC,id DESC LIMIT 150').fetchall()
    return render_template('index.html',page='audit',audit=rows)

@app.errorhandler(sqlite3.IntegrityError)
def integrity(_):
    db().rollback(); flash('That record could not be saved. Check for duplicate names or missing values.','error'); return redirect(request.referrer or url_for('dashboard'))

initialize()
if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get('PORT',5000)))

