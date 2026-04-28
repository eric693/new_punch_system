import hashlib
import math
import os
import threading
import time
import traceback
import urllib.request
from datetime import date
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from psycopg_pool import ConnectionPool
from psycopg_pool.errors import PoolTimeout
from flask import (
    Flask, request, jsonify, render_template,
    session, redirect, url_for, abort, make_response
)
from flask_compress import Compress
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, LocationMessage
)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
ADMIN_PASSWORD            = os.environ.get('ADMIN_PASSWORD', 'admin123')
_raw_db_url               = os.environ.get('DATABASE_URL', '')
DATABASE_URL              = _raw_db_url.replace('postgres://', 'postgresql://', 1) if _raw_db_url.startswith('postgres://') else _raw_db_url
RENDER_EXTERNAL_URL       = os.environ.get('RENDER_EXTERNAL_URL', '')

app = Flask(__name__)
Compress(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# SECRET_KEY 必須穩定，否則每次重啟都會讓所有 session 失效
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

@app.before_request
def _refresh_session():
    """每次請求刷新 session 存活時間，避免使用中途登出。"""
    if session.get('logged_in'):
        session.modified = True

print(f"[startup] DATABASE_URL prefix: {DATABASE_URL[:20] if DATABASE_URL else 'NOT SET'}")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ─── Imports ──────────────────────────────────────────────────────────────────
import json as _json
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

TW_TZ = _tz(_td(hours=8))   # Asia/Taipei (UTC+8)

WEEKDAY_ZH = ['一', '二', '三', '四', '五', '六', '日']

# ─── PostgreSQL ───────────────────────────────────────────────────────────────

_db_pool: 'ConnectionPool | None' = None

def _init_db_pool():
    global _db_pool
    if DATABASE_URL and _db_pool is None:
        try:
            _db_pool = ConnectionPool(
                DATABASE_URL,
                min_size=3,
                max_size=20,
                kwargs={'row_factory': dict_row},
                open=True,
                reconnect_timeout=30,
                max_lifetime=600,   # recycle connections every 10 min before they go stale
                max_idle=300,       # close connections idle > 5 min
                check=ConnectionPool.check_connection,
            )
            print("[pool] Connection pool initialized")
        except Exception as e:
            print(f"[pool] Failed to init pool: {e}")

@contextmanager
def get_db():
    if _db_pool is not None:
        try:
            with _db_pool.connection(timeout=10.0) as conn:
                yield conn
            return
        except (psycopg.OperationalError, PoolTimeout) as exc:
            print(f"[pool] connection failed ({type(exc).__name__}), falling back to direct connect")
            try:
                _db_pool.check()
            except Exception:
                pass
    # Direct fallback with up to 3 attempts (handles brief DB blips)
    last_exc = None
    for attempt in range(3):
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row,
                                 connect_timeout=10) as conn:
                yield conn
            return
        except Exception as e:
            last_exc = e
            if attempt < 2:
                print(f"[db] direct connect attempt {attempt+1} failed: {e}, retrying...")
                time.sleep(2)
    raise last_exc

def _hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# ─── In-Memory Caches ─────────────────────────────────────────────────────────
# punch_config 和 punch_locations 幾乎不變，快取後每次打卡少 2 個 DB 查詢。

_punch_cfg_cache:  dict = {'data': None, 'at': 0.0}
_punch_locs_cache: dict = {'data': None, 'at': 0.0}
_PUNCH_CFG_TTL  = 300   # 5 分鐘
_PUNCH_LOCS_TTL = 60    # 1 分鐘

# 靜態查詢表快取（財務類別、常用品項、門市）—— 30 秒 TTL
_fin_cats_cache:   dict = {}   # key: company_unit str → {data, at}
_qproducts_cache:  dict = {}   # key: company_unit str → {data, at}
_stores_cache:     dict = {'data': None, 'at': 0.0}
_STATIC_TTL = 30.0             # 30 秒

# 更靜態的查詢表快取（假別、班別、薪資項目、公告）—— 60 秒 TTL
_leave_types_all_cache:    dict = {'data': None, 'at': 0.0}
_leave_types_pub_cache:    dict = {'data': None, 'at': 0.0}
_shift_types_all_cache:    dict = {'data': None, 'at': 0.0}
_shift_types_pub_cache:    dict = {'data': None, 'at': 0.0}
_salary_items_cache:       dict = {'data': None, 'at': 0.0}
_ann_public_cache:         dict = {'data': None, 'at': 0.0}
_holidays_pub_cache:       dict = {}   # key: "YYYY-MM" or "YYYY" → {data, at}
_SEMISTATIC_TTL = 60.0           # 60 秒
_HOLIDAY_TTL    = 600.0          # 10 分鐘（國定假日幾乎不動）
_ANN_TTL        = 30.0           # 30 秒（公告較常更新）

# 待審 badge 計數快取（每位管理員各自一份）—— 8 秒 TTL
_badges_cache:   dict = {}   # key: admin_id → {data, at}
_BADGES_TTL = 8.0            # 8 秒（足夠吸收 setInterval+手動觸發的重複呼叫）

# 重量級查詢快取 —— 避免每次切頁都重跑複雜 GROUP BY
_dashboard_cache:    dict = {}   # key: "month" → {data, at}
_punch_summary_cache: dict = {}  # key: "month" → {data, at}
_anomalies_cache:    dict = {'data': None, 'at': 0.0}
_labor_cost_cache:   dict = {'data': None, 'at': 0.0}
_heatmap_cache:      dict = {}   # key: "month" → {data, at}
_DASHBOARD_TTL  = 60.0   # 60 秒
_SUMMARY_TTL    = 60.0   # 60 秒
_ANOMALIES_TTL  = 120.0  # 2 分鐘（7天異常掃描，變化不頻繁）
_LABOR_TTL      = 120.0  # 2 分鐘

# 管理員帳號快取 —— 登入時免 DB 查詢（帳號異動時立即清除）
_admin_acct_cache: dict = {'by_username': None, 'by_id': None, 'at': 0.0}
_ADMIN_ACCT_TTL = 30.0   # 30 秒

def _invalidate_admin_cache():
    _admin_acct_cache['by_username'] = None
    _admin_acct_cache['by_id'] = None

def _ensure_admin_cache():
    now = time.time()
    c = _admin_acct_cache
    if c['by_username'] is not None and now - c['at'] < _ADMIN_ACCT_TTL:
        return
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM admin_accounts WHERE active=TRUE").fetchall()
    dicts = [dict(r) for r in rows]
    c['by_username'] = {r['username']: r for r in dicts}
    c['by_id']       = {r['id']:       r for r in dicts}
    c['at'] = now

def _get_admin_by_username(username: str):
    _ensure_admin_cache()
    return _admin_acct_cache['by_username'].get(username)

def _get_admin_by_id(admin_id: int):
    _ensure_admin_cache()
    return _admin_acct_cache['by_id'].get(admin_id)


def _get_cfg_cached(conn):
    now = time.time()
    if _punch_cfg_cache['data'] is not None and now - _punch_cfg_cache['at'] < _PUNCH_CFG_TTL:
        return _punch_cfg_cache['data']
    row = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
    _punch_cfg_cache['data'] = row
    _punch_cfg_cache['at']   = now
    return row


def _invalidate_cfg_cache():
    _punch_cfg_cache['data'] = None


def _get_locs_cached(conn):
    now = time.time()
    if _punch_locs_cache['data'] is not None and now - _punch_locs_cache['at'] < _PUNCH_LOCS_TTL:
        return _punch_locs_cache['data']
    rows = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()
    _punch_locs_cache['data'] = rows
    _punch_locs_cache['at']   = now
    return rows


def _invalidate_locs_cache():
    _punch_locs_cache['data'] = None


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
        traceback.print_exc()
        return

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
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_code TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_name TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_branch TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_account TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS account_holder TEXT DEFAULT ''",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS day_type TEXT DEFAULT 'weekday'",
        "ALTER TABLE overtime_requests ALTER COLUMN start_time DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN end_time DROP NOT NULL",
        # 員工個人/保險欄位
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS national_id TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS insurance_type TEXT DEFAULT 'regular'",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS address TEXT DEFAULT ''",
        # 多店
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
        # 財務 + 報價 二公司支援
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
        # ── 效能索引 ──────────────────────────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_punch_records_staff_punched ON punch_records(staff_id, punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_punch_records_staff_type_punched ON punch_records(staff_id, punch_type, punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_punch_records_punched ON punch_records(punched_at DESC)",
        # Functional index for to_char month queries throughout the codebase
        "CREATE INDEX IF NOT EXISTS idx_punch_records_tw_month ON punch_records(to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM'))",
        "CREATE INDEX IF NOT EXISTS idx_punch_staff_username ON punch_staff(username)",
        "CREATE INDEX IF NOT EXISTS idx_punch_staff_line_user ON punch_staff(line_user_id) WHERE line_user_id IS NOT NULL",
        # ── 公告管理 ─────────────────────────────────────────────────────────────
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
        # ── 教育訓練 ─────────────────────────────────────────────────────────────
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
        # ── 財務管理基礎表（expense_claims 依賴這三張表）─────────────────────────────
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
        # ── 費用報帳申請 ─────────────────────────────────────────────────────────────
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

    # Seed default super admin if no accounts exist; always sync password & active
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
                # Keep built-in admin password in sync with ADMIN_PASSWORD env var
                conn.execute("""
                    UPDATE admin_accounts
                    SET password_hash=%s, active=TRUE
                    WHERE username='admin' AND is_super=TRUE
                """, (_hash_pw(ADMIN_PASSWORD),))
    except Exception as e:
        print(f"[WARN] admin seed: {e}")

    # 確保預設店家存在，並補齊舊資料
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO stores (name, code) VALUES ('主店','main') ON CONFLICT (code) DO NOTHING")
            conn.execute("UPDATE punch_staff     SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
            conn.execute("UPDATE punch_locations SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
    except Exception as e:
        print(f"[WARN] store seed: {e}")

    print("[OK] Database initialised")


_init_db_pool()
init_db()

# ─── Keep-Alive ───────────────────────────────────────────────────────────────
# Only run in gunicorn worker #1 (GUNICORN_WORKER_ID=1 set via config) or when
# running directly (no gunicorn). This prevents N workers from each spawning a
# ping thread and flooding logs when the service is temporarily unreachable.

_WORKER_ID = os.environ.get('GUNICORN_WORKER_ID', '1')

def keep_alive():
    if _WORKER_ID != '1':
        return
    time.sleep(30)  # wait for server to fully start
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
        time.sleep(4 * 60)  # 4 min — under pool max_idle (5 min) to keep connections warm

threading.Thread(target=keep_alive, daemon=True).start()


def _db_pool_keep_alive():
    """Ping pool every 4 min to prevent idle connections from being dropped."""
    if _WORKER_ID != '1':
        return
    time.sleep(60)
    while True:
        try:
            if _db_pool is not None:
                with _db_pool.connection(timeout=5.0) as conn:
                    conn.execute('SELECT 1')
        except Exception as e:
            print(f"[db-keepalive] {e}")
        time.sleep(240)

threading.Thread(target=_db_pool_keep_alive, daemon=True).start()


# ─── 特休自動同步 ─────────────────────────────────────────────────────────────

def _run_annual_leave_sync():
    """依勞基法第38條，依到職日計算特休天數，寫入 leave_balances。每日午夜自動執行。"""
    from datetime import date as _d_sync
    import json as _json_sync
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
    import time as _time_sync
    _time_sync.sleep(30)  # wait for module to finish loading before first sync
    # 啟動時立即執行一次
    _run_annual_leave_sync()
    while True:
        # 計算距離明天 00:05 台北時間的秒數
        now = _dt.now(TW_TZ)
        tmr = (now + _td(days=1)).date()
        tomorrow_05 = _dt(tmr.year, tmr.month, tmr.day, 0, 5, tzinfo=TW_TZ)
        sleep_secs = (tomorrow_05 - now).total_seconds()
        if sleep_secs < 0:
            sleep_secs = 3600
        _time_sync.sleep(sleep_secs)
        _run_annual_leave_sync()


threading.Thread(target=_annual_leave_sync_loop, daemon=True).start()


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    try:
        with get_db() as conn:
            conn.execute('SELECT 1')
        return jsonify({'status': 'ok', 'db': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500


@app.errorhandler(Exception)
def handle_unhandled(e):
    # Let HTTP exceptions (404, 401, etc.) pass through unchanged
    if isinstance(e, HTTPException):
        return e
    print(f"[ERROR] unhandled exception on {request.path}: {e}")
    if request.path.startswith('/api/'):
        return jsonify({'error': '伺服器錯誤，請稍後再試'}), 500
    return jsonify({'error': '伺服器錯誤，請稍後再試'}), 500

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

@app.route('/api/admin/badges')
@login_required
def api_admin_badges():
    perms     = session.get('admin_permissions') or []
    is_super  = session.get('admin_is_super', False)
    admin_id  = session.get('admin_id', 0)
    has_sched = is_super or 'sched' in perms
    has_leave = is_super or 'leave' in perms
    now = time.time()
    cached = _badges_cache.get(admin_id)
    if cached and now - cached['at'] < _BADGES_TTL:
        return jsonify(cached['data'])
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM punch_requests    WHERE status='pending')          AS punch_cnt,
                (SELECT COUNT(*) FROM overtime_requests WHERE status='pending')          AS ot_cnt,
                (SELECT COUNT(*) FROM schedule_requests WHERE status='pending')          AS sched_pending_cnt,
                (SELECT COUNT(*) FROM schedule_requests WHERE status='modified_pending') AS sched_modified_cnt,
                (SELECT COUNT(*) FROM leave_requests    WHERE status='pending')          AS leave_cnt,
                (SELECT COUNT(*) FROM expense_claims    WHERE status='pending')          AS expense_cnt
        """).fetchone()
    result = {
        'punch':          int(row['punch_cnt']),
        'overtime':       int(row['ot_cnt']),
        'sched_pending':  int(row['sched_pending_cnt'])  if has_sched else 0,
        'sched_modified': int(row['sched_modified_cnt']) if has_sched else 0,
        'leave':          int(row['leave_cnt'])          if has_leave else 0,
        'expense':        int(row['expense_cnt']),
    }
    _badges_cache[admin_id] = {'data': result, 'at': now}
    return jsonify(result)

def _update_last_login(admin_id):
    try:
        with get_db() as conn:
            conn.execute("UPDATE admin_accounts SET last_login_at=NOW() WHERE id=%s", (admin_id,))
    except Exception:
        pass

def _set_admin_session(row):
    perms = row['permissions']
    if isinstance(perms, str):
        try: perms = _json.loads(perms)
        except (ValueError, TypeError): perms = []
    session.permanent             = True
    session['logged_in']          = True
    session['admin_id']           = row['id']
    session['admin_username']     = row['username']
    session['admin_display_name'] = row['display_name'] or row['username']
    session['admin_permissions']  = perms
    session['admin_is_super']     = bool(row['is_super'])
    threading.Thread(target=_update_last_login, args=(row['id'],), daemon=True).start()

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
            try:
                row = _get_admin_by_username(username)
                if row and row['password_hash'] == _hash_pw(password):
                    _set_admin_session(row)
                    return redirect(url_for('admin_dashboard'))
                error = '帳號或密碼錯誤'
            except Exception as e:
                print(f"[ERROR] admin_login db error: {e}")
                error = '系統錯誤，請稍後再試'
    return render_template('login.html', error=error)

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    """AJAX admin login — returns JSON so the browser can navigate without a full page reload."""
    b = request.get_json(force=True)
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '請輸入帳號與密碼'}), 400
    try:
        row = _get_admin_by_username(username)
        if not row or row['password_hash'] != _hash_pw(password):
            return jsonify({'error': '帳號或密碼錯誤'}), 401
        _set_admin_session(row)
        return jsonify({'ok': True, 'redirect': '/admin'})
    except Exception as e:
        print(f"[ERROR] api_admin_login: {e}")
        return jsonify({'error': '系統錯誤，請稍後再試'}), 500

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
    admin_id = session.get('admin_id', 0)
    # ETag = template mtime + user identity，template 未改且權限未變時直接 304
    template_path = os.path.join(app.template_folder or 'templates', 'admin.html')
    try:
        tmtime = int(os.path.getmtime(template_path))
    except OSError:
        tmtime = 0
    etag_src = f"{tmtime}:{admin_id}:{sorted(perms)}:{is_super}"
    etag = '"' + hashlib.md5(etag_src.encode()).hexdigest()[:16] + '"'
    if request.headers.get('If-None-Match') == etag:
        return ('', 304)
    resp = make_response(render_template('admin.html',
        admin_display_name=session.get('admin_display_name',''),
        admin_permissions=perms,
        admin_is_super=is_super,
    ))
    resp.headers['ETag']          = etag
    resp.headers['Cache-Control'] = 'private, no-cache'
    return resp

# ── Admin Accounts API ────────────────────────────────────────────────────────

def _admin_row(r):
    if not r: return None
    d = dict(r)
    d.pop('password_hash', None)
    if 'password_plain' not in d: d['password_plain'] = ''
    perms = d.get('permissions')
    if isinstance(perms, str):
        try: d['permissions'] = _json.loads(perms)
        except (ValueError, TypeError): d['permissions'] = []
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
                INSERT INTO admin_accounts (username, password_hash, password_plain, display_name, permissions, is_super, active)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (username, _hash_pw(password), password, b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)), True)).fetchone()
        except Exception as e:
            if 'unique' in str(e).lower(): return jsonify({'error': '帳號已存在'}), 409
            return jsonify({'error': str(e)}), 500
    _invalidate_admin_cache()
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
                UPDATE admin_accounts SET username=%s, password_hash=%s, password_plain=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, _hash_pw(password), password, b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
    _invalidate_admin_cache()
    return jsonify(_admin_row(row)) if row else ('', 404)

@app.route('/api/admin/accounts/<int:aid>', methods=['DELETE'])
@require_super
def api_admin_account_delete(aid):
    if aid == session.get('admin_id'):
        return jsonify({'error': '不能刪除自己的帳號'}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM admin_accounts WHERE id=%s", (aid,))
    _invalidate_admin_cache()
    return jsonify({'deleted': aid})

# ─── Shared Helpers ───────────────────────────────────────────────────────────

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
    d['has_password'] = bool(d.get('password_hash'))
    d.pop('password_hash', None)
    if 'password_plain' not in d: d['password_plain'] = ''
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
    if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
    return d

def punch_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['latitude', 'longitude']:
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('punched_at'):
        pa = d['punched_at']
        if hasattr(pa, 'astimezone'): pa = pa.astimezone(TW_TZ)
        d['punched_at'] = pa.isoformat()
    if d.get('created_at'):
        ca = d['created_at']
        if hasattr(ca, 'astimezone'): ca = ca.astimezone(TW_TZ)
        d['created_at'] = ca.isoformat()
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
    for f in ('requested_at', 'reviewed_at', 'created_at'):
        if d.get(f):
            v = d[f]
            if hasattr(v, 'astimezone'): v = v.astimezone(TW_TZ)
            d[f] = v.isoformat()
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
        except (ValueError, TypeError): d['dates'] = []
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
        cfg  = _get_cfg_cached(conn)
        locs = _get_locs_cached(conn)
    return jsonify({
        'gps_required': cfg['gps_required'] if cfg else False,
        'locations': [loc_row(r) for r in locs]
    })

def _fetch_today_log(conn, staff_id):
    """Query today's punch records within an existing DB connection."""
    now_tw = _dt.now(TW_TZ)
    today_start    = _dt(now_tw.year, now_tw.month, now_tw.day, tzinfo=TW_TZ)
    tomorrow_start = today_start + _td(days=1)
    rows = conn.execute("""
        SELECT pr.*, ps.name as staff_name
        FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
        WHERE pr.staff_id=%s
          AND pr.punched_at >= %s AND pr.punched_at < %s
        ORDER BY pr.punched_at ASC
    """, (staff_id, today_start, tomorrow_start)).fetchall()
    return [punch_record_row(r) for r in rows]

@app.route('/api/punch/init', methods=['GET'])
def api_punch_init():
    """Combined init: returns me (if logged in) + gps settings + today log in one request."""
    sid = session.get('punch_staff_id')
    with get_db() as conn:
        cfg  = _get_cfg_cached(conn)
        locs = _get_locs_cached(conn)
        staff     = None
        today_log = []
        if sid:
            staff = conn.execute(
                "SELECT id, name, role FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
            ).fetchone()
            if not staff:
                session.pop('punch_staff_id', None)
            else:
                today_log = _fetch_today_log(conn, sid)
    company_name = ''
    try:
        with get_db() as conn2:
            row = conn2.execute(
                "SELECT setting_value FROM finance_settings WHERE setting_key='company_name'"
            ).fetchone()
            if row: company_name = row['setting_value'] or ''
    except Exception:
        pass
    return jsonify({
        'me':          dict(staff) if staff else None,
        'gps_required': cfg['gps_required'] if cfg else False,
        'locations':   [loc_row(r) for r in locs],
        'today_log':   today_log,
        'company_name': company_name,
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
    _invalidate_cfg_cache()
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
    _invalidate_locs_cache()
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
    _invalidate_locs_cache()
    return jsonify(loc_row(row)) if row else ('', 404)

@app.route('/api/punch/locations/<int:lid>', methods=['DELETE'])
@login_required
def api_punch_locations_delete(lid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_locations WHERE id=%s", (lid,))
    _invalidate_locs_cache()
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
        cfg  = _get_cfg_cached(conn)
        locs = _get_locs_cached(conn)

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
    now_tw = _dt.now(TW_TZ)
    today_start = _dt(now_tw.year, now_tw.month, now_tw.day, tzinfo=TW_TZ)
    tomorrow_start = today_start + _td(days=1)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.*, ps.name as staff_name
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE pr.staff_id=%s
              AND pr.punched_at >= %s AND pr.punched_at < %s
            ORDER BY pr.punched_at ASC
        """, (sid, today_start, tomorrow_start)).fetchall()
    return jsonify([punch_record_row(r) for r in rows])

@app.route('/api/punch/my-records', methods=['GET'])
def api_punch_my_records():
    """Employee self-service: own punch records for a month."""
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    month = request.args.get('month', '')
    if not month:
        month = _dt.now(TW_TZ).strftime('%Y-%m')
    import calendar as _cal_mr
    _y_mr, _m_mr = int(month[:4]), int(month[5:])
    month_start = _dt(_y_mr, _m_mr, 1, tzinfo=TW_TZ)
    last_day    = _cal_mr.monthrange(_y_mr, _m_mr)[1]
    month_end   = _dt(_y_mr, _m_mr, last_day, tzinfo=TW_TZ) + _td(days=1)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND punched_at >= %s AND punched_at < %s
            ORDER BY punched_at ASC
        """, (sid, month_start, month_end)).fetchall()
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
        rows = conn.execute("SELECT * FROM punch_staff ORDER BY sort_order, name").fetchall()
    return jsonify([punch_staff_row(r) for r in rows])

@app.route('/api/punch/staff/reorder', methods=['POST'])
@login_required
def api_punch_staff_reorder():
    """儲存員工自訂排序順序"""
    ids = request.get_json(force=True)
    if not isinstance(ids, list):
        return jsonify({'error': 'expected list of ids'}), 400
    with get_db() as conn:
        for idx, sid in enumerate(ids):
            conn.execute("UPDATE punch_staff SET sort_order=%s WHERE id=%s", (idx, sid))
    return jsonify({'ok': True})

@app.route('/api/punch/staff', methods=['POST'])
@login_required
def api_punch_staff_create():
    b        = request.get_json(force=True)
    name     = b.get('name', '').strip()
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not name:     return jsonify({'error': '姓名為必填'}), 400
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password:
        return jsonify({'error': '密碼為必填'}), 400
    employee_code  = (b.get('employee_code') or '').strip() or None
    department     = (b.get('department') or '').strip()
    role           = (b.get('role') or '').strip()
    position_title = (b.get('position_title') or '').strip()
    hire_date      = b.get('hire_date') or None
    birth_date     = b.get('birth_date') or None
    national_id    = (b.get('national_id') or '').strip()
    gender         = (b.get('gender') or '').strip()
    address        = (b.get('address') or '').strip()
    bank_code      = (b.get('bank_code') or '').strip()
    bank_name      = (b.get('bank_name') or '').strip()
    bank_branch    = (b.get('bank_branch') or '').strip()
    bank_account   = (b.get('bank_account') or '').strip()
    account_holder = (b.get('account_holder') or '').strip()
    active         = bool(b.get('active', True))
    try:
        with get_db() as conn:
            row = conn.execute("""
                INSERT INTO punch_staff
                  (name, username, password_hash, password_plain, role, employee_code,
                   department, position_title, hire_date, birth_date,
                   national_id, gender, address,
                   bank_code, bank_name, bank_branch, bank_account, account_holder,
                   active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (name, username, _hash_pw(password), password, role, employee_code,
                  department, position_title, hire_date, birth_date,
                  national_id, gender, address,
                  bank_code, bank_name, bank_branch, bank_account, account_holder,
                  active)).fetchone()
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
    bank_code     = (b.get('bank_code') or '').strip()
    bank_name     = (b.get('bank_name') or '').strip()
    bank_branch   = (b.get('bank_branch') or '').strip()
    bank_account  = (b.get('bank_account') or '').strip()
    account_holder= (b.get('account_holder') or '').strip()
    department    = (b.get('department') or '').strip()
    position_title= (b.get('position_title') or '').strip()
    hire_date     = b.get('hire_date') or None
    birth_date    = b.get('birth_date') or None
    national_id   = (b.get('national_id') or '').strip()
    gender        = (b.get('gender') or '').strip()
    address       = (b.get('address') or '').strip()
    if not name or not username:
        return jsonify({'error': '姓名和帳號為必填'}), 400
    with get_db() as conn:
        if password:
            row = conn.execute("""
                UPDATE punch_staff
                SET name=%s,username=%s,password_hash=%s,password_plain=%s,role=%s,active=%s,employee_code=%s,
                    bank_code=%s,bank_name=%s,bank_branch=%s,bank_account=%s,account_holder=%s,
                    department=%s,position_title=%s,hire_date=%s,birth_date=%s,
                    national_id=%s,gender=%s,address=%s
                WHERE id=%s RETURNING *
            """, (name, username, _hash_pw(password), password, role, active, employee_code,
                  bank_code, bank_name, bank_branch, bank_account, account_holder,
                  department, position_title, hire_date, birth_date,
                  national_id, gender, address, sid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE punch_staff
                SET name=%s,username=%s,role=%s,active=%s,employee_code=%s,
                    bank_code=%s,bank_name=%s,bank_branch=%s,bank_account=%s,account_holder=%s,
                    department=%s,position_title=%s,hire_date=%s,birth_date=%s,
                    national_id=%s,gender=%s,address=%s
                WHERE id=%s RETURNING *
            """, (name, username, role, active, employee_code,
                  bank_code, bank_name, bank_branch, bank_account, account_holder,
                  department, position_title, hire_date, birth_date,
                  national_id, gender, address, sid)).fetchone()
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
        import calendar as _cal_pr
        _ypr, _mpr = int(month[:4]), int(month[5:])
        _pr_start = _dt(_ypr, _mpr, 1, tzinfo=TW_TZ)
        _pr_end   = _dt(_ypr, _mpr, _cal_pr.monthrange(_ypr, _mpr)[1], tzinfo=TW_TZ) + _td(days=1)
        conds.append("pr.punched_at >= %s AND pr.punched_at < %s")
        params.extend([_pr_start, _pr_end])
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
    b          = request.get_json(force=True)
    punch_type = b.get('punch_type')
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_records
            SET punch_type=%s, punched_at=%s, note=%s, is_manual=TRUE, manual_by=%s
            WHERE id=%s RETURNING *
        """, (punch_type, b.get('punched_at'),
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
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    _ps_now = _time.time()
    _ps_c = _punch_summary_cache.get(month)
    if _ps_c and _ps_now - _ps_c['at'] < _SUMMARY_TTL:
        return jsonify(_ps_c['data'])
    from datetime import date as _date2, timedelta as _td2, datetime as _dt2
    import calendar as _cal2
    _y2, _m2 = int(month[:4]), int(month[5:])
    # Range: TW 1st of month 00:00 → TW 1st of month+2 00:00 (include overflow day)
    _range_start = _dt2(_y2, _m2, 1, tzinfo=TW_TZ)
    _last2       = _cal2.monthrange(_y2, _m2)[1]
    _range_end   = _dt2(_y2, _m2, _last2, tzinfo=TW_TZ) + _td2(days=2)
    _next_date2  = (_date2(_y2, _m2, _last2) + _td2(days=1)).isoformat()

    with get_db() as conn:
        rows = conn.execute("""
            SELECT ps.id as staff_id, ps.name as staff_name,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_out,
                   COUNT(*) as punch_count,
                   BOOL_OR(pr.is_manual) as has_manual
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE pr.punched_at >= %s AND pr.punched_at < %s
            GROUP BY ps.id, ps.name, (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date ASC, ps.name
        """, (_range_start, _range_end)).fetchall()

    # Build per-(staff, date) dict
    day_map = {}
    for r in rows:
        key = (r['staff_id'], r['work_date'].isoformat())
        day_map[key] = dict(r)

    # Merge cross-day sessions:
    # (staff, D) has clock_in but no clock_out  +  (staff, D+1) has clock_out but no clock_in
    # → move D+1's clock_out into D and discard D+1
    merged = set()
    for (sid, ds), row in list(day_map.items()):
        if row['clock_in'] and not row['clock_out']:
            nds  = (_date2.fromisoformat(ds) + _td2(days=1)).isoformat()
            nkey = (sid, nds)
            if nkey in day_map and nkey not in merged:
                nrow = day_map[nkey]
                if nrow['clock_out'] and not nrow['clock_in']:
                    if (nrow['clock_out'] - row['clock_in']).total_seconds() <= 86400:
                        row['clock_out']    = nrow['clock_out']
                        row['punch_count'] += nrow['punch_count']
                        row['has_manual']   = row['has_manual'] or nrow['has_manual']
                        merged.add(nkey)

    result = []
    for (sid, ds), row in day_map.items():
        if (sid, ds) in merged or not ds.startswith(month):
            continue
        d = dict(row)
        d['work_date'] = ds
        d['clock_in']  = d['clock_in'].isoformat()  if d['clock_in']  else None
        d['clock_out'] = d['clock_out'].isoformat() if d['clock_out'] else None
        if d['clock_in'] and d['clock_out']:
            ci = _dt2.fromisoformat(d['clock_in'])
            co = _dt2.fromisoformat(d['clock_out'])
            d['duration_min'] = max(0, int((co - ci).total_seconds() / 60))
        else:
            d['duration_min'] = None
        result.append(d)

    result.sort(key=lambda x: (x['work_date'], x['staff_name']), reverse=True)
    _punch_summary_cache[month] = {'data': result, 'at': _ps_now}
    return jsonify(result)

@app.route('/api/attendance/monthly-stats', methods=['GET'])
@login_required
def api_attendance_monthly_stats():
    """
    月出勤統計報表（每位員工匯總）
    回傳：出勤天數、總工時、遲到次數、缺打卡次數、平均工時
    """
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    from datetime import date as _date3, timedelta as _td3, datetime as _dt3
    import calendar as _cal3
    _y3, _m3 = int(month[:4]), int(month[5:])
    _last3      = _cal3.monthrange(_y3, _m3)[1]
    _next_date3 = (_date3(_y3, _m3, _last3) + _td3(days=1)).isoformat()
    _range3_start = _dt3(_y3, _m3, 1, tzinfo=TW_TZ)
    _range3_end   = _dt3(_y3, _m3, _last3, tzinfo=TW_TZ) + _td3(days=2)

    with get_db() as conn:
        # 每人每日打卡彙整（多抓下個月第一天，供跨日班次合併）
        rows = conn.execute("""
            SELECT ps.id as staff_id, ps.name as staff_name,
                   ps.department, ps.role,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_out,
                   BOOL_OR(pr.punch_type='in')  as has_in,
                   BOOL_OR(pr.punch_type='out') as has_out
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id AND ps.active = TRUE
            WHERE pr.punched_at >= %s AND pr.punched_at < %s
            GROUP BY ps.id, ps.name, ps.department, ps.role,
                     (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY ps.name, work_date
        """, (_range3_start, _range3_end)).fetchall()

        # 班別指派（用於遲到判斷）
        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time
            FROM shift_assignments sa
            JOIN shift_types st ON st.id = sa.shift_type_id
            WHERE TO_CHAR(sa.shift_date,'YYYY-MM') = %s
        """, (month,)).fetchall()
        shift_map = {(r['staff_id'], str(r['shift_date'])): r for r in shift_rows}

    # 建立 (staff_id, date_str) → row_dict，並合併跨日班次
    day_map = {}
    for r in rows:
        key = (r['staff_id'], str(r['work_date']))
        day_map[key] = {
            'staff_id':   r['staff_id'],   'staff_name': r['staff_name'],
            'department': r['department'], 'role':       r['role'],
            'work_date':  str(r['work_date']),
            'clock_in':   r['clock_in'],   'clock_out':  r['clock_out'],
            'has_in':  bool(r['has_in']),  'has_out': bool(r['has_out']),
        }

    # 合併跨日：(staff, D) 有 in 無 out + (staff, D+1) 有 out 無 in → 合為同一班次
    merged = set()
    for (sid, ds), row in list(day_map.items()):
        if row['has_in'] and not row['has_out']:
            nds  = (_date3.fromisoformat(ds) + _td3(days=1)).isoformat()
            nkey = (sid, nds)
            if nkey in day_map and nkey not in merged:
                nrow = day_map[nkey]
                if nrow['has_out'] and not nrow['has_in']:
                    ci, co = row['clock_in'], nrow['clock_out']
                    if ci and co and (co - ci).total_seconds() <= 86400:
                        row['clock_out'] = co
                        row['has_out']   = True
                        merged.add(nkey)

    from collections import defaultdict
    stats = defaultdict(lambda: {
        'staff_id': None, 'staff_name': '', 'department': '', 'role': '',
        'days_worked': 0, 'total_minutes': 0,
        'late_count': 0, 'early_count': 0, 'missing_in_count': 0, 'missing_out_count': 0,
        'anomaly_dates': [],
    })

    for (sid, ds), row in day_map.items():
        if (sid, ds) in merged or not ds.startswith(month):
            continue

        s = stats[sid]
        s['staff_id']   = sid
        s['staff_name'] = row['staff_name']
        s['department'] = row['department'] or ''
        s['role']       = row['role']       or ''

        has_in  = row['has_in']
        has_out = row['has_out']

        if has_in or has_out:
            s['days_worked'] += 1

        if row['clock_in'] and row['clock_out']:
            diff = (row['clock_out'] - row['clock_in']).total_seconds() / 60
            if diff > 0:
                s['total_minutes'] += int(diff)

        # 缺打卡
        if has_in and not has_out:
            s['missing_out_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_out', 'label': '缺下班卡'})
        if not has_in and has_out:
            s['missing_in_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_in', 'label': '缺上班卡'})

        # 遲到（比對班別）
        if has_in and row['clock_in']:
            shift = shift_map.get((sid, ds))
            if shift and shift['start_time']:
                try:
                    sh, sm    = map(int, str(shift['start_time'])[:5].split(':'))
                    ci_local  = row['clock_in']
                    late_mins = (ci_local.hour * 60 + ci_local.minute) - (sh * 60 + sm)
                    if late_mins > 10:
                        s['late_count'] += 1
                        s['anomaly_dates'].append({'date': ds, 'type': 'late',
                                                   'label': f'遲到 {late_mins} 分鐘'})
                except Exception:
                    pass

        # 早退（比對班別）
        if has_out and row['clock_out']:
            shift = shift_map.get((sid, ds))
            if shift and shift['end_time']:
                try:
                    eh, em     = map(int, str(shift['end_time'])[:5].split(':'))
                    co_local   = row['clock_out']
                    early_mins = (eh * 60 + em) - (co_local.hour * 60 + co_local.minute)
                    if early_mins > 15:
                        s['early_count'] += 1
                        s['anomaly_dates'].append({'date': ds, 'type': 'early',
                                                   'label': f'早退 {early_mins} 分鐘'})
                except Exception:
                    pass

    result = []
    for s in sorted(stats.values(), key=lambda x: (x['department'], x['staff_name'])):
        h   = s['total_minutes'] // 60
        m   = s['total_minutes'] % 60
        avg = round(s['total_minutes'] / s['days_worked'] / 60, 1) if s['days_worked'] else 0
        s['total_hours']   = round(s['total_minutes'] / 60, 1)
        s['avg_hours_day'] = avg
        s['total_hm']      = f"{h}h {m:02d}m"
        result.append(s)
    return jsonify({'month': month, 'stats': result})

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
_line_conv_state      = {}   # {line_user_id: {'flow': str, 'step': int, 'data': {}}}


def get_line_punch_config():
    if not DATABASE_URL: return None
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# Thread-local storage for the current webhook event's reply token.
# When set, _send_line_punch / _push_line_msg will use reply_message (free)
# instead of push_message (consumes quota). The token is consumed on first use.
_line_reply_ctx = threading.local()


def _send_line_punch(user_id, text):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    token = getattr(_line_reply_ctx, 'reply_token', None)
    try:
        api = LineBotApi(cfg['channel_access_token'])
        if token:
            _line_reply_ctx.reply_token = None  # consume – reply_token is single-use
            api.reply_message(token, TextSendMessage(text=text))
        else:
            api.push_message(user_id, TextSendMessage(text=text))
    except Exception as e:
        print(f"[LINE PUNCH] send error: {e}")


def _push_line_msg(user_id, *messages):
    """Send one or more raw message dicts; uses reply API when inside a webhook event."""
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    token = getattr(_line_reply_ctx, 'reply_token', None)
    if token:
        _line_reply_ctx.reply_token = None  # consume
        body = _json.dumps({'replyToken': token, 'messages': list(messages)}).encode('utf-8')
        url  = 'https://api.line.me/v2/bot/message/reply'
    else:
        body = _json.dumps({'to': user_id, 'messages': list(messages)}).encode('utf-8')
        url  = 'https://api.line.me/v2/bot/message/push'
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {cfg["channel_access_token"]}'}
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f'[LINE] send error: {e}')


def _qr_pb(*items):
    """Build quickReply with postback actions. items=(label, data, displayText)."""
    return {'items': [
        {'type': 'action', 'action': {
            'type': 'postback', 'label': lbl[:20],
            'data': dat, 'displayText': disp
        }} for lbl, dat, disp in items
    ]}


def _flex_ask(title, color, question, hint=''):
    """Simple Flex bubble for a step in an interactive flow."""
    body_items = [{'type': 'text', 'text': question, 'wrap': True,
                   'size': 'sm', 'weight': 'bold'}]
    if hint:
        body_items.append({'type': 'text', 'text': hint, 'wrap': True,
                            'size': 'xs', 'color': '#888888', 'margin': 'sm'})
    return {
        'type': 'flex',
        'altText': question,
        'contents': {
            'type': 'bubble', 'size': 'kilo',
            'header': {
                'type': 'box', 'layout': 'vertical', 'paddingAll': '12px',
                'backgroundColor': color,
                'contents': [{'type': 'text', 'text': title, 'color': '#ffffff',
                               'weight': 'bold', 'size': 'sm'}]
            },
            'body': {
                'type': 'box', 'layout': 'vertical',
                'paddingAll': '14px', 'spacing': 'sm',
                'contents': body_items
            }
        }
    }


# ── Leave interactive flow ────────────────────────────────────────────

# Half-hour time slots 00:00–23:30 for leave start/end selection
_LV_TIMES = [f'{h:02d}:{m:02d}' for h in range(24) for m in (0, 30)]
_LV_TIME_PAGE = 10  # 10 items + prev/next + cancel stays within LINE's 13-item limit


def _lv_parse_time(s):
    """Parse 'HH:MM' (full-width colon accepted), return (h, m) or raise ValueError."""
    s = (s or '').strip().replace('：', ':')
    parts = s.split(':')
    if len(parts) != 2:
        raise ValueError
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m


def _line_lv_time_qr(user_id, title_text, prompt_text, val_prefix, pg_prefix, times, page=0):
    """Send a paginated quick-reply time picker (30-min slots)."""
    chunk = times[page * _LV_TIME_PAGE:(page + 1) * _LV_TIME_PAGE]
    has_prev = page > 0
    has_next = len(times) > (page + 1) * _LV_TIME_PAGE
    qr = [(t, f'{val_prefix}{t}', t) for t in chunk]
    if has_prev:
        qr.append(('⬅️ 上頁', f'{pg_prefix}{page - 1}', '上一頁'))
    if has_next:
        qr.append(('➡️ 下頁', f'{pg_prefix}{page + 1}', '下一頁'))
    qr.append(('❌ 取消', 'cancel', '取消'))
    msg = _flex_ask('📝 請假申請', '#27AE60', title_text, prompt_text)
    msg['quickReply'] = _qr_pb(*qr)
    _push_line_msg(user_id, msg)


def _line_leave_start(staff, user_id):
    with get_db() as conn:
        types = conn.execute(
            "SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
    if not types:
        _send_line_punch(user_id, '目前無可用假別，請聯絡管理員。')
        return
    _line_conv_state[user_id] = {'flow': 'leave', 'step': 1, 'data': {}, 'all_types': [t['name'] for t in types]}
    # Quick reply max 13 items; show first 12 + cancel; if more, paginate via postback
    _line_leave_send_types(user_id, [t['name'] for t in types], page=0)


def _line_leave_send_types(user_id, all_types, page=0):
    PAGE = 12
    chunk = all_types[page*PAGE:(page+1)*PAGE]
    has_next = len(all_types) > (page+1)*PAGE
    qr = [(t, f'lv_type={t}', f'假別：{t}') for t in chunk]
    if has_next:
        qr.append(('➡️ 更多', f'lv_page={page+1}', '查看更多假別'))
    qr.append(('❌ 取消', 'cancel', '取消'))
    msg = _flex_ask('📝 請假申請', '#27AE60',
                    f'請選擇假別（第{page+1}頁，共{len(all_types)}種）',
                    '點選下方按鈕')
    msg['quickReply'] = _qr_pb(*qr)
    _push_line_msg(user_id, msg)


def _handle_conv_leave(staff, user_id, state, text=None, pb_data=None):
    from datetime import date as _dl, timedelta as _tdl
    step = state['step']
    data = state['data']
    today = _dl.today().isoformat()
    tmrw  = (_dl.today() + _tdl(days=1)).isoformat()

    if pb_data == 'cancel' or text == '取消':
        _line_conv_state.pop(user_id, None)
        _send_line_punch(user_id, '已取消請假申請。')
        return

    # Pagination for leave types
    if pb_data and pb_data.startswith('lv_page='):
        try:
            page = int(pb_data[8:])
            _line_leave_send_types(user_id, state.get('all_types', []), page=page)
        except ValueError:
            pass
        return

    if step == 1:
        lv_type = (pb_data[8:] if pb_data and pb_data.startswith('lv_type=')
                   else (text.strip() if text else None))
        if not lv_type:
            return
        # Validate against known types
        known = state.get('all_types', [])
        if known and lv_type not in known:
            _send_line_punch(user_id, f'找不到假別「{lv_type}」，請點選按鈕選擇。')
            return
        data['leave_type'] = lv_type
        state['step'] = 2
        msg = _flex_ask('📝 請假申請', '#27AE60',
                        f'假別：{lv_type}\n\n請輸入開始日期',
                        '格式：YYYY-MM-DD，或點選快速選擇')
        msg['quickReply'] = _qr_pb(
            (f'今天 ({today})', f'lv_sd={today}', today),
            (f'明天 ({tmrw})',  f'lv_sd={tmrw}',  tmrw),
            ('❌ 取消', 'cancel', '取消'),
        )
        _push_line_msg(user_id, msg)

    elif step == 2:
        raw = (pb_data[6:] if pb_data and pb_data.startswith('lv_sd=')
               else (text.strip() if text else None))
        try:
            _dl.fromisoformat(raw)
        except Exception:
            _send_line_punch(user_id, f'日期格式錯誤，請輸入 YYYY-MM-DD，例：{today}')
            return
        data['start_date'] = raw
        state['step'] = 3
        msg = _flex_ask('📝 請假申請', '#27AE60',
                        f'開始日期：{raw}\n\n請輸入結束日期',
                        '單日假點「同一天」，多日請直接輸入')
        msg['quickReply'] = _qr_pb(
            ('同一天', f'lv_ed={raw}', raw),
            ('❌ 取消', 'cancel', '取消'),
        )
        _push_line_msg(user_id, msg)

    elif step == 3:
        raw = (pb_data[6:] if pb_data and pb_data.startswith('lv_ed=')
               else (text.strip() if text else None))
        try:
            _dl.fromisoformat(raw)
        except Exception:
            _send_line_punch(user_id, '日期格式錯誤，請輸入 YYYY-MM-DD')
            return
        if raw < data['start_date']:
            _send_line_punch(user_id, '⚠️ 結束日期不能早於開始日期')
            return
        data['end_date'] = raw
        state['step'] = 4
        date_range = data['start_date'] + (f' ～ {raw}' if raw != data['start_date'] else '')
        _line_lv_time_qr(user_id,
            f'假別：{data["leave_type"]}\n日期：{date_range}\n\n請選擇開始時間',
            '點選按鈕或輸入 HH:MM（如 09:00）',
            'lv_st=', 'lv_st_p=', _LV_TIMES, 0)

    elif step == 4:
        # Start-time picker (30-min slots)
        if pb_data and pb_data.startswith('lv_st_p='):
            try:
                page = int(pb_data[8:])
            except ValueError:
                page = 0
            date_range = data['start_date'] + (f' ～ {data["end_date"]}' if data['end_date'] != data['start_date'] else '')
            _line_lv_time_qr(user_id,
                f'假別：{data["leave_type"]}\n日期：{date_range}\n\n請選擇開始時間',
                '點選按鈕或輸入 HH:MM（如 09:30）',
                'lv_st=', 'lv_st_p=', _LV_TIMES, page)
            return
        raw = (pb_data[6:] if pb_data and pb_data.startswith('lv_st=')
               else (text.strip() if text else None))
        if not raw:
            return
        try:
            sh, sm = _lv_parse_time(raw)
        except ValueError:
            _send_line_punch(user_id, '時間格式錯誤，請輸入 HH:MM，例：09:00')
            return
        start_time = f'{sh:02d}:{sm:02d}'
        data['start_time'] = start_time
        state['step'] = 5
        # Only offer end times after the chosen start time
        st_mins = sh * 60 + sm
        et_times = [t for t in _LV_TIMES if int(t[:2]) * 60 + int(t[3:]) > st_mins]
        state['et_times'] = et_times
        date_range = data['start_date'] + (f' ～ {data["end_date"]}' if data['end_date'] != data['start_date'] else '')
        _line_lv_time_qr(user_id,
            f'假別：{data["leave_type"]}\n日期：{date_range}\n開始：{start_time}\n\n請選擇結束時間',
            '點選按鈕或輸入 HH:MM（如 18:00）',
            'lv_et=', 'lv_et_p=', et_times, 0)

    elif step == 5:
        # End-time picker (30-min slots)
        if pb_data and pb_data.startswith('lv_et_p='):
            try:
                page = int(pb_data[8:])
            except ValueError:
                page = 0
            et_times = state.get('et_times', _LV_TIMES)
            date_range = data['start_date'] + (f' ～ {data["end_date"]}' if data['end_date'] != data['start_date'] else '')
            _line_lv_time_qr(user_id,
                f'假別：{data["leave_type"]}\n日期：{date_range}\n開始：{data["start_time"]}\n\n請選擇結束時間',
                '點選按鈕或輸入 HH:MM（如 18:00）',
                'lv_et=', 'lv_et_p=', et_times, page)
            return
        raw = (pb_data[6:] if pb_data and pb_data.startswith('lv_et=')
               else (text.strip() if text else None))
        if not raw:
            return
        try:
            eh, em = _lv_parse_time(raw)
        except ValueError:
            _send_line_punch(user_id, '時間格式錯誤，請輸入 HH:MM，例：17:30')
            return
        sh, sm = _lv_parse_time(data['start_time'])
        if eh * 60 + em <= sh * 60 + sm:
            _send_line_punch(user_id, '⚠️ 結束時間必須晚於開始時間，請重新選擇。')
            return
        end_time = f'{eh:02d}:{em:02d}'
        data['end_time'] = end_time
        state['step'] = 6
        total_mins = (eh * 60 + em) - (sh * 60 + sm)
        hrs = total_mins / 60
        date_range = data['start_date'] + (f' ～ {data["end_date"]}' if data['end_date'] != data['start_date'] else '')
        msg = _flex_ask('📝 請假申請', '#27AE60',
                        f'假別：{data["leave_type"]}\n'
                        f'日期：{date_range}\n'
                        f'時間：{data["start_time"]} ～ {end_time}（{hrs:.1f} 小時）\n\n'
                        f'請輸入請假原因',
                        '或點「跳過」')
        msg['quickReply'] = _qr_pb(
            ('跳過', 'lv_skip_reason', '（未填原因）'),
            ('❌ 取消', 'cancel', '取消'),
        )
        _push_line_msg(user_id, msg)

    elif step == 6:
        reason = ('' if pb_data == 'lv_skip_reason'
                  else (text.strip() if text else None))
        if reason is None:
            return
        _line_conv_state.pop(user_id, None)
        _do_line_leave_submit(staff, user_id,
                              data['leave_type'], data['start_date'], data['end_date'],
                              data['start_time'], data['end_time'], reason)


def _do_line_leave_submit(staff, user_id, leave_type_name, start_date, end_date,
                           start_time, end_time, reason):
    """Insert leave request with time-based hours (hours ÷ 8 = days)."""
    from datetime import date as _dlv
    try:
        _dlv.fromisoformat(start_date); _dlv.fromisoformat(end_date)
    except ValueError:
        _send_line_punch(user_id, '日期格式錯誤。'); return

    with get_db() as conn:
        lt = conn.execute(
            "SELECT * FROM leave_types WHERE active=TRUE AND name=%s", (leave_type_name,)
        ).fetchone()
        if not lt:
            lt = conn.execute(
                "SELECT * FROM leave_types WHERE active=TRUE AND name ILIKE %s LIMIT 1",
                (f'%{leave_type_name}%',)
            ).fetchone()
        if not lt:
            avail = conn.execute("SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order").fetchall()
            _send_line_punch(user_id, f'找不到假別「{leave_type_name}」\n可用：{"、".join(r["name"] for r in avail)}')
            return

        days = _calc_leave_days(start_date, end_date, start_time=start_time, end_time=end_time)
        year = int(start_date[:4])
        bal = conn.execute("""
            SELECT total_days, used_days FROM leave_balances
            WHERE staff_id=%s AND leave_type_id=%s AND year=%s
        """, (staff['id'], lt['id'], year)).fetchone()

        remain = None
        if bal:
            remain = float(bal['total_days'] or 0) - float(bal['used_days'] or 0)
            if remain < days:
                _send_line_punch(user_id,
                    f'⚠️ {lt["name"]} 餘額不足\n剩餘 {remain:.1f} 天，申請 {days} 天\n\n請至員工系統調整後再申請。')
                return

        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date,
               lv_start_time, lv_end_time,
               total_days, reason, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',NOW()) RETURNING id
        """, (staff['id'], lt['id'], start_date, end_date,
              start_time, end_time, days, reason or '（LINE 請假）')).fetchone()

    bal_str = f'（剩餘 {remain:.1f} 天）' if remain is not None else ''
    _send_line_punch(user_id,
        f'✅ 請假申請已送出\n\n'
        f'假別：{lt["name"]} {bal_str}\n'
        f'日期：{start_date}' + (f' ～ {end_date}' if end_date != start_date else '') + '\n'
        f'時間：{start_time}～{end_time}\n'
        f'天數：{days:.1f} 天\n'
        + (f'原因：{reason}\n' if reason else '')
        + f'申請號：#{row["id"]}，等待管理員審核。')


# ── Overtime interactive flow ─────────────────────────────────────────

def _line_ot_start(staff, user_id):
    from datetime import date as _d, timedelta as _td
    today = _d.today().isoformat()
    tmrw  = (_d.today() + _td(days=1)).isoformat()
    _line_conv_state[user_id] = {'flow': 'ot', 'step': 1, 'data': {}}
    msg = _flex_ask('⏰ 加班申請', '#E67E22',
                    '請選擇加班日期',
                    '或直接輸入 YYYY-MM-DD')
    msg['quickReply'] = _qr_pb(
        (f'今天 ({today})', f'ot_date={today}', today),
        (f'明天 ({tmrw})',  f'ot_date={tmrw}',  tmrw),
        ('❌ 取消', 'cancel', '取消'),
    )
    _push_line_msg(user_id, msg)


def _handle_conv_ot(staff, user_id, state, text=None, pb_data=None):
    from datetime import date as _dot, datetime as _dtt
    step = state['step']
    data = state['data']
    today = _dot.today().isoformat()

    if pb_data == 'cancel' or text == '取消':
        _line_conv_state.pop(user_id, None)
        _send_line_punch(user_id, '已取消加班申請。')
        return

    def _parse_time(s):
        """Parse HH:MM, return (h, m) or raise ValueError."""
        s = (s or '').strip().replace('：', ':')
        h, m = s.split(':')
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError
        return h, m

    if step == 1:
        raw = (pb_data[8:] if pb_data and pb_data.startswith('ot_date=')
               else (text.strip() if text else None))
        try:
            _dot.fromisoformat(raw)
        except Exception:
            _send_line_punch(user_id, f'日期格式錯誤，請輸入 YYYY-MM-DD，例：{today}')
            return
        data['date'] = raw
        state['step'] = 2
        msg = _flex_ask('⏰ 加班申請', '#E67E22',
                        f'加班日期：{raw}\n\n請選擇或輸入開始時間',
                        '格式：HH:MM，例：18:00')
        msg['quickReply'] = _qr_pb(
            ('17:00', 'ot_st=17:00', '17:00'),
            ('18:00', 'ot_st=18:00', '18:00'),
            ('19:00', 'ot_st=19:00', '19:00'),
            ('20:00', 'ot_st=20:00', '20:00'),
            ('❌ 取消', 'cancel', '取消'),
        )
        _push_line_msg(user_id, msg)

    elif step == 2:
        raw = (pb_data[6:] if pb_data and pb_data.startswith('ot_st=')
               else (text.strip() if text else None))
        try:
            h, m = _parse_time(raw)
        except Exception:
            _send_line_punch(user_id, '請輸入有效時間，格式：HH:MM，例：18:00')
            return
        data['start_time'] = f'{h:02d}:{m:02d}'
        state['step'] = 3
        # Suggest end times based on start
        ends = [f'{(h+i)%24:02d}:{m:02d}' for i in range(1, 5)]
        qr = [(t, f'ot_et={t}', t) for t in ends] + [('❌ 取消', 'cancel', '取消')]
        msg = _flex_ask('⏰ 加班申請', '#E67E22',
                        f'加班日期：{data["date"]}\n開始：{data["start_time"]}\n\n請選擇或輸入結束時間',
                        '格式：HH:MM')
        msg['quickReply'] = _qr_pb(*qr)
        _push_line_msg(user_id, msg)

    elif step == 3:
        raw = (pb_data[6:] if pb_data and pb_data.startswith('ot_et=')
               else (text.strip() if text else None))
        try:
            eh, em = _parse_time(raw)
        except Exception:
            _send_line_punch(user_id, '請輸入有效時間，格式：HH:MM，例：21:00')
            return
        end_time = f'{eh:02d}:{em:02d}'
        sh, sm = _parse_time(data['start_time'])
        # Calculate hours (handle midnight crossing)
        minutes = (eh * 60 + em) - (sh * 60 + sm)
        if minutes <= 0: minutes += 24 * 60
        hrs = round(minutes / 60, 2)
        if hrs > 12:
            _send_line_punch(user_id, f'⚠️ 加班時數異常（{hrs}h），請重新確認時間。')
            return
        data['end_time'] = end_time
        data['hours'] = hrs
        state['step'] = 4
        msg = _flex_ask('⏰ 加班申請', '#E67E22',
                        f'加班日期：{data["date"]}\n'
                        f'時間：{data["start_time"]} ～ {end_time}\n'
                        f'時數：{hrs}h\n\n請輸入加班原因',
                        '或點「跳過」')
        msg['quickReply'] = _qr_pb(
            ('跳過', 'ot_skip_reason', '（未填原因）'),
            ('❌ 取消', 'cancel', '取消'),
        )
        _push_line_msg(user_id, msg)

    elif step == 4:
        reason = ('' if pb_data == 'ot_skip_reason'
                  else (text.strip() if text else None))
        if reason is None:
            return
        _line_conv_state.pop(user_id, None)
        d = data
        with get_db() as conn:
            row = conn.execute("""
                INSERT INTO overtime_requests
                  (staff_id, request_date, start_time, end_time, ot_hours, reason, status)
                VALUES (%s,%s,%s,%s,%s,%s,'pending') RETURNING id
            """, (staff['id'], d['date'], d['start_time'], d['end_time'],
                  d['hours'], reason or '（LINE 加班申請）')).fetchone()
        _send_line_punch(user_id,
            f'✅ 加班申請已送出\n\n'
            f'日期：{d["date"]}\n'
            f'時間：{d["start_time"]} ～ {d["end_time"]}\n'
            f'時數：{d["hours"]}h\n'
            + (f'原因：{reason}\n' if reason else '')
            + f'申請編號：#{row["id"]}\n\n'
            '請等候管理員審核，審核結果將通知您。')


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
    # Store reply token so _send_line_punch/_push_line_msg can use reply_message (free)
    _line_reply_ctx.reply_token = event.get('replyToken') or None

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

    # ── Postback events (from Quick Reply interactive flows) ──────────
    if evt_type == 'postback':
        pb_data = event.get('postback', {}).get('data', '')
        with get_db() as conn:
            staff = conn.execute(
                "SELECT * FROM punch_staff WHERE line_user_id=%s AND active=TRUE", (user_id,)
            ).fetchone()
        if not staff:
            return
        state = _line_conv_state.get(user_id)
        if state:
            if state['flow'] == 'leave':
                _handle_conv_leave(staff, user_id, state, pb_data=pb_data)
            elif state['flow'] == 'ot':
                _handle_conv_ot(staff, user_id, state, pb_data=pb_data)
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
    # Merge custom rich-menu button texts for punch positions (0=in, 1=out) only
    try:
        _rm_cfg = get_line_punch_config()
        _rm_texts = _rm_cfg.get('richmenu_area_texts') if _rm_cfg else None
        if _rm_texts:
            _rm_list = _json.loads(_rm_texts) if isinstance(_rm_texts, str) else _rm_texts
            for _i, _pt in enumerate(['in', 'out']):
                _t = _rm_list[_i].strip() if _i < len(_rm_list) and _rm_list[_i] else ''
                if _t:
                    PUNCH_CMDS[_t] = _pt
    except Exception:
        pass
    PUNCH_LABEL = {
        'in': '上班打卡', 'out': '下班打卡',
        'break_out': '休息開始', 'break_in': '休息結束',
    }

    if msg_type == 'location':
        lat = msg.get('latitude'); lng = msg.get('longitude')
        _do_line_punch(staff, user_id, lat, lng, None, PUNCH_LABEL)

    elif msg_type == 'text':
        text = msg.get('text', '').strip()

        # ── Active conversation state machine ──────────────────────
        _cs = _line_conv_state.get(user_id)
        if _cs:
            if _cs['flow'] == 'leave':
                _handle_conv_leave(staff, user_id, _cs, text=text)
            elif _cs['flow'] == 'ot':
                _handle_conv_ot(staff, user_id, _cs, text=text)
            return

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
                msg = _flex_ask('📍 需要位置驗證', '#2980B9',
                                f'請傳送您的位置來完成{PUNCH_LABEL[punch_type]}',
                                '點下方「傳送位置」按鈕即可打卡')
                msg['quickReply'] = {'items': [
                    {'type': 'action', 'action': {'type': 'location', 'label': '📍 傳送位置'}}
                ]}
                _push_line_msg(user_id, msg)
                _pending_line_punches[user_id] = punch_type
            else:
                _do_line_punch(staff, user_id, None, None, punch_type, PUNCH_LABEL)
        elif text in ('查餘假', '餘假', '假期', '查假', '特休'):
            _line_query_leave_balance(staff, user_id)
        elif text in ('查薪資', '薪資', '薪水', '薪資單', '查薪水'):
            _line_query_salary(staff, user_id)
        elif text == '請假':
            _line_leave_start(staff, user_id)
        elif text.startswith('請假 '):
            _line_submit_leave(staff, user_id, text)   # direct format still works
        elif text in ('績效', '考核', '我的考核', '查績效'):
            _line_query_performance(staff, user_id)
        elif text in ('假別', '假別清單', '假別列表'):
            _line_show_leave_types(staff, user_id)
        elif (text in ('出勤紀錄', '出勤記錄', '月出勤', '打卡紀錄', '打卡記錄', '出勤查詢')
              or text.startswith('出勤紀錄 ') or text.startswith('出勤記錄 ')
              or text.startswith('打卡紀錄 ') or text.startswith('打卡記錄 ')):
            _line_query_monthly_records(staff, user_id, text)
        elif text == '加班':
            _line_ot_start(staff, user_id)
        elif text.startswith('加班 ') or text.startswith('申請加班'):
            _line_submit_overtime(staff, user_id, text)  # direct format still works
        elif text in ('選單', '功能', '菜單', '?', '？', 'help', 'Help', 'HELP'):
            _line_show_help(staff, user_id)
        else:
            _line_show_help(staff, user_id)


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
                SELECT punch_type, punched_at FROM punch_records
                WHERE staff_id=%s
                  AND punched_at > NOW() - INTERVAL '24 hours'
                ORDER BY punched_at DESC LIMIT 1
            """, (staff['id'],)).fetchone()
        if not last:                               punch_type = 'in'
        elif last['punch_type'] == 'in':           punch_type = 'out'
        elif last['punch_type'] == 'break_out':    punch_type = 'break_in'
        elif last['punch_type'] == 'out':
            # 下班後若在 30 分鐘內再打卡，視為誤觸，拒絕並提示
            from datetime import datetime as _dtnow, timezone as _tznow, timedelta as _tdnow
            _TW_now = _tznow(_tdnow(hours=8))
            _last_at = last.get('punched_at')
            if _last_at is not None:
                if _last_at.tzinfo is None:
                    _last_at = _last_at.replace(tzinfo=_tznow.utc)
                _mins_since = (_dtnow.now(_TW_now) - _last_at.astimezone(_TW_now)).total_seconds() / 60
                if _mins_since < 30:
                    _send_line_punch(user_id,
                        f'⚠️ 您已於 {int(_mins_since)} 分鐘前下班打卡，\n'
                        '請確認是否要重新上班打卡？\n\n'
                        '若要繼續，請再次點選「上班」。')
                    _pending_line_punches[user_id] = 'in'
                    return
            punch_type = 'in'
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
    enabled = bool(b.get('enabled', False))
    token   = b.get('channel_access_token')   # None = not provided (don't overwrite)
    secret  = b.get('channel_secret')
    with get_db() as conn:
        if token is not None or secret is not None:
            # 有帶入 token/secret → 一起更新
            conn.execute("""
                UPDATE line_punch_config
                SET channel_access_token=%s, channel_secret=%s, enabled=%s, updated_at=NOW()
                WHERE id=1
            """, (
                token.strip() if token else '',
                secret.strip() if secret else '',
                enabled,
            ))
        else:
            # 僅更新啟用狀態，保留原有 token/secret
            conn.execute("""
                UPDATE line_punch_config SET enabled=%s, updated_at=NOW() WHERE id=1
            """, (enabled,))
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


def _gdrive_download(url):
    """Download an image from a Google Drive sharing URL. Returns bytes or None."""
    import re
    # Extract file ID from various Drive URL formats
    m = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if not m:
        m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if not m:
        return None
    file_id = m.group(1)
    download_url = f'https://drive.google.com/uc?export=download&id={file_id}'
    req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        # If Google returns an HTML confirmation page (large file), try the confirm URL
        if data[:5] in (b'<!DOC', b'<html', b'<!doc'):
            import re as _re
            confirm = _re.search(rb'confirm=([0-9A-Za-z_-]+)', data)
            if confirm:
                confirm_url = f'https://drive.google.com/uc?export=download&confirm={confirm.group(1).decode()}&id={file_id}'
                req2 = urllib.request.Request(confirm_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req2, timeout=30) as resp2:
                    data = resp2.read()
            else:
                return None
        # Validate it's a PNG or JPEG
        if data[:8] == b'\x89PNG\r\n\x1a\n' or data[:2] == b'\xff\xd8':
            return data
        return None
    except Exception:
        return None


def _make_richmenu_png():
    """Generate a simple 2500×1686 PNG with 4 colored quadrants (no external deps)."""
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

    body_json = request.get_json(silent=True) or {}
    gdrive_url = (body_json.get('gdrive_url') or '').strip()
    raw_texts  = body_json.get('area_texts') or []
    # Expect [top-left, top-right, bottom-left, bottom-right]
    defaults   = ['上班', '下班', '請假', '加班']
    area_texts = [(raw_texts[i].strip() if i < len(raw_texts) and raw_texts[i].strip() else defaults[i])
                  for i in range(4)]

    # Save custom area texts to config so webhook can use them
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE line_punch_config SET richmenu_area_texts=%s WHERE id=1",
                (_json.dumps(area_texts),)
            )
    except Exception:
        pass

    bounds = [
        {"x": 0,    "y": 0,   "width": 1250, "height": 843},
        {"x": 1250, "y": 0,   "width": 1250, "height": 843},
        {"x": 0,    "y": 843, "width": 1250, "height": 843},
        {"x": 1250, "y": 843, "width": 1250, "height": 843},
    ]
    body = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "Punch Menu",
        "chatBarText": "Punch",
        "areas": [
            {"bounds": bounds[i], "action": {"type": "message", "text": area_texts[i]}}
            for i in range(4)
        ]
    }

    status, data = _call_line_api(cfg, 'POST', '/richmenu', body)
    if status != 200:
        return jsonify({'error': f'建立失敗 ({status}): {data.get("error","")}'}), 500

    rich_menu_id = data.get('richMenuId', '')

    # Upload image — priority: Google Drive URL > local custom file > auto-generate
    png_bytes = None
    if gdrive_url:
        try:
            png_bytes = _gdrive_download(gdrive_url)
        except Exception:
            pass
    if not png_bytes:
        try:
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
        _badges_cache.clear()
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
    _badges_cache.clear()
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
            except (ValueError, TypeError): dates_val = []
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
    now = time.time()
    if _shift_types_all_cache['data'] is not None and now - _shift_types_all_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_shift_types_all_cache['data'])
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM shift_types ORDER BY sort_order, id").fetchall()
    result = [shift_type_row(r) for r in rows]
    _shift_types_all_cache['data'] = result; _shift_types_all_cache['at'] = now
    return jsonify(result)

@app.route('/api/shifts/types/public', methods=['GET'])
def api_shift_types_public():
    """Public endpoint for employee page."""
    now = time.time()
    if _shift_types_pub_cache['data'] is not None and now - _shift_types_pub_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_shift_types_pub_cache['data'])
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM shift_types WHERE active=TRUE ORDER BY sort_order, id"
        ).fetchall()
    result = [shift_type_row(r) for r in rows]
    _shift_types_pub_cache['data'] = result; _shift_types_pub_cache['at'] = now
    return jsonify(result)

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
    _shift_types_all_cache['data'] = None; _shift_types_pub_cache['data'] = None
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
    _shift_types_all_cache['data'] = None; _shift_types_pub_cache['data'] = None
    return jsonify(shift_type_row(row)) if row else ('', 404)

@app.route('/api/shifts/types/<int:sid>', methods=['DELETE'])
@require_module('sched')
def api_shift_type_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM shift_types WHERE id=%s", (sid,))
    _shift_types_all_cache['data'] = None; _shift_types_pub_cache['data'] = None
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
                    rows_sr = conn.execute("""
                        SELECT dates FROM schedule_requests
                        WHERE staff_id=%s AND month=%s AND status IN ('approved','pending')
                    """, (sid, month)).fetchall()
                    for row in rows_sr:
                        row_dates = row['dates'] or []
                        if isinstance(row_dates, str):
                            try: row_dates = _json.loads(row_dates)
                            except (ValueError, TypeError): row_dates = []
                        if sid not in leave_lookup:
                            leave_lookup[sid] = set()
                        leave_lookup[sid].update(row_dates)

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

    # Notify each assigned staff via LINE
    if created > 0:
        with get_db() as conn:
            shift_info = conn.execute(
                "SELECT name, start_time, end_time FROM shift_types WHERE id=%s", (shift_type_id,)
            ).fetchone()
        if shift_info:
            date_range = f"{min(dates)} ~ {max(dates)}" if len(dates) > 1 else dates[0]
            msg = (f"[排班通知] 已為您安排班別\n"
                   f"班別：{shift_info['name']}（{str(shift_info['start_time'])[:5]}～{str(shift_info['end_time'])[:5]}）\n"
                   f"日期：{date_range}\n"
                   f"共 {len(dates)} 天，請至員工系統查看完整排班。")
            for sid in staff_ids:
                _notify_staff_line(sid, msg)

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
    匯入班表 CSV 或 Excel (.xlsx)。
    表頭（第一列）：姓名,日期,班別,備註  或  代碼,日期,班別,備註
    日期格式：YYYY-MM-DD
    force=1 query param 可強制覆蓋排休衝突。
    """
    import csv, io as _io
    force = request.args.get('force', '0') == '1'
    rows = []

    if 'file' in request.files:
        f = request.files['file']
        fname = (f.filename or '').lower()
        if fname.endswith('.xlsx') or fname.endswith('.xls'):
            # ── Excel 解析 ────────────────────────────────────
            import openpyxl as _opx
            wb = _opx.load_workbook(_io.BytesIO(f.read()), read_only=True, data_only=True)
            ws = wb.active
            all_rows = list(ws.values)
            if not all_rows:
                return jsonify({'error': '檔案內容為空'}), 400
            headers = [str(h).strip() if h is not None else '' for h in all_rows[0]]
            for row in all_rows[1:]:
                if all(v is None or str(v).strip() == '' for v in row):
                    continue  # skip blank rows
                d = {}
                for i, h in enumerate(headers):
                    d[h] = str(row[i]).strip() if i < len(row) and row[i] is not None else ''
                rows.append(d)
        else:
            raw = f.read().decode('utf-8-sig')
            if not raw.strip():
                return jsonify({'error': '檔案內容為空'}), 400
            reader = csv.DictReader(_io.StringIO(raw))
            if reader.fieldnames is None:
                return jsonify({'error': '無法解析 CSV 欄位'}), 400
            reader.fieldnames = [h.strip() for h in reader.fieldnames]
            rows = list(reader)
    else:
        raw = request.get_data(as_text=True)
        if not raw.strip():
            return jsonify({'error': '檔案內容為空'}), 400
        reader = csv.DictReader(_io.StringIO(raw))
        if reader.fieldnames is None:
            return jsonify({'error': '無法解析 CSV 欄位'}), 400
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        rows = list(reader)

    if not rows:
        return jsonify({'error': '無資料列'}), 400

    # 確認必要欄位
    all_keys = rows[0].keys() if rows else []
    has_name = '姓名' in all_keys
    has_code = '代碼' in all_keys
    if not (has_name or has_code):
        return jsonify({'error': '檔案缺少「姓名」或「代碼」欄位'}), 400
    if '日期' not in all_keys:
        return jsonify({'error': '檔案缺少「日期」欄位'}), 400
    if '班別' not in all_keys:
        return jsonify({'error': '檔案缺少「班別」欄位'}), 400
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

        # 預先讀取所有涉及員工的核准/待審排休（僅在非強制時）
        leave_lookup = {}   # { staff_id: set(date_str) }
        if not force:
            leave_rows = conn.execute("""
                SELECT staff_id, dates FROM schedule_requests
                WHERE status IN ('approved','pending')
            """).fetchall()
            for lr in leave_rows:
                sid = lr['staff_id']
                dates_val = lr['dates']
                if isinstance(dates_val, str):
                    try: dates_val = _json.loads(dates_val)
                    except (ValueError, TypeError): dates_val = []
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


@app.route('/api/shifts/conflicts', methods=['GET'])
@require_module('sched')
def api_shift_conflicts():
    """
    偵測班表衝突與警示：
    - overtime_hours : 單班時數 > 10 小時
    - midnight_cross : 跨日班別（結束時間 < 開始時間）
    - consecutive_days : 連續排班 >= 6 天（6天警告，7天以上錯誤）
    """
    month = request.args.get('month', '')
    if not month:
        return jsonify({'error': '請指定月份'}), 400

    from datetime import date as _dc, timedelta as _tdc

    conflicts = []

    with get_db() as conn:
        rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date,
                   ps.name  AS staff_name,
                   st.name  AS shift_name,
                   st.start_time, st.end_time
            FROM shift_assignments sa
            JOIN punch_staff  ps ON ps.id = sa.staff_id
            JOIN shift_types  st ON st.id = sa.shift_type_id
            WHERE TO_CHAR(sa.shift_date, 'YYYY-MM') = %s
            ORDER BY sa.staff_id, sa.shift_date
        """, (month,)).fetchall()

    # ── 每班時數 & 跨日 ────────────────────────────────────────────
    for r in rows:
        s = r['start_time'];  e = r['end_time']
        sm = s.hour * 60 + s.minute
        em = e.hour * 60 + e.minute
        cross = em < sm
        dur   = ((24 * 60 - sm) + em) if cross else (em - sm)
        hrs   = dur / 60

        if cross:
            conflicts.append({
                'type':       'midnight_cross',
                'severity':   'info',
                'date':       str(r['shift_date']),
                'staff_name': r['staff_name'],
                'shift_name': r['shift_name'],
                'message':    f"跨日班別 {str(s)[:5]}～{str(e)[:5]}（共 {hrs:.1f} 小時）",
            })

        if hrs > 10:
            conflicts.append({
                'type':       'overtime_hours',
                'severity':   'warning' if hrs <= 12 else 'error',
                'date':       str(r['shift_date']),
                'staff_name': r['staff_name'],
                'shift_name': r['shift_name'],
                'message':    f"單班 {hrs:.1f} 小時，超過 10 小時上限",
            })

    # ── 連續排班天數 ───────────────────────────────────────────────
    staff_dates = {}
    for r in rows:
        sid = r['staff_id']
        if sid not in staff_dates:
            staff_dates[sid] = {'name': r['staff_name'], 'dates': []}
        staff_dates[sid]['dates'].append(_dc.fromisoformat(str(r['shift_date'])))

    for sid, info in staff_dates.items():
        dates = sorted(set(info['dates']))
        streak = [dates[0]]
        for i in range(1, len(dates)):
            if (dates[i] - dates[i-1]).days == 1:
                streak.append(dates[i])
            else:
                # evaluate finished streak
                if len(streak) >= 6:
                    sev = 'error' if len(streak) >= 7 else 'warning'
                    conflicts.append({
                        'type':       'consecutive_days',
                        'severity':   sev,
                        'date':       streak[0].isoformat(),
                        'staff_name': info['name'],
                        'shift_name': '',
                        'message':    (
                            f"連續排班 {len(streak)} 天"
                            f"（{streak[0].isoformat()} ～ {streak[-1].isoformat()}）"
                            + ('，違反勞基法每 7 日至少休 1 日' if len(streak) >= 7 else '，接近法定上限')
                        ),
                    })
                streak = [dates[i]]
        # last streak
        if len(streak) >= 6:
            sev = 'error' if len(streak) >= 7 else 'warning'
            conflicts.append({
                'type':       'consecutive_days',
                'severity':   sev,
                'date':       streak[0].isoformat(),
                'staff_name': info['name'],
                'shift_name': '',
                'message':    (
                    f"連續排班 {len(streak)} 天"
                    f"（{streak[0].isoformat()} ～ {streak[-1].isoformat()}）"
                    + ('，違反勞基法每 7 日至少休 1 日' if len(streak) >= 7 else '，接近法定上限')
                ),
            })

    # sort: error first, then by date
    sev_order = {'error': 0, 'warning': 1, 'info': 2}
    conflicts.sort(key=lambda c: (sev_order.get(c['severity'], 9), c['date']))
    return jsonify({'month': month, 'count': len(conflicts), 'conflicts': conflicts})


@app.route('/api/shifts/export', methods=['GET'])
@require_module('sched')
def api_shift_export():
    """匯出指定月份班表為 Excel (.xlsx)"""
    month = request.args.get('month', '')
    if not month:
        return jsonify({'error': '請指定月份'}), 400

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side, GradientFill
    from openpyxl.utils import get_column_letter
    from io import BytesIO
    import calendar as _cal2
    from datetime import date as _de

    y, mo = int(month[:4]), int(month[5:7])
    days_in_month = _cal2.monthrange(y, mo)[1]
    DAYS_CN = ['一','二','三','四','五','六','日']

    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT id, name, employee_code, role FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        assigns = conn.execute("""
            SELECT sa.staff_id, sa.shift_date, sa.note,
                   st.name AS shift_name, st.start_time, st.end_time
            FROM shift_assignments sa
            JOIN shift_types st ON st.id = sa.shift_type_id
            WHERE TO_CHAR(sa.shift_date, 'YYYY-MM') = %s
        """, (month,)).fetchall()
        holidays = {str(r['date']) for r in conn.execute(
            "SELECT date FROM public_holidays WHERE TO_CHAR(date,'YYYY-MM')=%s", (month,)
        ).fetchall()}

    lookup = {}
    for a in assigns:
        key = (a['staff_id'], str(a['shift_date']))
        lookup[key] = f"{a['shift_name']}\n{str(a['start_time'])[:5]}~{str(a['end_time'])[:5]}"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{month} 班表"

    # ── 樣式定義 ─────────────────────────────────────────────
    navy_fill   = PatternFill('solid', fgColor='0F1C3A')
    grey_fill   = PatternFill('solid', fgColor='F4F6FA')
    wkend_fill  = PatternFill('solid', fgColor='FFF5F5')
    hol_fill    = PatternFill('solid', fgColor='FFF0F0')
    thin_border = Border(
        left=Side(style='thin', color='DDDDDD'),
        right=Side(style='thin', color='DDDDDD'),
        top=Side(style='thin', color='DDDDDD'),
        bottom=Side(style='thin', color='DDDDDD'),
    )
    hdr_font  = Font(bold=True, color='FFFFFF', size=10)
    info_font = Font(bold=True, size=10)
    cell_font = Font(size=9)
    center    = Alignment(horizontal='center', vertical='center', wrap_text=True)

    # ── 標題列 ───────────────────────────────────────────────
    ws.row_dimensions[1].height = 36
    for col, label in enumerate(['姓名', '代碼', '職稱'], start=1):
        c = ws.cell(1, col, label)
        c.font = hdr_font; c.fill = navy_fill; c.alignment = center; c.border = thin_border
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 9
    ws.column_dimensions['C'].width = 9

    for d in range(1, days_in_month + 1):
        col  = d + 3
        dt   = _de(y, mo, d)
        wd   = dt.weekday()    # 0=Mon … 6=Sun
        ds   = f"{month}-{d:02d}"
        is_wkend = wd >= 5      # Sat or Sun
        is_hol   = ds in holidays
        c = ws.cell(1, col, f"{d}\n{DAYS_CN[wd]}")
        c.font      = Font(bold=True, color='FF4444' if is_wkend or is_hol else 'FFFFFF', size=9)
        c.fill      = PatternFill('solid', fgColor='1A3060') if not (is_wkend or is_hol) else PatternFill('solid', fgColor='8B2020')
        c.alignment = center
        c.border    = thin_border
        ws.column_dimensions[get_column_letter(col)].width = 11

    # ── 員工列 ───────────────────────────────────────────────
    for row_idx, staff in enumerate(staff_list, start=2):
        ws.row_dimensions[row_idx].height = 30
        for col, val in enumerate([staff['name'], staff['employee_code'] or '', staff['role'] or ''], start=1):
            c = ws.cell(row_idx, col, val)
            c.font = info_font if col == 1 else cell_font
            c.fill = grey_fill; c.alignment = center; c.border = thin_border

        for d in range(1, days_in_month + 1):
            col = d + 3
            ds  = f"{month}-{d:02d}"
            dt  = _de(y, mo, d); wd = dt.weekday()
            val = lookup.get((staff['id'], ds), '')
            c   = ws.cell(row_idx, col, val)
            c.font      = Font(size=8, color='1A1A2E' if val else 'CCCCCC')
            c.alignment = center
            c.border    = thin_border
            if not val:
                c.fill = wkend_fill if wd >= 5 else (hol_fill if ds in holidays else PatternFill('solid', fgColor='FFFFFF'))

    buf = BytesIO()
    wb.save(buf); buf.seek(0)
    from flask import send_file
    return send_file(
        buf, as_attachment=True,
        download_name=f"班表_{month}.xlsx",
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


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
    time_str = (f"{row['start_time']}～{row['end_time']}" if row.get('start_time') and row.get('end_time')
                else f"{float(row['ot_hours'])} 小時")
    extra = f"{row['request_date']} {time_str}"
    if action == 'approve' and float(row.get('ot_pay') or 0) > 0:
        extra += f"\n加班費：${float(row['ot_pay']):,.0f}"
    if review_note: extra += f"\n審核意見：{review_note}"
    _notify_review_result(req['staff_id'], '加班申請', action, extra)
    _badges_cache.clear()
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
    month = request.args.get('month', '') or _dt.now(TW_TZ).strftime('%Y-%m')
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
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS attachment BYTEA",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS attachment_name TEXT DEFAULT ''",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS attachment_type TEXT DEFAULT ''",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS lv_start_time TIME",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS lv_end_time TIME",
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
    if d.get('lv_start_time'): d['lv_start_time'] = str(d['lv_start_time'])[:5]
    if d.get('lv_end_time'):   d['lv_end_time']   = str(d['lv_end_time'])[:5]
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    d['has_attachment'] = bool(d.get('attachment'))
    d.pop('attachment', None)  # don't send binary over JSON
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

def _calc_leave_days(start_date_str, end_date_str, start_half=False, end_half=False,
                     start_time=None, end_time=None):
    """計算請假天數（排除週日）。
    若提供 start_time/end_time（HH:MM），以 每日小時數 ÷ 8 計算，四捨五入至 0.5。
    否則沿用 start_half/end_half 半天旗標邏輯。
    """
    from datetime import date as _date, timedelta as _tdd
    try:
        s = _date.fromisoformat(start_date_str)
        e = _date.fromisoformat(end_date_str)
    except Exception:
        return 0.0
    if e < s: return 0.0

    # Time-based: hours ÷ 8 per working day
    if start_time and end_time:
        try:
            sh, sm = _lv_parse_time(start_time)
            eh, em = _lv_parse_time(end_time)
            daily_hours = (eh * 60 + em - sh * 60 - sm) / 60.0
        except (ValueError, Exception):
            daily_hours = 0.0
        if daily_hours > 0:
            working_days = sum(
                1 for i in range((e - s).days + 1)
                if (s + _tdd(days=i)).weekday() != 6
            )
            raw = working_days * daily_hours / 8.0
            return max(0.5, round(raw * 2) / 2)  # round to nearest 0.5, min 0.5

    # Half-day flag logic
    days = 0.0
    cur  = s
    while cur <= e:
        if cur.weekday() != 6:
            if cur == s and cur == e:
                if start_half and end_half: days += 1.0
                elif start_half or end_half: days += 0.5
                else: days += 1.0
            elif cur == s and start_half: days += 0.5
            elif cur == e and end_half:   days += 0.5
            else: days += 1.0
        cur += _tdd(days=1)
    return days

# ── Leave Type CRUD ──────────────────────────────────────────────

@app.route('/api/leave/types', methods=['GET'])
@require_module('leave')
def api_leave_types_list():
    now = time.time()
    if _leave_types_all_cache['data'] is not None and now - _leave_types_all_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_leave_types_all_cache['data'])
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types ORDER BY sort_order, id").fetchall()
    result = [leave_type_row(r) for r in rows]
    _leave_types_all_cache['data'] = result; _leave_types_all_cache['at'] = now
    return jsonify(result)

@app.route('/api/leave/types/public', methods=['GET'])
def api_leave_types_public():
    now = time.time()
    if _leave_types_pub_cache['data'] is not None and now - _leave_types_pub_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_leave_types_pub_cache['data'])
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types WHERE active=TRUE ORDER BY sort_order, id").fetchall()
    result = [leave_type_row(r) for r in rows]
    _leave_types_pub_cache['data'] = result; _leave_types_pub_cache['at'] = now
    return jsonify(result)

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
    _leave_types_all_cache['data'] = None; _leave_types_pub_cache['data'] = None
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
    _leave_types_all_cache['data'] = None; _leave_types_pub_cache['data'] = None
    return jsonify(leave_type_row(row)) if row else ('', 404)

@app.route('/api/leave/types/<int:tid>', methods=['DELETE'])
@require_module('leave')
def api_leave_type_delete(tid):
    with get_db() as conn:
        conn.execute("DELETE FROM leave_types WHERE id=%s", (tid,))
    _leave_types_all_cache['data'] = None; _leave_types_pub_cache['data'] = None
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
    lv_start_time = (b.get('lv_start_time') or '').strip() or None
    lv_end_time   = (b.get('lv_end_time')   or '').strip() or None
    reason        = b.get('reason', '').strip()
    status        = b.get('status', 'approved')

    if not all([sid, leave_type_id, start_date, end_date]):
        return jsonify({'error': '缺少必要欄位'}), 400

    total_days = _calc_leave_days(start_date, end_date,
                                  start_time=lv_start_time, end_time=lv_end_time)
    if total_days <= 0:
        return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date,
               lv_start_time, lv_end_time,
               total_days, reason, status, reviewed_by, reviewed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              CASE WHEN %s='approved' THEN NOW() ELSE NULL END)
            RETURNING *
        """, (sid, leave_type_id, start_date, end_date,
              lv_start_time, lv_end_time,
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
    _badges_cache.clear()
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
    lv_start_time = (b.get('lv_start_time') or '').strip() or None
    lv_end_time   = (b.get('lv_end_time')   or '').strip() or None
    reason        = b.get('reason', '').strip()
    substitute    = b.get('substitute_name', '').strip()

    if not all([leave_type_id, start_date, end_date]):
        return jsonify({'error': '缺少必要欄位'}), 400

    total_days = _calc_leave_days(start_date, end_date,
                                  start_time=lv_start_time, end_time=lv_end_time)
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
              (staff_id, leave_type_id, start_date, end_date,
               lv_start_time, lv_end_time,
               total_days, reason, substitute_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, leave_type_id, start_date, end_date,
              lv_start_time, lv_end_time,
              total_days, reason, substitute)).fetchone()
    return jsonify(leave_req_row(row)), 201

@app.route('/api/leave/my-requests/<int:rid>/attachment', methods=['POST'])
def api_leave_attachment_upload(rid):
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    file = request.files.get('file')
    if not file: return jsonify({'error': '請選擇檔案'}), 400
    raw = file.read()
    if len(raw) > 10 * 1024 * 1024:
        return jsonify({'error': '檔案大小不能超過 10MB'}), 413
    filename   = file.filename or 'attachment'
    media_type = file.content_type or 'application/octet-stream'
    with get_db() as conn:
        req = conn.execute("SELECT id FROM leave_requests WHERE id=%s AND staff_id=%s", (rid, sid)).fetchone()
        if not req: return jsonify({'error': '找不到請假申請'}), 404
        conn.execute("""
            UPDATE leave_requests SET attachment=%s, attachment_name=%s, attachment_type=%s WHERE id=%s
        """, (raw, filename, media_type, rid))
    return jsonify({'ok': True, 'attachment_name': filename})

@app.route('/api/leave/attachment/<int:rid>', methods=['GET'])
def api_leave_attachment_get(rid):
    sid = session.get('punch_staff_id')
    is_admin = session.get('logged_in')
    with get_db() as conn:
        row = conn.execute("SELECT staff_id, attachment, attachment_name, attachment_type FROM leave_requests WHERE id=%s", (rid,)).fetchone()
    if not row or not row['attachment']:
        return ('', 404)
    if not is_admin and row['staff_id'] != sid:
        return jsonify({'error': '無權限'}), 403
    from flask import Response
    return Response(
        bytes(row['attachment']),
        mimetype=row['attachment_type'] or 'application/octet-stream',
        headers={'Content-Disposition': f'inline; filename="{os.path.basename(row["attachment_name"] or "attachment")}"'}
    )

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
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_overrides JSONB DEFAULT NULL",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS income_tax_withheld NUMERIC(12,2) DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS salary_advances (
            id           SERIAL PRIMARY KEY,
            staff_id     INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
            advance_date DATE NOT NULL,
            deduct_month TEXT NOT NULL DEFAULT '',
            note         TEXT DEFAULT '',
            status       TEXT DEFAULT 'pending',
            created_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
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
        except (ValueError, TypeError): d['items'] = []
    if d.get('confirmed_at'): d['confirmed_at'] = d['confirmed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    if d.get('updated_at'):   d['updated_at']   = d['updated_at'].isoformat()
    return d

def _eval_formula(formula, base_salary, insured_salary, service_years):
    """安全計算薪資公式（僅允許數值四則運算，不使用 eval）"""
    import ast as _ast
    if not formula: return 0.0

    _ALLOWED_NODES = (
        _ast.Expression, _ast.BinOp, _ast.UnaryOp, _ast.Constant,
        _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.FloorDiv,
        _ast.Mod, _ast.Pow, _ast.USub, _ast.UAdd, _ast.Name,
    )
    _vars = {
        'base_salary':    float(base_salary or 0),
        'insured_salary': float(insured_salary or 0),
        'service_years':  float(service_years or 0),
    }

    def _safe_eval(node):
        if isinstance(node, _ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, _ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError('非數值常數')
            return float(node.value)
        if isinstance(node, _ast.Name):
            if node.id not in _vars:
                raise ValueError(f'未知變數：{node.id}')
            return _vars[node.id]
        if isinstance(node, _ast.BinOp):
            l, r = _safe_eval(node.left), _safe_eval(node.right)
            op = node.op
            if isinstance(op, _ast.Add):      return l + r
            if isinstance(op, _ast.Sub):      return l - r
            if isinstance(op, _ast.Mult):     return l * r
            if isinstance(op, _ast.Div):      return l / r if r else 0.0
            if isinstance(op, _ast.FloorDiv): return float(int(l // r)) if r else 0.0
            if isinstance(op, _ast.Mod):      return l % r if r else 0.0
            if isinstance(op, _ast.Pow):
                if abs(r) > 32: raise ValueError('指數過大')
                return l ** r
        if isinstance(node, _ast.UnaryOp):
            v = _safe_eval(node.operand)
            if isinstance(node.op, _ast.USub): return -v
            if isinstance(node.op, _ast.UAdd): return +v
        raise ValueError(f'不允許的運算：{type(node).__name__}')

    try:
        tree = _ast.parse(formula.strip(), mode='eval')
        # 白名單節點檢查
        for node in _ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ValueError(f'不允許的語法：{type(node).__name__}')
        result = _safe_eval(tree)
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
    邏輯：每天找最早 in + 最晚 out，扣除休息時間；支援跨日班次
    回傳 (total_hours, work_days, details)
    """
    from datetime import datetime as _dth, timezone as _tzh, timedelta as _tdh, date as _dateh
    import calendar as _calh
    TW = _tzh(_tdh(hours=8))

    _yh, _mh   = int(month[:4]), int(month[5:])
    _last_h    = _calh.monthrange(_yh, _mh)[1]
    _next_date_h = (_dateh(_yh, _mh, _last_h) + _tdh(days=1)).isoformat()

    rows = conn.execute("""
        SELECT punch_type, punched_at
        FROM punch_records
        WHERE staff_id=%s
          AND (
            to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            OR (punched_at AT TIME ZONE 'Asia/Taipei')::date = %s
          )
        ORDER BY punched_at ASC
    """, (staff_id, month, _next_date_h)).fetchall()

    # Group by work session: non-'in' records within 24h of the last 'in'
    # are assigned to that 'in' record's date (handles cross-day shifts)
    day_map       = {}
    last_in_ds    = None
    last_in_dt    = None
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tzh.utc)
        pa_tw = pa.astimezone(TW)
        ptype = r['punch_type']
        ds    = pa_tw.strftime('%Y-%m-%d')

        if ptype == 'in':
            last_in_ds = ds
            last_in_dt = pa_tw
            target_ds  = ds
        elif last_in_ds and last_in_dt and (pa_tw - last_in_dt).total_seconds() <= 86400:
            target_ds  = last_in_ds   # 屬於前一天跨日班次
        else:
            target_ds  = ds           # 孤立打卡，以自身日期為準

        if target_ds not in day_map:
            day_map[target_ds] = []
        day_map[target_ds].append({'type': ptype, 'dt': pa_tw})

    # 只保留本月的日期（跨日班次的 in 已在本月，out 被歸回同 key）
    day_map = {ds: v for ds, v in day_map.items() if ds.startswith(month)}

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
    from datetime import date as _d5, timedelta as _td5, datetime as _dts5, timezone as _tz5
    _TW5 = _tz5(_td5(hours=8))
    _today5 = _dts5.now(_TW5).date()
    y, m = int(month[:4]), int(month[5:])
    total_work_days = work_days
    scheduled_dates = set()

    if total_work_days is None:
        # 1. 優先從排班取工作日
        shift_date_rows = conn.execute("""
            SELECT DISTINCT date FROM shift_assignments
            WHERE staff_id=%s AND TO_CHAR(date,'YYYY-MM')=%s
            ORDER BY date
        """, (staff['id'], month)).fetchall()
        if shift_date_rows:
            scheduled_dates = {r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date']) for r in shift_date_rows}
            total_work_days = len(scheduled_dates)
        else:
            # 2. 備援：日曆扣除週日 + 國定假日
            holiday_rows = conn.execute("""
                SELECT date FROM public_holidays
                WHERE TO_CHAR(date,'YYYY-MM')=%s
            """, (month,)).fetchall()
            holiday_dates = {r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date']) for r in holiday_rows}
            days_in_month = _cal2.monthrange(y, m)[1]
            for _d in range(1, days_in_month + 1):
                _dt = _d5(y, m, _d)
                _ds = _dt.isoformat()
                if _dt.weekday() != 6 and _ds not in holiday_dates:
                    scheduled_dates.add(_ds)
            total_work_days = len(scheduled_dates)

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
    # 員工個人金額覆寫 {str(item_id): amount}
    _overrides = staff.get('salary_item_overrides') or {}
    if isinstance(_overrides, str):
        try: _overrides = _json.loads(_overrides)
        except Exception: _overrides = {}

    def _apply_override(item_id, calculated_amt):
        """若員工設有個人金額，使用個人金額；否則使用計算值"""
        key = str(item_id)
        if key in _overrides and _overrides[key] is not None and _overrides[key] != '':
            return float(_overrides[key]), True   # (amount, is_overridden)
        return calculated_amt, False

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
            calc_amt = _eval_formula(it['formula'] or '', base_salary or insured_salary,
                                     insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else (it['formula'] or '')
            items.append({
                'id': it['id'], 'name': it['name'], 'type': 'deduction',
                'amount': round(amt, 2), 'formula': it['formula'] or '',
                'calc_note': note,
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
            formula  = it['formula'] or ''
            calc_amt = float(it['amount'] or 0)
            if formula:
                calc_amt = _eval_formula(formula, base_salary, insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else formula
            items.append({
                'id':        it['id'],
                'name':      it['name'],
                'type':      it['item_type'],
                'amount':    round(amt, 2),
                'formula':   formula,
                'calc_note': note,
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

    # ── 月薪制：缺勤扣款（打卡記錄核查） ─────────────────────
    absent_days = 0
    if salary_type == 'monthly' and scheduled_dates and daily_wage > 0:
        punch_rows = conn.execute("""
            SELECT DISTINCT (punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date
            FROM punch_records WHERE staff_id=%s
              AND TO_CHAR(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
        """, (staff['id'], month)).fetchall()
        punched_dates = {r['work_date'].isoformat() if hasattr(r['work_date'], 'isoformat') else str(r['work_date']) for r in punch_rows}
        # 已核准請假日期集合
        leave_date_rows = conn.execute("""
            SELECT start_date, end_date FROM leave_requests
            WHERE staff_id=%s AND status='approved'
              AND TO_CHAR(start_date,'YYYY-MM')=%s
        """, (staff['id'], month)).fetchall()
        leave_date_set = set()
        for _lr in leave_date_rows:
            _ld = _lr['start_date']
            _le = _lr['end_date']
            while _ld <= _le:
                leave_date_set.add(_ld.isoformat() if hasattr(_ld, 'isoformat') else str(_ld))
                _ld += _td5(days=1)
        # 缺勤 = 排班但未打卡且非假日，僅計算過去日期
        absent_date_list = sorted(
            ds for ds in scheduled_dates
            if ds not in punched_dates and ds not in leave_date_set
               and _d5.fromisoformat(ds) < _today5
        )
        absent_days = len(absent_date_list)
        if absent_days > 0:
            deduct = round(daily_wage * absent_days, 2)
            sample = '、'.join(absent_date_list[:3]) + ('等' if absent_days > 3 else '')
            items.append({
                'id': 'absent', 'name': f'缺勤扣款（{absent_days} 天）', 'type': 'deduction',
                'amount': deduct, 'formula': '',
                'calc_note': f'{absent_days} 天 × 日薪 ${round(daily_wage, 0)}（{sample}）',
            })
            deduction_total += deduct

    # ── 薪資預支扣帳 ─────────────────────────────────────────
    advance_rows = conn.execute("""
        SELECT id, amount, advance_date, note
        FROM salary_advances
        WHERE staff_id=%s AND status='pending' AND deduct_month=%s
        ORDER BY advance_date
    """, (staff['id'], month)).fetchall()
    for adv in advance_rows:
        adv_amt  = float(adv['amount'])
        adv_date = adv['advance_date'].isoformat() if hasattr(adv['advance_date'], 'isoformat') else str(adv['advance_date'])
        adv_note = adv['note'] or ''
        items.append({
            'id': f'advance_{adv["id"]}', 'name': '薪資預支扣帳',
            'type': 'deduction', 'amount': adv_amt, 'formula': '',
            'calc_note': f'{adv_date} 預支 ${adv_amt:,.0f}' + (f'（{adv_note}）' if adv_note else ''),
        })
        deduction_total += adv_amt

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
        'actual_days':        max(0, actual_days - absent_days),
        'leave_days':         leave_days,
        'unpaid_days':        unpaid_days,
        'absent_days':        absent_days,
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
    now = time.time()
    if _salary_items_cache['data'] is not None and now - _salary_items_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_salary_items_cache['data'])
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM salary_items ORDER BY sort_order, id").fetchall()
    result = [salary_item_row(r) for r in rows]
    _salary_items_cache['data'] = result; _salary_items_cache['at'] = now
    return jsonify(result)

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
    _salary_items_cache['data'] = None
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
    _salary_items_cache['data'] = None
    return jsonify(salary_item_row(row)) if row else ('', 404)

@app.route('/api/salary/items/<int:iid>', methods=['DELETE'])
@require_module('salary')
def api_salary_item_delete(iid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_items WHERE id=%s", (iid,))
    _salary_items_cache['data'] = None
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

_SALARY_GENERATE_LOCK_KEY = 0x53414C41  # 'SALA' — advisory lock for batch salary generation

@app.route('/api/salary/records/generate', methods=['POST'])
@require_module('salary')
def api_salary_generate():
    """自動產生或更新該月薪資"""
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    if not month: return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        # 取得 session-level advisory lock，避免同時多次批次產生互相覆蓋
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (_SALARY_GENERATE_LOCK_KEY,)
        ).fetchone()['ok']
        if not acquired:
            return jsonify({'error': '薪資批次正在產生中，請稍後再試'}), 409
        try:
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
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SALARY_GENERATE_LOCK_KEY,))
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

@app.route('/api/salary/records/confirm-all', methods=['POST'])
@require_module('salary')
def api_salary_confirm_all():
    b    = request.get_json(force=True)
    month = b.get('month','').strip()
    by   = b.get('confirmed_by','管理員')
    if not month: return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        rows = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE month=%s AND status='draft'
            RETURNING id, staff_id, month, net_pay
        """, (by, month)).fetchall()
        if rows:
            staff_ids = [r['staff_id'] for r in rows]
            placeholders = ','.join(['%s'] * len(staff_ids))
            conn.execute(
                f"UPDATE salary_advances SET status='deducted' WHERE staff_id IN ({placeholders}) AND deduct_month=%s AND status='pending'",
                (*staff_ids, month)
            )
    confirmed = len(rows)
    for row in rows:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify({'ok': True, 'confirmed': confirmed})

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
            conn.execute("""
                UPDATE salary_advances SET status='deducted'
                WHERE staff_id=%s AND deduct_month=%s AND status='pending'
            """, (row['staff_id'], row['month']))
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

# ── Salary Advances (薪資預支扣帳) ────────────────────────────────

def _advance_row(r):
    d = dict(r)
    if d.get('advance_date'):
        d['advance_date'] = d['advance_date'].isoformat() if hasattr(d['advance_date'], 'isoformat') else str(d['advance_date'])
    if d.get('created_at'):
        d['created_at'] = d['created_at'].isoformat()
    d['amount'] = float(d.get('amount') or 0)
    return d

@app.route('/api/salary/advances', methods=['GET'])
@require_module('salary')
def api_salary_advances_list():
    staff_id = request.args.get('staff_id', type=int)
    month    = request.args.get('month', '')
    status   = request.args.get('status', '')
    wheres, params = [], []
    if staff_id: wheres.append('sa.staff_id=%s');    params.append(staff_id)
    if month:    wheres.append('sa.deduct_month=%s'); params.append(month)
    if status:   wheres.append('sa.status=%s');       params.append(status)
    where_sql = ('WHERE ' + ' AND '.join(wheres)) if wheres else ''
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT sa.*, ps.name as staff_name, ps.department
            FROM salary_advances sa
            JOIN punch_staff ps ON ps.id=sa.staff_id
            {where_sql}
            ORDER BY sa.advance_date DESC, sa.id DESC
        """, params).fetchall()
    return jsonify([_advance_row(r) for r in rows])

@app.route('/api/salary/advances', methods=['POST'])
@require_module('salary')
def api_salary_advance_create():
    b            = request.get_json(force=True)
    staff_id     = b.get('staff_id')
    amount       = float(b.get('amount') or 0)
    advance_date = b.get('advance_date', '').strip()
    deduct_month = b.get('deduct_month', '').strip()
    note         = b.get('note', '').strip()
    if not staff_id or amount <= 0 or not advance_date:
        return jsonify({'error': '請填寫員工、金額、預支日期'}), 400
    if not deduct_month:
        from datetime import date as _da
        deduct_month = _da.today().strftime('%Y-%m')
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO salary_advances (staff_id, amount, advance_date, deduct_month, note)
            VALUES (%s,%s,%s,%s,%s) RETURNING *
        """, (staff_id, amount, advance_date, deduct_month, note)).fetchone()
    return jsonify(_advance_row(row)), 201

@app.route('/api/salary/advances/<int:aid>', methods=['PATCH'])
@require_module('salary')
def api_salary_advance_update(aid):
    b = request.get_json(force=True)
    fields, params = [], []
    if 'status'       in b: fields.append('status=%s');       params.append(b['status'])
    if 'note'         in b: fields.append('note=%s');         params.append(b['note'])
    if 'deduct_month' in b: fields.append('deduct_month=%s'); params.append(b['deduct_month'])
    if 'amount'       in b: fields.append('amount=%s');       params.append(float(b['amount']))
    if not fields: return jsonify({'error': '無更新欄位'}), 400
    params.append(aid)
    with get_db() as conn:
        row = conn.execute(
            f"UPDATE salary_advances SET {','.join(fields)} WHERE id=%s RETURNING *", params
        ).fetchone()
    return jsonify(_advance_row(row)) if row else ('', 404)

@app.route('/api/salary/advances/<int:aid>', methods=['DELETE'])
@require_module('salary')
def api_salary_advance_delete(aid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_advances WHERE id=%s", (aid,))
    return jsonify({'deleted': aid})

# ── Salary Staff Settings ─────────────────────────────────────────

@app.route('/api/salary/staff', methods=['GET'])
@require_module('salary')
def api_salary_staff_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, name, username, role, active, employee_code, department,
                   position_title, hire_date, birth_date, base_salary, insured_salary,
                   daily_hours, ot_rate1, ot_rate2, salary_type, hourly_rate,
                   vacation_quota, salary_notes, salary_item_ids, salary_item_overrides,
                   national_id, gender, insurance_type, address
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
        overrides = b.get('salary_item_overrides')  # dict {str(item_id): amount}
        overrides_json = _json.dumps(overrides) if overrides else None
        conn.execute("""
            UPDATE punch_staff SET
              employee_code=%s, department=%s, position_title=%s,
              hire_date=%s, birth_date=%s,
              base_salary=%s, insured_salary=%s, daily_hours=%s,
              ot_rate1=%s, ot_rate2=%s, salary_type=%s,
              hourly_rate=%s, vacation_quota=%s, salary_notes=%s,
              salary_item_ids=%s, salary_item_overrides=%s,
              national_id=%s, gender=%s, insurance_type=%s, address=%s
            WHERE id=%s
        """, (_s('employee_code'), _s('department'), _s('position_title'),
              _s('hire_date'), _s('birth_date'),
              _f('base_salary'), _f('insured_salary'), _f('daily_hours') or 8,
              _f('ot_rate1') or 1.33, _f('ot_rate2') or 1.67,
              b.get('salary_type','monthly'),
              _f('hourly_rate'), b.get('vacation_quota') or None,
              b.get('salary_notes',''), salary_item_ids_json, overrides_json,
              (b.get('national_id') or '').strip(),
              (b.get('gender') or '').strip(),
              (b.get('insurance_type') or 'regular').strip(),
              (b.get('address') or '').strip(),
              sid))
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
    _ann_public_cache['data'] = None
    if row and row['active']:
        _broadcast_announcement_line(row['title'], row['content'])
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
    _ann_public_cache['data'] = None
    return jsonify(ann_row(row)) if row else ('', 404)

@app.route('/api/announcements/<int:aid>', methods=['DELETE'])
@require_module('ann')
def api_ann_delete(aid):
    with get_db() as conn:
        conn.execute("DELETE FROM announcements WHERE id=%s", (aid,))
    _ann_public_cache['data'] = None
    return jsonify({'deleted': aid})

@app.route('/api/announcements/<int:aid>/pin', methods=['POST'])
@require_module('ann')
def api_ann_toggle_pin(aid):
    with get_db() as conn:
        row = conn.execute(
            "UPDATE announcements SET is_pinned=NOT is_pinned, updated_at=NOW() WHERE id=%s RETURNING *",
            (aid,)
        ).fetchone()
    _ann_public_cache['data'] = None
    return jsonify(ann_row(row)) if row else ('', 404)

# ── Public: employee reads ────────────────────────────────────────

@app.route('/api/announcements/public', methods=['GET'])
def api_ann_public():
    """員工端讀取有效公告"""
    now = time.time()
    if _ann_public_cache['data'] is not None and now - _ann_public_cache['at'] < _ANN_TTL:
        return jsonify(_ann_public_cache['data'])
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM announcements
            WHERE active = TRUE
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY is_pinned DESC, published_at DESC
            LIMIT 50
        """).fetchall()
    result = [ann_row(r) for r in rows]
    _ann_public_cache['data'] = result; _ann_public_cache['at'] = now
    return jsonify(result)

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
    year  = request.args.get('year', '')
    month = request.args.get('month', '')
    # strip cache-busting param added by old client code
    cache_key = f"{year}:{month}" if (year or month) else '__all__'
    now = time.time()
    cached = _holidays_pub_cache.get(cache_key)
    if cached and now - cached['at'] < _HOLIDAY_TTL:
        return jsonify(cached['data'])
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
    result = {r['date'].isoformat(): r['name'] for r in rows}
    _holidays_pub_cache[cache_key] = {'data': result, 'at': now}
    return jsonify(result)

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
    _holidays_pub_cache.clear()
    return jsonify(holiday_row(row)), 201

@app.route('/api/holidays/<int:hid>', methods=['DELETE'])
@require_module('holiday')
def api_holiday_delete(hid):
    with get_db() as conn:
        conn.execute("DELETE FROM public_holidays WHERE id=%s", (hid,))
    _holidays_pub_cache.clear()
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
    _holidays_pub_cache.clear()
    return jsonify({'imported': count})


# ═══════════════════════════════════════════════════════════════════
# Feature 2: LINE Notification Helper
# ═══════════════════════════════════════════════════════════════════

def _notify_staff_line(staff_id, message):
    """
    Send LINE notification to a staff member if they have LINE bound.
    Uses the line_punch_config token (same LINE OA).
    Runs in a background thread to avoid blocking the request.
    """
    def _send():
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
    threading.Thread(target=_send, daemon=True).start()


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
    """匯出月度出勤明細 Excel"""
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

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"出勤明細_{month}"

    headers = ['員工代碼','姓名','部門','職稱','日期','打卡類型','時間','補打卡','操作人','GPS距離(m)','地點','備註']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        vals = [
            r['employee_code'] or '', r['staff_name'],
            r['department'] or '', r['role'] or '',
            str(r['work_date']),
            PUNCH_LABEL.get(r['punch_type'], r['punch_type']),
            r['punch_time'],
            '是' if r['is_manual'] else '',
            r['manual_by'] or '',
            r['gps_distance'] if r['gps_distance'] is not None else '',
            r['location_name'] or '', r['note'] or '',
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([10,12,10,10,12,10,8,6,10,12,16,20], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=attendance_{month}.xlsx'})


@app.route('/api/export/attendance-summary', methods=['GET'])
@login_required
def api_export_attendance_summary():
    """匯出月度出勤摘要 Excel（每人每天工時）"""
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

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"出勤摘要_{month}"

    headers = ['員工代碼','姓名','部門','職稱','日期','上班','下班','工時(h)','打卡次數','含補打']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        dur_h = ''
        if r['ci_ts'] and r['co_ts']:
            from datetime import datetime as _dtx
            try:
                ci = r['ci_ts'] if hasattr(r['ci_ts'], 'timestamp') else _dtx.fromisoformat(str(r['ci_ts']))
                co = r['co_ts'] if hasattr(r['co_ts'], 'timestamp') else _dtx.fromisoformat(str(r['co_ts']))
                dur_h = round((co - ci).total_seconds() / 3600, 2)
            except Exception:
                pass
        vals = [
            r['employee_code'] or '', r['name'],
            r['department'] or '', r['role'] or '',
            str(r['work_date']),
            r['clock_in'] or '', r['clock_out'] or '',
            dur_h, r['punch_count'],
            '是' if r['has_manual'] else '',
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([10,12,10,10,12,8,8,8,8,8], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=attendance_summary_{month}.xlsx'})


@app.route('/api/attendance/anomaly-report', methods=['GET'])
@login_required
def api_anomaly_report_excel():
    """匯出出勤異常報告 Excel（缺打卡、遲到、早退）"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from io import BytesIO
    import calendar as _cal
    from datetime import datetime as _dtx, timedelta as _tdx

    month = request.args.get('month', '') or _dt.now(TW_TZ).strftime('%Y-%m')
    try:
        y, mo = int(month[:4]), int(month[5:7])
    except Exception:
        return jsonify({'error': '月份格式錯誤'}), 400

    TW_OFF = _tdx(hours=8)

    with get_db() as conn:
        punch_rows = conn.execute("""
            SELECT ps.id as staff_id, ps.name as staff_name,
                   ps.department,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN (pr.punched_at AT TIME ZONE 'Asia/Taipei') END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN (pr.punched_at AT TIME ZONE 'Asia/Taipei') END) as clock_out,
                   BOOL_OR(pr.punch_type='in')  as has_in,
                   BOOL_OR(pr.punch_type='out') as has_out
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id=pr.staff_id AND ps.active=TRUE
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            GROUP BY ps.id, ps.name, ps.department,
                     (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY work_date, ps.name
        """, (month,)).fetchall()

        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date,
                   st.start_time::text as start_time,
                   st.end_time::text   as end_time,
                   ps.name as staff_name, ps.department
            FROM shift_assignments sa
            JOIN shift_types st ON st.id=sa.shift_type_id
            JOIN punch_staff ps ON ps.id=sa.staff_id AND ps.active=TRUE
            WHERE TO_CHAR(sa.shift_date,'YYYY-MM')=%s
        """, (month,)).fetchall()

        y_int = int(month[:4]); mo_int = int(month[5:7])
        first_day = f"{y_int}-{mo_int:02d}-01"
        days_in   = _cal.monthrange(y_int, mo_int)[1]
        last_day  = f"{y_int}-{mo_int:02d}-{days_in:02d}"
        leave_rows = conn.execute("""
            SELECT staff_id, start_date, end_date
            FROM leave_requests
            WHERE status='approved'
              AND start_date <= %s AND end_date >= %s
        """, (last_day, first_day)).fetchall()

        # 有打卡記錄的 (staff_id, date) 組合
        punched_dates = conn.execute("""
            SELECT DISTINCT pr.staff_id,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date
            FROM punch_records pr
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
        """, (month,)).fetchall()

    # Build lookup maps
    shift_map = {(r['staff_id'], str(r['shift_date'])): r for r in shift_rows}
    punched_set = {(r['staff_id'], str(r['work_date'])) for r in punched_dates}
    leave_set = set()
    from datetime import date as _dax, timedelta as _tdax
    for lr in leave_rows:
        s = lr['start_date']; e = lr['end_date']
        cur = s
        while cur <= e:
            leave_set.add((lr['staff_id'], str(cur)))
            cur = _dax.fromisoformat(str(cur)) + _tdax(days=1)
            cur = cur if isinstance(cur, _dax) else cur.date()

    today = _dax.today()

    # Build anomaly rows
    anomalies = []
    for r in punch_rows:
        ds = str(r['work_date'])
        sid = r['staff_id']
        shift = shift_map.get((sid, ds))

        anomaly_type = ''; detail = ''
        late_min = 0; early_min = 0

        if not r['has_in'] and r['has_out']:
            anomaly_type = '缺上班打卡'; detail = f"僅有下班 {str(r['clock_out'])[11:16]}"
        elif r['has_in'] and not r['has_out']:
            if _dax.fromisoformat(ds) < today:
                anomaly_type = '缺下班打卡'; detail = f"上班 {str(r['clock_in'])[11:16]} 無下班"
        elif r['has_in'] and r['has_out'] and shift:
            ci_t = str(r['clock_in'])[11:16]
            co_t = str(r['clock_out'])[11:16]
            sh_s = str(shift['start_time'])[:5]
            sh_e = str(shift['end_time'])[:5]
            try:
                ci_m = int(ci_t[:2])*60 + int(ci_t[3:5])
                sh_s_m = int(sh_s[:2])*60 + int(sh_s[3:5])
                if ci_m - sh_s_m > 10:
                    late_min = ci_m - sh_s_m
                    anomaly_type = '遲到'; detail = f"應 {sh_s}，實際 {ci_t}（+{late_min}分）"
            except Exception:
                pass
            if not anomaly_type:
                try:
                    co_m = int(co_t[:2])*60 + int(co_t[3:5])
                    sh_e_m = int(sh_e[:2])*60 + int(sh_e[3:5])
                    if sh_e_m - co_m > 15:
                        early_min = sh_e_m - co_m
                        anomaly_type = '早退'; detail = f"應 {sh_e}，實際 {co_t}（-{early_min}分）"
                except Exception:
                    pass

        if anomaly_type:
            anomalies.append({
                'staff_name':  r['staff_name'],
                'department':  r['department'] or '',
                'date':        ds,
                'shift_start': str(shift['start_time'])[:5] if shift else '—',
                'shift_end':   str(shift['end_time'])[:5]   if shift else '—',
                'clock_in':    str(r['clock_in'])[11:16]  if r['clock_in']  else '—',
                'clock_out':   str(r['clock_out'])[11:16] if r['clock_out'] else '—',
                'anomaly_type': anomaly_type,
                'detail':       detail,
            })

    # 有排班但完全未打卡（且非假日）的記錄
    for sr in shift_rows:
        ds = str(sr['shift_date'])
        sid = sr['staff_id']
        if (sid, ds) not in punched_set and (sid, ds) not in leave_set:
            shift_date_obj = _dax.fromisoformat(ds)
            if shift_date_obj < today:
                anomalies.append({
                    'staff_name':   sr['staff_name'],
                    'department':   sr['department'] or '',
                    'date':         ds,
                    'shift_start':  str(sr['start_time'])[:5],
                    'shift_end':    str(sr['end_time'])[:5],
                    'clock_in':     '—',
                    'clock_out':    '—',
                    'anomaly_type': '完全未打卡',
                    'detail':       f"排班 {str(sr['start_time'])[:5]}～{str(sr['end_time'])[:5]}，當日無任何打卡記錄",
                })
    anomalies.sort(key=lambda x: (x['date'], x['staff_name']))

    # Build Excel
    wb   = openpyxl.Workbook()
    ws   = wb.active
    ws.title = f'{month} 異常明細'

    thin = Border(
        left=Side(style='thin', color='DDDDDD'), right=Side(style='thin', color='DDDDDD'),
        top=Side(style='thin',  color='DDDDDD'), bottom=Side(style='thin', color='DDDDDD'),
    )
    header_fill   = PatternFill('solid', fgColor='0F1C3A')
    warn_fill     = PatternFill('solid', fgColor='FFF3CD')
    err_fill      = PatternFill('solid', fgColor='FDECEA')
    center_align  = Alignment(horizontal='center', vertical='center')

    headers = ['員工姓名','部門','日期','應上班','應下班','實際上班','實際下班','異常類型','說明']
    col_w   = [12, 10, 12, 8, 8, 8, 8, 12, 30]
    for ci, (h, w) in enumerate(zip(headers, col_w), 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font      = Font(bold=True, color='FFFFFF', name='Noto Sans TC', size=11)
        cell.fill      = header_fill
        cell.alignment = center_align
        cell.border    = thin
        ws.column_dimensions[ws.cell(row=1, column=ci).column_letter].width = w

    for ri, a in enumerate(anomalies, 2):
        row_fill = err_fill if a['anomaly_type'] in ('缺上班打卡','缺下班打卡','完全未打卡') else warn_fill
        vals = [a['staff_name'], a['department'], a['date'],
                a['shift_start'], a['shift_end'],
                a['clock_in'], a['clock_out'],
                a['anomaly_type'], a['detail']]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.fill      = row_fill
            cell.alignment = center_align if ci != 9 else Alignment(vertical='center')
            cell.border    = thin

    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'

    # Summary sheet
    ws2 = wb.create_sheet('摘要')
    ws2.append(['統計', '數量'])
    ws2.append(['異常總筆數', len(anomalies)])
    by_type = {}
    for a in anomalies:
        by_type[a['anomaly_type']] = by_type.get(a['anomaly_type'], 0) + 1
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        ws2.append([t, c])

    buf = BytesIO()
    wb.save(buf); buf.seek(0)
    from flask import Response as _FR
    return _FR(
        buf.read(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename=anomaly_{month}.xlsx'}
    )


@app.route('/api/export/salary', methods=['GET'])
@login_required
def api_export_salary():
    """匯出月度薪資明細 Excel"""
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

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"薪資_{month}"

    headers = ['員工代碼','姓名','部門','職稱','薪資制度',
               '工作日','出勤天數','請假天數','無薪假天數',
               '津貼合計','扣除合計','加班費','實領金額','狀態','備註']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        sal_type = r['salary_type'] or 'monthly'
        vals = [
            r['employee_code'] or '', r['staff_name'],
            r['department'] or '', r['role'] or '',
            '時薪制' if sal_type == 'hourly' else '月薪制',
            float(r['work_days'] or 0), float(r['actual_days'] or 0),
            float(r['leave_days'] or 0), float(r['unpaid_days'] or 0),
            float(r['allowance_total'] or 0), float(r['deduction_total'] or 0),
            float(r['ot_pay'] or 0), float(r['net_pay'] or 0),
            '已確認' if r['status'] == 'confirmed' else '草稿',
            r['note'] or '',
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([10,12,10,10,8,8,8,8,8,10,10,10,12,8,20], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=salary_{month}.xlsx'})


@app.route('/api/export/leave', methods=['GET'])
@login_required
def api_export_leave():
    """匯出請假記錄 Excel"""
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

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"請假_{month or year or 'all'}"

    headers = ['員工代碼','姓名','部門','假別','薪資倍率','開始日期','結束日期','天數','原因','代理人','狀態']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    PAY_LABEL = {1.0:'全薪', 0.5:'半薪', 0.0:'無薪'}
    STATUS_LABEL = {'approved':'已核准','rejected':'已退回','pending':'待審核'}
    for i, r in enumerate(rows, 2):
        vals = [
            r['employee_code'] or '', r['staff_name'], r['department'] or '',
            r['leave_type_name'], PAY_LABEL.get(float(r['pay_rate']), f"{r['pay_rate']}倍"),
            str(r['start_date']), str(r['end_date']),
            float(r['total_days']),
            r['reason'] or '', r['substitute_name'] or '',
            STATUS_LABEL.get(r['status'], r['status']),
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([10,12,10,10,8,12,12,6,24,10,8], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    tag = month or year or 'all'
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=leave_{tag}.xlsx'})


@app.route('/api/export/monthly-stats', methods=['GET'])
@login_required
def api_export_monthly_stats():
    """匯出出勤月統計 Excel"""
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _dm
        month = _dm.today().strftime('%Y-%m')

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                ps.name, ps.department,
                COUNT(DISTINCT (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date) as days_worked,
                ROUND(SUM(
                    CASE WHEN pr.punch_type IN ('in','out') THEN 0 ELSE 0 END
                )::numeric, 2) as placeholder
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM') = %s
            GROUP BY ps.id, ps.name, ps.department
            ORDER BY ps.name
        """, (month,)).fetchall()

        # 取每人每日上下班時間以計算工時
        detail = conn.execute("""
            SELECT ps.name, ps.department,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as ci,
                   MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as co
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM') = %s
            GROUP BY ps.name, ps.department, (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
        """, (month,)).fetchall()

    from datetime import datetime as _dtx
    stats = {}
    for r in detail:
        key = (r['name'], r['department'] or '')
        if key not in stats:
            stats[key] = {'days': 0, 'total_min': 0}
        stats[key]['days'] += 1
        if r['ci'] and r['co']:
            try:
                ci = r['ci'] if hasattr(r['ci'], 'timestamp') else _dtx.fromisoformat(str(r['ci']))
                co = r['co'] if hasattr(r['co'], 'timestamp') else _dtx.fromisoformat(str(r['co']))
                stats[key]['total_min'] += int((co - ci).total_seconds() / 60)
            except Exception:
                pass

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"出勤月統計_{month}"

    headers = ['姓名','部門','出勤天數','總工時(h)','平均時數/天']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, (key, s) in enumerate(sorted(stats.items()), 2):
        name, dept = key
        days = s['days']
        total_h = round(s['total_min'] / 60, 2)
        avg_h   = round(total_h / days, 2) if days else 0
        for col, v in enumerate([name, dept, days, total_h, avg_h], 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([14,12,10,10,12], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=monthly_stats_{month}.xlsx'})


@app.route('/api/export/overtime', methods=['GET'])
@login_required
def api_export_overtime():
    """匯出加班申請 Excel"""
    month  = request.args.get('month', '')
    status = request.args.get('status', '')
    conds, params = ['TRUE'], []
    if month:  conds.append("to_char(r.request_date,'YYYY-MM')=%s"); params.append(month)
    if status: conds.append("r.status=%s"); params.append(status)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT r.*, ps.name as staff_name, ps.employee_code, ps.department, ps.role as staff_role
            FROM overtime_requests r
            JOIN punch_staff ps ON ps.id = r.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY r.request_date DESC, r.created_at DESC
        """, params).fetchall()

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"加班申請_{month or 'all'}"

    headers = ['員工代碼','姓名','部門','職稱','申請日期','開始時間','結束時間','加班時數','加班費','日別','原因','狀態','審核人','備註']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')
    DAY_LABEL = {'weekday':'平日','holiday':'假日','rest_day':'休息日','sunday':'星期日'}
    STATUS_LABEL = {'approved':'已核准','rejected':'已退回','pending':'待審核'}

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        vals = [
            r['employee_code'] or '', r['staff_name'],
            r['department'] or '', r['staff_role'] or '',
            str(r['request_date']),
            str(r['start_time'])[:5] if r['start_time'] else '',
            str(r['end_time'])[:5]   if r['end_time']   else '',
            float(r['ot_hours'] or 0), float(r['ot_pay'] or 0),
            DAY_LABEL.get(r['day_type'] or '', r['day_type'] or ''),
            r['reason'] or '',
            STATUS_LABEL.get(r['status'], r['status']),
            r['reviewed_by'] or '', r['review_note'] or '',
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([10,12,10,10,12,8,8,8,10,8,20,8,10,20], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=overtime_{month or "all"}.xlsx'})


@app.route('/api/export/training', methods=['GET'])
@login_required
def api_export_training():
    """匯出教育訓練記錄 Excel"""
    staff_id = request.args.get('staff_id', '')
    category = request.args.get('category', '')
    sql = """
        SELECT tr.*, ps.name AS staff_name, ps.department
        FROM training_records tr
        JOIN punch_staff ps ON tr.staff_id = ps.id
        WHERE 1=1
    """
    params = []
    if staff_id: sql += " AND tr.staff_id=%s"; params.append(int(staff_id))
    if category: sql += " AND tr.category=%s"; params.append(category)
    sql += " ORDER BY ps.name, tr.expiry_date ASC NULLS LAST"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    from datetime import date as _td
    today = _td.today()

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "教育訓練記錄"

    headers = ['姓名','部門','課程名稱','類別','完成日期','到期日期','證書號碼','狀態','剩餘天數']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    exp_fill = PatternFill('solid', fgColor='FFF0F0')
    soon_fill= PatternFill('solid', fgColor='FFFBEA')
    center   = Alignment(horizontal='center', vertical='center')
    CAT_LABEL = {'food_safety':'食品安全','fire_safety':'消防安全','first_aid':'急救訓練',
                 'hygiene':'衛生管理','service':'服務禮儀','equipment':'設備操作',
                 'general':'一般訓練','other':'其他'}
    STATUS_LABEL = {'expired':'已過期','expiring_soon':'即將到期','valid':'有效','no_expiry':'無到期日'}

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        expiry_str = str(r['expiry_date']) if r['expiry_date'] else ''
        days_left = ''
        status = 'no_expiry'
        row_fill = alt_fill if i % 2 == 0 else None
        if expiry_str:
            from datetime import datetime as _dtx2
            ed = _dtx2.strptime(expiry_str, '%Y-%m-%d').date()
            dl = (ed - today).days
            days_left = dl
            if dl < 0:   status = 'expired';       row_fill = exp_fill
            elif dl <= 60: status = 'expiring_soon'; row_fill = soon_fill
            else:          status = 'valid'

        vals = [
            r['staff_name'], r['department'] or '',
            r['course_name'], CAT_LABEL.get(r['category'] or '', r['category'] or ''),
            str(r['completed_date']) if r['completed_date'] else '',
            expiry_str, r['certificate_no'] or '',
            STATUS_LABEL.get(status, status), days_left,
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if row_fill:
                c.fill = row_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([12,10,24,10,12,12,14,10,8], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=training_records.xlsx'})


@app.route('/api/export/expense-claims', methods=['GET'])
@login_required
def api_export_expense_claims():
    """匯出費用報帳 Excel（按表格欄位格式）"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    status    = request.args.get('status', '')
    year_month = request.args.get('ym', '')   # e.g. 2026-04
    conds, params = ['TRUE'], []
    if status:     conds.append("ec.status=%s");               params.append(status)
    if year_month: conds.append("TO_CHAR(ec.expense_date,'YYYY-MM')=%s"); params.append(year_month)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT ec.*, ps.name as staff_name, ps.employee_code, ps.department
            FROM expense_claims ec
            JOIN punch_staff ps ON ps.id = ec.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY ec.expense_date ASC, ec.created_at ASC
        """, params).fetchall()

    def roc_ym(d):
        """西元日期 → 民國年月，如 115/03月"""
        if not d: return ''
        try:
            from datetime import date as _date
            if hasattr(d, 'year'):
                return f"{d.year - 1911}/{d.month:02d}月"
            parts = str(d)[:7].split('-')
            return f"{int(parts[0]) - 1911}/{parts[1]}月"
        except Exception:
            return str(d)[:7]

    STATUS_LABEL = {'approved': '已核准', 'rejected': '已拒絕', 'pending': '待審核'}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '費用申請匯出'

    # ── 樣式定義 ──────────────────────────────────────────────────
    navy      = '0F1C3A'
    white     = 'FFFFFF'
    alt_gray  = 'F7F8FB'
    border_c  = 'CBD2E0'

    hdr_font  = Font(bold=True, color=white, size=10, name='微軟正黑體')
    body_font = Font(size=10, name='微軟正黑體')
    hdr_fill  = PatternFill('solid', fgColor=navy)
    alt_fill  = PatternFill('solid', fgColor=alt_gray)

    thin = Side(style='thin', color=border_c)
    full_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left_align   = Alignment(horizontal='left',   vertical='center', wrap_text=True)
    right_align  = Alignment(horizontal='right',  vertical='center')

    # ── 標題列（公司名 + 表單名） ──────────────────────────────────
    ws.merge_cells('A1:M1')
    title_c = ws.cell(1, 1, '進光設計　費用申請表')
    title_c.font      = Font(bold=True, size=14, name='微軟正黑體', color=navy)
    title_c.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 32

    # ── 表頭 ──────────────────────────────────────────────────────
    headers = [
        ('年月',     8),  ('員工代碼', 10), ('申請人',  12),
        ('費用名目', 24), ('費用類別', 14), ('費用性質', 9),
        ('報帳方式', 9),  ('費用金額', 12),
        ('戶名',    14),  ('銀行',    14),  ('帳號',   18),
        ('備註',    20),  ('狀態',     9),
    ]
    for col, (h, _) in enumerate(headers, 1):
        c = ws.cell(2, col, h)
        c.font      = hdr_font
        c.fill      = hdr_fill
        c.alignment = center_align
        c.border    = full_border
    ws.row_dimensions[2].height = 22
    ws.freeze_panes = 'A3'

    # ── 資料列 ────────────────────────────────────────────────────
    total_amount = 0
    for i, r in enumerate(rows, 3):
        amt = float(r['amount'] or 0)
        total_amount += amt
        fill = alt_fill if i % 2 == 0 else None

        row_vals = [
            (roc_ym(r['expense_date']),                      center_align),
            (r['employee_code'] or '',                       center_align),
            (r['staff_name'] or '',                          left_align),
            (r['title'] or '',                               left_align),
            (r['category'] or '',                            center_align),
            (r.get('expense_type') or '支出',                center_align),
            (r.get('reimbursement_method') or '匯款',        center_align),
            (amt,                                            right_align),
            (r.get('account_holder') or '',                  left_align),
            (r.get('bank_name') or '',                       left_align),
            (r.get('bank_account') or '',                    left_align),
            (r['note'] or '',                                left_align),
            (STATUS_LABEL.get(r['status'], r['status']),     center_align),
        ]
        for col, (val, align) in enumerate(row_vals, 1):
            c = ws.cell(i, col, val)
            c.font      = body_font
            c.alignment = align
            c.border    = full_border
            if fill: c.fill = fill
            # 金額格式
            if col == 8 and isinstance(val, (int, float)):
                c.number_format = '$#,##0'

        ws.row_dimensions[i].height = 18

    # ── 合計列 ────────────────────────────────────────────────────
    total_row = len(rows) + 3
    ws.merge_cells(f'A{total_row}:G{total_row}')
    tc = ws.cell(total_row, 1, '合　計')
    tc.font      = Font(bold=True, size=10, name='微軟正黑體', color=navy)
    tc.alignment = center_align
    tc.fill      = PatternFill('solid', fgColor='E8ECF6')
    tc.border    = full_border
    for col in range(2, 8):
        ws.cell(total_row, col).border = full_border
        ws.cell(total_row, col).fill   = PatternFill('solid', fgColor='E8ECF6')
    ac = ws.cell(total_row, 8, total_amount)
    ac.font          = Font(bold=True, size=10, name='微軟正黑體')
    ac.alignment     = right_align
    ac.number_format = '$#,##0'
    ac.fill          = PatternFill('solid', fgColor='E8ECF6')
    ac.border        = full_border
    for col in range(9, 14):
        ws.cell(total_row, col).border = full_border
        ws.cell(total_row, col).fill   = PatternFill('solid', fgColor='E8ECF6')
    ws.row_dimensions[total_row].height = 22

    # ── 欄寬 ──────────────────────────────────────────────────────
    for col, (_, w) in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col)].width = w

    # ── 列印設定 ──────────────────────────────────────────────────
    ws.page_setup.orientation      = 'landscape'
    ws.page_setup.paperSize         = 9   # A4
    ws.page_setup.fitToWidth        = 1
    ws.print_title_rows             = '1:2'

    fname = f"進光設計_費用申請_{year_month or status or 'all'}.xlsx"
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{__import__('urllib.parse').parse.quote(fname)}"})


@app.route('/api/export/performance', methods=['GET'])
@login_required
def api_export_performance():
    """匯出績效考核 Excel"""
    staff_id = request.args.get('staff_id', '')
    period   = request.args.get('period', '')
    conds, params = ['TRUE'], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if period:   conds.append("pr.period_label ILIKE %s"); params.append(f'%{period}%')

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name AS staff_name, ps.role AS staff_role,
                   ps.department, pt.name AS tpl_name
            FROM performance_reviews pr
            JOIN punch_staff ps ON ps.id = pr.staff_id
            LEFT JOIN performance_templates pt ON pt.id = pr.template_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.reviewed_at DESC
        """, params).fetchall()

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "績效考核"

    headers = ['姓名','部門','職稱','考核期間','考核範本','分數','等級','備註','考核人','考核日期','薪資調整']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        reviewed_at = str(r['reviewed_at'])[:10] if r['reviewed_at'] else ''
        adj = r['salary_adjusted']
        adj_str = f"+{adj:,.0f}" if adj and adj > 0 else (f"{adj:,.0f}" if adj and adj < 0 else '—')
        vals = [
            r['staff_name'], r['department'] or '', r['staff_role'] or '',
            r['period_label'] or '', r['tpl_name'] or '',
            float(r['total_score'] or 0), r['grade'] or '',
            r['comments'] or '', r['reviewer'] or '', reviewed_at, adj_str,
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            if i % 2 == 0:
                c.fill = alt_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([12,10,10,12,14,8,6,24,10,12,10], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=performance_reviews.xlsx'})


# ── Patch existing review functions with LINE notifications ──────

def _patch_reviews_with_notifications():
    """
    This is called after all route functions are defined.
    We monkey-patch the review endpoints to send LINE notifications.
    The actual patching is done inline in the route handlers below
    via the _notify_review_result helper.
    """
    pass

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
    _badges_cache.clear()
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
        except Exception:
            month = today.strftime('%Y-%m')
    else:
        month = today.strftime('%Y-%m')

    # 快取 key 包含今天日期，確保今日出勤資料每天刷新
    _cache_key = f"{month}:{today.isoformat()}"
    now = time.time()
    cached = _dashboard_cache.get(_cache_key)
    if cached and now - cached['at'] < _DASHBOARD_TTL:
        return jsonify(cached['data'])

    # Pre-compute today's UTC timestamp range for index-friendly queries
    from datetime import datetime as _ddt2
    _today_start = _ddt2(today.year, today.month, today.day, tzinfo=TW)
    _today_end   = _today_start + _tdd(days=1)

    with get_db() as conn:

        # ── 今日出勤明細（每人狀態）+ 計數 + 請假 —— 一次 JOIN 取完 ──
        today_detail_rows = conn.execute("""
            SELECT ps.id, ps.name, ps.role,
                   MAX(CASE WHEN pr.punch_type='in'  THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN to_char(pr.punched_at AT TIME ZONE 'Asia/Taipei','HH24:MI') END) as clock_out,
                   COUNT(pr.id) as punch_count,
                   MAX(lt.name) as leave_name
            FROM punch_staff ps
            LEFT JOIN punch_records pr
              ON pr.staff_id = ps.id
              AND pr.punched_at >= %s AND pr.punched_at < %s
            LEFT JOIN leave_requests lr
              ON lr.staff_id = ps.id AND lr.status='approved'
              AND lr.start_date <= %s AND lr.end_date >= %s
            LEFT JOIN leave_types lt ON lt.id = lr.leave_type_id
            WHERE ps.active = TRUE
            GROUP BY ps.id, ps.name, ps.role
            ORDER BY ps.name
        """, (_today_start, _today_end, today, today)).fetchall()

        # 從明細彙整今日統計數字（不需要再跑獨立 COUNT 查詢）
        total_staff    = len(today_detail_rows)
        clocked_in     = sum(1 for r in today_detail_rows if r['clock_in'])
        clocked_out    = sum(1 for r in today_detail_rows if r['clock_out'])
        on_leave_today = sum(1 for r in today_detail_rows if r['leave_name'] and not r['clock_in'])

        today_detail = []
        for r in today_detail_rows:
            if r['clock_in']:
                if r['clock_out']:
                    status, status_label = 'done', '已下班'
                else:
                    status, status_label = 'working', '上班中'
            elif r['leave_name']:
                status, status_label = 'leave', r['leave_name']
            else:
                status, status_label = 'absent', '未出勤'

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

        # ── 待審申請數（單一查詢）────────────────────────────────
        _prow = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM punch_requests    WHERE status='pending')                    AS punch_cnt,
                (SELECT COUNT(*) FROM overtime_requests WHERE status='pending')                    AS ot_cnt,
                (SELECT COUNT(*) FROM schedule_requests WHERE status IN ('pending','modified_pending')) AS sched_cnt,
                (SELECT COUNT(*) FROM leave_requests    WHERE status='pending')                    AS leave_cnt
        """).fetchone()
        pending_punch = _prow['punch_cnt']
        pending_ot    = _prow['ot_cnt']
        pending_sched = _prow['sched_cnt']
        pending_leave = _prow['leave_cnt']

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
    result = {
        'month':            month,
        'today':            str(today),
        'is_current_month': month == cur_month,
        'today_summary': {
            'total':       total_staff,
            'working':     clocked_in - clocked_out,
            'clocked_in':  clocked_in,
            'clocked_out': clocked_out,
            'on_leave':    on_leave_today,
            'absent':      total_staff - clocked_in - on_leave_today,
        },
        'today_detail': today_detail,
        'pending': {
            'punch':  pending_punch,
            'ot':     pending_ot,
            'sched':  pending_sched,
            'leave':  pending_leave,
            'total':  pending_punch + pending_ot + pending_sched + pending_leave,
        },
        'salary_summary': {
            'total_count':     sal_rows['total_count'],
            'confirmed_count': sal_rows['confirmed_count'],
            'total_net':       float(sal_rows['total_net']),
            'total_allow':     float(sal_rows['total_allow']),
            'total_deduct':    float(sal_rows['total_deduct']),
        },
        'daily_attendance':    daily_attendance,
        'leave_distribution':  leave_distribution,
        'ot_ranking':          ot_ranking,
    }
    _dashboard_cache[_cache_key] = {'data': result, 'at': now}
    return jsonify(result)


# ── Dashboard 擴充 API ────────────────────────────────────────────────────────

@app.route('/api/dashboard/labor-cost', methods=['GET'])
@login_required
def api_dashboard_labor_cost():
    """近 12 個月人事費用趨勢"""
    now = _time.time()
    if _labor_cost_cache.get('data') is not None and now - _labor_cost_cache['at'] < _LABOR_TTL:
        return jsonify(_labor_cost_cache['data'])
    from datetime import date as _dlc
    today = _dlc.today()
    months = []
    for i in range(11, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0: m += 12; y -= 1
        months.append(f'{y}-{m:02d}')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT month, COALESCE(SUM(net_pay),0) as total
            FROM salary_records
            WHERE month = ANY(%s)
            GROUP BY month
        """, (months,)).fetchall()
    cost_map = {r['month']: float(r['total']) for r in rows}
    result = {
        'months':     months,
        'labor_cost': [cost_map.get(m, 0) for m in months],
    }
    _labor_cost_cache['data'] = result
    _labor_cost_cache['at']   = now
    return jsonify(result)


@app.route('/api/dashboard/attendance-heatmap', methods=['GET'])
@login_required
def api_dashboard_attendance_heatmap():
    """本月每日出勤率（熱力圖資料）"""
    from datetime import date as _dah
    import calendar as _calh
    month = request.args.get('month', '') or _dah.today().strftime('%Y-%m')
    now = _time.time()
    _hc = _heatmap_cache.get(month)
    if _hc and now - _hc['at'] < _DASHBOARD_TTL:
        return jsonify(_hc['data'])
    y, mo = int(month[:4]), int(month[5:7])
    days_in = _calh.monthrange(y, mo)[1]

    with get_db() as conn:
        total_staff = conn.execute(
            "SELECT COUNT(*) as c FROM punch_staff WHERE active=TRUE"
        ).fetchone()['c']

        punch_rows = conn.execute("""
            SELECT (punched_at AT TIME ZONE 'Asia/Taipei')::date as d,
                   COUNT(DISTINCT staff_id) as cnt
            FROM punch_records
            WHERE punch_type='in'
              AND to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            GROUP BY d
        """, (month,)).fetchall()

        leave_rows = conn.execute("""
            SELECT lr.start_date, lr.end_date, COUNT(*) as cnt
            FROM leave_requests lr
            WHERE lr.status='approved'
              AND TO_CHAR(lr.start_date,'YYYY-MM')=%s OR TO_CHAR(lr.end_date,'YYYY-MM')=%s
            GROUP BY lr.start_date, lr.end_date
        """, (month, month)).fetchall()

    punch_map = {str(r['d']): int(r['cnt']) for r in punch_rows}

    from datetime import date as _dah2, timedelta as _tdah
    leave_map = {}
    for lr in leave_rows:
        s = _dah2.fromisoformat(str(lr['start_date']))
        e = _dah2.fromisoformat(str(lr['end_date']))
        cur = s
        while cur <= e:
            ds = str(cur)
            if ds.startswith(month):
                leave_map[ds] = leave_map.get(ds, 0) + 1
            cur += _tdah(days=1)

    days = []
    for d in range(1, days_in + 1):
        ds = f'{y}-{mo:02d}-{d:02d}'
        cnt = punch_map.get(ds, 0)
        rate = round(cnt / total_staff, 3) if total_staff > 0 else 0
        days.append({
            'date': ds,
            'day_of_week': _dah2(y, mo, d).weekday(),
            'count': cnt,
            'attendance_rate': rate,
            'on_leave': leave_map.get(ds, 0),
        })

    result = {'month': month, 'total_staff': total_staff, 'days': days}
    _heatmap_cache[month] = {'data': result, 'at': now}
    return jsonify(result)


@app.route('/api/dashboard/leave-distribution', methods=['GET'])
@login_required
def api_dashboard_leave_distribution():
    """本年度請假類型分佈"""
    from datetime import date as _dld
    year = request.args.get('year', str(_dld.today().year))
    with get_db() as conn:
        rows = conn.execute("""
            SELECT lt.name, lt.color,
                   COUNT(*) as cnt,
                   COALESCE(SUM(lr.total_days), 0) as days
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.status='approved'
              AND EXTRACT(YEAR FROM lr.start_date)=%s
            GROUP BY lt.name, lt.color
            ORDER BY days DESC
        """, (int(year),)).fetchall()
    total = sum(float(r['days']) for r in rows)
    return jsonify({
        'year': year,
        'total_leave_days': total,
        'breakdown': [{
            'name':  r['name'],
            'color': r['color'] or '#4a7bda',
            'days':  float(r['days']),
            'count': int(r['cnt']),
            'pct':   round(float(r['days']) / total * 100, 1) if total > 0 else 0,
        } for r in rows],
    })


# ── 年度扣繳憑單 ────────────────────────────────────────────────────────────

@app.route('/api/export/withholding', methods=['GET'])
@require_module('salary')
def api_export_withholding():
    """年度薪資所得扣繳憑單（所得類別50）"""
    from datetime import date as _dwh
    year   = request.args.get('year', str(_dwh.today().year))
    fmt    = request.args.get('format', 'html')

    fs = _get_finance_settings()
    company_name   = fs.get('company_name', '')
    company_tax_id = fs.get('company_tax_id', '')
    company_address= fs.get('company_address', '')

    with get_db() as conn:
        rows = conn.execute("""
            SELECT ps.id, ps.name, ps.national_id, ps.address,
                   COALESCE(SUM(sr.allowance_total), 0)       AS gross_salary,
                   COALESCE(SUM(sr.income_tax_withheld), 0)   AS tax_withheld,
                   COALESCE(AVG(sr.insured_salary), 0)        AS avg_insured
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.month LIKE %s AND sr.status='confirmed'
            GROUP BY ps.id, ps.name, ps.national_id, ps.address
            ORDER BY ps.name
        """, (f'{year}-%',)).fetchall()

    # 計算二代健保補充費
    def supp_nhi(gross, insured):
        base = float(gross) - float(insured) * 12
        return max(0, round(base * 0.0211, 0)) if base > 0 else 0

    data = []
    for i, r in enumerate(rows, 1):
        gross = float(r['gross_salary'])
        insured = float(r['avg_insured'])
        data.append({
            'no':          i,
            'name':        r['name'],
            'national_id': r['national_id'] or '—',
            'address':     r['address'] or '—',
            'gross':       gross,
            'supp_nhi':    supp_nhi(gross, insured),
            'tax':         float(r['tax_withheld']),
        })

    if fmt == 'xlsx':
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from io import BytesIO
        wb = wb2 = openpyxl.Workbook()
        ws = wb.active; ws.title = f'{year}年扣繳憑單'
        hfill = PatternFill('solid', fgColor='0F1C3A')
        thin  = Border(*[Side(style='thin', color='DDDDDD')]*4)
        hdrs  = ['序號','姓名','身分證字號','地址','年度薪資合計','二代健保補充費','扣繳稅額']
        ws.append(hdrs)
        for ci, h in enumerate(hdrs, 1):
            c = ws.cell(1, ci); c.font = Font(bold=True, color='FFFFFF', size=10); c.fill = hfill
            c.alignment = Alignment(horizontal='center', vertical='center'); c.border = thin
        ws.column_dimensions['A'].width = 5; ws.column_dimensions['B'].width = 12
        ws.column_dimensions['C'].width = 14; ws.column_dimensions['D'].width = 30
        ws.column_dimensions['E'].width = 16; ws.column_dimensions['F'].width = 16; ws.column_dimensions['G'].width = 12
        for d in data:
            ws.append([d['no'], d['name'], d['national_id'], d['address'],
                       d['gross'], d['supp_nhi'], d['tax']])
        buf = BytesIO(); wb.save(buf); buf.seek(0)
        from flask import Response as _FR2
        return _FR2(buf.read(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename=withholding_{year}.xlsx'})

    # HTML printable
    rows_html = ''.join(f"""
      <tr>
        <td style="text-align:center">{d['no']}</td>
        <td>{d['name']}</td>
        <td style="font-family:monospace">{d['national_id']}</td>
        <td style="font-size:11px">{d['address']}</td>
        <td style="text-align:right;font-family:monospace">{d['gross']:,.0f}</td>
        <td style="text-align:right;font-family:monospace">{d['supp_nhi']:,.0f}</td>
        <td style="text-align:right;font-family:monospace">{d['tax']:,.0f}</td>
      </tr>""" for d in data)
    html = f"""<!DOCTYPE html><html lang="zh-TW"><head>
<meta charset="UTF-8"><title>{year}年度薪資扣繳憑單</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Noto Sans TC',sans-serif;font-size:12px;padding:20px;color:#1e2a45}}
h2{{font-size:16px;font-weight:700;margin-bottom:4px}}
.meta{{font-size:11px;color:#666;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;margin-bottom:20px}}
th{{background:#0f1c3a;color:#fff;padding:7px 10px;font-size:11px;font-weight:600;text-align:left}}
td{{padding:6px 10px;border-bottom:1px solid #eee;font-size:12px}}
tr:nth-child(even){{background:#f8f9fb}}
.note{{font-size:10px;color:#888;border-top:1px solid #ddd;padding-top:8px}}
@media print{{button{{display:none}}}}
</style></head><body>
<button onclick="window.print()" style="margin-bottom:16px;padding:6px 16px;background:#0f1c3a;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">列印</button>
<h2>{year} 年度薪資所得扣繳憑單（所得類別 50）</h2>
<div class="meta">扣繳義務人：{company_name}　統一編號：{company_tax_id}　地址：{company_address}　製表日期：{_dwh.today().isoformat()}</div>
<table>
<thead><tr><th>#</th><th>員工姓名</th><th>身分證字號</th><th>地址</th><th>年度薪資合計(元)</th><th>二代健保補充費(元)</th><th>扣繳稅額(元)</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<div class="note">※ 本報表依薪資紀錄計算，二代健保補充費 = 超出投保薪資部分 × 2.11%。扣繳稅額請依各月薪資記錄人工確認。</div>
</body></html>"""
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


# ── 勞健保 EDI 申報 ─────────────────────────────────────────────────────────

def _get_insurance_settings():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT setting_key, setting_value FROM insurance_settings").fetchall()
        return {r['setting_key']: r['setting_value'] for r in rows}
    except Exception:
        return {}

def _roc_date(date_str):
    """Convert YYYY-MM-DD to YYYMMDD (ROC year)"""
    if not date_str: return '0000000'
    try:
        from datetime import date as _dedi
        d = _dedi.fromisoformat(str(date_str)[:10])
        return f'{d.year - 1911:03d}{d.month:02d}{d.day:02d}'
    except Exception:
        return '0000000'

def _edi_bytes(val, width, numeric=False):
    """Encode value to fixed-width bytes (Big5 for text, ASCII-padded for numeric)"""
    s = str(val or '')
    if numeric:
        return s.rjust(width, '0').encode('ascii', errors='replace')[:width]
    try:
        b = s.encode('big5', errors='replace')
    except Exception:
        b = s.encode('ascii', errors='replace')
    if len(b) < width:
        b = b + b' ' * (width - len(b))
    return b[:width]


@app.route('/api/insurance/settings', methods=['GET'])
@require_module('salary')
def api_insurance_settings_get():
    return jsonify(_get_insurance_settings())

@app.route('/api/insurance/settings', methods=['PUT'])
@require_module('salary')
def api_insurance_settings_put():
    b = request.get_json(force=True)
    with get_db() as conn:
        for k in ('labor_insurance_no', 'health_insurance_no', 'employer_name', 'employer_id'):
            conn.execute(
                "INSERT INTO insurance_settings VALUES (%s,%s) ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                (k, str(b.get(k, '')).strip()))
    return jsonify({'ok': True})


def _get_edi_staff(staff_ids_str):
    """Fetch staff rows for EDI, optionally filtered by comma-separated IDs."""
    with get_db() as conn:
        if staff_ids_str:
            ids = [int(x) for x in staff_ids_str.split(',') if x.strip().isdigit()]
            rows = conn.execute(
                f"SELECT * FROM punch_staff WHERE id = ANY(%s) AND active=TRUE ORDER BY name",
                (ids,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM punch_staff WHERE active=TRUE ORDER BY name").fetchall()
    return rows


@app.route('/api/export/edi/labor-enroll', methods=['GET'])
@require_module('salary')
def api_edi_labor_enroll():
    """勞工保險加退保申報 EDI（Big5 固定寬度格式）"""
    event_type  = request.args.get('event_type', 'in')   # in=加保 out=退保
    staff_ids   = request.args.get('staff_ids', '')
    event_date  = request.args.get('event_date', '')
    cfg         = _get_insurance_settings()
    labor_no    = cfg.get('labor_insurance_no', '').ljust(8)[:8]
    event_code  = b'1' if event_type == 'in' else b'2'
    event_roc   = _roc_date(event_date).encode('ascii')

    lines = []
    for s in _get_edi_staff(staff_ids):
        gender_code = b'1' if (s.get('gender') or '').upper() in ('M', '男') else b'2'
        insured = str(int(float(s.get('insured_salary') or 0))).rjust(6, '0').encode('ascii')
        line = (
            _edi_bytes(labor_no, 8) +
            _edi_bytes(s['name'], 20) +
            _edi_bytes(s.get('national_id', ''), 10) +
            _roc_date(s.get('birth_date')).encode('ascii') +
            event_roc +
            event_code +
            insured +
            gender_code +
            b'00'   # 職業類別（一般）
        )
        lines.append(line)
    content = b'\r\n'.join(lines)
    fname   = f'labor_{"enroll" if event_type=="in" else "exit"}_{event_date or "date"}.edi'
    from flask import Response as _FRe
    return _FRe(content, mimetype='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename={fname}'})


@app.route('/api/export/edi/labor-salary', methods=['GET'])
@require_module('salary')
def api_edi_labor_salary():
    """勞工保險投保薪資調整申報 EDI"""
    month     = request.args.get('month', '')
    staff_ids = request.args.get('staff_ids', '')
    cfg       = _get_insurance_settings()
    labor_no  = cfg.get('labor_insurance_no', '').ljust(8)[:8]
    if not month:
        from datetime import date as _dm2
        month = _dm2.today().strftime('%Y-%m')
    month_roc = f"{int(month[:4]) - 1911:03d}{month[5:7]}".encode('ascii')

    lines = []
    for s in _get_edi_staff(staff_ids):
        insured = str(int(float(s.get('insured_salary') or 0))).rjust(6, '0').encode('ascii')
        line = (
            _edi_bytes(labor_no, 8) +
            _edi_bytes(s['name'], 20) +
            _edi_bytes(s.get('national_id', ''), 10) +
            insured +
            month_roc
        )
        lines.append(line)
    content = b'\r\n'.join(lines)
    from flask import Response as _FRs
    return _FRs(content, mimetype='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename=labor_salary_{month}.edi'})


@app.route('/api/export/edi/health-enroll', methods=['GET'])
@require_module('salary')
def api_edi_health_enroll():
    """全民健康保險加退保申報 EDI"""
    event_type = request.args.get('event_type', 'in')
    staff_ids  = request.args.get('staff_ids', '')
    event_date = request.args.get('event_date', '')
    cfg        = _get_insurance_settings()
    health_no  = cfg.get('health_insurance_no', '').ljust(10)[:10]
    event_code = b'1' if event_type == 'in' else b'2'
    event_roc  = _roc_date(event_date).encode('ascii')

    lines = []
    for s in _get_edi_staff(staff_ids):
        gender_code = b'1' if (s.get('gender') or '').upper() in ('M', '男') else b'2'
        insured = str(int(float(s.get('insured_salary') or 0))).rjust(6, '0').encode('ascii')
        line = (
            _edi_bytes(health_no, 10) +
            _edi_bytes(s['name'], 20) +
            _edi_bytes(s.get('national_id', ''), 10) +
            _roc_date(s.get('birth_date')).encode('ascii') +
            event_roc +
            event_code +
            insured +
            gender_code
        )
        lines.append(line)
    content = b'\r\n'.join(lines)
    fname   = f'health_{"enroll" if event_type=="in" else "exit"}_{event_date or "date"}.edi'
    from flask import Response as _FRh
    return _FRh(content, mimetype='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename={fname}'})


# ── 多店管理 ─────────────────────────────────────────────────────────────────

@app.route('/api/stores', methods=['GET'])
@login_required
def api_stores_list():
    now    = time.time()
    cached = _stores_cache.get('data')
    if cached is not None and now - _stores_cache['at'] < _STATIC_TTL:
        return jsonify(cached)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM stores ORDER BY id").fetchall()
    result = [dict(r) for r in rows]
    _stores_cache['data'] = result
    _stores_cache['at']   = now
    return jsonify(result)

@app.route('/api/stores', methods=['POST'])
@login_required
def api_stores_create():
    b = request.get_json(force=True)
    name = (b.get('name') or '').strip()
    code = (b.get('code') or '').strip() or None
    if not name: return jsonify({'error': '店名為必填'}), 400
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO stores (name, code, address) VALUES (%s,%s,%s) RETURNING *",
            (name, code, (b.get('address') or '').strip())
        ).fetchone()
    _stores_cache['data'] = None
    return jsonify(dict(row)), 201

@app.route('/api/stores/<int:sid>', methods=['PUT'])
@login_required
def api_stores_update(sid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE stores SET name=%s, code=%s, address=%s, active=%s WHERE id=%s RETURNING *
        """, ((b.get('name') or '').strip(), (b.get('code') or None),
              (b.get('address') or '').strip(), bool(b.get('active', True)), sid)).fetchone()
    _stores_cache['data'] = None
    return jsonify(dict(row)) if row else ('', 404)

@app.route('/api/stores/<int:sid>', methods=['DELETE'])
@login_required
def api_stores_delete(sid):
    with get_db() as conn:
        conn.execute("UPDATE punch_staff     SET store_id=NULL WHERE store_id=%s", (sid,))
        conn.execute("UPDATE punch_locations SET store_id=NULL WHERE store_id=%s", (sid,))
        conn.execute("DELETE FROM stores WHERE id=%s", (sid,))
    _stores_cache['data'] = None
    return jsonify({'deleted': sid})

@app.route('/api/stores/<int:sid>/staff', methods=['GET'])
@login_required
def api_store_staff(sid):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, role, active FROM punch_staff WHERE store_id=%s ORDER BY name", (sid,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/staff/<int:sid>/store', methods=['PUT'])
@login_required
def api_staff_assign_store(sid):
    b = request.get_json(force=True)
    store_id = b.get('store_id')
    with get_db() as conn:
        conn.execute("UPDATE punch_staff SET store_id=%s WHERE id=%s", (store_id, sid))
    return jsonify({'ok': True})


# ── 排班需求 & 自動排班 ──────────────────────────────────────────────────────

@app.route('/api/shifts/staffing-requirements', methods=['GET'])
@login_required
def api_staffing_req_get():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT r.id, r.shift_type_id, r.day_of_week, r.required_count,
                   st.name as shift_name, st.color as shift_color
            FROM shift_staffing_requirements r
            JOIN shift_types st ON st.id=r.shift_type_id
            ORDER BY st.sort_order, r.day_of_week
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/shifts/staffing-requirements', methods=['PUT'])
@login_required
def api_staffing_req_put():
    items = request.get_json(force=True)
    if not isinstance(items, list):
        return jsonify({'error': '格式錯誤'}), 400
    count = 0
    with get_db() as conn:
        for it in items:
            stid = int(it.get('shift_type_id', 0))
            dow  = int(it.get('day_of_week', 0))
            req  = max(0, int(it.get('required_count', 1)))
            if req == 0:
                conn.execute(
                    "DELETE FROM shift_staffing_requirements WHERE shift_type_id=%s AND day_of_week=%s",
                    (stid, dow))
            else:
                conn.execute("""
                    INSERT INTO shift_staffing_requirements (shift_type_id, day_of_week, required_count, updated_at)
                    VALUES (%s,%s,%s,NOW())
                    ON CONFLICT (shift_type_id, day_of_week)
                    DO UPDATE SET required_count=EXCLUDED.required_count, updated_at=NOW()
                """, (stid, dow, req))
            count += 1
    return jsonify({'ok': True, 'upserted': count})


@app.route('/api/schedule/auto-generate', methods=['POST'])
@login_required
def api_auto_generate_schedule():
    """自動排班引擎：依人力需求與員工可用性生成班表建議"""
    from datetime import date as _dag, timedelta as _tdag
    import calendar as _calag

    b        = request.get_json(force=True)
    month    = (b.get('month') or '').strip()
    overwrite = bool(b.get('overwrite', False))
    if not month:
        month = _dag.today().strftime('%Y-%m')
    try:
        y, mo = int(month[:4]), int(month[5:7])
    except Exception:
        return jsonify({'error': '月份格式錯誤'}), 400

    days_in   = _calag.monthrange(y, mo)[1]
    all_dates = [_dag(y, mo, d) for d in range(1, days_in + 1)]

    with get_db() as conn:
        shift_types = conn.execute(
            "SELECT * FROM shift_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
        requirements = conn.execute("""
            SELECT shift_type_id, day_of_week, required_count
            FROM shift_staffing_requirements
        """).fetchall()
        staff_list = conn.execute(
            "SELECT id, name FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()

        # 本月已核准休假日期（per staff）
        leave_rows = conn.execute("""
            SELECT staff_id, start_date, end_date
            FROM leave_requests
            WHERE status='approved'
              AND start_date <= %s AND end_date >= %s
        """, (f'{y}-{mo:02d}-{days_in:02d}', f'{y}-{mo:02d}-01')).fetchall()

        # 已核准排休
        sched_rows = conn.execute("""
            SELECT staff_id, requested_dates
            FROM schedule_requests
            WHERE status='approved'
              AND to_char(created_at,'YYYY-MM')=%s
        """, (month,)).fetchall()

        # 現有班表
        existing = conn.execute("""
            SELECT staff_id, shift_date FROM shift_assignments
            WHERE TO_CHAR(shift_date,'YYYY-MM')=%s
        """, (month,)).fetchall()

    # 建立不可上班日 set: {(staff_id, date_str)}
    off_days = set()
    for lr in leave_rows:
        s = _dag.fromisoformat(str(lr['start_date']))
        e = _dag.fromisoformat(str(lr['end_date']))
        cur = s
        while cur <= e:
            off_days.add((lr['staff_id'], str(cur)))
            cur += _tdag(days=1)
    for sr in sched_rows:
        rdates = sr['requested_dates']
        if isinstance(rdates, str):
            try: rdates = _json.loads(rdates)
            except (ValueError, TypeError): rdates = []
        for ds in (rdates or []):
            off_days.add((sr['staff_id'], ds))

    # 已有班表 set（不 overwrite 時跳過）
    existing_set = {(r['staff_id'], str(r['shift_date'])) for r in existing}

    # 需求 map: {(shift_type_id, day_of_week): required_count}
    req_map = {(r['shift_type_id'], r['day_of_week']): r['required_count'] for r in requirements}

    # 排班計數器（避免連續超時）
    assigned_days  = {s['id']: [] for s in staff_list}  # staff_id -> [date]
    assignments    = []
    conflicts      = []
    staff_ids      = [s['id'] for s in staff_list]
    staff_name_map = {s['id']: s['name'] for s in staff_list}

    for date in all_dates:
        dow = date.weekday()  # 0=Mon, 6=Sun
        ds  = str(date)

        for st in shift_types:
            stid     = st['id']
            needed   = req_map.get((stid, dow), 0)
            if needed <= 0:
                continue

            # 可用員工：未請假、未排休
            available = [
                sid for sid in staff_ids
                if (sid, ds) not in off_days
            ]

            # 排除已被指派在其他班（同日）
            already_today = {a['staff_id'] for a in assignments if a['shift_date'] == ds}
            available = [sid for sid in available if sid not in already_today]

            # 排除連續 7 天（含本日）的員工
            def consecutive_days(sid, d):
                days = sorted(assigned_days[sid])
                streak = 0
                check = d
                while check in days:
                    streak += 1
                    check = str(_dag.fromisoformat(check) - _tdag(days=1))
                return streak

            available_ok = [sid for sid in available if consecutive_days(sid, ds) < 6]

            # 按本月已排天數升序（均衡分配）
            available_ok.sort(key=lambda sid: len(assigned_days[sid]))

            assigned_count = 0
            for sid in available_ok:
                if assigned_count >= needed:
                    break
                if not overwrite and (sid, ds) in existing_set:
                    assigned_count += 1
                    continue
                assignments.append({
                    'staff_id':     sid,
                    'staff_name':   staff_name_map[sid],
                    'shift_type_id': stid,
                    'shift_name':   st['name'],
                    'shift_date':   ds,
                })
                assigned_days[sid].append(ds)
                assigned_count += 1

            if assigned_count < needed:
                conflicts.append({
                    'type':   'understaffed',
                    'date':   ds,
                    'shift':  st['name'],
                    'detail': f'{ds} {st["name"]} 需要 {needed} 人，僅能排 {assigned_count} 人',
                })

    # 寫入資料庫
    inserted = 0
    if assignments:
        with get_db() as conn:
            for a in assignments:
                try:
                    if overwrite:
                        conn.execute("""
                            INSERT INTO shift_assignments (staff_id, shift_type_id, shift_date)
                            VALUES (%s,%s,%s)
                            ON CONFLICT (staff_id, shift_date) DO UPDATE
                            SET shift_type_id=EXCLUDED.shift_type_id
                        """, (a['staff_id'], a['shift_type_id'], a['shift_date']))
                    else:
                        conn.execute("""
                            INSERT INTO shift_assignments (staff_id, shift_type_id, shift_date)
                            VALUES (%s,%s,%s)
                            ON CONFLICT DO NOTHING
                        """, (a['staff_id'], a['shift_type_id'], a['shift_date']))
                    inserted += 1
                except Exception:
                    pass

    return jsonify({
        'ok':          True,
        'month':       month,
        'assignments': assignments,
        'conflicts':   conflicts,
        'summary': {
            'assigned':       inserted,
            'conflict_count': len(conflicts),
        },
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
        except (ValueError, TypeError): return '$0'

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
        # 一次撈出所有 pending 的請假單，避免迴圈內逐筆查詢
        pending = {
            r['id']: r for r in conn.execute(
                "SELECT * FROM leave_requests WHERE id = ANY(%s) AND status='pending'",
                (ids,)
            ).fetchall()
        }
        for rid in ids:
            old = pending.get(rid)
            if not old:
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
    _badges_cache.clear()
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
    _an_now = _time.time()
    if _anomalies_cache.get('data') is not None and _an_now - _anomalies_cache['at'] < _ANOMALIES_TTL:
        return jsonify(_anomalies_cache['data'])
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

        # 取得班別指派（用於遲到／早退判斷）
        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time, st.name as shift_name
            FROM shift_assignments sa
            JOIN shift_types st ON st.id = sa.shift_type_id
            WHERE sa.shift_date BETWEEN %s AND %s
        """, (date_from, today)).fetchall()
        shift_map = {(r['staff_id'], str(r['shift_date'])): r for r in shift_rows}

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
                            'severity':   'warning',
                            'staff_id':   r['staff_id'],
                            'name':       r['name'],
                            'role':       r['role'] or '',
                            'department': r['department'] or '',
                            'date':       ds,
                            'detail':     f"應 {shift['start_time'][:5]} 上班，實際 {r['first_in']}（晚 {late_mins} 分鐘）",
                        })
                except Exception:
                    pass

        # 早退判斷（有班別指派）
        if has_out and r['last_out'] and ds != str(today):
            shift = shift_map.get((r['staff_id'], ds))
            if shift and shift['end_time']:
                try:
                    eh, em = map(int, str(shift['end_time'])[:5].split(':'))
                    oh, om = map(int, r['last_out'].split(':'))
                    early_mins = (eh * 60 + em) - (oh * 60 + om)
                    if early_mins > 15:  # 超過15分鐘算早退
                        anomalies.append({
                            'type':       'early',
                            'label':      '早退',
                            'severity':   'warning',
                            'staff_id':   r['staff_id'],
                            'name':       r['name'],
                            'role':       r['role'] or '',
                            'department': r['department'] or '',
                            'date':       ds,
                            'detail':     f"應 {shift['end_time'][:5]} 下班，實際 {r['last_out']}（早 {early_mins} 分鐘）",
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
    result = {'anomalies': anomalies, 'count': len(anomalies), 'checked_from': str(date_from)}
    _anomalies_cache['data'] = result
    _anomalies_cache['at']   = _an_now
    return jsonify(result)


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

# ═══════════════════════════════════════════════════════════════════
# Finance Module (財務模組)
# ═══════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def init_finance_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS finance_categories (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'expense',
            color       TEXT DEFAULT '#4a7bda',
            sort_order  INT DEFAULT 0,
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
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
        """CREATE TABLE IF NOT EXISTS finance_documents (
            id              SERIAL PRIMARY KEY,
            filename        TEXT NOT NULL,
            doc_type        TEXT DEFAULT '',
            ocr_raw         JSONB DEFAULT '{}',
            upload_date     DATE DEFAULT CURRENT_DATE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_recurring (
            id              SERIAL PRIMARY KEY,
            title           TEXT NOT NULL,
            type            TEXT NOT NULL DEFAULT 'expense',
            category_id     INT REFERENCES finance_categories(id) ON DELETE SET NULL,
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            tax_amount      NUMERIC(14,2) DEFAULT 0,
            vendor          TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            frequency       TEXT NOT NULL DEFAULT 'monthly',
            day_of_month    INT DEFAULT 1,
            start_date      DATE NOT NULL,
            end_date        DATE,
            last_generated  TEXT DEFAULT '',
            active          BOOLEAN DEFAULT TRUE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS bank_statements (
            id                  SERIAL PRIMARY KEY,
            account_name        TEXT DEFAULT '',
            txn_date            DATE NOT NULL,
            amount              NUMERIC(14,2) NOT NULL,
            txn_type            TEXT DEFAULT 'debit',
            description         TEXT DEFAULT '',
            reconciled          BOOLEAN DEFAULT FALSE,
            matched_record_id   INT REFERENCES finance_records(id) ON DELETE SET NULL,
            import_batch        TEXT DEFAULT '',
            created_at          TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_payables (
            id              SERIAL PRIMARY KEY,
            payable_type    TEXT NOT NULL DEFAULT 'payable',
            title           TEXT NOT NULL,
            party_name      TEXT DEFAULT '',
            invoice_no      TEXT DEFAULT '',
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            due_date        DATE,
            status          TEXT NOT NULL DEFAULT 'open',
            paid_date       DATE,
            linked_record_id INT REFERENCES finance_records(id) ON DELETE SET NULL,
            note            TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_budgets (
            id              SERIAL PRIMARY KEY,
            year            INT NOT NULL,
            month           INT NOT NULL,
            category_id     INT REFERENCES finance_categories(id) ON DELETE CASCADE,
            budget_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(year, month, category_id)
        )""",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS finance_synced BOOLEAN DEFAULT FALSE",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[finance_init] {str(e)[:80]}")

    # Seed default categories
    defaults_income = [
        ('餐飲內用收入', 'income', '#2e9e6b', 1),
        ('外帶收入',     'income', '#0ea5e9', 2),
        ('外送收入',     'income', '#8b5cf6', 3),
        ('其他收入',     'income', '#c8a96e', 4),
    ]
    defaults_expense = [
        ('食材成本',   'expense', '#d64242', 10),
        ('薪資支出',   'expense', '#e07b2a', 11),
        ('租金',       'expense', '#8892a4', 12),
        ('水電費',     'expense', '#4a7bda', 13),
        ('設備維修',   'expense', '#e05c8a', 14),
        ('消耗品',     'expense', '#6366f1', 15),
        ('廣告行銷',   'expense', '#f59e0b', 16),
        ('其他支出',   'expense', '#64748b', 17),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM finance_categories").fetchone()['c']
            if cnt == 0:
                for name, ftype, color, sort in (defaults_income + defaults_expense):
                    conn.execute(
                        "INSERT INTO finance_categories (name,type,color,sort_order) VALUES (%s,%s,%s,%s)",
                        (name, ftype, color, sort)
                    )
    except Exception as e:
        print(f"[finance_seed] {e}")

init_finance_db()

def _finance_cat_row(r):
    if not r: return None
    d = dict(r)
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def _finance_rec_row(r):
    if not r: return None
    d = dict(r)
    if d.get('record_date'): d['record_date'] = str(d['record_date'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    for f in ('amount','tax_amount'):
        if d.get(f) is not None: d[f] = float(d[f])
    return d

# ── Finance Categories ─────────────────────────────────────────

@app.route('/api/finance/categories', methods=['GET'])
@require_module('finance')
def api_finance_categories_list():
    company = request.args.get('company', '')
    key     = company or '__all__'
    now     = time.time()
    cached  = _fin_cats_cache.get(key)
    if cached and now - cached['at'] < _STATIC_TTL:
        return jsonify(cached['data'])
    with get_db() as conn:
        if company:
            rows = conn.execute(
                "SELECT * FROM finance_categories WHERE company_unit=%s ORDER BY sort_order, id", (company,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM finance_categories ORDER BY sort_order, id").fetchall()
    result = [_finance_cat_row(r) for r in rows]
    _fin_cats_cache[key] = {'data': result, 'at': now}
    return jsonify(result)

@app.route('/api/finance/categories', methods=['POST'])
@require_module('finance')
def api_finance_category_create():
    b = request.get_json(force=True)
    if not b.get('name','').strip(): return jsonify({'error': '名稱為必填'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO finance_categories (name,type,color,sort_order,active,statement_section)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'].strip(), b.get('type','expense'), b.get('color','#4a7bda'),
              int(b.get('sort_order',0)), bool(b.get('active',True)),
              b.get('statement_section') or ('operating_revenue' if b.get('type')=='income' else 'operating_expense')
             )).fetchone()
    _fin_cats_cache.clear()
    return jsonify(_finance_cat_row(row)), 201

@app.route('/api/finance/categories/<int:cid>', methods=['PUT'])
@require_module('finance')
def api_finance_category_update(cid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE finance_categories SET name=%s,type=%s,color=%s,sort_order=%s,active=%s,statement_section=%s
            WHERE id=%s RETURNING *
        """, (b.get('name','').strip(), b.get('type','expense'), b.get('color','#4a7bda'),
              int(b.get('sort_order',0)), bool(b.get('active',True)),
              b.get('statement_section') or ('operating_revenue' if b.get('type')=='income' else 'operating_expense'),
              cid)).fetchone()
    _fin_cats_cache.clear()
    return jsonify(_finance_cat_row(row)) if row else ('', 404)

@app.route('/api/finance/categories/<int:cid>', methods=['DELETE'])
@require_module('finance')
def api_finance_category_delete(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM finance_categories WHERE id=%s", (cid,))
    _fin_cats_cache.clear()
    return jsonify({'deleted': cid})

# ── Finance Records ────────────────────────────────────────────

@app.route('/api/finance/records', methods=['GET'])
@require_module('finance')
def api_finance_records_list():
    month   = request.args.get('month', '')
    ftype   = request.args.get('type', '')
    cat_id  = request.args.get('category_id', '')
    company = request.args.get('company', '')
    conds, params = ['TRUE'], []
    if month:
        conds.append("to_char(fr.record_date,'YYYY-MM')=%s"); params.append(month)
    if ftype:
        conds.append("fr.type=%s"); params.append(ftype)
    if cat_id:
        conds.append("fr.category_id=%s"); params.append(int(cat_id))
    if company:
        conds.append("fr.company_unit=%s"); params.append(company)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT fr.*, fc.name as category_name, fc.color as category_color,
                   fd.filename as doc_filename, fd.ocr_raw as ocr_raw
            FROM finance_records fr
            LEFT JOIN finance_categories fc ON fc.id=fr.category_id
            LEFT JOIN finance_documents fd ON fd.id=fr.document_id
            WHERE {' AND '.join(conds)}
            ORDER BY fr.record_date DESC, fr.id DESC
        """, params).fetchall()
    result = []
    for r in rows:
        d = _finance_rec_row(r)
        d['category_name']  = r['category_name']
        d['category_color'] = r['category_color']
        d['doc_filename']   = r['doc_filename']
        d['ocr_raw']        = r['ocr_raw'] if r['ocr_raw'] else None
        result.append(d)
    return jsonify(result)


@app.route('/api/finance/documents', methods=['GET'])
@require_module('finance')
def api_finance_documents_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT fd.*,
                   COUNT(fr.id) as linked_count,
                   MAX(fr.title) as linked_title,
                   MAX(fr.id) as linked_record_id
            FROM finance_documents fd
            LEFT JOIN finance_records fr ON fr.document_id = fd.id
            GROUP BY fd.id
            ORDER BY fd.created_at DESC
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get('upload_date'): d['upload_date'] = str(d['upload_date'])
        if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
        d['linked_count'] = int(d['linked_count'] or 0)
        result.append(d)
    return jsonify(result)

@app.route('/api/finance/records', methods=['POST'])
@require_module('finance')
def api_finance_record_create():
    b = request.get_json(force=True)
    if not b.get('title','').strip(): return jsonify({'error': '標題為必填'}), 400
    if not b.get('record_date'):      return jsonify({'error': '日期為必填'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO finance_records
              (record_date, category_id, type, title, amount, tax_amount, vendor, invoice_no, note, document_id, created_by, company_unit)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['record_date'], b.get('category_id') or None, b.get('type','expense'),
              b['title'].strip(), float(b.get('amount',0)), float(b.get('tax_amount',0)),
              b.get('vendor','').strip(), b.get('invoice_no','').strip(),
              b.get('note','').strip(), b.get('document_id') or None,
              session.get('admin_display_name',''),
              b.get('company_unit','ad'))).fetchone()
    return jsonify(_finance_rec_row(row)), 201

@app.route('/api/finance/records/<int:rid>', methods=['PUT'])
@require_module('finance')
def api_finance_record_update(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE finance_records SET
              record_date=%s, category_id=%s, type=%s, title=%s, amount=%s,
              tax_amount=%s, vendor=%s, invoice_no=%s, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b['record_date'], b.get('category_id') or None, b.get('type','expense'),
              b.get('title','').strip(), float(b.get('amount',0)), float(b.get('tax_amount',0)),
              b.get('vendor','').strip(), b.get('invoice_no','').strip(),
              b.get('note','').strip(), rid)).fetchone()
    return jsonify(_finance_rec_row(row)) if row else ('', 404)

@app.route('/api/finance/records/<int:rid>', methods=['DELETE'])
@require_module('finance')
def api_finance_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM finance_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

# ── Finance P&L Summary ────────────────────────────────────────

@app.route('/api/finance/summary/<year>/<month>', methods=['GET'])
@require_module('finance')
def api_finance_summary(year, month):
    period  = f"{year}-{month.zfill(2)}"
    company = request.args.get('company', '')
    c_cond     = "AND company_unit=%s" if company else ""
    c_cond_fr  = "AND fr.company_unit=%s" if company else ""
    c_param = [company] if company else []

    with get_db() as conn:
        totals = conn.execute(f"""
            SELECT type, COALESCE(SUM(amount),0) as total
            FROM finance_records
            WHERE to_char(record_date,'YYYY-MM')=%s {c_cond}
            GROUP BY type
        """, [period] + c_param).fetchall()
        income  = next((float(r['total']) for r in totals if r['type']=='income'), 0.0)
        expense = next((float(r['total']) for r in totals if r['type']=='expense'), 0.0)

        by_cat = conn.execute(f"""
            SELECT fc.name, fc.color, fr.type, COALESCE(SUM(fr.amount),0) as total
            FROM finance_records fr
            LEFT JOIN finance_categories fc ON fc.id=fr.category_id
            WHERE to_char(fr.record_date,'YYYY-MM')=%s {c_cond_fr}
            GROUP BY fc.name, fc.color, fr.type
            ORDER BY total DESC
        """, [period] + c_param).fetchall()

        # Last 6 months trend
        trend = conn.execute(f"""
            SELECT to_char(record_date,'YYYY-MM') as mon,
                   type, COALESCE(SUM(amount),0) as total
            FROM finance_records
            WHERE record_date >= (DATE_TRUNC('month', %s::date) - INTERVAL '5 months')
              AND record_date <  (DATE_TRUNC('month', %s::date) + INTERVAL '1 month')
              {c_cond}
            GROUP BY to_char(record_date,'YYYY-MM'), type
            ORDER BY mon
        """, [f"{period}-01", f"{period}-01"] + c_param).fetchall()

        # 營業收入月報（年度各月彙總，只計 income）
        revenue_monthly = conn.execute(f"""
            SELECT to_char(record_date,'YYYY-MM') as mon,
                   COALESCE(SUM(amount),0) as total
            FROM finance_records
            WHERE type='income'
              AND to_char(record_date,'YYYY')=%s
              {c_cond}
            GROUP BY to_char(record_date,'YYYY-MM')
            ORDER BY mon
        """, [year] + c_param).fetchall()

    return jsonify({
        'income':  income,
        'expense': expense,
        'net':     income - expense,
        'by_category': [
            {'name': r['name'] or '未分類', 'color': r['color'] or '#8892a4',
             'type': r['type'], 'total': float(r['total'])}
            for r in by_cat
        ],
        'trend': [
            {'month': r['mon'], 'type': r['type'], 'total': float(r['total'])}
            for r in trend
        ],
        'revenue_monthly': [
            {'month': r['mon'], 'total': float(r['total'])}
            for r in revenue_monthly
        ],
    })

# ── Finance OCR ────────────────────────────────────────────────

@app.route('/api/finance/ocr', methods=['POST'])
@require_module('finance')
def api_finance_ocr():
    import anthropic as _ant
    import base64, re as _re

    if not ANTHROPIC_API_KEY:
        return jsonify({'error': '尚未設定 ANTHROPIC_API_KEY 環境變數'}), 500

    file = request.files.get('file')
    if not file:
        return jsonify({'error': '請上傳圖片或 PDF 檔案'}), 400

    raw = file.read()
    media_type = file.content_type or 'image/jpeg'
    # Only image types supported by Claude vision
    if media_type not in ('image/jpeg','image/png','image/gif','image/webp'):
        media_type = 'image/jpeg'

    img_b64 = base64.standard_b64encode(raw).decode()

    client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1024,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}},
                    {'type': 'text', 'text': (
                        '請辨識此文件，以JSON格式回傳以下欄位（找不到的欄位填null）：\n'
                        '{"date":"YYYY-MM-DD","vendor":"廠商名稱","invoice_no":"發票或單據號碼",'
                        '"total_amount":含稅總金額數字,"tax_amount":稅額數字,"pre_tax_amount":未稅金額數字,'
                        '"doc_type":"invoice或receipt或expense之一",'
                        '"title":"建議記帳標題（簡短）",'
                        '"items":[{"name":"品項","qty":數量,"unit_price":單價,"amount":小計}],'
                        '"currency":"TWD"}\n只回傳JSON，不要其他文字或markdown。'
                    )}
                ]
            }]
        )
        text = msg.content[0].text.strip()
        text = _re.sub(r'^```json\s*', '', text, flags=_re.MULTILINE)
        text = _re.sub(r'\s*```$', '', text, flags=_re.MULTILINE)
        result = _json.loads(text)
    except _json.JSONDecodeError:
        result = {'raw_text': text, 'error': 'OCR 回傳格式無法解析'}
    except Exception as e:
        return jsonify({'error': f'OCR 失敗：{str(e)}'}), 500

    try:
        with get_db() as conn:
            doc = conn.execute("""
                INSERT INTO finance_documents (filename, doc_type, ocr_raw, upload_date)
                VALUES (%s,%s,%s,CURRENT_DATE) RETURNING id
            """, (file.filename, result.get('doc_type',''), _json.dumps(result))).fetchone()
        result['document_id'] = doc['id']
    except Exception as e:
        print(f"[finance_ocr doc save] {e}")

    return jsonify(result)

# ── Finance Export ─────────────────────────────────────────────

@app.route('/api/finance/export', methods=['GET'])
@require_module('finance')
def api_finance_export():
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO
    month   = request.args.get('month', '')
    company = request.args.get('company', '')
    conds, params = ['TRUE'], []
    if month:
        conds.append("to_char(fr.record_date,'YYYY-MM')=%s"); params.append(month)
    if company:
        conds.append("fr.company_unit=%s"); params.append(company)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT fr.record_date, fr.type, fr.title, fr.amount, fr.tax_amount,
                   fr.vendor, fr.invoice_no, fr.note, fc.name as category_name,
                   fr.company_unit
            FROM finance_records fr
            LEFT JOIN finance_categories fc ON fc.id=fr.category_id
            WHERE {' AND '.join(conds)}
            ORDER BY fr.record_date, fr.id
        """, params).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"財務_{month or 'all'}"

    co_label = {'ad':'AD影像事務所','jm':'進光設計'}
    headers = ['日期','類型','類別','標題','金額','稅額','廠商','單據號碼','備註','公司單位']
    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    inc_fill = PatternFill('solid', fgColor='F0FFF4')
    exp_fill = PatternFill('solid', fgColor='FFF5F5')
    center   = Alignment(horizontal='center', vertical='center')

    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        is_income = r['type'] == 'income'
        vals = [
            str(r['record_date']), '收入' if is_income else '支出',
            r['category_name'] or '', r['title'],
            float(r['amount'] or 0), float(r['tax_amount'] or 0),
            r['vendor'] or '', r['invoice_no'] or '', r['note'] or '',
            co_label.get(r.get('company_unit',''), r.get('company_unit','') or ''),
        ]
        row_fill = inc_fill if is_income else exp_fill
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            c.fill = row_fill
            c.alignment = Alignment(vertical='center')

    for col, w in enumerate([12,8,12,24,12,8,16,14,24,14], 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    # 小計行
    total_income  = sum(float(r['amount'] or 0) for r in rows if r['type']=='income')
    total_expense = sum(float(r['amount'] or 0) for r in rows if r['type']=='expense')
    last = len(rows) + 2
    ws.cell(last, 1, '合計').font = Font(bold=True)
    ws.cell(last, 5, total_income - total_expense).font = Font(bold=True,
        color='2E9E6B' if total_income >= total_expense else 'D64242')
    ws.cell(last, 2, f'收入{total_income:,.0f} / 支出{total_expense:,.0f}')

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    co_tag = f'_{company}' if company else ''
    fname = f"finance{co_tag}_{month or 'all'}.xlsx"
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})

# ── Finance Settings & Financial Statements ────────────────────

def init_finance_settings_db():
    migrations = [
        "ALTER TABLE finance_categories ADD COLUMN IF NOT EXISTS statement_section TEXT",
        """CREATE TABLE IF NOT EXISTS finance_settings (
            id            SERIAL PRIMARY KEY,
            setting_key   TEXT UNIQUE NOT NULL,
            setting_value TEXT DEFAULT ''
        )""",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[finance_settings_init] {str(e)[:80]}")

    # Set default statement_section based on type for existing rows with NULL
    section_defaults = {
        '餐飲內用收入': 'operating_revenue',
        '外帶收入':     'operating_revenue',
        '外送收入':     'operating_revenue',
        '其他收入':     'other_revenue',
        '食材成本':     'cogs',
        '薪資支出':     'operating_expense',
        '租金':         'operating_expense',
        '水電費':       'operating_expense',
        '設備維修':     'operating_expense',
        '消耗品':       'operating_expense',
        '廣告行銷':     'operating_expense',
        '其他支出':     'other_expense',
    }
    try:
        with get_db() as conn:
            # Fill named defaults
            for name, sec in section_defaults.items():
                conn.execute(
                    "UPDATE finance_categories SET statement_section=%s WHERE name=%s AND statement_section IS NULL",
                    (sec, name)
                )
            # Remaining NULLs: income → operating_revenue, expense → operating_expense
            conn.execute("""
                UPDATE finance_categories
                SET statement_section = CASE WHEN type='income' THEN 'operating_revenue' ELSE 'operating_expense' END
                WHERE statement_section IS NULL
            """)
    except Exception as e:
        print(f"[finance_settings_seed] {e}")

    # Seed settings defaults
    for k, v in [('company_name', ''), ('opening_cash', '0'), ('opening_equity', '0'),
                  ('company_tax_id', ''), ('company_address', '')]:
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO finance_settings (setting_key, setting_value) VALUES (%s,%s) ON CONFLICT (setting_key) DO NOTHING",
                    (k, v)
                )
        except Exception as e:
            print(f"[finance_settings_default] {e}")

init_finance_settings_db()


def init_insurance_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS insurance_settings (
                    setting_key   TEXT PRIMARY KEY,
                    setting_value TEXT DEFAULT ''
                )
            """)
        for k, v in [('labor_insurance_no', ''), ('health_insurance_no', ''),
                     ('employer_name', ''), ('employer_id', '')]:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO insurance_settings (setting_key, setting_value) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (k, v))
    except Exception as e:
        print(f"[insurance_init] {e}")

init_insurance_db()


# ═══════════════════════════════════════════════════════════════════════════════
# 教育訓練追蹤 (Training & Certificate Tracking)
# ═══════════════════════════════════════════════════════════════════════════════

def init_training_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS training_records (
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
                )
            """)
    except Exception as e:
        print(f"[training_init] {e}")

init_training_db()

TRAINING_CATEGORIES = {
    'food_safety':  '食品安全',
    'fire_safety':  '消防安全',
    'first_aid':    '急救訓練',
    'hygiene':      '衛生管理',
    'service':      '服務禮儀',
    'equipment':    '設備操作',
    'general':      '一般訓練',
    'other':        '其他',
}

@app.route('/api/training/records', methods=['GET'])
@login_required
def api_training_list():
    staff_id  = request.args.get('staff_id')
    category  = request.args.get('category', '')
    expiring  = request.args.get('expiring')   # days, e.g. 60
    expired   = request.args.get('expired')    # '1' = show only expired

    sql = """
        SELECT tr.*, ps.name AS staff_name, ps.department
        FROM training_records tr
        JOIN punch_staff ps ON tr.staff_id = ps.id
        WHERE 1=1
    """
    params = []
    if staff_id:
        sql += " AND tr.staff_id = %s"; params.append(int(staff_id))
    if category:
        sql += " AND tr.category = %s"; params.append(category)
    if expiring:
        days = int(expiring)
        sql += " AND tr.expiry_date IS NOT NULL AND tr.expiry_date <= CURRENT_DATE + (%s * INTERVAL '1 day') AND tr.expiry_date >= CURRENT_DATE"
        params.append(days)
    if expired == '1':
        sql += " AND tr.expiry_date IS NOT NULL AND tr.expiry_date < CURRENT_DATE"
    sql += " ORDER BY tr.expiry_date ASC NULLS LAST, tr.completed_date DESC"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for k in ('completed_date', 'expiry_date', 'created_at', 'updated_at'):
            if d.get(k): d[k] = str(d[k])
        today = date.today()
        if d.get('expiry_date'):
            ed = _dt.strptime(d['expiry_date'], '%Y-%m-%d').date()
            days_left = (ed - today).days
            d['days_left'] = days_left
            d['status'] = 'expired' if days_left < 0 else 'expiring_soon' if days_left <= 60 else 'valid'
        else:
            d['days_left'] = None
            d['status'] = 'no_expiry'
        result.append(d)
    return jsonify(result)

@app.route('/api/training/records', methods=['POST'])
@login_required
def api_training_create():
    b = request.get_json(force=True) or {}
    staff_id       = b.get('staff_id')
    course_name    = (b.get('course_name') or '').strip()
    category       = b.get('category', 'general')
    completed_date = b.get('completed_date') or None
    expiry_date    = b.get('expiry_date') or None
    certificate_no = (b.get('certificate_no') or '').strip()
    note           = (b.get('note') or '').strip()
    if not staff_id or not course_name:
        return jsonify({'error': '缺少必填欄位'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO training_records
              (staff_id, course_name, category, completed_date, expiry_date, certificate_no, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (staff_id, course_name, category, completed_date, expiry_date, certificate_no, note)).fetchone()
    return jsonify({'ok': True, 'id': row['id']})

@app.route('/api/training/records/<int:rid>', methods=['PUT'])
@login_required
def api_training_update(rid):
    b = request.get_json(force=True) or {}
    with get_db() as conn:
        conn.execute("""
            UPDATE training_records SET
              course_name=%s, category=%s, completed_date=%s, expiry_date=%s,
              certificate_no=%s, note=%s, updated_at=NOW()
            WHERE id=%s
        """, (
            b.get('course_name'), b.get('category', 'general'),
            b.get('completed_date') or None, b.get('expiry_date') or None,
            b.get('certificate_no', ''), b.get('note', ''), rid
        ))
    return jsonify({'ok': True})

@app.route('/api/training/records/<int:rid>', methods=['DELETE'])
@login_required
def api_training_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM training_records WHERE id=%s", (rid,))
    return jsonify({'ok': True})

@app.route('/api/training/summary', methods=['GET'])
@login_required
def api_training_summary():
    """每位員工的訓練狀況摘要"""
    with get_db() as conn:
        staff_all = conn.execute(
            "SELECT id, name, department FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        records = conn.execute("""
            SELECT staff_id, category, expiry_date,
                   CASE
                     WHEN expiry_date IS NULL THEN 'no_expiry'
                     WHEN expiry_date < CURRENT_DATE THEN 'expired'
                     WHEN expiry_date <= CURRENT_DATE + INTERVAL '60 days' THEN 'expiring_soon'
                     ELSE 'valid'
                   END AS status
            FROM training_records
        """).fetchall()
    from collections import defaultdict
    by_staff = defaultdict(list)
    for r in records:
        by_staff[r['staff_id']].append(dict(r))

    result = []
    for s in staff_all:
        recs = by_staff[s['id']]
        result.append({
            'id': s['id'], 'name': s['name'], 'department': s['department'],
            'total': len(recs),
            'valid': sum(1 for r in recs if r['status'] in ('valid', 'no_expiry')),
            'expiring_soon': sum(1 for r in recs if r['status'] == 'expiring_soon'),
            'expired': sum(1 for r in recs if r['status'] == 'expired'),
        })
    return jsonify(result)

# ── 薪資計算預覽 (Salary Preview without saving) ───────────────────────────────

@app.route('/api/salary/records/preview', methods=['POST'])
@require_module('salary')
def api_salary_preview():
    """預覽薪資計算結果（不儲存）"""
    b     = request.get_json(force=True) or {}
    month = b.get('month', '').strip()
    if not month:
        return jsonify({'error': '請指定月份'}), 400
    result = []
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        for staff in staff_list:
            data = _auto_generate_salary(conn, dict(staff), month)
            # punch attendance days this month
            punch_days = conn.execute("""
                SELECT COUNT(DISTINCT punched_at::date) AS n
                FROM punch_records WHERE staff_id=%s
                  AND to_char(punched_at,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()['n']
            approved_ot = conn.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(ot_hours),0) AS hrs
                FROM overtime_requests WHERE staff_id=%s
                  AND status='approved'
                  AND to_char(COALESCE(ot_date, request_date),'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()
            result.append({
                'staff_id':       data['staff_id'],
                'staff_name':     staff['name'],
                'department':     staff['department'],
                'salary_type':    staff['salary_type'],
                'punch_days':     punch_days,
                'work_days':      float(data['work_days']),
                'actual_days':    float(data['actual_days']),
                'leave_days':     float(data['leave_days']),
                'unpaid_days':    float(data['unpaid_days']),
                'ot_count':       int(approved_ot['n']),
                'ot_hours':       float(approved_ot['hrs']),
                'ot_pay':         float(data['ot_pay']),
                'base_salary':    float(data['base_salary']),
                'allowance_total': float(data['allowance_total']),
                'deduction_total': float(data['deduction_total']),
                'net_pay':        float(data['net_pay']),
            })
    return jsonify({'ok': True, 'month': month, 'records': result})


@app.route('/api/finance/settings', methods=['GET'])
@require_module('finance')
def api_finance_settings_get():
    with get_db() as conn:
        rows = conn.execute("SELECT setting_key, setting_value FROM finance_settings").fetchall()
    return jsonify({r['setting_key']: r['setting_value'] for r in rows})


@app.route('/api/finance/settings', methods=['POST'])
@require_module('finance')
def api_finance_settings_save():
    data = request.get_json(force=True)
    with get_db() as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT INTO finance_settings (setting_key, setting_value) VALUES (%s,%s) ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",
                (k, str(v))
            )
    return jsonify({'ok': True})


def _get_finance_settings():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT setting_key, setting_value FROM finance_settings").fetchall()
            return {r['setting_key']: r['setting_value'] for r in rows}
    except Exception:
        return {}


def _roc_year(year):
    return int(year) - 1911


def _month_last_day(year, month):
    import calendar
    return calendar.monthrange(int(year), int(month))[1]


def _compute_statements(year, month):
    """Compute all three financial statements for the given year/month."""
    from collections import defaultdict
    period = f"{year}-{str(month).zfill(2)}"
    settings = _get_finance_settings()
    opening_cash   = float(settings.get('opening_cash',   0) or 0)
    opening_equity = float(settings.get('opening_equity', 0) or 0)
    company_name   = settings.get('company_name', '') or '公司名稱'

    with get_db() as conn:
        records = conn.execute("""
            SELECT fr.type, fr.amount,
                   fc.name            AS cat_name,
                   fc.statement_section AS section
            FROM finance_records fr
            LEFT JOIN finance_categories fc ON fc.id = fr.category_id
            WHERE to_char(fr.record_date,'YYYY-MM') = %s
        """, (period,)).fetchall()

        # Cumulative net before this month (for balance sheet period-start)
        prev = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END), 0) AS cum_income,
                COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS cum_expense
            FROM finance_records
            WHERE record_date < DATE_TRUNC('month', %s::date)
        """, (f"{period}-01",)).fetchone()

    cum_net_before = float(prev['cum_income']) - float(prev['cum_expense'])

    by_section = defaultdict(float)
    by_cat     = defaultdict(float)
    for r in records:
        sec = r['section'] or ('operating_revenue' if r['type'] == 'income' else 'operating_expense')
        by_section[sec]               += float(r['amount'])
        by_cat[(sec, r['cat_name'] or '未分類')] += float(r['amount'])

    operating_revenue = by_section['operating_revenue']
    other_revenue     = by_section['other_revenue']
    cogs              = by_section['cogs']
    operating_expense = by_section['operating_expense']
    other_expense     = by_section['other_expense']

    gross_profit      = operating_revenue - cogs
    operating_income  = gross_profit - operating_expense
    net_income        = operating_income + other_revenue - other_expense
    total_income      = operating_revenue + other_revenue
    total_expense     = cogs + operating_expense + other_expense

    cum_net_total     = cum_net_before + net_income
    cash_balance      = opening_cash + opening_equity + cum_net_total
    total_equity      = opening_equity + cum_net_total

    def cat_lines(section):
        return [{'name': k[1], 'amount': round(v, 2)}
                for k, v in sorted(by_cat.items()) if k[0] == section]

    return {
        'company_name': company_name,
        'year': int(year), 'month': int(month),
        'roc_year': _roc_year(year),
        'last_day': _month_last_day(year, month),
        'income_statement': {
            'operating_revenue':       round(operating_revenue, 2),
            'operating_revenue_lines': cat_lines('operating_revenue'),
            'other_revenue':           round(other_revenue, 2),
            'other_revenue_lines':     cat_lines('other_revenue'),
            'cogs':                    round(cogs, 2),
            'cogs_lines':              cat_lines('cogs'),
            'gross_profit':            round(gross_profit, 2),
            'operating_expense':       round(operating_expense, 2),
            'operating_expense_lines': cat_lines('operating_expense'),
            'operating_income':        round(operating_income, 2),
            'other_expense':           round(other_expense, 2),
            'other_expense_lines':     cat_lines('other_expense'),
            'net_income':              round(net_income, 2),
        },
        'balance_sheet': {
            'cash':                    round(cash_balance, 2),
            'total_assets':            round(cash_balance, 2),
            'total_liabilities':       0,
            'opening_equity':          round(opening_equity, 2),
            'retained_earnings':       round(cum_net_total, 2),
            'total_equity':            round(total_equity, 2),
            'total_liabilities_equity': round(total_equity, 2),
        },
        'cash_flow': {
            'operating_inflow':        round(total_income, 2),
            'operating_inflow_lines':  cat_lines('operating_revenue') + cat_lines('other_revenue'),
            'operating_outflow':       round(total_expense, 2),
            'operating_outflow_lines': cat_lines('cogs') + cat_lines('operating_expense') + cat_lines('other_expense'),
            'operating_net':           round(total_income - total_expense, 2),
            'investing_net':           0,
            'financing_net':           0,
            'net_change':              round(total_income - total_expense, 2),
            'opening_cash':            round(opening_cash + opening_equity + cum_net_before, 2),
            'closing_cash':            round(cash_balance, 2),
        },
    }


@app.route('/api/finance/statements/<year>/<month>', methods=['GET'])
@require_module('finance')
def api_finance_statements(year, month):
    try:
        return jsonify(_compute_statements(year, month))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/finance/export/statements/<year>/<month>', methods=['GET'])
@require_module('finance')
def api_finance_export_statements(year, month):
    import openpyxl, io as _io
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

    d  = _compute_statements(year, month)
    co = d['company_name']
    ry = d['roc_year']
    m  = d['month']
    ld = d['last_day']
    IS = d['income_statement']
    BS = d['balance_sheet']
    CF = d['cash_flow']

    wb = openpyxl.Workbook()

    NAVY   = '1C3557'
    AMT    = '#,##0'
    FONT   = '標楷體'
    thin   = Side(style='thin')
    medium = Side(style='medium')

    def _border(top=False, bottom=False, dbl=False):
        return Border(
            top    = thin   if top else None,
            bottom = medium if dbl  else (thin if bottom else None),
        )

    def setup_ws(ws, title, date_str):
        ws.column_dimensions['A'].width = 30
        ws.column_dimensions['B'].width = 4
        ws.column_dimensions['C'].width = 18
        for row_vals, styles in [
            ([co, '', ''],        {'font': Font(FONT, bold=True, size=14), 'align': 'center', 'merge': True}),
            ([title, '', ''],     {'font': Font(FONT, bold=True, size=13), 'align': 'center', 'merge': True}),
            ([date_str, '', ''],  {'font': Font(FONT, size=11),            'align': 'center', 'merge': True}),
        ]:
            ws.append(row_vals)
            r = ws.max_row
            ws.cell(r, 1).font      = styles['font']
            ws.cell(r, 1).alignment = Alignment(horizontal=styles['align'])
            if styles.get('merge'):
                ws.merge_cells(f'A{r}:C{r}')
        ws.append([])  # blank

        # column headers
        ws.append(['項　　目', '', '金　額（元）'])
        r = ws.max_row
        ws.cell(r, 1).font      = Font(FONT, bold=True, size=11, color='FFFFFF')
        ws.cell(r, 3).font      = Font(FONT, bold=True, size=11, color='FFFFFF')
        ws.cell(r, 1).fill      = PatternFill('solid', fgColor=NAVY)
        ws.cell(r, 3).fill      = PatternFill('solid', fgColor=NAVY)
        ws.cell(r, 2).fill      = PatternFill('solid', fgColor=NAVY)
        ws.cell(r, 1).alignment = Alignment(horizontal='center')
        ws.cell(r, 3).alignment = Alignment(horizontal='right')

    def row(ws, label, amount=None, indent=0, bold=False, subtotal=False, total=False, dbl=False):
        prefix = '　' * indent
        ws.append([prefix + label, '', amount])
        r = ws.max_row
        b = bold or subtotal or total
        ws.cell(r, 1).font      = Font(FONT, bold=b, size=11)
        ws.cell(r, 1).alignment = Alignment(horizontal='left')
        if amount is not None:
            ws.cell(r, 3).font           = Font(FONT, bold=b, size=11)
            ws.cell(r, 3).number_format  = AMT
            ws.cell(r, 3).alignment      = Alignment(horizontal='right')
        if subtotal or total:
            ws.cell(r, 3).border = _border(top=(subtotal or total), bottom=(subtotal or total), dbl=dbl)
        elif amount is None:
            ws.cell(r, 1).font = Font(FONT, bold=True, size=11)

    # ─── 損益表 ──────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = '損益表'
    setup_ws(ws1, '損益表', f'中華民國{ry}年{m}月份')

    row(ws1, '一、營業收入', bold=True)
    for l in IS['operating_revenue_lines']:
        row(ws1, l['name'], l['amount'], indent=2)
    row(ws1, '營業收入合計', IS['operating_revenue'], indent=1, subtotal=True)
    ws1.append([])

    row(ws1, '二、營業成本', bold=True)
    for l in IS['cogs_lines']:
        row(ws1, l['name'], l['amount'], indent=2)
    row(ws1, '營業成本合計', IS['cogs'], indent=1, subtotal=True)
    ws1.append([])

    row(ws1, '毛　利', IS['gross_profit'], indent=1, total=True)
    ws1.append([])

    row(ws1, '三、營業費用', bold=True)
    for l in IS['operating_expense_lines']:
        row(ws1, l['name'], l['amount'], indent=2)
    row(ws1, '營業費用合計', IS['operating_expense'], indent=1, subtotal=True)
    ws1.append([])

    row(ws1, '營業利益（損失）', IS['operating_income'], indent=1, total=True)
    ws1.append([])

    if IS['other_revenue'] or IS['other_revenue_lines']:
        row(ws1, '四、營業外收入', bold=True)
        for l in IS['other_revenue_lines']:
            row(ws1, l['name'], l['amount'], indent=2)
        row(ws1, '營業外收入合計', IS['other_revenue'], indent=1, subtotal=True)
        ws1.append([])

    if IS['other_expense'] or IS['other_expense_lines']:
        row(ws1, '五、營業外費用', bold=True)
        for l in IS['other_expense_lines']:
            row(ws1, l['name'], l['amount'], indent=2)
        row(ws1, '營業外費用合計', IS['other_expense'], indent=1, subtotal=True)
        ws1.append([])

    row(ws1, '本期淨利（損）', IS['net_income'], bold=True, total=True, dbl=True)

    # ─── 資產負債表 ───────────────────────────────────────────────
    ws2 = wb.create_sheet('資產負債表')
    setup_ws(ws2, '資產負債表', f'中華民國{ry}年{m}月{ld}日')

    row(ws2, '【資　產】', bold=True)
    row(ws2, '流動資產', indent=1, bold=True)
    row(ws2, '現金及約當現金', BS['cash'], indent=2)
    row(ws2, '資產合計', BS['total_assets'], indent=1, total=True)
    ws2.append([])

    row(ws2, '【負　債】', bold=True)
    row(ws2, '流動負債', indent=1, bold=True)
    row(ws2, '應付帳款', 0, indent=2)
    row(ws2, '負債合計', BS['total_liabilities'], indent=1, total=True)
    ws2.append([])

    row(ws2, '【股東權益】', bold=True)
    row(ws2, '資本額', BS['opening_equity'], indent=2)
    row(ws2, '保留盈餘', BS['retained_earnings'], indent=2)
    row(ws2, '股東權益合計', BS['total_equity'], indent=1, total=True)
    ws2.append([])

    row(ws2, '負債及股東權益合計', BS['total_liabilities_equity'], bold=True, total=True, dbl=True)

    # ─── 現金流量表 ───────────────────────────────────────────────
    ws3 = wb.create_sheet('現金流量表')
    setup_ws(ws3, '現金流量表（直接法）', f'中華民國{ry}年{m}月份')

    row(ws3, '一、營業活動之現金流量', bold=True)
    row(ws3, '（一）收現收入', indent=1, bold=True)
    for l in CF['operating_inflow_lines']:
        row(ws3, l['name'], l['amount'], indent=3)
    row(ws3, '收現合計', CF['operating_inflow'], indent=2, subtotal=True)
    ws3.append([])
    row(ws3, '（二）付現費用', indent=1, bold=True)
    for l in CF['operating_outflow_lines']:
        row(ws3, l['name'], -l['amount'], indent=3)
    row(ws3, '付現合計', -CF['operating_outflow'], indent=2, subtotal=True)
    ws3.append([])
    row(ws3, '營業活動淨現金流量', CF['operating_net'], indent=1, total=True)
    ws3.append([])

    row(ws3, '二、投資活動之現金流量', bold=True)
    row(ws3, '投資活動淨現金流量', CF['investing_net'], indent=1, total=True)
    ws3.append([])

    row(ws3, '三、籌資活動之現金流量', bold=True)
    row(ws3, '籌資活動淨現金流量', CF['financing_net'], indent=1, total=True)
    ws3.append([])

    row(ws3, '四、本期現金增減', CF['net_change'], bold=True, total=True)
    row(ws3, '五、期初現金及約當現金', CF['opening_cash'], bold=True)
    row(ws3, '六、期末現金及約當現金', CF['closing_cash'], bold=True, total=True, dbl=True)

    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"statements_{year}{str(month).zfill(2)}.xlsx"
    return (buf.read(), 200, {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': f'attachment; filename="{fname}"',
    })


# ═══════════════════════════════════════════════════════════════════
# Feature 1: 定期自動分錄 (Recurring Entries)
# ═══════════════════════════════════════════════════════════════════

def _recurring_row(r):
    if not r: return None
    d = dict(r)
    for f in ('amount', 'tax_amount'):
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('start_date'): d['start_date'] = str(d['start_date'])
    if d.get('end_date'):   d['end_date']   = str(d['end_date'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

@app.route('/api/finance/recurring', methods=['GET'])
@require_module('finance')
def api_recurring_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT fr.*, fc.name as category_name
            FROM finance_recurring fr
            LEFT JOIN finance_categories fc ON fc.id=fr.category_id
            ORDER BY fr.active DESC, fr.id
        """).fetchall()
    result = []
    for r in rows:
        d = _recurring_row(r)
        d['category_name'] = r['category_name']
        result.append(d)
    return jsonify(result)

@app.route('/api/finance/recurring', methods=['POST'])
@require_module('finance')
def api_recurring_create():
    b = request.get_json(force=True)
    if not b.get('title','').strip(): return jsonify({'error': '標題為必填'}), 400
    if not b.get('start_date'):       return jsonify({'error': '開始日期為必填'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO finance_recurring
              (title, type, category_id, amount, tax_amount, vendor, note,
               frequency, day_of_month, start_date, end_date, active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING *
        """, (b['title'].strip(), b.get('type','expense'), b.get('category_id') or None,
              float(b.get('amount',0)), float(b.get('tax_amount',0)),
              b.get('vendor','').strip(), b.get('note','').strip(),
              b.get('frequency','monthly'), int(b.get('day_of_month',1) or 1),
              b['start_date'], b.get('end_date') or None)).fetchone()
    return jsonify(_recurring_row(row)), 201

@app.route('/api/finance/recurring/<int:rid>', methods=['PUT'])
@require_module('finance')
def api_recurring_update(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE finance_recurring SET
              title=%s, type=%s, category_id=%s, amount=%s, tax_amount=%s,
              vendor=%s, note=%s, frequency=%s, day_of_month=%s,
              start_date=%s, end_date=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b.get('title','').strip(), b.get('type','expense'), b.get('category_id') or None,
              float(b.get('amount',0)), float(b.get('tax_amount',0)),
              b.get('vendor','').strip(), b.get('note','').strip(),
              b.get('frequency','monthly'), int(b.get('day_of_month',1) or 1),
              b.get('start_date'), b.get('end_date') or None,
              bool(b.get('active', True)), rid)).fetchone()
    return jsonify(_recurring_row(row)) if row else ('', 404)

@app.route('/api/finance/recurring/<int:rid>', methods=['DELETE'])
@require_module('finance')
def api_recurring_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM finance_recurring WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

@app.route('/api/finance/recurring/generate', methods=['POST'])
@require_module('finance')
def api_recurring_generate():
    """為指定月份產生定期分錄（冪等：已產生則跳過）"""
    from datetime import date as _d, timedelta as _td
    import calendar as _cal
    b = request.get_json(force=True)
    month = b.get('month', '')  # YYYY-MM
    if not month:
        from datetime import datetime, timezone, timedelta
        month = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m')
    y, m = int(month[:4]), int(month[5:])
    days_in_month = _cal.monthrange(y, m)[1]

    created, skipped = 0, 0
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM finance_recurring
            WHERE active=TRUE
              AND start_date <= %s
              AND (end_date IS NULL OR end_date >= %s)
        """, (f"{month}-28", f"{month}-01")).fetchall()

        for r in rows:
            # Check already generated this month
            if r['last_generated'] == month:
                skipped += 1
                continue
            # Check frequency
            freq = r['frequency']
            start_m = r['start_date'].month
            if freq == 'quarterly' and (m - start_m) % 3 != 0:
                skipped += 1
                continue
            if freq == 'yearly' and m != start_m:
                skipped += 1
                continue
            # Determine record date
            day = min(int(r['day_of_month'] or 1), days_in_month)
            rec_date = _d(y, m, day)
            conn.execute("""
                INSERT INTO finance_records
                  (record_date, category_id, type, title, amount, tax_amount, vendor, note, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'auto-recurring')
            """, (rec_date, r['category_id'], r['type'], r['title'],
                  r['amount'], r['tax_amount'] or 0, r['vendor'] or '', r['note'] or ''))
            conn.execute("UPDATE finance_recurring SET last_generated=%s WHERE id=%s",
                         (month, r['id']))
            created += 1

    return jsonify({'created': created, 'skipped': skipped, 'month': month})


# ═══════════════════════════════════════════════════════════════════
# Feature 2: 銀行對帳 (Bank Reconciliation)
# ═══════════════════════════════════════════════════════════════════

def _bank_row(r):
    if not r: return None
    d = dict(r)
    if d.get('txn_date'):   d['txn_date']   = str(d['txn_date'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    return d

@app.route('/api/finance/bank/import', methods=['POST'])
@require_module('finance')
def api_bank_import():
    """匯入銀行對帳單 CSV"""
    import csv, io as _io
    file = request.files.get('file')
    if not file: return jsonify({'error': '請上傳 CSV 檔案'}), 400
    raw = file.read().decode('utf-8-sig', errors='replace')
    account_name = request.form.get('account_name', '').strip() or '銀行帳戶'
    import_batch = _dt.now(TW_TZ).strftime('%Y%m%d%H%M%S')

    reader = csv.reader(_io.StringIO(raw))
    rows_data = [r for r in reader if any(c.strip() for c in r)]
    if not rows_data: return jsonify({'error': 'CSV 無資料'}), 400

    # Auto-detect header row (skip rows where first column is not a date-like string)
    def _is_date(s):
        s = s.strip().replace('/', '-').replace('.', '-')
        for fmt in ('%Y-%m-%d','%Y-%m-%d','%m-%d-%Y','%d-%m-%Y'):
            try: _dt.strptime(s, fmt); return True
            except Exception: pass
        # ROC year: 民國 e.g. 113/01/15
        import re
        if re.match(r'^\d{2,3}[/-]\d{1,2}[/-]\d{1,2}$', s):
            parts = re.split(r'[/-]', s)
            if int(parts[0]) < 200: return True
        return False

    def _parse_date(s):
        import re
        s = s.strip()
        parts = re.split(r'[/\-\.]', s)
        if len(parts) == 3:
            if int(parts[0]) < 200:
                # ROC year
                y2 = int(parts[0]) + 1911
                return f"{y2}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
            if int(parts[0]) > 31:
                return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
            if int(parts[2]) > 31:
                return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
        return None

    def _parse_amount(s):
        import re
        s = re.sub(r'[,$\s]', '', str(s).strip())
        try: return float(s)
        except Exception: return None

    inserted = 0
    with get_db() as conn:
        for row in rows_data:
            if len(row) < 2: continue
            date_str = _parse_date(row[0])
            if not date_str: continue
            desc = row[1].strip() if len(row) > 1 else ''
            # Format: date, desc, debit, credit  OR  date, desc, amount
            if len(row) >= 4:
                debit  = _parse_amount(row[2])
                credit = _parse_amount(row[3])
                if debit and debit > 0:
                    conn.execute("""INSERT INTO bank_statements
                        (account_name,txn_date,amount,txn_type,description,import_batch)
                        VALUES (%s,%s,%s,'debit',%s,%s)
                    """, (account_name, date_str, debit, desc, import_batch))
                    inserted += 1
                if credit and credit > 0:
                    conn.execute("""INSERT INTO bank_statements
                        (account_name,txn_date,amount,txn_type,description,import_batch)
                        VALUES (%s,%s,%s,'credit',%s,%s)
                    """, (account_name, date_str, credit, desc, import_batch))
                    inserted += 1
            elif len(row) >= 3:
                amt = _parse_amount(row[2])
                if amt is not None and amt != 0:
                    txn_type = 'credit' if amt > 0 else 'debit'
                    conn.execute("""INSERT INTO bank_statements
                        (account_name,txn_date,amount,txn_type,description,import_batch)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (account_name, date_str, abs(amt), txn_type, desc, import_batch))
                    inserted += 1
    return jsonify({'inserted': inserted, 'batch': import_batch})

@app.route('/api/finance/bank/statements', methods=['GET'])
@require_module('finance')
def api_bank_statements():
    month = request.args.get('month', '')
    conds, params = ['TRUE'], []
    if month:
        conds.append("TO_CHAR(bs.txn_date,'YYYY-MM')=%s"); params.append(month)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT bs.*, fr.title as matched_title, fr.amount as matched_amount,
                   fr.record_date as matched_date
            FROM bank_statements bs
            LEFT JOIN finance_records fr ON fr.id=bs.matched_record_id
            WHERE {' AND '.join(conds)}
            ORDER BY bs.txn_date DESC, bs.id DESC
        """, params).fetchall()
    result = []
    for r in rows:
        d = _bank_row(r)
        d['matched_title']  = r['matched_title']
        d['matched_amount'] = float(r['matched_amount']) if r['matched_amount'] else None
        d['matched_date']   = str(r['matched_date']) if r['matched_date'] else None
        result.append(d)
    return jsonify(result)

@app.route('/api/finance/bank/statements/<int:sid>', methods=['DELETE'])
@require_module('finance')
def api_bank_statement_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM bank_statements WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})

@app.route('/api/finance/bank/match', methods=['POST'])
@require_module('finance')
def api_bank_match():
    b = request.get_json(force=True)
    sid = b.get('statement_id')
    rid = b.get('record_id')  # None to unmatch
    with get_db() as conn:
        if rid:
            conn.execute("UPDATE bank_statements SET reconciled=TRUE, matched_record_id=%s WHERE id=%s",
                         (rid, sid))
        else:
            conn.execute("UPDATE bank_statements SET reconciled=FALSE, matched_record_id=NULL WHERE id=%s",
                         (sid,))
    return jsonify({'ok': True})

@app.route('/api/finance/bank/auto-match', methods=['POST'])
@require_module('finance')
def api_bank_auto_match():
    """自動比對：相同金額且日期在 3 天內"""
    b = request.get_json(force=True)
    month = b.get('month', '')
    matched = 0
    with get_db() as conn:
        stmts = conn.execute("""
            SELECT * FROM bank_statements
            WHERE reconciled=FALSE
            """ + ("AND TO_CHAR(txn_date,'YYYY-MM')=%s" if month else ""),
            ([month] if month else [])).fetchall()
        for s in stmts:
            # Find finance record: same amount, date within ±3 days, same type direction
            ftype = 'income' if s['txn_type'] == 'credit' else 'expense'
            rec = conn.execute("""
                SELECT id FROM finance_records
                WHERE type=%s AND amount=%s
                  AND ABS(record_date - %s::date) <= 3
                  AND id NOT IN (
                      SELECT matched_record_id FROM bank_statements
                      WHERE matched_record_id IS NOT NULL
                  )
                ORDER BY ABS(record_date - %s::date), id
                LIMIT 1
            """, (ftype, s['amount'], s['txn_date'], s['txn_date'])).fetchone()
            if rec:
                conn.execute("""UPDATE bank_statements SET reconciled=TRUE, matched_record_id=%s
                                WHERE id=%s""", (rec['id'], s['id']))
                matched += 1
    return jsonify({'matched': matched})

@app.route('/api/finance/bank/summary', methods=['GET'])
@require_module('finance')
def api_bank_summary():
    month = request.args.get('month', '')
    cond = "AND TO_CHAR(txn_date,'YYYY-MM')=%s" if month else ""
    params = [month] if month else []
    with get_db() as conn:
        r = conn.execute(f"""
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN reconciled THEN 1 ELSE 0 END) as matched,
              SUM(CASE WHEN txn_type='credit' THEN amount ELSE 0 END) as total_credit,
              SUM(CASE WHEN txn_type='debit'  THEN amount ELSE 0 END) as total_debit,
              SUM(CASE WHEN reconciled AND txn_type='credit' THEN amount ELSE 0 END) as matched_credit,
              SUM(CASE WHEN reconciled AND txn_type='debit'  THEN amount ELSE 0 END) as matched_debit
            FROM bank_statements WHERE TRUE {cond}
        """, params).fetchone()
    d = dict(r)
    for k in d:
        if d[k] is not None: d[k] = float(d[k]) if isinstance(d[k], type(r['total_credit'])) else int(d[k])
    return jsonify(d)


# ═══════════════════════════════════════════════════════════════════
# Feature 3: 稅務申報準備 (Tax Filing Prep — Taiwan VAT 401)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/finance/tax/<int:year>/<int:period>', methods=['GET'])
@require_module('finance')
def api_finance_tax(year, period):
    """
    period: 1=Jan-Feb, 2=Mar-Apr, 3=May-Jun, 4=Jul-Aug, 5=Sep-Oct, 6=Nov-Dec
    """
    if period < 1 or period > 6:
        return jsonify({'error': '期別需為 1-6'}), 400
    m_start = (period - 1) * 2 + 1
    m_end   = m_start + 1
    months  = [f"{year}-{str(m).zfill(2)}" for m in range(m_start, m_end + 1)]

    with get_db() as conn:
        rows = conn.execute("""
            SELECT fr.type, fr.amount, fr.tax_amount, fr.title,
                   fr.vendor, fr.invoice_no, fr.record_date,
                   fc.name as category_name
            FROM finance_records fr
            LEFT JOIN finance_categories fc ON fc.id=fr.category_id
            WHERE TO_CHAR(fr.record_date,'YYYY-MM') = ANY(%s)
            ORDER BY fr.record_date, fr.type
        """, (months,)).fetchall()

    sales_rows    = [r for r in rows if r['type'] == 'income']
    purchase_rows = [r for r in rows if r['type'] == 'expense']

    sales_amount    = sum(float(r['amount'])     for r in sales_rows)
    sales_tax       = sum(float(r['tax_amount'] or 0) for r in sales_rows)
    purchase_amount = sum(float(r['amount'])     for r in purchase_rows)
    purchase_tax    = sum(float(r['tax_amount'] or 0) for r in purchase_rows)
    tax_payable     = round(sales_tax - purchase_tax, 2)

    def _fmt_row(r):
        return {
            'date':     str(r['record_date']),
            'title':    r['title'],
            'vendor':   r['vendor'] or '',
            'invoice_no': r['invoice_no'] or '',
            'amount':   float(r['amount']),
            'tax_amount': float(r['tax_amount'] or 0),
            'category': r['category_name'] or '未分類',
        }

    return jsonify({
        'year': year, 'period': period,
        'roc_year': year - 1911,
        'months': months,
        'sales': {
            'rows':   [_fmt_row(r) for r in sales_rows],
            'amount': round(sales_amount, 2),
            'tax':    round(sales_tax, 2),
        },
        'purchases': {
            'rows':   [_fmt_row(r) for r in purchase_rows],
            'amount': round(purchase_amount, 2),
            'tax':    round(purchase_tax, 2),
        },
        'tax_payable': tax_payable,
        'is_refund':   tax_payable < 0,
    })


# ═══════════════════════════════════════════════════════════════════
# Feature 4: 應收/應付帳款 (AR/AP Tracking)
# ═══════════════════════════════════════════════════════════════════

def _payable_row(r):
    if not r: return None
    d = dict(r)
    if d.get('due_date'):   d['due_date']   = str(d['due_date'])
    if d.get('paid_date'):  d['paid_date']  = str(d['paid_date'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    return d

@app.route('/api/finance/payables', methods=['GET'])
@require_module('finance')
def api_payables_list():
    from datetime import date as _d
    ptype  = request.args.get('type', '')      # receivable / payable
    status = request.args.get('status', '')    # open / paid / overdue
    conds, params = ['TRUE'], []
    if ptype:  conds.append("payable_type=%s"); params.append(ptype)
    if status == 'overdue':
        conds.append("status='open' AND due_date < CURRENT_DATE")
    elif status:
        conds.append("status=%s"); params.append(status)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT *, CURRENT_DATE - due_date AS days_overdue
            FROM finance_payables
            WHERE {' AND '.join(conds)}
            ORDER BY
              CASE WHEN status='open' AND due_date < CURRENT_DATE THEN 0
                   WHEN status='open' THEN 1
                   ELSE 2 END,
              due_date
        """, params).fetchall()
    result = []
    for r in rows:
        d = _payable_row(r)
        d['days_overdue'] = int(r['days_overdue']) if r['days_overdue'] is not None else 0
        # Compute effective status
        if d['status'] == 'open' and d.get('due_date') and str(_d.today()) > d['due_date']:
            d['effective_status'] = 'overdue'
        else:
            d['effective_status'] = d['status']
        result.append(d)
    return jsonify(result)

@app.route('/api/finance/payables', methods=['POST'])
@require_module('finance')
def api_payable_create():
    b = request.get_json(force=True)
    if not b.get('title','').strip(): return jsonify({'error': '標題為必填'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO finance_payables
              (payable_type, title, party_name, invoice_no, amount, due_date, status, note)
            VALUES (%s,%s,%s,%s,%s,%s,'open',%s) RETURNING *
        """, (b.get('payable_type','payable'), b['title'].strip(),
              b.get('party_name','').strip(), b.get('invoice_no','').strip(),
              float(b.get('amount',0)), b.get('due_date') or None,
              b.get('note','').strip())).fetchone()
    return jsonify(_payable_row(row)), 201

@app.route('/api/finance/payables/<int:pid>', methods=['PUT'])
@require_module('finance')
def api_payable_update(pid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE finance_payables SET
              payable_type=%s, title=%s, party_name=%s, invoice_no=%s,
              amount=%s, due_date=%s, status=%s,
              paid_date=%s, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b.get('payable_type','payable'), b.get('title','').strip(),
              b.get('party_name','').strip(), b.get('invoice_no','').strip(),
              float(b.get('amount',0)), b.get('due_date') or None,
              b.get('status','open'), b.get('paid_date') or None,
              b.get('note','').strip(), pid)).fetchone()
    return jsonify(_payable_row(row)) if row else ('', 404)

@app.route('/api/finance/payables/<int:pid>', methods=['DELETE'])
@require_module('finance')
def api_payable_delete(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM finance_payables WHERE id=%s", (pid,))
    return jsonify({'deleted': pid})

@app.route('/api/finance/payables/aging', methods=['GET'])
@require_module('finance')
def api_payables_aging():
    ptype = request.args.get('type', 'payable')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT *,
              CURRENT_DATE - due_date AS days_overdue
            FROM finance_payables
            WHERE payable_type=%s AND status='open'
        """, (ptype,)).fetchall()
    buckets = {'current': 0, 'd1_30': 0, 'd31_60': 0, 'd61_90': 0, 'd90plus': 0}
    bucket_rows = {'current': [], 'd1_30': [], 'd31_60': [], 'd61_90': [], 'd90plus': []}
    for r in rows:
        do = int(r['days_overdue']) if r['days_overdue'] is not None else 0
        d = _payable_row(r)
        d['days_overdue'] = do
        if do <= 0:    k = 'current'
        elif do <= 30: k = 'd1_30'
        elif do <= 60: k = 'd31_60'
        elif do <= 90: k = 'd61_90'
        else:          k = 'd90plus'
        buckets[k]      += float(r['amount'])
        bucket_rows[k].append(d)
    return jsonify({'buckets': buckets, 'rows': bucket_rows, 'type': ptype})


# ═══════════════════════════════════════════════════════════════════
# Feature 5: 預算管理 (Budget vs Actual)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/finance/budgets', methods=['GET'])
@require_module('finance')
def api_budgets_list():
    year  = request.args.get('year',  '')
    month = request.args.get('month', '')
    if not year or not month:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8)))
        year, month = str(now.year), str(now.month)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT fb.*, fc.name as category_name, fc.type as category_type, fc.color
            FROM finance_budgets fb
            JOIN finance_categories fc ON fc.id=fb.category_id
            WHERE fb.year=%s AND fb.month=%s
            ORDER BY fc.type, fc.sort_order
        """, (int(year), int(month))).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['budget_amount'] = float(d['budget_amount'])
        if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
        if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
        result.append(d)
    return jsonify(result)

@app.route('/api/finance/budgets', methods=['POST'])
@require_module('finance')
def api_budgets_save():
    """Upsert list of budgets: [{category_id, budget_amount}]"""
    b = request.get_json(force=True)
    year  = int(b.get('year',  0))
    month = int(b.get('month', 0))
    items = b.get('items', [])
    if not year or not month: return jsonify({'error': '年月為必填'}), 400
    with get_db() as conn:
        for it in items:
            cid = it.get('category_id')
            amt = float(it.get('budget_amount', 0))
            if cid is None: continue
            if amt == 0:
                conn.execute("DELETE FROM finance_budgets WHERE year=%s AND month=%s AND category_id=%s",
                             (year, month, cid))
            else:
                conn.execute("""
                    INSERT INTO finance_budgets (year, month, category_id, budget_amount, updated_at)
                    VALUES (%s,%s,%s,%s,NOW())
                    ON CONFLICT (year, month, category_id)
                    DO UPDATE SET budget_amount=EXCLUDED.budget_amount, updated_at=NOW()
                """, (year, month, cid, amt))
    return jsonify({'ok': True})

@app.route('/api/finance/budgets/vs-actual', methods=['GET'])
@require_module('finance')
def api_budgets_vs_actual():
    year  = request.args.get('year',  '')
    month = request.args.get('month', '')
    if not year or not month:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8)))
        year, month = str(now.year), str(now.month)
    period = f"{year}-{str(month).zfill(2)}"
    with get_db() as conn:
        cats = conn.execute("""
            SELECT id, name, type, color FROM finance_categories WHERE active=TRUE ORDER BY type, sort_order
        """).fetchall()
        budgets = conn.execute("""
            SELECT category_id, budget_amount FROM finance_budgets WHERE year=%s AND month=%s
        """, (int(year), int(month))).fetchall()
        actuals = conn.execute("""
            SELECT category_id, SUM(amount) as total
            FROM finance_records
            WHERE TO_CHAR(record_date,'YYYY-MM')=%s
            GROUP BY category_id
        """, (period,)).fetchall()
    budget_map = {r['category_id']: float(r['budget_amount']) for r in budgets}
    actual_map = {r['category_id']: float(r['total']) for r in actuals}
    result = []
    for c in cats:
        cid = c['id']
        bgt = budget_map.get(cid, 0)
        act = actual_map.get(cid, 0)
        pct = round(act / bgt * 100, 1) if bgt > 0 else None
        result.append({
            'category_id':   cid,
            'category_name': c['name'],
            'category_type': c['type'],
            'color':         c['color'],
            'budget':        bgt,
            'actual':        act,
            'remaining':     round(bgt - act, 2),
            'pct':           pct,
            'over_budget':   bgt > 0 and act > bgt,
        })
    return jsonify({'year': year, 'month': month, 'items': result})


# ═══════════════════════════════════════════════════════════════════
# Feature 6: 薪資費用連動 (Payroll → Finance)
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/finance/payroll/status', methods=['GET'])
@require_module('finance')
def api_payroll_status():
    """列出各月薪資是否已同步至財務"""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT month,
                   COUNT(*) as total,
                   SUM(CASE WHEN finance_synced THEN 1 ELSE 0 END) as synced,
                   SUM(net_pay) as total_net_pay
            FROM salary_records
            WHERE status IN ('confirmed','draft')
            GROUP BY month ORDER BY month DESC LIMIT 24
        """).fetchall()
    return jsonify([{
        'month':         r['month'],
        'total':         int(r['total']),
        'synced':        int(r['synced']),
        'total_net_pay': float(r['total_net_pay'] or 0),
        'all_synced':    int(r['synced']) == int(r['total']),
    } for r in rows])

@app.route('/api/finance/payroll/sync', methods=['POST'])
@require_module('finance')
def api_payroll_sync():
    """將指定月份已確認薪資寫入財務記錄"""
    b     = request.get_json(force=True)
    month = b.get('month', '')
    if not month: return jsonify({'error': '請提供月份'}), 400
    # Find or create 薪資支出 category
    with get_db() as conn:
        cat = conn.execute("""
            SELECT id FROM finance_categories WHERE name='薪資支出' AND type='expense' LIMIT 1
        """).fetchone()
        if not cat:
            cat = conn.execute("""
                INSERT INTO finance_categories (name,type,color,sort_order)
                VALUES ('薪資支出','expense','#e07b2a',11) RETURNING *
            """).fetchone()
        cat_id = cat['id']

        records = conn.execute("""
            SELECT sr.*, ps.name as staff_name
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.month=%s AND sr.finance_synced=FALSE
        """, (month,)).fetchall()

        if not records:
            return jsonify({'created': 0, 'message': '無需同步的薪資記錄'})

        record_date = f"{month}-{str(28).zfill(2)}"  # Use 28th as payroll date
        created = 0
        for sr in records:
            # Main salary entry
            conn.execute("""
                INSERT INTO finance_records
                  (record_date, category_id, type, title, amount, note, created_by)
                VALUES (%s,%s,'expense',%s,%s,%s,'payroll-sync')
            """, (record_date, cat_id,
                  f"{sr['staff_name']} {month} 薪資",
                  float(sr['net_pay']),
                  f"薪資記錄 #{sr['id']}"))
            conn.execute("UPDATE salary_records SET finance_synced=TRUE WHERE id=%s", (sr['id'],))
            created += 1

    return jsonify({'created': created, 'month': month})


# ── Tax → Finance sync ──────────────────────────────────────────

@app.route('/api/finance/tax/<int:year>/<int:period>/sync', methods=['POST'])
@require_module('finance')
def api_finance_tax_sync(year, period):
    """將應繳/退稅金額建立為財務分錄，流入損益表"""
    if period < 1 or period > 6:
        return jsonify({'error': '期別需為 1-6'}), 400
    m_start = (period - 1) * 2 + 1
    m_end   = m_start + 1
    months  = [f"{year}-{str(m).zfill(2)}" for m in range(m_start, m_end + 1)]
    roc_year = year - 1911

    with get_db() as conn:
        rows = conn.execute("""
            SELECT type, SUM(tax_amount) as tax_total
            FROM finance_records
            WHERE TO_CHAR(record_date,'YYYY-MM') = ANY(%s)
              AND tax_amount IS NOT NULL AND tax_amount <> 0
            GROUP BY type
        """, (months,)).fetchall()

    sales_tax    = sum(float(r['tax_total']) for r in rows if r['type'] == 'income')
    purchase_tax = sum(float(r['tax_total']) for r in rows if r['type'] == 'expense')
    tax_payable  = round(sales_tax - purchase_tax, 2)

    if tax_payable == 0:
        return jsonify({'created': 0, 'message': '稅額為零，無需建立分錄'})

    # Record date = last day of period's last month
    import calendar as _cal
    record_date = f"{year}-{str(m_end).zfill(2)}-{_cal.monthrange(year, m_end)[1]}"
    note = f"銷項稅 ${round(sales_tax,0):,.0f} − 進項稅 ${round(purchase_tax,0):,.0f} = {'應繳' if tax_payable>0 else '退稅'} ${abs(round(tax_payable,0)):,.0f}"
    period_label = f"民國{roc_year}年第{period}期（{months[0]}～{months[-1]}）"

    created = 0
    with get_db() as conn:
        if tax_payable > 0:
            # 應繳稅款 → expense under 稅費
            cat = conn.execute(
                "SELECT id FROM finance_categories WHERE name='稅費' AND type='expense' LIMIT 1"
            ).fetchone()
            if not cat:
                cat = conn.execute("""
                    INSERT INTO finance_categories (name, type, color, sort_order, statement_section)
                    VALUES ('稅費','expense','#8892a4', 99,'operating_expense') RETURNING *
                """).fetchone()
            conn.execute("""
                INSERT INTO finance_records
                  (record_date, category_id, type, title, amount, tax_amount, note, created_by)
                VALUES (%s,%s,'expense',%s,%s,0,%s,'tax-sync')
            """, (record_date, cat['id'],
                  f"應繳營業稅 {period_label}", tax_payable, note))
            created += 1
        else:
            # 退稅 → income under 其他收入
            cat = conn.execute(
                "SELECT id FROM finance_categories WHERE name='其他收入' AND type='income' LIMIT 1"
            ).fetchone()
            if not cat:
                cat = conn.execute("""
                    INSERT INTO finance_categories (name, type, color, sort_order, statement_section)
                    VALUES ('其他收入','income','#c8a96e', 99,'other_revenue') RETURNING *
                """).fetchone()
            conn.execute("""
                INSERT INTO finance_records
                  (record_date, category_id, type, title, amount, tax_amount, note, created_by)
                VALUES (%s,%s,'income',%s,%s,0,%s,'tax-sync')
            """, (record_date, cat['id'],
                  f"營業稅退稅 {period_label}", abs(tax_payable), note))
            created += 1

    return jsonify({'created': created, 'tax_payable': tax_payable, 'record_date': record_date})


# ═══════════════════════════════════════════════════════════════════
# LINE Broadcast Helper
# ═══════════════════════════════════════════════════════════════════

def _broadcast_announcement_line(title, content):
    """廣播公告給所有已綁定 LINE 的在職員工"""
    try:
        with get_db() as conn:
            cfg = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
            if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
                return
            staff_rows = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE active=TRUE AND line_user_id IS NOT NULL"
            ).fetchall()
        if not staff_rows:
            return
        api = LineBotApi(cfg['channel_access_token'])
        snippet = content[:60] + ('…' if len(content) > 60 else '')
        msg = f"[公告] {title}\n{snippet}\n\n請至員工系統查看完整公告。"
        for s in staff_rows:
            try:
                api.push_message(s['line_user_id'], TextSendMessage(text=msg))
            except Exception as e:
                print(f"[LINE broadcast] {s['line_user_id']}: {e}")
    except Exception as e:
        print(f"[LINE broadcast] error: {e}")


# ═══════════════════════════════════════════════════════════════════
# Expense Claims 費用報帳申請
# ═══════════════════════════════════════════════════════════════════

def _init_expense_db():
    sqls = [
        """CREATE TABLE IF NOT EXISTS expense_claims (
            id                SERIAL PRIMARY KEY,
            staff_id          INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            title             TEXT NOT NULL,
            amount            NUMERIC(12,2) NOT NULL DEFAULT 0,
            expense_date      DATE NOT NULL,
            category          TEXT DEFAULT '',
            note              TEXT DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'pending',
            document_id       INT REFERENCES finance_documents(id) ON DELETE SET NULL,
            review_note       TEXT DEFAULT '',
            reviewed_by       TEXT DEFAULT '',
            reviewed_at       TIMESTAMPTZ,
            finance_record_id INT REFERENCES finance_records(id) ON DELETE SET NULL,
            created_at        TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS document_id2 INT REFERENCES finance_documents(id) ON DELETE SET NULL",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS reimbursement_method TEXT NOT NULL DEFAULT '匯款'",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS bank_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS bank_account TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS account_holder TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS expense_type TEXT NOT NULL DEFAULT '支出'",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS company TEXT NOT NULL DEFAULT '進光設計'",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS vendor TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_staff_id ON expense_claims(staff_id)",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_status ON expense_claims(status)",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[expense_init] {e}")

_init_expense_db()


def _expense_row(r):
    if not r: return None
    d = dict(r)
    if d.get('expense_date'): d['expense_date'] = str(d['expense_date'])
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    return d


# ── Employee endpoints ──────────────────────────────────────────

@app.route('/api/expense/my-claims', methods=['GET'])
def api_expense_my_list():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM expense_claims WHERE staff_id=%s ORDER BY created_at DESC LIMIT 50
        """, (sid,)).fetchall()
    return jsonify([_expense_row(r) for r in rows])


@app.route('/api/expense/my-claims', methods=['POST'])
def api_expense_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    b = request.get_json(force=True)
    if not b.get('title','').strip():  return jsonify({'error': '請填寫標題'}), 400
    if not b.get('expense_date'):      return jsonify({'error': '請填寫費用日期'}), 400
    try:
        amount = float(b.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'error': '金額格式錯誤'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO expense_claims
              (staff_id, title, amount, expense_date, category, note, document_id, expense_type, company, vendor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, b['title'].strip(), amount,
              b['expense_date'], b.get('category','').strip(),
              b.get('note','').strip(), b.get('document_id') or None,
              b.get('expense_type', '支出').strip(),
              b.get('company', '進光設計').strip(),
              b.get('vendor', '').strip())).fetchone()
    return jsonify(_expense_row(row)), 201


@app.route('/api/expense/ocr', methods=['POST'])
def api_expense_ocr():
    """員工自助 OCR — 複用 finance OCR 邏輯"""
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    import anthropic as _ant, base64, re as _re2
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': '尚未設定 ANTHROPIC_API_KEY'}), 500
    file = request.files.get('file')
    if not file: return jsonify({'error': '請上傳圖片'}), 400
    raw = file.read()
    media_type = file.content_type or 'image/jpeg'
    if media_type not in ('image/jpeg','image/png','image/gif','image/webp'):
        media_type = 'image/jpeg'
    img_b64 = base64.standard_b64encode(raw).decode()
    client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6', max_tokens=512,
            messages=[{'role':'user','content':[
                {'type':'image','source':{'type':'base64','media_type':media_type,'data':img_b64}},
                {'type':'text','text':'請辨識此收據或發票，以JSON格式回傳：{"date":"YYYY-MM-DD","vendor":"廠商","title":"建議標題","total_amount":數字,"doc_type":"receipt或invoice"}\n只回傳JSON。'}
            ]}]
        )
        text = msg.content[0].text.strip()
        text = _re2.sub(r'^```json\s*','',text,flags=_re2.MULTILINE)
        text = _re2.sub(r'\s*```$','',text,flags=_re2.MULTILINE)
        result = _json.loads(text)
    except Exception as e:
        return jsonify({'error': f'OCR 失敗：{e}'}), 500
    try:
        with get_db() as conn:
            doc = conn.execute("""
                INSERT INTO finance_documents (filename, doc_type, ocr_raw)
                VALUES (%s,%s,%s) RETURNING id
            """, (file.filename, result.get('doc_type',''), _json.dumps(result))).fetchone()
        result['document_id'] = doc['id']
    except Exception as e:
        print(f"[expense_ocr doc] {e}")
    return jsonify(result)


# ── Admin endpoints ─────────────────────────────────────────────

@app.route('/api/expense/admin-upload', methods=['POST'])
@login_required
def api_expense_admin_upload():
    """管理員上傳費用附件（不做 OCR 自動填入，僅存檔回傳 document_id）"""
    import base64 as _b64, re as _re3
    file = request.files.get('file')
    if not file: return jsonify({'error': '請上傳圖片'}), 400
    media_type = file.content_type or 'image/jpeg'
    if media_type not in ('image/jpeg','image/png','image/gif','image/webp'):
        media_type = 'image/jpeg'
    result = {}
    raw = file.read()  # 一次讀完，後續共用
    if ANTHROPIC_API_KEY:
        try:
            import anthropic as _ant
            img_b64 = _b64.standard_b64encode(raw).decode()
            client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=256,
                messages=[{'role':'user','content':[
                    {'type':'image','source':{'type':'base64','media_type':media_type,'data':img_b64}},
                    {'type':'text','text':'請辨識此收據或發票，以JSON格式回傳：{"date":"YYYY-MM-DD","vendor":"廠商","title":"建議標題","total_amount":數字,"doc_type":"receipt或invoice"}\n只回傳JSON。'}
                ]}]
            )
            text = msg.content[0].text.strip()
            text = _re3.sub(r'^```json\s*','',text,flags=_re3.MULTILINE)
            text = _re3.sub(r'\s*```$','',text,flags=_re3.MULTILINE)
            result = _json.loads(text)
        except Exception:
            pass
    try:
        with get_db() as conn:
            doc = conn.execute("""
                INSERT INTO finance_documents (filename, doc_type, ocr_raw)
                VALUES (%s,%s,%s) RETURNING id
            """, (file.filename, result.get('doc_type','receipt'), _json.dumps(result))).fetchone()
        result['document_id'] = doc['id']
    except Exception as e:
        return jsonify({'error': f'儲存失敗：{e}'}), 500
    return jsonify(result)


@app.route('/api/expense/claims/admin-create', methods=['POST'])
@login_required
def api_expense_admin_create():
    """管理員代員工新增費用申請"""
    b = request.get_json(force=True)
    staff_id = b.get('staff_id')
    if not staff_id:           return jsonify({'error': '請選擇員工'}), 400
    if not b.get('expense_date'):     return jsonify({'error': '請填寫費用日期'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO expense_claims
              (staff_id, title, amount, expense_date, category, note,
               document_id, document_id2,
               reimbursement_method, bank_name, bank_account, account_holder,
               expense_type, company, vendor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (staff_id, (b.get('title','').strip() or b.get('category','').strip() or '費用申請'), float(b.get('amount', 0)),
              b['expense_date'], b.get('category','').strip(),
              b.get('note','').strip(),
              b.get('document_id') or None,
              b.get('document_id2') or None,
              b.get('reimbursement_method', '匯款').strip(),
              b.get('bank_name', '').strip(),
              b.get('bank_account', '').strip(),
              b.get('account_holder', '').strip(),
              b.get('expense_type', '支出').strip(),
              b.get('company', '進光設計').strip(),
              b.get('vendor', '').strip())).fetchone()
        staff = conn.execute(
            "SELECT name, employee_code FROM punch_staff WHERE id=%s", (staff_id,)
        ).fetchone()
    d = _expense_row(row)
    if staff:
        d['staff_name']    = staff['name']
        d['employee_code'] = staff['employee_code'] or ''
    return jsonify(d), 201


@app.route('/api/expense/claims', methods=['GET'])
@login_required
def api_expense_admin_list():
    status = request.args.get('status', '')
    ym     = request.args.get('ym', '')   # YYYY-MM
    conds, params = ['TRUE'], []
    if status: conds.append("ec.status=%s");                              params.append(status)
    if ym:     conds.append("TO_CHAR(ec.expense_date,'YYYY-MM')=%s");    params.append(ym)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT ec.*, ps.name as staff_name, ps.employee_code
            FROM expense_claims ec
            JOIN punch_staff ps ON ps.id=ec.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY ec.expense_date ASC, ec.created_at ASC
        """, params).fetchall()
    result = []
    for r in rows:
        d = _expense_row(r)
        d['staff_name']    = r['staff_name']
        d['employee_code'] = r['employee_code']
        result.append(d)
    return jsonify(result)


@app.route('/api/expense/claims/<int:cid>', methods=['PUT'])
@login_required
def api_expense_review(cid):
    b      = request.get_json(force=True)
    action = b.get('action')  # approve / reject
    if action not in ('approve','reject'):
        return jsonify({'error': 'invalid action'}), 400
    reviewed_by  = session.get('admin_display_name','管理員')
    review_note  = b.get('review_note','').strip()
    new_status   = 'approved' if action == 'approve' else 'rejected'
    finance_rid  = None

    with get_db() as conn:
        claim = conn.execute("SELECT * FROM expense_claims WHERE id=%s", (cid,)).fetchone()
        if not claim: return ('', 404)

        if action == 'approve' and b.get('create_finance_record', True):
            # 費用類型 → 財務分類名稱對應
            _cat_map = {
                '餐費':        '食材成本',
                '辦公用品':    '消耗品',
                '交通費':      '其他支出',
                '住宿費':      '其他支出',
                '廠商費用':    '其他支出',
                '固定營業費用': '其他支出',
                '健保費用':    '薪資支出',
                '勞工保險金費用': '薪資支出',
                '勞工退休金費用': '薪資支出',
                '稅金費用':    '其他支出',
                '雇員獎金費用': '薪資支出',
                '其它雜支費用': '其他支出',
                '其他':        '其他支出',
            }
            claim_category = (claim.get('category') or '').strip()
            target_cat_name = _cat_map.get(claim_category, '其他支出')
            # 先按名稱找，找不到再取第一個 expense 分類
            cat = conn.execute(
                "SELECT id FROM finance_categories WHERE type='expense' AND active=TRUE AND name=%s LIMIT 1",
                (target_cat_name,)
            ).fetchone()
            if not cat:
                cat = conn.execute(
                    "SELECT id FROM finance_categories WHERE type='expense' AND active=TRUE ORDER BY sort_order LIMIT 1"
                ).fetchone()
            note_parts = [f"報帳申請 #{cid}"]
            if claim.get('note'): note_parts.append(claim['note'])
            if claim.get('document_id2'): note_parts.append(f"附件2 doc#{claim['document_id2']}")
            frec = conn.execute("""
                INSERT INTO finance_records
                  (record_date, category_id, type, title, amount, note, document_id, created_by)
                VALUES (%s,%s,'expense',%s,%s,%s,%s,'expense-claim') RETURNING id
            """, (claim['expense_date'], cat['id'] if cat else None,
                  claim['title'], claim['amount'],
                  '：'.join(note_parts),
                  claim['document_id'])).fetchone()
            finance_rid = frec['id']

        row = conn.execute("""
            UPDATE expense_claims SET
              status=%s, reviewed_by=%s, review_note=%s,
              reviewed_at=NOW(), finance_record_id=%s
            WHERE id=%s RETURNING *
        """, (new_status, reviewed_by, review_note, finance_rid, cid)).fetchone()
        staff = conn.execute(
            "SELECT name, employee_code FROM punch_staff WHERE id=%s", (claim['staff_id'],)
        ).fetchone() if row else None

    if row:
        extra = f"標題：{claim['title']}　金額：${float(claim['amount']):,.0f}"
        if review_note: extra += f"\n意見：{review_note}"
        _notify_review_result(claim['staff_id'], '費用報帳', action, extra)
        d = _expense_row(row)
        if staff:
            d['staff_name']    = staff['name']
            d['employee_code'] = staff['employee_code'] or ''
        _badges_cache.clear()
        return jsonify(d)
    return ('', 404)


@app.route('/api/expense/claims/<int:cid>', methods=['PATCH'])
@login_required
def api_expense_claim_edit(cid):
    b = request.get_json(force=True)
    allowed = ['expense_date', 'expense_type', 'category', 'vendor', 'amount',
               'note', 'reimbursement_method', 'bank_name', 'bank_account',
               'account_holder', 'title']
    sets, vals = [], []
    for key in allowed:
        if key in b:
            sets.append(f"{key}=%s")
            vals.append(b[key])
    if not sets:
        return jsonify({'error': 'nothing to update'}), 400
    vals.append(cid)
    with get_db() as conn:
        row = conn.execute(
            f"UPDATE expense_claims SET {', '.join(sets)} WHERE id=%s RETURNING *", vals
        ).fetchone()
        if not row:
            return ('', 404)
        staff = conn.execute(
            "SELECT name, employee_code FROM punch_staff WHERE id=%s", (row['staff_id'],)
        ).fetchone()
    d = _expense_row(row)
    if staff:
        d['staff_name']    = staff['name']
        d['employee_code'] = staff['employee_code'] or ''
    return jsonify(d)


@app.route('/api/expense/claims/<int:cid>', methods=['DELETE'])
@login_required
def api_expense_claim_delete(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM expense_claims WHERE id=%s", (cid,))
    return jsonify({'deleted': cid})


# ═══════════════════════════════════════════════════════════════════════════
# 績效考核模組
# ═══════════════════════════════════════════════════════════════════════════

def _init_performance_db():
    sqls = [
        """CREATE TABLE IF NOT EXISTS performance_templates (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            period      TEXT DEFAULT 'quarterly',
            items       JSONB DEFAULT '[]',
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS performance_reviews (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            template_id     INT REFERENCES performance_templates(id) ON DELETE SET NULL,
            period_label    TEXT NOT NULL,
            scores          JSONB DEFAULT '{}',
            total_score     NUMERIC(6,2) DEFAULT 0,
            max_score       NUMERIC(6,2) DEFAULT 100,
            grade           TEXT DEFAULT '',
            comments        TEXT DEFAULT '',
            reviewer        TEXT DEFAULT '',
            salary_adjusted BOOLEAN DEFAULT FALSE,
            salary_delta    NUMERIC(12,2) DEFAULT 0,
            reviewed_at     TIMESTAMPTZ DEFAULT NOW(),
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS performance_config (
            key        TEXT PRIMARY KEY,
            value      JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[perf_init] {e}")

_init_performance_db()

_DEFAULT_GRADE_CONFIG = [
    {'grade': 'A', 'label': '優秀', 'min_pct': 90},
    {'grade': 'B', 'label': '良好', 'min_pct': 75},
    {'grade': 'C', 'label': '待加強', 'min_pct': 60},
    {'grade': 'D', 'label': '需改善', 'min_pct':  0},
]

def _get_grade_config():
    """從 DB 讀取評級設定，若未設定則回傳預設值（按門檻由高到低排序）。"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM performance_config WHERE key='grade_config'"
            ).fetchone()
        if row:
            cfg = row['value']
            if isinstance(cfg, str):
                cfg = _json.loads(cfg)
            if isinstance(cfg, list) and cfg:
                return sorted(cfg, key=lambda x: -float(x.get('min_pct', 0)))
    except Exception:
        pass
    return _DEFAULT_GRADE_CONFIG

def _grade_labels():
    return {c['grade']: c['label'] for c in _get_grade_config()}


def _perf_template_row(r):
    if not r: return None
    d = dict(r)
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if isinstance(d.get('items'), str):
        try: d['items'] = _json.loads(d['items'])
        except (ValueError, TypeError): d['items'] = []
    return d

def _perf_review_row(r):
    if not r: return None
    d = dict(r)
    for f in ('reviewed_at', 'created_at'):
        if d.get(f): d[f] = d[f].isoformat()
    if isinstance(d.get('scores'), str):
        try: d['scores'] = _json.loads(d['scores'])
        except (ValueError, TypeError): d['scores'] = {}
    if d.get('total_score') is not None: d['total_score'] = float(d['total_score'])
    if d.get('max_score')   is not None: d['max_score']   = float(d['max_score'])
    if d.get('salary_delta')is not None: d['salary_delta']= float(d['salary_delta'])
    return d

def _score_to_grade(pct):
    for cfg in _get_grade_config():
        if pct >= cfg['min_pct']:
            return cfg['grade']
    return _get_grade_config()[-1]['grade']

# ── 考核範本 CRUD ───────────────────────────────────────────────

@app.route('/api/performance/templates', methods=['GET'])
@login_required
def api_perf_templates_list():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM performance_templates ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([_perf_template_row(r) for r in rows])

@app.route('/api/performance/templates', methods=['POST'])
@login_required
def api_perf_template_create():
    b = request.get_json(force=True)
    name = (b.get('name') or '').strip()
    if not name: return jsonify({'error': '請填寫範本名稱'}), 400
    items = b.get('items', [])
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO performance_templates (name, description, period, items)
            VALUES (%s,%s,%s,%s) RETURNING *
        """, (name, b.get('description',''), b.get('period','quarterly'),
              _json.dumps(items))).fetchone()
    return jsonify(_perf_template_row(row)), 201

@app.route('/api/performance/templates/<int:tid>', methods=['PUT'])
@login_required
def api_perf_template_update(tid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE performance_templates
            SET name=%s, description=%s, period=%s, items=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b.get('name','').strip(), b.get('description',''),
              b.get('period','quarterly'), _json.dumps(b.get('items',[])),
              bool(b.get('active', True)), tid)).fetchone()
    return jsonify(_perf_template_row(row)) if row else ('', 404)

@app.route('/api/performance/templates/<int:tid>', methods=['DELETE'])
@login_required
def api_perf_template_delete(tid):
    with get_db() as conn:
        conn.execute("DELETE FROM performance_templates WHERE id=%s", (tid,))
    return jsonify({'deleted': tid})

# ── 考核記錄 CRUD ───────────────────────────────────────────────

@app.route('/api/performance/reviews', methods=['GET'])
@login_required
def api_perf_reviews_list():
    staff_id = request.args.get('staff_id')
    period   = request.args.get('period')
    conds, params = ['TRUE'], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if period:   conds.append("pr.period_label ILIKE %s"); params.append(f'%{period}%')
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*,
                   ps.name  AS staff_name,  ps.role   AS staff_role,
                   pt.name  AS tpl_name
            FROM performance_reviews pr
            JOIN punch_staff         ps ON ps.id = pr.staff_id
            LEFT JOIN performance_templates pt ON pt.id = pr.template_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.reviewed_at DESC
        """, params).fetchall()
    result = []
    for r in rows:
        d = _perf_review_row(r)
        d['staff_name']   = r['staff_name']
        d['staff_role']   = r['staff_role']
        d['template_name'] = r['tpl_name'] or ''
        result.append(d)
    return jsonify(result)

@app.route('/api/performance/reviews', methods=['POST'])
@login_required
def api_perf_review_create():
    b           = request.get_json(force=True)
    staff_id    = b.get('staff_id')
    template_id = b.get('template_id')
    period_label= (b.get('period_label') or '').strip()
    scores      = b.get('scores', {})
    comments    = (b.get('comments') or '').strip()
    reviewer    = (b.get('reviewer') or '').strip() or session.get('admin_display_name', '管理員')

    if not staff_id or not period_label:
        return jsonify({'error': '請選擇員工及考核期間'}), 400

    # Calculate total & grade from template items
    total = 0.0; max_s = 100.0
    if template_id:
        with get_db() as conn:
            tpl = conn.execute(
                "SELECT items FROM performance_templates WHERE id=%s", (template_id,)
            ).fetchone()
        if tpl:
            items = tpl.get('items') or []
            if isinstance(items, str):
                try: items = _json.loads(items)
                except (ValueError, TypeError): items = []
            if items:
                max_s = sum(float(it.get('max_score', 10)) for it in items)
                total = sum(
                    float(scores.get(str(it.get('id', it.get('name',''))), 0))
                    for it in items
                )
    else:
        total = float(b.get('total_score', 0))
        max_s = float(b.get('max_score', 100))

    pct   = (total / max_s * 100) if max_s > 0 else 0
    grade = _score_to_grade(pct)

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO performance_reviews
              (staff_id, template_id, period_label, scores, total_score,
               max_score, grade, comments, reviewer, reviewed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING *
        """, (staff_id, template_id or None, period_label,
              _json.dumps(scores), round(total, 2), round(max_s, 2),
              grade, comments, reviewer)).fetchone()
        staff = conn.execute(
            "SELECT name FROM punch_staff WHERE id=%s", (staff_id,)
        ).fetchone()

    # LINE 通知
    grade_labels = _grade_labels()
    msg = (f"[績效考核] {period_label} 考核結果\n"
           f"總分：{total:.1f} / {max_s:.0f}（{pct:.0f}%）\n"
           f"評級：{grade} {grade_labels.get(grade,'')}\n"
           f"考核人：{reviewer}\n"
           + (f"備注：{comments[:60]}\n" if comments else '')
           + "請至員工系統查看詳情。")
    _notify_staff_line(staff_id, msg)

    d = _perf_review_row(row)
    d['staff_name'] = staff['name'] if staff else ''
    return jsonify(d), 201

@app.route('/api/performance/reviews/<int:rid>', methods=['PUT'])
@login_required
def api_perf_review_update(rid):
    b        = request.get_json(force=True)
    scores   = b.get('scores', {})
    comments = (b.get('comments') or '').strip()
    with get_db() as conn:
        rev = conn.execute(
            "SELECT * FROM performance_reviews WHERE id=%s", (rid,)
        ).fetchone()
        if not rev: return ('', 404)
        # Recalculate score
        total = float(b.get('total_score', rev['total_score']))
        max_s = float(b.get('max_score',   rev['max_score']))
        pct   = (total / max_s * 100) if max_s > 0 else 0
        grade = _score_to_grade(pct)
        row = conn.execute("""
            UPDATE performance_reviews
            SET scores=%s, total_score=%s, max_score=%s, grade=%s,
                comments=%s, reviewed_at=NOW()
            WHERE id=%s RETURNING *
        """, (_json.dumps(scores), round(total,2), round(max_s,2),
              grade, comments, rid)).fetchone()
    return jsonify(_perf_review_row(row)) if row else ('', 404)

@app.route('/api/performance/reviews/<int:rid>/adjust-salary', methods=['POST'])
@login_required
def api_perf_adjust_salary(rid):
    """依考核結果調薪 — 直接更新員工底薪並記錄"""
    b     = request.get_json(force=True)
    delta = float(b.get('salary_delta', b.get('delta', 0)))
    note  = (b.get('note') or '').strip()
    if delta == 0: return jsonify({'error': '調薪金額不可為 0'}), 400
    with get_db() as conn:
        rev = conn.execute(
            "SELECT * FROM performance_reviews WHERE id=%s", (rid,)
        ).fetchone()
        if not rev: return ('', 404)
        staff = conn.execute(
            "SELECT id, name, base_salary FROM punch_staff WHERE id=%s", (rev['staff_id'],)
        ).fetchone()
        if not staff: return ('', 404)
        new_salary = float(staff['base_salary'] or 0) + delta
        conn.execute(
            "UPDATE punch_staff SET base_salary=%s WHERE id=%s",
            (new_salary, staff['id'])
        )
        conn.execute("""
            UPDATE performance_reviews
            SET salary_adjusted=TRUE, salary_delta=%s
            WHERE id=%s
        """, (delta, rid))

    direction = '調升' if delta > 0 else '調降'
    msg = (f"[薪資調整] 績效考核連動\n"
           f"考核期：{rev['period_label']}　評級：{rev['grade']}\n"
           f"{direction} NT$ {abs(delta):,.0f}\n"
           f"新底薪：NT$ {new_salary:,.0f}\n"
           + (f"說明：{note}" if note else ''))
    _notify_staff_line(staff['id'], msg)

    return jsonify({'ok': True, 'new_salary': new_salary, 'delta': delta})

# ── 員工查自己的考核 ────────────────────────────────────────────

@app.route('/api/performance/my-reviews', methods=['GET'])
def api_perf_my_reviews():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.*, pt.name AS tpl_name
            FROM performance_reviews pr
            LEFT JOIN performance_templates pt ON pt.id=pr.template_id
            WHERE pr.staff_id=%s
            ORDER BY pr.reviewed_at DESC LIMIT 10
        """, (sid,)).fetchall()
    result = []
    for r in rows:
        d = _perf_review_row(r)
        d['template_name'] = r['tpl_name'] or ''
        result.append(d)
    return jsonify(result)


# ── 評級設定 CRUD ───────────────────────────────────────────────

@app.route('/api/performance/config', methods=['GET'])
@login_required
def api_perf_config_get():
    return jsonify({'grades': _get_grade_config()})

@app.route('/api/performance/config', methods=['PUT'])
@login_required
def api_perf_config_update():
    b      = request.get_json(force=True)
    grades = b.get('grades', [])
    if not grades:
        return jsonify({'error': '請至少設定一個評級'}), 400
    for g in grades:
        if not str(g.get('grade', '')).strip() or not str(g.get('label', '')).strip():
            return jsonify({'error': '評級代碼與標籤不可為空'}), 400
        pct = g.get('min_pct')
        if pct is None or not (0 <= float(pct) <= 100):
            return jsonify({'error': '門檻百分比需介於 0~100'}), 400
    # 確保至少有一個門檻為 0，避免無法分級
    if not any(float(g.get('min_pct', -1)) == 0 for g in grades):
        return jsonify({'error': '必須有一個評級的門檻設為 0%（作為最低等級）'}), 400
    grades_sorted = sorted(
        [{'grade': str(g['grade']).strip(), 'label': str(g['label']).strip(),
          'min_pct': float(g['min_pct'])} for g in grades],
        key=lambda x: -x['min_pct']
    )
    with get_db() as conn:
        conn.execute("""
            INSERT INTO performance_config (key, value, updated_at)
            VALUES ('grade_config', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (_json.dumps(grades_sorted),))
    return jsonify({'ok': True, 'grades': grades_sorted})


# ═══════════════════════════════════════════════════════════════════════════
# LINE Bot 雙向查詢擴充
# ═══════════════════════════════════════════════════════════════════════════

def _line_query_leave_balance(staff, user_id):
    """查詢員工本年度假期餘額"""
    from datetime import date as _dlb
    year = _dlb.today().year
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT lb.total_days, lb.used_days, lt.name AS type_name
                FROM leave_balances lb
                JOIN leave_types lt ON lt.id=lb.leave_type_id
                WHERE lb.staff_id=%s AND lb.year=%s
                ORDER BY lt.sort_order
            """, (staff['id'], year)).fetchall()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return
    if not rows:
        _send_line_punch(user_id, f'📋 {staff["name"]} {year} 年\n尚無假期餘額記錄，請聯絡管理員。')
        return
    lines = [f'📋 {staff["name"]} {year} 年假期餘額']
    for r in rows:
        total = float(r['total_days'] or 0)
        used  = float(r['used_days']  or 0)
        remain= total - used
        bar   = '▓' * int(remain) + '░' * max(0, int(total - remain))
        lines.append(f'\n【{r["type_name"]}】\n  剩餘 {remain:.1f} 天 / 共 {total:.0f} 天\n  {bar}')
    _send_line_punch(user_id, '\n'.join(lines))


def _line_query_salary(staff, user_id):
    """查詢員工最近一筆薪資記錄"""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT month, net_pay, base_salary, allowance_total, deduction_total, status
                FROM salary_records
                WHERE staff_id=%s
                ORDER BY month DESC LIMIT 1
            """, (staff['id'],)).fetchone()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return
    if not row:
        _send_line_punch(user_id, f'📊 {staff["name"]}\n尚無薪資記錄。')
        return
    status_map = {'draft':'草稿', 'confirmed':'已確認', 'paid':'已發放'}
    _send_line_punch(user_id,
        f'📊 {staff["name"]} {row["month"]} 薪資\n\n'
        f'底薪：NT$ {float(row["base_salary"] or 0):,.0f}\n'
        f'津貼：NT$ {float(row["allowance_total"] or 0):,.0f}\n'
        f'扣除：NT$ {float(row["deduction_total"] or 0):,.0f}\n'
        f'━━━━━━━━━━━━\n'
        f'實領：NT$ {float(row["net_pay"] or 0):,.0f}\n'
        f'狀態：{status_map.get(row["status"], row["status"])}\n\n'
        f'詳細資訊請至員工系統薪資單查看。')


def _line_submit_leave(staff, user_id, text):
    """
    解析並建立請假申請。
    格式：請假 [假別] [開始日期] [結束日期(選填)] [原因(選填)]
    範例：請假 特休 2026-04-01
         請假 事假 2026-04-01 2026-04-02 家庭事務
    """
    import re as _re_lv
    from datetime import date as _dlv
    parts = text.strip().split()
    # parts[0] = '請假'
    if len(parts) < 3:
        _send_line_punch(user_id,
            '請假格式：\n請假 [假別] [日期]\n\n'
            '範例：\n請假 特休 2026-04-01\n請假 事假 2026-04-01 2026-04-02 家庭事務\n\n'
            '輸入「假別」查看可用假別。')
        return

    leave_type_name = parts[1]
    date_str1 = parts[2]
    date_str2 = parts[3] if len(parts) > 3 and _re_lv.match(r'\d{4}-\d{2}-\d{2}', parts[3]) else date_str1
    reason = ' '.join(parts[4:]) if len(parts) > 4 else '（LINE 請假）'
    if date_str2 == date_str1 and len(parts) > 3 and not _re_lv.match(r'\d{4}-\d{2}-\d{2}', parts[3]):
        reason = ' '.join(parts[3:])

    # Validate dates
    try:
        _dlv.fromisoformat(date_str1)
        _dlv.fromisoformat(date_str2)
    except ValueError:
        _send_line_punch(user_id, f'日期格式錯誤，請使用 YYYY-MM-DD，例：{_dlv.today().isoformat()}')
        return

    # Find leave type (fuzzy: exact or contains)
    with get_db() as conn:
        lt = conn.execute(
            "SELECT * FROM leave_types WHERE active=TRUE AND name=%s", (leave_type_name,)
        ).fetchone()
        if not lt:
            lt = conn.execute(
                "SELECT * FROM leave_types WHERE active=TRUE AND name ILIKE %s LIMIT 1",
                (f'%{leave_type_name}%',)
            ).fetchone()
        if not lt:
            avail = conn.execute(
                "SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order"
            ).fetchall()
            names = '、'.join(r['name'] for r in avail)
            _send_line_punch(user_id, f'找不到假別「{leave_type_name}」\n\n可用假別：{names}')
            return

        # Check leave balance
        year = date_str1[:4]
        bal = conn.execute("""
            SELECT total_days, used_days FROM leave_balances
            WHERE staff_id=%s AND leave_type_id=%s AND year=%s
        """, (staff['id'], lt['id'], int(year))).fetchone()

        # Calculate requested days (exclude Sunday)
        from datetime import timedelta as _tdlv
        s = _dlv.fromisoformat(date_str1); e = _dlv.fromisoformat(date_str2)
        days = sum(1 for i in range((e-s).days+1)
                   if (_dlv.fromisoformat(date_str1) + __import__('datetime').timedelta(days=i)).weekday() != 6)

        remain = None
        if bal:
            remain = float(bal['total_days'] or 0) - float(bal['used_days'] or 0)
            if remain < days:
                _send_line_punch(user_id,
                    f'⚠️ {lt["name"]} 餘額不足\n剩餘 {remain:.1f} 天，申請 {days} 天\n\n'
                    f'請至員工系統調整後再申請。')
                return

        # Create leave request
        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, days,
               reason, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,'pending',NOW()) RETURNING id
        """, (staff['id'], lt['id'], date_str1, date_str2, days, reason or '（LINE 請假）')).fetchone()

    bal_str = f'（剩餘 {remain:.1f} 天）' if remain is not None else ''
    _send_line_punch(user_id,
        f'✅ 請假申請已送出\n\n'
        f'假別：{lt["name"]} {bal_str}\n'
        f'日期：{date_str1}' + (f' ～ {date_str2}' if date_str2 != date_str1 else '') + '\n'
        f'天數：{days} 天\n'
        f'原因：{reason}\n\n'
        f'申請號：#{row["id"]}，等待管理員審核。')


def _line_query_performance(staff, user_id):
    """查詢員工最近一次績效考核"""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT pr.period_label, pr.grade, pr.total_score, pr.max_score,
                       pr.comments, pr.salary_adjusted, pr.salary_delta,
                       pr.reviewed_at, pt.name AS tpl_name
                FROM performance_reviews pr
                LEFT JOIN performance_templates pt ON pt.id=pr.template_id
                WHERE pr.staff_id=%s
                ORDER BY pr.reviewed_at DESC LIMIT 1
            """, (staff['id'],)).fetchone()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return
    if not row:
        _send_line_punch(user_id, f'{staff["name"]}\n尚無績效考核記錄。')
        return
    grade_label = _grade_labels()
    pct = float(row['total_score']) / float(row['max_score']) * 100 if row['max_score'] else 0
    adj = f"\n薪資調整：NT$ {float(row['salary_delta']):+,.0f}" if row['salary_adjusted'] else ''
    reviewed = str(row['reviewed_at'])[:10] if row['reviewed_at'] else ''
    _send_line_punch(user_id,
        f'{staff["name"]} 最近考核\n\n'
        f'期間：{row["period_label"]}\n'
        f'範本：{row["tpl_name"] or "—"}\n'
        f'得分：{float(row["total_score"]):.1f} / {float(row["max_score"]):.0f}（{pct:.0f}%）\n'
        f'評級：{row["grade"]} {grade_label.get(row["grade"],"")}'
        f'{adj}\n'
        + (f'備注：{row["comments"][:60]}\n' if row['comments'] else '')
        + f'考核日：{reviewed}')


def _line_query_monthly_records(staff, user_id, text):
    """查詢員工月出勤記錄與打卡明細。
    格式：出勤紀錄 [YYYY-MM]（省略月份則查本月）
    """
    import re as _rem
    from datetime import date as _dm, timezone as _tzm, timedelta as _tdm, datetime as _dtm
    TW = _tzm(_tdm(hours=8))

    # 解析月份
    parts = text.strip().split()
    month = None
    if len(parts) >= 2:
        m = _rem.match(r'^(\d{4})-(\d{1,2})$', parts[1])
        if m:
            month = f"{m.group(1)}-{m.group(2).zfill(2)}"
    if not month:
        month = _dtm.now(TW).strftime('%Y-%m')

    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT punch_type, punched_at, is_manual
                FROM punch_records
                WHERE staff_id=%s
                  AND to_char(punched_at AT TIME ZONE 'Asia/Taipei', 'YYYY-MM') = %s
                ORDER BY punched_at ASC
            """, (staff['id'], month)).fetchall()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return

    if not rows:
        _send_line_punch(user_id, f'📋 {staff["name"]} {month}\n該月尚無打卡記錄。')
        return

    WDAY = ['一', '二', '三', '四', '五', '六', '日']

    # 依日期分組
    days = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            from datetime import timezone as _utzm
            pa = pa.replace(tzinfo=_utzm.utc)
        pa_tw = pa.astimezone(TW)
        ds = pa_tw.strftime('%Y-%m-%d')
        if ds not in days:
            days[ds] = []
        days[ds].append({'type': r['punch_type'], 'time': pa_tw.strftime('%H:%M'), 'manual': bool(r['is_manual'])})

    total_mins = 0
    anomaly_days = 0
    lines = []

    for ds in sorted(days.keys()):
        recs = days[ds]
        d = _dm.fromisoformat(ds)
        wday = WDAY[d.weekday()]

        clock_in  = next((r['time'] for r in recs if r['type'] == 'in'),  None)
        clock_out = next((r['time'] for r in recs if r['type'] == 'out'), None)
        has_manual = any(r['manual'] for r in recs)

        if clock_in and clock_out:
            ci = _dtm.strptime(f'{ds} {clock_in}',  '%Y-%m-%d %H:%M')
            co = _dtm.strptime(f'{ds} {clock_out}', '%Y-%m-%d %H:%M')
            mins = max(0, int((co - ci).total_seconds() / 60))
            total_mins += mins
            h, m = divmod(mins, 60)
            dur = f'{h}h{m:02d}' if m else f'{h}h'
        elif clock_in:
            dur = '⚠️缺下班'
            anomaly_days += 1
        else:
            dur = '⚠️缺上班'
            anomaly_days += 1

        manual_mark = '【補】' if has_manual else ''
        ci_str = clock_in  or '--:--'
        co_str = clock_out or '--:--'
        lines.append(f'{ds[5:]}({wday}) {ci_str}↑{co_str}↓ {dur}{manual_mark}')

    th, tm = divmod(total_mins, 60)
    total_str = f'{th}h{tm:02d}' if tm else f'{th}h'
    anomaly_str = f'｜異常 {anomaly_days} 天' if anomaly_days else ''
    header = (f'📋 {staff["name"]} {month} 出勤\n'
              f'出勤 {len(days)} 天｜工時 {total_str}{anomaly_str}\n'
              + '─' * 20)

    # 訊息過長時分批送出（LINE 單則上限約 5000 字）
    full = header + '\n' + '\n'.join(lines)
    if len(full) <= 4500:
        _send_line_punch(user_id, full)
    else:
        _send_line_punch(user_id, header)
        chunk, chunk_len = [], 0
        for line in lines:
            if chunk_len + len(line) + 1 > 1800:
                _send_line_punch(user_id, '\n'.join(chunk))
                chunk, chunk_len = [], 0
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            _send_line_punch(user_id, '\n'.join(chunk))


def _line_submit_overtime(staff, user_id, text):
    """
    LINE 加班申請。格式：加班 [YYYY-MM-DD] [時數] [原因]
    範例：加班 2026-04-05 3 業績衝刺
    """
    import re as _re_ot
    from datetime import date as _dot
    parts = text.strip().split(None, 3)
    if len(parts) < 3:
        _send_line_punch(user_id,
            '加班申請格式：\n加班 [日期] [時數] [原因]\n\n'
            '範例：加班 2026-04-05 3 業績衝刺\n'
            '（時數可用小數，如 1.5）')
        return
    date_str = parts[1]
    try:
        _dot.fromisoformat(date_str)
    except ValueError:
        _send_line_punch(user_id, f'日期格式錯誤，請使用 YYYY-MM-DD，例：{_dot.today().isoformat()}')
        return
    try:
        hours = float(parts[2])
        if hours <= 0 or hours > 24:
            raise ValueError
    except ValueError:
        _send_line_punch(user_id, '加班時數需為 0.5～24 之間的數字')
        return
    reason = parts[3].strip() if len(parts) > 3 else '（LINE 加班申請）'

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO overtime_requests
              (staff_id, request_date, start_time, end_time, ot_hours, reason, status)
            VALUES (%s, %s, NULL, NULL, %s, %s, 'pending')
            RETURNING id
        """, (staff['id'], date_str, hours, reason)).fetchone()

    _send_line_punch(user_id,
        f'✅ 加班申請已送出\n\n'
        f'日期：{date_str}\n'
        f'時數：{hours} 小時\n'
        f'原因：{reason}\n'
        f'申請編號：#{row["id"]}\n\n'
        '請等候管理員審核，審核結果將通知您。')


def _line_show_help(staff, user_id):
    _send_line_punch(user_id,
        f'哈囉 {staff["name"]}！以下是可用的指令：\n\n'
        '─── 打卡 ───\n'
        '📍 傳送位置 → 自動打卡\n'
        '💬 上班 / 下班\n'
        '📋 狀態 → 今日打卡記錄\n\n'
        '─── 查詢 ───\n'
        '🌿 查餘假 → 本年假期餘額\n'
        '💰 查薪資 → 最近薪資單\n'
        '📊 出勤紀錄 → 本月出勤明細\n'
        '   出勤紀錄 2026-03 → 指定月份\n'
        '考核 → 最近績效考核\n\n'
        '─── 申請 ───\n'
        '📝 請假 [假別] [日期] → 送出請假\n'
        '   範例：請假 特休 2026-04-01\n'
        '⏰ 加班 [日期] [時數] → 加班申請\n'
        '   範例：加班 2026-04-05 3\n'
        '🗂️ 假別 → 查看可用假別清單\n\n'
        '─── 其他 ───\n'
        '🔓 解除綁定')


def _line_show_leave_types(staff, user_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name, max_days FROM leave_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
    if not rows:
        _send_line_punch(user_id, '目前無可用假別。'); return
    lines = ['🗂️ 可用假別清單\n']
    for r in rows:
        limit = f'（年限 {r["max_days"]} 天）' if r['max_days'] else ''
        lines.append(f'• {r["name"]} {limit}')
    lines.append('\n申請方式：請假 [假別] [日期]')
    _send_line_punch(user_id, '\n'.join(lines))



# ═══════════════════════════════════════════════════════════════════════════════
# 📱 MOBILE API  (JWT-based, /api/mobile/*)
# ═══════════════════════════════════════════════════════════════════════════════
import jwt as _pyjwt

_MOBILE_JWT_SECRET = os.environ.get('MOBILE_JWT_SECRET', app.secret_key)
_JWT_EXPIRE_HOURS  = 24 * 7   # 7 days

def _make_jwt(payload: dict) -> str:
    payload['exp'] = _dt.now(_tz.utc) + _td(hours=_JWT_EXPIRE_HOURS)
    return _pyjwt.encode(payload, _MOBILE_JWT_SECRET, algorithm='HS256')

def _decode_jwt(token: str):
    return _pyjwt.decode(token, _MOBILE_JWT_SECRET, algorithms=['HS256'])

def mobile_jwt_required(f):
    """Decorator: reads Bearer token, sets g.mobile_user = {id, role, ...}"""
    from flask import g
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'error': '未授權'}), 401
        token = auth[7:]
        try:
            payload = _decode_jwt(token)
        except _pyjwt.ExpiredSignatureError:
            return jsonify({'error': 'token 已過期，請重新登入'}), 401
        except Exception:
            return jsonify({'error': 'token 無效'}), 401
        g.mobile_user = payload
        return f(*args, **kwargs)
    return decorated

def mobile_admin_required(f):
    """Decorator: must be admin role"""
    from flask import g
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'error': '未授權'}), 401
        token = auth[7:]
        try:
            payload = _decode_jwt(token)
        except Exception:
            return jsonify({'error': 'token 無效'}), 401
        if payload.get('role') != 'admin':
            return jsonify({'error': '需要管理員權限'}), 403
        g.mobile_user = payload
        return f(*args, **kwargs)
    return decorated

# ── Login ──────────────────────────────────────────────────────────────────────

@app.route('/api/mobile/login', methods=['POST'])
def mobile_login():
    b = request.get_json(force=True) or {}
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '請輸入帳號及密碼'}), 400

    # Try admin accounts first
    with get_db() as conn:
        admin = conn.execute(
            "SELECT * FROM admin_accounts WHERE username=%s AND active=TRUE", (username,)
        ).fetchone()
    if admin and admin['password_hash'] == _hash_pw(password):
        perms = admin['permissions']
        if isinstance(perms, str):
            try: perms = _json.loads(perms)
            except (ValueError, TypeError): perms = []
        token = _make_jwt({
            'sub': str(admin['id']), 'role': 'admin',
            'username': admin['username'],
            'display_name': admin['display_name'] or admin['username'],
            'is_super': bool(admin['is_super']),
            'permissions': perms,
        })
        with get_db() as conn:
            conn.execute("UPDATE admin_accounts SET last_login_at=NOW() WHERE id=%s", (admin['id'],))
        return jsonify({
            'token': token,
            'role': 'admin',
            'user': {
                'id': admin['id'],
                'username': admin['username'],
                'display_name': admin['display_name'] or admin['username'],
                'is_super': bool(admin['is_super']),
                'permissions': perms,
            }
        })

    # Try employee accounts
    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE username=%s AND active=TRUE", (username,)
        ).fetchone()
    if staff and staff['password_hash'] == _hash_pw(password):
        token = _make_jwt({
            'sub': str(staff['id']), 'role': 'employee',
            'staff_id': staff['id'],
            'name': staff['name'],
            'username': staff['username'],
        })
        return jsonify({
            'token': token,
            'role': 'employee',
            'user': {
                'id': staff['id'],
                'name': staff['name'],
                'username': staff['username'],
                'role': staff['role'],
                'department': staff['department'],
                'position_title': staff['position_title'],
                'employee_code': staff['employee_code'],
            }
        })

    return jsonify({'error': '帳號或密碼錯誤'}), 401

# ── Employee: Me & Profile ─────────────────────────────────────────────────────

@app.route('/api/mobile/me', methods=['GET'])
@mobile_jwt_required
def mobile_me():
    from flask import g
    u = g.mobile_user
    if u['role'] == 'employee':
        with get_db() as conn:
            staff = conn.execute(
                """SELECT id, name, username, role, department, position_title,
                          employee_code, hire_date, birth_date, base_salary,
                          insured_salary, daily_hours, salary_type, active
                   FROM punch_staff WHERE id=%s""", (int(u['sub']),)
            ).fetchone()
        if not staff:
            return jsonify({'error': '帳號不存在'}), 404
        d = dict(staff)
        for k in ('hire_date', 'birth_date'):
            if d.get(k): d[k] = str(d[k])
        return jsonify(d)
    else:
        with get_db() as conn:
            admin = conn.execute(
                "SELECT id, username, display_name, is_super, permissions FROM admin_accounts WHERE id=%s",
                (int(u['sub']),)
            ).fetchone()
        if not admin:
            return jsonify({'error': '帳號不存在'}), 404
        d = dict(admin)
        if isinstance(d['permissions'], str):
            try: d['permissions'] = _json.loads(d['permissions'])
            except (ValueError, TypeError): d['permissions'] = []
        return jsonify(d)

# ── Employee: Punch ────────────────────────────────────────────────────────────

@app.route('/api/mobile/punch', methods=['POST'])
@mobile_jwt_required
def mobile_punch():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可打卡'}), 403
    staff_id = int(u['sub'])
    b = request.get_json(force=True) or {}
    punch_type = b.get('punch_type', 'in')
    lat  = b.get('latitude')
    lng  = b.get('longitude')
    note = b.get('note', '')

    # GPS validation (same logic as web)
    with get_db() as conn:
        cfg = conn.execute("SELECT gps_required FROM punch_config WHERE id=1").fetchone()
        gps_required = cfg['gps_required'] if cfg else False
        locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()

    gps_distance = None
    location_name = ''
    if lat is not None and lng is not None and locs:
        def haversine(la1, lo1, la2, lo2):
            R = 6371000
            p = math.pi / 180
            a = (math.sin((la2-la1)*p/2)**2 +
                 math.cos(la1*p)*math.cos(la2*p)*math.sin((lo2-lo1)*p/2)**2)
            return int(2*R*math.asin(math.sqrt(a)))
        best = min(locs, key=lambda l: haversine(float(l['lat']), float(l['lng']), float(lat), float(lng)))
        gps_distance = haversine(float(best['lat']), float(best['lng']), float(lat), float(lng))
        location_name = best['location_name']
        if gps_required and gps_distance > best['radius_m']:
            return jsonify({'error': f'距離打卡地點 {gps_distance}m，超出範圍 {best["radius_m"]}m'}), 400
    elif gps_required:
        return jsonify({'error': '此門市需要 GPS 定位才能打卡'}), 400

    with get_db() as conn:
        conn.execute(
            """INSERT INTO punch_records
               (staff_id, punch_type, note, latitude, longitude, gps_distance, location_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (staff_id, punch_type, note, lat, lng, gps_distance, location_name)
        )
    return jsonify({'ok': True, 'location_name': location_name, 'gps_distance': gps_distance})

@app.route('/api/mobile/punch/status', methods=['GET'])
@mobile_jwt_required
def mobile_punch_status():
    """Return today's punch records for the employee."""
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    today = date.today().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT punch_type, punched_at, note, gps_distance, location_name
               FROM punch_records WHERE staff_id=%s
               AND punched_at::date = %s::date ORDER BY punched_at""",
            (staff_id, today)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d['punched_at'] = d['punched_at'].isoformat() if d.get('punched_at') else None
        data.append(d)
    return jsonify(data)

# ── Employee: Attendance ───────────────────────────────────────────────────────

@app.route('/api/mobile/attendance', methods=['GET'])
@mobile_jwt_required
def mobile_attendance():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    month = request.args.get('month', _dt.now(TW_TZ).strftime('%Y-%m'))
    try:
        y, m = map(int, month.split('-'))
    except Exception:
        return jsonify({'error': '月份格式錯誤'}), 400
    with get_db() as conn:
        rows = conn.execute(
            """SELECT punch_type, punched_at, note, gps_distance, location_name, is_manual
               FROM punch_records WHERE staff_id=%s
               AND date_trunc('month', punched_at) = %s::date
               ORDER BY punched_at""",
            (staff_id, f'{y}-{m:02d}-01')
        ).fetchall()

    # Group by day
    from collections import defaultdict
    days = defaultdict(list)
    for r in rows:
        day = r['punched_at'].date().isoformat()
        days[day].append({
            'type': r['punch_type'],
            'time': r['punched_at'].strftime('%H:%M'),
            'note': r['note'],
            'gps_distance': r['gps_distance'],
            'location_name': r['location_name'],
            'is_manual': r['is_manual'],
        })

    result = []
    for day in sorted(days.keys()):
        records = days[day]
        ins  = [r for r in records if r['type'] == 'in']
        outs = [r for r in records if r['type'] == 'out']
        clock_in  = ins[0]['time']  if ins  else None
        clock_out = outs[-1]['time'] if outs else None
        hours = None
        if clock_in and clock_out:
            ci = _dt.strptime(clock_in,  '%H:%M')
            co = _dt.strptime(clock_out, '%H:%M')
            diff = (co - ci).seconds / 3600
            hours = round(diff, 2)
        result.append({'date': day, 'clock_in': clock_in, 'clock_out': clock_out,
                       'hours': hours, 'records': records})
    return jsonify(result)

# ── Employee: Leave ────────────────────────────────────────────────────────────

@app.route('/api/mobile/leave/types', methods=['GET'])
@mobile_jwt_required
def mobile_leave_types():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, max_days FROM leave_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/mobile/leave', methods=['GET'])
@mobile_jwt_required
def mobile_leave_list():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    with get_db() as conn:
        rows = conn.execute(
            """SELECT lr.id, lt.name AS leave_type, lr.start_date, lr.end_date,
                      lr.total_days AS days, lr.reason, lr.status, lr.created_at
               FROM leave_requests lr
               JOIN leave_types lt ON lr.leave_type_id = lt.id
               WHERE lr.staff_id=%s ORDER BY lr.created_at DESC LIMIT 50""",
            (staff_id,)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for k in ('start_date','end_date','created_at'):
            if d.get(k): d[k] = str(d[k])
        data.append(d)
    return jsonify(data)

@app.route('/api/mobile/leave', methods=['POST'])
@mobile_jwt_required
def mobile_leave_apply():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可申請'}), 403
    staff_id = int(u['sub'])
    b = request.get_json(force=True) or {}
    leave_type_id = b.get('leave_type_id')
    start_date    = b.get('start_date')
    end_date      = b.get('end_date', start_date)
    reason        = b.get('reason', '')
    if not leave_type_id or not start_date:
        return jsonify({'error': '缺少必填欄位'}), 400
    try:
        sd = _dt.strptime(start_date, '%Y-%m-%d').date()
        ed = _dt.strptime(end_date,   '%Y-%m-%d').date()
        days = (ed - sd).days + 1
    except Exception:
        return jsonify({'error': '日期格式錯誤'}), 400
    with get_db() as conn:
        conn.execute(
            """INSERT INTO leave_requests (staff_id, leave_type_id, start_date, end_date, days, reason, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
            (staff_id, leave_type_id, start_date, end_date, days, reason)
        )
    return jsonify({'ok': True})

# ── Employee: Schedule ─────────────────────────────────────────────────────────

@app.route('/api/mobile/schedule', methods=['GET'])
@mobile_jwt_required
def mobile_schedule():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    month = request.args.get('month', _dt.now(TW_TZ).strftime('%Y-%m'))
    try:
        y, m = map(int, month.split('-'))
    except Exception:
        return jsonify({'error': '月份格式錯誤'}), 400
    with get_db() as conn:
        rows = conn.execute(
            """SELECT sa.shift_date, st.name AS shift_name, st.start_time, st.end_time, st.color
               FROM shift_assignments sa
               JOIN shift_types st ON sa.shift_type_id = st.id
               WHERE sa.staff_id=%s AND date_trunc('month', sa.shift_date) = %s::date
               ORDER BY sa.shift_date""",
            (staff_id, f'{y}-{m:02d}-01')
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d['shift_date'] = str(d['shift_date'])
        if d.get('start_time'): d['start_time'] = str(d['start_time'])
        if d.get('end_time'):   d['end_time']   = str(d['end_time'])
        data.append(d)
    return jsonify(data)

# ── Employee: Salary ───────────────────────────────────────────────────────────

@app.route('/api/mobile/salary', methods=['GET'])
@mobile_jwt_required
def mobile_salary():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, period_year, period_month, base_salary, bonus, deductions,
                      net_salary, status, paid_at, created_at
               FROM salary_records WHERE staff_id=%s ORDER BY period_year DESC, period_month DESC LIMIT 12""",
            (staff_id,)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for k in ('paid_at','created_at'):
            if d.get(k): d[k] = str(d[k])
        for k in ('base_salary','bonus','deductions','net_salary'):
            if d.get(k) is not None: d[k] = float(d[k])
        data.append(d)
    return jsonify(data)

# ── Employee: Overtime ─────────────────────────────────────────────────────────

@app.route('/api/mobile/overtime', methods=['POST'])
@mobile_jwt_required
def mobile_overtime():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可申請'}), 403
    staff_id = int(u['sub'])
    b = request.get_json(force=True) or {}
    ot_date = b.get('ot_date')
    hours   = b.get('hours')
    reason  = b.get('reason', '')
    if not ot_date or not hours:
        return jsonify({'error': '缺少必填欄位'}), 400
    try:
        _dt.strptime(ot_date, '%Y-%m-%d')
        hours = float(hours)
    except Exception:
        return jsonify({'error': '格式錯誤'}), 400
    with get_db() as conn:
        conn.execute(
            """INSERT INTO overtime_requests
               (staff_id, ot_date, ot_hours, reason, status)
               VALUES (%s, %s, %s, %s, 'pending')""",
            (staff_id, ot_date, hours, reason)
        )
    return jsonify({'ok': True})

@app.route('/api/mobile/overtime', methods=['GET'])
@mobile_jwt_required
def mobile_overtime_list():
    from flask import g
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, ot_date, ot_hours, reason, status, created_at
               FROM overtime_requests WHERE staff_id=%s ORDER BY ot_date DESC LIMIT 30""",
            (staff_id,)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for k in ('ot_date','created_at'):
            if d.get(k): d[k] = str(d[k])
        if d.get('ot_hours'): d['ot_hours'] = float(d['ot_hours'])
        data.append(d)
    return jsonify(data)

# ── Admin: Dashboard ───────────────────────────────────────────────────────────

@app.route('/api/mobile/admin/dashboard', methods=['GET'])
@mobile_admin_required
def mobile_admin_dashboard():
    today = date.today().isoformat()
    with get_db() as conn:
        total_staff = conn.execute("SELECT COUNT(*) AS n FROM punch_staff WHERE active=TRUE").fetchone()['n']
        punched_today = conn.execute(
            "SELECT COUNT(DISTINCT staff_id) AS n FROM punch_records WHERE punched_at::date=%s::date", (today,)
        ).fetchone()['n']
        pending_leaves = conn.execute(
            "SELECT COUNT(*) AS n FROM leave_requests WHERE status='pending'"
        ).fetchone()['n']
        pending_ot = conn.execute(
            "SELECT COUNT(*) AS n FROM overtime_requests WHERE status='pending'"
        ).fetchone()['n']
        # Last 7 days attendance rate
        rows_7d = conn.execute(
            """SELECT punched_at::date AS day, COUNT(DISTINCT staff_id) AS cnt
               FROM punch_records
               WHERE punched_at::date >= (CURRENT_DATE - INTERVAL '6 days')
               GROUP BY day ORDER BY day""",
        ).fetchall()
    attendance_trend = [{'date': str(r['day']), 'count': r['cnt']} for r in rows_7d]
    return jsonify({
        'total_staff': total_staff,
        'punched_today': punched_today,
        'pending_leaves': pending_leaves,
        'pending_ot': pending_ot,
        'attendance_trend': attendance_trend,
    })

# ── Admin: Today's Attendance ──────────────────────────────────────────────────

@app.route('/api/mobile/admin/attendance/today', methods=['GET'])
@mobile_admin_required
def mobile_admin_attendance_today():
    today = date.today().isoformat()
    with get_db() as conn:
        staff_all = conn.execute(
            "SELECT id, name, department, position_title FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        records = conn.execute(
            """SELECT staff_id, punch_type, punched_at
               FROM punch_records WHERE punched_at::date=%s::date ORDER BY punched_at""",
            (today,)
        ).fetchall()
    from collections import defaultdict
    by_staff = defaultdict(list)
    for r in records:
        by_staff[r['staff_id']].append(r)

    result = []
    for s in staff_all:
        recs = by_staff[s['id']]
        ins  = [r for r in recs if r['punch_type'] == 'in']
        outs = [r for r in recs if r['punch_type'] == 'out']
        clock_in  = ins[0]['punched_at'].strftime('%H:%M')  if ins  else None
        clock_out = outs[-1]['punched_at'].strftime('%H:%M') if outs else None
        result.append({
            'id': s['id'], 'name': s['name'],
            'department': s['department'], 'position': s['position_title'],
            'clock_in': clock_in, 'clock_out': clock_out,
            'status': 'present' if clock_in else 'absent',
        })
    return jsonify(result)

# ── Admin: Leave Requests ──────────────────────────────────────────────────────

@app.route('/api/mobile/admin/leaves', methods=['GET'])
@mobile_admin_required
def mobile_admin_leaves():
    status = request.args.get('status', 'pending')
    with get_db() as conn:
        rows = conn.execute(
            """SELECT lr.id, ps.name AS staff_name, lt.name AS leave_type,
                      lr.start_date, lr.end_date, lr.total_days AS days, lr.reason, lr.status, lr.created_at
               FROM leave_requests lr
               JOIN punch_staff ps ON lr.staff_id = ps.id
               JOIN leave_types lt ON lr.leave_type_id = lt.id
               WHERE (%s = '' OR lr.status = %s)
               ORDER BY lr.created_at DESC LIMIT 50""",
            (status, status)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for k in ('start_date','end_date','created_at'):
            if d.get(k): d[k] = str(d[k])
        data.append(d)
    return jsonify(data)

@app.route('/api/mobile/admin/leaves/<int:lid>', methods=['PUT'])
@mobile_admin_required
def mobile_admin_leave_action(lid):
    from flask import g
    b = request.get_json(force=True) or {}
    action = b.get('action')  # 'approve' | 'reject'
    if action not in ('approve', 'reject'):
        return jsonify({'error': '無效操作'}), 400
    status = 'approved' if action == 'approve' else 'rejected'
    reviewer = g.mobile_user.get('display_name', g.mobile_user.get('username', ''))
    with get_db() as conn:
        conn.execute(
            "UPDATE leave_requests SET status=%s, reviewed_by=%s, reviewed_at=NOW() WHERE id=%s",
            (status, reviewer, lid)
        )
    return jsonify({'ok': True})

# ── Admin: Overtime Requests ───────────────────────────────────────────────────

@app.route('/api/mobile/admin/overtime', methods=['GET'])
@mobile_admin_required
def mobile_admin_overtime():
    status = request.args.get('status', 'pending')
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ot.id, ps.name AS staff_name, ot.ot_date, ot.ot_hours,
                      ot.reason, ot.status, ot.created_at
               FROM overtime_requests ot
               JOIN punch_staff ps ON ot.staff_id = ps.id
               WHERE (%s = '' OR ot.status = %s)
               ORDER BY ot.created_at DESC LIMIT 50""",
            (status, status)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for k in ('ot_date','created_at'):
            if d.get(k): d[k] = str(d[k])
        if d.get('ot_hours'): d['ot_hours'] = float(d['ot_hours'])
        data.append(d)
    return jsonify(data)

@app.route('/api/mobile/admin/overtime/<int:oid>', methods=['PUT'])
@mobile_admin_required
def mobile_admin_overtime_action(oid):
    from flask import g
    b = request.get_json(force=True) or {}
    action = b.get('action')
    if action not in ('approve', 'reject'):
        return jsonify({'error': '無效操作'}), 400
    status = 'approved' if action == 'approve' else 'rejected'
    reviewer = g.mobile_user.get('display_name', g.mobile_user.get('username', ''))
    with get_db() as conn:
        conn.execute(
            "UPDATE overtime_requests SET status=%s, reviewed_by=%s, reviewed_at=NOW() WHERE id=%s",
            (status, reviewer, oid)
        )
    return jsonify({'ok': True})

# ── Admin: Staff List ──────────────────────────────────────────────────────────

@app.route('/api/mobile/admin/staff', methods=['GET'])
@mobile_admin_required
def mobile_admin_staff():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, name, username, department, position_title, employee_code,
                      role, active, hire_date
               FROM punch_staff ORDER BY active DESC, name"""
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        if d.get('hire_date'): d['hire_date'] = str(d['hire_date'])
        data.append(d)
    return jsonify(data)

# ── Admin: Anomaly Summary ─────────────────────────────────────────────────────

@app.route('/api/mobile/admin/anomalies', methods=['GET'])
@mobile_admin_required
def mobile_admin_anomalies():
    month = request.args.get('month', _dt.now(TW_TZ).strftime('%Y-%m'))
    try:
        y, m = map(int, month.split('-'))
    except Exception:
        return jsonify({'error': '月份格式錯誤'}), 400
    import calendar
    total_days = calendar.monthrange(y, m)[1]
    with get_db() as conn:
        staff_all = conn.execute(
            "SELECT id, name, department FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        records = conn.execute(
            """SELECT staff_id, punch_type, punched_at::date AS day
               FROM punch_records
               WHERE date_trunc('month', punched_at) = %s::date""",
            (f'{y}-{m:02d}-01',)
        ).fetchall()
    from collections import defaultdict
    by_staff = defaultdict(set)
    for r in records:
        by_staff[r['staff_id']].add(str(r['day']))

    result = []
    for s in staff_all:
        work_days = len(by_staff[s['id']])
        result.append({
            'id': s['id'], 'name': s['name'], 'department': s['department'],
            'work_days': work_days, 'missing_days': max(0, 22 - work_days),
        })
    return jsonify(result)



# ═══════════════════════════════════════════════════════════════════════════════
# WebAuthn (Face ID / 指紋) — using py_webauthn library
# ═══════════════════════════════════════════════════════════════════════════════
import base64 as _b64

_WEBAUTHN_RP_ID   = os.environ.get('WEBAUTHN_RP_ID',  'punch-system.onrender.com')
_WEBAUTHN_RP_NAME = '打卡系統'
_WEBAUTHN_ORIGIN  = os.environ.get('WEBAUTHN_ORIGIN', 'https://punch-system.onrender.com')

def _b64url_encode(data: bytes) -> str:
    return _b64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')

def _b64url_decode(s: str) -> bytes:
    s = str(s).replace('-', '+').replace('_', '/')
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return _b64.b64decode(s)

def _init_webauthn_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    id            SERIAL PRIMARY KEY,
                    user_key      TEXT NOT NULL,
                    credential_id TEXT NOT NULL UNIQUE,
                    public_key    BYTEA NOT NULL,
                    sign_count    BIGINT DEFAULT 0,
                    device_name   TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        print(f'[webauthn_init] {e}')

_init_webauthn_db()

# ── Registration Begin ─────────────────────────────────────────────────────────

@app.route('/api/webauthn/register/begin', methods=['POST'])
def webauthn_register_begin():
    user_key = user_name = user_display = None
    if session.get('logged_in'):
        user_key     = f"admin_{session['admin_id']}"
        user_name    = session.get('admin_username', '')
        user_display = session.get('admin_display_name', user_name)
    elif session.get('punch_staff_id'):
        sid = session['punch_staff_id']
        user_key     = f"staff_{sid}"
        user_name    = session.get('punch_staff_name', str(sid))
        user_display = user_name
    else:
        return jsonify({'error': '請先登入'}), 401
    try:
        from webauthn import generate_registration_options, options_to_json as _wa_o2j
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria, UserVerificationRequirement,
            ResidentKeyRequirement, AuthenticatorAttachment,
            AttestationConveyancePreference, PublicKeyCredentialDescriptor,
        )
        from webauthn.helpers.cose import COSEAlgorithmIdentifier
        import json as _waj
        with get_db() as conn:
            existing = conn.execute(
                "SELECT credential_id FROM webauthn_credentials WHERE user_key=%s", (user_key,)
            ).fetchall()
        exclude_creds = [PublicKeyCredentialDescriptor(id=_b64url_decode(r['credential_id'])) for r in existing]
        options = generate_registration_options(
            rp_id=_WEBAUTHN_RP_ID,
            rp_name=_WEBAUTHN_RP_NAME,
            user_id=user_key.encode('utf-8'),
            user_name=user_name,
            user_display_name=user_display or user_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                user_verification=UserVerificationRequirement.REQUIRED,
                resident_key=ResidentKeyRequirement.PREFERRED,
            ),
            attestation=AttestationConveyancePreference.NONE,
            supported_pub_key_algs=[
                COSEAlgorithmIdentifier.ECDSA_SHA_256,
                COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
            ],
            exclude_credentials=exclude_creds,
        )
        session['webauthn_reg_challenge'] = _b64url_encode(options.challenge)
        session['webauthn_reg_user_key']  = user_key
        return jsonify(_waj.loads(_wa_o2j(options)))
    except Exception as e:
        return jsonify({'error': f'初始化失敗：{e}'}), 500

# ── Registration Complete ──────────────────────────────────────────────────────

@app.route('/api/webauthn/register/complete', methods=['POST'])
def webauthn_register_complete():
    challenge_b64 = session.get('webauthn_reg_challenge')
    user_key      = session.get('webauthn_reg_user_key')
    if not challenge_b64 or not user_key:
        return jsonify({'error': '找不到挑戰，請重新開始'}), 400
    b = request.get_json(force=True) or {}
    try:
        from webauthn import verify_registration_response
        from webauthn.helpers.structs import RegistrationCredential, AuthenticatorAttestationResponse
        credential = RegistrationCredential(
            id=b['id'],
            raw_id=_b64url_decode(b['rawId']),
            response=AuthenticatorAttestationResponse(
                client_data_json=_b64url_decode(b['response']['clientDataJSON']),
                attestation_object=_b64url_decode(b['response']['attestationObject']),
            ),
            type=b.get('type', 'public-key'),
        )
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge_b64),
            expected_rp_id=_WEBAUTHN_RP_ID,
            expected_origin=_WEBAUTHN_ORIGIN,
            require_user_verification=True,
        )
        credential_id = b['id']
        public_key    = verification.credential_public_key
        sign_count    = verification.sign_count
        device_name   = b.get('device_name', '我的裝置')
        with get_db() as conn:
            conn.execute("""
                INSERT INTO webauthn_credentials
                  (user_key, credential_id, public_key, sign_count, device_name)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (credential_id) DO UPDATE
                  SET sign_count=%s, device_name=%s, user_key=%s
            """, (user_key, credential_id, public_key, sign_count, device_name,
                  sign_count, device_name, user_key))
        session.pop('webauthn_reg_challenge', None)
        session.pop('webauthn_reg_user_key', None)
        return jsonify({'ok': True})
    except Exception as ex:
        return jsonify({'error': f'綁定失敗：{ex}'}), 400

# ── Authentication Begin ───────────────────────────────────────────────────────

@app.route('/api/webauthn/auth/begin', methods=['POST'])
def webauthn_auth_begin():
    b        = request.get_json(force=True) or {}
    username = (b.get('username') or '').strip()
    try:
        from webauthn import generate_authentication_options, options_to_json as _wa_o2j
        from webauthn.helpers.structs import UserVerificationRequirement, PublicKeyCredentialDescriptor
        import json as _waj
        allow_credentials = []
        if username:
            with get_db() as conn:
                admin = conn.execute(
                    "SELECT id FROM admin_accounts WHERE username=%s AND active=TRUE", (username,)
                ).fetchone()
                if admin:
                    user_key = f"admin_{admin['id']}"
                else:
                    staff = conn.execute(
                        "SELECT id FROM punch_staff WHERE username=%s AND active=TRUE", (username,)
                    ).fetchone()
                    user_key = f"staff_{staff['id']}" if staff else None
                if user_key:
                    creds = conn.execute(
                        "SELECT credential_id FROM webauthn_credentials WHERE user_key=%s", (user_key,)
                    ).fetchall()
                    allow_credentials = [
                        PublicKeyCredentialDescriptor(id=_b64url_decode(r['credential_id']))
                        for r in creds
                    ]
        options = generate_authentication_options(
            rp_id=_WEBAUTHN_RP_ID,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        session['webauthn_auth_challenge'] = _b64url_encode(options.challenge)
        return jsonify(_waj.loads(_wa_o2j(options)))
    except Exception as e:
        return jsonify({'error': f'初始化失敗：{e}'}), 500

# ── Authentication Complete ────────────────────────────────────────────────────

@app.route('/api/webauthn/auth/complete', methods=['POST'])
def webauthn_auth_complete():
    challenge_b64 = session.get('webauthn_auth_challenge')
    if not challenge_b64:
        return jsonify({'error': '找不到挑戰，請重新開始'}), 400
    b = request.get_json(force=True) or {}
    try:
        from webauthn import verify_authentication_response
        from webauthn.helpers.structs import AuthenticationCredential, AuthenticatorAssertionResponse
        import json as _json3
        credential_id = b['id']
        with get_db() as conn:
            cred = conn.execute(
                "SELECT * FROM webauthn_credentials WHERE credential_id=%s", (credential_id,)
            ).fetchone()
        if not cred:
            return jsonify({'error': '找不到已綁定的裝置，請先綁定'}), 401
        user_handle_raw = b['response'].get('userHandle')
        credential = AuthenticationCredential(
            id=credential_id,
            raw_id=_b64url_decode(b['rawId']),
            response=AuthenticatorAssertionResponse(
                client_data_json=_b64url_decode(b['response']['clientDataJSON']),
                authenticator_data=_b64url_decode(b['response']['authenticatorData']),
                signature=_b64url_decode(b['response']['signature']),
                user_handle=_b64url_decode(user_handle_raw) if user_handle_raw else None,
            ),
            type=b.get('type', 'public-key'),
        )
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge_b64),
            expected_rp_id=_WEBAUTHN_RP_ID,
            expected_origin=_WEBAUTHN_ORIGIN,
            credential_public_key=bytes(cred['public_key']),
            credential_current_sign_count=int(cred['sign_count']),
            require_user_verification=True,
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE webauthn_credentials SET sign_count=%s WHERE id=%s",
                (verification.new_sign_count, cred['id'])
            )
        session.pop('webauthn_auth_challenge', None)
        user_key = cred['user_key']
        if user_key.startswith('admin_'):
            admin_id = int(user_key[6:])
            admin = _get_admin_by_id(admin_id)
            if not admin:
                return jsonify({'error': '帳號不存在或已停用'}), 401
            perms = admin['permissions']
            if isinstance(perms, str):
                try: perms = _json3.loads(perms)
                except (ValueError, TypeError): perms = []
            session.permanent             = True
            session['logged_in']          = True
            session['admin_id']           = admin['id']
            session['admin_username']     = admin['username']
            session['admin_display_name'] = admin['display_name'] or admin['username']
            session['admin_permissions']  = perms
            session['admin_is_super']     = bool(admin['is_super'])
            return jsonify({'ok': True, 'redirect': '/admin', 'role': 'admin'})
        elif user_key.startswith('staff_'):
            staff_id = int(user_key[6:])
            with get_db() as conn:
                staff = conn.execute(
                    "SELECT id, name, role FROM punch_staff WHERE id=%s AND active=TRUE", (staff_id,)
                ).fetchone()
            if not staff:
                return jsonify({'error': '帳號不存在或已停用'}), 401
            session['punch_staff_id']   = staff['id']
            session['punch_staff_name'] = staff['name']
            return jsonify({'ok': True, 'role': 'staff', 'user': dict(staff)})
        return jsonify({'error': '未知帳號類型'}), 400
    except Exception as ex:
        return jsonify({'error': f'驗證失敗：{ex}'}), 400

# ── 已綁定裝置列表 & 刪除 ────────────────────────────────────────────────────────

@app.route('/api/webauthn/credentials', methods=['GET'])
def webauthn_list_credentials():
    if session.get('logged_in'):
        user_key = f"admin_{session['admin_id']}"
    elif session.get('punch_staff_id'):
        user_key = f"staff_{session['punch_staff_id']}"
    else:
        return jsonify({'error': '請先登入'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, device_name, created_at FROM webauthn_credentials WHERE user_key=%s ORDER BY created_at DESC",
            (user_key,)
        ).fetchall()
    return jsonify([{'id': r['id'], 'device_name': r['device_name'],
                     'created_at': str(r['created_at'])} for r in rows])

@app.route('/api/webauthn/credentials/<int:cid>', methods=['DELETE'])
def webauthn_delete_credential(cid):
    if session.get('logged_in'):
        user_key = f"admin_{session['admin_id']}"
    elif session.get('punch_staff_id'):
        user_key = f"staff_{session['punch_staff_id']}"
    else:
        return jsonify({'error': '請先登入'}), 401
    with get_db() as conn:
        conn.execute(
            "DELETE FROM webauthn_credentials WHERE id=%s AND user_key=%s", (cid, user_key)
        )
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════════
# 財報模組 — 報價單 (Quotation Module)
# ═══════════════════════════════════════════════════════════════════

def init_quotation_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS quotation_settings (
            id               SERIAL PRIMARY KEY,
            company_name     TEXT DEFAULT 'AD.Studio 影像事務所',
            company_address  TEXT DEFAULT '新北市三重區雙源街57巷7號1樓',
            company_phone    TEXT DEFAULT '(02)8985-1790',
            company_email    TEXT DEFAULT '',
            bank_name        TEXT DEFAULT '',
            bank_branch      TEXT DEFAULT '',
            account_name     TEXT DEFAULT '',
            account_no       TEXT DEFAULT '',
            tax_id           TEXT DEFAULT '',
            payment_notes    TEXT DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS quotation_products (
            id            SERIAL PRIMARY KEY,
            name          TEXT NOT NULL,
            unit          TEXT DEFAULT '次',
            default_price NUMERIC(12,2) DEFAULT 0,
            sort_order    INTEGER DEFAULT 0,
            active        BOOLEAN DEFAULT TRUE
        )""",
        """CREATE TABLE IF NOT EXISTS quotations (
            id              SERIAL PRIMARY KEY,
            quote_no        TEXT UNIQUE NOT NULL,
            quote_date      DATE NOT NULL DEFAULT CURRENT_DATE,
            client_name     TEXT NOT NULL DEFAULT '',
            client_phone    TEXT DEFAULT '',
            client_address  TEXT DEFAULT '',
            client_line_id  TEXT DEFAULT '',
            sales_rep       TEXT DEFAULT '',
            image_no        TEXT DEFAULT '',
            image_date      DATE,
            payment_method  TEXT DEFAULT '轉帳',
            status          TEXT DEFAULT 'draft',
            subtotal        NUMERIC(12,2) DEFAULT 0,
            notes           TEXT DEFAULT '',
            created_by      TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS quotation_items (
            id             SERIAL PRIMARY KEY,
            quotation_id   INTEGER REFERENCES quotations(id) ON DELETE CASCADE,
            sort_order     INTEGER DEFAULT 0,
            product_name   TEXT NOT NULL DEFAULT '',
            unit           TEXT DEFAULT '次',
            quantity       NUMERIC(10,2) DEFAULT 1,
            unit_price     NUMERIC(12,2) DEFAULT 0,
            handmade       NUMERIC(12,2) DEFAULT 0,
            people_count   INTEGER DEFAULT 0,
            amount         NUMERIC(12,2) DEFAULT 0,
            payment_status TEXT DEFAULT '',
            note           TEXT DEFAULT ''
        )""",
        # 確保欄位存在（建表後立即補齊，避免 init_db migrations 先跑時表尚未存在而被跳過）
        "ALTER TABLE quotation_settings ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotation_products ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotations         ADD COLUMN IF NOT EXISTS company_unit TEXT DEFAULT 'ad'",
        "ALTER TABLE quotations         ADD COLUMN IF NOT EXISTS deposit_rate NUMERIC DEFAULT 100",
        "ALTER TABLE quotations         ADD COLUMN IF NOT EXISTS show_wedding_content BOOLEAN DEFAULT TRUE",
        "ALTER TABLE quotation_settings ADD COLUMN IF NOT EXISTS frequent_accounts JSONB DEFAULT '[]'",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[init_quotation_db] {str(e)[:120]}")
    # 確保兩個公司各有一筆 settings
    try:
        with get_db() as conn:
            for cu, cname, caddr, cphone in [
                ('ad', 'AD影像事務所', '新北市三重區雙源街57巷7號1樓', '(02)8985-1790'),
                ('jm', '進光設計', '', ''),
            ]:
                row = conn.execute(
                    "SELECT id FROM quotation_settings WHERE company_unit=%s LIMIT 1", (cu,)
                ).fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO quotation_settings (company_unit,company_name,company_address,company_phone) VALUES (%s,%s,%s,%s)",
                        (cu, cname, caddr, cphone)
                    )
    except Exception as e:
        print(f"[init_quotation_db seed] {e}")

try:
    init_quotation_db()
except Exception as _eq:
    print(f"[quotation_init] {_eq}")


def _next_quote_no(company_unit='ad'):
    """產生流水號，格式 QT-YYYYMM-NNN（依公司分開計序）"""
    from datetime import date as _d
    cu_prefix = 'JM' if company_unit == 'jm' else 'AD'
    prefix = f'QT-{cu_prefix}-' + _d.today().strftime('%Y%m') + '-'
    with get_db() as conn:
        row = conn.execute(
            "SELECT quote_no FROM quotations WHERE quote_no LIKE %s ORDER BY quote_no DESC LIMIT 1",
            (prefix + '%',)
        ).fetchone()
    if row:
        try:
            seq = int(row['quote_no'].split('-')[-1]) + 1
        except Exception:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


# ── Next quote number preview ────────────────────────────────────
@app.route('/api/quotation/next-no', methods=['GET'])
@login_required
def api_quotation_next_no():
    company = request.args.get('company', 'ad')
    return jsonify({'quote_no': _next_quote_no(company)})

# ── Settings ─────────────────────────────────────────────────────
@app.route('/api/quotation/settings', methods=['GET'])
@login_required
def api_quotation_settings_get():
    company = request.args.get('company', 'ad')
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM quotation_settings WHERE company_unit=%s LIMIT 1", (company,)
        ).fetchone()
    return jsonify(dict(row) if row else {})

@app.route('/api/quotation/settings', methods=['PUT'])
@login_required
def api_quotation_settings_put():
    b = request.get_json(force=True) or {}
    company = b.get('company_unit', 'ad')
    fields = ['company_name','company_address','company_phone','company_email',
              'bank_name','bank_branch','account_name','account_no','tax_id','payment_notes']
    fa = b.get('frequent_accounts', None)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM quotation_settings WHERE company_unit=%s LIMIT 1", (company,)
        ).fetchone()
        if row:
            sets = ', '.join(f"{f}=%s" for f in fields)
            vals = [b.get(f,'') for f in fields]
            if fa is not None:
                sets += ', frequent_accounts=%s'
                vals.append(_json.dumps(fa, ensure_ascii=False))
            vals.append(row['id'])
            conn.execute(f"UPDATE quotation_settings SET {sets} WHERE id=%s", vals)
        else:
            cols = ', '.join(['company_unit'] + fields + (['frequent_accounts'] if fa is not None else []))
            phs  = ', '.join(['%s']*(len(fields)+1+(1 if fa is not None else 0)))
            row_vals = [company] + [b.get(f,'') for f in fields]
            if fa is not None:
                row_vals.append(_json.dumps(fa, ensure_ascii=False))
            conn.execute(f"INSERT INTO quotation_settings ({cols}) VALUES ({phs})", row_vals)
    return jsonify({'ok': True})


# ── Preset Products ───────────────────────────────────────────────
@app.route('/api/quotation/products', methods=['GET'])
@login_required
def api_qproducts_list():
    company = request.args.get('company', '')
    key     = company or '__all__'
    now     = time.time()
    cached  = _qproducts_cache.get(key)
    if cached and now - cached['at'] < _STATIC_TTL:
        return jsonify(cached['data'])
    with get_db() as conn:
        if company:
            rows = conn.execute(
                "SELECT * FROM quotation_products WHERE company_unit=%s ORDER BY sort_order, id", (company,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quotation_products ORDER BY sort_order, id"
            ).fetchall()
    result = [dict(r) for r in rows]
    _qproducts_cache[key] = {'data': result, 'at': now}
    return jsonify(result)

@app.route('/api/quotation/products', methods=['POST'])
@login_required
def api_qproducts_create():
    b = request.get_json(force=True) or {}
    company = b.get('company_unit', 'ad')
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO quotation_products (name, unit, default_price, sort_order, active, company_unit) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            (b.get('name','').strip(), b.get('unit','次'),
             float(b.get('default_price',0)), int(b.get('sort_order',0)), True, company)
        ).fetchone()
    _qproducts_cache.clear()
    return jsonify(dict(row)), 201

@app.route('/api/quotation/products/<int:pid>', methods=['PUT'])
@login_required
def api_qproducts_update(pid):
    b = request.get_json(force=True) or {}
    with get_db() as conn:
        conn.execute(
            "UPDATE quotation_products SET name=%s, unit=%s, default_price=%s, sort_order=%s, active=%s WHERE id=%s",
            (b.get('name','').strip(), b.get('unit','次'),
             float(b.get('default_price',0)), int(b.get('sort_order',0)),
             bool(b.get('active', True)), pid)
        )
    _qproducts_cache.clear()
    return jsonify({'ok': True})

@app.route('/api/quotation/products/<int:pid>', methods=['DELETE'])
@login_required
def api_qproducts_delete(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM quotation_products WHERE id=%s", (pid,))
    _qproducts_cache.clear()
    return jsonify({'ok': True})


# ── Quotations ───────────────────────────────────────────────────
@app.route('/api/quotations', methods=['GET'])
@login_required
def api_quotations_list():
    status  = request.args.get('status', '')
    q       = request.args.get('q', '')
    company = request.args.get('company', '')
    conds, params = ['TRUE'], []
    if status:  conds.append("q.status=%s"); params.append(status)
    if q:       conds.append("(q.client_name ILIKE %s OR q.quote_no ILIKE %s)"); params += [f'%{q}%', f'%{q}%']
    if company: conds.append("q.company_unit=%s"); params.append(company)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT q.*, COUNT(qi.id) as item_count
            FROM quotations q
            LEFT JOIN quotation_items qi ON qi.quotation_id = q.id
            WHERE {' AND '.join(conds)}
            GROUP BY q.id
            ORDER BY q.created_at DESC
        """, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for k in ('quote_date','image_date','created_at','updated_at'):
            if d.get(k): d[k] = str(d[k])
        result.append(d)
    return jsonify(result)

@app.route('/api/quotations', methods=['POST'])
@login_required
def api_quotations_create():
    b = request.get_json(force=True) or {}
    company  = b.get('company_unit', 'ad')
    quote_no = _next_quote_no(company)
    from datetime import date as _dd
    quote_date = b.get('quote_date') or str(_dd.today())
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO quotations
              (quote_no, quote_date, client_name, client_phone, client_address,
               client_line_id, sales_rep, image_no, image_date, payment_method,
               status, notes, created_by, company_unit, deposit_rate, show_wedding_content)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (quote_no, quote_date,
              b.get('client_name','').strip(),
              b.get('client_phone','').strip(),
              b.get('client_address','').strip(),
              b.get('client_line_id','').strip(),
              b.get('sales_rep','').strip(),
              b.get('image_no','').strip(),
              b.get('image_date') or None,
              b.get('payment_method','轉帳'),
              b.get('status','draft'),
              b.get('notes','').strip(),
              session.get('admin_display_name',''),
              company,
              float(b.get('deposit_rate', 100)),
              bool(b.get('show_wedding_content', True))
              )).fetchone()
        qid = row['id']
        # 寫入品項
        for i, item in enumerate(b.get('items', [])):
            qty   = float(item.get('quantity', 1))
            price = float(item.get('unit_price', 0))
            amt   = round(qty * price, 2)
            conn.execute("""
                INSERT INTO quotation_items
                  (quotation_id, sort_order, product_name, unit, quantity,
                   unit_price, handmade, people_count, amount, payment_status, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (qid, i,
                  item.get('product_name','').strip(),
                  item.get('unit','次'),
                  qty, price,
                  float(item.get('handmade', 0)),
                  int(item.get('people_count', 0)),
                  amt,
                  item.get('payment_status',''),
                  item.get('note','').strip()))
        # 更新小計
        conn.execute(
            "UPDATE quotations SET subtotal=(SELECT COALESCE(SUM(amount),0) FROM quotation_items WHERE quotation_id=%s) WHERE id=%s",
            (qid, qid)
        )
    return jsonify({'id': qid, 'quote_no': quote_no}), 201

@app.route('/api/quotations/<int:qid>', methods=['GET'])
@login_required
def api_quotation_get(qid):
    with get_db() as conn:
        q = conn.execute("SELECT * FROM quotations WHERE id=%s", (qid,)).fetchone()
        if not q: return ('', 404)
        items = conn.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=%s ORDER BY sort_order, id",
            (qid,)
        ).fetchall()
    d = dict(q)
    for k in ('quote_date','image_date','created_at','updated_at'):
        if d.get(k): d[k] = str(d[k])
    d['items'] = [dict(i) for i in items]
    return jsonify(d)

@app.route('/api/quotations/<int:qid>', methods=['PUT'])
@login_required
def api_quotation_update(qid):
    b = request.get_json(force=True) or {}
    new_status = b.get('status', 'draft')
    with get_db() as conn:
        old = conn.execute("SELECT status, subtotal, company_unit, quote_no, client_name FROM quotations WHERE id=%s", (qid,)).fetchone()
        conn.execute("""
            UPDATE quotations SET
              quote_date=%s, client_name=%s, client_phone=%s, client_address=%s,
              client_line_id=%s, sales_rep=%s, image_no=%s, image_date=%s,
              payment_method=%s, status=%s, notes=%s,
              deposit_rate=%s, show_wedding_content=%s, updated_at=NOW()
            WHERE id=%s
        """, (b.get('quote_date'), b.get('client_name','').strip(),
              b.get('client_phone','').strip(), b.get('client_address','').strip(),
              b.get('client_line_id','').strip(), b.get('sales_rep','').strip(),
              b.get('image_no','').strip(), b.get('image_date') or None,
              b.get('payment_method','轉帳'), new_status,
              b.get('notes','').strip(),
              float(b.get('deposit_rate', 100)),
              bool(b.get('show_wedding_content', True)),
              qid))
        # 狀態變為「已接受」時自動建立財務收入分錄
        if old and old['status'] != 'accepted' and new_status == 'accepted':
            subtotal = float(old['subtotal'] or 0)
            if subtotal > 0:
                company = old['company_unit'] or 'ad'
                cat = conn.execute(
                    "SELECT id FROM finance_categories WHERE type='income' AND company_unit=%s ORDER BY sort_order LIMIT 1",
                    (company,)
                ).fetchone()
                cat_id = cat['id'] if cat else None
                already = conn.execute(
                    "SELECT id FROM finance_records WHERE linked_quotation_id=%s LIMIT 1", (qid,)
                ).fetchone()
                if not already:
                    from datetime import date as _dd2
                    conn.execute("""
                        INSERT INTO finance_records
                          (record_date, type, title, amount, category_id, company_unit, linked_quotation_id, note)
                        VALUES (%s,'income',%s,%s,%s,%s,%s,%s)
                    """, (str(_dd2.today()),
                          f"報價單收入 {old['quote_no']} - {old['client_name']}",
                          subtotal, cat_id, company, qid,
                          '由報價單自動建立'))
        # 重建品項
        conn.execute("DELETE FROM quotation_items WHERE quotation_id=%s", (qid,))
        for i, item in enumerate(b.get('items', [])):
            qty   = float(item.get('quantity', 1))
            price = float(item.get('unit_price', 0))
            amt   = round(qty * price, 2)
            conn.execute("""
                INSERT INTO quotation_items
                  (quotation_id, sort_order, product_name, unit, quantity,
                   unit_price, handmade, people_count, amount, payment_status, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (qid, i,
                  item.get('product_name','').strip(),
                  item.get('unit','次'),
                  qty, price,
                  float(item.get('handmade', 0)),
                  int(item.get('people_count', 0)),
                  amt,
                  item.get('payment_status',''),
                  item.get('note','').strip()))
        conn.execute(
            "UPDATE quotations SET subtotal=(SELECT COALESCE(SUM(amount),0) FROM quotation_items WHERE quotation_id=%s) WHERE id=%s",
            (qid, qid)
        )
    return jsonify({'ok': True})

@app.route('/api/quotations/<int:qid>', methods=['DELETE'])
@login_required
def api_quotation_delete(qid):
    with get_db() as conn:
        conn.execute("DELETE FROM quotations WHERE id=%s", (qid,))
    return jsonify({'ok': True})


@app.route('/api/quotations/<int:qid>/duplicate', methods=['POST'])
@login_required
def api_quotation_duplicate(qid):
    with get_db() as conn:
        q = conn.execute("SELECT * FROM quotations WHERE id=%s", (qid,)).fetchone()
        if not q: return ('', 404)
        items = conn.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=%s ORDER BY sort_order", (qid,)
        ).fetchall()
        q = dict(q)
        from datetime import date as _dd3
        new_no = _next_quote_no(q.get('company_unit', 'ad'))
        new_row = conn.execute("""
            INSERT INTO quotations
              (quote_no, quote_date, client_name, client_phone, client_address,
               client_line_id, sales_rep, image_no, image_date, payment_method,
               status, notes, created_by, company_unit, deposit_rate, show_wedding_content)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s) RETURNING id
        """, (new_no, str(_dd3.today()),
              q.get('client_name',''), q.get('client_phone',''), q.get('client_address',''),
              q.get('client_line_id',''), q.get('sales_rep',''),
              q.get('image_no',''), q.get('image_date'),
              q.get('payment_method','轉帳'),
              q.get('notes',''), session.get('admin_display_name',''),
              q.get('company_unit','ad'),
              float(q.get('deposit_rate') or 100),
              bool(q.get('show_wedding_content', True))
              )).fetchone()
        new_id = new_row['id']
        for item in items:
            item = dict(item)
            conn.execute("""
                INSERT INTO quotation_items
                  (quotation_id, sort_order, product_name, unit, quantity,
                   unit_price, handmade, people_count, amount, payment_status, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (new_id, item['sort_order'], item['product_name'], item['unit'],
                  item['quantity'], item['unit_price'], item['handmade'],
                  item['people_count'], item['amount'],
                  item['payment_status'], item['note']))
        conn.execute(
            "UPDATE quotations SET subtotal=(SELECT COALESCE(SUM(amount),0) FROM quotation_items WHERE quotation_id=%s) WHERE id=%s",
            (new_id, new_id)
        )
    return jsonify({'id': new_id, 'quote_no': new_no}), 201


# ── Clients CRUD ──────────────────────────────────────────────────
@app.route('/api/clients', methods=['GET'])
@login_required
def api_clients_list():
    q       = request.args.get('q', '')
    company = request.args.get('company', '')
    conds, params = ['TRUE'], []
    if company: conds.append("company_unit=%s"); params.append(company)
    if q:       conds.append("(name ILIKE %s OR phone ILIKE %s)"); params += [f'%{q}%', f'%{q}%']
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM clients WHERE {' AND '.join(conds)} ORDER BY name",
            params
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/clients', methods=['POST'])
@login_required
def api_clients_create():
    b = request.get_json(force=True) or {}
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO clients (company_unit, name, phone, address, line_id, email, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b.get('company_unit','ad'), b.get('name','').strip(),
              b.get('phone','').strip(), b.get('address','').strip(),
              b.get('line_id','').strip(), b.get('email','').strip(),
              b.get('note','').strip())).fetchone()
    return jsonify(dict(row)), 201

@app.route('/api/clients/<int:cid>', methods=['PUT'])
@login_required
def api_clients_update(cid):
    b = request.get_json(force=True) or {}
    with get_db() as conn:
        conn.execute("""
            UPDATE clients SET name=%s, phone=%s, address=%s, line_id=%s, email=%s, note=%s
            WHERE id=%s
        """, (b.get('name','').strip(), b.get('phone','').strip(),
              b.get('address','').strip(), b.get('line_id','').strip(),
              b.get('email','').strip(), b.get('note','').strip(), cid))
    return jsonify({'ok': True})

@app.route('/api/clients/<int:cid>', methods=['DELETE'])
@login_required
def api_clients_delete(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM clients WHERE id=%s", (cid,))
    return jsonify({'ok': True})


# ── Annual Revenue (single query, replaces 24 separate calls) ─────
@app.route('/api/finance/revenue-annual', methods=['GET'])
@login_required
def api_finance_revenue_annual():
    year    = request.args.get('year', '')
    company = request.args.get('company', '')
    if not year:
        from datetime import datetime as _dt
        year = str(_dt.now().year)
    c_cond  = "AND company_unit=%s" if company else ""
    c_param = [company] if company else []
    with get_db() as conn:
        this_rows = conn.execute(f"""
            SELECT to_char(record_date,'MM') as month, type,
                   COALESCE(SUM(amount),0) as total
            FROM finance_records
            WHERE to_char(record_date,'YYYY')=%s {c_cond}
            GROUP BY 1,2
        """, [year] + c_param).fetchall()
        last_rows = conn.execute(f"""
            SELECT to_char(record_date,'MM') as month,
                   COALESCE(SUM(amount),0) as total
            FROM finance_records
            WHERE to_char(record_date,'YYYY')=%s AND type='income' {c_cond}
            GROUP BY 1
        """, [str(int(year)-1)] + c_param).fetchall()
    inc  = {r['month']: float(r['total']) for r in this_rows if r['type']=='income'}
    exp  = {r['month']: float(r['total']) for r in this_rows if r['type']=='expense'}
    last = {r['month']: float(r['total']) for r in last_rows}
    months = [{'month': str(i).zfill(2),
               'income':  inc.get(str(i).zfill(2), 0),
               'expense': exp.get(str(i).zfill(2), 0),
               'last_income': last.get(str(i).zfill(2), 0)} for i in range(1,13)]
    return jsonify({'year': year, 'months': months})


# ── Print View ────────────────────────────────────────────────────
@app.route('/quotation/<int:qid>/print')
@login_required
def quotation_print(qid):
    with get_db() as conn:
        q = conn.execute("SELECT * FROM quotations WHERE id=%s", (qid,)).fetchone()
        if not q: return '找不到此報價單', 404
        items = conn.execute(
            "SELECT * FROM quotation_items WHERE quotation_id=%s ORDER BY sort_order, id",
            (qid,)
        ).fetchall()
        # 依報價單所屬公司讀取對應設定
        company_unit = dict(q).get('company_unit', 'ad')
        settings = conn.execute(
            "SELECT * FROM quotation_settings WHERE company_unit=%s LIMIT 1", (company_unit,)
        ).fetchone()
        if not settings:
            settings = conn.execute("SELECT * FROM quotation_settings LIMIT 1").fetchone()
    from flask import render_template
    qd = dict(q)
    for k in ('quote_date','image_date','created_at','updated_at'):
        if qd.get(k): qd[k] = str(qd[k])
    qd.setdefault('deposit_rate', 100)
    qd.setdefault('show_wedding_content', True)
    return render_template('quotation_print.html',
                           q=qd, items=[dict(i) for i in items],
                           s=dict(settings) if settings else {})


# ── Revenue Detail (from quotation items) ────────────────────────
@app.route('/api/revenue/detail', methods=['GET'])
@login_required
def api_revenue_detail():
    """從報價單品項拉取逐筆收入明細（已接受 / 已發送的訂單）"""
    year    = request.args.get('year',  '')
    month   = request.args.get('month', '')  # YYYY-MM
    company = request.args.get('company', '')
    status_filter = request.args.get('status', '')   # 可傳 accepted,sent 等

    conds, params = ['TRUE'], []
    if year and not month:
        conds.append("to_char(q.quote_date,'YYYY')=%s"); params.append(year)
    if month:
        conds.append("to_char(q.quote_date,'YYYY-MM')=%s"); params.append(month)
    if company:
        conds.append("q.company_unit=%s"); params.append(company)
    if status_filter:
        statuses = status_filter.split(',')
        ph = ','.join(['%s']*len(statuses))
        conds.append(f"q.status IN ({ph})"); params.extend(statuses)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT
                q.quote_no, q.quote_date, q.client_name, q.client_phone,
                q.image_no, q.image_date, q.payment_method,
                q.status, q.sales_rep, q.company_unit,
                qi.sort_order, qi.product_name, qi.unit, qi.quantity,
                qi.unit_price, qi.handmade, qi.people_count,
                qi.amount, qi.payment_status, qi.note
            FROM quotation_items qi
            JOIN quotations q ON q.id = qi.quotation_id
            WHERE {' AND '.join(conds)}
            ORDER BY q.quote_date DESC, q.id, qi.sort_order
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        for k in ('quote_date', 'image_date'):
            if d.get(k): d[k] = str(d[k])
        for k in ('unit_price','handmade','amount','quantity'):
            if d.get(k) is not None: d[k] = float(d[k])
        result.append(d)
    return jsonify(result)


# ── Revenue Detail Excel Export ───────────────────────────────────
@app.route('/api/revenue/export', methods=['GET'])
@login_required
def api_revenue_export():
    """營業收入明細 Excel 匯出"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    year    = request.args.get('year',    '')
    month   = request.args.get('month',   '')
    company = request.args.get('company', '')

    conds, params = ['TRUE'], []
    if year and not month:
        conds.append("to_char(q.quote_date,'YYYY')=%s"); params.append(year)
    if month:
        conds.append("to_char(q.quote_date,'YYYY-MM')=%s"); params.append(month)
    if company:
        conds.append("q.company_unit=%s"); params.append(company)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT
                q.quote_no, q.quote_date, q.client_name,
                q.image_no, q.image_date, q.payment_method,
                q.status, q.sales_rep,
                qi.product_name, qi.unit, qi.quantity,
                qi.unit_price, qi.handmade, qi.people_count,
                qi.amount, qi.payment_status, qi.note
            FROM quotation_items qi
            JOIN quotations q ON q.id = qi.quotation_id
            WHERE {' AND '.join(conds)}
            ORDER BY q.quote_date DESC, q.id, qi.sort_order
        """, params).fetchall()

    status_map = {'draft':'草稿','sent':'已發送','accepted':'已接受','rejected':'已拒絕'}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '營業收入明細'

    hdr_fill = PatternFill('solid', fgColor='0F1C3A')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    alt_fill = PatternFill('solid', fgColor='F4F6FA')
    center   = Alignment(horizontal='center', vertical='center')

    headers = ['訂單編號','報價日期','客戶','影像編號','影像日期',
               '訂單付款方式','狀態','業務人員',
               '品項目','單位','數量','費率(元)','手工費','手操人數',
               '應收款項','收款狀況','備註']
    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 24

    for i, r in enumerate(rows, 2):
        vals = [
            r['quote_no'], str(r['quote_date'] or ''),
            r['client_name'] or '', r['image_no'] or '',
            str(r['image_date'] or ''), r['payment_method'] or '',
            status_map.get(r['status'], r['status'] or ''), r['sales_rep'] or '',
            r['product_name'] or '', r['unit'] or '',
            float(r['quantity'] or 0), float(r['unit_price'] or 0),
            float(r['handmade'] or 0), int(r['people_count'] or 0),
            float(r['amount'] or 0),
            r['payment_status'] or '', r['note'] or '',
        ]
        fill = alt_fill if i % 2 == 0 else PatternFill()
        for col, v in enumerate(vals, 1):
            c = ws.cell(i, col, v)
            c.fill = fill
            c.alignment = Alignment(vertical='center')
            if col in (12, 13, 15):
                c.number_format = '#,##0'

    # 欄寬
    widths = [16,12,14,14,12,12,8,10,22,6,6,10,10,8,12,12,24]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1,col).column_letter].width = w

    # 小計列
    last = len(rows) + 2
    total = sum(float(r['amount'] or 0) for r in rows)
    ws.cell(last, 1, f'共 {len(rows)} 筆').font = Font(bold=True)
    ws.cell(last, 15, total).font = Font(bold=True, color='2E9E6B')
    ws.cell(last, 15).number_format = '#,##0'

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    tag = month or year or 'all'
    co_tag = f'_{company}' if company else ''
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename=revenue{co_tag}_{tag}.xlsx'})


# ── Quotation Excel Export ────────────────────────────────────────
@app.route('/api/quotation/export', methods=['GET'])
@login_required
def api_quotation_export():
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    company = request.args.get('company', '')
    status  = request.args.get('status', '')
    conds, params = ['TRUE'], []
    if company: conds.append("q.company_unit=%s"); params.append(company)
    if status:  conds.append("q.status=%s");        params.append(status)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT q.id, q.quote_no, q.quote_date, q.client_name, q.client_phone,
                   q.client_address, q.sales_rep, q.image_no, q.image_date,
                   q.payment_method, q.status, q.subtotal, q.notes,
                   q.company_unit, COUNT(qi.id) as item_count
            FROM quotations q
            LEFT JOIN quotation_items qi ON qi.quotation_id = q.id
            WHERE {' AND '.join(conds)}
            GROUP BY q.id
            ORDER BY q.created_at DESC
        """, params).fetchall()

        # detail items sheet
        items_rows = conn.execute(f"""
            SELECT q.quote_no, q.client_name, qi.product_name, qi.unit, qi.quantity,
                   qi.unit_price, qi.handmade, qi.people_count, qi.amount,
                   qi.payment_status, qi.note
            FROM quotation_items qi
            JOIN quotations q ON q.id = qi.quotation_id
            WHERE {' AND '.join(conds).replace('q.company_unit','q.company_unit').replace('q.status','q.status')}
            ORDER BY q.created_at DESC, qi.sort_order
        """, params).fetchall()

    wb = openpyxl.Workbook()

    # ── Sheet 1: 報價單列表 ────────────────────────────────
    ws1 = wb.active
    ws1.title = '報價單列表'
    hdr_fill  = PatternFill('solid', fgColor='0F1C3A')
    hdr_font  = Font(bold=True, color='FFFFFF', size=10)
    alt_fill  = PatternFill('solid', fgColor='F4F6FA')
    center    = Alignment(horizontal='center', vertical='center')
    status_map = {'draft':'草稿','sent':'已發送','accepted':'已接受','rejected':'已拒絕'}
    co_map     = {'ad':'AD影像事務所','jm':'進光設計'}

    hdrs1 = ['訂單編號','報價日期','客戶','聯絡電話','業務人員','影像編號',
             '付款方式','狀態','金額','品項數','公司單位']
    for col, h in enumerate(hdrs1, 1):
        c = ws1.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws1.freeze_panes = 'A2'
    ws1.row_dimensions[1].height = 22

    for i, r in enumerate(rows, 2):
        vals = [
            r['quote_no'], str(r['quote_date'] or ''),
            r['client_name'] or '', r['client_phone'] or '',
            r['sales_rep'] or '', r['image_no'] or '',
            r['payment_method'] or '',
            status_map.get(r['status'], r['status'] or ''),
            float(r['subtotal'] or 0), int(r['item_count'] or 0),
            co_map.get(r['company_unit'] or '', r['company_unit'] or ''),
        ]
        fill = alt_fill if i % 2 == 0 else PatternFill()
        for col, v in enumerate(vals, 1):
            c = ws1.cell(i, col, v)
            c.fill = fill
            c.alignment = Alignment(vertical='center')
            if col == 9:  # 金額
                c.number_format = '#,##0'
                c.font = Font(bold=True)

    for col, w in enumerate([18,12,16,14,12,16,10,10,14,8,14], 1):
        ws1.column_dimensions[ws1.cell(1,col).column_letter].width = w

    # 小計
    last1 = len(rows) + 2
    total_amt = sum(float(r['subtotal'] or 0) for r in rows)
    ws1.cell(last1, 1, f'共 {len(rows)} 筆').font = Font(bold=True)
    ws1.cell(last1, 9, total_amt).font = Font(bold=True, color='2E9E6B')
    ws1.cell(last1, 9).number_format = '#,##0'

    # ── Sheet 2: 品項明細 ────────────────────────────────
    ws2 = wb.create_sheet('品項明細')
    hdrs2 = ['訂單編號','客戶','產品名稱','單位','數量','費率(元)','手工費','人數','費用','收款狀況','備註']
    for col, h in enumerate(hdrs2, 1):
        c = ws2.cell(1, col, h)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center
    ws2.freeze_panes = 'A2'
    ws2.row_dimensions[1].height = 22

    for i, r in enumerate(items_rows, 2):
        vals2 = [
            r['quote_no'], r['client_name'] or '',
            r['product_name'] or '', r['unit'] or '',
            float(r['quantity'] or 0), float(r['unit_price'] or 0),
            float(r['handmade'] or 0), int(r['people_count'] or 0),
            float(r['amount'] or 0),
            r['payment_status'] or '', r['note'] or '',
        ]
        fill = alt_fill if i % 2 == 0 else PatternFill()
        for col, v in enumerate(vals2, 1):
            c = ws2.cell(i, col, v)
            c.fill = fill
            c.alignment = Alignment(vertical='center')
            if col in (5,6,7,9): c.number_format = '#,##0.##'

    for col, w in enumerate([18,16,24,8,8,12,10,8,12,12,20], 1):
        ws2.column_dimensions[ws2.cell(1,col).column_letter].width = w

    buf = BytesIO(); wb.save(buf); buf.seek(0)
    co_tag = f'_{company}' if company else ''
    fname  = f"quotations{co_tag}.xlsx"
    from flask import Response
    return Response(buf.read(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})


# ══════════════════════════════════════════════════════════════════════════════
# 工作日誌模組
# ══════════════════════════════════════════════════════════════════════════════

def _init_work_logs_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_logs (
                id         SERIAL PRIMARY KEY,
                staff_id   INT NOT NULL REFERENCES punch_staff(id) ON DELETE CASCADE,
                log_date   DATE NOT NULL,
                content    TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(staff_id, log_date)
            )
        """)

_init_work_logs_db()


@app.route('/api/work-logs', methods=['GET'])
def api_work_logs_my():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    month = request.args.get('month', '')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT log_date::text, content, updated_at::text
            FROM work_logs
            WHERE staff_id=%s AND to_char(log_date,'YYYY-MM')=%s
            ORDER BY log_date
        """, (sid, month)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/work-logs', methods=['POST'])
def api_work_logs_save():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    b = request.get_json(force=True)
    log_date = b.get('log_date', '').strip()
    content  = b.get('content', '').strip()
    if not log_date:
        return jsonify({'error': '日期必填'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO work_logs (staff_id, log_date, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (staff_id, log_date)
            DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()
            RETURNING log_date::text, content, updated_at::text
        """, (sid, log_date, content)).fetchone()
    return jsonify(dict(row)), 200


@app.route('/api/admin/work-logs', methods=['GET'])
@login_required
def api_admin_work_logs():
    month    = request.args.get('month', '')
    staff_id = request.args.get('staff_id', '')
    if not month:
        return jsonify([])
    conds  = ["to_char(w.log_date,'YYYY-MM')=%s"]
    params = [month]
    if staff_id:
        conds.append("w.staff_id=%s")
        params.append(int(staff_id))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT w.id, w.staff_id, s.name AS staff_name, s.employee_code,
                   w.log_date::text, w.content, w.updated_at::text
            FROM work_logs w
            JOIN punch_staff s ON s.id=w.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY w.log_date, s.name
        """, params).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Department Expense Category Visibility ────────────────────────────────────

_DEFAULT_EXPENSE_CATS = ['廠商','雇員薪資','雇員獎金','固定營業費用','全民健康保險','勞工保險金','勞工退休金','稅務相關','政府相關','房租費用','建築師事務所','會計師事務所','律師事務所','客戶退款','雜支']
_DEFAULT_INCOME_CATS  = ['客變','設計','裝修工程','追加','退傭','介紹費','其它']

def _init_dept_expense_cats_db():
    sqls = [
        """CREATE TABLE IF NOT EXISTS dept_expense_categories (
            id           SERIAL PRIMARY KEY,
            department   TEXT NOT NULL DEFAULT '',
            expense_cats JSONB NOT NULL DEFAULT '[]',
            income_cats  JSONB NOT NULL DEFAULT '[]',
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS dept_expense_cats_dept_uniq ON dept_expense_categories(department)",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[dept_expense_cats_init] {e}")
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) AS c FROM dept_expense_categories").fetchone()['c']
            if cnt == 0:
                conn.execute(
                    "INSERT INTO dept_expense_categories (department, expense_cats, income_cats) VALUES (%s,%s,%s)",
                    ('', _json.dumps(_DEFAULT_EXPENSE_CATS), _json.dumps(_DEFAULT_INCOME_CATS))
                )
    except Exception as e:
        print(f"[dept_expense_cats_seed] {e}")

_init_dept_expense_cats_db()


def _get_dept_cats(department: str):
    """Return (expense_cats, income_cats) for a given department, falling back to default."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT expense_cats, income_cats FROM dept_expense_categories WHERE department=%s",
            (department,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT expense_cats, income_cats FROM dept_expense_categories WHERE department=''",
            ).fetchone()
    if not row:
        return _DEFAULT_EXPENSE_CATS, _DEFAULT_INCOME_CATS
    ec = row['expense_cats'] if isinstance(row['expense_cats'], list) else _json.loads(row['expense_cats'] or '[]')
    ic = row['income_cats']  if isinstance(row['income_cats'],  list) else _json.loads(row['income_cats']  or '[]')
    return ec, ic


@app.route('/api/expense/categories', methods=['GET'])
def api_expense_categories():
    """Staff-facing: return expense/income category lists based on the current staff's department."""
    sid = session.get('punch_staff_id')
    department = ''
    if sid:
        with get_db() as conn:
            staff = conn.execute("SELECT department FROM punch_staff WHERE id=%s", (sid,)).fetchone()
        if staff:
            department = staff['department'] or ''
    ec, ic = _get_dept_cats(department)
    return jsonify({'支出': ec, '收入': ic, 'department': department})


@app.route('/api/admin/dept-expense-cats', methods=['GET'])
@login_required
def api_admin_get_dept_expense_cats():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT department, expense_cats, income_cats, updated_at FROM dept_expense_categories ORDER BY department"
        ).fetchall()
        depts = conn.execute(
            "SELECT DISTINCT department FROM punch_staff WHERE department!='' ORDER BY department"
        ).fetchall()
    configs = []
    for r in rows:
        ec = r['expense_cats'] if isinstance(r['expense_cats'], list) else _json.loads(r['expense_cats'] or '[]')
        ic = r['income_cats']  if isinstance(r['income_cats'],  list) else _json.loads(r['income_cats']  or '[]')
        configs.append({
            'department':   r['department'],
            'expense_cats': ec,
            'income_cats':  ic,
            'updated_at':   r['updated_at'].isoformat() if r['updated_at'] else None,
        })
    return jsonify({
        'configs':     configs,
        'departments': [d['department'] for d in depts],
        'defaults':    {'expense_cats': _DEFAULT_EXPENSE_CATS, 'income_cats': _DEFAULT_INCOME_CATS},
    })


@app.route('/api/admin/dept-expense-cats', methods=['POST'])
@login_required
def api_admin_save_dept_expense_cats():
    b = request.get_json(force=True)
    department   = (b.get('department') or '').strip()
    expense_cats = b.get('expense_cats', [])
    income_cats  = b.get('income_cats',  [])
    if not isinstance(expense_cats, list): expense_cats = []
    if not isinstance(income_cats,  list): income_cats  = []
    with get_db() as conn:
        conn.execute("""
            INSERT INTO dept_expense_categories (department, expense_cats, income_cats, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (department) DO UPDATE
            SET expense_cats=EXCLUDED.expense_cats, income_cats=EXCLUDED.income_cats, updated_at=NOW()
        """, (department, _json.dumps(expense_cats), _json.dumps(income_cats)))
    return jsonify({'ok': True})


@app.route('/api/admin/dept-expense-cats/<path:dept>', methods=['DELETE'])
@login_required
def api_admin_delete_dept_expense_cats(dept):
    if dept == '__default__':
        dept = ''
    if dept == '':
        return jsonify({'error': '預設設定無法刪除'}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM dept_expense_categories WHERE department=%s", (dept,))
    return jsonify({'ok': True})
