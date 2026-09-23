import hashlib
import hmac
import os
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException

from .db import connect

HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash('non-account-timing-placeholder')
COOKIE = 'emine_session'
SESSION_SECONDS = 12 * 3600


def secure_cookie():
    return os.environ.get('COOKIE_SECURE', 'true').lower() == 'true'


def validate_config():
    if len(os.environ.get('SECRET_KEY', '')) < 32:
        raise RuntimeError('SECRET_KEY en az 32 karakter olmalıdır.')
    if os.environ.get('APP_ENV', 'production') not in ('production', 'development', 'test'):
        raise RuntimeError('APP_ENV production veya development olmalıdır.')
    if os.environ.get('APP_ENV', 'production') == 'production':
        if not secure_cookie():
            raise RuntimeError('Üretimde COOKIE_SECURE=true gerekir.')
        hosts = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',')]
        if not hosts or '*' in hosts or not any(h.strip() for h in hosts):
            raise RuntimeError('Üretimde açık ALLOWED_HOSTS listesi gerekir.')
    # Credentials are required on every startup to avoid accidental insecure deployments.
    for prefix in ('ADMIN', 'EMINE'):
        name = os.environ.get(f'{prefix}_USERNAME', '')
        password = os.environ.get(f'{prefix}_PASSWORD', '')
        if not name.strip() or name != name.strip() or len(name) > 80 or len(password) < 12 or len(password) > 256:
            raise RuntimeError(f'{prefix}_USERNAME ve en az 12 karakterli {prefix}_PASSWORD gereklidir.')
        if password.lower() in ('changeme12345', 'password1234', '123456789012', 'change-me-now') or 'buraya_' in password.lower():
            raise RuntimeError('Örnek/varsayılan şifre kullanılamaz.')
    if os.environ['ADMIN_USERNAME'] == os.environ['EMINE_USERNAME']:
        raise RuntimeError('Kullanıcı adları farklı olmalıdır.')


def hash_password(password):
    return HASHER.hash(password)


def verify_password(encoded, password):
    try:
        return HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def token_hash(token):
    return hmac.new(os.environ['SECRET_KEY'].encode(), token.encode(), hashlib.sha256).hexdigest()


def create_session(user_id=None):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with connect(write=True) as con:
        con.execute('DELETE FROM sessions WHERE expires_at < ?', (int(time.time()),))
        con.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(token), user_id, csrf, int(time.time()) + (SESSION_SECONDS if user_id else 900)))
    return token, csrf


def read_session(request):
    token = request.cookies.get(COOKIE, '')
    if not token or len(token) > 128:
        return None
    with connect() as con:
        row = con.execute('SELECT s.*,u.username,u.role FROM sessions s LEFT JOIN users u ON s.user_id=u.id WHERE token_hash=? AND expires_at>?', (token_hash(token), int(time.time()))).fetchone()
        return dict(row) if row else None


def set_cookie(response, token):
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure_cookie(), samesite='strict', path='/')


def revoke(request):
    token = request.cookies.get(COOKIE, '')
    if token:
        with connect(write=True) as con:
            con.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(token),))


def require_user(request, admin=False):
    session = read_session(request)
    if not session or not session['user_id']:
        raise HTTPException(303, headers={'Location': '/login'})
    if admin and session['role'] != 'admin':
        raise HTTPException(403, 'Bu ekran yalnız yönetici içindir.')
    request.state.session = session
    return {'id': session['user_id'], 'username': session['username'], 'role': session['role']}


async def checked_form(request, anonymous=False):
    session = read_session(request)
    form = await request.form(max_files=1, max_fields=50, max_part_size=64 * 1024)
    if not session or not hmac.compare_digest(str(form.get('csrf', '')), session['csrf']):
        raise HTTPException(403, 'Oturum doğrulaması başarısız. Sayfayı yenileyip tekrar deneyin.')
    if not anonymous and not session['user_id']:
        raise HTTPException(403, 'Giriş yapmanız gerekiyor.')
    return form


def throttle_login(request, username):
    # Trust only socket peer / explicitly configured Uvicorn trusted proxy, never raw XFF.
    ip = request.client.host if request.client else 'unknown'
    stamp = int(time.time())
    keys = [(token_hash('ip:' + ip), 30), (token_hash('user:' + username.casefold()), 8)]
    with connect(write=True) as con:
        con.execute('DELETE FROM login_attempts WHERE reset_at<=?', (stamp,))
        for bucket, limit in keys:
            row = con.execute('SELECT * FROM login_attempts WHERE bucket=?', (bucket,)).fetchone()
            if row and row['count'] >= limit:
                raise HTTPException(429, 'Çok fazla giriş denemesi. 15 dakika sonra tekrar deneyin.', headers={'Retry-After': str(row['reset_at'] - stamp)})
        for bucket, _ in keys:
            con.execute('INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (bucket, stamp + 900))
