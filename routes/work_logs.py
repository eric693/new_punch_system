import json as _json

from flask import Blueprint, request, jsonify, session

from auth import login_required
from db import get_db

bp = Blueprint('work_logs', __name__)

_DEFAULT_EXPENSE_CATS = ['廠商','雇員薪資','雇員獎金','固定營業費用','全民健康保險','勞工保險金','勞工退休金','稅務相關','政府相關','房租費用','建築師事務所','會計師事務所','律師事務所','客戶退款','雜支']
_DEFAULT_INCOME_CATS  = ['客變','設計','裝修工程','追加','退傭','介紹費','其它']


def init():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_logs (
                id         SERIAL PRIMARY KEY,
                staff_id   INT NOT NULL REFERENCES punch_staff(id) ON DELETE CASCADE,
                log_date   DATE NOT NULL,
                content    TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(staff_id, log_date)
            )
        """)

    sqls = [
        """CREATE TABLE IF NOT EXISTS dept_expense_categories (
            id           SERIAL PRIMARY KEY,
            department   TEXT NOT NULL DEFAULT '',
            expense_cats JSONB NOT NULL DEFAULT '[]',
            income_cats  JSONB NOT NULL DEFAULT '[]',
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS dept_expense_cats_dept_uniq ON dept_expense_categories(department)",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[dept_expense_cats_init] {e}")
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) AS c FROM dept_expense_categories").fetchone()['c']
            if cnt == 0:
                conn.execute(
                    "INSERT INTO dept_expense_categories (department, expense_cats, income_cats) VALUES (%s,%s,%s)",
                    ('', _json.dumps(_DEFAULT_EXPENSE_CATS), _json.dumps(_DEFAULT_INCOME_CATS))
                )
    except Exception as e:
        print(f"[dept_expense_cats_seed] {e}")


def _get_dept_cats(department: str):
    """Return (expense_cats, income_cats) for a given department, falling back to default."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT expense_cats, income_cats FROM dept_expense_categories WHERE department=%s",
            (department,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT expense_cats, income_cats FROM dept_expense_categories WHERE department=''",
            ).fetchone()
    if not row:
        return _DEFAULT_EXPENSE_CATS, _DEFAULT_INCOME_CATS
    ec = row['expense_cats'] if isinstance(row['expense_cats'], list) else _json.loads(row['expense_cats'] or '[]')
    ic = row['income_cats']  if isinstance(row['income_cats'],  list) else _json.loads(row['income_cats']  or '[]')
    return ec, ic


@bp.route('/api/work-logs', methods=['GET'])
def api_work_logs_my():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    month = request.args.get('month', '')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT log_date::text, content, updated_at::text
            FROM work_logs
            WHERE staff_id=%s AND to_char(log_date,'YYYY-MM')=%s
            ORDER BY log_date
        """, (sid, month)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/work-logs', methods=['POST'])
def api_work_logs_save():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    b = request.get_json(force=True)
    log_date = b.get('log_date', '').strip()
    content  = b.get('content', '').strip()
    if not log_date:
        return jsonify({'error': '日期必填'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO work_logs (staff_id, log_date, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (staff_id, log_date)
            DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()
            RETURNING log_date::text, content, updated_at::text
        """, (sid, log_date, content)).fetchone()
    return jsonify(dict(row)), 200


@bp.route('/api/admin/work-logs', methods=['GET'])
@login_required
def api_admin_work_logs():
    month    = request.args.get('month', '')
    staff_id = request.args.get('staff_id', '')
    if not month:
        return jsonify([])
    conds  = ["to_char(w.log_date,'YYYY-MM')=%s"]
    params = [month]
    if staff_id:
        conds.append("w.staff_id=%s")
        params.append(int(staff_id))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT w.id, w.staff_id, s.name AS staff_name, s.employee_code,
                   w.log_date::text, w.content, w.updated_at::text
            FROM work_logs w
            JOIN punch_staff s ON s.id=w.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY w.log_date, s.name
        """, params).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/expense/categories', methods=['GET'])
def api_expense_categories():
    """Staff-facing: return expense/income category lists based on the current staff's department."""
    sid = session.get('punch_staff_id')
    department = ''
    if sid:
        with get_db() as conn:
            staff = conn.execute("SELECT department FROM punch_staff WHERE id=%s", (sid,)).fetchone()
        if staff:
            department = staff['department'] or ''
    ec, ic = _get_dept_cats(department)
    return jsonify({'支出': ec, '收入': ic, 'department': department})


@bp.route('/api/admin/dept-expense-cats', methods=['GET'])
@login_required
def api_admin_get_dept_expense_cats():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT department, expense_cats, income_cats, updated_at FROM dept_expense_categories ORDER BY department"
        ).fetchall()
        depts = conn.execute(
            "SELECT DISTINCT department FROM punch_staff WHERE department!='' ORDER BY department"
        ).fetchall()
    configs = []
    for r in rows:
        ec = r['expense_cats'] if isinstance(r['expense_cats'], list) else _json.loads(r['expense_cats'] or '[]')
        ic = r['income_cats']  if isinstance(r['income_cats'],  list) else _json.loads(r['income_cats']  or '[]')
        configs.append({
            'department':   r['department'],
            'expense_cats': ec,
            'income_cats':  ic,
            'updated_at':   r['updated_at'].isoformat() if r['updated_at'] else None,
        })
    return jsonify({
        'configs':     configs,
        'departments': [d['department'] for d in depts],
        'defaults':    {'expense_cats': _DEFAULT_EXPENSE_CATS, 'income_cats': _DEFAULT_INCOME_CATS},
    })


@bp.route('/api/admin/dept-expense-cats', methods=['POST'])
@login_required
def api_admin_save_dept_expense_cats():
    b = request.get_json(force=True)
    department   = (b.get('department') or '').strip()
    expense_cats = b.get('expense_cats', [])
    income_cats  = b.get('income_cats',  [])
    if not isinstance(expense_cats, list): expense_cats = []
    if not isinstance(income_cats,  list): income_cats  = []
    with get_db() as conn:
        conn.execute("""
            INSERT INTO dept_expense_categories (department, expense_cats, income_cats, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (department) DO UPDATE
            SET expense_cats=EXCLUDED.expense_cats, income_cats=EXCLUDED.income_cats, updated_at=NOW()
        """, (department, _json.dumps(expense_cats), _json.dumps(income_cats)))
    return jsonify({'ok': True})


@bp.route('/api/admin/dept-expense-cats/<path:dept>', methods=['DELETE'])
@login_required
def api_admin_delete_dept_expense_cats(dept):
    if dept == '__default__':
        dept = ''
    if dept == '':
        return jsonify({'error': '預設設定無法刪除'}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM dept_expense_categories WHERE department=%s", (dept,))
    return jsonify({'ok': True})
