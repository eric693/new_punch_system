import os
from datetime import timezone, timedelta

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
ADMIN_PASSWORD            = os.environ.get('ADMIN_PASSWORD', 'admin123')
RENDER_EXTERNAL_URL       = os.environ.get('RENDER_EXTERNAL_URL', '')
ANTHROPIC_API_KEY         = os.environ.get('ANTHROPIC_API_KEY', '')

TW_TZ      = timezone(timedelta(hours=8))
WEEKDAY_ZH = ['一', '二', '三', '四', '五', '六', '日']
