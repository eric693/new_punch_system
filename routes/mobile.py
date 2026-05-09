import json as _json
import math
import os
from datetime import datetime as _dt, timedelta as _td, timezone as _tz, date
from functools import wraps

import jwt as _pyjwt
from flask import Blueprint, request, jsonify, session, g, current_app

from config import TW_TZ
from db import get_db, _hash_pw

bp = Blueprint('mobile', __name__)

_MOBILE_JWT_SECRET = os.environ.get('MOBILE_JWT_SECRET', '')
_JWT_EXPIRE_HOURS  = 24 * 7  # 7 days


def _get_jwt_secret():
    return _MOBILE_JWT_SECRET or current_app.secret_key


def _make_jwt(payload: dict) -> str:
    payload['exp'] = _dt.now(_tz.utc) + _td(hours=_JWT_EXPIRE_HOURS)
    return _pyjwt.encode(payload, _get_jwt_secret(), algorithm='HS256')


def _decode_jwt(token: str):
    return _pyjwt.decode(token, _get_jwt_secret(), algorithms=['HS256'])


def mobile_jwt_required(f):
    """Decorator: reads Bearer token, sets g.mobile_user = {id, role, ...}"""
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


@bp.route('/api/mobile/login', methods=['POST'])
def mobile_login():
    b = request.get_json(force=True) or {}
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '請輸入帳號及密碼'}), 400

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


@bp.route('/api/mobile/me', methods=['GET'])
@mobile_jwt_required
def mobile_me():
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


@bp.route('/api/mobile/punch', methods=['POST'])
@mobile_jwt_required
def mobile_punch():
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可打卡'}), 403
    staff_id = int(u['sub'])
    b = request.get_json(force=True) or {}
    punch_type = b.get('punch_type', 'in')
    lat  = b.get('latitude')
    lng  = b.get('longitude')
    note = b.get('note', '')

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


@bp.route('/api/mobile/punch/status', methods=['GET'])
@mobile_jwt_required
def mobile_punch_status():
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


@bp.route('/api/mobile/attendance', methods=['GET'])
@mobile_jwt_required
def mobile_attendance():
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


@bp.route('/api/mobile/leave/types', methods=['GET'])
@mobile_jwt_required
def mobile_leave_types():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, max_days FROM leave_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/mobile/leave', methods=['GET'])
@mobile_jwt_required
def mobile_leave_list():
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


@bp.route('/api/mobile/leave', methods=['POST'])
@mobile_jwt_required
def mobile_leave_apply():
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
            """INSERT INTO leave_requests (staff_id, leave_type_id, start_date, end_date, total_days, reason, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
            (staff_id, leave_type_id, start_date, end_date, days, reason)
        )
    return jsonify({'ok': True})


@bp.route('/api/mobile/schedule', methods=['GET'])
@mobile_jwt_required
def mobile_schedule():
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


@bp.route('/api/mobile/salary', methods=['GET'])
@mobile_jwt_required
def mobile_salary():
    u = g.mobile_user
    if u['role'] != 'employee':
        return jsonify({'error': '僅員工可查詢'}), 403
    staff_id = int(u['sub'])
    with get_db() as conn:
        rows = conn.execute(
            """SELECT sr.id, sr.month, sr.base_salary, sr.allowance_total, sr.deduction_total,
                      sr.net_pay, sr.ot_pay, sr.actual_work_hours, sr.leave_days, sr.absent_days,
                      sr.status, sr.confirmed_at, sr.created_at,
                      ps.salary_type, ps.hourly_rate
               FROM salary_records sr
               JOIN punch_staff ps ON ps.id = sr.staff_id
               WHERE sr.staff_id=%s ORDER BY sr.month DESC LIMIT 12""",
            (staff_id,)
        ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        for k in ('confirmed_at', 'created_at'):
            if d.get(k): d[k] = str(d[k])
        for k in ('base_salary', 'allowance_total', 'deduction_total', 'net_pay',
                  'ot_pay', 'actual_work_hours', 'leave_days', 'absent_days', 'hourly_rate'):
            if d.get(k) is not None: d[k] = float(d[k])
        data.append(d)
    return jsonify(data)


@bp.route('/api/mobile/overtime', methods=['POST'])
@mobile_jwt_required
def mobile_overtime():
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
    from datetime import date as _dt_today
    request_date = _dt_today.today().isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO overtime_requests
               (staff_id, ot_date, request_date, ot_hours, reason, status)
               VALUES (%s, %s, %s, %s, %s, 'pending')""",
            (staff_id, ot_date, request_date, hours, reason)
        )
    return jsonify({'ok': True})


@bp.route('/api/mobile/overtime', methods=['GET'])
@mobile_jwt_required
def mobile_overtime_list():
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


@bp.route('/api/mobile/admin/dashboard', methods=['GET'])
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


@bp.route('/api/mobile/admin/attendance/today', methods=['GET'])
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


@bp.route('/api/mobile/admin/leaves', methods=['GET'])
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


@bp.route('/api/mobile/admin/leaves/<int:lid>', methods=['PUT'])
@mobile_admin_required
def mobile_admin_leave_action(lid):
    b = request.get_json(force=True) or {}
    action = b.get('action')
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


@bp.route('/api/mobile/admin/overtime', methods=['GET'])
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


@bp.route('/api/mobile/admin/overtime/<int:oid>', methods=['PUT'])
@mobile_admin_required
def mobile_admin_overtime_action(oid):
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


@bp.route('/api/mobile/admin/staff', methods=['GET'])
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


@bp.route('/api/mobile/admin/anomalies', methods=['GET'])
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
