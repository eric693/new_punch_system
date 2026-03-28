import hashlib
import math
import os
import secrets
import threading
import time
import traceback
import urllib.request
from datetime import date
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask, request, jsonify, render_template,
    session, redirect, url_for, abort
)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, LocationMessage
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
ADMIN_PASSWORD            = os.environ.get('ADMIN_PASSWORD', 'admin123')
_raw_db_url               = os.environ.get('DATABASE_URL', '')
DATABASE_URL              = _raw_db_url.replace('postgres://', 'postgresql://', 1) if _raw_db_url.startswith('postgres://') else _raw_db_url
RENDER_EXTERNAL_URL       = os.environ.get('RENDER_EXTERNAL_URL', '')

print(f"[startup] DATABASE_URL prefix: {DATABASE_URL[:20] if DATABASE_URL else 'NOT SET'}")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ─── Imports ──────────────────────────────────────────────────────────────────
import json as _json
from datetime import datetime as _dt, timedelta as _td

WEEKDAY_ZH = ['一', '二', '三', '四', '五', '六', '日']

# ─── PostgreSQL ───────────────────────────────────────────────────────────────

def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    if not DATABASE_URL:
        print("[WARNING] DATABASE_URL not set — skipping init_db()")
        return
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_staff (
                    id              SERIAL PRIMARY KEY,
                    name            TEXT NOT NULL UNIQUE,
                    username        TEXT UNIQUE,
                    password_hash   TEXT DEFAULT '',
                    role            TEXT DEFAULT '',
                    active          BOOLEAN DEFAULT TRUE,
                    employee_code   TEXT DEFAULT '',
                    department      TEXT DEFAULT '',
                    position_title  TEXT DEFAULT '',
                    hire_date       DATE,
                    birth_date      DATE,
                    base_salary     NUMERIC(12,2) DEFAULT 0,
                    insured_salary  NUMERIC(12,2) DEFAULT 0,
                    daily_hours     NUMERIC(4,1) DEFAULT 8,
                    ot_rate1        NUMERIC(4,2) DEFAULT 1.33,
                    ot_rate2        NUMERIC(4,2) DEFAULT 1.67,
                    salary_type     TEXT DEFAULT 'monthly',
                    hourly_rate     NUMERIC(12,2) DEFAULT 0,
                    vacation_quota  INT DEFAULT NULL,
                    salary_notes    TEXT DEFAULT '',
                    line_user_id    TEXT,
                    bind_code       TEXT,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_records (
                    id            SERIAL PRIMARY KEY,
                    staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    punch_type    TEXT NOT NULL,
                    punched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    note          TEXT DEFAULT '',
                    is_manual     BOOLEAN DEFAULT FALSE,
                    manual_by     TEXT DEFAULT '',
                    latitude      NUMERIC(10,6),
                    longitude     NUMERIC(10,6),
                    gps_distance  INT,
                    location_name TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_locations (
                    id            SERIAL PRIMARY KEY,
                    location_name TEXT NOT NULL DEFAULT '打卡地點',
                    lat           NUMERIC(10,6) NOT NULL,
                    lng           NUMERIC(10,6) NOT NULL,
                    radius_m      INT DEFAULT 100,
                    active        BOOLEAN DEFAULT TRUE,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_config (
                    id           INT PRIMARY KEY DEFAULT 1,
                    gps_required BOOLEAN DEFAULT FALSE,
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                INSERT INTO punch_config (id, gps_required)
                VALUES (1, FALSE)
                ON CONFLICT (id) DO NOTHING
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS line_punch_config (
                    id                   INT PRIMARY KEY DEFAULT 1,
                    channel_access_token TEXT DEFAULT '',
                    channel_secret       TEXT DEFAULT '',
                    enabled              BOOLEAN DEFAULT FALSE,
                    updated_at           TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                INSERT INTO line_punch_config (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule_config (
                    month           TEXT PRIMARY KEY,
                    max_off_per_day INT DEFAULT 2,
                    vacation_quota  INT DEFAULT 8,
                    notes           TEXT DEFAULT '',
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule_requests (
                    id           SERIAL PRIMARY KEY,
                    staff_id     INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    month        TEXT NOT NULL,
                    dates        JSONB NOT NULL DEFAULT '[]',
                    status       TEXT DEFAULT 'pending',
                    submit_note  TEXT DEFAULT '',
                    reviewed_by  TEXT DEFAULT '',
                    reviewed_at  TIMESTAMPTZ,
                    review_note  TEXT DEFAULT '',
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(staff_id, month)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_requests (
                    id            SERIAL PRIMARY KEY,
                    staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    punch_type    TEXT NOT NULL,
                    requested_at  TIMESTAMPTZ NOT NULL,
                    reason        TEXT DEFAULT '',
                    status        TEXT DEFAULT 'pending',
                    reviewed_by   TEXT DEFAULT '',
                    review_note   TEXT DEFAULT '',
                    reviewed_at   TIMESTAMPTZ,
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shift_types (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL,
                    start_time  TIME NOT NULL,
                    end_time    TIME NOT NULL,
                    color       TEXT DEFAULT '#4a7bda',
                    departments TEXT DEFAULT '',
                    active      BOOLEAN DEFAULT TRUE,
                    sort_order  INT DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shift_assignments (
                    id            SERIAL PRIMARY KEY,
                    staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    shift_type_id INT REFERENCES shift_types(id) ON DELETE CASCADE,
                    shift_date    DATE NOT NULL,
                    note          TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(staff_id, shift_date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS overtime_requests (
                    id              SERIAL PRIMARY KEY,
                    staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    request_date    DATE NOT NULL,
                    start_time      TIME NOT NULL,
                    end_time        TIME NOT NULL,
                    ot_hours        NUMERIC(5,2),
                    reason          TEXT DEFAULT '',
                    status          TEXT DEFAULT 'pending',
                    reviewed_by     TEXT DEFAULT '',
                    review_note     TEXT DEFAULT '',
                    ot_pay          NUMERIC(12,2) DEFAULT 0,
                    day_type        TEXT DEFAULT 'weekday',
                    reviewed_at     TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Seed default shifts if empty
            existing_shifts = conn.execute("SELECT COUNT(*) as cnt FROM shift_types").fetchone()
            if existing_shifts['cnt'] == 0:
                defaults = [
                    ('吧台班',  '08:00', '16:00', '#8b5cf6', '吧台', 1),
                    ('外場A班', '09:00', '17:00', '#2e9e6b', '外場', 2),
                    ('外場B班', '14:00', '22:00', '#0ea5e9', '外場', 3),
                    ('廚房A班', '08:00', '16:00', '#e07b2a', '廚房', 4),
                    ('廚房B班', '12:00', '20:00', '#d64242', '廚房', 5),
                ]
                for name, st, et, color, dept, sort in defaults:
                    conn.execute(
                        "INSERT INTO shift_types (name,start_time,end_time,color,departments,sort_order) VALUES (%s,%s,%s,%s,%s,%s)",
                        (name, st, et, color, dept, sort)
                    )

        print("[OK] Database tables created")
    except Exception as e:
        print(f"[ERROR] init_db failed: {e}")
        raise

    # Schema migrations (each in its own connection to avoid transaction abort)
    migrations = [
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS username TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS password_hash TEXT DEFAULT ''",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS latitude NUMERIC(10,6)",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS longitude NUMERIC(10,6)",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS gps_distance INT",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS location_name TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS line_user_id TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bind_code TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS employee_code TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS department TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS position_title TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS hire_date DATE",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS birth_date DATE",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS base_salary NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS insured_salary NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_notes TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS daily_hours NUMERIC(4,1) DEFAULT 8",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS ot_rate1 NUMERIC(4,2) DEFAULT 1.33",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS ot_rate2 NUMERIC(4,2) DEFAULT 1.67",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_type TEXT DEFAULT 'monthly'",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS vacation_quota INT DEFAULT NULL",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS day_type TEXT DEFAULT 'weekday'",
        "ALTER TABLE schedule_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
        """CREATE TABLE IF NOT EXISTS admin_accounts (
            id              SERIAL PRIMARY KEY,
            username        TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            display_name    TEXT DEFAULT '',
            permissions     JSONB DEFAULT '[]',
            is_super        BOOLEAN DEFAULT FALSE,
            active          BOOLEAN DEFAULT TRUE,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            last_login_at   TIMESTAMPTZ
        )""",
    ]
    for sql in migrations:
        try:
            with get_db() as mc:
                mc.execute(sql)
        except Exception as me:
            print(f"[MIGRATION SKIP] {sql[:70]}: {me}")

    # Seed default super admin if no accounts exist
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM admin_accounts").fetchone()['c']
            if cnt == 0:
                all_modules = _json.dumps(['punch','sched','leave','salary','ann','holiday'])
                conn.execute("""
                    INSERT INTO admin_accounts (username, password_hash, display_name, permissions, is_super)
                    VALUES (%s,%s,'超級管理員',%s,TRUE)
                """, ('admin', _hash_pw(ADMIN_PASSWORD), all_modules))
                print("[OK] Default super admin seeded (username: admin)")
    except Exception as e:
        print(f"[WARN] admin seed: {e}")

    print("[OK] Database initialised")


init_db()

# ─── Keep-Alive ───────────────────────────────────────────────────────────────

def keep_alive():
    time.sleep(10)
    while True:
        try:
            base = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else 'http://localhost:5000'
            urllib.request.urlopen(
                urllib.request.Request(f'{base}/health', headers={'User-Agent': 'KeepAlive/1.0'}),
                timeout=10
            )
        except Exception as e:
            print(f"[keep-alive] ping failed: {e}")
        time.sleep(14 * 60)

threading.Thread(target=keep_alive, daemon=True).start()

# ─── Health ───────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    try:
        with get_db() as conn:
            conn.execute('SELECT 1')
        return jsonify({'status': 'ok', 'db': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

# ─── Admin Auth ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': '請先登入'}), 401
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def require_module(module):
    """確認已登入且擁有指定模組權限（超級管理員跳過模組檢查）。"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get('logged_in'):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'error': '請先登入'}), 401
                return redirect(url_for('admin_login'))
            if not session.get('admin_is_super'):
                perms = session.get('admin_permissions') or []
                if module not in perms:
                    return jsonify({'error': f'無「{module}」模組的存取權限'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

def require_super(f):
    """只允許超級管理員存取。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'error': '請先登入'}), 401
        if not session.get('admin_is_super'):
            return jsonify({'error': '需要超級管理員權限'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    return redirect(url_for('admin_login'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            error = '請輸入帳號與密碼'
        else:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT * FROM admin_accounts WHERE username=%s AND active=TRUE",
                    (username,)
                ).fetchone()
            if row and row['password_hash'] == _hash_pw(password):
                perms = row['permissions']
                if isinstance(perms, str):
                    try: perms = _json.loads(perms)
                    except: perms = []
                session['logged_in']          = True
                session['admin_id']           = row['id']
                session['admin_username']     = row['username']
                session['admin_display_name'] = row['display_name'] or row['username']
                session['admin_permissions']  = perms
                session['admin_is_super']     = bool(row['is_super'])
                with get_db() as conn:
                    conn.execute("UPDATE admin_accounts SET last_login_at=NOW() WHERE id=%s", (row['id'],))
                return redirect(url_for('admin_dashboard'))
            error = '帳號或密碼錯誤'
    return render_template('login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/admin')
@app.route('/admin/')
@login_required
def admin_dashboard():
    perms    = session.get('admin_permissions') or []
    is_super = bool(session.get('admin_is_super'))
    return render_template('admin.html',
        admin_display_name=session.get('admin_display_name',''),
        admin_permissions=perms,
        admin_is_super=is_super,
    )

# ── Admin Accounts API ────────────────────────────────────────────────────────

def _admin_row(r):
    if not r: return None
    d = dict(r)
    d.pop('password_hash', None)
    perms = d.get('permissions')
    if isinstance(perms, str):
        try: d['permissions'] = _json.loads(perms)
        except: d['permissions'] = []
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    if d.get('last_login_at'): d['last_login_at'] = d['last_login_at'].isoformat()
    return d

@app.route('/api/admin/me', methods=['GET'])
@login_required
def api_admin_me():
    return jsonify({
        'id':           session.get('admin_id'),
        'username':     session.get('admin_username'),
        'display_name': session.get('admin_display_name'),
        'permissions':  session.get('admin_permissions') or [],
        'is_super':     bool(session.get('admin_is_super')),
    })

@app.route('/api/admin/accounts', methods=['GET'])
@require_super
def api_admin_accounts_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM admin_accounts ORDER BY id").fetchall()
    return jsonify([_admin_row(r) for r in rows])

@app.route('/api/admin/accounts', methods=['POST'])
@require_super
def api_admin_account_create():
    b = request.get_json(force=True)
    username = b.get('username','').strip()
    password = b.get('password','').strip()
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password or len(password) < 4: return jsonify({'error': '密碼至少 4 個字元'}), 400
    perms = b.get('permissions', [])
    with get_db() as conn:
        try:
            row = conn.execute("""
                INSERT INTO admin_accounts (username, password_hash, display_name, permissions, is_super, active)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
            """, (username, _hash_pw(password), b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)), True)).fetchone()
        except Exception as e:
            if 'unique' in str(e).lower(): return jsonify({'error': '帳號已存在'}), 409
            return jsonify({'error': str(e)}), 500
    return jsonify(_admin_row(row)), 201

@app.route('/api/admin/accounts/<int:aid>', methods=['PUT'])
@require_super
def api_admin_account_update(aid):
    b = request.get_json(force=True)
    username = b.get('username','').strip()
    if not username: return jsonify({'error': '帳號為必填'}), 400
    password = b.get('password','').strip()
    perms = b.get('permissions', [])
    with get_db() as conn:
        if password:
            if len(password) < 4: return jsonify({'error': '密碼至少 4 個字元'}), 400
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, password_hash=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, _hash_pw(password), b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
    return jsonify(_admin_row(row)) if row else ('', 404)

@app.route('/api/admin/accounts/<int:aid>', methods=['DELETE'])
@require_super
def api_admin_account_delete(aid):
    if aid == session.get('admin_id'):
        return jsonify({'error': '不能刪除自己的帳號'}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM admin_accounts WHERE id=%s", (aid,))
    return jsonify({'deleted': aid})

# ─── Shared Helpers ───────────────────────────────────────────────────────────

def _hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def _gps_distance(lat1, lng1, lat2, lng2):
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return int(2 * R * math.asin(math.sqrt(a)))

def punch_staff_row(row):
    if not row: return None
    d = dict(row)
    d.pop('password_hash', None)
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
    if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
    return d

def punch_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['latitude', 'longitude']:
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('punched_at'): d['punched_at'] = d['punched_at'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def loc_row(row):
    if not row: return None
    d = dict(row)
    for f in ['lat', 'lng']:
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d

def punch_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('requested_at'): d['requested_at'] = d['requested_at'].isoformat()
    if d.get('reviewed_at'):  d['reviewed_at']  = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    return d

def ot_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('request_date'): d['request_date'] = d['request_date'].isoformat()
    if d.get('start_time'):   d['start_time']   = str(d['start_time'])[:5]
    if d.get('end_time'):     d['end_time']      = str(d['end_time'])[:5]
    if d.get('ot_pay'):       d['ot_pay']        = float(d['ot_pay'])
    if d.get('ot_hours'):     d['ot_hours']      = float(d['ot_hours'])
    if d.get('reviewed_at'):  d['reviewed_at']   = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']    = d['created_at'].isoformat()
    return d

def shift_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_time'): d['start_time'] = str(d['start_time'])[:5]
    if d.get('end_time'):   d['end_time']   = str(d['end_time'])[:5]
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def shift_assign_row(row):
    if not row: return None
    d = dict(row)
    if d.get('shift_date'): d['shift_date'] = d['shift_date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def sched_req_row(row):
    if not row: return None
    d = dict(row)
    if isinstance(d.get('dates'), str):
        try: d['dates'] = _json.loads(d['dates'])
        except: d['dates'] = []
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    return d

def get_schedule_config(conn, month):
    row = conn.execute("SELECT * FROM schedule_config WHERE month=%s", (month,)).fetchone()
    if not row:
        return {'month': month, 'max_off_per_day': 2, 'vacation_quota': 8, 'notes': ''}
    return dict(row)

def get_off_counts(conn, month):
    rows = conn.execute("""
        SELECT elem as d, COUNT(*) as cnt
        FROM schedule_requests,
             jsonb_array_elements_text(dates) as elem
        WHERE month=%s AND status IN ('approved','pending')
        GROUP BY elem
    """, (month,)).fetchall()
    return {r['d']: int(r['cnt']) for r in rows}

# ═══════════════════════════════════════════════════════════════════
# Employee Punch Page
# ═══════════════════════════════════════════════════════════════════

@app.route('/punch')
@app.route('/staff')
def punch_page():
    return render_template('staff.html')

# ── Employee Session ──────────────────────────────────────────────

@app.route('/api/punch/login', methods=['POST'])
def api_punch_login():
    b = request.get_json(force=True)
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '請輸入帳號及密碼'}), 400
    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE username=%s AND active=TRUE", (username,)
        ).fetchone()
    if not staff or staff['password_hash'] != _hash_pw(password):
        return jsonify({'error': '帳號或密碼錯誤'}), 401
    session['punch_staff_id']   = staff['id']
    session['punch_staff_name'] = staff['name']
    return jsonify({'id': staff['id'], 'name': staff['name'], 'role': staff['role']})

@app.route('/api/punch/logout', methods=['POST'])
def api_punch_logout():
    session.pop('punch_staff_id', None)
    session.pop('punch_staff_name', None)
    return jsonify({'ok': True})

@app.route('/api/punch/me', methods=['GET'])
def api_punch_me():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        staff = conn.execute(
            "SELECT id,name,role FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
    if not staff:
        session.pop('punch_staff_id', None)
        return jsonify({'error': 'not logged in'}), 401
    return jsonify(dict(staff))

# ── GPS Settings ──────────────────────────────────────────────────

@app.route('/api/punch/settings', methods=['GET'])
def api_punch_settings_get():
    """Public: GPS config + active locations for the punch page."""
    with get_db() as conn:
        cfg  = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
        locs = conn.execute(
            "SELECT * FROM punch_locations WHERE active=TRUE ORDER BY id"
        ).fetchall()
    return jsonify({
        'gps_required': cfg['gps_required'] if cfg else False,
        'locations': [loc_row(r) for r in locs]
    })

@app.route('/api/punch/config', methods=['PUT'])
@login_required
def api_punch_config_update():
    b = request.get_json(force=True)
    gps_required = bool(b.get('gps_required', False))
    with get_db() as conn:
        conn.execute(
            "UPDATE punch_config SET gps_required=%s, updated_at=NOW() WHERE id=1",
            (gps_required,)
        )
    return jsonify({'gps_required': gps_required})

@app.route('/api/punch/locations', methods=['GET'])
@login_required
def api_punch_locations_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_locations ORDER BY id").fetchall()
    return jsonify([loc_row(r) for r in rows])

@app.route('/api/punch/locations', methods=['POST'])
@login_required
def api_punch_locations_create():
    b = request.get_json(force=True)
    name = b.get('location_name', '').strip() or '打卡地點'
    try:
        lat = float(b['lat']); lng = float(b['lng'])
    except Exception:
        return jsonify({'error': '請填入有效的緯度和經度'}), 400
    radius_m = int(b.get('radius_m') or 100)
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO punch_locations (location_name, lat, lng, radius_m) VALUES (%s,%s,%s,%s) RETURNING *",
            (name, lat, lng, radius_m)
        ).fetchone()
    return jsonify(loc_row(row)), 201

@app.route('/api/punch/locations/<int:lid>', methods=['PUT'])
@login_required
def api_punch_locations_update(lid):
    b = request.get_json(force=True)
    name = b.get('location_name', '').strip() or '打卡地點'
    try:
        lat = float(b['lat']); lng = float(b['lng'])
    except Exception:
        return jsonify({'error': '請填入有效的緯度和經度'}), 400
    radius_m = int(b.get('radius_m') or 100)
    active   = bool(b.get('active', True))
    with get_db() as conn:
        row = conn.execute(
            "UPDATE punch_locations SET location_name=%s,lat=%s,lng=%s,radius_m=%s,active=%s,updated_at=NOW() WHERE id=%s RETURNING *",
            (name, lat, lng, radius_m, active, lid)
        ).fetchone()
    return jsonify(loc_row(row)) if row else ('', 404)

@app.route('/api/punch/locations/<int:lid>', methods=['DELETE'])
@login_required
def api_punch_locations_delete(lid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_locations WHERE id=%s", (lid,))
    return jsonify({'deleted': lid})

# ── Clock In/Out ──────────────────────────────────────────────────

@app.route('/api/punch/clock', methods=['POST'])
def api_punch_clock():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401

    b          = request.get_json(force=True)
    punch_type = b.get('punch_type')
    lat        = b.get('lat')
    lng        = b.get('lng')

    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400

    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
        if not staff:
            return jsonify({'error': '員工不存在'}), 404
        cfg  = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
        locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()

    gps_required = cfg['gps_required'] if cfg else False
    gps_distance = None
    matched_loc  = None

    if lat is not None and lng is not None and locs:
        for loc in locs:
            d = _gps_distance(lat, lng, float(loc['lat']), float(loc['lng']))
            if gps_distance is None or d < gps_distance:
                gps_distance = d
                matched_loc  = loc

    if gps_required:
        if lat is None or lng is None:
            return jsonify({'error': '無法取得 GPS，請允許定位權限後重試'}), 403
        if not locs:
            return jsonify({'error': '管理員尚未設定任何打卡地點'}), 403
        if gps_distance is None or gps_distance > int(matched_loc['radius_m']):
            return jsonify({
                'error': f'距離最近地點「{matched_loc["location_name"]}」{gps_distance} 公尺，超出允許範圍（{matched_loc["radius_m"]} 公尺）',
                'distance': gps_distance,
                'radius': int(matched_loc['radius_m'])
            }), 403

    with get_db() as conn:
        recent = conn.execute("""
            SELECT id FROM punch_records
            WHERE staff_id=%s AND punch_type=%s
              AND punched_at > NOW() - INTERVAL '1 minute'
        """, (sid, punch_type)).fetchone()
        if recent:
            return jsonify({'error': '1 分鐘內已打過卡'}), 429

        matched_name = matched_loc['location_name'] if matched_loc else ''
        row = conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, latitude, longitude, gps_distance, location_name)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, punch_type, lat, lng, gps_distance, matched_name)).fetchone()

    d = punch_record_row(row)
    d['staff_name']   = staff['name']
    d['gps_distance'] = gps_distance
    return jsonify(d), 201

@app.route('/api/punch/today', methods=['GET'])
def api_punch_today():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify([])
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.*, ps.name as staff_name
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE pr.staff_id=%s
              AND (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
                = (NOW() AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY pr.punched_at ASC
        """, (sid,)).fetchall()
    return jsonify([punch_record_row(r) for r in rows])

@app.route('/api/punch/my-records', methods=['GET'])
def api_punch_my_records():
    """Employee self-service: own punch records for a month."""
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    month = request.args.get('month', '')
    if not month:
        from datetime import timezone as _tz, timedelta as _tda
        month = _dt.now(_tz(_tda(hours=8))).strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND to_char(punched_at AT TIME ZONE 'Asia/Taipei', 'YYYY-MM') = %s
            ORDER BY punched_at ASC
        """, (sid, month)).fetchall()
    from datetime import timezone as _tz2, timedelta as _tdb
    TW = _tz2(_tdb(hours=8))
    LABEL = {'in': '上班', 'out': '下班', 'break_out': '休息開始', 'break_in': '休息結束'}
    result = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            from datetime import timezone as _utz
            pa = pa.replace(tzinfo=_utz.utc)
        pa_tw    = pa.astimezone(TW)
        date_str = pa_tw.strftime('%Y-%m-%d')
        time_str = pa_tw.strftime('%H:%M')
        if date_str not in result:
            result[date_str] = []
        result[date_str].append({
            'type':          r['punch_type'],
            'label':         LABEL.get(r['punch_type'], r['punch_type']),
            'time':          time_str,
            'gps_distance':  r['gps_distance'],
            'location_name': r['location_name'] or '',
            'is_manual':     bool(r['is_manual']),
        })
    return jsonify({'month': month, 'records': result})

# ── Admin: Staff CRUD ─────────────────────────────────────────────

@app.route('/api/punch/staff', methods=['GET'])
@login_required
def api_punch_staff_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_staff ORDER BY name").fetchall()
    return jsonify([punch_staff_row(r) for r in rows])

@app.route('/api/punch/staff', methods=['POST'])
@login_required
def api_punch_staff_create():
    b        = request.get_json(force=True)
    name     = b.get('name', '').strip()
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not name:     return jsonify({'error': '姓名為必填'}), 400
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password or len(password) < 4:
        return jsonify({'error': '密碼至少 4 個字元'}), 400
    employee_code = b.get('employee_code', '') or None
    if employee_code: employee_code = employee_code.strip() or None
    try:
        with get_db() as conn:
            row = conn.execute("""
                INSERT INTO punch_staff (name, username, password_hash, role, employee_code)
                VALUES (%s,%s,%s,%s,%s) RETURNING *
            """, (name, username, _hash_pw(password), b.get('role', '').strip(), employee_code)).fetchone()
        return jsonify(punch_staff_row(row)), 201
    except psycopg.errors.UniqueViolation:
        return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
    except Exception as e:
        print(f"[punch_staff_create] error: {e}")
        # Check if it's a unique constraint in the error message
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
        return jsonify({'error': f'新增失敗：{str(e)}'}), 500

@app.route('/api/punch/staff/<int:sid>', methods=['PUT'])
@login_required
def api_punch_staff_update(sid):
    b             = request.get_json(force=True)
    name          = b.get('name', '').strip()
    username      = b.get('username', '').strip()
    password      = b.get('password', '').strip()
    role          = b.get('role', '').strip()
    active        = bool(b.get('active', True))
    employee_code = b.get('employee_code', '') or None
    if employee_code: employee_code = employee_code.strip() or None
    if not name or not username:
        return jsonify({'error': '姓名和帳號為必填'}), 400
    with get_db() as conn:
        if password:
            if len(password) < 4:
                return jsonify({'error': '密碼至少 4 個字元'}), 400
            row = conn.execute("""
                UPDATE punch_staff
                SET name=%s,username=%s,password_hash=%s,role=%s,active=%s,employee_code=%s
                WHERE id=%s RETURNING *
            """, (name, username, _hash_pw(password), role, active, employee_code, sid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE punch_staff
                SET name=%s,username=%s,role=%s,active=%s,employee_code=%s
                WHERE id=%s RETURNING *
            """, (name, username, role, active, employee_code, sid)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)

@app.route('/api/punch/staff/<int:sid>', methods=['DELETE'])
@login_required
def api_punch_staff_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_staff WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})

# ── Admin: Punch Records ──────────────────────────────────────────

@app.route('/api/punch/records', methods=['GET'])
@login_required
def api_punch_records():
    staff_id  = request.args.get('staff_id')
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    month     = request.args.get('month')

    conds, params = ["TRUE"], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if month:
        conds.append("TO_CHAR(pr.punched_at,'YYYY-MM')=%s"); params.append(month)
    elif date_from:
        conds.append("(pr.punched_at AT TIME ZONE 'Asia/Taipei')::date>=%s"); params.append(date_from)
        if date_to:
            conds.append("(pr.punched_at AT TIME ZONE 'Asia/Taipei')::date<=%s"); params.append(date_to)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.punched_at DESC LIMIT 500
        """, params).fetchall()
    return jsonify([punch_record_row(r) for r in rows])

@app.route('/api/punch/records', methods=['POST'])
@login_required
def api_punch_record_manual():
    b          = request.get_json(force=True)
    staff_id   = b.get('staff_id')
    punch_type = b.get('punch_type')
    punched_at = b.get('punched_at')
    note       = b.get('note', '').strip()
    manual_by  = b.get('manual_by', '').strip()
    if not all([staff_id, punch_type, punched_at]):
        return jsonify({'error': '缺少必要欄位'}), 400
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, punched_at, note, is_manual, manual_by)
            VALUES (%s,%s,%s,%s,TRUE,%s) RETURNING *
        """, (staff_id, punch_type, punched_at, note, manual_by)).fetchone()
        staff = conn.execute("SELECT name FROM punch_staff WHERE id=%s", (staff_id,)).fetchone()
    d = punch_record_row(row)
    if staff: d['staff_name'] = staff['name']
    return jsonify(d), 201

@app.route('/api/punch/records/<int:rid>', methods=['PUT'])
@login_required
def api_punch_record_update(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_records
            SET punch_type=%s, punched_at=%s, note=%s, is_manual=TRUE, manual_by=%s
            WHERE id=%s RETURNING *
        """, (b.get('punch_type'), b.get('punched_at'),
              b.get('note', ''), b.get('manual_by', ''), rid)).fetchone()
    return jsonify(punch_record_row(row)) if row else ('', 404)

@app.route('/api/punch/records/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

@app.route('/api/punch/summary', methods=['GET'])
@login_required
def api_punch_summary():
    from datetime import datetime as _dtnow
    month = request.args.get('month') or _dtnow.now().strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT ps.id as staff_id, ps.name as staff_name,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_out,
                   COUNT(*) as punch_count,
                   BOOL_OR(pr.is_manual) as has_manual
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            GROUP BY ps.id, ps.name, (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date DESC, ps.name
        """, (month,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['work_date']  = d['work_date'].isoformat()  if d['work_date']  else None
        d['clock_in']   = d['clock_in'].isoformat()   if d['clock_in']   else None
        d['clock_out']  = d['clock_out'].isoformat()  if d['clock_out']  else None
        if d['clock_in'] and d['clock_out']:
            from datetime import datetime as _dt2
            ci = _dt2.fromisoformat(d['clock_in'].replace('Z', ''))
            co = _dt2.fromisoformat(d['clock_out'].replace('Z', ''))
            d['duration_min'] = max(0, int((co - ci).total_seconds() / 60))
        else:
            d['duration_min'] = None
        result.append(d)
    return jsonify(result)

# ── Punch Requests (補打卡申請) ───────────────────────────────────

@app.route('/api/punch/request', methods=['POST'])
def api_punch_req_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b            = request.get_json(force=True)
    punch_type   = b.get('punch_type')
    requested_at = b.get('requested_at')
    reason       = b.get('reason', '').strip()
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    if not requested_at:
        return jsonify({'error': '請選擇補打時間'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO punch_requests (staff_id, punch_type, requested_at, reason)
            VALUES (%s,%s,%s,%s) RETURNING *
        """, (sid, punch_type, requested_at, reason)).fetchone()
    return jsonify(punch_req_row(row)), 201

@app.route('/api/punch/request/my', methods=['GET'])
def api_punch_req_my():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM punch_requests WHERE staff_id=%s ORDER BY requested_at DESC LIMIT 20",
            (sid,)
        ).fetchall()
    return jsonify([punch_req_row(r) for r in rows])

@app.route('/api/punch/requests', methods=['GET'])
@login_required
def api_punch_reqs_list():
    status = request.args.get('status', '')
    conds, params = ['TRUE'], []
    if status: conds.append('pr.status=%s'); params.append(status)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_requests pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.created_at DESC LIMIT 200
        """, params).fetchall()
    return jsonify([punch_req_row(r) for r in rows])

@app.route('/api/punch/requests/<int:rid>', methods=['PUT'])
@login_required
def api_punch_req_review(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'invalid action'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_requests
            SET status=%s, reviewed_by=%s, review_note=%s, reviewed_at=NOW()
            WHERE id=%s
            RETURNING *, (SELECT name FROM punch_staff WHERE id=staff_id) as staff_name
        """, (new_status, reviewed_by, review_note, rid)).fetchone()
        if not row: return ('', 404)
        if action == 'approve':
            conn.execute("""
                INSERT INTO punch_records
                  (staff_id, punch_type, punched_at, note, is_manual, manual_by)
                VALUES (%s,%s,%s,%s,TRUE,%s)
            """, (row['staff_id'], row['punch_type'], row['requested_at'],
                  f'補打卡申請 #{rid}：{row["reason"]}', reviewed_by))
    return jsonify(punch_req_row(row))

@app.route('/api/punch/requests/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_req_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

# ═══════════════════════════════════════════════════════════════════
# LINE Punch Clock
# ═══════════════════════════════════════════════════════════════════

CUSTOM_RICHMENU_IMAGE_PATH = '/tmp/custom_richmenu.png'
_pending_line_punches = {}   # {line_user_id: punch_type}


def get_line_punch_config():
    if not DATABASE_URL: return None
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _send_line_punch(user_id, text):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    try:
        LineBotApi(cfg['channel_access_token']).push_message(
            user_id, TextSendMessage(text=text)
        )
    except Exception as e:
        print(f"[LINE PUNCH] push_message error: {e}")


@app.route('/line-punch/webhook', methods=['POST'])
def line_punch_webhook():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_secret'):
        return 'disabled', 200

    signature = request.headers.get('X-Line-Signature', '')
    body      = request.get_data(as_text=True)

    import hmac, hashlib as _hl, base64 as _b64
    secret   = cfg['channel_secret'].encode('utf-8')
    computed = _b64.b64encode(
        hmac.new(secret, body.encode('utf-8'), _hl.sha256).digest()
    ).decode('utf-8')
    if not hmac.compare_digest(computed, signature):
        return 'Invalid signature', 400

    events = _json.loads(body).get('events', [])
    for event in events:
        try:
            _handle_line_punch_event(event, cfg)
        except Exception as e:
            print(f"[LINE PUNCH] event handler error: {e}\n{traceback.format_exc()}")
    return 'OK', 200


def _handle_line_punch_event(event, cfg):
    source   = event.get('source', {})
    user_id  = source.get('userId')
    evt_type = event.get('type')
    if not user_id: return

    msg      = event.get('message', {})
    msg_type = msg.get('type', '')

    if evt_type == 'follow':
        _send_line_punch(user_id,
            '歡迎使用員工打卡系統！👋\n\n'
            '請輸入您的登入帳號完成綁定。\n\n'
            '✏️ 輸入範例：\n  綁定 mary123\n'
            '（請將 mary123 換成您自己的帳號）\n\n'
            '不知道帳號？請詢問管理員。')
        return

    if evt_type != 'message': return

    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE line_user_id=%s AND active=TRUE", (user_id,)
        ).fetchone()

    # ── Not bound yet ─────────────────────────────────────────
    if not staff:
        if msg_type == 'text':
            text = msg.get('text', '').strip()
            if text.startswith('綁定 ') or text.startswith('绑定 '):
                username = text.split(' ', 1)[1].strip()
                if username in ('帳號', '您的帳號', '[您的帳號]', 'username', '帳號名稱'):
                    _send_line_punch(user_id,
                        '請輸入您「實際的」登入帳號，而非說明文字。\n\n'
                        '範例：綁定 mary123')
                    return
                with get_db() as conn:
                    candidate = conn.execute(
                        "SELECT * FROM punch_staff WHERE username=%s AND active=TRUE",
                        (username,)
                    ).fetchone()
                if not candidate:
                    _send_line_punch(user_id,
                        f'找不到帳號「{username}」\n\n'
                        '請確認帳號是否正確，或詢問管理員您的登入帳號。')
                    return
                if candidate['line_user_id']:
                    _send_line_punch(user_id, '此帳號已綁定其他 LINE 帳號，請聯絡管理員。')
                    return
                with get_db() as conn:
                    conn.execute(
                        "UPDATE punch_staff SET line_user_id=%s WHERE id=%s",
                        (user_id, candidate['id'])
                    )
                _send_line_punch(user_id,
                    f'✅ 綁定成功！\n歡迎 {candidate["name"]}！\n\n'
                    '打卡方式：\n📍 傳送位置訊息 → 自動打卡\n'
                    '💬 或輸入：上班 / 下班 / 休息 / 回來\n\n'
                    '輸入「狀態」可查看今日打卡記錄。')
            else:
                _send_line_punch(user_id,
                    '您尚未綁定打卡帳號。\n\n'
                    '請輸入您的登入帳號：\n  綁定 [您的帳號]\n\n'
                    '範例：綁定 mary123')
        return

    # ── Bound staff ───────────────────────────────────────────
    PUNCH_CMDS = {
        '上班': 'in', '上班打卡': 'in',
        '下班': 'out', '下班打卡': 'out',
        '休息': 'break_out', '休息開始': 'break_out',
        '回來': 'break_in', '休息結束': 'break_in',
    }
    PUNCH_LABEL = {
        'in': '上班打卡', 'out': '下班打卡',
        'break_out': '休息開始', 'break_in': '休息結束',
    }

    if msg_type == 'location':
        lat = msg.get('latitude'); lng = msg.get('longitude')
        _do_line_punch(staff, user_id, lat, lng, None, PUNCH_LABEL)

    elif msg_type == 'text':
        text = msg.get('text', '').strip()

        if text in ('狀態', '打卡記錄'):
            _send_status(staff, user_id); return

        if text == '解除綁定':
            with get_db() as conn:
                conn.execute("UPDATE punch_staff SET line_user_id=NULL WHERE id=%s", (staff['id'],))
            _send_line_punch(user_id, '已解除 LINE 帳號綁定。'); return

        punch_type = PUNCH_CMDS.get(text)
        if punch_type:
            with get_db() as conn:
                pcfg = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
                locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()
            gps_required = pcfg['gps_required'] if pcfg else False
            if gps_required and locs:
                _send_line_punch(user_id,
                    f'請傳送您的位置來完成{PUNCH_LABEL[punch_type]}\n'
                    '點下方「傳送位置」按鈕即可打卡')
                _pending_line_punches[user_id] = punch_type
            else:
                _do_line_punch(staff, user_id, None, None, punch_type, PUNCH_LABEL)
        else:
            _send_line_punch(user_id,
                f'哈囉 {staff["name"]}！\n\n'
                '打卡指令：\n📍 傳送位置 → 自動打卡\n'
                '💬 上班 / 下班 / 休息 / 回來\n'
                '📋 狀態 → 查看今日記錄')


def _do_line_punch(staff, user_id, lat, lng, forced_type, PUNCH_LABEL):
    from datetime import datetime as _dt3, timezone as _tz3, timedelta as _td3
    TW = _tz3(_td3(hours=8))

    # Determine punch type
    if forced_type:
        punch_type = forced_type
    elif user_id in _pending_line_punches:
        punch_type = _pending_line_punches.pop(user_id)
    else:
        with get_db() as conn:
            last = conn.execute("""
                SELECT punch_type FROM punch_records
                WHERE staff_id=%s
                  AND (punched_at AT TIME ZONE 'Asia/Taipei')::date
                    = (NOW() AT TIME ZONE 'Asia/Taipei')::date
                ORDER BY punched_at DESC LIMIT 1
            """, (staff['id'],)).fetchone()
        if not last:                               punch_type = 'in'
        elif last['punch_type'] == 'in':           punch_type = 'out'
        elif last['punch_type'] == 'break_out':    punch_type = 'break_in'
        else:                                      punch_type = 'in'

    label = PUNCH_LABEL.get(punch_type, punch_type)

    # GPS check
    gps_distance = None; matched_name = ''
    if lat is not None and lng is not None:
        with get_db() as conn:
            pcfg = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
            locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()
        gps_required = pcfg['gps_required'] if pcfg else False
        if locs:
            min_dist = None; min_loc = None
            for loc in locs:
                d = _gps_distance(lat, lng, float(loc['lat']), float(loc['lng']))
                if min_dist is None or d < min_dist:
                    min_dist = d; min_loc = loc
            gps_distance = min_dist
            matched_name = min_loc['location_name'] if min_loc else ''
            if gps_required and min_dist > int(min_loc['radius_m']):
                _send_line_punch(user_id,
                    f'❌ {label}失敗\n'
                    f'您距離「{min_loc["location_name"]}」{min_dist} 公尺\n'
                    f'超出允許範圍 {min_loc["radius_m"]} 公尺\n\n'
                    '請確認您在正確地點後重試。')
                return

    # Duplicate guard
    with get_db() as conn:
        recent = conn.execute("""
            SELECT id FROM punch_records
            WHERE staff_id=%s AND punch_type=%s
              AND punched_at > NOW() - INTERVAL '1 minute'
        """, (staff['id'], punch_type)).fetchone()
        if recent:
            _send_line_punch(user_id, f'⚠️ 1 分鐘內已打過{label}，請勿重複打卡。'); return

        conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, latitude, longitude, gps_distance, location_name)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (staff['id'], punch_type, lat, lng, gps_distance, matched_name))

    now      = _dt3.now(TW)
    gps_info = f'\n📍 {matched_name} ({gps_distance}m)' if gps_distance is not None else ''
    _send_line_punch(user_id,
        f'✅ {label}成功\n'
        f'👤 {staff["name"]}\n'
        f'🕐 {now.strftime("%Y/%m/%d %H:%M")}'
        f'{gps_info}')


def _send_status(staff, user_id):
    from datetime import timezone as _tz4, timedelta as _td4
    TW = _tz4(_td4(hours=8))
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND (punched_at AT TIME ZONE 'Asia/Taipei')::date
                = (NOW() AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY punched_at ASC
        """, (staff['id'],)).fetchall()
    LABEL = {'in': '上班', 'out': '下班', 'break_out': '休息開始', 'break_in': '休息結束'}
    if not rows:
        _send_line_punch(user_id, f'📋 {staff["name"]} 今日尚無打卡記錄。'); return
    lines = [f'📋 {staff["name"]} 今日打卡記錄']
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            from datetime import timezone as _utz2
            pa = pa.replace(tzinfo=_utz2.utc)
        t    = pa.astimezone(TW).strftime('%H:%M')
        dist = f' ({r["gps_distance"]}m)' if r['gps_distance'] is not None else ''
        man  = ' [補打]' if r['is_manual'] else ''
        lines.append(f'• {LABEL.get(r["punch_type"], r["punch_type"])} {t}{dist}{man}')
    _send_line_punch(user_id, '\n'.join(lines))

# ── Admin LINE Punch Config API ────────────────────────────────────

@app.route('/api/line-punch/config', methods=['GET'])
@login_required
def api_line_punch_config_get():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
    if not row:
        return jsonify({'enabled': False, 'channel_access_token': '', 'channel_secret': ''})
    d = dict(row)
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return jsonify(d)

@app.route('/api/line-punch/config', methods=['PUT'])
@login_required
def api_line_punch_config_put():
    b       = request.get_json(force=True)
    token   = b.get('channel_access_token', '').strip()
    secret  = b.get('channel_secret', '').strip()
    enabled = bool(b.get('enabled', False))
    with get_db() as conn:
        conn.execute("""
            UPDATE line_punch_config
            SET channel_access_token=%s, channel_secret=%s, enabled=%s, updated_at=NOW()
            WHERE id=1
        """, (token, secret, enabled))
    return jsonify({'ok': True, 'enabled': enabled})

@app.route('/api/line-punch/staff', methods=['GET'])
@login_required
def api_line_punch_staff():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,name,username,role,active,line_user_id FROM punch_staff ORDER BY name"
        ).fetchall()
    return jsonify([{
        'id': r['id'], 'name': r['name'], 'username': r['username'],
        'role': r['role'], 'active': r['active'],
        'line_bound': bool(r['line_user_id']),
        'line_user_id': r['line_user_id'] or ''
    } for r in rows])

@app.route('/api/line-punch/staff/<int:sid>/unbind', methods=['POST'])
@login_required
def api_line_punch_unbind(sid):
    with get_db() as conn:
        conn.execute("UPDATE punch_staff SET line_user_id=NULL WHERE id=%s", (sid,))
    return jsonify({'ok': True})

# ── Rich Menu ──────────────────────────────────────────────────────

def _call_line_api(cfg, method, path, body=None):
    token = cfg.get('channel_access_token', '')
    url   = 'https://api.line.me/v2/bot' + path
    data  = _json.dumps(body).encode('utf-8') if body else None
    req   = urllib.request.Request(
        url, data=data, method=method,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {'error': e.read().decode('utf-8', errors='replace')}
    except Exception as e:
        return 0, {'error': str(e)}


def _make_richmenu_png():
    """Generate a simple 2500×1686 PNG with 4 colored quadrants."""
    import struct, zlib
    W, H = 2500, 1686
    colors = [(0x2e,0x9e,0x6b), (0xd6,0x42,0x42), (0xe0,0x7b,0x2a), (0x4a,0x7b,0xda)]
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            p = (0 if y < 843 else 1) * 2 + (0 if x < 1250 else 1)
            r, g, b = colors[p]
            if x in (1249, 1250) or y in (842, 843):
                r, g, b = 0x0f, 0x1c, 0x3a
            row += bytes([r, g, b])
        rows.append(bytes([0]) + bytes(row))
    compressed = zlib.compress(b''.join(rows), 1)

    def chunk(name, data):
        c = struct.pack('>I', len(data)) + name + data
        return c + struct.pack('>I', zlib.crc32(c[4:]) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', compressed)
            + chunk(b'IEND', b''))


@app.route('/api/line-punch/richmenu/create', methods=['POST'])
@login_required
def api_richmenu_create():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '請先設定 Channel Access Token'}), 400

    staff_url = (RENDER_EXTERNAL_URL or request.host_url).rstrip('/') + '/staff'
    body = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "打卡選單",
        "chatBarText": "打卡",
        "areas": [
            {"bounds": {"x": 0,    "y": 0,   "width": 1250, "height": 843}, "action": {"type": "message", "text": "上班"}},
            {"bounds": {"x": 1250, "y": 0,   "width": 1250, "height": 843}, "action": {"type": "message", "text": "下班"}},
            {"bounds": {"x": 0,    "y": 843, "width": 1250, "height": 843}, "action": {"type": "message", "text": "休息"}},
            {"bounds": {"x": 1250, "y": 843, "width": 1250, "height": 843}, "action": {"type": "message", "text": "回來"}},
        ]
    }

    status, data = _call_line_api(cfg, 'POST', '/richmenu', body)
    if status != 200:
        return jsonify({'error': f'建立失敗 ({status}): {data.get("error","")}'}), 500

    rich_menu_id = data.get('richMenuId', '')

    # Upload image — try custom first, then auto-generate
    png_bytes = None
    try:
        import os
        for _cp in [CUSTOM_RICHMENU_IMAGE_PATH,
                    CUSTOM_RICHMENU_IMAGE_PATH.replace('.png', '.jpg')]:
            if os.path.exists(_cp):
                with open(_cp, 'rb') as f:
                    png_bytes = f.read()
                break
    except Exception:
        pass
    if not png_bytes:
        try:
            png_bytes = _make_richmenu_png()
        except Exception:
            pass

    img_ok = False
    if png_bytes:
        content_type = 'image/jpeg' if png_bytes[:2] == b'\xff\xd8' else 'image/png'
        upload_url = f'https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content'
        req = urllib.request.Request(
            upload_url, data=png_bytes, method='POST',
            headers={'Content-Type': content_type, 'Authorization': f'Bearer {cfg["channel_access_token"]}'}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                img_ok = resp.status in (200, 204)
        except Exception:
            pass

    # Set as default for all users
    _call_line_api(cfg, 'POST', f'/user/all/richmenu/{rich_menu_id}')

    return jsonify({'ok': True, 'rich_menu_id': rich_menu_id, 'image_uploaded': img_ok})


@app.route('/api/line-punch/richmenu/list', methods=['GET'])
@login_required
def api_richmenu_list():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'menus': []})
    status, data = _call_line_api(cfg, 'GET', '/richmenu/list')
    if status != 200:
        return jsonify({'menus': [], 'error': data.get('error', '')})
    return jsonify({'menus': data.get('richmenus', [])})


@app.route('/api/line-punch/richmenu/<rich_menu_id>', methods=['DELETE'])
@login_required
def api_richmenu_delete(rich_menu_id):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '未設定 Token'}), 400
    _call_line_api(cfg, 'DELETE', '/user/all/richmenu')
    status, _ = _call_line_api(cfg, 'DELETE', f'/richmenu/{rich_menu_id}')
    return jsonify({'ok': status in (200, 204), 'status': status})


@app.route('/api/line-punch/richmenu/default', methods=['DELETE'])
@login_required
def api_richmenu_unset_default():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '未設定 Token'}), 400
    status, _ = _call_line_api(cfg, 'DELETE', '/user/all/richmenu')
    return jsonify({'ok': status in (200, 204)})

# ═══════════════════════════════════════════════════════════════════
# Schedule / Shift API
# ═══════════════════════════════════════════════════════════════════

# ── Employee: schedule config + my request ────────────────────────

@app.route('/api/schedule/config/<month>', methods=['GET'])
def api_sched_config_get(month):
    sid = session.get('punch_staff_id')
    with get_db() as conn:
        cfg    = dict(get_schedule_config(conn, month))
        counts = get_off_counts(conn, month)
        if sid:
            row = conn.execute(
                "SELECT vacation_quota FROM punch_staff WHERE id=%s", (sid,)
            ).fetchone()
            if row and row['vacation_quota'] is not None:
                cfg['vacation_quota']  = int(row['vacation_quota'])
                cfg['quota_personal']  = True
    return jsonify({**cfg, 'off_counts': counts})


@app.route('/api/schedule/my-request/<month>', methods=['GET'])
def api_sched_my_request(month):
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name
            FROM schedule_requests sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.staff_id=%s AND sr.month=%s
        """, (sid, month)).fetchone()
    return jsonify(sched_req_row(row)) if row else jsonify(None)


@app.route('/api/schedule/my-request', methods=['POST'])
def api_sched_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    dates = b.get('dates', [])
    note  = b.get('submit_note', '').strip()

    if not month: return jsonify({'error': '請選擇月份'}), 400
    if not isinstance(dates, list): return jsonify({'error': '日期格式錯誤'}), 400
    for d in dates:
        if not d.startswith(month):
            return jsonify({'error': f'日期 {d} 不屬於 {month}'}), 400

    try:
        with get_db() as conn:
            cfg = get_schedule_config(conn, month)

            staff_row = conn.execute(
                "SELECT vacation_quota FROM punch_staff WHERE id=%s", (sid,)
            ).fetchone()
            personal_quota  = staff_row['vacation_quota'] if staff_row and staff_row['vacation_quota'] is not None else None
            effective_quota = personal_quota if personal_quota is not None else cfg['vacation_quota']

            if len(dates) > effective_quota:
                quota_source = '個人配額' if personal_quota is not None else '月份預設配額'
                return jsonify({'error': f'申請天數（{len(dates)}天）超過{quota_source}（{effective_quota}天）'}), 422

            overcrowded = []
            for d in dates:
                try:
                    others = conn.execute("""
                        SELECT COUNT(*) as cnt
                        FROM schedule_requests,
                             jsonb_array_elements_text(dates) as elem
                        WHERE month=%s AND status IN ('approved','pending')
                          AND staff_id != %s AND elem=%s
                    """, (month, sid, d)).fetchone()
                    others_count = int(others['cnt']) if others else 0
                except Exception:
                    others_count = 0
                if others_count >= cfg['max_off_per_day']:
                    dt_obj = _dt.strptime(d, '%Y-%m-%d')
                    overcrowded.append({
                        'date': d,
                        'weekday': WEEKDAY_ZH[dt_obj.weekday()],
                        'count': others_count,
                        'max': cfg['max_off_per_day']
                    })
            if overcrowded:
                msgs = [f"{x['date']}（{x['weekday']}）已有 {x['count']} 人排休" for x in overcrowded]
                return jsonify({'error': '以下日期休假人數已達上限：' + '、'.join(msgs), 'overcrowded': overcrowded}), 422

            prev = conn.execute(
                "SELECT status FROM schedule_requests WHERE staff_id=%s AND month=%s",
                (sid, month)
            ).fetchone()
            new_status = 'modified_pending' if prev and prev['status'] == 'approved' else 'pending'
            dates_json = _json.dumps(dates, ensure_ascii=False)

            row = conn.execute("""
                INSERT INTO schedule_requests
                  (staff_id, month, dates, status, submit_note, updated_at)
                VALUES (%s, %s, %s::jsonb, %s, %s, NOW())
                ON CONFLICT (staff_id, month) DO UPDATE
                  SET dates=EXCLUDED.dates, status=EXCLUDED.status,
                      submit_note=EXCLUDED.submit_note, updated_at=NOW()
                RETURNING *
            """, (sid, month, dates_json, new_status, note)).fetchone()

        return jsonify(sched_req_row(row)), 201
    except Exception as e:
        import traceback as _tb
        print(f"[SCHED SUBMIT ERROR] {e}\n{_tb.format_exc()}")
        return jsonify({'error': f'系統錯誤：{str(e)}'}), 500

# ── Admin: schedule config ────────────────────────────────────────

@app.route('/api/schedule/admin/config/<month>', methods=['GET'])
@require_module('sched')
def api_sched_admin_config_get(month):
    with get_db() as conn:
        cfg    = get_schedule_config(conn, month)
        counts = get_off_counts(conn, month)
    return jsonify({**cfg, 'off_counts': counts})


@app.route('/api/schedule/admin/config/<month>', methods=['PUT'])
@require_module('sched')
def api_sched_admin_config_put(month):
    b       = request.get_json(force=True)
    max_off = int(b.get('max_off_per_day') or 2)
    quota   = int(b.get('vacation_quota')   or 8)
    notes   = b.get('notes', '').strip()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO schedule_config (month, max_off_per_day, vacation_quota, notes)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (month) DO UPDATE
              SET max_off_per_day=%s, vacation_quota=%s, notes=%s, updated_at=NOW()
        """, (month, max_off, quota, notes, max_off, quota, notes))
    return jsonify({'month': month, 'max_off_per_day': max_off,
                    'vacation_quota': quota, 'notes': notes})

# ── Admin: schedule requests ──────────────────────────────────────

@app.route('/api/schedule/admin/requests', methods=['GET'])
@require_module('sched')
def api_sched_admin_requests():
    month  = request.args.get('month', '')
    status = request.args.get('status', '')
    conds, params = ['TRUE'], []
    if month:  conds.append('sr.month=%s');  params.append(month)
    if status: conds.append('sr.status=%s'); params.append(status)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role
            FROM schedule_requests sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY sr.month DESC, ps.name
        """, params).fetchall()
    return jsonify([sched_req_row(r) for r in rows])


@app.route('/api/schedule/admin/requests/<int:rid>', methods=['PUT'])
@require_module('sched')
def api_sched_admin_review(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject', 'revoke'):
        return jsonify({'error': 'action must be approve / reject / revoke'}), 400

    if action == 'revoke':
        with get_db() as conn:
            row = conn.execute("""
                UPDATE schedule_requests
                SET status='pending', reviewed_by='', review_note=%s,
                    reviewed_at=NULL, updated_at=NOW()
                WHERE id=%s RETURNING *
            """, (review_note or '主管已撤銷核准', rid)).fetchone()
        return jsonify(sched_req_row(row)) if row else ('', 404)

    new_status = 'approved' if action == 'approve' else 'rejected'
    with get_db() as conn:
        row = conn.execute("""
            UPDATE schedule_requests
            SET status=%s, reviewed_by=%s, review_note=%s,
                reviewed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (new_status, reviewed_by, review_note, rid)).fetchone()
    if row:
        dates = row['dates'] if isinstance(row['dates'], list) else _json.loads(row['dates'] or '[]')
        extra = f"{row['month']} 排休 {len(dates)} 天"
        if review_note: extra += f"\n審核意見：{review_note}"
        _notify_review_result(row['staff_id'], '排休申請', action, extra)
    return jsonify(sched_req_row(row)) if row else ('', 404)


@app.route('/api/schedule/admin/requests/<int:rid>', methods=['DELETE'])
@require_module('sched')
def api_sched_admin_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM schedule_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


@app.route('/api/schedule/admin/calendar/<month>', methods=['GET'])
@require_module('sched')
def api_sched_admin_calendar(month):
    with get_db() as conn:
        cfg   = get_schedule_config(conn, month)
        staff = conn.execute(
            "SELECT id,name,role FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        reqs  = conn.execute("""
            SELECT sr.staff_id, sr.dates, sr.status, ps.name
            FROM schedule_requests sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.month=%s AND sr.status IN ('approved','pending','modified_pending')
        """, (month,)).fetchall()

    year_int, month_int = int(month[:4]), int(month[5:])
    import calendar as _cal
    days_in_month = _cal.monthrange(year_int, month_int)[1]

    staff_off = {}
    for r in reqs:
        dates_val = r['dates']
        if isinstance(dates_val, str):
            try: dates_val = _json.loads(dates_val)
            except: dates_val = []
        for d in (dates_val or []):
            if r['staff_id'] not in staff_off:
                staff_off[r['staff_id']] = {}
            staff_off[r['staff_id']][d] = r['status']

    days = []
    for day in range(1, days_in_month + 1):
        date_str = f"{month}-{day:02d}"
        dt       = _dt(year_int, month_int, day)
        off_list = []
        for s in staff:
            st = staff_off.get(s['id'], {}).get(date_str)
            if st:
                off_list.append({'staff_id': s['id'], 'name': s['name'],
                                  'role': s['role'], 'status': st})
        days.append({
            'date':          date_str,
            'day':           day,
            'weekday':       WEEKDAY_ZH[dt.weekday()],
            'is_weekend':    dt.weekday() >= 5,
            'off_count':     len(off_list),
            'off_list':      off_list,
            'working_count': len(staff) - len(off_list),
            'over_limit':    len(off_list) > cfg['max_off_per_day'],
        })
    return jsonify({'month': month, 'config': cfg, 'staff_count': len(staff), 'days': days})


@app.route('/api/schedule/admin/summary/<month>', methods=['GET'])
@require_module('sched')
def api_sched_admin_summary(month):
    with get_db() as conn:
        cfg   = get_schedule_config(conn, month)
        staff = conn.execute(
            "SELECT id,name,role FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        reqs  = conn.execute(
            "SELECT sr.* FROM schedule_requests sr WHERE sr.month=%s", (month,)
        ).fetchall()
    req_map = {r['staff_id']: sched_req_row(r) for r in reqs}
    result  = []
    for s in staff:
        req = req_map.get(s['id'])
        result.append({
            'staff_id':   s['id'],
            'name':       s['name'],
            'role':       s['role'],
            'status':     req['status']  if req else 'not_submitted',
            'days_off':   len(req['dates']) if req else 0,
            'quota':      cfg['vacation_quota'],
            'dates':      req['dates']   if req else [],
            'request_id': req['id']      if req else None,
        })
    return jsonify({'config': cfg, 'staff': result})

# ── Shift Types CRUD ──────────────────────────────────────────────

@app.route('/api/shifts/types', methods=['GET'])
@require_module('sched')
def api_shift_types_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM shift_types ORDER BY sort_order, id").fetchall()
    return jsonify([shift_type_row(r) for r in rows])

@app.route('/api/shifts/types/public', methods=['GET'])
def api_shift_types_public():
    """Public endpoint for employee page."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM shift_types WHERE active=TRUE ORDER BY sort_order, id"
        ).fetchall()
    return jsonify([shift_type_row(r) for r in rows])

@app.route('/api/shifts/types', methods=['POST'])
@require_module('sched')
def api_shift_type_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO shift_types (name, start_time, end_time, color, departments, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b['start_time'], b['end_time'],
              b.get('color', '#4a7bda'), b.get('departments', ''),
              int(b.get('sort_order', 0)))).fetchone()
    return jsonify(shift_type_row(row)), 201

@app.route('/api/shifts/types/<int:sid>', methods=['PUT'])
@require_module('sched')
def api_shift_type_update(sid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE shift_types
            SET name=%s, start_time=%s, end_time=%s, color=%s,
                departments=%s, sort_order=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b['start_time'], b['end_time'],
              b.get('color', '#4a7bda'), b.get('departments', ''),
              int(b.get('sort_order', 0)), bool(b.get('active', True)),
              sid)).fetchone()
    return jsonify(shift_type_row(row)) if row else ('', 404)

@app.route('/api/shifts/types/<int:sid>', methods=['DELETE'])
@require_module('sched')
def api_shift_type_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM shift_types WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})

# ── Shift Assignments ─────────────────────────────────────────────

@app.route('/api/shifts/assignments', methods=['GET'])
@require_module('sched')
def api_shift_assignments_list():
    month = request.args.get('month', '')
    conds, params = ['TRUE'], []
    if month:
        conds.append("to_char(sa.shift_date,'YYYY-MM')=%s"); params.append(month)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT sa.*,
                   ps.name as staff_name, ps.role as staff_role,
                   st.name as shift_name, st.start_time, st.end_time,
                   st.color, st.departments
            FROM shift_assignments sa
            JOIN punch_staff ps ON ps.id=sa.staff_id
            JOIN shift_types  st ON st.id=sa.shift_type_id
            WHERE {' AND '.join(conds)}
            ORDER BY sa.shift_date, ps.name
        """, params).fetchall()
    result = []
    for r in rows:
        d = shift_assign_row(r)
        d['staff_name'] = r['staff_name']
        d['staff_role'] = r['staff_role']
        d['shift_name'] = r['shift_name']
        d['start_time'] = str(r['start_time'])[:5]
        d['end_time']   = str(r['end_time'])[:5]
        d['color']      = r['color']
        result.append(d)
    return jsonify(result)


@app.route('/api/shifts/assignments', methods=['POST'])
@require_module('sched')
def api_shift_assignment_create():
    b             = request.get_json(force=True)
    staff_ids     = b.get('staff_ids', [])
    shift_type_id = b.get('shift_type_id')
    dates         = b.get('dates', [])
    note          = b.get('note', '').strip()
    force         = bool(b.get('force', False))

    if not staff_ids or not shift_type_id or not dates:
        return jsonify({'error': '請選擇員工、班別及日期'}), 400

    created = 0
    blocked = []

    with get_db() as conn:
        # Build leave lookup for all involved staff
        leave_lookup = {}
        if not force:
            for sid in staff_ids:
                months = list({d[:7] for d in dates})
                for month in months:
                    row = conn.execute("""
                        SELECT dates FROM schedule_requests
                        WHERE staff_id=%s AND month=%s AND status='approved'
                    """, (sid, month)).fetchone()
                    if row:
                        approved_dates = row['dates'] or []
                        if isinstance(approved_dates, str):
                            try: approved_dates = _json.loads(approved_dates)
                            except: approved_dates = []
                        if sid not in leave_lookup:
                            leave_lookup[sid] = set()
                        leave_lookup[sid].update(approved_dates)

        staff_names = {}
        for r in conn.execute(
            "SELECT id,name FROM punch_staff WHERE id = ANY(%s::int[])", (staff_ids,)
        ).fetchall():
            staff_names[r['id']] = r['name']

        for sid in staff_ids:
            leave_dates = leave_lookup.get(sid, set())
            for date_str in dates:
                if date_str in leave_dates and not force:
                    blocked.append({'staff_name': staff_names.get(sid, str(sid)), 'date': date_str})
                    continue
                conn.execute("""
                    INSERT INTO shift_assignments (staff_id, shift_type_id, shift_date, note)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (staff_id, shift_date) DO UPDATE
                      SET shift_type_id=%s, note=%s, created_at=NOW()
                """, (sid, shift_type_id, date_str, note, shift_type_id, note))
                created += 1

    if blocked and created == 0:
        return jsonify({
            'error': '以下日期員工已有核准的排休，無法指派班別：' +
                     '、'.join([f'{x["staff_name"]} {x["date"]}' for x in blocked]),
            'blocked': blocked
        }), 422

    result = {'created': created}
    if blocked:
        result['warning'] = f'已指派 {created} 筆，跳過 {len(blocked)} 筆（員工當日有核准排休）'
        result['blocked'] = blocked
    return jsonify(result), 201


@app.route('/api/shifts/assignments/batch-delete', methods=['POST'])
@require_module('sched')
def api_shift_assignment_batch_delete():
    b         = request.get_json(force=True)
    staff_ids = b.get('staff_ids', [])
    dates     = b.get('dates', [])
    if not staff_ids or not dates:
        return jsonify({'error': '請選擇員工及日期'}), 400
    deleted = 0
    with get_db() as conn:
        for sid in staff_ids:
            for date_str in dates:
                r = conn.execute(
                    "DELETE FROM shift_assignments WHERE staff_id=%s AND shift_date=%s RETURNING id",
                    (sid, date_str)
                ).fetchone()
                if r: deleted += 1
    return jsonify({'deleted': deleted})


@app.route('/api/shifts/import', methods=['POST'])
@require_module('sched')
def api_shift_import():
    """
    匯入班表 CSV。
    表頭（第一列）：姓名,日期,班別,備註  或  代碼,日期,班別,備註
    日期格式：YYYY-MM-DD
    force=1 query param 可強制覆蓋排休衝突。
    """
    import csv, io as _io
    force = request.args.get('force', '0') == '1'

    # 支援 multipart file 或 raw text/csv body
    if 'file' in request.files:
        raw = request.files['file'].read().decode('utf-8-sig')
    else:
        raw = request.get_data(as_text=True)

    if not raw.strip():
        return jsonify({'error': '檔案內容為空'}), 400

    reader = csv.DictReader(_io.StringIO(raw))
    # 正規化欄位名稱（去空白）
    if reader.fieldnames is None:
        return jsonify({'error': '無法解析 CSV 欄位'}), 400
    reader.fieldnames = [f.strip() for f in reader.fieldnames]

    # 確認必要欄位存在
    has_name = '姓名' in reader.fieldnames
    has_code = '代碼' in reader.fieldnames
    if not (has_name or has_code):
        return jsonify({'error': 'CSV 缺少「姓名」或「代碼」欄位'}), 400
    if '日期' not in reader.fieldnames:
        return jsonify({'error': 'CSV 缺少「日期」欄位'}), 400
    if '班別' not in reader.fieldnames:
        return jsonify({'error': 'CSV 缺少「班別」欄位'}), 400

    rows = list(reader)
    if not rows:
        return jsonify({'error': 'CSV 無資料列'}), 400

    with get_db() as conn:
        # 預先建立索引，避免逐列查詢
        staff_by_name = {r['name']: r['id'] for r in conn.execute(
            "SELECT id, name FROM punch_staff WHERE active=TRUE"
        ).fetchall()}
        staff_by_code = {r['employee_code']: r['id'] for r in conn.execute(
            "SELECT id, employee_code FROM punch_staff WHERE active=TRUE AND employee_code IS NOT NULL AND employee_code!=''",
        ).fetchall()}
        shift_by_name = {r['name']: r['id'] for r in conn.execute(
            "SELECT id, name FROM shift_types WHERE active=TRUE"
        ).fetchall()}

        # 預先讀取所有涉及員工的核准排休（僅在非強制時）
        leave_lookup = {}   # { staff_id: set(date_str) }
        if not force:
            leave_rows = conn.execute("""
                SELECT staff_id, dates FROM schedule_requests
                WHERE status='approved'
            """).fetchall()
            for lr in leave_rows:
                sid = lr['staff_id']
                dates_val = lr['dates']
                if isinstance(dates_val, str):
                    try: dates_val = _json.loads(dates_val)
                    except: dates_val = []
                if sid not in leave_lookup:
                    leave_lookup[sid] = set()
                leave_lookup[sid].update(dates_val or [])

        created = 0
        skipped = []   # 衝突（排休）
        errors  = []   # 找不到員工/班別、日期格式錯誤

        for i, row in enumerate(rows, start=2):   # 從第2列計算（第1列是表頭）
            name_val = row.get('姓名', '').strip()
            code_val = row.get('代碼', '').strip()
            date_str = row.get('日期', '').strip()
            shift_name = row.get('班別', '').strip()
            note = row.get('備註', '').strip()

            # 找員工 ID
            staff_id = None
            if code_val:
                staff_id = staff_by_code.get(code_val)
            if staff_id is None and name_val:
                staff_id = staff_by_name.get(name_val)
            if staff_id is None:
                errors.append({'row': i, 'reason': f'找不到員工：{code_val or name_val}'})
                continue

            # 找班別 ID
            shift_id = shift_by_name.get(shift_name)
            if shift_id is None:
                errors.append({'row': i, 'reason': f'找不到班別：{shift_name}'})
                continue

            # 驗證日期
            try:
                from datetime import date as _date
                _date.fromisoformat(date_str)
            except ValueError:
                errors.append({'row': i, 'reason': f'日期格式錯誤：{date_str}（應為 YYYY-MM-DD）'})
                continue

            # 排休衝突檢查
            if not force and date_str in leave_lookup.get(staff_id, set()):
                display = name_val or code_val
                skipped.append({'row': i, 'reason': f'{display} 於 {date_str} 有核准排休'})
                continue

            # 寫入（衝突則覆蓋）
            conn.execute("""
                INSERT INTO shift_assignments (staff_id, shift_type_id, shift_date, note)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (staff_id, shift_date) DO UPDATE
                  SET shift_type_id=%s, note=%s, created_at=NOW()
            """, (staff_id, shift_id, date_str, note, shift_id, note))
            created += 1

    result = {'created': created, 'skipped': skipped, 'errors': errors}
    if errors or skipped:
        result['message'] = f'匯入完成：{created} 筆成功，{len(skipped)} 筆排休衝突跳過，{len(errors)} 筆錯誤'
    else:
        result['message'] = f'匯入完成：共 {created} 筆排班'
    return jsonify(result), 201


@app.route('/api/shifts/my-schedule', methods=['GET'])
def api_my_shift_schedule():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    month = request.args.get('month', '')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sa.shift_date, sa.note,
                   st.name as shift_name, st.start_time, st.end_time, st.color
            FROM shift_assignments sa
            JOIN shift_types st ON st.id=sa.shift_type_id
            WHERE sa.staff_id=%s
              AND to_char(sa.shift_date,'YYYY-MM')=%s
            ORDER BY sa.shift_date
        """, (sid, month)).fetchall()
    result = {}
    for r in rows:
        ds = r['shift_date'].isoformat()
        result[ds] = {
            'shift_name': r['shift_name'],
            'start_time': str(r['start_time'])[:5],
            'end_time':   str(r['end_time'])[:5],
            'color':      r['color'],
            'note':       r['note'],
        }
    return jsonify({'month': month, 'shifts': result})

# ── Overtime Requests ─────────────────────────────────────────────

@app.route('/api/overtime/my-requests', methods=['GET'])
def api_ot_my_list():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM overtime_requests WHERE staff_id=%s ORDER BY request_date DESC LIMIT 30",
            (sid,)
        ).fetchall()
    return jsonify([ot_req_row(r) for r in rows])


@app.route('/api/overtime/my-requests', methods=['POST'])
def api_ot_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b            = request.get_json(force=True)
    request_date = b.get('request_date', '').strip()
    start_time   = b.get('start_time', '').strip()
    end_time     = b.get('end_time', '').strip()
    reason       = b.get('reason', '').strip()
    day_type     = b.get('day_type', 'weekday').strip()
    if day_type not in ('weekday', 'rest_day', 'holiday', 'special'):
        day_type = 'weekday'
    if not request_date or not start_time or not end_time:
        return jsonify({'error': '請填寫加班日期及時間'}), 400
    if not reason:
        return jsonify({'error': '請填寫加班原因'}), 400
    from datetime import datetime as _dtot, timedelta as _tdot
    try:
        s = _dtot.strptime(start_time, '%H:%M')
        e = _dtot.strptime(end_time,   '%H:%M')
        if e <= s: e += _tdot(days=1)
        ot_hours = round((e - s).total_seconds() / 3600, 2)
    except ValueError:
        return jsonify({'error': '時間格式錯誤'}), 400
    if ot_hours <= 0 or ot_hours > 12:
        return jsonify({'error': '加班時數不合理（0~12小時）'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO overtime_requests
              (staff_id, request_date, start_time, end_time, ot_hours, reason, day_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, request_date, start_time, end_time, ot_hours, reason, day_type)).fetchone()
    return jsonify(ot_req_row(row)), 201


@app.route('/api/overtime/requests', methods=['GET'])
@login_required
def api_ot_admin_list():
    status = request.args.get('status', '')
    month  = request.args.get('month', '')
    conds, params = ['TRUE'], []
    if status: conds.append('r.status=%s');                          params.append(status)
    if month:  conds.append("to_char(r.request_date,'YYYY-MM')=%s"); params.append(month)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT r.*, ps.name as staff_name, ps.role as staff_role
            FROM overtime_requests r
            JOIN punch_staff ps ON ps.id=r.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY r.request_date DESC, r.created_at DESC
        """, params).fetchall()
    return jsonify([
        ot_req_row(r) | {'staff_name': r['staff_name'], 'staff_role': r['staff_role']}
        for r in rows
    ])


def _calc_ot_pay(staff_row, ot_hours, day_type='weekday'):
    salary_type = staff_row.get('salary_type', 'monthly') or 'monthly'
    base_salary = float(staff_row.get('base_salary')  or 0)
    hourly_rate = float(staff_row.get('hourly_rate')  or 0)
    daily_hours = float(staff_row.get('daily_hours')  or 8)
    ot_rate1    = float(staff_row.get('ot_rate1')     or 1.33)
    ot_rate2    = float(staff_row.get('ot_rate2')     or 1.67)

    if salary_type == 'hourly':
        base_hourly = hourly_rate
    else:
        base_hourly = base_salary / 30 / daily_hours if (base_salary and daily_hours) else 0

    if base_hourly <= 0:
        return 0.0, base_hourly

    h = float(ot_hours)
    if day_type in ('holiday', 'special'):
        pay = round(base_hourly * h * 2.0, 0)
    elif day_type == 'rest_day':
        billed = max(h, 4.0)
        h1  = min(billed, 2.0); h2  = max(0.0, billed - 2.0)
        pay = round(base_hourly * (h1 * ot_rate1 + h2 * ot_rate2), 0)
    else:
        h1  = min(h, 2.0); h2  = max(0.0, h - 2.0)
        pay = round(base_hourly * (h1 * ot_rate1 + h2 * ot_rate2), 0)

    return pay, base_hourly


@app.route('/api/overtime/requests/<int:rid>', methods=['PUT'])
@login_required
def api_ot_review(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'invalid action'}), 400
    new_status   = 'approved' if action == 'approve' else 'rejected'
    ot_pay_final = 0.0

    with get_db() as conn:
        req = conn.execute(
            "SELECT * FROM overtime_requests WHERE id=%s", (rid,)
        ).fetchone()
        if not req: return ('', 404)

        if action == 'approve':
            staff = conn.execute("""
                SELECT base_salary, hourly_rate, daily_hours,
                       ot_rate1, ot_rate2, salary_type
                FROM punch_staff WHERE id=%s
            """, (req['staff_id'],)).fetchone()
            if staff:
                dtype        = req.get('day_type', 'weekday') or 'weekday'
                ot_pay_final, _ = _calc_ot_pay(staff, req['ot_hours'] or 0, dtype)

        row = conn.execute("""
            UPDATE overtime_requests
            SET status=%s, reviewed_by=%s, review_note=%s,
                ot_pay=%s, reviewed_at=NOW()
            WHERE id=%s RETURNING *
        """, (new_status, reviewed_by, review_note, ot_pay_final, rid)).fetchone()

        sn = conn.execute(
            "SELECT name FROM punch_staff WHERE id=%s", (req['staff_id'],)
        ).fetchone()

    result = ot_req_row(row)
    result['staff_name'] = sn['name'] if sn else ''
    # LINE notification
    extra = f"{row['request_date']} {row['start_time']}～{row['end_time']} {float(row['ot_hours'])}小時"
    if action == 'approve' and float(row.get('ot_pay') or 0) > 0:
        extra += f"\n加班費：${float(row['ot_pay']):,.0f}"
    if review_note: extra += f"\n審核意見：{review_note}"
    _notify_review_result(req['staff_id'], '加班申請', action, extra)
    return jsonify(result)


@app.route('/api/overtime/requests/<int:rid>', methods=['DELETE'])
@login_required
def api_ot_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM overtime_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


@app.route('/api/overtime/monthly-summary', methods=['GET'])
@login_required
def api_ot_monthly_summary():
    month = request.args.get('month', '') or _dt.now().strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                ps.id   AS staff_id,
                ps.name AS staff_name,
                ps.role AS staff_role,
                COUNT(*)                                                      AS request_count,
                SUM(r.ot_hours)                                               AS total_hours,
                SUM(CASE WHEN r.status='approved' THEN r.ot_hours ELSE 0 END) AS approved_hours,
                SUM(CASE WHEN r.status='pending'  THEN r.ot_hours ELSE 0 END) AS pending_hours,
                SUM(CASE WHEN r.status='rejected' THEN r.ot_hours ELSE 0 END) AS rejected_hours,
                COUNT(CASE WHEN r.status='approved' THEN 1 END)               AS approved_count,
                COUNT(CASE WHEN r.status='pending'  THEN 1 END)               AS pending_count,
                COUNT(CASE WHEN r.status='rejected' THEN 1 END)               AS rejected_count
            FROM overtime_requests r
            JOIN punch_staff ps ON ps.id = r.staff_id
            WHERE to_char(r.request_date, 'YYYY-MM') = %s
            GROUP BY ps.id, ps.name, ps.role
            ORDER BY total_hours DESC
        """, (month,)).fetchall()
    return jsonify([{
        'staff_id':       r['staff_id'],
        'staff_name':     r['staff_name'],
        'staff_role':     r['staff_role'] or '',
        'request_count':  r['request_count'],
        'total_hours':    float(r['total_hours']    or 0),
        'approved_hours': float(r['approved_hours'] or 0),
        'pending_hours':  float(r['pending_hours']  or 0),
        'rejected_hours': float(r['rejected_hours'] or 0),
        'approved_count': r['approved_count'],
        'pending_count':  r['pending_count'],
        'rejected_count': r['rejected_count'],
    } for r in rows])


@app.route('/api/overtime/calc-preview', methods=['POST'])
@login_required
def api_ot_calc_preview():
    b        = request.get_json(force=True)
    staff_id = b.get('staff_id')
    ot_hours = float(b.get('ot_hours') or 0)
    if not staff_id: return jsonify({'error': 'staff_id required'}), 400
    with get_db() as conn:
        staff = conn.execute("""
            SELECT name, base_salary, hourly_rate, daily_hours,
                   ot_rate1, ot_rate2, salary_type
            FROM punch_staff WHERE id=%s
        """, (staff_id,)).fetchone()
    if not staff: return ('', 404)
    day_type     = b.get('day_type', 'weekday') or 'weekday'
    ot_pay, base_hourly = _calc_ot_pay(staff, ot_hours, day_type)

    if day_type == 'rest_day':
        billed = max(ot_hours, 4.0); h1 = min(billed, 2.0); h2 = max(0.0, billed - 2.0)
    elif day_type in ('holiday', 'special'):
        h1 = ot_hours; h2 = 0.0
    else:
        h1 = min(ot_hours, 2.0); h2 = max(0.0, ot_hours - 2.0)

    return jsonify({
        'staff_name':  staff['name'],
        'salary_type': staff.get('salary_type', 'monthly'),
        'base_salary': float(staff.get('base_salary') or 0),
        'hourly_rate': float(staff.get('hourly_rate') or 0),
        'base_hourly': round(base_hourly, 2),
        'ot_hours':    ot_hours,
        'day_type':    day_type,
        'h1':          h1,
        'h2':          h2,
        'ot_rate1':    float(staff.get('ot_rate1') or 1.33),
        'ot_rate2':    float(staff.get('ot_rate2') or 1.67),
        'ot_pay':      ot_pay,
    })


# ═══════════════════════════════════════════════════════════════════
# Leave Management (請假管理)
# 2026 勞基法：
#   特休：到職1年10天、2年15天、3~5年每年+1、滿5年20天(上限)
#   病假：每年30天(半薪)，超過住院病假 365 天內 30 天(全薪)
#   事假：每年14天(無薪)
#   生理假：每月1天(含病假計算，前3天半薪)
#   婚假：8天全薪
#   喪假：父母/配偶/子女8天；祖父母/孫子女/兄弟姐妹6天；曾祖父母3天
#   產假：8週全薪；陪產假：7天全薪
#   公假：全薪
# ═══════════════════════════════════════════════════════════════════

# ── Leave Tables ─────────────────────────────────────────────────────────────

def init_leave_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS leave_types (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            code        TEXT NOT NULL UNIQUE,
            pay_rate    NUMERIC(4,2) DEFAULT 1.0,
            max_days    NUMERIC(5,1),
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#4a7bda',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS leave_requests (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            leave_type_id   INT REFERENCES leave_types(id),
            start_date      DATE NOT NULL,
            end_date        DATE NOT NULL,
            start_half      BOOLEAN DEFAULT FALSE,
            end_half        BOOLEAN DEFAULT FALSE,
            total_days      NUMERIC(5,1) NOT NULL DEFAULT 0,
            reason          TEXT DEFAULT '',
            status          TEXT DEFAULT 'pending',
            reviewed_by     TEXT DEFAULT '',
            review_note     TEXT DEFAULT '',
            reviewed_at     TIMESTAMPTZ,
            substitute_name TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS leave_balances (
            id          SERIAL PRIMARY KEY,
            staff_id    INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            leave_type_id INT REFERENCES leave_types(id),
            year        INT NOT NULL,
            total_days  NUMERIC(5,1) DEFAULT 0,
            used_days   NUMERIC(5,1) DEFAULT 0,
            note        TEXT DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(staff_id, leave_type_id, year)
        )""",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[leave_init] {str(e)[:80]}")

    # Seed default leave types
    defaults = [
        ('特休假',   'annual',      1.0,  30,  '#2e9e6b', 1),
        ('病假',     'sick',        0.5,  30,  '#e07b2a', 2),
        ('住院病假', 'hospitalize', 1.0,  30,  '#d64242', 3),
        ('事假',     'personal',    0.0,  14,  '#8892a4', 4),
        ('生理假',   'menstrual',   0.5,  12,  '#c45cb8', 5),
        ('婚假',     'marriage',    1.0,   8,  '#c8a96e', 6),
        ('喪假',     'funeral',     1.0,   8,  '#4a7bda', 7),
        ('產假',     'maternity',   1.0,  56,  '#e05c8a', 8),
        ('陪產假',   'paternity',   1.0,   7,  '#5cb8c4', 9),
        ('公假',     'official',    1.0, None, '#243d6e', 10),
        ('補休',     'compensatory',1.0, None, '#8b5cf6', 11),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM leave_types").fetchone()['c']
            if cnt == 0:
                for name, code, pay, maxd, color, sort in defaults:
                    conn.execute(
                        "INSERT INTO leave_types (name,code,pay_rate,max_days,color,sort_order) VALUES (%s,%s,%s,%s,%s,%s)",
                        (name, code, pay, maxd, color, sort)
                    )
    except Exception as e:
        print(f"[leave_seed] {e}")

init_leave_db()

def leave_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('max_days') is not None: d['max_days'] = float(d['max_days'])
    if d.get('pay_rate') is not None: d['pay_rate'] = float(d['pay_rate'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def leave_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_date'): d['start_date'] = d['start_date'].isoformat()
    if d.get('end_date'):   d['end_date']   = d['end_date'].isoformat()
    if d.get('total_days'): d['total_days'] = float(d['total_days'])
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    return d

def leave_balance_row(row):
    if not row: return None
    d = dict(row)
    if d.get('total_days') is not None: d['total_days'] = float(d['total_days'])
    if d.get('used_days')  is not None: d['used_days']  = float(d['used_days'])
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d

def _calc_annual_leave_days(hire_date_str, ref_date_str=None):
    """
    勞基法第38條特休天數計算（2017年修正版，現行有效）

    到職滿6個月：3天
    到職滿1年：7天
    到職滿2年：10天
    到職滿3年：14天
    到職滿4年：14天（同第3年）
    到職滿5年：15天
    到職滿6～9年：15天（同第5年）
    到職滿10年起：每年+1天，上限30天

    回傳當期應給特休天數（整數）
    """
    if not hire_date_str:
        return 0
    from datetime import date as _date
    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return 0

    ref = _date.today()
    if ref_date_str:
        try:
            ref = _date.fromisoformat(str(ref_date_str))
        except Exception:
            pass

    # 計算到職滿幾個月（以完整月份計）
    months = (ref.year - hire.year) * 12 + (ref.month - hire.month)
    # 若當月日期未到到職日，扣一個月
    if ref.day < hire.day:
        months -= 1
    if months < 0:
        months = 0

    # 正確換算年數（以整月為準）
    years_complete = months // 12
    months_extra   = months % 12

    # 勞基法第38條逐段對應
    if months < 6:
        return 0
    elif months < 12:
        # 滿6個月未滿1年：3天
        return 3
    elif years_complete < 2:
        # 滿1年未滿2年：7天
        return 7
    elif years_complete < 3:
        # 滿2年未滿3年：10天
        return 10
    elif years_complete < 5:
        # 滿3年未滿5年：14天
        return 14
    elif years_complete < 10:
        # 滿5年未滿10年：15天
        return 15
    else:
        # 滿10年：16天，之後每年+1，上限30天
        # years_complete=10 → extra=1 → 15+1=16 ✓
        extra = years_complete - 9
        return min(15 + extra, 30)


def _calc_annual_leave_schedule(hire_date_str):
    """
    回傳員工特休天數完整排程表，供前端顯示用。
    每一列：{ label, days, date_reached, is_past, is_current }
    """
    if not hire_date_str:
        return []
    from datetime import date as _date
    import calendar as _cal

    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return []

    today = _date.today()

    milestones = [
        (6,   3,  '滿6個月'),
        (12,  7,  '滿1年'),
        (24, 10,  '滿2年'),
        (36, 14,  '滿3年'),
        (60, 15,  '滿5年'),
        (120,16,  '滿10年'),
        (132,17,  '滿11年'),
        (144,18,  '滿12年'),
        (156,19,  '滿13年'),
        (168,20,  '滿14年'),
        (180,21,  '滿15年'),
        (192,22,  '滿16年'),
        (204,23,  '滿17年'),
        (216,24,  '滿18年'),
        (228,25,  '滿19年'),
        (240,30,  '滿20年（上限30天）'),
    ]

    result      = []
    current_days = _calc_annual_leave_days(hire_date_str)

    for months_needed, days, label in milestones:
        total_m = hire.month + months_needed
        y = hire.year + (total_m - 1) // 12
        m = (total_m - 1) % 12 + 1
        max_day = _cal.monthrange(y, m)[1]
        try:
            reached = _date(y, m, min(hire.day, max_day))
        except Exception:
            continue

        result.append({
            'label':        label,
            'days':         days,
            'date_reached': reached.isoformat(),
            'is_past':      reached <= today,
            'is_current':   (days == current_days and reached <= today),
        })

    return result

def _calc_leave_days(start_date_str, end_date_str, start_half=False, end_half=False):
    """計算請假天數（含半天選項），排除週日"""
    from datetime import date as _date, timedelta as _tdd
    try:
        s = _date.fromisoformat(start_date_str)
        e = _date.fromisoformat(end_date_str)
    except Exception:
        return 0.0
    if e < s: return 0.0
    days = 0.0
    cur  = s
    while cur <= e:
        if cur.weekday() != 6:  # exclude Sunday (勞基法最低標準)
            if cur == s and start_half: days += 0.5
            elif cur == e and end_half: days += 0.5
            else: days += 1.0
        cur += _tdd(days=1)
    return days

# ── Leave Type CRUD ──────────────────────────────────────────────

@app.route('/api/leave/types', methods=['GET'])
@require_module('leave')
def api_leave_types_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types ORDER BY sort_order, id").fetchall()
    return jsonify([leave_type_row(r) for r in rows])

@app.route('/api/leave/types/public', methods=['GET'])
def api_leave_types_public():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types WHERE active=TRUE ORDER BY sort_order, id").fetchall()
    return jsonify([leave_type_row(r) for r in rows])

@app.route('/api/leave/types', methods=['POST'])
@require_module('leave')
def api_leave_type_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO leave_types (name,code,pay_rate,max_days,description,color,sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b['code'], float(b.get('pay_rate',1.0)),
              b.get('max_days') or None, b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)))).fetchone()
    return jsonify(leave_type_row(row)), 201

@app.route('/api/leave/types/<int:tid>', methods=['PUT'])
@require_module('leave')
def api_leave_type_update(tid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE leave_types SET name=%s,code=%s,pay_rate=%s,max_days=%s,
              description=%s,color=%s,sort_order=%s,active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b['code'], float(b.get('pay_rate',1.0)),
              b.get('max_days') or None, b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)),
              bool(b.get('active',True)), tid)).fetchone()
    return jsonify(leave_type_row(row)) if row else ('', 404)

@app.route('/api/leave/types/<int:tid>', methods=['DELETE'])
@require_module('leave')
def api_leave_type_delete(tid):
    with get_db() as conn:
        conn.execute("DELETE FROM leave_types WHERE id=%s", (tid,))
    return jsonify({'deleted': tid})

# ── Leave Requests ────────────────────────────────────────────────

@app.route('/api/leave/requests', methods=['GET'])
@require_module('leave')
def api_leave_requests_list():
    status   = request.args.get('status', '')
    month    = request.args.get('month', '')
    staff_id = request.args.get('staff_id', '')
    conds, params = ['TRUE'], []
    if status:   conds.append('lr.status=%s');                            params.append(status)
    if staff_id: conds.append('lr.staff_id=%s');                          params.append(int(staff_id))
    if month:    conds.append("to_char(lr.start_date,'YYYY-MM')=%s");     params.append(month)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT lr.*, ps.name as staff_name, ps.role as staff_role,
                   lt.name as leave_type_name, lt.code as leave_code,
                   lt.pay_rate, lt.color as leave_color
            FROM leave_requests lr
            JOIN punch_staff ps ON ps.id=lr.staff_id
            JOIN leave_types  lt ON lt.id=lr.leave_type_id
            WHERE {' AND '.join(conds)}
            ORDER BY lr.start_date DESC, lr.created_at DESC LIMIT 300
        """, params).fetchall()
    result = []
    for r in rows:
        d = leave_req_row(r)
        d['staff_name']      = r['staff_name']
        d['staff_role']      = r['staff_role']
        d['leave_type_name'] = r['leave_type_name']
        d['leave_code']      = r['leave_code']
        d['pay_rate']        = float(r['pay_rate'])
        d['leave_color']     = r['leave_color']
        result.append(d)
    return jsonify(result)

@app.route('/api/leave/requests', methods=['POST'])
@require_module('leave')
def api_leave_request_admin_create():
    """管理員直接建立請假記錄"""
    b = request.get_json(force=True)
    sid           = b.get('staff_id')
    leave_type_id = b.get('leave_type_id')
    start_date    = b.get('start_date', '').strip()
    end_date      = b.get('end_date', '').strip()
    start_half    = bool(b.get('start_half', False))
    end_half      = bool(b.get('end_half', False))
    reason        = b.get('reason', '').strip()
    status        = b.get('status', 'approved')

    if not all([sid, leave_type_id, start_date, end_date]):
        return jsonify({'error': '缺少必要欄位'}), 400

    total_days = _calc_leave_days(start_date, end_date, start_half, end_half)
    if total_days <= 0:
        return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, start_half, end_half,
               total_days, reason, status, reviewed_by, reviewed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              CASE WHEN %s='approved' THEN NOW() ELSE NULL END)
            RETURNING *
        """, (sid, leave_type_id, start_date, end_date, start_half, end_half,
              total_days, reason, status, b.get('reviewed_by','管理員'), status)).fetchone()
        if status == 'approved':
            _update_leave_balance(conn, sid, leave_type_id, start_date[:4], total_days)
    return jsonify(leave_req_row(row)), 201

@app.route('/api/leave/requests/<int:rid>', methods=['PUT'])
@require_module('leave')
def api_leave_request_review(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'invalid action'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    with get_db() as conn:
        old = conn.execute("SELECT * FROM leave_requests WHERE id=%s", (rid,)).fetchone()
        if not old: return ('', 404)
        row = conn.execute("""
            UPDATE leave_requests
            SET status=%s, reviewed_by=%s, review_note=%s,
                reviewed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (new_status, reviewed_by, review_note, rid)).fetchone()
        if action == 'approve':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], float(old['total_days']))
    if row:
        extra = f"{str(old['start_date'])} ~ {str(old['end_date'])} 共 {float(old['total_days'])} 天"
        if review_note: extra += f"\n審核意見：{review_note}"
        _notify_review_result(old['staff_id'], '請假申請', action, extra)
    return jsonify(leave_req_row(row)) if row else ('', 404)

@app.route('/api/leave/requests/<int:rid>', methods=['DELETE'])
@require_module('leave')
def api_leave_request_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM leave_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

def _update_leave_balance(conn, staff_id, leave_type_id, year_str, delta_days):
    year = int(year_str)
    conn.execute("""
        INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
        VALUES (%s, %s, %s, 0, %s)
        ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
          SET used_days = leave_balances.used_days + EXCLUDED.used_days,
              updated_at = NOW()
    """, (staff_id, leave_type_id, year, delta_days))

# ── Employee: submit leave request ────────────────────────────────

@app.route('/api/leave/my-requests', methods=['GET'])
def api_leave_my_list():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT lr.*, lt.name as leave_type_name, lt.code as leave_code,
                   lt.color as leave_color, lt.pay_rate
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s
            ORDER BY lr.start_date DESC LIMIT 30
        """, (sid,)).fetchall()
    result = []
    for r in rows:
        d = leave_req_row(r)
        d['leave_type_name'] = r['leave_type_name']
        d['leave_code']      = r['leave_code']
        d['leave_color']     = r['leave_color']
        d['pay_rate']        = float(r['pay_rate'])
        result.append(d)
    return jsonify(result)

@app.route('/api/leave/my-requests', methods=['POST'])
def api_leave_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b             = request.get_json(force=True)
    leave_type_id = b.get('leave_type_id')
    start_date    = b.get('start_date', '').strip()
    end_date      = b.get('end_date',   '').strip()
    start_half    = bool(b.get('start_half', False))
    end_half      = bool(b.get('end_half',   False))
    reason        = b.get('reason', '').strip()
    substitute    = b.get('substitute_name', '').strip()

    if not all([leave_type_id, start_date, end_date]):
        return jsonify({'error': '缺少必要欄位'}), 400

    total_days = _calc_leave_days(start_date, end_date, start_half, end_half)
    if total_days <= 0:
        return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

    with get_db() as conn:
        # Check balance for types with limits
        lt = conn.execute("SELECT * FROM leave_types WHERE id=%s", (leave_type_id,)).fetchone()
        if lt and lt['max_days'] is not None:
            year = start_date[:4]
            bal  = conn.execute("""
                SELECT COALESCE(used_days,0) as used
                FROM leave_balances
                WHERE staff_id=%s AND leave_type_id=%s AND year=%s
            """, (sid, leave_type_id, year)).fetchone()
            used = float(bal['used']) if bal else 0.0
            if used + total_days > float(lt['max_days']):
                remaining = float(lt['max_days']) - used
                return jsonify({'error': f'{lt["name"]}剩餘 {remaining} 天，無法申請 {total_days} 天'}), 422

        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, start_half, end_half,
               total_days, reason, substitute_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, leave_type_id, start_date, end_date, start_half, end_half,
              total_days, reason, substitute)).fetchone()
    return jsonify(leave_req_row(row)), 201

# ── Leave Balance ─────────────────────────────────────────────────

@app.route('/api/leave/balances', methods=['GET'])
def api_leave_balances():
    """管理員和員工都可以查詢，員工只能查自己的"""
    year     = request.args.get('year', '')
    staff_id = request.args.get('staff_id', '')

    # 員工端：只能查自己
    if not session.get('logged_in'):
        sid = session.get('punch_staff_id')
        if not sid:
            return jsonify({'error': 'not logged in'}), 401
        staff_id = str(sid)   # 強制只查自己
    if not year:
        from datetime import date as _d2
        year = str(_d2.today().year)
    conds, params = ["lb.year=%s"], [int(year)]
    if staff_id: conds.append("lb.staff_id=%s"); params.append(int(staff_id))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT lb.*, ps.name as staff_name, lt.name as leave_type_name,
                   lt.code as leave_code, lt.max_days, lt.color as leave_color
            FROM leave_balances lb
            JOIN punch_staff  ps ON ps.id=lb.staff_id
            JOIN leave_types  lt ON lt.id=lb.leave_type_id
            WHERE {' AND '.join(conds)}
            ORDER BY ps.name, lt.sort_order
        """, params).fetchall()
    result = []
    for r in rows:
        d = leave_balance_row(r)
        d['staff_name']      = r['staff_name']
        d['leave_type_name'] = r['leave_type_name']
        d['leave_code']      = r['leave_code']
        d['leave_color']     = r['leave_color']
        d['max_days']        = float(r['max_days']) if r['max_days'] is not None else None
        result.append(d)
    return jsonify(result)

@app.route('/api/leave/balances/init', methods=['POST'])
@require_module('leave')
def api_leave_balance_init():
    """初始化/更新員工特休天數（依勞基法第38條，以到職日精確計算）"""
    b    = request.get_json(force=True)
    year = b.get('year', '')
    if not year:
        from datetime import date as _d3
        year = str(_d3.today().year)

    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT id, name, hire_date FROM punch_staff WHERE active=TRUE"
        ).fetchall()
        lt = conn.execute("SELECT id FROM leave_types WHERE code='annual'").fetchone()
        if not lt: return jsonify({'error': '找不到特休假類型'}), 404
        lt_id   = lt['id']
        updated = 0
        details = []

        for s in staff_list:
            days = _calc_annual_leave_days(s['hire_date'])
            # 未滿6個月的員工也記錄（0天），方便後續追蹤
            conn.execute("""
                INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
                VALUES (%s,%s,%s,%s,0)
                ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
                  SET total_days=EXCLUDED.total_days, updated_at=NOW()
            """, (s['id'], lt_id, int(year), days))
            updated += 1
            details.append({
                'name':      s['name'],
                'hire_date': str(s['hire_date']) if s['hire_date'] else None,
                'days':      days,
            })

    return jsonify({'ok': True, 'updated': updated, 'year': year, 'details': details})


@app.route('/api/leave/annual-schedule/<int:staff_id>', methods=['GET'])
@require_module('leave')
def api_annual_leave_schedule(staff_id):
    """回傳員工特休天數完整排程（各里程碑日期與天數）"""
    with get_db() as conn:
        staff = conn.execute(
            "SELECT name, hire_date FROM punch_staff WHERE id=%s", (staff_id,)
        ).fetchone()
    if not staff:
        return ('', 404)
    schedule = _calc_annual_leave_schedule(staff['hire_date'])
    current  = _calc_annual_leave_days(staff['hire_date'])
    return jsonify({
        'staff_id':      staff_id,
        'name':          staff['name'],
        'hire_date':     str(staff['hire_date']) if staff['hire_date'] else None,
        'current_days':  current,
        'schedule':      schedule,
    })


@app.route('/api/leave/annual-schedule/public', methods=['GET'])
def api_annual_leave_schedule_public():
    """員工查看自己的特休排程"""
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        staff = conn.execute(
            "SELECT name, hire_date FROM punch_staff WHERE id=%s", (sid,)
        ).fetchone()
    if not staff:
        return ('', 404)
    schedule = _calc_annual_leave_schedule(staff['hire_date'])
    current  = _calc_annual_leave_days(staff['hire_date'])
    return jsonify({
        'name':         staff['name'],
        'hire_date':    str(staff['hire_date']) if staff['hire_date'] else None,
        'current_days': current,
        'schedule':     schedule,
    })


@app.route('/api/leave/balances/<int:bid>', methods=['PUT'])
@require_module('leave')
def api_leave_balance_update(bid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE leave_balances SET total_days=%s, used_days=%s, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('total_days',0)), float(b.get('used_days',0)),
              b.get('note',''), bid)).fetchone()
    return jsonify(leave_balance_row(row)) if row else ('', 404)

# ── Leave Summary (for salary integration) ───────────────────────

@app.route('/api/leave/summary/<int:staff_id>/<month>', methods=['GET'])
@require_module('leave')
def api_leave_summary(staff_id, month):
    """取得員工某月請假摘要（供薪資計算用）"""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT lr.*, lt.name as leave_type_name, lt.code, lt.pay_rate
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s
              AND lr.status='approved'
              AND to_char(lr.start_date,'YYYY-MM')=%s
            ORDER BY lr.start_date
        """, (staff_id, month)).fetchall()
    total_leave_days = 0.0
    unpaid_days      = 0.0
    half_pay_days    = 0.0
    items = []
    for r in rows:
        d = float(r['total_days'])
        pay_r = float(r['pay_rate'])
        total_leave_days += d
        if pay_r == 0:   unpaid_days   += d
        elif pay_r < 1:  half_pay_days += d
        items.append({
            'leave_type': r['leave_type_name'],
            'code':       r['code'],
            'days':       d,
            'pay_rate':   pay_r,
            'start_date': r['start_date'].isoformat(),
            'end_date':   r['end_date'].isoformat(),
        })
    return jsonify({
        'staff_id':         staff_id,
        'month':            month,
        'total_leave_days': total_leave_days,
        'unpaid_days':      unpaid_days,
        'half_pay_days':    half_pay_days,
        'items':            items,
    })

# ═══════════════════════════════════════════════════════════════════
# Salary Management (薪資管理)
# 2026 勞基法：
#   勞保費率 10.5%（員工負擔 20%=2.1%，含就業保險）
#   健保費率 5.17%（員工負擔 30%=1.551%）
#   勞退提撥 6%（雇主強制提撥，員工自願另計）
#   最低工資 2026年 NT$28,590（月薪）
# ═══════════════════════════════════════════════════════════════════

def init_salary_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS salary_items (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            item_type   TEXT NOT NULL DEFAULT 'allowance',
            formula     TEXT DEFAULT '',
            amount      NUMERIC(12,2) DEFAULT 0,
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#4a7bda',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_ids JSONB DEFAULT NULL",
        """CREATE TABLE IF NOT EXISTS salary_records (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            month           TEXT NOT NULL,
            base_salary     NUMERIC(12,2) DEFAULT 0,
            insured_salary  NUMERIC(12,2) DEFAULT 0,
            work_days       NUMERIC(5,1)  DEFAULT 0,
            actual_days     NUMERIC(5,1)  DEFAULT 0,
            leave_days      NUMERIC(5,1)  DEFAULT 0,
            unpaid_days     NUMERIC(5,1)  DEFAULT 0,
            ot_pay          NUMERIC(12,2) DEFAULT 0,
            allowance_total NUMERIC(12,2) DEFAULT 0,
            deduction_total NUMERIC(12,2) DEFAULT 0,
            net_pay         NUMERIC(12,2) DEFAULT 0,
            items           JSONB         DEFAULT '[]',
            status          TEXT          DEFAULT 'draft',
            note            TEXT          DEFAULT '',
            confirmed_by    TEXT          DEFAULT '',
            confirmed_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ   DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   DEFAULT NOW(),
            UNIQUE(staff_id, month)
        )""",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[salary_init] {str(e)[:80]}")

    # Seed default salary items
    defaults = [
        ('本薪',        'allowance', 'base_salary+service_years*1000', 0,    '#2e9e6b', 1),
        ('職務加給',    'allowance', '',                                0,    '#0ea5e9', 2),
        ('全勤獎金',    'allowance', '',                                0,    '#c8a96e', 3),
        ('獎金',        'allowance', '',                                0,    '#8b5cf6', 4),
        ('生日禮金',    'allowance', '',                                1000, '#e05c8a', 5),
        ('勞退6%',      'allowance', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 6),
        ('病/事/假',    'deduction', '',                                0,    '#8892a4', 7),
        ('勞保費',      'deduction', 'insured_salary*0.125*0.2',       0,    '#d64242', 8),
        ('健保費',      'deduction', 'insured_salary*0.0517*0.3',      0,    '#e07b2a', 9),
        ('勞退提撥6%',  'deduction', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 10),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM salary_items").fetchone()['c']
            if cnt == 0:
                for name, itype, formula, amount, color, sort in defaults:
                    conn.execute("""
                        INSERT INTO salary_items (name,item_type,formula,amount,color,sort_order)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (name, itype, formula, amount, color, sort))
    except Exception as e:
        print(f"[salary_seed] {e}")

init_salary_db()

def salary_item_row(row):
    if not row: return None
    d = dict(row)
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def salary_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['base_salary','insured_salary','work_days','actual_days','leave_days',
              'unpaid_days','ot_pay','allowance_total','deduction_total','net_pay']:
        if d.get(f) is not None: d[f] = float(d[f])
    if isinstance(d.get('items'), str):
        try: d['items'] = _json.loads(d['items'])
        except: d['items'] = []
    if d.get('confirmed_at'): d['confirmed_at'] = d['confirmed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    if d.get('updated_at'):   d['updated_at']   = d['updated_at'].isoformat()
    return d

def _eval_formula(formula, base_salary, insured_salary, service_years):
    """安全計算薪資公式"""
    if not formula: return 0.0
    try:
        result = eval(formula, {"__builtins__": {}}, {
            'base_salary':    float(base_salary or 0),
            'insured_salary': float(insured_salary or 0),
            'service_years':  float(service_years or 0),
        })
        return round(float(result), 2)
    except Exception:
        return 0.0

def _calc_service_years(hire_date_str):
    if not hire_date_str: return 0.0
    from datetime import date as _d4
    try:
        hire = _d4.fromisoformat(str(hire_date_str))
        return round((_d4.today() - hire).days / 365.25, 2)
    except Exception:
        return 0.0

def _calc_punch_hours(conn, staff_id, month):
    """
    從打卡記錄計算實際工時（時薪制用）
    邏輯：每天找最早 in + 最晚 out，扣除休息時間
    回傳 (total_hours, work_days, details)
    """
    from datetime import datetime as _dth, timezone as _tzh, timedelta as _tdh
    TW = _tzh(_tdh(hours=8))

    rows = conn.execute("""
        SELECT punch_type, punched_at
        FROM punch_records
        WHERE staff_id=%s
          AND to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
        ORDER BY punched_at ASC
    """, (staff_id, month)).fetchall()

    # Group by date
    day_map = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tzh.utc)
        pa_tw  = pa.astimezone(TW)
        ds     = pa_tw.strftime('%Y-%m-%d')
        if ds not in day_map:
            day_map[ds] = []
        day_map[ds].append({'type': r['punch_type'], 'dt': pa_tw})

    total_hours = 0.0
    details     = []
    for ds, punches in sorted(day_map.items()):
        ins   = [p['dt'] for p in punches if p['type'] == 'in']
        outs  = [p['dt'] for p in punches if p['type'] == 'out']
        b_out = [p['dt'] for p in punches if p['type'] == 'break_out']
        b_in  = [p['dt'] for p in punches if p['type'] == 'break_in']

        if not ins or not outs:
            continue

        work_start = min(ins)
        work_end   = max(outs)
        gross_mins = (work_end - work_start).total_seconds() / 60

        # 扣除休息時間
        break_mins = 0.0
        for bo in b_out:
            # 找最近的 break_in
            matched = [bi for bi in b_in if bi > bo]
            if matched:
                break_mins += (min(matched) - bo).total_seconds() / 60

        net_mins = max(0.0, gross_mins - break_mins)
        net_hrs  = round(net_mins / 60, 2)
        total_hours += net_hrs
        details.append({
            'date':        ds,
            'clock_in':    work_start.strftime('%H:%M'),
            'clock_out':   work_end.strftime('%H:%M'),
            'break_mins':  round(break_mins),
            'net_hours':   net_hrs,
        })

    return round(total_hours, 2), len(day_map), details


def _auto_generate_salary(conn, staff, month, work_days=None):
    """
    自動產生員工月薪資料
    ─ 月薪制：底薪 + 薪資項目公式 + 加班費 - 請假扣款
    ─ 時薪制：打卡實際工時 × 時薪 + 加班費 - 請假扣款
    """
    import calendar as _cal2
    y, m = int(month[:4]), int(month[5:])
    total_work_days = work_days
    if total_work_days is None:
        days_in_month = _cal2.monthrange(y, m)[1]
        from datetime import date as _d5
        total_work_days = sum(1 for d in range(1, days_in_month + 1)
                              if _d5(y, m, d).weekday() != 6)

    salary_type    = staff.get('salary_type', 'monthly') or 'monthly'
    base_salary    = float(staff.get('base_salary')    or 0)
    hourly_rate    = float(staff.get('hourly_rate')    or 0)
    insured_salary = float(staff.get('insured_salary') or base_salary)
    daily_hours    = float(staff.get('daily_hours')    or 8)
    service_years  = _calc_service_years(staff.get('hire_date'))

    # ── 時薪制：從打卡記錄計算工時 ──────────────────────────
    actual_work_hours = 0.0
    punch_details     = []
    if salary_type == 'hourly':
        actual_work_hours, punch_work_days, punch_details = _calc_punch_hours(
            conn, staff['id'], month
        )
        # 時薪制的 base_salary 等於 實際工時 × 時薪
        hourly_base_pay = round(actual_work_hours * hourly_rate, 2)
    else:
        # 月薪制：daily_wage 用於請假扣款
        hourly_base_pay = 0.0

    # ── 已核准加班費 ────────────────────────────────────────
    ot_rows = conn.execute("""
        SELECT COALESCE(SUM(ot_pay), 0) as total
        FROM overtime_requests
        WHERE staff_id=%s AND status='approved'
          AND to_char(request_date,'YYYY-MM')=%s
    """, (staff['id'], month)).fetchone()
    ot_pay = float(ot_rows['total']) if ot_rows else 0.0

    # ── 請假資訊 ────────────────────────────────────────────
    leave_rows = conn.execute("""
        SELECT lr.total_days, lt.pay_rate, lt.code, lt.name as leave_name
        FROM leave_requests lr
        JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.staff_id=%s AND lr.status='approved'
          AND to_char(lr.start_date,'YYYY-MM')=%s
    """, (staff['id'], month)).fetchall()
    leave_days    = sum(float(r['total_days']) for r in leave_rows)
    unpaid_days   = sum(float(r['total_days']) for r in leave_rows if float(r['pay_rate']) == 0)
    half_pay_days = sum(float(r['total_days']) for r in leave_rows if 0 < float(r['pay_rate']) < 1)
    actual_days   = total_work_days - leave_days

    # ── 日薪 / 時薪（用於請假扣款） ───────────────────────
    if salary_type == 'hourly':
        daily_wage  = hourly_rate * daily_hours   # 時薪制日薪 = 時薪 × 每日工時
        hourly_wage = hourly_rate
    else:
        daily_wage  = base_salary / 30 if base_salary > 0 else 0
        hourly_wage = daily_wage / daily_hours if daily_hours > 0 else 0

    # ── 組裝薪資項目 ────────────────────────────────────────
    items           = []
    allowance_total = 0.0
    deduction_total = 0.0

    if salary_type == 'hourly':
        # 時薪制：第一筆項目是「本薪（工時計算）」
        items.append({
            'id': 'hourly_base', 'name': '本薪（工時）', 'type': 'allowance',
            'amount': hourly_base_pay, 'formula': '',
            'calc_note': (
                f'{actual_work_hours}h × 時薪${hourly_rate}'
                + (f'（{len(punch_details)}天出勤）' if punch_details else '')
            ),
        })
        allowance_total += hourly_base_pay

        # 時薪制加班費（從打卡計算，若無申請記錄則估算）
        # 先用「加班申請」核准金額；若為 0 則嘗試從工時估算
        if ot_pay == 0 and actual_work_hours > 0:
            # 每天超過 daily_hours 的部分算加班
            for pd in punch_details:
                overtime_h = max(0.0, pd['net_hours'] - daily_hours)
                if overtime_h > 0:
                    h1 = min(overtime_h, 2.0)
                    h2 = max(0.0, overtime_h - 2.0)
                    rate1 = float(staff.get('ot_rate1') or 1.33)
                    rate2 = float(staff.get('ot_rate2') or 1.67)
                    ot_pay += round(hourly_rate * (h1 * rate1 + h2 * rate2), 2)

        # 時薪制的保險費以 insured_salary 為準（若未設定則用月薪換算）
        if insured_salary == 0:
            insured_salary = round(hourly_rate * daily_hours * 30, 0)

        # 時薪制只加入保險類扣除項（若員工有指定則只取指定中的保險項）
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            salary_items_rows = conn.execute(f"""
                SELECT * FROM salary_items
                WHERE active=TRUE AND id IN ({placeholders})
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """, staff_item_ids).fetchall()
        else:
            salary_items_rows = conn.execute("""
                SELECT * FROM salary_items
                WHERE active=TRUE
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """).fetchall()
        for it in salary_items_rows:
            amt = _eval_formula(it['formula'] or '', base_salary or insured_salary,
                                insured_salary, service_years)
            items.append({
                'id': it['id'], 'name': it['name'], 'type': 'deduction',
                'amount': round(amt, 2), 'formula': it['formula'] or '',
                'calc_note': it['formula'] or '',
            })
            deduction_total += amt

    else:
        # 月薪制：跑啟用的薪資項目（若員工有指定則只跑指定項目）
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            items_rows = conn.execute(
                f"SELECT * FROM salary_items WHERE active=TRUE AND id IN ({placeholders}) ORDER BY sort_order, id",
                staff_item_ids
            ).fetchall()
        else:
            items_rows = conn.execute(
                "SELECT * FROM salary_items WHERE active=TRUE ORDER BY sort_order, id"
            ).fetchall()
        for it in items_rows:
            formula = it['formula'] or ''
            amt     = float(it['amount'] or 0)
            if formula:
                amt = _eval_formula(formula, base_salary, insured_salary, service_years)
            items.append({
                'id':        it['id'],
                'name':      it['name'],
                'type':      it['item_type'],
                'amount':    round(amt, 2),
                'formula':   formula,
                'calc_note': formula,
            })
            if it['item_type'] == 'allowance':
                allowance_total += amt
            else:
                deduction_total += amt

    # ── 加班費（申請核准） ──────────────────────────────────
    if ot_pay > 0:
        items.append({
            'id': 'ot', 'name': '加班費（申請）', 'type': 'allowance',
            'amount': round(ot_pay, 2), 'formula': '',
            'calc_note': '核准加班費合計',
        })
        allowance_total += ot_pay

    # ── 請假扣款 ────────────────────────────────────────────
    if unpaid_days > 0 and daily_wage > 0:
        leave_names = '、'.join(set(
            r['leave_name'] for r in leave_rows if float(r['pay_rate']) == 0
        ))
        deduct = round(daily_wage * unpaid_days, 2)
        items.append({
            'id': 'unpaid', 'name': f'無薪假扣款（{leave_names}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{unpaid_days}天 × 日薪${round(daily_wage, 0)}',
        })
        deduction_total += deduct

    if half_pay_days > 0 and daily_wage > 0:
        leave_names = '、'.join(set(
            r['leave_name'] for r in leave_rows if 0 < float(r['pay_rate']) < 1
        ))
        deduct = round(daily_wage * half_pay_days * 0.5, 2)
        items.append({
            'id': 'halfpay', 'name': f'半薪假扣款（{leave_names}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{half_pay_days}天 × 日薪${round(daily_wage, 0)} × 0.5',
        })
        deduction_total += deduct

    net_pay = round(allowance_total - deduction_total, 2)

    return {
        'staff_id':           staff['id'],
        'month':              month,
        'salary_type':        salary_type,
        'base_salary':        base_salary if salary_type == 'monthly' else 0,
        'hourly_rate':        hourly_rate if salary_type == 'hourly' else 0,
        'hourly_base_pay':    hourly_base_pay if salary_type == 'hourly' else 0,
        'actual_work_hours':  actual_work_hours if salary_type == 'hourly' else 0,
        'insured_salary':     insured_salary,
        'work_days':          total_work_days,
        'actual_days':        actual_days,
        'leave_days':         leave_days,
        'unpaid_days':        unpaid_days,
        'ot_pay':             ot_pay,
        'allowance_total':    round(allowance_total, 2),
        'deduction_total':    round(deduction_total, 2),
        'net_pay':            net_pay,
        'items':              items,
        'punch_details':      punch_details,   # 時薪制：每日打卡明細
        'status':             'draft',
    }

# ── Employee: view own payslip ────────────────────────────────────

@app.route('/api/salary/my-payslip', methods=['GET'])
def api_my_payslip():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _dp
        month = _dp.today().strftime('%Y-%m')
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.salary_type, ps.hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.staff_id = %s AND sr.month = %s
        """, (sid, month)).fetchone()
    if not row:
        return jsonify({'error': f'{month} 尚無薪資記錄，請聯絡管理員'}), 404
    d = salary_record_row(row)
    d['staff_name']    = row['staff_name']
    d['staff_role']    = row['staff_role']
    d['employee_code'] = row['employee_code'] or ''
    d['department']    = row['department']    or ''
    d['salary_type']   = row['salary_type']   or 'monthly'
    d['hourly_rate']   = float(row['hourly_rate'] or 0)
    return jsonify(d)

# ── Salary Items CRUD ─────────────────────────────────────────────

@app.route('/api/salary/items', methods=['GET'])
@require_module('salary')
def api_salary_items_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM salary_items ORDER BY sort_order, id").fetchall()
    return jsonify([salary_item_row(r) for r in rows])

@app.route('/api/salary/items', methods=['POST'])
@require_module('salary')
def api_salary_item_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO salary_items (name, item_type, formula, amount, description, color, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b.get('item_type','allowance'), b.get('formula',''),
              float(b.get('amount',0)), b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)))).fetchone()
    return jsonify(salary_item_row(row)), 201

@app.route('/api/salary/items/<int:iid>', methods=['PUT'])
@require_module('salary')
def api_salary_item_update(iid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_items SET name=%s, item_type=%s, formula=%s, amount=%s,
              description=%s, color=%s, sort_order=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b.get('item_type','allowance'), b.get('formula',''),
              float(b.get('amount',0)), b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)),
              bool(b.get('active',True)), iid)).fetchone()
    return jsonify(salary_item_row(row)) if row else ('', 404)

@app.route('/api/salary/items/<int:iid>', methods=['DELETE'])
@require_module('salary')
def api_salary_item_delete(iid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_items WHERE id=%s", (iid,))
    return jsonify({'deleted': iid})

# ── Salary Records ─────────────────────────────────────────────────

@app.route('/api/salary/records', methods=['GET'])
@require_module('salary')
def api_salary_records_list():
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _d6
        month = _d6.today().strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.month=%s
            ORDER BY ps.name
        """, (month,)).fetchall()
    result = []
    for r in rows:
        d = salary_record_row(r)
        d['staff_name']    = r['staff_name']
        d['staff_role']    = r['staff_role']
        d['employee_code'] = r['employee_code'] or ''
        d['department']    = r['department'] or ''
        result.append(d)
    return jsonify(result)

@app.route('/api/salary/records/generate', methods=['POST'])
@require_module('salary')
def api_salary_generate():
    """自動產生或更新該月薪資"""
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    if not month: return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE"
        ).fetchall()
        generated = 0
        for staff in staff_list:
            data = _auto_generate_salary(conn, dict(staff), month)
            items_json = _json.dumps(data['items'], ensure_ascii=False)
            conn.execute("""
                INSERT INTO salary_records
                  (staff_id, month, base_salary, insured_salary, work_days, actual_days,
                   leave_days, unpaid_days, ot_pay, allowance_total, deduction_total,
                   net_pay, items, status, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'draft',NOW())
                ON CONFLICT (staff_id, month) DO UPDATE
                  SET base_salary=%s, insured_salary=%s, work_days=%s, actual_days=%s,
                      leave_days=%s, unpaid_days=%s, ot_pay=%s, allowance_total=%s,
                      deduction_total=%s, net_pay=%s, items=%s::jsonb,
                      status=CASE WHEN salary_records.status='confirmed' THEN 'confirmed' ELSE 'draft' END,
                      updated_at=NOW()
            """, (
                data['staff_id'], month, data['base_salary'], data['insured_salary'],
                data['work_days'], data['actual_days'], data['leave_days'], data['unpaid_days'],
                data['ot_pay'], data['allowance_total'], data['deduction_total'],
                data['net_pay'], items_json,
                data['base_salary'], data['insured_salary'], data['work_days'], data['actual_days'],
                data['leave_days'], data['unpaid_days'], data['ot_pay'], data['allowance_total'],
                data['deduction_total'], data['net_pay'], items_json,
            ))
            generated += 1
    return jsonify({'ok': True, 'generated': generated, 'month': month})

@app.route('/api/salary/records/<int:rid>', methods=['GET'])
@require_module('salary')
def api_salary_record_get(rid):
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.hire_date
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.id=%s
        """, (rid,)).fetchone()
    if not row: return ('', 404)
    d = salary_record_row(row)
    d['staff_name']    = row['staff_name']
    d['staff_role']    = row['staff_role']
    d['employee_code'] = row['employee_code'] or ''
    d['department']    = row['department'] or ''
    d['hire_date']     = row['hire_date'].isoformat() if row['hire_date'] else ''
    return jsonify(d)

@app.route('/api/salary/records/<int:rid>', methods=['PUT'])
@require_module('salary')
def api_salary_record_update(rid):
    b = request.get_json(force=True)
    items_json = _json.dumps(b.get('items', []), ensure_ascii=False)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET
              allowance_total=%s, deduction_total=%s, net_pay=%s,
              items=%s::jsonb, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('allowance_total',0)), float(b.get('deduction_total',0)),
              float(b.get('net_pay',0)), items_json,
              b.get('note',''), rid)).fetchone()
    return jsonify(salary_record_row(row)) if row else ('', 404)

@app.route('/api/salary/records/<int:rid>/confirm', methods=['POST'])
@require_module('salary')
def api_salary_confirm(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b.get('confirmed_by','管理員'), rid)).fetchone()
    if row:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify(salary_record_row(row)) if row else ('', 404)

@app.route('/api/salary/records/<int:rid>', methods=['DELETE'])
@require_module('salary')
def api_salary_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

# ── Salary Staff Settings ─────────────────────────────────────────

@app.route('/api/salary/staff', methods=['GET'])
@require_module('salary')
def api_salary_staff_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, name, username, role, active, employee_code, department,
                   position_title, hire_date, birth_date, base_salary, insured_salary,
                   daily_hours, ot_rate1, ot_rate2, salary_type, hourly_rate,
                   vacation_quota, salary_notes, salary_item_ids
            FROM punch_staff ORDER BY name
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in ['base_salary','insured_salary','daily_hours','ot_rate1','ot_rate2','hourly_rate']:
            if d.get(f) is not None: d[f] = float(d[f])
        if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
        if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
        d['annual_leave_days'] = _calc_annual_leave_days(d.get('hire_date'))
        d['service_years']     = _calc_service_years(d.get('hire_date'))
        result.append(d)
    return jsonify(result)

@app.route('/api/salary/staff/<int:sid>', methods=['PUT'])
@require_module('salary')
def api_salary_staff_update(sid):
    b = request.get_json(force=True)
    def _f(k, default=0): return float(b.get(k, default) or default)
    def _s(k): return b.get(k, '').strip() if b.get(k) else None
    with get_db() as conn:
        salary_item_ids = b.get('salary_item_ids')
        salary_item_ids_json = _json.dumps(salary_item_ids) if salary_item_ids is not None else None
        conn.execute("""
            UPDATE punch_staff SET
              employee_code=%s, department=%s, position_title=%s,
              hire_date=%s, birth_date=%s,
              base_salary=%s, insured_salary=%s, daily_hours=%s,
              ot_rate1=%s, ot_rate2=%s, salary_type=%s,
              hourly_rate=%s, vacation_quota=%s, salary_notes=%s,
              salary_item_ids=%s
            WHERE id=%s
        """, (_s('employee_code'), _s('department'), _s('position_title'),
              _s('hire_date'), _s('birth_date'),
              _f('base_salary'), _f('insured_salary'), _f('daily_hours') or 8,
              _f('ot_rate1') or 1.33, _f('ot_rate2') or 1.67,
              b.get('salary_type','monthly'),
              _f('hourly_rate'), b.get('vacation_quota') or None,
              b.get('salary_notes',''), salary_item_ids_json, sid))
        row = conn.execute("SELECT * FROM punch_staff WHERE id=%s", (sid,)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)


# ═══════════════════════════════════════════════════════════════════
# Announcement Module (公告管理)
# ═══════════════════════════════════════════════════════════════════

def init_announcement_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id          SERIAL PRIMARY KEY,
                    title       TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    category    TEXT DEFAULT 'general',
                    priority    TEXT DEFAULT 'normal',
                    is_pinned   BOOLEAN DEFAULT FALSE,
                    visible_to  TEXT DEFAULT 'all',
                    published_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ,
                    author      TEXT DEFAULT '管理員',
                    active      BOOLEAN DEFAULT TRUE,
                    view_count  INT DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        print(f"[announcement_init] {e}")

init_announcement_db()

def ann_row(row):
    if not row: return None
    d = dict(row)
    if d.get('published_at'): d['published_at'] = d['published_at'].isoformat()
    if d.get('expires_at'):   d['expires_at']   = d['expires_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    if d.get('updated_at'):   d['updated_at']   = d['updated_at'].isoformat()
    return d

# ── Admin: CRUD ───────────────────────────────────────────────────

@app.route('/api/announcements', methods=['GET'])
@require_module('ann')
def api_ann_list_admin():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM announcements
            ORDER BY is_pinned DESC, published_at DESC
            LIMIT 200
        """).fetchall()
    return jsonify([ann_row(r) for r in rows])

@app.route('/api/announcements', methods=['POST'])
@require_module('ann')
def api_ann_create():
    b = request.get_json(force=True)
    if not b.get('title','').strip():
        return jsonify({'error': '請填寫公告標題'}), 400
    if not b.get('content','').strip():
        return jsonify({'error': '請填寫公告內容'}), 400
    expires = b.get('expires_at') or None
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO announcements
              (title, content, category, priority, is_pinned,
               visible_to, expires_at, author, active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['title'].strip(), b['content'].strip(),
              b.get('category','general'), b.get('priority','normal'),
              bool(b.get('is_pinned', False)), b.get('visible_to','all'),
              expires, b.get('author','管理員').strip(),
              bool(b.get('active', True)))).fetchone()
    return jsonify(ann_row(row)), 201

@app.route('/api/announcements/<int:aid>', methods=['PUT'])
@require_module('ann')
def api_ann_update(aid):
    b = request.get_json(force=True)
    if not b.get('title','').strip():
        return jsonify({'error': '請填寫公告標題'}), 400
    expires = b.get('expires_at') or None
    with get_db() as conn:
        row = conn.execute("""
            UPDATE announcements SET
              title=%s, content=%s, category=%s, priority=%s,
              is_pinned=%s, visible_to=%s, expires_at=%s,
              author=%s, active=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b['title'].strip(), b.get('content','').strip(),
              b.get('category','general'), b.get('priority','normal'),
              bool(b.get('is_pinned', False)), b.get('visible_to','all'),
              expires, b.get('author','管理員').strip(),
              bool(b.get('active', True)), aid)).fetchone()
    return jsonify(ann_row(row)) if row else ('', 404)

@app.route('/api/announcements/<int:aid>', methods=['DELETE'])
@require_module('ann')
def api_ann_delete(aid):
    with get_db() as conn:
        conn.execute("DELETE FROM announcements WHERE id=%s", (aid,))
    return jsonify({'deleted': aid})

@app.route('/api/announcements/<int:aid>/pin', methods=['POST'])
@require_module('ann')
def api_ann_toggle_pin(aid):
    with get_db() as conn:
        row = conn.execute(
            "UPDATE announcements SET is_pinned=NOT is_pinned, updated_at=NOW() WHERE id=%s RETURNING *",
            (aid,)
        ).fetchone()
    return jsonify(ann_row(row)) if row else ('', 404)

# ── Public: employee reads ────────────────────────────────────────

@app.route('/api/announcements/public', methods=['GET'])
def api_ann_public():
    """員工端讀取有效公告"""
    from datetime import datetime as _dta
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM announcements
            WHERE active = TRUE
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY is_pinned DESC, published_at DESC
            LIMIT 50
        """).fetchall()
        # 增加閱讀計數（批次）
    return jsonify([ann_row(r) for r in rows])

@app.route('/api/announcements/<int:aid>/view', methods=['POST'])
def api_ann_view(aid):
    with get_db() as conn:
        conn.execute(
            "UPDATE announcements SET view_count = view_count + 1 WHERE id=%s", (aid,)
        )
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════════
# Feature 1: Taiwan Public Holidays (國定假日)
# ═══════════════════════════════════════════════════════════════════

def init_holiday_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS public_holidays (
                    id          SERIAL PRIMARY KEY,
                    date        DATE NOT NULL UNIQUE,
                    name        TEXT NOT NULL,
                    holiday_type TEXT DEFAULT 'national',
                    note        TEXT DEFAULT '',
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        # Seed 2025 & 2026 Taiwan holidays
        _seed_holidays()
    except Exception as e:
        print(f"[holiday_init] {e}")

def _seed_holidays():
    """台灣2025-2026國定假日"""
    holidays_2025 = [
        ('2025-01-01', '元旦'),
        ('2025-01-27', '農曆除夕'),
        ('2025-01-28', '春節'),
        ('2025-01-29', '春節'),
        ('2025-01-30', '春節'),
        ('2025-01-31', '春節補假'),
        ('2025-02-28', '和平紀念日'),
        ('2025-04-03', '兒童節補假'),
        ('2025-04-04', '兒童節/清明節'),
        ('2025-05-01', '勞動節'),
        ('2025-05-30', '端午節補假'),
        ('2025-06-02', '端午節'),
        ('2025-10-06', '中秋節補假'),
        ('2025-10-07', '中秋節'),
        ('2025-10-10', '國慶日'),
    ]
    holidays_2026 = [
        ('2026-01-01', '元旦'),
        ('2026-01-28', '農曆除夕'),
        ('2026-01-29', '春節'),
        ('2026-01-30', '春節'),
        ('2026-01-31', '春節'),
        ('2026-02-02', '春節補假'),
        ('2026-02-28', '和平紀念日'),
        ('2026-03-02', '和平紀念日補假'),
        ('2026-04-03', '兒童節'),
        ('2026-04-04', '清明節'),
        ('2026-04-05', '清明節補假'),
        ('2026-05-01', '勞動節'),
        ('2026-06-19', '端午節'),
        ('2026-09-25', '中秋節'),
        ('2026-10-09', '國慶日補假'),
        ('2026-10-10', '國慶日'),
    ]
    all_holidays = holidays_2025 + holidays_2026
    try:
        with get_db() as conn:
            existing = conn.execute("SELECT COUNT(*) as c FROM public_holidays").fetchone()['c']
            if existing == 0:
                for date_str, name in all_holidays:
                    try:
                        conn.execute(
                            "INSERT INTO public_holidays (date, name) VALUES (%s,%s) ON CONFLICT (date) DO NOTHING",
                            (date_str, name)
                        )
                    except Exception:
                        pass
    except Exception as e:
        print(f"[holiday_seed] {e}")

init_holiday_db()

def holiday_row(row):
    if not row: return None
    d = dict(row)
    if d.get('date'):       d['date']       = d['date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def _is_holiday(conn, date_str):
    """Check if a date is a public holiday"""
    row = conn.execute(
        "SELECT id FROM public_holidays WHERE date=%s", (date_str,)
    ).fetchone()
    return row is not None

# ── Holiday CRUD API ─────────────────────────────────────────────

@app.route('/api/holidays', methods=['GET'])
@require_module('holiday')
def api_holidays_list():
    year = request.args.get('year', '')
    conds, params = ['TRUE'], []
    if year:
        conds.append("EXTRACT(YEAR FROM date)=%s")
        params.append(int(year))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM public_holidays WHERE {' AND '.join(conds)} ORDER BY date",
            params
        ).fetchall()
    return jsonify([holiday_row(r) for r in rows])

@app.route('/api/holidays/public', methods=['GET'])
def api_holidays_public():
    """Public endpoint for staff page"""
    year = request.args.get('year', '')
    month = request.args.get('month', '')
    conds, params = ['TRUE'], []
    if year:
        conds.append("EXTRACT(YEAR FROM date)=%s"); params.append(int(year))
    if month:
        conds.append("to_char(date,'YYYY-MM')=%s"); params.append(month)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT date, name FROM public_holidays WHERE {' AND '.join(conds)} ORDER BY date",
            params
        ).fetchall()
    return jsonify({r['date'].isoformat(): r['name'] for r in rows})

@app.route('/api/holidays', methods=['POST'])
@require_module('holiday')
def api_holiday_create():
    b = request.get_json(force=True)
    if not b.get('date') or not b.get('name','').strip():
        return jsonify({'error': '請填寫日期和名稱'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO public_holidays (date, name, holiday_type, note)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (date) DO UPDATE
              SET name=EXCLUDED.name, holiday_type=EXCLUDED.holiday_type, note=EXCLUDED.note
            RETURNING *
        """, (b['date'], b['name'].strip(),
              b.get('holiday_type','national'), b.get('note',''))).fetchone()
    return jsonify(holiday_row(row)), 201

@app.route('/api/holidays/<int:hid>', methods=['DELETE'])
@require_module('holiday')
def api_holiday_delete(hid):
    with get_db() as conn:
        conn.execute("DELETE FROM public_holidays WHERE id=%s", (hid,))
    return jsonify({'deleted': hid})

@app.route('/api/holidays/batch', methods=['POST'])
@require_module('holiday')
def api_holiday_batch():
    """Batch import holidays from JSON list"""
    b    = request.get_json(force=True)
    rows = b.get('holidays', [])
    count = 0
    with get_db() as conn:
        for item in rows:
            try:
                conn.execute("""
                    INSERT INTO public_holidays (date, name, holiday_type, note)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (date) DO UPDATE SET name=EXCLUDED.name
                """, (item['date'], item['name'],
                      item.get('holiday_type','national'), item.get('note','')))
                count += 1
            except Exception:
                pass
    return jsonify({'imported': count})


# ═══════════════════════════════════════════════════════════════════
# Feature 2: LINE Notification Helper
# ═══════════════════════════════════════════════════════════════════

def _notify_staff_line(staff_id, message):
    """
    Send LINE notification to a staff member if they have LINE bound.
    Uses the line_punch_config token (same LINE OA).
    """
    if not DATABASE_URL:
        return
    try:
        with get_db() as conn:
            staff = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE id=%s", (staff_id,)
            ).fetchone()
            if not staff or not staff['line_user_id']:
                return
            cfg = conn.execute(
                "SELECT * FROM line_punch_config WHERE id=1"
            ).fetchone()
        if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
            return
        LineBotApi(cfg['channel_access_token']).push_message(
            staff['line_user_id'],
            TextSendMessage(text=message)
        )
    except Exception as e:
        print(f"[LINE notify] staff_id={staff_id}: {e}")


def _notify_review_result(staff_id, category, action, extra_info=''):
    """
    Send a formatted LINE notification for review results.
    category: '補打卡申請', '排休申請', '加班申請', '請假申請', '薪資確認'
    action:   'approved', 'rejected', 'confirmed'
    """
    ACTION_LABEL = {'approved': '核准', 'rejected': '退回', 'confirmed': '確認'}
    ACTION_ICON  = {'approved': '[核准]', 'rejected': '[退回]', 'confirmed': '[確認]'}
    label = ACTION_LABEL.get(action, action)
    icon  = ACTION_ICON.get(action, '')
    msg   = f"{icon} {category}{label}\n{extra_info}\n\n請至員工系統查看詳情。"
    _notify_staff_line(staff_id, msg.strip())


# ═══════════════════════════════════════════════════════════════════
# Feature 3: Export Reports (出勤/薪資報表匯出)
# ═══════════════════════════════════════════════════════════════════

import csv
import io

@app.route('/api/export/attendance', methods=['GET'])
@login_required
def api_export_attendance():
    """匯出月度出勤明細 CSV"""
    month    = request.args.get('month', '')
    staff_id = request.args.get('staff_id', '')
    if not month:
        from datetime import date as _de
        month = _de.today().strftime('%Y-%m')

    conds, params = ["TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s"], [month]
    if staff_id:
        conds.append("pr.staff_id=%s"); params.append(int(staff_id))

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT
                ps.employee_code,
                ps.name as staff_name,
                ps.department,
                ps.role,
                (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                pr.punch_type,
                to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei', 'HH24:MI') as punch_time,
                pr.is_manual,
                pr.manual_by,
                pr.gps_distance,
                pr.location_name,
                pr.note
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY ps.name, pr.punched_at
        """, params).fetchall()

    PUNCH_LABEL = {'in':'上班打卡','out':'下班打卡','break_out':'休息開始','break_in':'休息結束'}

    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel
    writer = csv.writer(output)
    writer.writerow(['員工代碼','姓名','部門','職稱','日期','打卡類型','時間','補打卡','操作人','GPS距離(m)','地點','備註'])

    for r in rows:
        writer.writerow([
            r['employee_code'] or '',
            r['staff_name'],
            r['department']    or '',
            r['role']          or '',
            str(r['work_date']),
            PUNCH_LABEL.get(r['punch_type'], r['punch_type']),
            r['punch_time'],
            '是' if r['is_manual'] else '',
            r['manual_by']     or '',
            r['gps_distance']  if r['gps_distance'] is not None else '',
            r['location_name'] or '',
            r['note']          or '',
        ])

    csv_content = output.getvalue()
    from flask import Response
    return Response(
        csv_content.encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=attendance_{month}.csv'}
    )


@app.route('/api/export/attendance-summary', methods=['GET'])
@login_required
def api_export_attendance_summary():
    """匯出月度出勤摘要 CSV（每人每天工時）"""
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _df
        month = _df.today().strftime('%Y-%m')

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                ps.employee_code,
                ps.name,
                ps.department,
                ps.role,
                (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                MIN(CASE WHEN pr.punch_type='in'  THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as clock_in,
                MAX(CASE WHEN pr.punch_type='out' THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as clock_out,
                MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as ci_ts,
                MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as co_ts,
                BOOL_OR(pr.is_manual) as has_manual,
                COUNT(*) as punch_count
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            GROUP BY ps.employee_code, ps.name, ps.department, ps.role,
                     (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY ps.name, (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
        """, (month,)).fetchall()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['員工代碼','姓名','部門','職稱','日期','上班','下班','工時(h)','打卡次數','含補打'])

    for r in rows:
        dur_h = ''
        if r['ci_ts'] and r['co_ts']:
            from datetime import datetime as _dtx
            try:
                ci = r['ci_ts'] if hasattr(r['ci_ts'], 'timestamp') else _dtx.fromisoformat(str(r['ci_ts']))
                co = r['co_ts'] if hasattr(r['co_ts'], 'timestamp') else _dtx.fromisoformat(str(r['co_ts']))
                dur_h = round((co - ci).total_seconds() / 3600, 2)
            except Exception:
                pass
        writer.writerow([
            r['employee_code'] or '',
            r['name'], r['department'] or '', r['role'] or '',
            str(r['work_date']),
            r['clock_in'] or '', r['clock_out'] or '',
            dur_h,
            r['punch_count'],
            '是' if r['has_manual'] else '',
        ])

    from flask import Response
    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=attendance_summary_{month}.csv'}
    )


@app.route('/api/export/salary', methods=['GET'])
@login_required
def api_export_salary():
    """匯出月度薪資明細 CSV"""
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _dg
        month = _dg.today().strftime('%Y-%m')

    with get_db() as conn:
        rows = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.employee_code,
                   ps.department, ps.role, ps.salary_type
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.month = %s
            ORDER BY ps.name
        """, (month,)).fetchall()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow([
        '員工代碼','姓名','部門','職稱','薪資制度',
        '工作日','出勤天數','請假天數','無薪假天數',
        '津貼合計','扣除合計','加班費','實領金額','狀態','備註'
    ])

    for r in rows:
        items = r['items'] if isinstance(r['items'], list) else _json.loads(r['items'] or '[]')
        sal_type = r['salary_type'] or 'monthly'
        writer.writerow([
            r['employee_code'] or '', r['staff_name'],
            r['department'] or '', r['role'] or '',
            '時薪制' if sal_type == 'hourly' else '月薪制',
            float(r['work_days'] or 0), float(r['actual_days'] or 0),
            float(r['leave_days'] or 0), float(r['unpaid_days'] or 0),
            float(r['allowance_total'] or 0), float(r['deduction_total'] or 0),
            float(r['ot_pay'] or 0), float(r['net_pay'] or 0),
            '已確認' if r['status'] == 'confirmed' else '草稿',
            r['note'] or '',
        ])

    from flask import Response
    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=salary_{month}.csv'}
    )


@app.route('/api/export/leave', methods=['GET'])
@login_required
def api_export_leave():
    """匯出請假記錄 CSV"""
    month    = request.args.get('month', '')
    year     = request.args.get('year',  '')
    staff_id = request.args.get('staff_id', '')

    conds, params = ['lr.status=%s'], ['approved']
    if month: conds.append("to_char(lr.start_date,'YYYY-MM')=%s"); params.append(month)
    if year:  conds.append("EXTRACT(YEAR FROM lr.start_date)=%s"); params.append(int(year))
    if staff_id: conds.append("lr.staff_id=%s"); params.append(int(staff_id))

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT lr.*, ps.name as staff_name, ps.employee_code,
                   ps.department, lt.name as leave_type_name, lt.pay_rate
            FROM leave_requests lr
            JOIN punch_staff ps ON ps.id = lr.staff_id
            JOIN leave_types  lt ON lt.id = lr.leave_type_id
            WHERE {' AND '.join(conds)}
            ORDER BY lr.start_date, ps.name
        """, params).fetchall()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['員工代碼','姓名','部門','假別','薪資倍率','開始日期','結束日期','天數','原因','代理人','狀態'])

    PAY_LABEL = {1.0:'全薪', 0.5:'半薪', 0.0:'無薪'}
    for r in rows:
        writer.writerow([
            r['employee_code'] or '', r['staff_name'], r['department'] or '',
            r['leave_type_name'], PAY_LABEL.get(float(r['pay_rate']), f"{r['pay_rate']}倍"),
            str(r['start_date']), str(r['end_date']),
            float(r['total_days']),
            r['reason'] or '', r['substitute_name'] or '',
            {'approved':'已核准','rejected':'已退回','pending':'待審核'}.get(r['status'], r['status']),
        ])

    from flask import Response
    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=leave_{month or year or "all"}.csv'}
    )


# ── Patch existing review functions with LINE notifications ──────

def _patch_reviews_with_notifications():
    """
    This is called after all route functions are defined.
    We monkey-patch the review endpoints to send LINE notifications.
    The actual patching is done inline in the route handlers below
    via the _notify_review_result helper.
    """
    pass

# Override punch request review to add LINE notification
_orig_punch_req_review = app.view_functions.get('api_punch_req_review')

@app.route('/api/punch/requests/<int:rid>', methods=['PUT'])
@login_required
def api_punch_req_review_v2(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'invalid action'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_requests
            SET status=%s, reviewed_by=%s, review_note=%s, reviewed_at=NOW()
            WHERE id=%s
            RETURNING *, (SELECT name FROM punch_staff WHERE id=staff_id) as staff_name
        """, (new_status, reviewed_by, review_note, rid)).fetchone()
        if not row: return ('', 404)
        if action == 'approve':
            conn.execute("""
                INSERT INTO punch_records
                  (staff_id, punch_type, punched_at, note, is_manual, manual_by)
                VALUES (%s,%s,%s,%s,TRUE,%s)
            """, (row['staff_id'], row['punch_type'], row['requested_at'],
                  f'補打卡申請 #{rid}：{row["reason"]}', reviewed_by))
    # LINE notification
    LABEL = {'in':'上班打卡','out':'下班打卡','break_out':'休息開始','break_in':'休息結束'}
    dt_str = row['requested_at'].isoformat()[:16].replace('T',' ')
    extra  = f"{LABEL.get(row['punch_type'],'')} {dt_str}"
    if review_note: extra += f"\n審核意見：{review_note}"
    _notify_review_result(row['staff_id'], '補打卡申請', action, extra)
    return jsonify(punch_req_row(row))


# ═══════════════════════════════════════════════════════════════════
# Dashboard API
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/dashboard', methods=['GET'])
@login_required
def api_dashboard():
    from datetime import date as _dd, datetime as _ddt, timezone as _tz, timedelta as _tdd
    TW    = _tz(_tdd(hours=8))
    today = _ddt.now(TW).date()

    # 支援傳入月份參數；預設為當月
    req_month = request.args.get('month', '').strip()
    if req_month and len(req_month) == 7:
        month = req_month
        try:
            y, m = int(month[:4]), int(month[5:])
            import calendar as _cal_d
            last_day = _cal_d.monthrange(y, m)[1]
            from datetime import date as _dcheck
            # 如果查詢的是未來月份，today 仍用實際今天
        except Exception:
            month = today.strftime('%Y-%m')
    else:
        month = today.strftime('%Y-%m')

    with get_db() as conn:

        # ── 今日出勤狀況 ─────────────────────────────────────────
        total_staff = conn.execute(
            "SELECT COUNT(*) as c FROM punch_staff WHERE active=TRUE"
        ).fetchone()['c']

        # 今日已打上班卡的人數
        clocked_in = conn.execute("""
            SELECT COUNT(DISTINCT staff_id) as c
            FROM punch_records
            WHERE punch_type='in'
              AND (punched_at AT TIME ZONE 'Asia/Taipei')::date = %s
        """, (today,)).fetchone()['c']

        # 今日已打下班卡的人數
        clocked_out = conn.execute("""
            SELECT COUNT(DISTINCT staff_id) as c
            FROM punch_records
            WHERE punch_type='out'
              AND (punched_at AT TIME ZONE 'Asia/Taipei')::date = %s
        """, (today,)).fetchone()['c']

        # 今日請假人數（已核准）
        on_leave_today = conn.execute("""
            SELECT COUNT(DISTINCT staff_id) as c
            FROM leave_requests
            WHERE status='approved'
              AND start_date <= %s AND end_date >= %s
        """, (today, today)).fetchone()['c']

        # 今日出勤明細（每人狀態）
        today_detail_rows = conn.execute("""
            SELECT ps.id, ps.name, ps.role,
                   MAX(CASE WHEN pr.punch_type='in'  THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as clock_out,
                   COUNT(pr.id) as punch_count
            FROM punch_staff ps
            LEFT JOIN punch_records pr
              ON pr.staff_id = ps.id
              AND (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date = %s
            WHERE ps.active = TRUE
            GROUP BY ps.id, ps.name, ps.role
            ORDER BY ps.name
        """, (today,)).fetchall()

        today_detail = []
        for r in today_detail_rows:
            # Check if on leave
            leave_row = conn.execute("""
                SELECT lt.name as leave_name
                FROM leave_requests lr
                JOIN leave_types lt ON lt.id = lr.leave_type_id
                WHERE lr.staff_id=%s AND lr.status='approved'
                  AND lr.start_date <= %s AND lr.end_date >= %s
                LIMIT 1
            """, (r['id'], today, today)).fetchone()

            if r['clock_in']:
                if r['clock_out']:
                    status = 'done'
                    status_label = '已下班'
                else:
                    status = 'working'
                    status_label = '上班中'
            elif leave_row:
                status = 'leave'
                status_label = leave_row['leave_name']
            else:
                status = 'absent'
                status_label = '未出勤'

            today_detail.append({
                'id':           r['id'],
                'name':         r['name'],
                'role':         r['role'] or '',
                'clock_in':     r['clock_in']  or '',
                'clock_out':    r['clock_out'] or '',
                'punch_count':  r['punch_count'],
                'status':       status,
                'status_label': status_label,
            })

        # ── 待審申請數 ───────────────────────────────────────────
        pending_punch   = conn.execute("SELECT COUNT(*) as c FROM punch_requests WHERE status='pending'").fetchone()['c']
        pending_ot      = conn.execute("SELECT COUNT(*) as c FROM overtime_requests WHERE status='pending'").fetchone()['c']
        pending_sched   = conn.execute("SELECT COUNT(*) as c FROM schedule_requests WHERE status IN ('pending','modified_pending')").fetchone()['c']
        pending_leave   = conn.execute("SELECT COUNT(*) as c FROM leave_requests WHERE status='pending'").fetchone()['c']

        # ── 本月薪資總覽 ─────────────────────────────────────────
        sal_rows = conn.execute("""
            SELECT COUNT(*) as total_count,
                   COUNT(*) FILTER (WHERE status='confirmed') as confirmed_count,
                   COALESCE(SUM(net_pay),0) as total_net,
                   COALESCE(SUM(allowance_total),0) as total_allow,
                   COALESCE(SUM(deduction_total),0) as total_deduct
            FROM salary_records WHERE month=%s
        """, (month,)).fetchone()

        # ── 本月出勤統計（每天出勤人數，用於折線圖）─────────────
        import calendar as _cal
        days_in_month = _cal.monthrange(today.year, today.month)[1]
        daily_rows = conn.execute("""
            SELECT (punched_at AT TIME ZONE 'Asia/Taipei')::date as d,
                   COUNT(DISTINCT staff_id) as cnt
            FROM punch_records
            WHERE punch_type='in'
              AND to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            GROUP BY (punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY d
        """, (month,)).fetchall()
        daily_map = {str(r['d']): r['cnt'] for r in daily_rows}
        daily_attendance = []
        for day in range(1, days_in_month + 1):
            ds = f"{month}-{day:02d}"
            dt = _dd(today.year, today.month, day)
            daily_attendance.append({
                'date':    ds,
                'day':     day,
                'count':   daily_map.get(ds, 0),
                'is_past': dt <= today,
                'weekday': dt.weekday(),
            })

        # ── 本月請假類型分佈（圓餅圖）───────────────────────────
        leave_dist_rows = conn.execute("""
            SELECT lt.name, lt.color, COUNT(*) as cnt,
                   COALESCE(SUM(lr.total_days),0) as days
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id = lr.leave_type_id
            WHERE lr.status='approved'
              AND to_char(lr.start_date,'YYYY-MM')=%s
            GROUP BY lt.name, lt.color
            ORDER BY days DESC
        """, (month,)).fetchall()
        leave_distribution = [
            {'name': r['name'], 'color': r['color'], 'count': r['cnt'], 'days': float(r['days'])}
            for r in leave_dist_rows
        ]

        # ── 本月加班費排行（橫條圖）─────────────────────────────
        ot_rank_rows = conn.execute("""
            SELECT ps.name, ps.role,
                   COALESCE(SUM(r.ot_pay),0) as total_pay,
                   COALESCE(SUM(r.ot_hours),0) as total_hours
            FROM overtime_requests r
            JOIN punch_staff ps ON ps.id = r.staff_id
            WHERE r.status='approved'
              AND to_char(r.request_date,'YYYY-MM')=%s
            GROUP BY ps.name, ps.role
            ORDER BY total_pay DESC
            LIMIT 8
        """, (month,)).fetchall()
        ot_ranking = [
            {'name': r['name'], 'role': r['role'] or '', 'pay': float(r['total_pay']), 'hours': float(r['total_hours'])}
            for r in ot_rank_rows
        ]

    from datetime import date as _ddc
    cur_month = _ddc.today().strftime('%Y-%m')
    return jsonify({
        'month':            month,
        'today':            str(today),
        'is_current_month': month == cur_month,
        # 今日出勤
        'today_summary': {
            'total':       total_staff,
            'working':     clocked_in - clocked_out,
            'clocked_in':  clocked_in,
            'clocked_out': clocked_out,
            'on_leave':    on_leave_today,
            'absent':      total_staff - clocked_in - on_leave_today,
        },
        'today_detail': today_detail,
        # 待審申請
        'pending': {
            'punch':  pending_punch,
            'ot':     pending_ot,
            'sched':  pending_sched,
            'leave':  pending_leave,
            'total':  pending_punch + pending_ot + pending_sched + pending_leave,
        },
        # 本月薪資
        'salary_summary': {
            'total_count':     sal_rows['total_count'],
            'confirmed_count': sal_rows['confirmed_count'],
            'total_net':       float(sal_rows['total_net']),
            'total_allow':     float(sal_rows['total_allow']),
            'total_deduct':    float(sal_rows['total_deduct']),
        },
        # 圖表資料
        'daily_attendance':    daily_attendance,
        'leave_distribution':  leave_distribution,
        'ot_ranking':          ot_ranking,
    })


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

# ═══════════════════════════════════════════════════════════════════
# Feature: Salary PDF (HTML print endpoint)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/salary/records/<int:rid>/pdf', methods=['GET'])
@require_module('salary')
def api_salary_pdf(rid):
    """回傳薪資單 HTML（供瀏覽器列印/另存 PDF）"""
    # 允許員工查看自己的薪資單
    if not session.get('logged_in'):
        sid = session.get('punch_staff_id')
        if not sid:
            return '未登入', 401
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.employee_code,
                   ps.department, ps.role, ps.salary_type,
                   ps.hourly_rate, ps.hire_date
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.id = %s
        """, (rid,)).fetchone()
    if not row:
        return '找不到薪資記錄', 404
    # 員工只能看自己的
    if not session.get('logged_in'):
        if row['staff_id'] != session.get('punch_staff_id'):
            return '無權限', 403

    d         = salary_record_row(row)
    items     = d.get('items') or []
    allow_items  = [i for i in items if i.get('type') == 'allowance']
    deduct_items = [i for i in items if i.get('type') == 'deduction']
    is_hourly = (row['salary_type'] == 'hourly')

    def money(v):
        try: return f"${float(v):,.0f}"
        except: return '$0'

    def esc_h(s):
        return str(s or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    allow_rows = ''.join(f"""
        <tr>
          <td>{esc_h(i['name'])}</td>
          <td class="num green">{money(i['amount'])}</td>
          <td class="note">{esc_h(i.get('calc_note',''))}</td>
        </tr>""" for i in allow_items)

    deduct_rows = ''.join(f"""
        <tr>
          <td>{esc_h(i['name'])}</td>
          <td class="num red">-{money(i['amount'])}</td>
          <td class="note">{esc_h(i.get('calc_note',''))}</td>
        </tr>""" for i in deduct_items)

    punch_table = ''
    if is_hourly and d.get('punch_details'):
        punch_rows = ''.join(f"""
            <tr>
              <td>{p['date']}</td>
              <td>{p['clock_in']}</td>
              <td>{p['clock_out']}</td>
              <td>{p.get('break_mins',0)} min</td>
              <td class="num">{p['net_hours']} h</td>
            </tr>""" for p in d['punch_details'])
        punch_table = f"""
        <h3>每日工時明細</h3>
        <table>
          <thead><tr><th>日期</th><th>上班</th><th>下班</th><th>休息</th><th>工時</th></tr></thead>
          <tbody>{punch_rows}</tbody>
          <tfoot><tr><td colspan="4"><strong>合計</strong></td><td class="num"><strong>{d.get('actual_work_hours',0)} h</strong></td></tr></tfoot>
        </table>"""

    status_str = '已確認' if row['status'] == 'confirmed' else '草稿（未確認）'
    sal_type   = '時薪制' if is_hourly else '月薪制'
    attend_str = (f"實際工時 {d.get('actual_work_hours',0)}h × 時薪 ${float(row['hourly_rate'] or 0):,.0f}"
                  if is_hourly else
                  f"出勤 {d.get('actual_days',0)} 天 / 工作日 {d.get('work_days',0)} 天")
    if float(d.get('leave_days',0)) > 0:
        attend_str += f"，請假 {d.get('leave_days',0)} 天"
    if float(d.get('unpaid_days',0)) > 0:
        attend_str += f"（無薪 {d.get('unpaid_days',0)} 天）"

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>薪資單 {esc_h(row['staff_name'])} {esc_h(row['month'])}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Noto Sans TC', 'PingFang TC', 'Microsoft JhengHei', sans-serif;
          font-size: 13px; color: #1a2340; background: #fff; padding: 32px; }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start;
             border-bottom: 3px solid #1a2340; padding-bottom: 16px; margin-bottom: 24px; }}
  .company {{ font-size: 20px; font-weight: 800; color: #1a2340; }}
  .slip-title {{ font-size: 14px; color: #666; margin-top: 4px; }}
  .staff-info {{ font-size: 12px; color: #444; text-align: right; line-height: 1.8; }}
  .summary {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-bottom: 24px; }}
  .sum-card {{ border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 12px 16px; text-align: center; }}
  .sum-label {{ font-size: 10px; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .06em; }}
  .sum-val {{ font-size: 22px; font-weight: 800; font-family: 'DM Mono', monospace; }}
  .sum-val.green {{ color: #2e9e6b; }}
  .sum-val.red   {{ color: #d64242; }}
  .sum-val.navy  {{ color: #1a2340; }}
  .attend {{ background: #f8fafc; border-radius: 6px; padding: 8px 14px;
             font-size: 12px; color: #666; margin-bottom: 20px; }}
  h3 {{ font-size: 12px; font-weight: 700; color: #888; letter-spacing: .08em;
        text-transform: uppercase; margin: 20px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f1f5f9; padding: 8px 12px; text-align: left;
        font-size: 11px; font-weight: 700; color: #666;
        border-bottom: 2px solid #e2e8f0; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #f0f2f8; }}
  td.num {{ text-align: right; font-family: 'DM Mono', monospace; font-weight: 600; }}
  td.note {{ font-size: 11px; color: #999; }}
  td.green {{ color: #2e9e6b; }}
  td.red   {{ color: #d64242; }}
  tfoot td {{ font-weight: 700; background: #f8fafc; border-top: 2px solid #e2e8f0; }}
  .net-row td {{ font-size: 16px; font-weight: 800; background: #1a2340; color: #fff; }}
  .net-row td.num {{ color: #f0c040; font-size: 20px; }}
  .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #e2e8f0;
             display: flex; justify-content: space-between; font-size: 11px; color: #999; }}
  .sign-area {{ display: flex; gap: 48px; margin-top: 40px; }}
  .sign-box {{ flex: 1; border-top: 1px solid #ccc; padding-top: 6px; font-size: 11px; color: #666; }}
  @media print {{
    body {{ padding: 16px; }}
    @page {{ margin: 12mm; size: A4; }}
    .no-print {{ display: none !important; }}
  }}
</style>
</head>
<body>

<div class="no-print" style="text-align:right;margin-bottom:20px">
  <button onclick="window.print()"
    style="padding:10px 24px;background:#1a2340;color:#fff;border:none;border-radius:6px;
           font-size:13px;font-weight:700;cursor:pointer">列印 / 儲存 PDF</button>
</div>

<div class="header">
  <div>
    <div class="company">薪資明細單</div>
    <div class="slip-title">{esc_h(row['month'])} · {sal_type}</div>
  </div>
  <div class="staff-info">
    <div><strong>{esc_h(row['staff_name'])}</strong></div>
    <div>{esc_h(row['employee_code'] or '')}　{esc_h(row['department'] or '')}　{esc_h(row['role'] or '')}</div>
    <div>到職日：{esc_h(str(row['hire_date']) if row['hire_date'] else '—')}</div>
    <div>狀態：<strong>{status_str}</strong></div>
  </div>
</div>

<div class="summary">
  <div class="sum-card">
    <div class="sum-label">津貼合計</div>
    <div class="sum-val green">{money(d.get('allowance_total',0))}</div>
  </div>
  <div class="sum-card">
    <div class="sum-label">扣除合計</div>
    <div class="sum-val red">-{money(d.get('deduction_total',0))}</div>
  </div>
  <div class="sum-card" style="border-color:#1a2340">
    <div class="sum-label">實領金額</div>
    <div class="sum-val navy">{money(d.get('net_pay',0))}</div>
  </div>
</div>

<div class="attend">{attend_str}</div>

<h3>津貼項目</h3>
<table>
  <thead><tr><th>項目</th><th style="text-align:right">金額</th><th>計算說明</th></tr></thead>
  <tbody>{allow_rows}</tbody>
  <tfoot>
    <tr><td><strong>津貼合計</strong></td><td class="num green"><strong>{money(d.get('allowance_total',0))}</strong></td><td></td></tr>
  </tfoot>
</table>

<h3>扣除項目</h3>
<table>
  <thead><tr><th>項目</th><th style="text-align:right">金額</th><th>計算說明</th></tr></thead>
  <tbody>{deduct_rows if deduct_rows else '<tr><td colspan="3" style="color:#ccc;text-align:center;padding:12px">無扣除項目</td></tr>'}</tbody>
  <tfoot>
    <tr><td><strong>扣除合計</strong></td><td class="num red"><strong>-{money(d.get('deduction_total',0))}</strong></td><td></td></tr>
  </tfoot>
</table>

<table style="margin-top:12px">
  <tbody>
    <tr class="net-row">
      <td><strong>實領金額</strong></td>
      <td class="num">{money(d.get('net_pay',0))}</td>
      <td style="color:#ccc;font-size:11px">= 津貼 {money(d.get('allowance_total',0))} - 扣除 {money(d.get('deduction_total',0))}</td>
    </tr>
  </tbody>
</table>

{punch_table}

<div class="sign-area">
  <div class="sign-box">員工簽名</div>
  <div class="sign-box">主管確認</div>
  <div class="sign-box">人資確認</div>
</div>

<div class="footer">
  <span>本薪資單由系統自動產生</span>
  <span>列印日期：<script>document.write(new Date().toLocaleDateString('zh-TW'))</script></span>
</div>

</body>
</html>"""

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ═══════════════════════════════════════════════════════════════════
# Feature: Batch Review (批次審核)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/punch/requests/batch', methods=['POST'])
@login_required
def api_punch_req_batch():
    b      = request.get_json(force=True)
    ids    = [int(i) for i in b.get('ids', [])]
    action = b.get('action')
    by     = b.get('reviewed_by', '管理員')
    note   = b.get('review_note', '')
    if not ids or action not in ('approve', 'reject'):
        return jsonify({'error': '參數錯誤'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    done = 0
    with get_db() as conn:
        for rid in ids:
            row = conn.execute("""
                UPDATE punch_requests SET status=%s, reviewed_by=%s,
                  review_note=%s, reviewed_at=NOW()
                WHERE id=%s AND status='pending' RETURNING *
            """, (new_status, by, note, rid)).fetchone()
            if row:
                if action == 'approve':
                    conn.execute("""
                        INSERT INTO punch_records
                          (staff_id, punch_type, punched_at, note, is_manual, manual_by)
                        VALUES (%s,%s,%s,%s,TRUE,%s)
                    """, (row['staff_id'], row['punch_type'], row['requested_at'],
                          f'補打卡申請#{rid}', by))
                _notify_review_result(row['staff_id'], '補打卡申請', action,
                                      note and f'批次審核意見：{note}' or '')
                done += 1
    return jsonify({'ok': True, 'done': done})


@app.route('/api/overtime/requests/batch', methods=['POST'])
@login_required
def api_ot_batch():
    b      = request.get_json(force=True)
    ids    = [int(i) for i in b.get('ids', [])]
    action = b.get('action')
    by     = b.get('reviewed_by', '管理員')
    note   = b.get('review_note', '')
    if not ids or action not in ('approve', 'reject'):
        return jsonify({'error': '參數錯誤'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    done = 0
    with get_db() as conn:
        for rid in ids:
            row = conn.execute("""
                UPDATE overtime_requests SET status=%s, reviewed_by=%s,
                  review_note=%s, reviewed_at=NOW()
                WHERE id=%s AND status='pending' RETURNING *
            """, (new_status, by, note, rid)).fetchone()
            if row:
                if action == 'approve':
                    pay, _ = _calc_ot_pay(dict(row), float(row['ot_hours']),
                                          row.get('day_type','weekday'))
                    conn.execute("""
                        UPDATE overtime_requests SET ot_pay=%s WHERE id=%s
                    """, (pay, rid))
                _notify_review_result(row['staff_id'], '加班申請', action, '')
                done += 1
    return jsonify({'ok': True, 'done': done})


@app.route('/api/schedule/requests/batch', methods=['POST'])
@login_required
def api_sched_batch():
    b      = request.get_json(force=True)
    ids    = [int(i) for i in b.get('ids', [])]
    action = b.get('action')
    by     = b.get('reviewed_by', '管理員')
    note   = b.get('review_note', '')
    if not ids or action not in ('approve', 'reject'):
        return jsonify({'error': '參數錯誤'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    done = 0
    with get_db() as conn:
        for rid in ids:
            row = conn.execute("""
                UPDATE schedule_requests SET status=%s, reviewed_by=%s,
                  review_note=%s, reviewed_at=NOW(), updated_at=NOW()
                WHERE id=%s AND status IN ('pending','modified_pending') RETURNING *
            """, (new_status, by, note, rid)).fetchone()
            if row:
                _notify_review_result(row['staff_id'], '排休申請', action, '')
                done += 1
    return jsonify({'ok': True, 'done': done})


@app.route('/api/leave/requests/batch', methods=['POST'])
@login_required
def api_leave_batch():
    b      = request.get_json(force=True)
    ids    = [int(i) for i in b.get('ids', [])]
    action = b.get('action')
    by     = b.get('reviewed_by', '管理員')
    note   = b.get('review_note', '')
    if not ids or action not in ('approve', 'reject'):
        return jsonify({'error': '參數錯誤'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    done = 0
    with get_db() as conn:
        for rid in ids:
            old = conn.execute("SELECT * FROM leave_requests WHERE id=%s", (rid,)).fetchone()
            if not old or old['status'] != 'pending':
                continue
            row = conn.execute("""
                UPDATE leave_requests SET status=%s, reviewed_by=%s,
                  review_note=%s, reviewed_at=NOW(), updated_at=NOW()
                WHERE id=%s RETURNING *
            """, (new_status, by, note, rid)).fetchone()
            if row:
                if action == 'approve':
                    _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                          str(old['start_date'])[:4], float(old['total_days']))
                _notify_review_result(old['staff_id'], '請假申請', action, '')
                done += 1
    return jsonify({'ok': True, 'done': done})


# ═══════════════════════════════════════════════════════════════════
# Feature: Attendance Anomaly Detection (出勤異常)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/attendance/anomalies', methods=['GET'])
@login_required
def api_attendance_anomalies():
    """
    偵測出勤異常：
    - 忘記打下班卡（有上班無下班）
    - 只有下班無上班
    - 遲到（上班時間晚於班別開始時間）
    """
    from datetime import date as _da, datetime as _dta, timezone as _tz, timedelta as _td
    TW    = _tz(_td(hours=8))
    today = _dta.now(TW).date()
    # Check last 7 days
    date_from = today - _td(days=7)

    with get_db() as conn:
        # 取得最近7天打卡記錄（按人、按天）
        rows = conn.execute("""
            SELECT ps.id as staff_id, ps.name, ps.role, ps.department,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   array_agg(pr.punch_type ORDER BY pr.punched_at) as types,
                   MIN(CASE WHEN pr.punch_type='in'  THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as first_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as last_out
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id
            WHERE (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date BETWEEN %s AND %s
              AND ps.active = TRUE
            GROUP BY ps.id, ps.name, ps.role, ps.department,
                     (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY work_date DESC, ps.name
        """, (date_from, today)).fetchall()

        # 取得班別指派（用於遲到判斷）
        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.date, st.start_time, st.name as shift_name
            FROM shift_assignments sa
            JOIN shift_types st ON st.id = sa.shift_type_id
            WHERE sa.date BETWEEN %s AND %s
        """, (date_from, today)).fetchall()
        shift_map = {(r['staff_id'], str(r['date'])): r for r in shift_rows}

        # 今日應出勤但未出勤（排除請假）
        all_staff = conn.execute(
            "SELECT id, name, role, department FROM punch_staff WHERE active=TRUE"
        ).fetchall()
        today_punched_ids = {r['staff_id'] for r in rows if str(r['work_date']) == str(today)}
        on_leave_today_ids = set()
        leave_today = conn.execute("""
            SELECT DISTINCT staff_id FROM leave_requests
            WHERE status='approved' AND start_date <= %s AND end_date >= %s
        """, (today, today)).fetchall()
        for r in leave_today:
            on_leave_today_ids.add(r['staff_id'])

    anomalies = []

    # 1. 近7天：有上班但無下班卡
    for r in rows:
        types = list(r['types']) if r['types'] else []
        has_in  = 'in'  in types
        has_out = 'out' in types
        ds = str(r['work_date'])

        if has_in and not has_out and ds != str(today):
            # 昨天或更早沒打下班卡（今天的可能還沒下班）
            anomalies.append({
                'type':       'missing_out',
                'label':      '忘記下班打卡',
                'severity':   'warning',
                'staff_id':   r['staff_id'],
                'name':       r['name'],
                'role':       r['role'] or '',
                'department': r['department'] or '',
                'date':       ds,
                'detail':     f"上班 {r['first_in']}，無下班記錄",
            })

        if not has_in and has_out:
            anomalies.append({
                'type':       'missing_in',
                'label':      '忘記上班打卡',
                'severity':   'warning',
                'staff_id':   r['staff_id'],
                'name':       r['name'],
                'role':       r['role'] or '',
                'department': r['department'] or '',
                'date':       ds,
                'detail':     f"下班 {r['last_out']}，無上班記錄",
            })

        # 遲到判斷（有班別指派）
        if has_in and r['first_in']:
            shift = shift_map.get((r['staff_id'], ds))
            if shift and shift['start_time']:
                try:
                    sh, sm = map(int, str(shift['start_time'])[:5].split(':'))
                    ih, im = map(int, r['first_in'].split(':'))
                    late_mins = (ih * 60 + im) - (sh * 60 + sm)
                    if late_mins > 10:  # 超過10分鐘算遲到
                        anomalies.append({
                            'type':       'late',
                            'label':      '遲到',
                            'severity':   'info',
                            'staff_id':   r['staff_id'],
                            'name':       r['name'],
                            'role':       r['role'] or '',
                            'department': r['department'] or '',
                            'date':       ds,
                            'detail':     f"應 {shift['start_time'][:5]} 上班，實際 {r['first_in']}（晚 {late_mins} 分鐘）",
                        })
                except Exception:
                    pass

    # 2. 今日未出勤（不含請假）
    for s in all_staff:
        if s['id'] not in today_punched_ids and s['id'] not in on_leave_today_ids:
            anomalies.append({
                'type':       'absent',
                'label':      '今日未出勤',
                'severity':   'error',
                'staff_id':   s['id'],
                'name':       s['name'],
                'role':       s['role'] or '',
                'department': s['department'] or '',
                'date':       str(today),
                'detail':     '今日尚無打卡記錄且未請假',
            })

    # Sort: error > warning > info, then by date desc
    sev_order = {'error': 0, 'warning': 1, 'info': 2}
    anomalies.sort(key=lambda x: (sev_order.get(x['severity'], 9), x['date']))
    return jsonify({'anomalies': anomalies, 'count': len(anomalies), 'checked_from': str(date_from)})


# ═══════════════════════════════════════════════════════════════════
# Feature: Staff Termination (離職流程)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/punch/staff/<int:sid>/terminate', methods=['POST'])
@login_required
def api_staff_terminate(sid):
    """辦理離職：設定離職日、停用帳號、記錄備註"""
    b = request.get_json(force=True)
    termination_date = b.get('termination_date', '')
    reason           = b.get('reason', '').strip()
    last_month       = b.get('last_salary_month', '')
    note             = b.get('note', '').strip()

    if not termination_date:
        return jsonify({'error': '請填寫離職日期'}), 400

    with get_db() as conn:
        # Ensure column exists
        try:
            conn.execute("ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS termination_date DATE")
            conn.execute("ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS termination_reason TEXT DEFAULT ''")
            conn.execute("ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS termination_note TEXT DEFAULT ''")
        except Exception:
            pass

        row = conn.execute("""
            UPDATE punch_staff SET
              active = FALSE,
              termination_date   = %s,
              termination_reason = %s,
              termination_note   = %s,
              salary_notes = COALESCE(salary_notes,'') || %s
            WHERE id = %s RETURNING *
        """, (termination_date, reason, note,
              f'\n【離職】{termination_date} {reason}',
              sid)).fetchone()
        if not row:
            return ('', 404)

    return jsonify({
        'ok': True,
        'staff_id': sid,
        'name': row['name'],
        'termination_date': termination_date,
        'last_salary_month': last_month,
    })


@app.route('/api/punch/staff/<int:sid>/reinstate', methods=['POST'])
@login_required
def api_staff_reinstate(sid):
    """復職（重新啟用帳號）"""
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_staff SET active=TRUE,
              termination_date=NULL, termination_reason='', termination_note=''
            WHERE id=%s RETURNING *
        """, (sid,)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)


@app.route('/api/punch/staff/terminated', methods=['GET'])
@login_required
def api_staff_terminated_list():
    """離職員工清單"""
    with get_db() as conn:
        # Check if column exists
        try:
            rows = conn.execute("""
                SELECT id, name, employee_code, department, role,
                       hire_date, termination_date, termination_reason
                FROM punch_staff
                WHERE active = FALSE
                ORDER BY termination_date DESC NULLS LAST, name
            """).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT id, name, employee_code, department, role, hire_date FROM punch_staff WHERE active=FALSE"
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in ('hire_date','termination_date'):
            if d.get(f): d[f] = str(d[f])
        result.append(d)
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════
# Feature: Salary Formula Builder support (公式說明 API)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/salary/formula/preview', methods=['POST'])
@require_module('salary')
def api_formula_preview():
    """即時預覽公式計算結果"""
    b             = request.get_json(force=True)
    formula       = b.get('formula', '').strip()
    base_salary   = float(b.get('base_salary', 30000))
    insured_salary= float(b.get('insured_salary', 30000))
    service_years = float(b.get('service_years', 1))

    if not formula:
        return jsonify({'result': 0, 'error': None})
    try:
        result = _eval_formula(formula, base_salary, insured_salary, service_years)
        return jsonify({'result': round(result, 2), 'error': None})
    except Exception as e:
        return jsonify({'result': None, 'error': str(e)})