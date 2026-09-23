import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Istanbul')


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def data_dir():
    return Path(os.environ.get('DATA_DIR', '/data'))


@contextmanager
def connect(write=False):
    con = sqlite3.connect(data_dir() / 'emine.sqlite3', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def audit(con, user, entity, entity_id, action, before, after):
    if before == after:
        return
    con.execute('INSERT INTO audit(created_at,username,entity,entity_id,action,before_json,after_json) VALUES(?,?,?,?,?,?,?)',
                (now(), user['username'], entity, str(entity_id), action,
                 json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True)))


def initialize():
    from .security import hash_password, validate_config
    validate_config()
    data_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / 'receipts').mkdir(exist_ok=True)
    with connect() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','emine')));
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),csrf TEXT NOT NULL,expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL,reset_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS days(
            date TEXT PRIMARY KEY,menu TEXT NOT NULL DEFAULT '',requested INTEGER,made INTEGER,delivered INTEGER,revenue_cents INTEGER,
            prior_material_cents INTEGER,carry_material_cents INTEGER,
            material_confirmed INTEGER NOT NULL DEFAULT 0,other_confirmed INTEGER NOT NULL DEFAULT 0,time_confirmed INTEGER NOT NULL DEFAULT 0,energy_confirmed INTEGER NOT NULL DEFAULT 0,
            labor_source TEXT NOT NULL DEFAULT 'clock',shopping_minutes INTEGER,preparation_minutes INTEGER,delivery_minutes INTEGER,cleaning_minutes INTEGER,
            leftovers TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY,day_date TEXT NOT NULL REFERENCES days(date),category TEXT NOT NULL,name TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>=0));
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY,day_date TEXT NOT NULL REFERENCES days(date),paid_on TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0),note TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS payments_paid_on ON payments(paid_on);
        CREATE TABLE IF NOT EXISTS timers(id INTEGER PRIMARY KEY,day_date TEXT NOT NULL REFERENCES days(date),kind TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_timer ON timers(kind) WHERE ended_at IS NULL;
        CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,day_date TEXT NOT NULL REFERENCES days(date),filename TEXT UNIQUE NOT NULL,original_name TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),labor_hourly_cents INTEGER,stove_hourly_cents INTEGER,oven_hourly_cents INTEGER);
        INSERT OR IGNORE INTO settings(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS fixed_costs(id INTEGER PRIMARY KEY,month TEXT NOT NULL,name TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>=0));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,username TEXT NOT NULL,entity TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL);
        ''')
        if not con.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            for prefix, role in [('ADMIN', 'admin'), ('EMINE', 'emine')]:
                username = os.environ[f'{prefix}_USERNAME']
                password = os.environ[f'{prefix}_PASSWORD']
                con.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)', (username, hash_password(password), role))
