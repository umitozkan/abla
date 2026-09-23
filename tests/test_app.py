"""HTTP integration checks for persisted records and security boundaries."""

import importlib
import csv
import io
import json
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image


ADMIN_USER = "yonetici_test"
ADMIN_PASSWORD = "Gh7!tfR2-uK9pZ"
EMINE_USER = "emine_test"
EMINE_PASSWORD = "Mn8!vyQ3-aL6sX"
DAY = "2024-02-15"


@pytest.fixture
def env(monkeypatch, tmp_path):
    values = {
        "DATA_DIR": str(tmp_path / "data"), "APP_ENV": "test", "COOKIE_SECURE": "false",
        "ALLOWED_HOSTS": "testserver,localhost", "SECRET_KEY": "ae147b3018c25434b93f119a3e38bf8694065825df4d",
        "ADMIN_USERNAME": ADMIN_USER, "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "EMINE_USERNAME": EMINE_USER, "EMINE_PASSWORD": EMINE_PASSWORD,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return tmp_path / "data"


@pytest.fixture
def client(env):
    main = importlib.import_module("app.main")
    with TestClient(main.app, follow_redirects=False) as client:
        yield client


def csrf(response):
    assert response.status_code == 200
    match = re.search(r'name="csrf"\s+value="([^"]+)"', response.text)
    assert match, "HTML form must carry a CSRF token"
    return match.group(1)


def login(client, role="emine"):
    token = csrf(client.get("/login"))
    response = client.post("/login", data={
        "csrf": token, "username": ADMIN_USER if role == "admin" else EMINE_USER,
        "password": ADMIN_PASSWORD if role == "admin" else EMINE_PASSWORD,
    })
    assert response.status_code == 303
    return csrf(client.get("/"))


def create_day(client, token, day_date=DAY):
    response = client.post("/days", data={"csrf": token, "date": day_date})
    assert response.status_code == 303
    assert response.headers["location"] == "/days/" + day_date
    return response


def saveday_payload(token, **changes):
    result = {
        "csrf": token, "revision": "1", "menu": "Mercimek çorbası, pilav", "requested": "15",
        "made": "15", "delivered": "15", "revenue": "4000", "prior_material": "0", "carry_material": "0",
        "labor_source": "tasks", "shopping_minutes": "30", "preparation_minutes": "120",
        "delivery_minutes": "30", "cleaning_minutes": "30", "material_confirmed": "on",
        "other_confirmed": "on", "time_confirmed": "on", "energy_confirmed": "on",
        "leftovers": "", "notes": "",
    }
    result.update(changes)
    return result


def database_rows(sql, params=()):
    from app.db import connect
    with connect() as connection:
        return [dict(row) for row in connection.execute(sql, params)]


def test_anonymous_requests_redirect_and_health_is_public(client):
    for path in ["/", "/days/" + DAY, "/admin", "/receipts/1"]:
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    assert client.get("/health").json() == {"status": "ok"}


def test_login_requires_csrf_and_rotates_session(client):
    page = client.get("/login")
    old_cookie = client.cookies.get("emine_session")
    old_token = csrf(page)
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "SameSite=strict" in page.headers["set-cookie"]
    rejected = client.post("/login", data={"username": EMINE_USER, "password": EMINE_PASSWORD})
    assert rejected.status_code == 403
    response = client.post("/login", data={"csrf": old_token, "username": EMINE_USER, "password": EMINE_PASSWORD})
    assert response.status_code == 303
    assert client.cookies.get("emine_session") != old_cookie
    assert client.post("/days", data={"csrf": old_token, "date": DAY}).status_code == 403
    from app.security import token_hash
    assert database_rows("SELECT * FROM sessions WHERE token_hash=?", (token_hash(old_cookie),)) == []


def test_passwords_are_hashed_and_session_tokens_are_not_stored(client):
    token = login(client)
    assert token
    for row in database_rows("SELECT * FROM users"):
        assert row["password_hash"].startswith("$argon2")
        assert row["password_hash"] not in (ADMIN_PASSWORD, EMINE_PASSWORD)
    cookie = client.cookies.get("emine_session")
    assert all(row["token_hash"] != cookie for row in database_rows("SELECT * FROM sessions"))


def test_emine_cannot_view_admin_pages(client):
    token = login(client)
    for path in ["/admin", "/admin/settings", "/admin/audit", "/admin/export.csv", "/admin/scenario"]:
        assert client.get(path).status_code == 403
    assert client.post("/admin/settings", data={"csrf": token, "labor_hourly": "1"}).status_code == 403
    assert client.post("/admin/fixed", data={"csrf": token, "month": "2024-02", "name": "Gider", "amount": "1"}).status_code == 403


def test_create_edit_and_revision_conflict_keep_actor_and_old_values(client):
    token = login(client)
    assert client.post("/days", data={"date": DAY}).status_code == 403
    create_day(client, token)
    first = client.post(f"/days/{DAY}/save", data=saveday_payload(token))
    assert first.status_code == 303
    assert database_rows("SELECT * FROM days")[0]["revision"] == 2
    updated = client.post(f"/days/{DAY}/save", data=saveday_payload(token, revision="2", revenue="4100,25"))
    assert updated.status_code == 303
    stale = client.post(f"/days/{DAY}/save", data=saveday_payload(token, revision="2", revenue="9"))
    assert stale.status_code == 409
    assert database_rows("SELECT * FROM days")[0]["revenue_cents"] == 410025
    audit_rows = database_rows("SELECT * FROM audit WHERE entity='days' ORDER BY id")
    assert len(audit_rows) == 3
    assert audit_rows[-1]["username"] == EMINE_USER
    assert json.loads(audit_rows[-1]["before_json"])["revenue_cents"] == 400000
    assert json.loads(audit_rows[-1]["after_json"])["revenue_cents"] == 410025


def test_duplicate_day_creation_preserves_existing_data(client):
    token = login(client)
    create_day(client, token)
    assert client.post(f"/days/{DAY}/save", data=saveday_payload(token)).status_code == 303
    create_day(client, token)
    assert len(database_rows("SELECT * FROM days")) == 1
    assert database_rows("SELECT * FROM days")[0]["menu"] == "Mercimek çorbası, pilav"
    assert len(database_rows("SELECT * FROM audit WHERE entity='days' AND action='create'")) == 1


def test_user_content_is_escaped_and_response_has_security_headers(client):
    token = login(client)
    create_day(client, token)
    marker = "<script>alert('x')</script>"
    assert client.post(f"/days/{DAY}/save", data=saveday_payload(token, menu=marker)).status_code == 303
    page = client.get(f"/days/{DAY}")
    assert page.status_code == 200
    assert marker not in page.text
    assert "&lt;script&gt;" in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"


def test_multiple_expenses_equipment_and_edit_audit(client):
    token = login(client)
    create_day(client, token)
    for category, value in [("material", "2000"), ("material", "20"), ("other", "100"), ("equipment", "7000")]:
        response = client.post(f"/days/{DAY}/expenses", data={"csrf": token, "category": category, "name": category, "amount": value})
        assert response.status_code == 303
    expenses = database_rows("SELECT * FROM expenses ORDER BY id")
    assert len(expenses) == 4
    response = client.post(f"/days/{DAY}/expenses/{expenses[0]['id']}", data={"csrf": token, "category": "material", "name": "Düzeltilmiş malzeme", "amount": "1900"})
    assert response.status_code == 303
    assert json.loads(database_rows("SELECT * FROM audit WHERE entity='expenses' ORDER BY id DESC")[0]["before_json"])["amount_cents"] == 200000
    assert client.post(f"/days/{DAY}/save", data=saveday_payload(token)).status_code == 303
    page = client.get(f"/days/{DAY}")
    result = page.context["result"]
    assert result["purchases_cents"] == 192000
    assert result["equipment_cents"] == 700000
    assert result["before_labor_other_cents"] == 208000
    assert result["result_cents"] == 198000  # labor rate is absent and visibly flagged
    assert result["missing"]


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1.234", "2.000", "1e3", "100000001", "abc"])
def test_invalid_money_never_writes_expense(client, value):
    token = login(client)
    create_day(client, token)
    response = client.post(f"/days/{DAY}/expenses", data={"csrf": token, "category": "material", "name": "Geçersiz", "amount": value})
    assert response.status_code == 422
    assert database_rows("SELECT * FROM expenses") == []


def test_expense_cannot_be_moved_between_days_by_changing_url(client):
    token = login(client)
    create_day(client, token)
    create_day(client, token, "2024-02-16")
    values = {"csrf": token, "category": "material", "name": "Pirinç", "amount": "25"}
    assert client.post(f"/days/{DAY}/expenses", data=values).status_code == 303
    expense_id = database_rows("SELECT * FROM expenses")[0]["id"]
    assert client.post(f"/days/2024-02-16/expenses/{expense_id}", data=values).status_code == 404
    assert client.post(f"/days/2024-02-16/expenses/{expense_id}/delete", data={"csrf": token}).status_code == 404
    assert database_rows("SELECT * FROM expenses")[0]["day_date"] == DAY


def test_delayed_payments_keep_sale_day_separate_from_cash_date(client):
    token = login(client)
    create_day(client, token)
    assert client.post(f"/days/{DAY}/save", data=saveday_payload(token)).status_code == 303
    for paid_on, amount in [(DAY, "1000"), ("2024-02-20", "500")]:
        response = client.post(f"/days/{DAY}/payments", data={"csrf": token, "paid_on": paid_on, "amount": amount, "note": "Tahsilat"})
        assert response.status_code == 303
    page = client.get(f"/days/{DAY}")
    assert page.context["result"]["revenue_cents"] == 400000
    assert page.context["collected_cents"] == 150000
    assert page.context["receivable_cents"] == 250000
    assert database_rows("SELECT sum(amount_cents) AS total FROM payments WHERE paid_on=?", (DAY,))[0]["total"] == 100000
    assert client.post(f"/days/{DAY}/payments", data={"csrf": token, "paid_on": DAY, "amount": "0"}).status_code == 422


def test_admin_report_cash_dates_fixed_gaps_and_weekly_totals(client):
    token = login(client, "admin")
    create_day(client, token)
    assert client.post(f"/days/{DAY}/save", data=saveday_payload(token)).status_code == 303
    assert client.post(f"/days/{DAY}/expenses", data={"csrf": token, "category": "material", "name": "Malzeme", "amount": "2000"}).status_code == 303
    assert client.post("/admin/fixed", data={"csrf": token, "month": "2024-02", "name": "Kira", "amount": "2900"}).status_code == 303
    for paid_on, amount in [(DAY, "1000"), ("2024-02-20", "500")]:
        assert client.post(f"/days/{DAY}/payments", data={"csrf": token, "paid_on": paid_on, "amount": amount}).status_code == 303

    initial = client.get("/admin", params={"start": DAY, "end": DAY})
    assert initial.status_code == 200
    assert initial.context["totals"]["revenue_cents"] == 400000
    assert initial.context["totals"]["collected_cents"] == 100000
    assert initial.context["totals"]["receivable_cents"] == 300000

    report = client.get("/admin", params={"start": DAY, "end": "2024-02-20"})
    assert report.status_code == 200
    totals = report.context["totals"]
    assert totals["revenue_cents"] == 400000
    assert totals["collected_cents"] == 150000
    assert totals["receivable_cents"] == 250000
    assert totals["fixed_cents"] == 60000
    assert totals["result_cents"] == 140000
    assert len(report.context["missing_days"]) == 5
    assert report.context["unrecorded_fixed_cents"] == 50000
    assert sum(week["result_cents"] for week in report.context["weeks"]) == totals["result_cents"]

    cash_day = client.get("/admin", params={"start": "2024-02-20", "end": "2024-02-20"})
    assert cash_day.context["totals"]["revenue_cents"] == 0
    assert cash_day.context["totals"]["collected_cents"] == 50000
    assert cash_day.context["totals"]["result_cents"] == -10000


def test_admin_settings_scenario_and_csv_formula_protection(client):
    token = login(client, "admin")
    create_day(client, token)
    assert client.post(f"/days/{DAY}/save", data=saveday_payload(token, menu="=HYPERLINK(\"https://example.invalid\")")).status_code == 303
    assert client.post(f"/days/{DAY}/expenses", data={"csrf": token, "category": "material", "name": "Malzeme", "amount": "2000"}).status_code == 303
    assert client.post("/admin/settings", data={"csrf": token, "labor_hourly": "100", "stove_hourly": "10", "oven_hourly": "20"}).status_code == 303
    settings = database_rows("SELECT * FROM settings")[0]
    assert settings["labor_hourly_cents"] == 10000
    assert database_rows("SELECT * FROM audit WHERE entity='settings'")[-1]["username"] == ADMIN_USER

    response = client.get("/admin/scenario", params={"date": DAY, "portions": "20", "unit_price": "300"})
    assert response.status_code == 200
    scenario_result = response.context["scenario_result"]
    assert scenario_result["revenue_cents"] == 600000
    assert scenario_result["total_cost_cents"] == 235000
    assert scenario_result["result_cents"] == 365000
    assert client.get("/admin/scenario", params={"date": DAY, "portions": "0", "unit_price": "300"}).status_code == 422

    export = client.get("/admin/export.csv", params={"start": DAY, "end": DAY})
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment" in export.headers["content-disposition"]
    records = list(csv.reader(io.StringIO(export.text.lstrip("\ufeff")), delimiter=";"))
    assert records[1][2].startswith("'=HYPERLINK")
    assert records[1][4] == "4000,00"
    assert records[1][16] == "1650,00"


def test_manual_timer_rejects_negative_duration_overlap_and_over_24_hours(client):
    token = login(client)
    create_day(client, token)
    values = {"csrf": token, "kind": "work", "started_at": DAY + "T10:00", "ended_at": DAY + "T11:00"}
    assert client.post(f"/days/{DAY}/timers", data=values).status_code == 303
    for start, end, expected in [
        ("2024-02-15T12:00", "2024-02-15T11:00", 422),
        ("2024-02-15T10:30", "2024-02-15T11:30", 409),
        ("2024-02-15T09:00", "2024-02-16T10:00", 422),
    ]:
        values.update(started_at=start, ended_at=end)
        assert client.post(f"/days/{DAY}/timers", data=values).status_code == expected
    assert len(database_rows("SELECT * FROM timers")) == 1


def test_timer_overlap_is_checked_across_adjacent_days(client):
    token = login(client)
    create_day(client, token)
    create_day(client, token, "2024-02-16")
    first = {"csrf": token, "kind": "oven", "started_at": DAY + "T23:30", "ended_at": "2024-02-16T00:30"}
    assert client.post(f"/days/{DAY}/timers", data=first).status_code == 303
    second = {"csrf": token, "kind": "oven", "started_at": "2024-02-16T00:00", "ended_at": "2024-02-16T01:00"}
    assert client.post("/days/2024-02-16/timers", data=second).status_code == 409


def test_quick_timer_start_stop_and_duplicate_start(client):
    from app.db import TZ
    today = datetime.now(TZ).date().isoformat()
    token = login(client)
    create_day(client, token, today)
    values = {"csrf": token, "kind": "work", "action": "start"}
    assert client.post(f"/days/{today}/timer", data=values).status_code == 303
    assert client.post(f"/days/{today}/timer", data=values).status_code == 409
    values["action"] = "stop"
    assert client.post(f"/days/{today}/timer", data=values).status_code == 303
    saved = database_rows("SELECT * FROM timers")[0]
    assert saved["ended_at"] > saved["started_at"]
    assert client.post(f"/days/{today}/timer", data=values).status_code == 409


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (20, 30), (180, 220, 140)).save(output, format="PNG")
    return output.getvalue()


def test_receipt_is_normalized_protected_and_filename_cannot_escape_data_dir(client, env):
    token = login(client)
    create_day(client, token)
    response = client.post(f"/days/{DAY}/receipts", data={"csrf": token}, files={"photo": ("../../outside.php", png_bytes(), "image/png")})
    assert response.status_code == 303
    item = database_rows("SELECT * FROM receipts")[0]
    assert re.fullmatch(r"[a-f0-9]{48}\.jpg", item["filename"])
    assert item["original_name"] == "outside.php"
    assert not (env.parent / "outside.php").exists()
    assert (env / "receipts" / item["filename"]).is_file()
    image_response = client.get(f"/receipts/{item['id']}")
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(image_response.content)).format == "JPEG"
    assert image_response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert client.get(f"/receipts/{item['id']}").status_code == 303


def test_receipt_rejects_fake_image_wrong_mime_and_oversize(client):
    token = login(client)
    create_day(client, token)
    for content, content_type, expected in [
        (b"<script>not a picture</script>", "image/jpeg", 422),
        (png_bytes(), "text/plain", 422),
        (b"x" * (8 * 1024 * 1024 + 1), "image/jpeg", 413),
    ]:
        response = client.post(f"/days/{DAY}/receipts", data={"csrf": token}, files={"photo": ("fis.jpg", content, content_type)})
        assert response.status_code == expected
    assert database_rows("SELECT * FROM receipts") == []


def test_logout_revokes_session_and_rejects_csrf(client):
    token = login(client)
    cookie = client.cookies.get("emine_session")
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.get("/").status_code == 200
    assert client.post("/logout", data={"csrf": token}).status_code == 303
    assert client.get("/").status_code == 303
    client.cookies.set("emine_session", cookie)
    assert client.get("/").status_code == 303


def test_login_rate_limit_applies_to_existing_and_unknown_accounts(client):
    token = csrf(client.get("/login"))
    for attempt in range(8):
        response = client.post("/login", data={"csrf": token, "username": EMINE_USER, "password": "wrong-password"})
        assert response.status_code == 401
    rejected = client.post("/login", data={"csrf": token, "username": EMINE_USER, "password": EMINE_PASSWORD})
    assert rejected.status_code == 429
    # No account enumeration through a different limit for a nonexistent account.
    for attempt in range(8):
        response = client.post("/login", data={"csrf": token, "username": "missing-account", "password": "wrong-password"})
        assert response.status_code == 401
    assert client.post("/login", data={"csrf": token, "username": "missing-account", "password": "wrong-password"}).status_code == 429


def test_untrusted_host_is_rejected(client):
    assert client.get("/login", headers={"Host": "attacker.example"}).status_code == 400


@pytest.mark.parametrize("overrides", [
    {"COOKIE_SECURE": "false"}, {"ALLOWED_HOSTS": "*"}, {"ALLOWED_HOSTS": " * "},
    {"ALLOWED_HOSTS": ""}, {"SECRET_KEY": "short"},
    {"ADMIN_PASSWORD": "changeme12345"}, {"EMINE_PASSWORD": "short"},
])
def test_unsafe_production_configuration_is_rejected(env, monkeypatch, overrides):
    from app.security import validate_config
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("ALLOWED_HOSTS", "abla.umitozkan.com.tr")
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        validate_config()


def test_expired_session_cannot_open_authenticated_page(client):
    from app.db import connect
    token = login(client)
    assert token
    with connect(write=True) as con:
        con.execute("UPDATE sessions SET expires_at=0")
    assert client.get("/").status_code == 303
