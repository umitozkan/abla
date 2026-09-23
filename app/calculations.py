"""Pure, integer-kuruş calculations for the kitchen's *estimated* results.

``calculate_day`` accepts dictionaries so that persistence and HTML concerns stay
outside the accounting arithmetic. Unknown costs are provisionally zero and are
always disclosed in ``missing``. An impossible material balance, or an unknown
sales amount, prevents publishing a monetary result altogether. ``complete``
means the requested inputs are complete; it never turns an estimate into net
profit. Purchases and equipment are exposed separately from consumed materials.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ZERO = Decimal(0)
SIXTY = Decimal(60)


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _nonnegative_integer(value: Any) -> int | None:
    """Accept integral input only; never truncate fractions or accept bools."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _timer_minutes(timers: list[dict], kind: str, missing: list[str]) -> Decimal:
    labels = {"work": "İş", "stove": "Ocak", "oven": "Fırın"}
    intervals = []
    for timer in timers:
        if timer.get("kind") != kind:
            continue
        if not timer.get("ended_at"):
            missing.append(f"{labels[kind]} sayacı açık; bitiş saati girilmeli.")
            continue
        try:
            start = datetime.fromisoformat(str(timer.get("started_at")))
            end = datetime.fromisoformat(str(timer["ended_at"]))
            if start.utcoffset() is None or end.utcoffset() is None or end < start:
                raise ValueError("Invalid timer interval")
        except (TypeError, ValueError, OverflowError):
            missing.append(f"{labels[kind]} saat aralığı geçersiz; saatleri düzeltin.")
            continue
        intervals.append((start, end))

    # Overlapping intervals cannot create extra hours. They still require review.
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start < merged[-1][1]:
            missing.append(f"{labels[kind]} saat aralıkları çakışıyor; saatleri düzeltin.")
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    minutes = ZERO
    for start, end in merged:
        elapsed = end - start
        microseconds = (
            (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
        )
        minutes += Decimal(microseconds) / Decimal(60000000)
    return minutes


def calculate_day(
    day: dict,
    expenses: list[dict],
    timers: list[dict],
    settings: dict,
    fixed_costs: list[dict],
) -> dict:
    """Calculate a day's estimate; all monetary inputs and outputs use kuruş.

    Labor uses only ``labor_source`` (``clock`` or ``tasks``), never both.
    ``fixed_costs`` are distributed over calendar days of their YYYY-MM month.
    Remainder kuruş go to the earliest days, preserving the monthly total.
    Per-portion figures use delivered portions, not requested/made portions.
    """
    missing: list[str] = []

    def amount(record: dict, key: str, label: str) -> int | None:
        value = _nonnegative_integer(record.get(key))
        if value is None:
            missing.append(f"{label} girilmemiş veya geçersiz.")
        return value

    try:
        record_date = date.fromisoformat(str(day.get("date")))
    except (ValueError, TypeError):
        record_date = None
        missing.append("Kayıt tarihi geçersiz; sabit gider payı hesaplanamadı.")
    if not str(day.get("menu") or "").strip():
        missing.append("Günlük menü girilmemiş.")

    requested = amount(day, "requested", "İstenen porsiyon sayısı")
    made = amount(day, "made", "Yapılan porsiyon sayısı")
    delivered = amount(day, "delivered", "Teslim edilen porsiyon sayısı")
    if made is not None and delivered is not None and delivered > made:
        missing.append("Teslim edilen porsiyon, yapılan porsiyondan fazla; kontrol edin.")
    revenue = amount(day, "revenue_cents", "O gün kazanılan satış geliri")

    expense_totals = {"material": 0, "other": 0, "equipment": 0}
    for expense in expenses:
        category = expense.get("category")
        if category not in expense_totals:
            missing.append("Sınıflandırılmamış gider var; gider türünü düzeltin.")
            continue
        value = amount(expense, "amount_cents", "Gider tutarı")
        expense_totals[category] += value or 0
    purchases = expense_totals["material"]
    prior = amount(day, "prior_material_cents", "Önceki günlerden kullanılan malzeme tutarı")
    carry = amount(day, "carry_material_cents", "Sonraya kalan malzeme tutarı")
    material: int | None = purchases + (prior or 0) - (carry or 0)
    if material < 0:
        missing.append("Sonraya kalan malzeme tutarı, alışveriş ve önceki günden kullanımı aşıyor.")
        material = None
    other = expense_totals["other"]

    for key, label in (
        ("material_confirmed", "Malzeme alışverişi ve kullanılan/kalan tutarlar"),
        ("other_confirmed", "Diğer giderler"),
        ("time_confirmed", "Emek süreleri"),
        ("energy_confirmed", "Ocak ve fırın kullanım süreleri"),
    ):
        if not day.get(key):
            missing.append(f"{label} tamamlandı olarak işaretlenmemiş.")

    # Validate work timers even in tasks mode, but never count them twice.
    work_minutes = _timer_minutes(timers, "work", missing)
    source = day.get("labor_source", "clock")
    if source == "tasks":
        labor_minutes = ZERO
        for key, label in (
            ("shopping_minutes", "Alışveriş süresi"),
            ("preparation_minutes", "Hazırlık süresi"),
            ("delivery_minutes", "Teslimat süresi"),
            ("cleaning_minutes", "Temizlik süresi"),
        ):
            labor_minutes += Decimal(amount(day, key, label) or 0)
    elif source == "clock":
        labor_minutes = work_minutes
        if not any(timer.get("kind") == "work" for timer in timers):
            missing.append("İşe başlama ve bitiş saatleri girilmemiş.")
    else:
        labor_minutes = ZERO
        missing.append("Emek süresi kaynağı geçersiz.")

    labor_rate = amount(settings, "labor_hourly_cents", "Saatlik emek bedeli")
    labor = _round(labor_minutes * Decimal(labor_rate or 0) / SIXTY)
    stove_minutes = _timer_minutes(timers, "stove", missing)
    oven_minutes = _timer_minutes(timers, "oven", missing)
    energy = 0
    for kind, minutes, label in (
        ("stove", stove_minutes, "Ocak"),
        ("oven", oven_minutes, "Fırın"),
    ):
        rate = _nonnegative_integer(settings.get(f"{kind}_hourly_cents"))
        if minutes and rate is None:
            missing.append(f"{label} saatlik tahmini enerji maliyeti girilmemiş veya geçersiz.")
        energy += _round(minutes * Decimal(rate or 0) / SIXTY)

    fixed = 0
    if record_date is not None:
        month = record_date.strftime("%Y-%m")
        days_in_month = calendar.monthrange(record_date.year, record_date.month)[1]
        for cost in fixed_costs:
            if cost.get("month") != month:
                continue
            value = amount(cost, "amount_cents", "Aylık sabit gider tutarı")
            base, remainder = divmod(value or 0, days_in_month)
            fixed += base + int(record_date.day <= remainder)

    operating_cost = None if material is None else material + other + energy + fixed
    total_cost = None if operating_cost is None else operating_cost + labor
    before_labor = (
        None if revenue is None or operating_cost is None else revenue - operating_cost
    )
    result = None if revenue is None or total_cost is None else revenue - total_cost
    if delivered == 0:
        missing.append("Teslim edilen porsiyon sıfır; porsiyon başına tutarlar hesaplanamadı.")
    if revenue == 0:
        missing.append("Satış geliri sıfır; kâr marjı hesaplanamadı.")

    def per_portion(value: int | None) -> int | None:
        return None if value is None or not delivered else _round(Decimal(value) / delivered)

    return {
        "purchases_cents": purchases,
        "prior_material_cents": prior,
        "carry_material_cents": carry,
        "material_cents": material,
        "other_cents": other,
        "equipment_cents": expense_totals["equipment"],
        "revenue_cents": revenue,
        "before_labor_other_cents": None if revenue is None else revenue - purchases,
        "gross_cents": None if revenue is None or material is None else revenue - material,
        "labor_minutes": _number(labor_minutes),
        "labor_cents": labor,
        "stove_minutes": _number(stove_minutes),
        "oven_minutes": _number(oven_minutes),
        "energy_cents": energy,
        "fixed_cents": fixed,
        "operating_cost_cents": operating_cost,
        "total_cost_cents": total_cost,
        "before_labor_cents": before_labor,
        "result_cents": result,
        "margin_percent": (
            None if result is None or not revenue
            else float((Decimal(result) * 100 / revenue).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        ),
        "price_per_portion_cents": per_portion(revenue),
        "cost_per_portion_cents": per_portion(total_cost),
        "result_per_portion_cents": per_portion(result),
        "requested": requested,
        "made": made,
        "delivered": delivered,
        "missing": list(dict.fromkeys(missing)),
        "complete": not missing,
        "estimate": True,
        "fixed_allocation_method": (
            "Aylık sabit gider / ayın takvim günü sayısı. Kuruş farkları ayın ilk günlerine "
            "dağıtılır; kayıtsız günlerin payı diğer günlere aktarılmaz."
        ),
        "formulae": [
            "Emek ve diğer giderler öncesi alışveriş farkı = satış geliri − malzeme alışverişi.",
            "Tüketilen malzeme tahmini = alışveriş + önceki günlerden kullanılan − sonraya kalan.",
            "Emek bedeli = seçilen emek süresi (saat) × saatlik emek bedeli.",
            "Enerji tahmini = ocak saati × ocak saatlik tahmini maliyeti + fırın saati × fırın saatlik tahmini maliyeti. Sayaç ölçümü değildir.",
            "Emek bedeli hariç kalan = satış geliri − tüketilen malzeme − diğer giderler − enerji tahmini − sabit gider payı.",
            "Emek bedeli dâhil tahmini sonuç = emek bedeli hariç kalan − emek bedeli.",
            "Tahmini kâr marjı (%) = tahmini sonuç / satış geliri × 100.",
            "Porsiyon başına satış / maliyet / sonuç = ilgili tutar / teslim edilen porsiyon.",
            "Ekipman alımı bu sonuçtan ayrı gösterilir; günlük malzeme giderine katılmaz.",
            "Tahsilat satış gelirini değiştirmez; alacak = satış geliri − bu satış için yapılan tahsilatlar.",
        ],
    }


def scenario(result: dict, portions: int, unit_price_cents: int) -> dict:
    """Change revenue only; the existing day's total costs remain fixed.

    This deliberately simple MVP scenario is not a prediction of the additional
    food, energy or labor a greater number of portions would require.
    """
    count = _nonnegative_integer(portions)
    price = _nonnegative_integer(unit_price_cents)
    if count is None or count == 0 or price is None:
        raise ValueError("Porsiyon pozitif tam sayı, kişi başı fiyat sıfır veya pozitif olmalı.")
    revenue = count * price
    total_cost = result.get("total_cost_cents")
    estimated_result = None if total_cost is None else revenue - total_cost
    return {
        "portions": count,
        "unit_price_cents": price,
        "revenue_cents": revenue,
        "total_cost_cents": total_cost,
        "result_cents": estimated_result,
        "margin_percent": (
            None if estimated_result is None or not revenue
            else float((Decimal(estimated_result) * 100 / revenue).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        ),
        "cost_per_portion_cents": None if total_cost is None else _round(Decimal(total_cost) / count),
        "result_per_portion_cents": None if estimated_result is None else _round(Decimal(estimated_result) / count),
        "missing": list(result.get("missing", [])),
        "complete": result.get("complete", False),
        "estimate": True,
        "assumption": "Günün tüm maliyetleri aynı kalır; yalnız porsiyon sayısı × kişi başı fiyat ile satış geliri değişir. Ek malzeme, emek ve enerji ihtiyacı modellenmez.",
    }
