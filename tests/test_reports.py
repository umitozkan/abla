"""Integration checks for report periods, estimation and exported values."""
import csv
import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def reporting(tmp_path, monkeypatch):
    for key, value in {
        "DATA_DIR": str(tmp_path),
        "APP_ENV": "test",
        "SECRET_KEY": "report-test-session-key-32-characters-minimum",
        "ADMIN_USERNAME": "report_admin",
        "ADMIN_PASSWORD": "report-admin-password-test-only",
        "EMINE_USERNAME": "report_emine",
        "EMINE_PASSWORD": "report-emine-password-test-only",
        "COOKIE_SECURE": "false",
        "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
    }.items():
        monkeypatch.setenv(key, value)
    from app import main, security
    from app.db import connect

    # localhost is allowed by both the Compose config and the test config, even
    # when a different test module has already imported the application.
    with TestClient(main.app, base_url="http://localhost") as client:
        with connect(write=True) as con:
            con.execute("UPDATE settings SET labor_hourly_cents=0,stove_hourly_cents=0,oven_hourly_cents=0")
            admin_id = con.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
        token, _ = security.create_session(admin_id)
        client.cookies.set(security.COOKIE, token)
        yield main, client


def add_day(day_date="2026-09-24", **changes):
    from app.db import connect

    values = {
        "date": day_date, "menu": "Mercimek çorbası", "requested": 15,
        "made": 15, "delivered": 15, "revenue_cents": 400000,
        "prior_material_cents": 0, "carry_material_cents": 0,
        "material_confirmed": 1, "other_confirmed": 1,
        "time_confirmed": 1, "energy_confirmed": 1,
        "labor_source": "tasks", "shopping_minutes": 0,
        "preparation_minutes": 0, "delivery_minutes": 0, "cleaning_minutes": 0,
    }
    values.update(changes)
    with connect(write=True) as con:
        con.execute(
            f"INSERT INTO days({','.join(values)}) VALUES({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


def add_payment(day_date, paid_on, amount):
    from app.db import connect

    with connect(write=True) as con:
        con.execute("INSERT INTO payments(day_date,paid_on,amount_cents) VALUES(?,?,?)", (day_date, paid_on, amount))


def add_expense(day_date, category, amount):
    from app.db import connect

    with connect(write=True) as con:
        con.execute("INSERT INTO expenses(day_date,category,name,amount_cents) VALUES(?,?,?,?)", (day_date, category, "Test kalemi", amount))


def test_cash_uses_payment_date_and_receivables_use_period_end(reporting):
    main, client = reporting
    add_day("2026-09-23", revenue_cents=50000)
    add_day()
    add_payment("2026-09-23", "2026-09-24", 30000)
    add_payment("2026-09-24", "2026-09-23", 20000)
    add_payment("2026-09-24", "2026-09-24", 100000)
    add_payment("2026-09-24", "2026-09-25", 50000)

    report = main.build_report("2026-09-24", "2026-09-24")
    assert report["totals"]["revenue_cents"] == 400000
    assert report["totals"]["collected_cents"] == 130000
    assert report["totals"]["receivable_cents"] == 280000
    assert report["rows"][0]["collected_cents"] == 120000
    assert report["rows"][0]["receivable_cents"] == 280000
    assert main.build_report("2026-09-24", "2026-09-25")["totals"]["receivable_cents"] == 230000
    response = client.get("/admin", params={"start": "2026-09-24", "end": "2026-09-24"})
    assert response.status_code == 200
    assert response.context["totals"]["collected_cents"] == 130000


def test_overpayments_do_not_cancel_another_days_receivable(reporting):
    main, _ = reporting
    add_day("2026-09-23", revenue_cents=10000)
    add_day("2026-09-24", revenue_cents=10000)
    add_payment("2026-09-23", "2026-09-23", 12000)
    add_payment("2026-09-24", "2026-09-24", 3000)
    report = main.build_report("2026-09-23", "2026-09-24")
    assert report["totals"]["receivable_cents"] == 7000
    assert report["totals"]["advance_cents"] == 2000
    assert report["totals"]["collected_cents"] == 15000
    assert next(row for row in report["rows"] if row["day"]["date"] == "2026-09-23")["receivable_cents"] == -2000


@pytest.mark.parametrize("with_record", [True, False])
def test_fixed_costs_cover_every_calendar_day_and_preserve_month_total(reporting, with_record):
    main, _ = reporting
    from app.db import connect

    if with_record:
        add_day()
    with connect(write=True) as con:
        con.execute("INSERT INTO fixed_costs(month,name,amount_cents) VALUES('2026-09','Kira',300001)")
    report = main.build_report("2026-09-01", "2026-09-30")
    assert len(report["missing_days"]) == (29 if with_record else 30)
    assert report["totals"]["fixed_cents"] == 300001
    assert report["totals"]["total_cost_cents"] == 300001
    assert report["unrecorded_fixed_cents"] == (290001 if with_record else 300001)
    assert report["totals"]["result_cents"] == (99999 if with_record else -300001)
    assert sum(week["result_cents"] for week in report["weeks"]) == report["totals"]["result_cents"]
    if with_record:
        assert report["rows"][0]["result"]["fixed_cents"] == 10000
        assert "2026-09-24" not in report["missing_days"]
    # The one remainder kuruş belongs to September 1, even without an entry.
    assert main.build_report("2026-09-01", "2026-09-01")["totals"]["fixed_cents"] == 10001


def test_unknown_sales_still_deduct_known_costs_from_partial_aggregate(reporting):
    main, client = reporting
    from app.db import connect

    add_day("2026-09-23", preparation_minutes=120)
    add_day("2026-09-24", revenue_cents=None, preparation_minutes=60)
    add_expense("2026-09-23", "material", 200000)
    add_expense("2026-09-23", "other", 10000)
    add_expense("2026-09-24", "material", 30000)
    add_expense("2026-09-24", "other", 5000)
    with connect(write=True) as con:
        con.execute("UPDATE settings SET labor_hourly_cents=10000")
    report = main.build_report("2026-09-23", "2026-09-24")
    assert report["totals"]["revenue_cents"] == 400000
    assert report["totals"]["total_cost_cents"] == 275000
    assert report["totals"]["before_labor_cents"] == 155000
    assert report["totals"]["result_cents"] == 125000
    assert report["weeks"][0]["result_cents"] == 125000
    assert report["incomplete_count"] == 1
    assert report["rows"][0]["result"]["result_cents"] is None
    assert report["rows"][0]["result"]["total_cost_cents"] == 45000
    response = client.get("/admin", params={"start": "2026-09-23", "end": "2026-09-24"})
    assert response.status_code == 200
    assert response.context["incomplete_count"] == 1


def test_impossible_material_balance_invalidates_aggregate_and_weekly_profit(reporting):
    main, client = reporting
    add_day("2026-09-23")
    add_expense("2026-09-23", "material", 200000)
    add_day("2026-09-24", revenue_cents=100000, carry_material_cents=10000)
    report = main.build_report("2026-09-23", "2026-09-24")
    assert report["totals"]["revenue_cents"] == 500000
    for key in ("material_cents", "total_cost_cents", "before_labor_cents", "result_cents", "margin_percent", "cost_per_portion_cents", "result_per_portion_cents"):
        assert report["totals"][key] is None, key
    assert report["weeks"][0]["result_cents"] is None
    assert report["incomplete_count"] == 1
    response = client.get("/admin", params={"start": "2026-09-23", "end": "2026-09-24"})
    assert response.status_code == 200


@pytest.mark.parametrize("menu", ["=1+1", "+SUM(A1:A2)", "-2+3", "@SUM(A1:A2)", " \t=1+1", "\r=2", "\n=3"])
def test_csv_protects_user_text_from_spreadsheet_formula_execution(reporting, menu):
    _, client = reporting
    add_day(menu=menu)
    response = client.get("/admin/export.csv", params={"start": "2026-09-24", "end": "2026-09-24"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    records = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert records[1][0] == "Gün"
    assert records[1][2] == "'" + menu
    assert records[1][4] == "4000,00"


def test_scenario_preserves_all_costs_and_does_not_update_the_day(reporting):
    main, client = reporting
    from app.db import connect

    add_day(preparation_minutes=120)
    add_expense("2026-09-24", "material", 200000)
    add_expense("2026-09-24", "other", 10000)
    add_expense("2026-09-24", "equipment", 800000)
    with connect(write=True) as con:
        con.execute("UPDATE settings SET labor_hourly_cents=10000,stove_hourly_cents=1000")
        con.execute("INSERT INTO timers(day_date,kind,started_at,ended_at) VALUES('2026-09-24','stove','2026-09-24T10:00:00+03:00','2026-09-24T11:00:00+03:00')")
        con.execute("INSERT INTO fixed_costs(month,name,amount_cents) VALUES('2026-09','Kira',300000)")
    before = main.build_report("2026-09-24", "2026-09-24")
    costs = before["rows"][0]["result"]["total_cost_cents"]
    assert costs == 241000
    for portions in (10, 30):
        response = client.get("/admin/scenario", params={"date": "2026-09-24", "portions": portions, "unit_price": "300"})
        assert response.status_code == 200
        result = response.context["scenario_result"]
        assert result["total_cost_cents"] == costs
        assert result["revenue_cents"] == portions * 30000
        assert result["result_cents"] == portions * 30000 - costs
        assert result["estimate"] is True
        assert "aynı kalır" in result["assumption"]
    assert main.build_report("2026-09-24", "2026-09-24") == before
