"""LINE notification helpers shared across leave, salary, overtime, punch_req modules."""

import threading

from linebot import LineBotApi
from linebot.models import TextSendMessage

from db import get_db, DATABASE_URL


def _notify_staff_line(staff_id, message):
    """
    Send LINE notification to a staff member if they have LINE bound.
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
        api     = LineBotApi(cfg['channel_access_token'])
        snippet = content[:60] + ('…' if len(content) > 60 else '')
        msg     = f"[公告] {title}\n{snippet}\n\n請至員工系統查看完整公告。"
        for s in staff_rows:
            try:
                api.push_message(s['line_user_id'], TextSendMessage(text=msg))
            except Exception as e:
                print(f"[LINE broadcast] {s['line_user_id']}: {e}")
    except Exception as e:
        print(f"[LINE broadcast] error: {e}")


def _notify_review_result(staff_id, category, action, extra_info=''):
    """
    Send a formatted LINE notification for review results.
    category: '補打卡申請', '排休申請', '加班申請', '請假申請', '薪資確認'
    action:   'approve'/'approved', 'reject'/'rejected', 'confirmed', 'cancelled'
    """
    action = {'approve': 'approved', 'reject': 'rejected'}.get(action, action)
    ACTION_LABEL = {'approved': '核准', 'rejected': '退回', 'confirmed': '確認', 'cancelled': '取消'}
    ACTION_ICON  = {'approved': '[核准]', 'rejected': '[退回]', 'confirmed': '[確認]', 'cancelled': '[取消]'}
    label = ACTION_LABEL.get(action, action)
    icon  = ACTION_ICON.get(action, '')
    detail = f"\n{extra_info}" if extra_info else ''
    msg    = f"{icon} {category}{label}{detail}\n\n請至員工系統查看詳情。"
    _notify_staff_line(staff_id, msg.strip())
