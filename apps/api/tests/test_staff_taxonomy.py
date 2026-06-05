from app.services.staff_taxonomy import categories_for_payroll_role


def test_prep_allows_intern_category_from_current_staff_sheet() -> None:
    assert "intern" in categories_for_payroll_role("prep")
