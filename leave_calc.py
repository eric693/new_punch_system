"""Leave calculation utilities shared between leave routes, salary, and LINE bot."""

from db import get_db


def _lv_parse_time(s):
    """Parse 'HH:MM' (full-width colon accepted), return (h, m) or raise ValueError."""
    s = (s or '').strip().replace('：', ':')
    parts = s.split(':')
    if len(parts) != 2:
        raise ValueError
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m


def _calc_annual_leave_days(hire_date_str, ref_date_str=None):
    """
    勞基法第38條特休天數計算（2017年修正版，現行有效）
    回傳當期應給特休天數（整數）
    """
    if not hire_date_str:
        return 0
    from datetime import date as _date
    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return 0

    ref = _date.today()
    if ref_date_str:
        try:
            ref = _date.fromisoformat(str(ref_date_str))
        except Exception:
            pass

    months = (ref.year - hire.year) * 12 + (ref.month - hire.month)
    if ref.day < hire.day:
        months -= 1
    if months < 0:
        months = 0

    years_complete = months // 12

    if months < 6:
        return 0
    elif months < 12:
        return 3
    elif years_complete < 2:
        return 7
    elif years_complete < 3:
        return 10
    elif years_complete < 5:
        return 14
    elif years_complete < 10:
        return 15
    else:
        extra = years_complete - 9
        return min(15 + extra, 30)


def _calc_annual_leave_schedule(hire_date_str):
    """
    回傳員工特休天數完整排程表，供前端顯示用。
    每一列：{ label, days, date_reached, is_past, is_current }
    """
    if not hire_date_str:
        return []
    from datetime import date as _date
    import calendar as _cal

    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return []

    today = _date.today()

    milestones = [
        (6,   3,  '滿6個月'),
        (12,  7,  '滿1年'),
        (24, 10,  '滿2年'),
        (36, 14,  '滿3年'),
        (60, 15,  '滿5年'),
        (120,16,  '滿10年'),
        (132,17,  '滿11年'),
        (144,18,  '滿12年'),
        (156,19,  '滿13年'),
        (168,20,  '滿14年'),
        (180,21,  '滿15年'),
        (192,22,  '滿16年'),
        (204,23,  '滿17年'),
        (216,24,  '滿18年'),
        (228,25,  '滿19年'),
        (240,30,  '滿20年（上限30天）'),
    ]

    result       = []
    current_days = _calc_annual_leave_days(hire_date_str)

    for months_needed, days, label in milestones:
        total_m = hire.month + months_needed
        y = hire.year + (total_m - 1) // 12
        m = (total_m - 1) % 12 + 1
        max_day = _cal.monthrange(y, m)[1]
        try:
            reached = _date(y, m, min(hire.day, max_day))
        except Exception:
            continue

        result.append({
            'label':        label,
            'days':         days,
            'date_reached': reached.isoformat(),
            'is_past':      reached <= today,
            'is_current':   (days == current_days and reached <= today),
        })

    return result


def _get_scheduled_dates(conn, staff_id, start_date, end_date):
    """回傳員工在 [start_date, end_date] 內的排班日期集合。
    若無排班記錄則回傳 None，讓呼叫方退回到週一至週五邏輯。"""
    rows = conn.execute("""
        SELECT DISTINCT shift_date FROM shift_assignments
        WHERE staff_id=%s AND shift_date BETWEEN %s AND %s
    """, (staff_id, start_date, end_date)).fetchall()
    if not rows:
        return None
    return {(r['shift_date'].isoformat() if hasattr(r['shift_date'], 'isoformat') else str(r['shift_date'])) for r in rows}


def _calc_leave_days(start_date_str, end_date_str, start_half=False, end_half=False,
                     start_time=None, end_time=None, scheduled_dates=None):
    """計算請假天數。
    若提供 scheduled_dates（set of 'YYYY-MM-DD'），以排班記錄判斷工作日（支援週末班）；
    否則排除週六週日（weekday < 5）。
    若提供 start_time/end_time（HH:MM），以 每日小時數 ÷ 8 計算，四捨五入至 0.5。
    否則沿用 start_half/end_half 半天旗標邏輯。
    """
    from datetime import date as _date, timedelta as _tdd
    try:
        s = _date.fromisoformat(start_date_str)
        e = _date.fromisoformat(end_date_str)
    except Exception:
        return 0.0
    if e < s: return 0.0

    def _is_workday(dt):
        if scheduled_dates is not None:
            return dt.isoformat() in scheduled_dates
        return dt.weekday() < 5

    if start_time and end_time:
        try:
            sh, sm = _lv_parse_time(start_time)
            eh, em = _lv_parse_time(end_time)
            daily_hours = (eh * 60 + em - sh * 60 - sm) / 60.0
        except (ValueError, Exception):
            daily_hours = 0.0
        if daily_hours > 0:
            working_days = sum(
                1 for i in range((e - s).days + 1)
                if _is_workday(s + _tdd(days=i))
            )
            raw = working_days * daily_hours / 8.0
            return max(0.5, round(raw * 2) / 2)

    days = 0.0
    cur  = s
    while cur <= e:
        if _is_workday(cur):
            if cur == s and cur == e:
                if start_half and end_half: days += 1.0
                elif start_half or end_half: days += 0.5
                else: days += 1.0
            elif cur == s and start_half: days += 0.5
            elif cur == e and end_half:   days += 0.5
            else: days += 1.0
        cur += _tdd(days=1)
    return days
