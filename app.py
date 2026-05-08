"""
Entry point for the punch system — thin app.py with Blueprint architecture.
All routes are registered from routes/* blueprints.
"""
import hashlib
import json as _json
import os
import threading
import time
import traceback
import urllib.request
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

import psycopg
from flask import Flask, request, jsonify, session
from flask_compress import Compress
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

from config import TW_TZ, WEEKDAY_ZH, RENDER_EXTERNAL_URL, ADMIN_PASSWORD
from db import (
    _init_db_pool, get_db, _hash_pw, DATABASE_URL,
    _db_pool,
)
from leave_calc import _calc_annual_leave_days

# ─── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['COMPRESS_ALGORITHM']  = ['br', 'gzip']
app.config['COMPRESS_LEVEL']      = 6
app.config['COMPRESS_MIN_SIZE']   = 1000
app.config['COMPRESS_MIMETYPES']  = [
    'text/html', 'text/css', 'text/javascript',
    'application/json', 'application/javascript',
]
Compress(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    _seed = DATABASE_URL or 'punch-system-stable-fallback-key'
    _secret_key = hashlib.sha256(_seed.encode()).hexdigest()
    print("[WARNING] SECRET_KEY env var not set — using derived key. Please set SECRET_KEY for security.")
app.secret_key = _secret_key

app.config['PERMANENT_SESSION_LIFETIME'] = __import__('datetime').timedelta(hours=12)
app.config['SESSION_COOKIE_HTTPONLY']    = True
app.config['SESSION_COOKIE_SAMESITE']   = 'Lax'
app.config['SESSION_COOKIE_SECURE']     = True

print(f"[startup] DATABASE_URL prefix: {DATABASE_URL[:20] if DATABASE_URL else 'NOT SET'}")


@app.before_request
def _refresh_session():
    if session.get('logged_in'):
        session.modified = True


@app.after_request
def _static_cache_headers(response):
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@app.errorhandler(Exception)
def handle_unhandled(e):
    if isinstance(e, HTTPException):
        return e
    print(f"[ERROR] unhandled exception on {request.path}: {e}")
    if request.path.startswith('/api/'):
        return jsonify({'error': '伺服器錯誤，請稍後再試'}), 500
    return jsonify({'error': '伺服器錯誤，請稍後再試'}), 500


# ─── Database Init ────────────────────────────────────────────────────────────

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
                ALTER TABLE line_punch_config
                ADD COLUMN IF NOT EXISTS richmenu_area_texts JSONB DEFAULT NULL
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
        traceback.print_exc()
        return

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
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_code TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_name TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_branch TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_account TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS account_holder TEXT DEFAULT ''",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS day_type TEXT DEFAULT 'weekday'",
        "ALTER TABLE overtime_requests ALTER COLUMN start_time DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN end_time DROP NOT NULL",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS ot_date DATE",
        "ALTER TABLE overtime_requests ALTER COLUMN request_date DROP NOT NULL",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS national_id TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS insurance_type TEXT DEFAULT 'regular'",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS address TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS stores (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL,
            code       TEXT UNIQUE,
            address    TEXT DEFAULT '',
            active     BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS store_id INT REFERENCES stores(id) ON DELETE SET NULL",
        "ALTER TABLE punch_locations ADD COLUMN IF NOT EXISTS store_id INT REFERENCES stores(id) ON DELETE SET NULL",
        "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS store_ids JSONB DEFAULT '[]'",
        "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
        "UPDATE admin_accounts SET active=TRUE WHERE active IS NULL",
        "ALTER TABLE schedule_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
        """CREATE TABLE IF NOT EXISTS shift_staffing_requirements (
            id            SERIAL PRIMARY KEY,
            shift_type_id INT REFERENCES shift_types(id) ON DELETE CASCADE,
            day_of_week   SMALLINT NOT NULL,
            required_count INT NOT NULL DEFAULT 1,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(shift_type_id, day_of_week)
        )""",
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
        "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS password_plain TEXT DEFAULT ''",
        "ALTER TABLE punch_staff    ADD COLUMN IF NOT EXISTS password_plain TEXT DEFAULT ''",
        "ALTER TABLE punch_staff    ADD COLUMN IF NOT EXISTS sort_order INT DEFAULT 0",
        "ALTER TABLE finance_records    ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE finance_categories ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotation_settings ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotations         ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotation_products ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotations         ADD COLUMN IF NOT EXISTS deposit_rate NUMERIC DEFAULT 100",
        "ALTER TABLE quotations         ADD COLUMN IF NOT EXISTS show_wedding_content BOOLEAN DEFAULT TRUE",
        "ALTER TABLE finance_records    ADD COLUMN IF NOT EXISTS linked_quotation_id INTEGER",
        """CREATE TABLE IF NOT EXISTS clients (
            id           SERIAL PRIMARY KEY,
            company_unit TEXT DEFAULT 'ad',
            name         TEXT NOT NULL,
            phone        TEXT DEFAULT '',
            address      TEXT DEFAULT '',
            line_id      TEXT DEFAULT '',
            email        TEXT DEFAULT '',
            note         TEXT DEFAULT '',
            created_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_punch_records_staff_punched ON punch_records(staff_id, punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_punch_records_staff_type_punched ON punch_records(staff_id, punch_type, punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_punch_records_punched ON punch_records(punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_punch_records_tw_month ON punch_records(to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM'))",
        "CREATE INDEX IF NOT EXISTS idx_punch_staff_username ON punch_staff(username)",
        "CREATE INDEX IF NOT EXISTS idx_punch_staff_line_user ON punch_staff(line_user_id) WHERE line_user_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_punch_requests_status ON punch_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_punch_requests_staff_status ON punch_requests(staff_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_punch_requests_status_created ON punch_requests(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_overtime_requests_status ON overtime_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_overtime_requests_staff_status ON overtime_requests(staff_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_overtime_requests_status_created ON overtime_requests(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_schedule_requests_status ON schedule_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_schedule_requests_staff ON schedule_requests(staff_id)",
        "CREATE INDEX IF NOT EXISTS idx_shift_assignments_date ON shift_assignments(shift_date)",
        "CREATE INDEX IF NOT EXISTS idx_shift_assignments_staff_date ON shift_assignments(staff_id, shift_date)",
        "CREATE INDEX IF NOT EXISTS idx_shift_assignments_type_date ON shift_assignments(shift_type_id, shift_date)",
        """CREATE TABLE IF NOT EXISTS announcements (
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
        )""",
        """CREATE TABLE IF NOT EXISTS training_records (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            course_name     TEXT NOT NULL,
            category        TEXT NOT NULL DEFAULT 'general',
            completed_date  DATE,
            expiry_date     DATE,
            certificate_no  TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_categories (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'expense',
            color       TEXT DEFAULT '#4a7bda',
            sort_order  INT DEFAULT 0,
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_documents (
            id              SERIAL PRIMARY KEY,
            filename        TEXT NOT NULL,
            doc_type        TEXT DEFAULT '',
            ocr_raw         JSONB DEFAULT '{}',
            upload_date     DATE DEFAULT CURRENT_DATE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_records (
            id              SERIAL PRIMARY KEY,
            record_date     DATE NOT NULL,
            category_id     INT REFERENCES finance_categories(id) ON DELETE SET NULL,
            type            TEXT NOT NULL DEFAULT 'expense',
            title           TEXT NOT NULL,
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            tax_amount      NUMERIC(14,2) DEFAULT 0,
            vendor          TEXT DEFAULT '',
            invoice_no      TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            document_id     INT,
            created_by      TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS expense_claims (
            id                   SERIAL PRIMARY KEY,
            staff_id             INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            title                TEXT NOT NULL,
            amount               NUMERIC(12,2) NOT NULL DEFAULT 0,
            expense_date         DATE NOT NULL,
            category             TEXT DEFAULT '',
            note                 TEXT DEFAULT '',
            status               TEXT NOT NULL DEFAULT 'pending',
            document_id          INT REFERENCES finance_documents(id) ON DELETE SET NULL,
            review_note          TEXT DEFAULT '',
            reviewed_by          TEXT DEFAULT '',
            reviewed_at          TIMESTAMPTZ,
            finance_record_id    INT REFERENCES finance_records(id) ON DELETE SET NULL,
            created_at           TIMESTAMPTZ DEFAULT NOW(),
            document_id2         INT REFERENCES finance_documents(id) ON DELETE SET NULL,
            reimbursement_method TEXT NOT NULL DEFAULT '匯款',
            bank_name            TEXT NOT NULL DEFAULT '',
            bank_account         TEXT NOT NULL DEFAULT '',
            account_holder       TEXT NOT NULL DEFAULT '',
            expense_type         TEXT NOT NULL DEFAULT '支出',
            company              TEXT NOT NULL DEFAULT '進光設計',
            vendor               TEXT NOT NULL DEFAULT ''
        )""",
    ]
    for sql in migrations:
        try:
            with get_db() as mc:
                mc.execute(sql)
        except Exception as me:
            print(f"[MIGRATION SKIP] {sql[:70]}: {me}")

    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM admin_accounts").fetchone()['c']
            if cnt == 0:
                all_modules = _json.dumps(['punch','sched','leave','salary','ann','holiday','finance'])
                conn.execute("""
                    INSERT INTO admin_accounts (username, password_hash, display_name, permissions, is_super, active)
                    VALUES (%s,%s,'超級管理員',%s,TRUE,TRUE)
                """, ('admin', _hash_pw(ADMIN_PASSWORD), all_modules))
                print("[OK] Default super admin seeded (username: admin)")
            else:
                conn.execute("""
                    UPDATE admin_accounts
                    SET password_hash=%s, active=TRUE
                    WHERE username='admin' AND is_super=TRUE
                """, (_hash_pw(ADMIN_PASSWORD),))
    except Exception as e:
        print(f"[WARN] admin seed: {e}")

    try:
        with get_db() as conn:
            conn.execute("INSERT INTO stores (name, code) VALUES ('主店','main') ON CONFLICT (code) DO NOTHING")
            conn.execute("UPDATE punch_staff     SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
            conn.execute("UPDATE punch_locations SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
    except Exception as e:
        print(f"[WARN] store seed: {e}")

    print("[OK] Database initialised")


# ─── Blueprint Init & Registration ───────────────────────────────────────────

def _register_blueprints():
    from routes.admin        import bp as admin_bp,        init as admin_init
    from routes.punch        import bp as punch_bp,        init as punch_init
    from routes.line_bot     import bp as line_bot_bp,     init as line_bot_init
    from routes.schedule     import bp as schedule_bp,     init as schedule_init
    from routes.shifts       import bp as shifts_bp,       init as shifts_init
    from routes.overtime     import bp as overtime_bp,     init as overtime_init
    from routes.leave        import bp as leave_bp,        init as leave_init
    from routes.salary       import bp as salary_bp,       init as salary_init
    from routes.finance      import bp as finance_bp,      init as finance_init
    from routes.quotation    import bp as quotation_bp,    init as quotation_init
    from routes.expense      import bp as expense_bp,      init as expense_init
    from routes.work_logs    import bp as work_logs_bp,    init as work_logs_init
    from routes.performance  import bp as performance_bp,  init as performance_init
    from routes.announcements import bp as ann_bp,         init as ann_init
    from routes.holidays     import bp as holidays_bp,     init as holidays_init
    from routes.mobile       import bp as mobile_bp
    from routes.webauthn     import bp as webauthn_bp, init as webauthn_init

    # Call each blueprint's init() to create module-specific tables
    for fn, name in [
        (finance_init,     'finance'),
        (quotation_init,   'quotation'),
        (salary_init,      'salary'),
        (leave_init,       'leave'),
        (performance_init, 'performance'),
        (expense_init,     'expense'),
        (ann_init,         'announcements'),
        (holidays_init,    'holidays'),
        (work_logs_init,   'work_logs'),
        (line_bot_init,    'line_bot'),
        (admin_init,       'admin'),
        (punch_init,       'punch'),
        (schedule_init,    'schedule'),
        (shifts_init,      'shifts'),
        (overtime_init,    'overtime'),
        (webauthn_init,    'webauthn'),
    ]:
        try:
            fn()
        except Exception as e:
            print(f"[WARN] {name}.init() failed: {e}")

    # Register blueprints
    app.register_blueprint(admin_bp)
    app.register_blueprint(punch_bp)
    app.register_blueprint(line_bot_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(shifts_bp)
    app.register_blueprint(overtime_bp)
    app.register_blueprint(leave_bp)
    app.register_blueprint(salary_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(quotation_bp)
    app.register_blueprint(expense_bp)
    app.register_blueprint(work_logs_bp)
    app.register_blueprint(performance_bp)
    app.register_blueprint(ann_bp)
    app.register_blueprint(holidays_bp)
    app.register_blueprint(mobile_bp)
    app.register_blueprint(webauthn_bp)

    # export.py may not exist yet during development
    try:
        from routes.export import bp as export_bp, init as export_init
        try:
            export_init()
        except Exception as e:
            print(f"[WARN] export.init() failed: {e}")
        app.register_blueprint(export_bp)
    except ImportError:
        print("[WARN] routes/export.py not found — export routes unavailable")


# ─── Keep-Alive & Background Threads ─────────────────────────────────────────

_WORKER_ID = os.environ.get('GUNICORN_WORKER_ID', '1')


def keep_alive():
    if _WORKER_ID != '1':
        return
    time.sleep(30)
    base = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else 'http://localhost:5000'
    consecutive_failures = 0
    while True:
        try:
            urllib.request.urlopen(
                urllib.request.Request(f'{base}/health', headers={'User-Agent': 'KeepAlive/1.0'}),
                timeout=30,
            )
            if consecutive_failures > 0:
                print(f"[keep-alive] recovered after {consecutive_failures} failure(s)")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                print(f"[keep-alive] ping failed ({consecutive_failures}x): {e}")
        time.sleep(4 * 60)


def _db_pool_keep_alive():
    if _WORKER_ID != '1':
        return
    time.sleep(60)
    from db import _db_pool as pool
    while True:
        try:
            if pool is not None:
                with pool.connection(timeout=5.0) as conn:
                    conn.execute('SELECT 1')
        except Exception as e:
            print(f"[db-keepalive] {e}")
        time.sleep(240)


def _run_annual_leave_sync():
    from datetime import date as _d_sync
    year = str(_d_sync.today().year)
    try:
        with get_db() as conn:
            staff_list = conn.execute(
                "SELECT id, name, hire_date FROM punch_staff WHERE active=TRUE AND hire_date IS NOT NULL"
            ).fetchall()
            lt = conn.execute("SELECT id FROM leave_types WHERE code='annual'").fetchone()
            if not lt:
                return
            lt_id = lt['id']
            for s in staff_list:
                days = _calc_annual_leave_days(s['hire_date'])
                conn.execute("""
                    INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
                    VALUES (%s,%s,%s,%s,0)
                    ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
                      SET total_days=EXCLUDED.total_days, updated_at=NOW()
                """, (s['id'], lt_id, int(year), days))
    except Exception as e:
        print(f"[annual_leave_sync] {e}")


def _annual_leave_sync_loop():
    time.sleep(30)
    _run_annual_leave_sync()
    while True:
        now = _dt.now(TW_TZ)
        tmr = (now + _td(days=1)).date()
        tomorrow_05 = _dt(tmr.year, tmr.month, tmr.day, 0, 5, tzinfo=TW_TZ)
        sleep_secs = (tomorrow_05 - now).total_seconds()
        if sleep_secs < 0:
            sleep_secs = 3600
        time.sleep(sleep_secs)
        _run_annual_leave_sync()


# ─── Startup ──────────────────────────────────────────────────────────────────

_init_db_pool()

# Blueprint-specific tables first so migrations in init_db() can succeed
from routes.finance      import init as _finance_pre_init
from routes.quotation    import init as _quotation_pre_init
from routes.salary       import init as _salary_pre_init
from routes.leave        import init as _leave_pre_init
from routes.performance  import init as _perf_pre_init
from routes.expense      import init as _expense_pre_init
from routes.announcements import init as _ann_pre_init
from routes.holidays     import init as _holidays_pre_init
from routes.work_logs    import init as _work_logs_pre_init
from routes.line_bot     import init as _line_bot_pre_init

for _fn, _nm in [
    (_finance_pre_init,   'finance'),
    (_quotation_pre_init, 'quotation'),
    (_salary_pre_init,    'salary'),
    (_leave_pre_init,     'leave'),
    (_perf_pre_init,      'performance'),
    (_expense_pre_init,   'expense'),
    (_ann_pre_init,       'announcements'),
    (_holidays_pre_init,  'holidays'),
    (_work_logs_pre_init, 'work_logs'),
    (_line_bot_pre_init,  'line_bot'),
]:
    try:
        _fn()
    except Exception as _e:
        print(f"[WARN] pre-init {_nm}: {_e}")

init_db()
_register_blueprints()

threading.Thread(target=keep_alive,             daemon=True).start()
threading.Thread(target=_db_pool_keep_alive,    daemon=True).start()
threading.Thread(target=_annual_leave_sync_loop, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
