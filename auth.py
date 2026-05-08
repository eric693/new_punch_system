import hashlib
import os
import threading
import time
from functools import wraps

from flask import session, request, redirect, url_for, jsonify, render_template

from db import (
    get_db, _hash_pw, _get_admin_by_username,
    _admin_html_cache, _admin_tmtime_cache,
    _invalidate_admin_cache,
)


# ─── Decorators ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': '請先登入'}), 401
            return redirect(url_for('admin.admin_login'))
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
                return redirect(url_for('admin.admin_login'))
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


# ─── Session Helpers ──────────────────────────────────────────────────────────

def _update_last_login(admin_id):
    try:
        with get_db() as conn:
            conn.execute("UPDATE admin_accounts SET last_login_at=NOW() WHERE id=%s", (admin_id,))
    except Exception:
        pass


def _prewarm_admin_html(flask_app, admin_id, display_name, perms, is_super):
    """Login background pre-render of admin.html into cache."""
    try:
        now = time.time()
        tc  = _admin_tmtime_cache
        if now - tc['at'] > 30:
            template_path = os.path.join(flask_app.template_folder or 'templates', 'admin.html')
            try:
                tc['mtime'] = int(os.path.getmtime(template_path))
            except OSError:
                tc['mtime'] = 0
            tc['at'] = now
        tmtime = tc['mtime']
        etag_src = f"{tmtime}:{admin_id}:{sorted(perms)}:{is_super}:{display_name}"
        etag = '"' + hashlib.md5(etag_src.encode()).hexdigest()[:16] + '"'
        if etag in _admin_html_cache:
            return
        with flask_app.app_context():
            html = render_template('admin.html',
                admin_display_name=display_name,
                admin_permissions=perms,
                admin_is_super=is_super,
            )
        if len(_admin_html_cache) >= 20:
            _admin_html_cache.clear()
        _admin_html_cache[etag] = html
    except Exception as e:
        print(f"[prewarm admin html] {e}")


def _set_admin_session(flask_app, row):
    import json as _json
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
    threading.Thread(
        target=_prewarm_admin_html,
        args=(flask_app, row['id'], row['display_name'] or row['username'], perms, bool(row['is_super'])),
        daemon=True,
    ).start()
