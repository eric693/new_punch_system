"""LINE Bot API utilities: config loader, send helpers, UI builders."""

import json as _json
import threading
import urllib.request

from linebot import LineBotApi, WebhookHandler
from linebot.models import TextSendMessage

from config import LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET
from db import get_db, DATABASE_URL

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# Per-webhook reply-token context (thread-local)
_line_reply_ctx = threading.local()

# In-progress LINE Bot conversation state: {line_user_id: {'flow', 'step', 'data', ...}}
_line_conv_state: dict = {}

# Half-hour time slots 00:00–23:30 for leave start/end selection
_LV_TIMES     = [f'{h:02d}:{m:02d}' for h in range(24) for m in (0, 30)]
_LV_TIME_PAGE = 10


def get_line_punch_config():
    if not DATABASE_URL: return None
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _send_line_punch(user_id, text):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    token = getattr(_line_reply_ctx, 'reply_token', None)
    try:
        api = LineBotApi(cfg['channel_access_token'])
        if token:
            _line_reply_ctx.reply_token = None
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
        _line_reply_ctx.reply_token = None
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
