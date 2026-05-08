import json as _json
import time
import traceback

from flask import Blueprint, request, jsonify, session

from auth import login_required
from config import ANTHROPIC_API_KEY
from db import get_db, _expense_list_cache, _badges_cache, _EXPENSE_LIST_TTL
from notifications import _notify_review_result

bp = Blueprint('expense', __name__)


def init():
    sqls = [
        """CREATE TABLE IF NOT EXISTS finance_categories (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'expense',
            color       TEXT DEFAULT '#4a7bda',
            sort_order  INT DEFAULT 0,
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_documents (
            id              SERIAL PRIMARY KEY,
            filename        TEXT NOT NULL,
            doc_type        TEXT DEFAULT '',
            ocr_raw         JSONB DEFAULT '{}',
            upload_date     DATE DEFAULT CURRENT_DATE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_records (
            id              SERIAL PRIMARY KEY,
            record_date     DATE NOT NULL,
            category_id     INT REFERENCES finance_categories(id) ON DELETE SET NULL,
            type            TEXT NOT NULL DEFAULT 'expense',
            title           TEXT NOT NULL,
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            tax_amount      NUMERIC(14,2) DEFAULT 0,
            vendor          TEXT DEFAULT '',
            invoice_no      TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            document_id     INT,
            created_by      TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS expense_claims (
            id                   SERIAL PRIMARY KEY,
            staff_id             INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            title                TEXT NOT NULL,
            amount               NUMERIC(12,2) NOT NULL DEFAULT 0,
            expense_date         DATE NOT NULL,
            category             TEXT DEFAULT '',
            note                 TEXT DEFAULT '',
            status               TEXT NOT NULL DEFAULT 'pending',
            document_id          INT REFERENCES finance_documents(id) ON DELETE SET NULL,
            review_note          TEXT DEFAULT '',
            reviewed_by          TEXT DEFAULT '',
            reviewed_at          TIMESTAMPTZ,
            finance_record_id    INT REFERENCES finance_records(id) ON DELETE SET NULL,
            created_at           TIMESTAMPTZ DEFAULT NOW(),
            document_id2         INT REFERENCES finance_documents(id) ON DELETE SET NULL,
            reimbursement_method TEXT NOT NULL DEFAULT '匯款',
            bank_name            TEXT NOT NULL DEFAULT '',
            bank_branch          TEXT NOT NULL DEFAULT '',
            bank_account         TEXT NOT NULL DEFAULT '',
            account_holder       TEXT NOT NULL DEFAULT '',
            expense_type         TEXT NOT NULL DEFAULT '支出',
            company              TEXT NOT NULL DEFAULT '進光設計',
            vendor               TEXT NOT NULL DEFAULT ''
        )""",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS document_id2 INT REFERENCES finance_documents(id) ON DELETE SET NULL",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS reimbursement_method TEXT NOT NULL DEFAULT '匯款'",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS bank_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS bank_branch TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS bank_account TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS account_holder TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS expense_type TEXT NOT NULL DEFAULT '支出'",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS company TEXT NOT NULL DEFAULT '進光設計'",
        "ALTER TABLE expense_claims ADD COLUMN IF NOT EXISTS vendor TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_staff_id ON expense_claims(staff_id)",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_status ON expense_claims(status)",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_created_at ON expense_claims(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_expense_date ON expense_claims(expense_date)",
        "CREATE INDEX IF NOT EXISTS idx_expense_claims_status_created ON expense_claims(status, created_at DESC)",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[expense_init] {e}")

    # vendors table
    vendor_sqls = [
        """CREATE TABLE IF NOT EXISTS vendors (
            id              SERIAL PRIMARY KEY,
            name            TEXT NOT NULL,
            bank_name       TEXT NOT NULL DEFAULT '',
            bank_branch     TEXT NOT NULL DEFAULT '',
            account_holder  TEXT NOT NULL DEFAULT '',
            bank_account    TEXT NOT NULL DEFAULT '',
            contact         TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_vendors_name ON vendors(name)",
        "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS account_label TEXT NOT NULL DEFAULT ''",
        "DROP INDEX IF EXISTS idx_vendors_name",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_vendors_name_label ON vendors(name, account_label)",
    ]
    for sql in vendor_sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[vendors_init] {e}")


def _expense_row(r):
    if not r: return None
    d = dict(r)
    if d.get('expense_date'): d['expense_date'] = str(d['expense_date'])
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    return d


# ─── Vendors ──────────────────────────────────────────────────────────────────

@bp.route('/api/vendors', methods=['GET'])
@login_required
def api_vendors_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY name ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/vendors', methods=['POST'])
@login_required
def api_vendors_create():
    b = request.get_json(force=True)
    name = (b.get('name') or '').strip()
    if not name:
        return jsonify({'error': '廠商名稱為必填'}), 400
    account_label = (b.get('account_label') or '').strip()
    with get_db() as conn:
        try:
            row = conn.execute(
                """INSERT INTO vendors (name, account_label, bank_name, bank_branch, account_holder, bank_account, contact, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (name, account_label,
                 (b.get('bank_name') or '').strip(), (b.get('bank_branch') or '').strip(),
                 (b.get('account_holder') or '').strip(), (b.get('bank_account') or '').strip(),
                 (b.get('contact') or '').strip(), (b.get('note') or '').strip())
            ).fetchone()
        except Exception as e:
            if 'unique' in str(e).lower():
                return jsonify({'error': '此廠商與帳戶標籤組合已存在'}), 409
            raise
    return jsonify(dict(row)), 201


@bp.route('/api/vendors/<int:vid>', methods=['PUT'])
@login_required
def api_vendors_update(vid):
    b = request.get_json(force=True)
    name = (b.get('name') or '').strip()
    if not name:
        return jsonify({'error': '廠商名稱為必填'}), 400
    account_label = (b.get('account_label') or '').strip()
    with get_db() as conn:
        try:
            row = conn.execute(
                """UPDATE vendors SET name=%s, account_label=%s, bank_name=%s, bank_branch=%s,
                   account_holder=%s, bank_account=%s, contact=%s, note=%s
                   WHERE id=%s RETURNING *""",
                (name, account_label,
                 (b.get('bank_name') or '').strip(), (b.get('bank_branch') or '').strip(),
                 (b.get('account_holder') or '').strip(), (b.get('bank_account') or '').strip(),
                 (b.get('contact') or '').strip(), (b.get('note') or '').strip(), vid)
            ).fetchone()
        except Exception as e:
            if 'unique' in str(e).lower():
                return jsonify({'error': '此廠商與帳戶標籤組合已存在'}), 409
            raise
    if not row:
        return jsonify({'error': '找不到廠商'}), 404
    return jsonify(dict(row))


@bp.route('/api/vendors/<int:vid>', methods=['DELETE'])
@login_required
def api_vendors_delete(vid):
    with get_db() as conn:
        conn.execute("DELETE FROM vendors WHERE id=%s", (vid,))
    return jsonify({'deleted': vid})


# ─── Employee Expense Claims ───────────────────────────────────────────────────

@bp.route('/api/expense/my-claims', methods=['GET'])
def api_expense_my_list():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM expense_claims WHERE staff_id=%s ORDER BY created_at DESC LIMIT 50
        """, (sid,)).fetchall()
    return jsonify([_expense_row(r) for r in rows])


@bp.route('/api/expense/my-claims', methods=['POST'])
def api_expense_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    b = request.get_json(force=True)
    if not b.get('title','').strip():  return jsonify({'error': '請填寫標題'}), 400
    if not b.get('expense_date'):      return jsonify({'error': '請填寫費用日期'}), 400
    try:
        amount = float(b.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'error': '金額格式錯誤'}), 400
    try:
        with get_db() as conn:
            row = conn.execute("""
                INSERT INTO expense_claims
                  (staff_id, title, amount, expense_date, category, note, document_id, expense_type, company, vendor)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (sid, b['title'].strip(), amount,
                  b['expense_date'], b.get('category','').strip(),
                  b.get('note','').strip(), b.get('document_id') or None,
                  b.get('expense_type', '支出').strip(),
                  b.get('company', '進光設計').strip(),
                  b.get('vendor', '').strip())).fetchone()
    except Exception as e:
        print(f"[expense/my-claims POST] error: {e}")
        return jsonify({'error': '送出失敗，請稍後重試'}), 500
    return jsonify(_expense_row(row)), 201


@bp.route('/api/expense/ocr', methods=['POST'])
def api_expense_ocr():
    """員工自助 OCR"""
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    import anthropic as _ant, base64, re as _re2
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': '尚未設定 ANTHROPIC_API_KEY'}), 500
    file = request.files.get('file')
    if not file: return jsonify({'error': '請上傳圖片'}), 400
    raw = file.read()
    media_type = file.content_type or 'image/jpeg'
    if media_type not in ('image/jpeg','image/png','image/gif','image/webp'):
        media_type = 'image/jpeg'
    img_b64 = base64.standard_b64encode(raw).decode()
    client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6', max_tokens=512,
            messages=[{'role':'user','content':[
                {'type':'image','source':{'type':'base64','media_type':media_type,'data':img_b64}},
                {'type':'text','text':'請辨識此收據或發票，以JSON格式回傳：{"date":"YYYY-MM-DD","vendor":"廠商","title":"建議標題","total_amount":數字,"doc_type":"receipt或invoice"}\n只回傳JSON。'}
            ]}]
        )
        text = msg.content[0].text.strip()
        text = _re2.sub(r'^```json\s*','',text,flags=_re2.MULTILINE)
        text = _re2.sub(r'\s*```$','',text,flags=_re2.MULTILINE)
        result = _json.loads(text)
    except Exception as e:
        return jsonify({'error': f'OCR 失敗：{e}'}), 500
    try:
        with get_db() as conn:
            doc = conn.execute("""
                INSERT INTO finance_documents (filename, doc_type, ocr_raw)
                VALUES (%s,%s,%s) RETURNING id
            """, (file.filename, result.get('doc_type',''), _json.dumps(result))).fetchone()
        result['document_id'] = doc['id']
    except Exception as e:
        print(f"[expense_ocr doc] {e}")
    return jsonify(result)


# ─── Admin Expense Endpoints ───────────────────────────────────────────────────

@bp.route('/api/expense/admin-upload', methods=['POST'])
@login_required
def api_expense_admin_upload():
    import base64 as _b64a, re as _re3
    file = request.files.get('file')
    if not file: return jsonify({'error': '請上傳圖片'}), 400
    media_type = file.content_type or 'image/jpeg'
    if media_type not in ('image/jpeg','image/png','image/gif','image/webp'):
        media_type = 'image/jpeg'
    result = {}
    raw = file.read()
    if ANTHROPIC_API_KEY:
        try:
            import anthropic as _ant
            img_b64 = _b64a.standard_b64encode(raw).decode()
            client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=256,
                messages=[{'role':'user','content':[
                    {'type':'image','source':{'type':'base64','media_type':media_type,'data':img_b64}},
                    {'type':'text','text':'請辨識此收據或發票，以JSON格式回傳：{"date":"YYYY-MM-DD","vendor":"廠商","title":"建議標題","total_amount":數字,"doc_type":"receipt或invoice"}\n只回傳JSON。'}
                ]}]
            )
            text = msg.content[0].text.strip()
            text = _re3.sub(r'^```json\s*','',text,flags=_re3.MULTILINE)
            text = _re3.sub(r'\s*```$','',text,flags=_re3.MULTILINE)
            result = _json.loads(text)
        except Exception:
            pass
    try:
        with get_db() as conn:
            doc = conn.execute("""
                INSERT INTO finance_documents (filename, doc_type, ocr_raw)
                VALUES (%s,%s,%s) RETURNING id
            """, (file.filename, result.get('doc_type','receipt'), _json.dumps(result))).fetchone()
        result['document_id'] = doc['id']
    except Exception as e:
        return jsonify({'error': f'儲存失敗：{e}'}), 500
    return jsonify(result)


@bp.route('/api/expense/claims/admin-create', methods=['POST'])
@login_required
def api_expense_admin_create():
    b = request.get_json(force=True)
    staff_id = b.get('staff_id')
    if not staff_id:           return jsonify({'error': '請選擇員工'}), 400
    if not b.get('expense_date'):     return jsonify({'error': '請填寫費用日期'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO expense_claims
              (staff_id, title, amount, expense_date, category, note,
               document_id, document_id2,
               reimbursement_method, bank_name, bank_branch, bank_account, account_holder,
               expense_type, company, vendor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (staff_id, (b.get('title','').strip() or b.get('category','').strip() or '費用申請'),
              float(b.get('amount', 0)),
              b['expense_date'], b.get('category','').strip(),
              b.get('note','').strip(),
              b.get('document_id') or None,
              b.get('document_id2') or None,
              b.get('reimbursement_method', '匯款').strip(),
              b.get('bank_name', '').strip(),
              b.get('bank_branch', '').strip(),
              b.get('bank_account', '').strip(),
              b.get('account_holder', '').strip(),
              b.get('expense_type', '支出').strip(),
              b.get('company', '進光設計').strip(),
              b.get('vendor', '').strip())).fetchone()
        staff = conn.execute(
            "SELECT name, employee_code FROM punch_staff WHERE id=%s", (staff_id,)
        ).fetchone()
    d = _expense_row(row)
    if staff:
        d['staff_name']    = staff['name']
        d['employee_code'] = staff['employee_code'] or ''
    _expense_list_cache.clear()
    _badges_cache.clear()
    return jsonify(d), 201


@bp.route('/api/expense/claims', methods=['GET'])
@login_required
def api_expense_admin_list():
    status = request.args.get('status', '')
    ym     = request.args.get('ym', '')
    cache_key = f"{status}:{ym}"
    now = time.time()
    cached = _expense_list_cache.get(cache_key)
    if cached and now - cached['at'] < _EXPENSE_LIST_TTL:
        return jsonify(cached['data'])
    conds, params = [], []
    if status: conds.append("ec.status=%s"); params.append(status)
    if ym:
        try:
            y, m = ym.split('-')
            y, m = int(y), int(m)
            start = f"{y:04d}-{m:02d}-01"
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            end = f"{ny:04d}-{nm:02d}-01"
            conds.append("ec.expense_date >= %s AND ec.expense_date < %s")
            params.extend([start, end])
        except Exception:
            pass
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    limit_clause = '' if (status or ym) else 'LIMIT 500'
    try:
        with get_db() as conn:
            rows = conn.execute(f"""
                SELECT ec.id, ec.staff_id, ec.expense_date, ec.expense_type, ec.category,
                       ec.vendor, ec.amount, ec.note, ec.title, ec.company,
                       ec.reimbursement_method, ec.bank_name, ec.bank_branch,
                       ec.bank_account, ec.account_holder,
                       ec.document_id, ec.document_id2, ec.status, ec.review_note,
                       ec.reviewed_at, ec.created_at,
                       ps.name as staff_name, ps.employee_code
                FROM expense_claims ec
                LEFT JOIN punch_staff ps ON ps.id=ec.staff_id
                {where}
                ORDER BY ec.created_at DESC
                {limit_clause}
            """, params).fetchall()
    except Exception as e:
        print(f"[expense/claims GET] DB error: {e}")
        traceback.print_exc()
        return jsonify({'error': f'資料庫錯誤：{e}'}), 500
    try:
        result = []
        for r in rows:
            d = _expense_row(r)
            d['staff_name']    = r['staff_name'] or ''
            d['employee_code'] = (r['employee_code'] or '') if r['employee_code'] is not None else ''
            result.append(d)
        _expense_list_cache[cache_key] = {'data': result, 'at': now}
        return jsonify(result)
    except Exception as e:
        print(f"[expense/claims] serialize error: {e}")
        traceback.print_exc()
        return jsonify({'error': f'資料處理錯誤：{e}'}), 500


@bp.route('/api/expense/claims/<int:cid>', methods=['PUT'])
@login_required
def api_expense_review(cid):
    b      = request.get_json(force=True)
    action = b.get('action')
    if action not in ('approve', 'reject', 'revert'):
        return jsonify({'error': 'invalid action'}), 400

    with get_db() as conn:
        claim = conn.execute("SELECT * FROM expense_claims WHERE id=%s", (cid,)).fetchone()
        if not claim: return ('', 404)

        if action == 'revert':
            old_fin_id = claim.get('finance_record_id')
            if old_fin_id:
                conn.execute("DELETE FROM finance_records WHERE id=%s", (old_fin_id,))
            row = conn.execute("""
                UPDATE expense_claims
                SET status='pending', reviewed_by='', review_note='',
                    reviewed_at=NULL, finance_record_id=NULL
                WHERE id=%s RETURNING *
            """, (cid,)).fetchone()
            staff = conn.execute(
                "SELECT name, employee_code FROM punch_staff WHERE id=%s", (claim['staff_id'],)
            ).fetchone() if row else None
            _badges_cache.clear()
            _expense_list_cache.clear()
            if not row: return ('', 404)
            d = _expense_row(row)
            if staff:
                d['staff_name']    = staff['name']
                d['employee_code'] = staff['employee_code'] or ''
            return jsonify(d)

    reviewed_by  = session.get('admin_display_name','管理員')
    review_note  = b.get('review_note','').strip()
    new_status   = 'approved' if action == 'approve' else 'rejected'
    finance_rid  = None

    with get_db() as conn:
        claim = conn.execute("SELECT * FROM expense_claims WHERE id=%s", (cid,)).fetchone()
        if not claim: return ('', 404)

        if action == 'approve' and b.get('create_finance_record', True):
            _cat_map = {
                '餐費':        '食材成本',
                '辦公用品':    '消耗品',
                '交通費':      '其他支出',
                '住宿費':      '其他支出',
                '廠商費用':    '其他支出',
                '固定營業費用': '其他支出',
                '健保費用':    '薪資支出',
                '勞工保險金費用': '薪資支出',
                '勞工退休金費用': '薪資支出',
                '稅金費用':    '其他支出',
                '雇員獎金費用': '薪資支出',
                '其它雜支費用': '其他支出',
                '其他':        '其他支出',
            }
            claim_category = (claim.get('category') or '').strip()
            target_cat_name = _cat_map.get(claim_category, '其他支出')
            cat = conn.execute(
                "SELECT id FROM finance_categories WHERE type='expense' AND active=TRUE AND name=%s LIMIT 1",
                (target_cat_name,)
            ).fetchone()
            if not cat:
                cat = conn.execute(
                    "SELECT id FROM finance_categories WHERE type='expense' AND active=TRUE ORDER BY sort_order LIMIT 1"
                ).fetchone()
            note_parts = [f"報帳申請 #{cid}"]
            if claim.get('note'): note_parts.append(claim['note'])
            if claim.get('document_id2'): note_parts.append(f"附件2 doc#{claim['document_id2']}")
            _co_name_map = {'AD影像事務所': 'ad', '進光設計': 'jm', 'AD': 'ad', 'JM': 'jm'}
            company_unit = _co_name_map.get((claim.get('company') or '').strip(), 'ad')
            frec = conn.execute("""
                INSERT INTO finance_records
                  (record_date, category_id, type, title, amount, note, document_id, created_by, company_unit)
                VALUES (%s,%s,'expense',%s,%s,%s,%s,'expense-claim',%s) RETURNING id
            """, (claim['expense_date'], cat['id'] if cat else None,
                  claim['title'], claim['amount'],
                  '：'.join(note_parts),
                  claim['document_id'], company_unit)).fetchone()
            finance_rid = frec['id']

        row = conn.execute("""
            UPDATE expense_claims SET
              status=%s, reviewed_by=%s, review_note=%s,
              reviewed_at=NOW(), finance_record_id=%s
            WHERE id=%s RETURNING *
        """, (new_status, reviewed_by, review_note, finance_rid, cid)).fetchone()
        staff = conn.execute(
            "SELECT name, employee_code FROM punch_staff WHERE id=%s", (claim['staff_id'],)
        ).fetchone() if row else None

    if row:
        extra = f"標題：{claim['title']}　金額：${float(claim['amount']):,.0f}"
        if review_note: extra += f"\n意見：{review_note}"
        _notify_review_result(claim['staff_id'], '費用報帳', action, extra)
        d = _expense_row(row)
        if staff:
            d['staff_name']    = staff['name']
            d['employee_code'] = staff['employee_code'] or ''
        _badges_cache.clear()
        _expense_list_cache.clear()
        return jsonify(d)
    return ('', 404)


@bp.route('/api/expense/claims/<int:cid>', methods=['PATCH'])
@login_required
def api_expense_claim_edit(cid):
    b = request.get_json(force=True)
    allowed = ['staff_id', 'expense_date', 'expense_type', 'category', 'vendor', 'amount',
               'note', 'review_note', 'reimbursement_method', 'bank_name', 'bank_branch',
               'bank_account', 'account_holder', 'title']
    sets, vals = [], []
    for key in allowed:
        if key in b:
            sets.append(f"{key}=%s")
            vals.append(b[key])
    if not sets:
        return jsonify({'error': 'nothing to update'}), 400
    vals.append(cid)
    with get_db() as conn:
        row = conn.execute(
            f"UPDATE expense_claims SET {', '.join(sets)} WHERE id=%s RETURNING *", vals
        ).fetchone()
        if not row:
            return ('', 404)
        staff = conn.execute(
            "SELECT name, employee_code FROM punch_staff WHERE id=%s", (row['staff_id'],)
        ).fetchone()
    d = _expense_row(row)
    if staff:
        d['staff_name']    = staff['name']
        d['employee_code'] = staff['employee_code'] or ''
    _expense_list_cache.clear()
    return jsonify(d)


@bp.route('/api/expense/claims/<int:cid>', methods=['DELETE'])
@login_required
def api_expense_claim_delete(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM expense_claims WHERE id=%s", (cid,))
    _expense_list_cache.clear()
    return jsonify({'deleted': cid})
