import math
import time as _time
import calendar as _cal
from datetime import datetime as _dt, timedelta as _td, timezone as _tz, date as _date
from collections import defaultdict

import psycopg
from flask import Blueprint, request, jsonify, session, render_template

from auth import login_required
from config import TW_TZ
from db import (
    get_db, _hash_pw,
    _invalidate_cfg_cache, _invalidate_locs_cache,
    _get_cfg_cached, _get_locs_cached,
    _punch_summary_cache, _SUMMARY_TTL,
)

bp = Blueprint('punch', __name__)


def init():
    pass  # core tables created in init_db()


# ── Row helpers ───────────────────────────────────────────────────

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


# ── Pages ─────────────────────────────────────────────────────────

@bp.route('/punch')
@bp.route('/staff')
def punch_page():
    return render_template('staff.html')


# ── Employee Session ──────────────────────────────────────────────

@bp.route('/api/punch/login', methods=['POST'])
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


@bp.route('/api/punch/logout', methods=['POST'])
def api_punch_logout():
    session.pop('punch_staff_id', None)
    session.pop('punch_staff_name', None)
    return jsonify({'ok': True})


@bp.route('/api/punch/me', methods=['GET'])
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

@bp.route('/api/punch/settings', methods=['GET'])
def api_punch_settings_get():
    with get_db() as conn:
        cfg  = _get_cfg_cached(conn)
        locs = _get_locs_cached(conn)
    return jsonify({
        'gps_required': cfg['gps_required'] if cfg else False,
        'locations': [loc_row(r) for r in locs]
    })


def _fetch_today_log(conn, staff_id):
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


@bp.route('/api/punch/init', methods=['GET'])
def api_punch_init():
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


@bp.route('/api/punch/config', methods=['PUT'])
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


@bp.route('/api/punch/locations', methods=['GET'])
@login_required
def api_punch_locations_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_locations ORDER BY id").fetchall()
    return jsonify([loc_row(r) for r in rows])


@bp.route('/api/punch/locations', methods=['POST'])
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


@bp.route('/api/punch/locations/<int:lid>', methods=['PUT'])
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


@bp.route('/api/punch/locations/<int:lid>', methods=['DELETE'])
@login_required
def api_punch_locations_delete(lid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_locations WHERE id=%s", (lid,))
    _invalidate_locs_cache()
    return jsonify({'deleted': lid})


# ── Clock In/Out ──────────────────────────────────────────────────

@bp.route('/api/punch/clock', methods=['POST'])
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


@bp.route('/api/punch/today', methods=['GET'])
def api_punch_today():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify([])
    now_tw = _dt.now(TW_TZ)
    today_start    = _dt(now_tw.year, now_tw.month, now_tw.day, tzinfo=TW_TZ)
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


@bp.route('/api/punch/my-records', methods=['GET'])
def api_punch_my_records():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    month = request.args.get('month', '')
    if not month:
        month = _dt.now(TW_TZ).strftime('%Y-%m')
    _y_mr, _m_mr = int(month[:4]), int(month[5:])
    month_start = _dt(_y_mr, _m_mr, 1, tzinfo=TW_TZ)
    last_day    = _cal.monthrange(_y_mr, _m_mr)[1]
    month_end   = _dt(_y_mr, _m_mr, last_day, tzinfo=TW_TZ) + _td(days=1)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND punched_at >= %s AND punched_at < %s
            ORDER BY punched_at ASC
        """, (sid, month_start, month_end)).fetchall()
    TW = _tz(_td(hours=8))
    LABEL = {'in': '上班', 'out': '下班', 'break_out': '休息開始', 'break_in': '休息結束'}
    result = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tz.utc)
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

@bp.route('/api/punch/staff', methods=['GET'])
@login_required
def api_punch_staff_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_staff ORDER BY sort_order, name").fetchall()
    return jsonify([punch_staff_row(r) for r in rows])


@bp.route('/api/punch/staff/reorder', methods=['POST'])
@login_required
def api_punch_staff_reorder():
    ids = request.get_json(force=True)
    if not isinstance(ids, list):
        return jsonify({'error': 'expected list of ids'}), 400
    with get_db() as conn:
        for idx, sid in enumerate(ids):
            conn.execute("UPDATE punch_staff SET sort_order=%s WHERE id=%s", (idx, sid))
    return jsonify({'ok': True})


@bp.route('/api/punch/staff', methods=['POST'])
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
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
        return jsonify({'error': f'新增失敗：{str(e)}'}), 500


@bp.route('/api/punch/staff/<int:sid>', methods=['PUT'])
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
    bank_code      = (b.get('bank_code') or '').strip()
    bank_name      = (b.get('bank_name') or '').strip()
    bank_branch    = (b.get('bank_branch') or '').strip()
    bank_account   = (b.get('bank_account') or '').strip()
    account_holder = (b.get('account_holder') or '').strip()
    department     = (b.get('department') or '').strip()
    position_title = (b.get('position_title') or '').strip()
    hire_date      = b.get('hire_date') or None
    birth_date     = b.get('birth_date') or None
    national_id    = (b.get('national_id') or '').strip()
    gender         = (b.get('gender') or '').strip()
    address        = (b.get('address') or '').strip()
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


@bp.route('/api/punch/staff/<int:sid>', methods=['DELETE'])
@login_required
def api_punch_staff_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_staff WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})


# ── Admin: Punch Records ──────────────────────────────────────────

@bp.route('/api/punch/records', methods=['GET'])
@login_required
def api_punch_records():
    staff_id  = request.args.get('staff_id')
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    month     = request.args.get('month')

    conds, params = ["TRUE"], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if month:
        _ypr, _mpr = int(month[:4]), int(month[5:])
        _pr_start = _dt(_ypr, _mpr, 1, tzinfo=TW_TZ)
        _pr_end   = _dt(_ypr, _mpr, _cal.monthrange(_ypr, _mpr)[1], tzinfo=TW_TZ) + _td(days=1)
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


@bp.route('/api/punch/records', methods=['POST'])
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


@bp.route('/api/punch/records/<int:rid>', methods=['PUT'])
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


@bp.route('/api/punch/records/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


# ── Admin: Attendance Summary ─────────────────────────────────────

@bp.route('/api/punch/summary', methods=['GET'])
@login_required
def api_punch_summary():
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    _ps_now = _time.time()
    _ps_c = _punch_summary_cache.get(month)
    if _ps_c and _ps_now - _ps_c['at'] < _SUMMARY_TTL:
        return jsonify(_ps_c['data'])
    _y2, _m2 = int(month[:4]), int(month[5:])
    _range_start = _dt(_y2, _m2, 1, tzinfo=TW_TZ)
    _last2       = _cal.monthrange(_y2, _m2)[1]
    _range_end   = _dt(_y2, _m2, _last2, tzinfo=TW_TZ) + _td(days=2)

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

    day_map = {}
    for r in rows:
        key = (r['staff_id'], r['work_date'].isoformat())
        day_map[key] = dict(r)

    merged = set()
    for (sid, ds), row in list(day_map.items()):
        if row['clock_in'] and not row['clock_out']:
            nds  = (_date.fromisoformat(ds) + _td(days=1)).isoformat()
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
            ci = _dt.fromisoformat(d['clock_in'])
            co = _dt.fromisoformat(d['clock_out'])
            d['duration_min'] = max(0, int((co - ci).total_seconds() / 60))
        else:
            d['duration_min'] = None
        result.append(d)

    result.sort(key=lambda x: (x['work_date'], x['staff_name']), reverse=True)
    _punch_summary_cache[month] = {'data': result, 'at': _ps_now}
    return jsonify(result)


@bp.route('/api/attendance/monthly-stats', methods=['GET'])
@login_required
def api_attendance_monthly_stats():
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    _y3, _m3 = int(month[:4]), int(month[5:])
    _last3        = _cal.monthrange(_y3, _m3)[1]
    _range3_start = _dt(_y3, _m3, 1, tzinfo=TW_TZ)
    _range3_end   = _dt(_y3, _m3, _last3, tzinfo=TW_TZ) + _td(days=2)

    with get_db() as conn:
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

        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time
            FROM shift_assignments sa
            JOIN shift_types st ON st.id = sa.shift_type_id
            WHERE TO_CHAR(sa.shift_date,'YYYY-MM') = %s
        """, (month,)).fetchall()
        shift_map = {(r['staff_id'], str(r['shift_date'])): r for r in shift_rows}

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

    merged = set()
    for (sid, ds), row in list(day_map.items()):
        if row['has_in'] and not row['has_out']:
            nds  = (_date.fromisoformat(ds) + _td(days=1)).isoformat()
            nkey = (sid, nds)
            if nkey in day_map and nkey not in merged:
                nrow = day_map[nkey]
                if nrow['has_out'] and not nrow['has_in']:
                    ci, co = row['clock_in'], nrow['clock_out']
                    if ci and co and (co - ci).total_seconds() <= 86400:
                        row['clock_out'] = co
                        row['has_out']   = True
                        merged.add(nkey)

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

        if has_in and not has_out:
            s['missing_out_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_out', 'label': '缺下班卡'})
        if not has_in and has_out:
            s['missing_in_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_in', 'label': '缺上班卡'})

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

@bp.route('/api/punch/request', methods=['POST'])
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


@bp.route('/api/punch/request/my', methods=['GET'])
def api_punch_req_my():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM punch_requests WHERE staff_id=%s ORDER BY requested_at DESC LIMIT 20",
            (sid,)
        ).fetchall()
    return jsonify([punch_req_row(r) for r in rows])


@bp.route('/api/punch/requests', methods=['GET'])
@login_required
def api_punch_reqs_list():
    status = request.args.get('status', '')
    ym     = request.args.get('ym', '')
    conds, params = ['TRUE'], []
    if status: conds.append('pr.status=%s'); params.append(status)
    if ym:
        try:
            y, m = ym.split('-'); y, m = int(y), int(m)
            start = f"{y:04d}-{m:02d}-01"
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            end = f"{ny:04d}-{nm:02d}-01"
            conds.append('pr.requested_at >= %s AND pr.requested_at < %s')
            params.extend([start, end])
        except Exception:
            pass
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_requests pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.created_at DESC LIMIT 200
        """, params).fetchall()
    return jsonify([punch_req_row(r) for r in rows])


@bp.route('/api/punch/requests/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_req_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})
