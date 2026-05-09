import json as _json
import os
import traceback
import urllib.request
import urllib.error
from datetime import date as _date, datetime as _dt, timezone as _tz, timedelta as _td

from flask import Blueprint, request, jsonify, session

from auth import login_required
from db import get_db
from leave_calc import _lv_parse_time, _get_scheduled_dates, _calc_leave_days
from line_utils import (
    get_line_punch_config,
    _send_line_punch, _push_line_msg,
    _qr_pb, _flex_ask, _line_lv_time_qr,
    _line_conv_state, _LV_TIMES, _LV_TIME_PAGE, _line_reply_ctx,
)
from routes.performance import _grade_labels
from routes.punch import _gps_distance

bp = Blueprint('line_bot', __name__)

CUSTOM_RICHMENU_IMAGE_PATH = '/tmp/custom_richmenu.png'
_pending_line_punches: dict = {}   # {line_user_id: punch_type}


def init():
    sqls = [
        """CREATE TABLE IF NOT EXISTS line_punch_config (
            id                   INT PRIMARY KEY DEFAULT 1,
            channel_access_token TEXT DEFAULT '',
            channel_secret       TEXT DEFAULT '',
            enabled              BOOLEAN DEFAULT FALSE,
            updated_at           TIMESTAMPTZ DEFAULT NOW()
        )""",
        "INSERT INTO line_punch_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        "ALTER TABLE line_punch_config ADD COLUMN IF NOT EXISTS richmenu_area_texts JSONB DEFAULT NULL",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[line_bot_init] {e}")


# ── Leave interactive flow ────────────────────────────────────────

def _line_leave_start(staff, user_id):
    with get_db() as conn:
        types = conn.execute(
            "SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
    if not types:
        _send_line_punch(user_id, '目前無可用假別，請聯絡管理員。')
        return
    _line_conv_state[user_id] = {'flow': 'leave', 'step': 1, 'data': {}, 'all_types': [t['name'] for t in types]}
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
    step = state['step']
    data = state['data']
    today = _date.today().isoformat()
    tmrw  = (_date.today() + _td(days=1)).isoformat()

    if pb_data == 'cancel' or text == '取消':
        _line_conv_state.pop(user_id, None)
        _send_line_punch(user_id, '已取消請假申請。')
        return

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
            _date.fromisoformat(raw)
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
            _date.fromisoformat(raw)
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
        st_mins = sh * 60 + sm
        et_times = [t for t in _LV_TIMES if int(t[:2]) * 60 + int(t[3:]) > st_mins]
        state['et_times'] = et_times
        date_range = data['start_date'] + (f' ～ {data["end_date"]}' if data['end_date'] != data['start_date'] else '')
        _line_lv_time_qr(user_id,
            f'假別：{data["leave_type"]}\n日期：{date_range}\n開始：{start_time}\n\n請選擇結束時間',
            '點選按鈕或輸入 HH:MM（如 18:00）',
            'lv_et=', 'lv_et_p=', et_times, 0)

    elif step == 5:
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
    try:
        _date.fromisoformat(start_date); _date.fromisoformat(end_date)
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

        sched = _get_scheduled_dates(conn, staff['id'], start_date, end_date)
        days = _calc_leave_days(start_date, end_date, start_time=start_time, end_time=end_time,
                                scheduled_dates=sched)
        year = int(start_date[:4])
        bal = conn.execute("""
            SELECT total_days, used_days FROM leave_balances
            WHERE staff_id=%s AND leave_type_id=%s AND year=%s
        """, (staff['id'], lt['id'], year)).fetchone()

        remain = None
        if bal:
            quota = float(bal['total_days']) if bal['total_days'] else (float(lt['max_days']) if lt['max_days'] else None)
            used  = float(bal['used_days'] or 0)
            if quota is not None:
                remain = quota - used
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


# ── Overtime interactive flow ─────────────────────────────────────

def _line_ot_start(staff, user_id):
    today = _date.today().isoformat()
    tmrw  = (_date.today() + _td(days=1)).isoformat()
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
    step = state['step']
    data = state['data']
    today = _date.today().isoformat()

    if pb_data == 'cancel' or text == '取消':
        _line_conv_state.pop(user_id, None)
        _send_line_punch(user_id, '已取消加班申請。')
        return

    def _parse_time(s):
        s = (s or '').strip().replace('：', ':')
        h, m = s.split(':')
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError
        return h, m

    if step == 1:
        raw = (pb_data[8:] if pb_data and pb_data.startswith('ot_date=')
               else (text.strip() if text else None))
        try:
            _date.fromisoformat(raw)
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
                  (staff_id, ot_date, request_date, start_time, end_time, ot_hours, reason, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id
            """, (staff['id'], d['date'], d['date'], d['start_time'], d['end_time'],
                  d['hours'], reason or '（LINE 加班申請）')).fetchone()
        _send_line_punch(user_id,
            f'✅ 加班申請已送出\n\n'
            f'日期：{d["date"]}\n'
            f'時間：{d["start_time"]} ～ {d["end_time"]}\n'
            f'時數：{d["hours"]}h\n'
            + (f'原因：{reason}\n' if reason else '')
            + f'申請編號：#{row["id"]}\n\n'
            '請等候管理員審核，審核結果將通知您。')


# ── Webhook ───────────────────────────────────────────────────────

@bp.route('/line-punch/webhook', methods=['POST'])
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

    PUNCH_CMDS = {
        '上班': 'in', '上班打卡': 'in',
        '下班': 'out', '下班打卡': 'out',
        '休息': 'break_out', '休息開始': 'break_out',
        '回來': 'break_in', '休息結束': 'break_in',
    }
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
                msg_obj = _flex_ask('📍 需要位置驗證', '#2980B9',
                                f'請傳送您的位置來完成{PUNCH_LABEL[punch_type]}',
                                '點下方「傳送位置」按鈕即可打卡')
                msg_obj['quickReply'] = {'items': [
                    {'type': 'action', 'action': {'type': 'location', 'label': '📍 傳送位置'}}
                ]}
                _push_line_msg(user_id, msg_obj)
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
            _line_submit_leave(staff, user_id, text)
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
            _line_submit_overtime(staff, user_id, text)
        elif text in ('選單', '功能', '菜單', '?', '？', 'help', 'Help', 'HELP'):
            _line_show_help(staff, user_id)
        else:
            _line_show_help(staff, user_id)


def _do_line_punch(staff, user_id, lat, lng, forced_type, PUNCH_LABEL):
    TW = _tz(_td(hours=8))

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
            _TW_now = _tz(_td(hours=8))
            _last_at = last.get('punched_at')
            if _last_at is not None:
                if _last_at.tzinfo is None:
                    _last_at = _last_at.replace(tzinfo=_tz.utc)
                _mins_since = (_dt.now(_TW_now) - _last_at.astimezone(_TW_now)).total_seconds() / 60
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

    now      = _dt.now(TW)
    gps_info = f'\n📍 {matched_name} ({gps_distance}m)' if gps_distance is not None else ''
    _send_line_punch(user_id,
        f'✅ {label}成功\n'
        f'👤 {staff["name"]}\n'
        f'🕐 {now.strftime("%Y/%m/%d %H:%M")}'
        f'{gps_info}')


def _send_status(staff, user_id):
    TW = _tz(_td(hours=8))
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
            pa = pa.replace(tzinfo=_tz.utc)
        t    = pa.astimezone(TW).strftime('%H:%M')
        dist = f' ({r["gps_distance"]}m)' if r['gps_distance'] is not None else ''
        man  = ' [補打]' if r['is_manual'] else ''
        lines.append(f'• {LABEL.get(r["punch_type"], r["punch_type"])} {t}{dist}{man}')
    _send_line_punch(user_id, '\n'.join(lines))


# ── Admin LINE Punch Config API ────────────────────────────────────

@bp.route('/api/line-punch/config', methods=['GET'])
@login_required
def api_line_punch_config_get():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
    if not row:
        return jsonify({'enabled': False, 'channel_access_token': '', 'channel_secret': ''})
    d = dict(row)
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return jsonify(d)


@bp.route('/api/line-punch/config', methods=['PUT'])
@login_required
def api_line_punch_config_put():
    b       = request.get_json(force=True)
    enabled = bool(b.get('enabled', False))
    token   = b.get('channel_access_token')
    secret  = b.get('channel_secret')
    with get_db() as conn:
        if token is not None or secret is not None:
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
            conn.execute("""
                UPDATE line_punch_config SET enabled=%s, updated_at=NOW() WHERE id=1
            """, (enabled,))
    return jsonify({'ok': True, 'enabled': enabled})


@bp.route('/api/line-punch/staff', methods=['GET'])
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


@bp.route('/api/line-punch/staff/<int:sid>/unbind', methods=['POST'])
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
    import re
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
        if data[:8] == b'\x89PNG\r\n\x1a\n' or data[:2] == b'\xff\xd8':
            return data
        return None
    except Exception:
        return None


def _make_richmenu_png():
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


@bp.route('/api/line-punch/richmenu/create', methods=['POST'])
@login_required
def api_richmenu_create():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '請先設定 Channel Access Token'}), 400

    body_json  = request.get_json(silent=True) or {}
    gdrive_url = (body_json.get('gdrive_url') or '').strip()
    raw_texts  = body_json.get('area_texts') or []
    defaults   = ['上班', '下班', '請假', '加班']
    area_texts = [(raw_texts[i].strip() if i < len(raw_texts) and raw_texts[i].strip() else defaults[i])
                  for i in range(4)]

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

    _call_line_api(cfg, 'POST', f'/user/all/richmenu/{rich_menu_id}')
    return jsonify({'ok': True, 'rich_menu_id': rich_menu_id, 'image_uploaded': img_ok})


@bp.route('/api/line-punch/richmenu/list', methods=['GET'])
@login_required
def api_richmenu_list():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'menus': []})
    status, data = _call_line_api(cfg, 'GET', '/richmenu/list')
    if status != 200:
        return jsonify({'menus': [], 'error': data.get('error', '')})
    return jsonify({'menus': data.get('richmenus', [])})


@bp.route('/api/line-punch/richmenu/<rich_menu_id>', methods=['DELETE'])
@login_required
def api_richmenu_delete(rich_menu_id):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '未設定 Token'}), 400
    _call_line_api(cfg, 'DELETE', '/user/all/richmenu')
    status, _ = _call_line_api(cfg, 'DELETE', f'/richmenu/{rich_menu_id}')
    return jsonify({'ok': status in (200, 204), 'status': status})


@bp.route('/api/line-punch/richmenu/default', methods=['DELETE'])
@login_required
def api_richmenu_unset_default():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '未設定 Token'}), 400
    status, _ = _call_line_api(cfg, 'DELETE', '/user/all/richmenu')
    return jsonify({'ok': status in (200, 204)})


# ── Query / Display helpers (called from webhook handler) ──────────

def _line_query_leave_balance(staff, user_id):
    year = _date.today().year
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT lb.total_days, lb.used_days, lt.name AS type_name, lt.max_days
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
        total = float(r['total_days']) if r['total_days'] else (float(r['max_days']) if r['max_days'] else 0.0)
        used  = float(r['used_days']  or 0)
        remain= total - used
        if r['max_days'] is None:
            lines.append(f'\n【{r["type_name"]}】\n  剩餘 {remain:.1f} 天（無上限）')
        else:
            bar = '▓' * int(remain) + '░' * max(0, int(total - remain))
            lines.append(f'\n【{r["type_name"]}】\n  剩餘 {remain:.1f} 天 / 共 {total:.0f} 天\n  {bar}')
    _send_line_punch(user_id, '\n'.join(lines))


def _line_query_salary(staff, user_id):
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
    import re as _re_lv
    parts = text.strip().split()
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

    try:
        _date.fromisoformat(date_str1)
        _date.fromisoformat(date_str2)
    except ValueError:
        _send_line_punch(user_id, f'日期格式錯誤，請使用 YYYY-MM-DD，例：{_date.today().isoformat()}')
        return

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

        year = date_str1[:4]
        bal = conn.execute("""
            SELECT total_days, used_days FROM leave_balances
            WHERE staff_id=%s AND leave_type_id=%s AND year=%s
        """, (staff['id'], lt['id'], int(year))).fetchone()

        sched = _get_scheduled_dates(conn, staff['id'], date_str1, date_str2)
        days = _calc_leave_days(date_str1, date_str2, scheduled_dates=sched)

        remain = None
        if bal:
            quota = float(bal['total_days']) if bal['total_days'] else (float(lt['max_days']) if lt['max_days'] else None)
            used  = float(bal['used_days'] or 0)
            if quota is not None:
                remain = quota - used
                if remain < days:
                    _send_line_punch(user_id,
                        f'⚠️ {lt["name"]} 餘額不足\n剩餘 {remain:.1f} 天，申請 {days} 天\n\n'
                        f'請至員工系統調整後再申請。')
                    return

        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, total_days,
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
    import re as _rem
    TW = _tz(_td(hours=8))

    parts = text.strip().split()
    month = None
    if len(parts) >= 2:
        m = _rem.match(r'^(\d{4})-(\d{1,2})$', parts[1])
        if m:
            month = f"{m.group(1)}-{m.group(2).zfill(2)}"
    if not month:
        month = _dt.now(TW).strftime('%Y-%m')

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
    days = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tz.utc)
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
        d = _date.fromisoformat(ds)
        wday = WDAY[d.weekday()]

        clock_in  = next((r['time'] for r in recs if r['type'] == 'in'),  None)
        clock_out = next((r['time'] for r in recs if r['type'] == 'out'), None)
        has_manual = any(r['manual'] for r in recs)

        if clock_in and clock_out:
            ci = _dt.strptime(f'{ds} {clock_in}',  '%Y-%m-%d %H:%M')
            co = _dt.strptime(f'{ds} {clock_out}', '%Y-%m-%d %H:%M')
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
    import re as _re_ot
    parts = text.strip().split(None, 3)
    if len(parts) < 3:
        _send_line_punch(user_id,
            '加班申請格式：\n加班 [日期] [時數] [原因]\n\n'
            '範例：加班 2026-04-05 3 業績衝刺\n'
            '（時數可用小數，如 1.5）')
        return
    date_str = parts[1]
    try:
        _date.fromisoformat(date_str)
    except ValueError:
        _send_line_punch(user_id, f'日期格式錯誤，請使用 YYYY-MM-DD，例：{_date.today().isoformat()}')
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
              (staff_id, ot_date, request_date, start_time, end_time, ot_hours, reason, status)
            VALUES (%s, %s, %s, NULL, NULL, %s, %s, 'pending')
            RETURNING id
        """, (staff['id'], date_str, date_str, hours, reason)).fetchone()

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
