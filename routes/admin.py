import hashlib
import json as _json
import os
import time

from flask import (
    Blueprint, make_response, request, jsonify,
    session, redirect, url_for, render_template, Response,
)

from auth import (
    login_required, require_super,
    _set_admin_session,
)
from db import (
    get_db,
    _badges_cache, _BADGES_TTL,
    _admin_html_cache, _admin_tmtime_cache,
    _hash_pw,
    _get_admin_by_username, _get_admin_by_id,
    _invalidate_admin_cache,
)

bp = Blueprint('admin', __name__)


def init():
    """Admin tables are created in init_db(); nothing to do here."""
    pass


# ─── Health ───────────────────────────────────────────────────────────────────

@bp.route('/health')
def health():
    try:
        with get_db() as conn:
            conn.execute('SELECT 1')
        return jsonify({'status': 'ok', 'db': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500


# ─── Root Redirect ────────────────────────────────────────────────────────────

@bp.route('/')
def index():
    return redirect(url_for('admin.admin_login'))


# ─── Admin Auth ───────────────────────────────────────────────────────────────

@bp.route('/admin/login', methods=['GET', 'POST'])
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
                    from flask import current_app
                    _set_admin_session(current_app._get_current_object(), row)
                    return redirect(url_for('admin.admin_dashboard'))
                error = '帳號或密碼錯誤'
            except Exception as e:
                print(f"[ERROR] admin_login db error: {e}")
                error = '系統錯誤，請稍後再試'
    return render_template('login.html', error=error)


@bp.route('/api/admin/login', methods=['POST'])
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
        from flask import current_app
        _set_admin_session(current_app._get_current_object(), row)
        return jsonify({'ok': True, 'redirect': '/admin'})
    except Exception as e:
        print(f"[ERROR] api_admin_login: {e}")
        return jsonify({'error': '系統錯誤，請稍後再試'}), 500


@bp.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin.admin_login'))


# ─── Admin Dashboard (HTML cache + ETag) ─────────────────────────────────────

@bp.route('/admin')
@bp.route('/admin/')
@login_required
def admin_dashboard():
    perms        = session.get('admin_permissions') or []
    is_super     = bool(session.get('admin_is_super'))
    admin_id     = session.get('admin_id', 0)
    display_name = session.get('admin_display_name', '')

    # 快取 template mtime（每 30 秒才重新讀一次 IO）
    now = time.time()
    tc  = _admin_tmtime_cache
    if now - tc['at'] > 30:
        from flask import current_app
        template_path = os.path.join(
            current_app.template_folder or 'templates', 'admin.html'
        )
        try:
            tc['mtime'] = int(os.path.getmtime(template_path))
        except OSError:
            tc['mtime'] = 0
        tc['at'] = now
    tmtime = tc['mtime']

    # ETag = template mtime + admin 身份（含 display_name，避免改名後顯示舊名）
    etag_src = f"{tmtime}:{admin_id}:{sorted(perms)}:{is_super}:{display_name}"
    etag = '"' + hashlib.md5(etag_src.encode()).hexdigest()[:16] + '"'
    if request.headers.get('If-None-Match') == etag:
        return ('', 304)

    # 伺服器端 HTML render 快取：相同 etag 只渲染一次，不同 worker 各自暖機後共享
    html = _admin_html_cache.get(etag)
    if html is None:
        html = render_template('admin.html',
            admin_display_name=display_name,
            admin_permissions=perms,
            admin_is_super=is_super,
        )
        # 只保留最近 20 個版本（多帳號 × 多權限組合），避免無限增長
        if len(_admin_html_cache) >= 20:
            _admin_html_cache.clear()
        _admin_html_cache[etag] = html

    resp = make_response(html)
    resp.headers['ETag']          = etag
    resp.headers['Cache-Control'] = 'private, no-cache'
    return resp


# ─── Admin Badges API ─────────────────────────────────────────────────────────

@bp.route('/api/admin/badges')
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


# ─── Admin Me API ─────────────────────────────────────────────────────────────

@bp.route('/api/admin/me', methods=['GET'])
@login_required
def api_admin_me():
    return jsonify({
        'id':           session.get('admin_id'),
        'username':     session.get('admin_username'),
        'display_name': session.get('admin_display_name'),
        'permissions':  session.get('admin_permissions') or [],
        'is_super':     bool(session.get('admin_is_super')),
    })


# ─── Admin Accounts API ───────────────────────────────────────────────────────

def _admin_row(r):
    if not r: return None
    d = dict(r)
    d.pop('password_hash', None)
    if 'password_plain' not in d: d['password_plain'] = ''
    perms = d.get('permissions')
    if isinstance(perms, str):
        try: d['permissions'] = _json.loads(perms)
        except (ValueError, TypeError): d['permissions'] = []
    if d.get('created_at'):    d['created_at']    = d['created_at'].isoformat()
    if d.get('last_login_at'): d['last_login_at'] = d['last_login_at'].isoformat()
    return d


@bp.route('/api/admin/accounts', methods=['GET'])
@require_super
def api_admin_accounts_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM admin_accounts ORDER BY id").fetchall()
    return jsonify([_admin_row(r) for r in rows])


@bp.route('/api/admin/accounts', methods=['POST'])
@require_super
def api_admin_account_create():
    b = request.get_json(force=True)
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password or len(password) < 4: return jsonify({'error': '密碼至少 4 個字元'}), 400
    perms = b.get('permissions', [])
    with get_db() as conn:
        try:
            row = conn.execute("""
                INSERT INTO admin_accounts (username, password_hash, password_plain, display_name, permissions, is_super, active)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (username, _hash_pw(password), password, b.get('display_name', '').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)), True)).fetchone()
        except Exception as e:
            if 'unique' in str(e).lower(): return jsonify({'error': '帳號已存在'}), 409
            return jsonify({'error': str(e)}), 500
    _invalidate_admin_cache()
    return jsonify(_admin_row(row)), 201


@bp.route('/api/admin/accounts/<int:aid>', methods=['PUT'])
@require_super
def api_admin_account_update(aid):
    b = request.get_json(force=True)
    username = b.get('username', '').strip()
    if not username: return jsonify({'error': '帳號為必填'}), 400
    password = b.get('password', '').strip()
    perms = b.get('permissions', [])
    with get_db() as conn:
        if password:
            if len(password) < 4: return jsonify({'error': '密碼至少 4 個字元'}), 400
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, password_hash=%s, password_plain=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, _hash_pw(password), password, b.get('display_name', '').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, b.get('display_name', '').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
    _invalidate_admin_cache()
    return jsonify(_admin_row(row)) if row else ('', 404)


@bp.route('/api/admin/accounts/<int:aid>', methods=['DELETE'])
@require_super
def api_admin_account_delete(aid):
    if aid == session.get('admin_id'):
        return jsonify({'error': '不能刪除自己的帳號'}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM admin_accounts WHERE id=%s", (aid,))
    _invalidate_admin_cache()
    return jsonify({'deleted': aid})
