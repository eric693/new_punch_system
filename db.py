import hashlib
import os
import time

import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from psycopg_pool import ConnectionPool
from psycopg_pool.errors import PoolTimeout

_raw_db_url  = os.environ.get('DATABASE_URL', '')
DATABASE_URL = _raw_db_url.replace('postgres://', 'postgresql://', 1) if _raw_db_url.startswith('postgres://') else _raw_db_url

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
                max_lifetime=600,
                max_idle=300,
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
    last_exc = None
    for attempt in range(3):
        try:
            raw = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        except Exception as e:
            last_exc = e
            if attempt < 2:
                print(f"[db] direct connect attempt {attempt+1} failed: {e}, retrying...")
                time.sleep(2)
            continue
        with raw:
            yield raw
        return
    raise last_exc


def _hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# ─── In-Memory / Redis Caches ────────────────────────────────────────────────
# CacheDict: L1=in-process dict, L2=Redis (when REDIS_URL is set).
# Falls back to plain dict behaviour when Redis is unavailable.

from cache import CacheDict

_punch_cfg_cache:  CacheDict = CacheDict('punch_cfg',  redis_ttl=600)
_punch_locs_cache: CacheDict = CacheDict('punch_locs', redis_ttl=120)
_PUNCH_CFG_TTL  = 300
_PUNCH_LOCS_TTL = 60

_fin_cats_cache:   CacheDict = CacheDict('fin_cats',   redis_ttl=120)
_qproducts_cache:  CacheDict = CacheDict('qproducts',  redis_ttl=120)
_stores_cache:     CacheDict = CacheDict('stores',     redis_ttl=60)
_STATIC_TTL = 30.0

_leave_types_all_cache:  CacheDict = CacheDict('lv_types_all', redis_ttl=120)
_leave_types_pub_cache:  CacheDict = CacheDict('lv_types_pub', redis_ttl=120)
_shift_types_all_cache:  CacheDict = CacheDict('sh_types_all', redis_ttl=120)
_shift_types_pub_cache:  CacheDict = CacheDict('sh_types_pub', redis_ttl=120)
_salary_items_cache:     CacheDict = CacheDict('sal_items',    redis_ttl=120)
_ann_public_cache:       CacheDict = CacheDict('ann_pub',      redis_ttl=60)
_holidays_pub_cache:     CacheDict = CacheDict('holidays_pub', redis_ttl=1200)
_SEMISTATIC_TTL = 60.0
_HOLIDAY_TTL    = 600.0
_ANN_TTL        = 30.0

_badges_cache:   CacheDict = CacheDict('badges',    redis_ttl=30)
_BADGES_TTL = 8.0

# Admin HTML cache stays in-process (large payload, worker-local is fine)
_admin_html_cache:   dict = {}
_admin_tmtime_cache: dict = {'at': 0.0, 'mtime': 0}

_expense_list_cache: CacheDict = CacheDict('expense_list', redis_ttl=120)
_EXPENSE_LIST_TTL = 60.0

_dashboard_cache:     CacheDict = CacheDict('dashboard',  redis_ttl=120)
_punch_summary_cache: CacheDict = CacheDict('punch_sum',  redis_ttl=120)
_anomalies_cache:     CacheDict = CacheDict('anomalies',  redis_ttl=240)
_labor_cost_cache:    CacheDict = CacheDict('labor_cost', redis_ttl=240)
_heatmap_cache:       CacheDict = CacheDict('heatmap',    redis_ttl=120)
_DASHBOARD_TTL  = 60.0
_SUMMARY_TTL    = 60.0
_ANOMALIES_TTL  = 120.0
_LABOR_TTL      = 120.0

_admin_acct_cache: CacheDict = CacheDict('admin_acct', redis_ttl=600)
_ADMIN_ACCT_TTL = 300.0


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
