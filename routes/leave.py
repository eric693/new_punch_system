"""Leave management Blueprint — leave types, requests, balances, schedules."""

import os
import time

from flask import Blueprint, request, jsonify, session, Response

from auth import login_required, require_module
from db import (
    get_db,
    _leave_types_all_cache,
    _leave_types_pub_cache,
    _SEMISTATIC_TTL,
    _badges_cache,
)
from notifications import _notify_review_result
from leave_calc import (
    _calc_leave_days,
    _calc_annual_leave_days,
    _calc_annual_leave_schedule,
    _get_scheduled_dates,
)
from config import TW_TZ
from datetime import datetime as _dt
import json as _json

bp = Blueprint('leave', __name__)


# ── DB initialisation ─────────────────────────────────────────────

def init():
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
        # ── 假勤索引 ─────────────────────────────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_leave_requests_status ON leave_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_leave_requests_staff_status ON leave_requests(staff_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_leave_requests_staff_date ON leave_requests(staff_id, start_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_leave_requests_status_created ON leave_requests(status, created_at DESC)",
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
        ('特休假',   'annual',       1.0,  30,  '#2e9e6b', 1),
        ('病假',     'sick',         0.5,  30,  '#e07b2a', 2),
        ('住院病假', 'hospitalize',  1.0,  30,  '#d64242', 3),
        ('事假',     'personal',     0.0,  14,  '#8892a4', 4),
        ('生理假',   'menstrual',    0.5,  12,  '#c45cb8', 5),
        ('婚假',     'marriage',     1.0,   8,  '#c8a96e', 6),
        ('喪假',     'funeral',      1.0,   8,  '#4a7bda', 7),
        ('產假',     'maternity',    1.0,  56,  '#e05c8a', 8),
        ('陪產假',   'paternity',    1.0,   7,  '#5cb8c4', 9),
        ('公假',     'official',     1.0, None, '#243d6e', 10),
        ('補休',     'compensatory', 1.0, None, '#8b5cf6', 11),
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


# ── Row serialisers ───────────────────────────────────────────────

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


# ── Internal helper ───────────────────────────────────────────────

def _update_leave_balance(conn, staff_id, leave_type_id, year_str, delta_days):
    year = int(year_str)
    conn.execute("""
        INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
        VALUES (%s, %s, %s, 0, %s)
        ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
          SET used_days = leave_balances.used_days + EXCLUDED.used_days,
              updated_at = NOW()
    """, (staff_id, leave_type_id, year, delta_days))


# ── Leave Type CRUD ───────────────────────────────────────────────

@bp.route('/api/leave/types', methods=['GET'])
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


@bp.route('/api/leave/types/public', methods=['GET'])
def api_leave_types_public():
    now = time.time()
    if _leave_types_pub_cache['data'] is not None and now - _leave_types_pub_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_leave_types_pub_cache['data'])
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types WHERE active=TRUE ORDER BY sort_order, id").fetchall()
    result = [leave_type_row(r) for r in rows]
    _leave_types_pub_cache['data'] = result; _leave_types_pub_cache['at'] = now
    return jsonify(result)


@bp.route('/api/leave/types', methods=['POST'])
@require_module('leave')
def api_leave_type_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO leave_types (name,code,pay_rate,max_days,description,color,sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b['code'], float(b.get('pay_rate', 1.0)),
              b.get('max_days') or None, b.get('description', ''),
              b.get('color', '#4a7bda'), int(b.get('sort_order', 0)))).fetchone()
    _leave_types_all_cache['data'] = None; _leave_types_pub_cache['data'] = None
    return jsonify(leave_type_row(row)), 201


@bp.route('/api/leave/types/<int:tid>', methods=['PUT'])
@require_module('leave')
def api_leave_type_update(tid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE leave_types SET name=%s,code=%s,pay_rate=%s,max_days=%s,
              description=%s,color=%s,sort_order=%s,active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b['code'], float(b.get('pay_rate', 1.0)),
              b.get('max_days') or None, b.get('description', ''),
              b.get('color', '#4a7bda'), int(b.get('sort_order', 0)),
              bool(b.get('active', True)), tid)).fetchone()
    _leave_types_all_cache['data'] = None; _leave_types_pub_cache['data'] = None
    return jsonify(leave_type_row(row)) if row else ('', 404)


@bp.route('/api/leave/types/<int:tid>', methods=['DELETE'])
@require_module('leave')
def api_leave_type_delete(tid):
    with get_db() as conn:
        conn.execute("DELETE FROM leave_types WHERE id=%s", (tid,))
    _leave_types_all_cache['data'] = None; _leave_types_pub_cache['data'] = None
    return jsonify({'deleted': tid})


# ── Leave Requests ────────────────────────────────────────────────

@bp.route('/api/leave/requests', methods=['GET'])
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


@bp.route('/api/leave/requests', methods=['POST'])
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

    with get_db() as conn:
        sched = _get_scheduled_dates(conn, sid, start_date, end_date)
        total_days = _calc_leave_days(start_date, end_date,
                                      start_time=lv_start_time, end_time=lv_end_time,
                                      scheduled_dates=sched)
        if total_days <= 0:
            return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

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
              total_days, reason, status, b.get('reviewed_by', '管理員'), status)).fetchone()
        if status == 'approved':
            _update_leave_balance(conn, sid, leave_type_id, start_date[:4], total_days)
    return jsonify(leave_req_row(row)), 201


@bp.route('/api/leave/requests/<int:rid>', methods=['PUT'])
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
        if action == 'approve' and old['status'] != 'approved':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], float(old['total_days']))
        elif action == 'reject' and old['status'] == 'approved':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], -float(old['total_days']))
    if row:
        extra = f"{str(old['start_date'])} ~ {str(old['end_date'])} 共 {float(old['total_days'])} 天"
        if review_note: extra += f"\n審核意見：{review_note}"
        _notify_review_result(old['staff_id'], '請假申請', action, extra)
    _badges_cache.clear()
    return jsonify(leave_req_row(row)) if row else ('', 404)


@bp.route('/api/leave/requests/<int:rid>', methods=['DELETE'])
@require_module('leave')
def api_leave_request_delete(rid):
    with get_db() as conn:
        old = conn.execute("SELECT * FROM leave_requests WHERE id=%s", (rid,)).fetchone()
        if not old: return ('', 404)
        conn.execute("DELETE FROM leave_requests WHERE id=%s", (rid,))
        if old['status'] == 'approved':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], -float(old['total_days']))
    extra = f"{str(old['start_date'])} ~ {str(old['end_date'])} 共 {float(old['total_days'])} 天"
    if old['status'] == 'approved':
        extra += "\n（已核准，假別額度已歸還）"
    _notify_review_result(old['staff_id'], '請假申請', 'cancelled', extra)
    _badges_cache.clear()
    return jsonify({'deleted': rid})


# ── Employee: submit leave request ────────────────────────────────

@bp.route('/api/leave/my-requests', methods=['GET'])
def api_leave_my_list():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT lr.*, lt.name as leave_type_name, lt.code as leave_code,
                       lt.color as leave_color, lt.pay_rate
                FROM leave_requests lr
                JOIN leave_types lt ON lt.id=lr.leave_type_id
                WHERE lr.staff_id=%s
                ORDER BY lr.start_date DESC LIMIT 30
            """, (sid,)).fetchall()
    except Exception as e:
        print(f"[leave/my-requests GET] error: {e}")
        return jsonify([])
    result = []
    for r in rows:
        try:
            d = leave_req_row(r)
            d['leave_type_name'] = r['leave_type_name']
            d['leave_code']      = r['leave_code']
            d['leave_color']     = r['leave_color']
            d['pay_rate']        = float(r['pay_rate'])
            result.append(d)
        except Exception:
            pass
    return jsonify(result)


@bp.route('/api/leave/my-requests', methods=['POST'])
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

    with get_db() as conn:
        sched = _get_scheduled_dates(conn, sid, start_date, end_date)
        total_days = _calc_leave_days(start_date, end_date,
                                      start_time=lv_start_time, end_time=lv_end_time,
                                      scheduled_dates=sched)
        if total_days <= 0:
            return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

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


@bp.route('/api/leave/my-requests/<int:rid>/attachment', methods=['POST'])
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


@bp.route('/api/leave/attachment/<int:rid>', methods=['GET'])
def api_leave_attachment_get(rid):
    sid      = session.get('punch_staff_id')
    is_admin = session.get('logged_in')
    with get_db() as conn:
        row = conn.execute(
            "SELECT staff_id, attachment, attachment_name, attachment_type FROM leave_requests WHERE id=%s",
            (rid,)
        ).fetchone()
    if not row or not row['attachment']:
        return ('', 404)
    if not is_admin and row['staff_id'] != sid:
        return jsonify({'error': '無權限'}), 403
    return Response(
        bytes(row['attachment']),
        mimetype=row['attachment_type'] or 'application/octet-stream',
        headers={
            'Content-Disposition': f'inline; filename="{os.path.basename(row["attachment_name"] or "attachment")}"'
        }
    )


# ── Leave Balance ─────────────────────────────────────────────────

@bp.route('/api/leave/balances', methods=['GET'])
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


@bp.route('/api/leave/balances/init', methods=['POST'])
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


@bp.route('/api/leave/annual-schedule/<int:staff_id>', methods=['GET'])
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
        'staff_id':     staff_id,
        'name':         staff['name'],
        'hire_date':    str(staff['hire_date']) if staff['hire_date'] else None,
        'current_days': current,
        'schedule':     schedule,
    })


@bp.route('/api/leave/annual-schedule/public', methods=['GET'])
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


@bp.route('/api/leave/balances/<int:bid>', methods=['PUT'])
@require_module('leave')
def api_leave_balance_update(bid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE leave_balances SET total_days=%s, used_days=%s, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('total_days', 0)), float(b.get('used_days', 0)),
              b.get('note', ''), bid)).fetchone()
    return jsonify(leave_balance_row(row)) if row else ('', 404)


# ── Leave Summary (for salary integration) ────────────────────────

@bp.route('/api/leave/summary/<int:staff_id>/<month>', methods=['GET'])
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
        if pay_r == 0:  unpaid_days   += d
        elif pay_r < 1: half_pay_days += d
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
