import json as _json
import time
from datetime import datetime as _dt, date, timedelta

from flask import Blueprint, request, jsonify, session

from auth import login_required, require_module
from config import TW_TZ
from db import get_db, _salary_items_cache, _SEMISTATIC_TTL
from leave_calc import _calc_annual_leave_days, _calc_leave_days
from notifications import _notify_review_result

bp = Blueprint('salary', __name__)


# ─── DB init ──────────────────────────────────────────────────────────────────

def init():
    migrations = [
        """CREATE TABLE IF NOT EXISTS salary_items (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            item_type   TEXT NOT NULL DEFAULT 'allowance',
            formula     TEXT DEFAULT '',
            amount      NUMERIC(12,2) DEFAULT 0,
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#4a7bda',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_ids JSONB DEFAULT NULL",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_overrides JSONB DEFAULT NULL",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS income_tax_withheld NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS actual_work_hours NUMERIC(10,2) DEFAULT 0",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS punch_details JSONB DEFAULT '[]'",
        """CREATE TABLE IF NOT EXISTS salary_advances (
            id           SERIAL PRIMARY KEY,
            staff_id     INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
            advance_date DATE NOT NULL,
            deduct_month TEXT NOT NULL DEFAULT '',
            note         TEXT DEFAULT '',
            status       TEXT DEFAULT 'pending',
            created_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS salary_records (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            month           TEXT NOT NULL,
            base_salary     NUMERIC(12,2) DEFAULT 0,
            insured_salary  NUMERIC(12,2) DEFAULT 0,
            work_days       NUMERIC(5,1)  DEFAULT 0,
            actual_days     NUMERIC(5,1)  DEFAULT 0,
            leave_days      NUMERIC(5,1)  DEFAULT 0,
            unpaid_days     NUMERIC(5,1)  DEFAULT 0,
            ot_pay          NUMERIC(12,2) DEFAULT 0,
            allowance_total NUMERIC(12,2) DEFAULT 0,
            deduction_total NUMERIC(12,2) DEFAULT 0,
            net_pay         NUMERIC(12,2) DEFAULT 0,
            items           JSONB         DEFAULT '[]',
            status          TEXT          DEFAULT 'draft',
            note            TEXT          DEFAULT '',
            confirmed_by    TEXT          DEFAULT '',
            confirmed_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ   DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   DEFAULT NOW(),
            UNIQUE(staff_id, month)
        )""",
        # ── 薪資索引 ──────────────────────────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_salary_advances_staff ON salary_advances(staff_id)",
        "CREATE INDEX IF NOT EXISTS idx_salary_advances_staff_status ON salary_advances(staff_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_salary_advances_deduct_month ON salary_advances(deduct_month)",
        "CREATE INDEX IF NOT EXISTS idx_salary_records_staff_month ON salary_records(staff_id, month)",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[salary_init] {str(e)[:80]}")

    # Seed default salary items
    defaults = [
        ('本薪',        'allowance', 'base_salary+service_years*1000', 0,    '#2e9e6b', 1),
        ('職務加給',    'allowance', '',                                0,    '#0ea5e9', 2),
        ('全勤獎金',    'allowance', '',                                0,    '#c8a96e', 3),
        ('獎金',        'allowance', '',                                0,    '#8b5cf6', 4),
        ('生日禮金',    'allowance', '',                                1000, '#e05c8a', 5),
        ('勞退6%',      'allowance', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 6),
        ('病/事/假',    'deduction', '',                                0,    '#8892a4', 7),
        ('勞保費',      'deduction', 'insured_salary*0.125*0.2',       0,    '#d64242', 8),
        ('健保費',      'deduction', 'insured_salary*0.0517*0.3',      0,    '#e07b2a', 9),
        ('勞退提撥6%',  'deduction', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 10),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM salary_items").fetchone()['c']
            if cnt == 0:
                for name, itype, formula, amount, color, sort in defaults:
                    conn.execute("""
                        INSERT INTO salary_items (name,item_type,formula,amount,color,sort_order)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (name, itype, formula, amount, color, sort))
    except Exception as e:
        print(f"[salary_seed] {e}")


# ─── Row helpers ──────────────────────────────────────────────────────────────

def salary_item_row(row):
    if not row:
        return None
    d = dict(row)
    if d.get('amount') is not None:
        d['amount'] = float(d['amount'])
    if d.get('created_at'):
        d['created_at'] = d['created_at'].isoformat()
    return d


def salary_record_row(row):
    if not row:
        return None
    d = dict(row)
    for f in ['base_salary', 'insured_salary', 'work_days', 'actual_days', 'leave_days',
              'unpaid_days', 'ot_pay', 'allowance_total', 'deduction_total', 'net_pay',
              'actual_work_hours']:
        if d.get(f) is not None:
            d[f] = float(d[f])
    if isinstance(d.get('items'), str):
        try:
            d['items'] = _json.loads(d['items'])
        except (ValueError, TypeError):
            d['items'] = []
    if isinstance(d.get('punch_details'), str):
        try:
            d['punch_details'] = _json.loads(d['punch_details'])
        except (ValueError, TypeError):
            d['punch_details'] = []
    if d.get('punch_details') is None:
        d['punch_details'] = []
    if d.get('confirmed_at'):
        d['confirmed_at'] = d['confirmed_at'].isoformat()
    if d.get('created_at'):
        d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'):
        d['updated_at'] = d['updated_at'].isoformat()
    return d


def _punch_staff_row(row):
    """Minimal version of punch_staff_row used by salary staff update."""
    if not row:
        return None
    d = dict(row)
    d['has_password'] = bool(d.get('password_hash'))
    d.pop('password_hash', None)
    if 'password_plain' not in d:
        d['password_plain'] = ''
    if d.get('created_at'):
        d['created_at'] = d['created_at'].isoformat()
    if d.get('hire_date'):
        d['hire_date'] = d['hire_date'].isoformat()
    if d.get('birth_date'):
        d['birth_date'] = d['birth_date'].isoformat()
    return d


def _advance_row(r):
    d = dict(r)
    if d.get('advance_date'):
        d['advance_date'] = (d['advance_date'].isoformat()
                             if hasattr(d['advance_date'], 'isoformat')
                             else str(d['advance_date']))
    if d.get('created_at'):
        d['created_at'] = d['created_at'].isoformat()
    d['amount'] = float(d.get('amount') or 0)
    return d


# ─── Formula / service-years helpers ─────────────────────────────────────────

def _eval_formula(formula, base_salary, insured_salary, service_years):
    """安全計算薪資公式（僅允許數值四則運算，不使用 eval）"""
    import ast as _ast
    if not formula:
        return 0.0

    _ALLOWED_NODES = (
        _ast.Expression, _ast.BinOp, _ast.UnaryOp, _ast.Constant,
        _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.FloorDiv,
        _ast.Mod, _ast.Pow, _ast.USub, _ast.UAdd, _ast.Name,
    )
    _vars = {
        'base_salary':    float(base_salary or 0),
        'insured_salary': float(insured_salary or 0),
        'service_years':  float(service_years or 0),
    }

    def _safe_eval(node):
        if isinstance(node, _ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, _ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError('非數值常數')
            return float(node.value)
        if isinstance(node, _ast.Name):
            if node.id not in _vars:
                raise ValueError(f'未知變數：{node.id}')
            return _vars[node.id]
        if isinstance(node, _ast.BinOp):
            l, r = _safe_eval(node.left), _safe_eval(node.right)
            op = node.op
            if isinstance(op, _ast.Add):      return l + r
            if isinstance(op, _ast.Sub):      return l - r
            if isinstance(op, _ast.Mult):     return l * r
            if isinstance(op, _ast.Div):      return l / r if r else 0.0
            if isinstance(op, _ast.FloorDiv): return float(int(l // r)) if r else 0.0
            if isinstance(op, _ast.Mod):      return l % r if r else 0.0
            if isinstance(op, _ast.Pow):
                if abs(r) > 32:
                    raise ValueError('指數過大')
                return l ** r
        if isinstance(node, _ast.UnaryOp):
            v = _safe_eval(node.operand)
            if isinstance(node.op, _ast.USub): return -v
            if isinstance(node.op, _ast.UAdd): return +v
        raise ValueError(f'不允許的運算：{type(node).__name__}')

    try:
        tree = _ast.parse(formula.strip(), mode='eval')
        for node in _ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ValueError(f'不允許的語法：{type(node).__name__}')
        result = _safe_eval(tree)
        return round(float(result), 2)
    except Exception:
        return 0.0


def _calc_service_years(hire_date_str):
    if not hire_date_str:
        return 0.0
    from datetime import date as _d4
    try:
        hire = _d4.fromisoformat(str(hire_date_str))
        return round((_d4.today() - hire).days / 365.25, 2)
    except Exception:
        return 0.0


def _calc_punch_hours(conn, staff_id, month):
    """
    從打卡記錄計算實際工時（時薪制用）
    邏輯：每天找最早 in + 最晚 out，扣除休息時間；支援跨日班次
    回傳 (total_hours, work_days, details)
    """
    from datetime import datetime as _dth, timezone as _tzh, timedelta as _tdh, date as _dateh
    import calendar as _calh
    TW = _tzh(_tdh(hours=8))

    _yh, _mh = int(month[:4]), int(month[5:])
    _last_h = _calh.monthrange(_yh, _mh)[1]
    _next_date_h = (_dateh(_yh, _mh, _last_h) + _tdh(days=1)).isoformat()

    rows = conn.execute("""
        SELECT punch_type, punched_at
        FROM punch_records
        WHERE staff_id=%s
          AND (
            to_char(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            OR (punched_at AT TIME ZONE 'Asia/Taipei')::date = %s
          )
        ORDER BY punched_at ASC
    """, (staff_id, month, _next_date_h)).fetchall()

    # Group by work session: non-'in' records within 24h of the last 'in'
    # are assigned to that 'in' record's date (handles cross-day shifts)
    day_map = {}
    last_in_ds = None
    last_in_dt = None
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tzh.utc)
        pa_tw = pa.astimezone(TW)
        ptype = r['punch_type']
        ds = pa_tw.strftime('%Y-%m-%d')

        if ptype == 'in':
            last_in_ds = ds
            last_in_dt = pa_tw
            target_ds = ds
        elif last_in_ds and last_in_dt and (pa_tw - last_in_dt).total_seconds() <= 86400:
            target_ds = last_in_ds   # 屬於前一天跨日班次
        else:
            target_ds = ds           # 孤立打卡，以自身日期為準

        if target_ds not in day_map:
            day_map[target_ds] = []
        day_map[target_ds].append({'type': ptype, 'dt': pa_tw})

    # 只保留本月的日期（跨日班次的 in 已在本月，out 被歸回同 key）
    day_map = {ds: v for ds, v in day_map.items() if ds.startswith(month)}

    total_hours = 0.0
    details = []
    for ds, punches in sorted(day_map.items()):
        ins   = [p['dt'] for p in punches if p['type'] == 'in']
        outs  = [p['dt'] for p in punches if p['type'] == 'out']
        b_out = [p['dt'] for p in punches if p['type'] == 'break_out']
        b_in  = [p['dt'] for p in punches if p['type'] == 'break_in']

        if not ins or not outs:
            continue

        work_start = min(ins)
        work_end   = max(outs)
        gross_mins = (work_end - work_start).total_seconds() / 60

        # 扣除休息時間
        break_mins = 0.0
        for bo in b_out:
            matched = [bi for bi in b_in if bi > bo]
            if matched:
                break_mins += (min(matched) - bo).total_seconds() / 60

        net_mins = max(0.0, gross_mins - break_mins)
        net_hrs  = round(net_mins / 60, 2)
        total_hours += net_hrs
        details.append({
            'date':       ds,
            'clock_in':   work_start.strftime('%H:%M'),
            'clock_out':  work_end.strftime('%H:%M'),
            'break_mins': round(break_mins),
            'net_hours':  net_hrs,
        })

    return round(total_hours, 2), len(day_map), details


def _auto_generate_salary(conn, staff, month, work_days=None):
    """
    自動產生員工月薪資料
    ─ 月薪制：底薪 + 薪資項目公式 + 加班費 - 請假扣款
    ─ 時薪制：打卡實際工時 × 時薪 + 加班費 - 請假扣款
    """
    import calendar as _cal2
    from datetime import date as _d5, timedelta as _td5, datetime as _dts5, timezone as _tz5
    _TW5 = _tz5(_td5(hours=8))
    _today5 = _dts5.now(_TW5).date()
    y, m = int(month[:4]), int(month[5:])
    total_work_days = work_days
    scheduled_dates = set()

    if total_work_days is None:
        # 1. 優先從排班取工作日
        shift_date_rows = conn.execute("""
            SELECT DISTINCT shift_date FROM shift_assignments
            WHERE staff_id=%s AND TO_CHAR(shift_date,'YYYY-MM')=%s
            ORDER BY shift_date
        """, (staff['id'], month)).fetchall()
        if shift_date_rows:
            scheduled_dates = {
                r['shift_date'].isoformat() if hasattr(r['shift_date'], 'isoformat') else str(r['shift_date'])
                for r in shift_date_rows
            }
            total_work_days = len(scheduled_dates)
        else:
            # 2. 備援：日曆扣除週日 + 國定假日
            holiday_rows = conn.execute("""
                SELECT date FROM public_holidays
                WHERE TO_CHAR(date,'YYYY-MM')=%s
            """, (month,)).fetchall()
            holiday_dates = {
                r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date'])
                for r in holiday_rows
            }
            days_in_month = _cal2.monthrange(y, m)[1]
            for _d in range(1, days_in_month + 1):
                _dt = _d5(y, m, _d)
                _ds = _dt.isoformat()
                if _dt.weekday() < 5 and _ds not in holiday_dates:
                    scheduled_dates.add(_ds)
            total_work_days = len(scheduled_dates)

    salary_type    = staff.get('salary_type', 'monthly') or 'monthly'
    base_salary    = float(staff.get('base_salary')    or 0)
    hourly_rate    = float(staff.get('hourly_rate')    or 0)
    insured_salary = float(staff.get('insured_salary') or base_salary)
    daily_hours    = float(staff.get('daily_hours')    or 8)
    service_years  = _calc_service_years(staff.get('hire_date'))

    # ── 時薪制：從打卡記錄計算工時 ──────────────────────────────────────
    actual_work_hours = 0.0
    punch_details     = []
    if salary_type == 'hourly':
        actual_work_hours, punch_work_days, punch_details = _calc_punch_hours(
            conn, staff['id'], month
        )
        # 時薪制的 base_salary 等於 實際工時 × 時薪
        hourly_base_pay = round(actual_work_hours * hourly_rate, 2)
    else:
        # 月薪制：daily_wage 用於請假扣款
        hourly_base_pay = 0.0

    # ── 已核准加班費 ─────────────────────────────────────────────────────
    ot_rows = conn.execute("""
        SELECT COALESCE(SUM(ot_pay), 0) as total
        FROM overtime_requests
        WHERE staff_id=%s AND status='approved'
          AND to_char(ot_date,'YYYY-MM')=%s
    """, (staff['id'], month)).fetchone()
    ot_pay = float(ot_rows['total']) if ot_rows else 0.0

    # ── 請假資訊（含跨月假單）──────────────────────────────────────────
    month_start = f"{y}-{m:02d}-01"
    month_end   = f"{y}-{m:02d}-{_cal2.monthrange(y, m)[1]:02d}"
    _leave_raw = conn.execute("""
        SELECT lr.start_date, lr.end_date,
               lr.start_half, lr.end_half, lr.lv_start_time, lr.lv_end_time,
               lt.pay_rate, lt.code, lt.name as leave_name
        FROM leave_requests lr
        JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.staff_id=%s AND lr.status='approved'
          AND lr.start_date <= %s AND lr.end_date >= %s
    """, (staff['id'], month_end, month_start)).fetchall()
    leave_days      = 0.0
    unpaid_days     = 0.0
    leave_rows      = []   # 保留供後續扣款名稱使用
    partial_pay_map = {}   # key=pay_rate, value={'days':..., 'names':set()}
    for _lr in _leave_raw:
        _sd    = str(_lr['start_date'])
        _ed    = str(_lr['end_date'])
        _eff_s = max(_sd, month_start)
        _eff_e = min(_ed, month_end)
        _s_half = bool(_lr['start_half']) and (_sd == _eff_s)
        _e_half = bool(_lr['end_half'])   and (_ed == _eff_e)
        _s_time = (_lr['lv_start_time'] if _sd == _eff_s else None)
        _e_time = (_lr['lv_end_time']   if _ed == _eff_e else None)
        _days = _calc_leave_days(_eff_s, _eff_e,
                                 start_half=_s_half, end_half=_e_half,
                                 start_time=_s_time, end_time=_e_time,
                                 scheduled_dates=scheduled_dates if scheduled_dates else None)
        leave_days += _days
        _pay = float(_lr['pay_rate'])
        if _pay == 0:
            unpaid_days += _days
        elif 0 < _pay < 1:
            if _pay not in partial_pay_map:
                partial_pay_map[_pay] = {'days': 0.0, 'names': set()}
            partial_pay_map[_pay]['days'] += _days
            partial_pay_map[_pay]['names'].add(_lr['leave_name'])
        leave_rows.append({'total_days': _days, 'pay_rate': _lr['pay_rate'],
                           'leave_name': _lr['leave_name'], 'code': _lr['code']})
    actual_days = total_work_days - leave_days

    # 建立請假日期集合（供缺勤核查與時薪制加班估算排除請假日使用）
    leave_date_set = set()
    for _lr in _leave_raw:
        _ld_str = str(_lr['start_date'])
        _le_str = str(_lr['end_date'])
        _ld_eff = _d5.fromisoformat(max(_ld_str, month_start))
        _le_eff = _d5.fromisoformat(min(_le_str, month_end))
        _cur = _ld_eff
        while _cur <= _le_eff:
            leave_date_set.add(_cur.isoformat())
            _cur += _td5(days=1)

    # ── 日薪 / 時薪（用於請假扣款） ──────────────────────────────────
    if salary_type == 'hourly':
        daily_wage  = hourly_rate * daily_hours   # 時薪制日薪 = 時薪 × 每日工時
        hourly_wage = hourly_rate
    else:
        daily_wage  = base_salary / 30 if base_salary > 0 else 0
        hourly_wage = daily_wage / daily_hours if daily_hours > 0 else 0

    # ── 組裝薪資項目 ─────────────────────────────────────────────────
    items           = []
    allowance_total = 0.0
    deduction_total = 0.0
    # 員工個人金額覆寫 {str(item_id): amount}
    _overrides = staff.get('salary_item_overrides') or {}
    if isinstance(_overrides, str):
        try:
            _overrides = _json.loads(_overrides)
        except Exception:
            _overrides = {}

    def _apply_override(item_id, calculated_amt):
        """若員工設有個人金額，使用個人金額；否則使用計算值"""
        key = str(item_id)
        if key in _overrides and _overrides[key] is not None and _overrides[key] != '':
            return float(_overrides[key]), True   # (amount, is_overridden)
        return calculated_amt, False

    if salary_type == 'hourly':
        # 時薪制：第一筆項目是「本薪（工時計算）」
        items.append({
            'id': 'hourly_base', 'name': '本薪（工時）', 'type': 'allowance',
            'amount': hourly_base_pay, 'formula': '',
            'calc_note': (
                f'{actual_work_hours}h × 時薪${hourly_rate}'
                + (f'（{len(punch_details)}天出勤）' if punch_details else '')
            ),
        })
        allowance_total += hourly_base_pay

        # 時薪制加班費（從打卡計算，若無申請記錄則估算）
        if ot_pay == 0 and actual_work_hours > 0:
            for pd in punch_details:
                if pd.get('date') in leave_date_set:
                    continue
                overtime_h = max(0.0, pd['net_hours'] - daily_hours)
                if overtime_h > 0:
                    h1 = min(overtime_h, 2.0)
                    h2 = max(0.0, overtime_h - 2.0)
                    rate1 = float(staff.get('ot_rate1') or 1.33)
                    rate2 = float(staff.get('ot_rate2') or 1.67)
                    ot_pay += round(hourly_rate * (h1 * rate1 + h2 * rate2), 2)

        # 時薪制的保險費以 insured_salary 為準（若未設定則用月薪換算）
        if insured_salary == 0:
            insured_salary = round(hourly_rate * daily_hours * 30, 0)

        # 時薪制只加入保險類扣除項
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            salary_items_rows = conn.execute(f"""
                SELECT * FROM salary_items
                WHERE active=TRUE AND id IN ({placeholders})
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """, staff_item_ids).fetchall()
        else:
            salary_items_rows = conn.execute("""
                SELECT * FROM salary_items
                WHERE active=TRUE
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """).fetchall()
        for it in salary_items_rows:
            calc_amt = _eval_formula(it['formula'] or '', hourly_base_pay,
                                     insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else (it['formula'] or '')
            items.append({
                'id': it['id'], 'name': it['name'], 'type': 'deduction',
                'amount': round(amt, 2), 'formula': it['formula'] or '',
                'calc_note': note,
            })
            deduction_total += amt

    else:
        # 月薪制：跑啟用的薪資項目（若員工有指定則只跑指定項目）
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            items_rows = conn.execute(
                f"SELECT * FROM salary_items WHERE active=TRUE AND id IN ({placeholders}) ORDER BY sort_order, id",
                staff_item_ids
            ).fetchall()
        else:
            items_rows = conn.execute(
                "SELECT * FROM salary_items WHERE active=TRUE ORDER BY sort_order, id"
            ).fetchall()
        for it in items_rows:
            formula  = it['formula'] or ''
            calc_amt = float(it['amount'] or 0)
            if formula:
                calc_amt = _eval_formula(formula, base_salary, insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else formula
            items.append({
                'id':        it['id'],
                'name':      it['name'],
                'type':      it['item_type'],
                'amount':    round(amt, 2),
                'formula':   formula,
                'calc_note': note,
            })
            if it['item_type'] == 'allowance':
                allowance_total += amt
            else:
                deduction_total += amt

    # ── 加班費（申請核准） ──────────────────────────────────────────────
    if ot_pay > 0:
        items.append({
            'id': 'ot', 'name': '加班費（申請）', 'type': 'allowance',
            'amount': round(ot_pay, 2), 'formula': '',
            'calc_note': '核准加班費合計',
        })
        allowance_total += ot_pay

    # ── 請假扣款 ─────────────────────────────────────────────────────
    if unpaid_days > 0 and daily_wage > 0:
        leave_names = '、'.join(set(
            r['leave_name'] for r in leave_rows if float(r['pay_rate']) == 0
        ))
        deduct = round(daily_wage * unpaid_days, 2)
        items.append({
            'id': 'unpaid', 'name': f'無薪假扣款（{leave_names}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{unpaid_days}天 × 日薪${round(daily_wage, 0)}',
        })
        deduction_total += deduct

    for _pp_rate, _pp in sorted(partial_pay_map.items()):
        if _pp['days'] > 0 and daily_wage > 0:
            _deduct_ratio = round(1.0 - _pp_rate, 4)
            _pp_deduct = round(daily_wage * _pp['days'] * _deduct_ratio, 2)
            _pp_names  = '、'.join(_pp['names'])
            items.append({
                'id': f'halfpay_{int(_pp_rate*100)}', 'name': f'部分薪假扣款（{_pp_names}）',
                'type': 'deduction', 'amount': _pp_deduct, 'formula': '',
                'calc_note': f'{_pp["days"]}天 × 日薪${round(daily_wage,0)} × {_deduct_ratio}（pay_rate={_pp_rate}）',
            })
            deduction_total += _pp_deduct

    # ── 月薪制：缺勤扣款（打卡記錄核查） ──────────────────────────────
    absent_days = 0
    if salary_type == 'monthly' and scheduled_dates and daily_wage > 0:
        punch_rows = conn.execute("""
            SELECT DISTINCT (punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date
            FROM punch_records WHERE staff_id=%s
              AND TO_CHAR(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
        """, (staff['id'], month)).fetchall()
        punched_dates = {
            r['work_date'].isoformat() if hasattr(r['work_date'], 'isoformat') else str(r['work_date'])
            for r in punch_rows
        }
        absent_date_list = sorted(
            ds for ds in scheduled_dates
            if ds not in punched_dates and ds not in leave_date_set
               and _d5.fromisoformat(ds) < _today5
        )
        absent_days = len(absent_date_list)
        if absent_days > 0:
            deduct = round(daily_wage * absent_days, 2)
            sample = '、'.join(absent_date_list[:3]) + ('等' if absent_days > 3 else '')
            items.append({
                'id': 'absent', 'name': f'缺勤扣款（{absent_days} 天）', 'type': 'deduction',
                'amount': deduct, 'formula': '',
                'calc_note': f'{absent_days} 天 × 日薪 ${round(daily_wage, 0)}（{sample}）',
            })
            deduction_total += deduct

    # ── 薪資預支扣帳 ────────────────────────────────────────────────
    advance_rows = conn.execute("""
        SELECT id, amount, advance_date, note
        FROM salary_advances
        WHERE staff_id=%s AND status='pending' AND deduct_month=%s
        ORDER BY advance_date
    """, (staff['id'], month)).fetchall()
    for adv in advance_rows:
        adv_amt  = float(adv['amount'])
        adv_date = (adv['advance_date'].isoformat()
                    if hasattr(adv['advance_date'], 'isoformat')
                    else str(adv['advance_date']))
        adv_note = adv['note'] or ''
        items.append({
            'id': f'advance_{adv["id"]}', 'name': '薪資預支扣帳',
            'type': 'deduction', 'amount': adv_amt, 'formula': '',
            'calc_note': f'{adv_date} 預支 ${adv_amt:,.0f}' + (f'（{adv_note}）' if adv_note else ''),
        })
        deduction_total += adv_amt

    net_pay = round(allowance_total - deduction_total, 2)

    return {
        'staff_id':          staff['id'],
        'month':             month,
        'salary_type':       salary_type,
        'base_salary':       base_salary if salary_type == 'monthly' else 0,
        'hourly_rate':       hourly_rate if salary_type == 'hourly' else 0,
        'hourly_base_pay':   hourly_base_pay if salary_type == 'hourly' else 0,
        'actual_work_hours': actual_work_hours if salary_type == 'hourly' else 0,
        'insured_salary':    insured_salary,
        'work_days':         total_work_days,
        'actual_days':       max(0, actual_days - absent_days),
        'leave_days':        leave_days,
        'unpaid_days':       unpaid_days,
        'absent_days':       absent_days,
        'ot_pay':            ot_pay,
        'allowance_total':   round(allowance_total, 2),
        'deduction_total':   round(deduction_total, 2),
        'net_pay':           net_pay,
        'items':             items,
        'punch_details':     punch_details,   # 時薪制：每日打卡明細
        'status':            'draft',
    }


# ─── Advisory lock key ────────────────────────────────────────────────────────

_SALARY_GENERATE_LOCK_KEY = 0x53414C41  # 'SALA'


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── Employee: view own payslip ─────────────────────────────────────────────

@bp.route('/api/salary/my-payslip', methods=['GET'])
def api_my_payslip():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _dp
        month = _dp.today().strftime('%Y-%m')
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.salary_type, ps.hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.staff_id = %s AND sr.month = %s
        """, (sid, month)).fetchone()
    if not row:
        return jsonify({'error': f'{month} 尚無薪資記錄，請聯絡管理員'}), 404
    d = salary_record_row(row)
    d['staff_name']    = row['staff_name']
    d['staff_role']    = row['staff_role']
    d['employee_code'] = row['employee_code'] or ''
    d['department']    = row['department']    or ''
    d['salary_type']   = row['salary_type']   or 'monthly'
    d['hourly_rate']   = float(row['hourly_rate'] or 0)
    return jsonify(d)


# ── Salary Items CRUD ──────────────────────────────────────────────────────

@bp.route('/api/salary/items', methods=['GET'])
@require_module('salary')
def api_salary_items_list():
    now = time.time()
    if _salary_items_cache['data'] is not None and now - _salary_items_cache['at'] < _SEMISTATIC_TTL:
        return jsonify(_salary_items_cache['data'])
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM salary_items ORDER BY sort_order, id").fetchall()
    result = [salary_item_row(r) for r in rows]
    _salary_items_cache['data'] = result
    _salary_items_cache['at'] = now
    return jsonify(result)


@bp.route('/api/salary/items', methods=['POST'])
@require_module('salary')
def api_salary_item_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO salary_items (name, item_type, formula, amount, description, color, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b.get('item_type', 'allowance'), b.get('formula', ''),
              float(b.get('amount', 0)), b.get('description', ''),
              b.get('color', '#4a7bda'), int(b.get('sort_order', 0)))).fetchone()
    _salary_items_cache['data'] = None
    return jsonify(salary_item_row(row)), 201


@bp.route('/api/salary/items/<int:iid>', methods=['PUT'])
@require_module('salary')
def api_salary_item_update(iid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_items SET name=%s, item_type=%s, formula=%s, amount=%s,
              description=%s, color=%s, sort_order=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b.get('item_type', 'allowance'), b.get('formula', ''),
              float(b.get('amount', 0)), b.get('description', ''),
              b.get('color', '#4a7bda'), int(b.get('sort_order', 0)),
              bool(b.get('active', True)), iid)).fetchone()
    _salary_items_cache['data'] = None
    return jsonify(salary_item_row(row)) if row else ('', 404)


@bp.route('/api/salary/items/<int:iid>', methods=['DELETE'])
@require_module('salary')
def api_salary_item_delete(iid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_items WHERE id=%s", (iid,))
    _salary_items_cache['data'] = None
    return jsonify({'deleted': iid})


# ── Salary Records ────────────────────────────────────────────────────────

@bp.route('/api/salary/records', methods=['GET'])
@require_module('salary')
def api_salary_records_list():
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _d6
        month = _d6.today().strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.month=%s
            ORDER BY ps.name
        """, (month,)).fetchall()
    result = []
    for r in rows:
        d = salary_record_row(r)
        d['staff_name']    = r['staff_name']
        d['staff_role']    = r['staff_role']
        d['employee_code'] = r['employee_code'] or ''
        d['department']    = r['department'] or ''
        result.append(d)
    return jsonify(result)


@bp.route('/api/salary/records/generate', methods=['POST'])
@require_module('salary')
def api_salary_generate():
    """自動產生或更新該月薪資"""
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    if not month:
        return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (_SALARY_GENERATE_LOCK_KEY,)
        ).fetchone()['ok']
        if not acquired:
            return jsonify({'error': '薪資批次正在產生中，請稍後再試'}), 409
        try:
            staff_list = conn.execute(
                "SELECT * FROM punch_staff WHERE active=TRUE"
            ).fetchall()
            generated = 0
            for staff in staff_list:
                data = _auto_generate_salary(conn, dict(staff), month)
                items_json         = _json.dumps(data['items'], ensure_ascii=False)
                punch_details_json = _json.dumps(data.get('punch_details', []), ensure_ascii=False)
                stored_base = data['hourly_base_pay'] if data['salary_type'] == 'hourly' else data['base_salary']
                conn.execute("""
                    INSERT INTO salary_records
                      (staff_id, month, base_salary, insured_salary, work_days, actual_days,
                       leave_days, unpaid_days, ot_pay, allowance_total, deduction_total,
                       net_pay, items, actual_work_hours, punch_details, status, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,'draft',NOW())
                    ON CONFLICT (staff_id, month) DO UPDATE
                      SET base_salary=%s, insured_salary=%s, work_days=%s, actual_days=%s,
                          leave_days=%s, unpaid_days=%s, ot_pay=%s, allowance_total=%s,
                          deduction_total=%s, net_pay=%s, items=%s::jsonb,
                          actual_work_hours=%s, punch_details=%s::jsonb,
                          status=CASE WHEN salary_records.status='confirmed' THEN 'confirmed' ELSE 'draft' END,
                          updated_at=NOW()
                """, (
                    data['staff_id'], month, stored_base, data['insured_salary'],
                    data['work_days'], data['actual_days'], data['leave_days'], data['unpaid_days'],
                    data['ot_pay'], data['allowance_total'], data['deduction_total'],
                    data['net_pay'], items_json, data['actual_work_hours'], punch_details_json,
                    stored_base, data['insured_salary'], data['work_days'], data['actual_days'],
                    data['leave_days'], data['unpaid_days'], data['ot_pay'], data['allowance_total'],
                    data['deduction_total'], data['net_pay'], items_json,
                    data['actual_work_hours'], punch_details_json,
                ))
                generated += 1
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SALARY_GENERATE_LOCK_KEY,))
    return jsonify({'ok': True, 'generated': generated, 'month': month})


@bp.route('/api/salary/records/<int:rid>', methods=['GET'])
@require_module('salary')
def api_salary_record_get(rid):
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.hire_date
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.id=%s
        """, (rid,)).fetchone()
    if not row:
        return ('', 404)
    d = salary_record_row(row)
    d['staff_name']    = row['staff_name']
    d['staff_role']    = row['staff_role']
    d['employee_code'] = row['employee_code'] or ''
    d['department']    = row['department'] or ''
    d['hire_date']     = row['hire_date'].isoformat() if row['hire_date'] else ''
    return jsonify(d)


@bp.route('/api/salary/records/<int:rid>', methods=['PUT'])
@require_module('salary')
def api_salary_record_update(rid):
    b          = request.get_json(force=True)
    items_json = _json.dumps(b.get('items', []), ensure_ascii=False)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET
              allowance_total=%s, deduction_total=%s, net_pay=%s,
              items=%s::jsonb, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('allowance_total', 0)), float(b.get('deduction_total', 0)),
              float(b.get('net_pay', 0)), items_json,
              b.get('note', ''), rid)).fetchone()
    return jsonify(salary_record_row(row)) if row else ('', 404)


@bp.route('/api/salary/records/confirm-all', methods=['POST'])
@require_module('salary')
def api_salary_confirm_all():
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    by    = b.get('confirmed_by', '管理員')
    if not month:
        return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        rows = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE month=%s AND status='draft'
            RETURNING id, staff_id, month, net_pay
        """, (by, month)).fetchall()
        if rows:
            staff_ids    = [r['staff_id'] for r in rows]
            placeholders = ','.join(['%s'] * len(staff_ids))
            conn.execute(
                f"UPDATE salary_advances SET status='deducted' WHERE staff_id IN ({placeholders}) AND deduct_month=%s AND status='pending'",
                (*staff_ids, month)
            )
    confirmed = len(rows)
    for row in rows:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify({'ok': True, 'confirmed': confirmed})


@bp.route('/api/salary/records/<int:rid>/confirm', methods=['POST'])
@require_module('salary')
def api_salary_confirm(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b.get('confirmed_by', '管理員'), rid)).fetchone()
        if row:
            conn.execute("""
                UPDATE salary_advances SET status='deducted'
                WHERE staff_id=%s AND deduct_month=%s AND status='pending'
            """, (row['staff_id'], row['month']))
    if row:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify(salary_record_row(row)) if row else ('', 404)


@bp.route('/api/salary/records/<int:rid>', methods=['DELETE'])
@require_module('salary')
def api_salary_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})


# ── Salary Advances (薪資預支扣帳) ──────────────────────────────────────────

@bp.route('/api/salary/advances', methods=['GET'])
@require_module('salary')
def api_salary_advances_list():
    staff_id = request.args.get('staff_id', type=int)
    month    = request.args.get('month', '')
    status   = request.args.get('status', '')
    wheres, params = [], []
    if staff_id: wheres.append('sa.staff_id=%s');    params.append(staff_id)
    if month:    wheres.append('sa.deduct_month=%s'); params.append(month)
    if status:   wheres.append('sa.status=%s');       params.append(status)
    where_sql = ('WHERE ' + ' AND '.join(wheres)) if wheres else ''
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT sa.*, ps.name as staff_name, ps.department
            FROM salary_advances sa
            JOIN punch_staff ps ON ps.id=sa.staff_id
            {where_sql}
            ORDER BY sa.advance_date DESC, sa.id DESC
        """, params).fetchall()
    return jsonify([_advance_row(r) for r in rows])


@bp.route('/api/salary/advances', methods=['POST'])
@require_module('salary')
def api_salary_advance_create():
    b            = request.get_json(force=True)
    staff_id     = b.get('staff_id')
    amount       = float(b.get('amount') or 0)
    advance_date = b.get('advance_date', '').strip()
    deduct_month = b.get('deduct_month', '').strip()
    note         = b.get('note', '').strip()
    if not staff_id or amount <= 0 or not advance_date:
        return jsonify({'error': '請填寫員工、金額、預支日期'}), 400
    if not deduct_month:
        from datetime import date as _da
        deduct_month = _da.today().strftime('%Y-%m')
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO salary_advances (staff_id, amount, advance_date, deduct_month, note)
            VALUES (%s,%s,%s,%s,%s) RETURNING *
        """, (staff_id, amount, advance_date, deduct_month, note)).fetchone()
    return jsonify(_advance_row(row)), 201


@bp.route('/api/salary/advances/<int:aid>', methods=['PATCH'])
@require_module('salary')
def api_salary_advance_update(aid):
    b = request.get_json(force=True)
    fields, params = [], []
    if 'status'       in b: fields.append('status=%s');       params.append(b['status'])
    if 'note'         in b: fields.append('note=%s');         params.append(b['note'])
    if 'deduct_month' in b: fields.append('deduct_month=%s'); params.append(b['deduct_month'])
    if 'amount'       in b: fields.append('amount=%s');       params.append(float(b['amount']))
    if not fields:
        return jsonify({'error': '無更新欄位'}), 400
    params.append(aid)
    with get_db() as conn:
        row = conn.execute(
            f"UPDATE salary_advances SET {','.join(fields)} WHERE id=%s RETURNING *", params
        ).fetchone()
    return jsonify(_advance_row(row)) if row else ('', 404)


@bp.route('/api/salary/advances/<int:aid>', methods=['DELETE'])
@require_module('salary')
def api_salary_advance_delete(aid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_advances WHERE id=%s", (aid,))
    return jsonify({'deleted': aid})


# ── Salary Staff Settings ────────────────────────────────────────────────────

@bp.route('/api/salary/staff', methods=['GET'])
@require_module('salary')
def api_salary_staff_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, name, username, role, active, employee_code, department,
                   position_title, hire_date, birth_date, base_salary, insured_salary,
                   daily_hours, ot_rate1, ot_rate2, salary_type, hourly_rate,
                   vacation_quota, salary_notes, salary_item_ids, salary_item_overrides,
                   national_id, gender, insurance_type, address
            FROM punch_staff ORDER BY name
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in ['base_salary', 'insured_salary', 'daily_hours', 'ot_rate1', 'ot_rate2', 'hourly_rate']:
            if d.get(f) is not None:
                d[f] = float(d[f])
        if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
        if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
        d['annual_leave_days'] = _calc_annual_leave_days(d.get('hire_date'))
        d['service_years']     = _calc_service_years(d.get('hire_date'))
        result.append(d)
    return jsonify(result)


@bp.route('/api/salary/staff/<int:sid>', methods=['PUT'])
@require_module('salary')
def api_salary_staff_update(sid):
    b = request.get_json(force=True)
    def _f(k, default=0): return float(b.get(k, default) or default)
    def _s(k): return b.get(k, '').strip() if b.get(k) else None
    with get_db() as conn:
        salary_item_ids      = b.get('salary_item_ids')
        salary_item_ids_json = _json.dumps(salary_item_ids) if salary_item_ids is not None else None
        overrides            = b.get('salary_item_overrides')
        overrides_json       = _json.dumps(overrides) if overrides else None
        conn.execute("""
            UPDATE punch_staff SET
              employee_code=%s, department=%s, position_title=%s,
              hire_date=%s, birth_date=%s,
              base_salary=%s, insured_salary=%s, daily_hours=%s,
              ot_rate1=%s, ot_rate2=%s, salary_type=%s,
              hourly_rate=%s, vacation_quota=%s, salary_notes=%s,
              salary_item_ids=%s, salary_item_overrides=%s,
              national_id=%s, gender=%s, insurance_type=%s, address=%s
            WHERE id=%s
        """, (_s('employee_code'), _s('department'), _s('position_title'),
              _s('hire_date'), _s('birth_date'),
              _f('base_salary'), _f('insured_salary'), _f('daily_hours') or 8,
              _f('ot_rate1') or 1.33, _f('ot_rate2') or 1.67,
              b.get('salary_type', 'monthly'),
              _f('hourly_rate'), b.get('vacation_quota') or None,
              b.get('salary_notes', ''), salary_item_ids_json, overrides_json,
              (b.get('national_id') or '').strip(),
              (b.get('gender') or '').strip(),
              (b.get('insurance_type') or 'regular').strip(),
              (b.get('address') or '').strip(),
              sid))
        row = conn.execute("SELECT * FROM punch_staff WHERE id=%s", (sid,)).fetchone()
    return jsonify(_punch_staff_row(row)) if row else ('', 404)


# ── Salary Preview ───────────────────────────────────────────────────────────

@bp.route('/api/salary/records/preview', methods=['POST'])
@require_module('salary')
def api_salary_preview():
    """預覽薪資計算結果（不儲存）"""
    b     = request.get_json(force=True) or {}
    month = b.get('month', '').strip()
    if not month:
        return jsonify({'error': '請指定月份'}), 400
    result = []
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        for staff in staff_list:
            data = _auto_generate_salary(conn, dict(staff), month)
            punch_days = conn.execute("""
                SELECT COUNT(DISTINCT punched_at::date) AS n
                FROM punch_records WHERE staff_id=%s
                  AND to_char(punched_at,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()['n']
            approved_ot = conn.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(ot_hours),0) AS hrs
                FROM overtime_requests WHERE staff_id=%s
                  AND status='approved'
                  AND to_char(request_date,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()
            result.append({
                'staff_id':        data['staff_id'],
                'staff_name':      staff['name'],
                'department':      staff['department'],
                'salary_type':     staff['salary_type'],
                'punch_days':      punch_days,
                'work_days':       float(data['work_days']),
                'actual_days':     float(data['actual_days']),
                'leave_days':      float(data['leave_days']),
                'unpaid_days':     float(data['unpaid_days']),
                'ot_count':        int(approved_ot['n']),
                'ot_hours':        float(approved_ot['hrs']),
                'ot_pay':          float(data['ot_pay']),
                'base_salary':     float(data['base_salary']),
                'allowance_total': float(data['allowance_total']),
                'deduction_total': float(data['deduction_total']),
                'net_pay':         float(data['net_pay']),
            })
    return jsonify({'ok': True, 'month': month, 'records': result})
