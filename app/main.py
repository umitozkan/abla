import csv
import io
import os
import re
import secrets
import sqlite3
import warnings
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import security
from .calculations import calculate_day, scenario
from .db import TZ, audit, connect, data_dir, initialize, now

BASE = Path(__file__).parent
MAX_PHOTO = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app = FastAPI(title='Emine’nin Mutfağı', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')])


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        # Bound the entire request before Starlette parses either form encoding.
        limit = MAX_PHOTO + 128 * 1024 if scope['path'].endswith('/receipts') else 64 * 1024
        chunks, length = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            length += len(chunk)
            if length > limit:
                response = Response('Yükleme çok büyük. Fotoğraf en fazla 8 MB olabilir.', status_code=413)
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        sent = False
        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        await self.app(scope, bounded_receive, send)


app.add_middleware(BodyLimitMiddleware)


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=(), geolocation=()'
    if not request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-store'
    if security.secure_cookie():
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


# All templates escape user input; uploaded files are never mounted as static files.
BASE.joinpath('static').mkdir(exist_ok=True)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
templates = Jinja2Templates(directory=BASE / 'templates')


def money(value):
    if value is None:
        return '—'
    text = f'{Decimal(str(value)) / 100:,.2f}'
    return text.replace(',', '_').replace('.', ',').replace('_', '.') + ' TL'


def localdt(value):
    return datetime.fromisoformat(value).astimezone(TZ).strftime('%Y-%m-%dT%H:%M') if value else ''


templates.env.filters.update(money=money, localdt=localdt, dt=lambda v: datetime.fromisoformat(v).astimezone(TZ).strftime('%d.%m.%Y %H:%M') if v else 'Devam ediyor')


def render(request, name, user=None, status_code=200, **context):
    session = getattr(request.state, 'session', None) or security.read_session(request)
    return templates.TemplateResponse(request=request, name=name, context={
        'user': user, 'csrf': session['csrf'] if session else '', 'today': datetime.now(TZ).date().isoformat(), **context
    }, status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def error_handler(request, exc):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers['Location'], status_code=303)
    response = render(request, 'error.html', status_code=exc.status_code, message=exc.detail, status_code_display=exc.status_code)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


def iso_date(value):
    try:
        parsed = date.fromisoformat(str(value))
        if not 2000 <= parsed.year <= 2100:
            raise ValueError()
        return parsed.isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, 'Geçerli bir tarih girin (2000–2100).')


def text_field(form, name, max_len=2000, required=False):
    value = str(form.get(name, '')).strip()
    if len(value) > max_len or (required and not value):
        raise HTTPException(422, f'{name}: alan boş veya çok uzun.')
    return value


def cents(value, nullable=True):
    if value is None or str(value).strip() == '':
        if nullable:
            return None
        raise HTTPException(422, 'Tutar girin.')
    try:
        raw = str(value).strip()
        if not re.fullmatch(r'[0-9]+(?:[.,][0-9]{1,2})?', raw):
            raise ValueError()
        val = Decimal(raw.replace(',', '.'))
        if not val.is_finite() or val < 0 or val > 100_000_000 or val != val.quantize(Decimal('0.01')):
            raise ValueError()
        return int((val * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, 'Tutar 0–100.000.000 TL arasında, en fazla iki ondalık olmalıdır. Binlik ayırıcı kullanmayın.')


def integer(value, maximum=100000, nullable=True):
    if value is None or str(value).strip() == '':
        if nullable:
            return None
        raise HTTPException(422, 'Sayı girin.')
    try:
        val = int(str(value))
        if not 0 <= val <= maximum:
            raise ValueError()
        return val
    except ValueError:
        raise HTTPException(422, f'0–{maximum} arasında bir tam sayı girin.')


def get_day(con, day_date):
    row = con.execute('SELECT * FROM days WHERE date=?', (iso_date(day_date),)).fetchone()
    if not row:
        raise HTTPException(404, 'Günlük kayıt bulunamadı.')
    return dict(row)


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def one(con, table, item_id, day_date=None):
    # Table names are constants from the route handlers, never user-controlled.
    row = con.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
    if not row or (day_date and row['day_date'] != day_date):
        raise HTTPException(404, 'Kayıt bulunamadı.')
    return dict(row)


def save_item(con, user, table, values, item_id=None, day_date=None):
    before = one(con, table, item_id, day_date) if item_id is not None else None
    if before:
        con.execute(f'UPDATE {table} SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), item_id))
    else:
        cur = con.execute(f'INSERT INTO {table} (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
        item_id = cur.lastrowid
    after = one(con, table, item_id)
    audit(con, user, table, item_id, 'update' if before else 'create', before, after)
    return item_id


def redirect_day(day_date):
    return RedirectResponse('/days/' + day_date, status_code=303)


@app.get('/health')
def health():
    with connect() as con:
        con.execute('SELECT 1 FROM settings').fetchone()
    return {'status': 'ok'}


@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    session = security.read_session(request)
    if session and session['user_id']:
        return RedirectResponse('/', status_code=303)
    token, csrf = security.create_session()
    response = render(request, 'login.html', csrf=csrf)
    security.set_cookie(response, token)
    return response


@app.post('/login')
async def login(request: Request):
    form = await security.checked_form(request, anonymous=True)
    username = text_field(form, 'username', 80, True)
    password = str(form.get('password', ''))
    if not password or len(password) > 256:
        raise HTTPException(422, 'Şifre girin; en fazla 256 karakter olabilir.')
    security.throttle_login(request, username)
    with connect() as con:
        user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    # A real Argon2 verification for unknown usernames avoids fast account probing.
    encoded = user['password_hash'] if user else security.DUMMY_HASH
    valid = security.verify_password(encoded, password)
    if not user or not valid:
        return render(request, 'login.html', error='Kullanıcı adı veya şifre hatalı.', status_code=401)
    security.revoke(request)
    token, _ = security.create_session(user['id'])
    response = RedirectResponse('/admin' if user['role'] == 'admin' else '/', status_code=303)
    security.set_cookie(response, token)
    return response


@app.post('/logout')
async def logout(request: Request):
    await security.checked_form(request)
    security.revoke(request)
    response = RedirectResponse('/login', status_code=303)
    response.delete_cookie(security.COOKIE, path='/')
    return response


@app.get('/')
def home(request: Request):
    user = security.require_user(request)
    with connect() as con:
        days = rows(con, 'SELECT * FROM days ORDER BY date DESC LIMIT 90')
    return render(request, 'home.html', user, days=days)


@app.post('/days')
async def create_day(request: Request):
    user = security.require_user(request)
    form = await security.checked_form(request)
    day_date = iso_date(form.get('date'))
    with connect(write=True) as con:
        changed = con.execute('INSERT OR IGNORE INTO days(date) VALUES(?)', (day_date,)).rowcount
        if changed:
            audit(con, user, 'days', day_date, 'create', None, get_day(con, day_date))
    return redirect_day(day_date)


def day_data(con, day):
    expenses = rows(con, 'SELECT * FROM expenses WHERE day_date=? ORDER BY id', (day['date'],))
    timers = rows(con, 'SELECT * FROM timers WHERE day_date=? ORDER BY started_at', (day['date'],))
    settings = dict(con.execute('SELECT * FROM settings WHERE id=1').fetchone())
    fixed = rows(con, 'SELECT * FROM fixed_costs')
    return expenses, timers, calculate_day(day, expenses, timers, settings, fixed)


@app.get('/days/{day_date}')
def day_page(day_date: str, request: Request):
    user = security.require_user(request)
    with connect() as con:
        day = get_day(con, day_date)
        expenses, timers, result = day_data(con, day)
        payments = rows(con, 'SELECT * FROM payments WHERE day_date=? ORDER BY paid_on,id', (day_date,))
        receipts = rows(con, 'SELECT * FROM receipts WHERE day_date=? ORDER BY id DESC', (day_date,))
    collected = sum(p['amount_cents'] for p in payments)
    return render(request, 'day.html', user, day=day, expenses=expenses, timers=timers, result=result, payments=payments, receipts=receipts,
                  active={t['kind']: t for t in timers if not t['ended_at']}, collected_cents=collected,
                  receivable_cents=None if day['revenue_cents'] is None else day['revenue_cents'] - collected)


@app.post('/days/{day_date}/save')
async def save_day(day_date: str, request: Request):
    user = security.require_user(request)
    form = await security.checked_form(request)
    values = {key: text_field(form, key) for key in ('menu', 'leftovers', 'notes')}
    for key in ('requested', 'made', 'delivered'):
        values[key] = integer(form.get(key))
    for key in ('revenue', 'prior_material', 'carry_material'):
        values[key + '_cents'] = cents(form.get(key))
    for key in ('material_confirmed', 'other_confirmed', 'time_confirmed', 'energy_confirmed'):
        values[key] = int(form.get(key) in ('on', '1', 'true'))
    values['labor_source'] = text_field(form, 'labor_source', 20)
    if values['labor_source'] not in ('clock', 'tasks'):
        raise HTTPException(422, 'Emek süresi kaynağını seçin.')
    for key in ('shopping_minutes', 'preparation_minutes', 'delivery_minutes', 'cleaning_minutes'):
        values[key] = integer(form.get(key), 1440)
    if values['labor_source'] == 'tasks' and sum(values[k] or 0 for k in values if k.endswith('_minutes')) > 1440:
        raise HTTPException(422, 'Bir gün için toplam emek süresi 24 saati geçemez.')
    revision = integer(form.get('revision'), 1_000_000, False)
    with connect(write=True) as con:
        before = get_day(con, day_date)
        if before['revision'] != revision:
            raise HTTPException(409, 'Bu kayıt başka bir ekranda değişti. Sayfayı yenileyip son bilgileri kontrol edin.')
        values['revision'] = revision + 1
        con.execute('UPDATE days SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE date=?', (*values.values(), day_date))
        audit(con, user, 'days', day_date, 'update', before, get_day(con, day_date))
    return redirect_day(day_date)


async def expense_mutation(day_date, request, item_id=None):
    user = security.require_user(request)
    form = await security.checked_form(request)
    category = text_field(form, 'category', 20)
    if category not in ('material', 'other', 'equipment'):
        raise HTTPException(422, 'Gider türü geçersiz.')
    values = dict(day_date=day_date, category=category, name=text_field(form, 'name', 160, True), amount_cents=cents(form.get('amount'), False))
    with connect(write=True) as con:
        get_day(con, day_date)
        save_item(con, user, 'expenses', values, item_id, day_date)
    return redirect_day(day_date)


@app.post('/days/{day_date}/expenses')
async def add_expense(day_date: str, request: Request):
    return await expense_mutation(day_date, request)


@app.post('/days/{day_date}/expenses/{item_id}')
async def edit_expense(day_date: str, item_id: int, request: Request):
    return await expense_mutation(day_date, request, item_id)


async def payment_mutation(day_date, request, item_id=None):
    user = security.require_user(request)
    form = await security.checked_form(request)
    amount = cents(form.get('amount'), False)
    if not amount:
        raise HTTPException(422, 'Tahsilat sıfırdan büyük olmalıdır.')
    values = dict(day_date=day_date, paid_on=iso_date(form.get('paid_on')), amount_cents=amount, note=text_field(form, 'note', 300))
    with connect(write=True) as con:
        get_day(con, day_date)
        save_item(con, user, 'payments', values, item_id, day_date)
    return redirect_day(day_date)


@app.post('/days/{day_date}/payments')
async def add_payment(day_date: str, request: Request):
    return await payment_mutation(day_date, request)


@app.post('/days/{day_date}/payments/{item_id}')
async def edit_payment(day_date: str, item_id: int, request: Request):
    return await payment_mutation(day_date, request, item_id)


def parse_time(value, day_date):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=TZ)
        stamp = stamp.astimezone(TZ)
        if stamp.date() not in (date.fromisoformat(day_date), date.fromisoformat(day_date) + timedelta(days=1)):
            raise ValueError()
        if stamp > datetime.now(TZ) + timedelta(minutes=1):
            raise ValueError()
        return stamp.isoformat(timespec='seconds')
    except (TypeError, ValueError):
        raise HTTPException(422, 'Saat kaydın gününe veya ertesi güne ait olmalı ve gelecekte olmamalıdır.')


def validate_timer(con, day_date, kind, start, end, item_id=None):
    if not start or datetime.fromisoformat(start).date().isoformat() != day_date:
        raise HTTPException(422, 'Başlangıç saati kaydın gününde olmalıdır.')
    if end and (end <= start or datetime.fromisoformat(end) - datetime.fromisoformat(start) > timedelta(hours=24)):
        raise HTTPException(422, 'Bitiş başlangıçtan sonra ve en fazla 24 saat içinde olmalıdır.')
    # Same kind must not overlap, including timers on a neighbouring day.
    for timer in rows(con, 'SELECT * FROM timers WHERE kind=? AND id!=?', (kind, item_id or -1)):
        if start < (timer['ended_at'] or '9999') and timer['started_at'] < (end or '9999'):
            raise HTTPException(409, 'Bu türde çakışan veya açık bir saat kaydı var. Önce onu düzeltin.')


@app.post('/days/{day_date}/timer')
async def quick_timer(day_date: str, request: Request):
    user = security.require_user(request)
    form = await security.checked_form(request)
    kind, action = form.get('kind'), form.get('action')
    if kind not in ('work', 'stove', 'oven') or action not in ('start', 'stop'):
        raise HTTPException(422, 'Saat işlemi geçersiz.')
    with connect(write=True) as con:
        get_day(con, day_date)
        current = con.execute('SELECT * FROM timers WHERE kind=? AND ended_at IS NULL', (kind,)).fetchone()
        stamp = now()
        if action == 'start':
            if day_date != datetime.now(TZ).date().isoformat():
                raise HTTPException(422, 'Otomatik başlangıç yalnız bugün için kullanılabilir. Geçmiş günlerde elle saat ekleyin.')
            if current:
                raise HTTPException(409, f'{current["day_date"]} gününde açık saat kaydı var.')
            validate_timer(con, day_date, kind, stamp, None)
            save_item(con, user, 'timers', dict(day_date=day_date, kind=kind, started_at=stamp, ended_at=None))
        else:
            if not current or current['day_date'] != day_date:
                raise HTTPException(409, 'Bu günde açık saat kaydı yok.')
            # A one-second minimum permits a fast accidental start/stop to be recorded safely.
            if stamp <= current['started_at']:
                stamp = (datetime.fromisoformat(current['started_at']) + timedelta(seconds=1)).isoformat()
            validate_timer(con, day_date, kind, current['started_at'], stamp, current['id'])
            save_item(con, user, 'timers', {'ended_at': stamp}, current['id'], day_date)
    return redirect_day(day_date)


@app.post('/days/{day_date}/timers')
@app.post('/days/{day_date}/timers/{item_id}')
async def edit_timer(day_date: str, request: Request, item_id: int = None):
    user = security.require_user(request)
    form = await security.checked_form(request)
    with connect(write=True) as con:
        get_day(con, day_date)
        kind = one(con, 'timers', item_id, day_date)['kind'] if item_id else form.get('kind')
        if kind not in ('work', 'stove', 'oven'):
            raise HTTPException(422, 'Saat türünü seçin.')
        start, end = parse_time(form.get('started_at'), day_date), parse_time(form.get('ended_at'), day_date)
        validate_timer(con, day_date, kind, start, end, item_id)
        save_item(con, user, 'timers', dict(day_date=day_date, kind=kind, started_at=start, ended_at=end), item_id, day_date)
    return redirect_day(day_date)


@app.post('/days/{day_date}/receipts')
async def upload_receipt(day_date: str, request: Request):
    user = security.require_user(request)
    form = await security.checked_form(request)
    photo = form.get('photo')
    if not hasattr(photo, 'read') or photo.content_type not in ('image/jpeg', 'image/png', 'image/webp'):
        raise HTTPException(422, 'JPEG, PNG veya WebP fotoğraf seçin.')
    raw = await photo.read(MAX_PHOTO + 1)
    await photo.close()
    if len(raw) > MAX_PHOTO:
        raise HTTPException(413, 'Fotoğraf en fazla 8 MB olabilir.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format not in ('JPEG', 'PNG', 'WEBP') or picture.width * picture.height > 16_000_000:
                    raise ValueError()
                picture.load()
                normalized = picture.convert('RGB')
                # Preserve camera orientation while removing private EXIF metadata.
                from PIL import ImageOps
                normalized = ImageOps.exif_transpose(picture).convert('RGB')
                normalized.thumbnail((2400, 2400))
                out = io.BytesIO()
                normalized.save(out, format='JPEG', quality=88)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, 'Fotoğraf okunamadı veya 16 megapiksel sınırını aşıyor. Daha küçük JPEG/PNG/WebP yükleyin.')
    filename = secrets.token_hex(24) + '.jpg'
    dest = data_dir() / 'receipts' / filename
    try:
        with connect(write=True) as con:
            get_day(con, day_date)
            if con.execute('SELECT count(*) FROM receipts WHERE day_date=?', (day_date,)).fetchone()[0] >= 30:
                raise HTTPException(422, 'Bir güne en fazla 30 fiş eklenebilir.')
            dest.write_bytes(out.getvalue())
            save_item(con, user, 'receipts', dict(day_date=day_date, filename=filename, original_name=Path(photo.filename or 'Fiş').name[:160], created_at=now()))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return redirect_day(day_date)


@app.get('/receipts/{item_id}')
def receipt(item_id: int, request: Request):
    security.require_user(request)
    with connect() as con:
        item = one(con, 'receipts', item_id)
    root = (data_dir() / 'receipts').resolve()
    path = (root / item['filename']).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(404, 'Fiş bulunamadı.')
    return FileResponse(path, media_type='image/jpeg', headers={'Content-Disposition': 'inline; filename="fis.jpg"'})


@app.post('/days/{day_date}/{table}/{item_id}/delete')
async def delete_item(day_date: str, table: str, item_id: int, request: Request):
    if table not in ('expenses', 'payments', 'timers', 'receipts'):
        raise HTTPException(404, 'Kayıt türü bulunamadı.')
    user = security.require_user(request)
    await security.checked_form(request)
    with connect(write=True) as con:
        before = one(con, table, item_id, day_date)
        con.execute(f'DELETE FROM {table} WHERE id=?', (item_id,))
        audit(con, user, table, item_id, 'delete', before, None)
        # Files are retained if a DB error occurs; a successful deletion can leave no anonymous URL.
        if table == 'receipts':
            root = (data_dir() / 'receipts').resolve()
            path = (root / before['filename']).resolve()
            if path.parent != root:
                raise HTTPException(400, 'Dosya yolu geçersiz.')
            path.unlink(missing_ok=True)
    return redirect_day(day_date)


def report_period(start=None, end=None):
    today = datetime.now(TZ).date()
    end = iso_date(end or today.isoformat())
    start = iso_date(start or (date.fromisoformat(end) - timedelta(days=6)).isoformat())
    if start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days > 365:
        raise HTTPException(422, 'En fazla 366 günlük, başlangıcı bitişten önce olan bir aralık seçin.')
    return start, end


def build_report(start, end):
    with connect() as con:
        # One explicit read transaction ensures the report uses one SQLite snapshot.
        con.execute('BEGIN')
        days = rows(con, 'SELECT * FROM days WHERE date BETWEEN ? AND ? ORDER BY date DESC', (start, end))
        settings = dict(con.execute('SELECT * FROM settings WHERE id=1').fetchone())
        fixed = rows(con, 'SELECT * FROM fixed_costs')
        expenses = rows(con, 'SELECT * FROM expenses WHERE day_date BETWEEN ? AND ?', (start, end))
        timers = rows(con, 'SELECT * FROM timers WHERE day_date BETWEEN ? AND ?', (start, end))
        payments = rows(con, 'SELECT * FROM payments WHERE paid_on<=?', (end,))
    day_map = {d['date']: d for d in days}
    calculated_rows, missing_days = [], []
    sum_keys = ('revenue_cents', 'purchases_cents', 'material_cents', 'other_cents', 'equipment_cents', 'labor_minutes', 'labor_cents', 'energy_cents', 'fixed_cents', 'before_labor_cents', 'result_cents', 'total_cost_cents')
    totals = {k: 0 for k in sum_keys}
    totals.update(collected_cents=sum(p['amount_cents'] for p in payments if start <= p['paid_on'] <= end), receivable_cents=0, advance_cents=0, delivered=0)
    week_map = {}
    current = date.fromisoformat(start)
    invalid_cost = False
    unrecorded_fixed = 0
    while current <= date.fromisoformat(end):
        ds = current.isoformat()
        day = day_map.get(ds)
        week = (current - timedelta(days=current.weekday())).isoformat()
        weekly = week_map.setdefault(week, {'week': week, 'revenue_cents': 0, 'result_cents': 0, 'labor_minutes': 0})
        if day:
            result = calculate_day(day, [e for e in expenses if e['day_date'] == ds], [t for t in timers if t['day_date'] == ds], settings, fixed)
            collected = sum(p['amount_cents'] for p in payments if p['day_date'] == ds)
            balance = None if day['revenue_cents'] is None else day['revenue_cents'] - collected
            if balance is not None:
                totals['receivable_cents'] += max(0, balance)
                totals['advance_cents'] += max(0, -balance)
            if result['total_cost_cents'] is None:
                invalid_cost = True
                weekly['result_cents'] = None
            for k in sum_keys:
                totals[k] += result[k] or 0
            totals['delivered'] += day['delivered'] or 0
            for k in ('revenue_cents', 'result_cents', 'labor_minutes'):
                if weekly[k] is not None:
                    weekly[k] += result[k] or 0
            calculated_rows.append({'day': day, 'result': result, 'collected_cents': collected, 'receivable_cents': balance})
        else:
            missing_days.append(ds)
            allocation = calculate_day({'date': ds}, [], [], settings, fixed)['fixed_cents']
            totals['fixed_cents'] += allocation
            totals['total_cost_cents'] += allocation
            totals['before_labor_cents'] -= allocation
            totals['result_cents'] -= allocation
            unrecorded_fixed += allocation
            if weekly['result_cents'] is not None:
                weekly['result_cents'] -= allocation
        current += timedelta(days=1)
    # Unknown revenue is not silently dropped together with its known costs.
    totals['before_labor_cents'] = totals['revenue_cents'] - (totals['total_cost_cents'] - totals['labor_cents'])
    totals['result_cents'] = totals['revenue_cents'] - totals['total_cost_cents']
    if invalid_cost:
        for key in ('material_cents', 'total_cost_cents', 'before_labor_cents', 'result_cents'):
            totals[key] = None
    def ratio(value, denom, factor=1):
        return None if value is None or not denom else int((Decimal(value) * factor / Decimal(denom)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    totals['margin_percent'] = None if totals['result_cents'] is None or not totals['revenue_cents'] else round(totals['result_cents'] * 100 / totals['revenue_cents'], 2)
    totals['price_per_portion_cents'] = ratio(totals['revenue_cents'], totals['delivered'])
    totals['cost_per_portion_cents'] = ratio(totals['total_cost_cents'], totals['delivered'])
    totals['result_per_portion_cents'] = ratio(totals['result_cents'], totals['delivered'])
    # Recompute each week's result from known sales and costs, including empty-day allocations.
    for weekly in week_map.values():
        if weekly['result_cents'] is not None:
            wr = [r for r in calculated_rows if (date.fromisoformat(r['day']['date']) - timedelta(days=date.fromisoformat(r['day']['date']).weekday())).isoformat() == weekly['week']]
            known_costs = sum((r['result']['total_cost_cents'] or 0) for r in wr)
            gap_costs = sum(calculate_day({'date': ds}, [], [], settings, fixed)['fixed_cents'] for ds in missing_days if (date.fromisoformat(ds) - timedelta(days=date.fromisoformat(ds).weekday())).isoformat() == weekly['week'])
            weekly['result_cents'] = weekly['revenue_cents'] - known_costs - gap_costs
    return dict(start=start, end=end, rows=sorted(calculated_rows, key=lambda r: r['day']['date'], reverse=True), totals=totals,
                weeks=sorted(week_map.values(), key=lambda r: r['week'], reverse=True), missing_days=missing_days,
                incomplete_count=sum(not r['result']['complete'] for r in calculated_rows), unrecorded_fixed_cents=unrecorded_fixed)


@app.get('/admin')
def reports(request: Request, start: str = None, end: str = None):
    user = security.require_user(request, admin=True)
    start, end = report_period(start, end)
    return render(request, 'reports.html', user, **build_report(start, end))


@app.get('/admin/export.csv')
def export_csv(request: Request, start: str = None, end: str = None):
    security.require_user(request, admin=True)
    start, end = report_period(start, end)
    report = build_report(start, end)
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Tür', 'Tarih', 'Menü/açıklama', 'Teslim porsiyon', 'Satış TL', 'Tahsilat TL', 'Alacak TL', 'Alışveriş TL', 'Tüketilen malzeme tahmini TL', 'Diğer gider TL', 'Ekipman TL (sonuç dışı)', 'Emek saat', 'Emek bedeli TL', 'Enerji tahmini TL', 'Sabit gider TL', 'Emek hariç TL', 'Emek dahil tahmini sonuç TL', 'Eksikler'])
    def num(v):
        return '' if v is None else f'{Decimal(str(v)) / 100:.2f}'.replace('.', ',')
    def safe(v):
        value = str(v)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else value
    def record(kind, ds, label, portions, r, collection, balance, missing):
        writer.writerow([kind, ds, safe(label), portions or 0, num(r['revenue_cents']), num(collection), num(balance), num(r['purchases_cents']), num(r['material_cents']), num(r['other_cents']), num(r['equipment_cents']), round(r['labor_minutes'] / 60, 2), num(r['labor_cents']), num(r['energy_cents']), num(r['fixed_cents']), num(r['before_labor_cents']), num(r['result_cents']), safe(missing)])
    for row in report['rows']:
        record('Gün', row['day']['date'], row['day']['menu'], row['day']['delivered'], row['result'], row['collected_cents'], row['receivable_cents'], ' | '.join(row['result']['missing']))
    for ds in report['missing_days']:
        writer.writerow(['Kayıtsız gün', ds, 'Günlük giriş yok; sabit gider payı aralık toplamına dahil'])
    record('Aralık toplamı', f'{start} / {end}', 'Tahsilat ödeme tarihine göre; alacak aralıktaki satışlar için bitiş tarihi itibarıyla. Boş gelirler sıfır kabul edilen eksik tahmin.', report['totals']['delivered'], report['totals'], report['totals']['collected_cents'], report['totals']['receivable_cents'], f'{report["incomplete_count"]} eksik kayıt; {len(report["missing_days"])} kayıtsız gün. Gün satırı tahsilatı bu satışa ait, aralık sonuna kadar alınan tutardır.')
    return Response('\ufeff' + output.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="emine-{start}-{end}.csv"'})


@app.get('/admin/scenario')
def scenario_page(request: Request, date: str = None, portions: str = None, unit_price: str = None):
    user = security.require_user(request, admin=True)
    with connect() as con:
        day = get_day(con, date or datetime.now(TZ).date().isoformat())
        _, _, result = day_data(con, day)
    scenario_result = None
    if portions is not None or unit_price is not None:
        count, price = integer(portions, nullable=False), cents(unit_price, False)
        try:
            scenario_result = scenario(result, count, price)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    return render(request, 'scenario.html', user, day=day, result=result, scenario_result=scenario_result, portions=portions or day['delivered'] or 15, unit_price=unit_price or '')


@app.get('/admin/settings')
def settings_page(request: Request):
    user = security.require_user(request, admin=True)
    with connect() as con:
        settings = dict(con.execute('SELECT * FROM settings WHERE id=1').fetchone())
        fixed = rows(con, 'SELECT * FROM fixed_costs ORDER BY month DESC,id')
    return render(request, 'settings.html', user, settings=settings, fixed_costs=fixed)


@app.post('/admin/settings')
async def update_settings(request: Request):
    user = security.require_user(request, admin=True)
    form = await security.checked_form(request)
    values = {key + '_cents': cents(form.get(key)) for key in ('labor_hourly', 'stove_hourly', 'oven_hourly')}
    with connect(write=True) as con:
        save_item(con, user, 'settings', values, 1)
    return RedirectResponse('/admin/settings', status_code=303)


@app.post('/admin/fixed')
@app.post('/admin/fixed/{item_id}')
async def fixed_mutation(request: Request, item_id: int = None):
    user = security.require_user(request, admin=True)
    form = await security.checked_form(request)
    month = text_field(form, 'month', 7, True)
    if len(month) != 7:
        raise HTTPException(422, 'Ay YYYY-AA biçiminde olmalıdır.')
    iso_date(month + '-01')
    values = dict(month=month, name=text_field(form, 'name', 160, True), amount_cents=cents(form.get('amount'), False))
    with connect(write=True) as con:
        save_item(con, user, 'fixed_costs', values, item_id)
    return RedirectResponse('/admin/settings', status_code=303)


@app.post('/admin/fixed/{item_id}/delete')
async def delete_fixed(item_id: int, request: Request):
    user = security.require_user(request, admin=True)
    await security.checked_form(request)
    with connect(write=True) as con:
        before = one(con, 'fixed_costs', item_id)
        con.execute('DELETE FROM fixed_costs WHERE id=?', (item_id,))
        audit(con, user, 'fixed_costs', item_id, 'delete', before, None)
    return RedirectResponse('/admin/settings', status_code=303)


@app.get('/admin/audit')
def audit_page(request: Request):
    user = security.require_user(request, admin=True)
    with connect() as con:
        entries = rows(con, 'SELECT * FROM audit ORDER BY id DESC LIMIT 500')
    return render(request, 'audit.html', user, entries=entries)
