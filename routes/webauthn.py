import base64 as _b64
import json as _json3
import os

from flask import Blueprint, request, jsonify, session

from db import get_db, _get_admin_by_id

bp = Blueprint('webauthn', __name__)

_WEBAUTHN_RP_ID   = os.environ.get('WEBAUTHN_RP_ID',  'punch-system.onrender.com')
_WEBAUTHN_RP_NAME = '打卡系統'
_WEBAUTHN_ORIGIN  = os.environ.get('WEBAUTHN_ORIGIN', 'https://punch-system.onrender.com')


def _b64url_encode(data: bytes) -> str:
    return _b64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    s = str(s).replace('-', '+').replace('_', '/')
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return _b64.b64decode(s)


def init():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    id            SERIAL PRIMARY KEY,
                    user_key      TEXT NOT NULL,
                    credential_id TEXT NOT NULL UNIQUE,
                    public_key    BYTEA NOT NULL,
                    sign_count    BIGINT DEFAULT 0,
                    device_name   TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        print(f'[webauthn_init] {e}')


@bp.route('/api/webauthn/register/begin', methods=['POST'])
def webauthn_register_begin():
    user_key = user_name = user_display = None
    if session.get('logged_in'):
        user_key     = f"admin_{session['admin_id']}"
        user_name    = session.get('admin_username', '')
        user_display = session.get('admin_display_name', user_name)
    elif session.get('punch_staff_id'):
        sid = session['punch_staff_id']
        user_key     = f"staff_{sid}"
        user_name    = session.get('punch_staff_name', str(sid))
        user_display = user_name
    else:
        return jsonify({'error': '請先登入'}), 401
    try:
        from webauthn import generate_registration_options, options_to_json as _wa_o2j
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria, UserVerificationRequirement,
            ResidentKeyRequirement, AuthenticatorAttachment,
            AttestationConveyancePreference, PublicKeyCredentialDescriptor,
        )
        from webauthn.helpers.cose import COSEAlgorithmIdentifier
        import json as _waj
        with get_db() as conn:
            existing = conn.execute(
                "SELECT credential_id FROM webauthn_credentials WHERE user_key=%s", (user_key,)
            ).fetchall()
        exclude_creds = [PublicKeyCredentialDescriptor(id=_b64url_decode(r['credential_id'])) for r in existing]
        options = generate_registration_options(
            rp_id=_WEBAUTHN_RP_ID,
            rp_name=_WEBAUTHN_RP_NAME,
            user_id=user_key.encode('utf-8'),
            user_name=user_name,
            user_display_name=user_display or user_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                user_verification=UserVerificationRequirement.REQUIRED,
                resident_key=ResidentKeyRequirement.PREFERRED,
            ),
            attestation=AttestationConveyancePreference.NONE,
            supported_pub_key_algs=[
                COSEAlgorithmIdentifier.ECDSA_SHA_256,
                COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
            ],
            exclude_credentials=exclude_creds,
        )
        session['webauthn_reg_challenge'] = _b64url_encode(options.challenge)
        session['webauthn_reg_user_key']  = user_key
        return jsonify(_waj.loads(_wa_o2j(options)))
    except Exception as e:
        return jsonify({'error': f'初始化失敗：{e}'}), 500


@bp.route('/api/webauthn/register/complete', methods=['POST'])
def webauthn_register_complete():
    challenge_b64 = session.get('webauthn_reg_challenge')
    user_key      = session.get('webauthn_reg_user_key')
    if not challenge_b64 or not user_key:
        return jsonify({'error': '找不到挑戰，請重新開始'}), 400
    b = request.get_json(force=True) or {}
    try:
        from webauthn import verify_registration_response
        from webauthn.helpers.structs import RegistrationCredential, AuthenticatorAttestationResponse
        credential = RegistrationCredential(
            id=b['id'],
            raw_id=_b64url_decode(b['rawId']),
            response=AuthenticatorAttestationResponse(
                client_data_json=_b64url_decode(b['response']['clientDataJSON']),
                attestation_object=_b64url_decode(b['response']['attestationObject']),
            ),
            type=b.get('type', 'public-key'),
        )
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge_b64),
            expected_rp_id=_WEBAUTHN_RP_ID,
            expected_origin=_WEBAUTHN_ORIGIN,
            require_user_verification=True,
        )
        credential_id = b['id']
        public_key    = verification.credential_public_key
        sign_count    = verification.sign_count
        device_name   = b.get('device_name', '我的裝置')
        with get_db() as conn:
            conn.execute("""
                INSERT INTO webauthn_credentials
                  (user_key, credential_id, public_key, sign_count, device_name)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (credential_id) DO UPDATE
                  SET sign_count=%s, device_name=%s, user_key=%s
            """, (user_key, credential_id, public_key, sign_count, device_name,
                  sign_count, device_name, user_key))
        session.pop('webauthn_reg_challenge', None)
        session.pop('webauthn_reg_user_key', None)
        return jsonify({'ok': True})
    except Exception as ex:
        return jsonify({'error': f'綁定失敗：{ex}'}), 400


@bp.route('/api/webauthn/auth/begin', methods=['POST'])
def webauthn_auth_begin():
    b        = request.get_json(force=True) or {}
    username = (b.get('username') or '').strip()
    try:
        from webauthn import generate_authentication_options, options_to_json as _wa_o2j
        from webauthn.helpers.structs import UserVerificationRequirement, PublicKeyCredentialDescriptor
        import json as _waj
        allow_credentials = []
        if username:
            with get_db() as conn:
                admin = conn.execute(
                    "SELECT id FROM admin_accounts WHERE username=%s AND active=TRUE", (username,)
                ).fetchone()
                if admin:
                    user_key = f"admin_{admin['id']}"
                else:
                    staff = conn.execute(
                        "SELECT id FROM punch_staff WHERE username=%s AND active=TRUE", (username,)
                    ).fetchone()
                    user_key = f"staff_{staff['id']}" if staff else None
                if user_key:
                    creds = conn.execute(
                        "SELECT credential_id FROM webauthn_credentials WHERE user_key=%s", (user_key,)
                    ).fetchall()
                    allow_credentials = [
                        PublicKeyCredentialDescriptor(id=_b64url_decode(r['credential_id']))
                        for r in creds
                    ]
        options = generate_authentication_options(
            rp_id=_WEBAUTHN_RP_ID,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        session['webauthn_auth_challenge'] = _b64url_encode(options.challenge)
        return jsonify(_waj.loads(_wa_o2j(options)))
    except Exception as e:
        return jsonify({'error': f'初始化失敗：{e}'}), 500


@bp.route('/api/webauthn/auth/complete', methods=['POST'])
def webauthn_auth_complete():
    challenge_b64 = session.get('webauthn_auth_challenge')
    if not challenge_b64:
        return jsonify({'error': '找不到挑戰，請重新開始'}), 400
    b = request.get_json(force=True) or {}
    try:
        from webauthn import verify_authentication_response
        from webauthn.helpers.structs import AuthenticationCredential, AuthenticatorAssertionResponse
        credential_id = b['id']
        with get_db() as conn:
            cred = conn.execute(
                "SELECT * FROM webauthn_credentials WHERE credential_id=%s", (credential_id,)
            ).fetchone()
        if not cred:
            return jsonify({'error': '找不到已綁定的裝置，請先綁定'}), 401
        user_handle_raw = b['response'].get('userHandle')
        credential = AuthenticationCredential(
            id=credential_id,
            raw_id=_b64url_decode(b['rawId']),
            response=AuthenticatorAssertionResponse(
                client_data_json=_b64url_decode(b['response']['clientDataJSON']),
                authenticator_data=_b64url_decode(b['response']['authenticatorData']),
                signature=_b64url_decode(b['response']['signature']),
                user_handle=_b64url_decode(user_handle_raw) if user_handle_raw else None,
            ),
            type=b.get('type', 'public-key'),
        )
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge_b64),
            expected_rp_id=_WEBAUTHN_RP_ID,
            expected_origin=_WEBAUTHN_ORIGIN,
            credential_public_key=bytes(cred['public_key']),
            credential_current_sign_count=int(cred['sign_count']),
            require_user_verification=True,
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE webauthn_credentials SET sign_count=%s WHERE id=%s",
                (verification.new_sign_count, cred['id'])
            )
        session.pop('webauthn_auth_challenge', None)
        user_key = cred['user_key']
        if user_key.startswith('admin_'):
            admin_id = int(user_key[6:])
            admin = _get_admin_by_id(admin_id)
            if not admin:
                return jsonify({'error': '帳號不存在或已停用'}), 401
            perms = admin['permissions']
            if isinstance(perms, str):
                try: perms = _json3.loads(perms)
                except (ValueError, TypeError): perms = []
            session.permanent             = True
            session['logged_in']          = True
            session['admin_id']           = admin['id']
            session['admin_username']     = admin['username']
            session['admin_display_name'] = admin['display_name'] or admin['username']
            session['admin_permissions']  = perms
            session['admin_is_super']     = bool(admin['is_super'])
            return jsonify({'ok': True, 'redirect': '/admin', 'role': 'admin'})
        elif user_key.startswith('staff_'):
            staff_id = int(user_key[6:])
            with get_db() as conn:
                staff = conn.execute(
                    "SELECT id, name, role FROM punch_staff WHERE id=%s AND active=TRUE", (staff_id,)
                ).fetchone()
            if not staff:
                return jsonify({'error': '帳號不存在或已停用'}), 401
            session['punch_staff_id']   = staff['id']
            session['punch_staff_name'] = staff['name']
            return jsonify({'ok': True, 'role': 'staff', 'user': dict(staff)})
        return jsonify({'error': '未知帳號類型'}), 400
    except Exception as ex:
        return jsonify({'error': f'驗證失敗：{ex}'}), 400


@bp.route('/api/webauthn/credentials', methods=['GET'])
def webauthn_list_credentials():
    if session.get('logged_in'):
        user_key = f"admin_{session['admin_id']}"
    elif session.get('punch_staff_id'):
        user_key = f"staff_{session['punch_staff_id']}"
    else:
        return jsonify({'error': '請先登入'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, device_name, created_at FROM webauthn_credentials WHERE user_key=%s ORDER BY created_at DESC",
            (user_key,)
        ).fetchall()
    return jsonify([{'id': r['id'], 'device_name': r['device_name'],
                     'created_at': str(r['created_at'])} for r in rows])


@bp.route('/api/webauthn/credentials/<int:cid>', methods=['DELETE'])
def webauthn_delete_credential(cid):
    if session.get('logged_in'):
        user_key = f"admin_{session['admin_id']}"
    elif session.get('punch_staff_id'):
        user_key = f"staff_{session['punch_staff_id']}"
    else:
        return jsonify({'error': '請先登入'}), 401
    with get_db() as conn:
        conn.execute(
            "DELETE FROM webauthn_credentials WHERE id=%s AND user_key=%s", (cid, user_key)
        )
    return jsonify({'ok': True})
