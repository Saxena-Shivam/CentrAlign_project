from __future__ import annotations

import os
import sqlite3

from company_app.app import create_app


def test_login_and_payable_add(monkeypatch, tmp_path):
    db_path = tmp_path / "test_app.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    app = create_app()
    client = app.test_client()

    response = client.post("/login", data={"username": "demo", "password": "demo"}, follow_redirects=False)
    assert response.status_code == 302

    response = client.post(
        "/payables",
        data={
            "vendor": "Acme",
            "invoice_number": "INV-5001-unique",
            "amount": "240.75",
            "due_date": "2026-11-30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_validation_requires_unique_invoice(monkeypatch, tmp_path):
    db_path = tmp_path / "test_validation.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "demo", "password": "demo"}, follow_redirects=False)

    response = client.post(
        "/payables",
        data={
            "vendor": "Acme",
            "invoice_number": "INV-1042",
            "amount": "120",
            "due_date": "2026-10-10",
        },
        follow_redirects=True,
    )
    assert b"Invoice number must be unique" in response.data


def test_chaos_mode_first_submit_fails(monkeypatch, tmp_path):
    db_path = tmp_path / "test_chaos.db"
    monkeypatch.setenv("CHAOS_MODE", "first_submit_fails")
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "demo", "password": "demo"}, follow_redirects=False)

    response = client.post(
        "/payables",
        data={
            "vendor": "Acme",
            "invoice_number": "INV-5010",
            "amount": "10",
            "due_date": "2026-12-01",
        },
        follow_redirects=True,
    )
    assert b"first submission fails on purpose" in response.data


def test_chaos_mode_renames_visible_fields_and_accepts_submission(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_MODE", "rename_field")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "renamed_fields.db"))
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "demo", "password": "demo"})

    page = client.get("/payables/new")
    assert b'name="vendor_choice"' in page.data
    assert b'name="invoice_reference"' in page.data
    assert b'name="payable_total"' in page.data
    assert b'name="payment_deadline"' in page.data

    response = client.post(
        "/payables",
        data={
            "vendor_choice": "Acme",
            "invoice_reference": "INV-5011",
            "payable_total": "10",
            "payment_deadline": "2026-12-01",
        },
    )
    assert response.status_code == 302


def test_chaos_mode_extra_required_field_is_visible_and_enforced(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_MODE", "extra_required")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "extra_required.db"))
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "demo", "password": "demo"})

    page = client.get("/payables/new")
    assert b'name="approval_code"' in page.data

    response = client.post(
        "/payables",
        data={
            "vendor": "Acme",
            "invoice_number": "INV-5012",
            "amount": "10",
            "due_date": "2026-12-01",
        },
        follow_redirects=True,
    )
    assert b"Approval code is required" in response.data
    assert b'value="INV-5012"' in response.data


def test_chaos_mode_moves_submit_button_before_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_MODE", "move_submit")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "move_submit.db"))
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "demo", "password": "demo"})

    page = client.get("/payables/new").data
    assert page.index(b"Save payable") < page.index(b'name="vendor"')


def test_vendor_text_input_validates_known_vendors(monkeypatch, tmp_path):
    db_path = tmp_path / "vendor_text.db"
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"username": "demo", "password": "demo"})

    form = client.get("/payables/new")
    assert b'id="vendor"' in form.data
    assert b'name="vendor"' in form.data
    assert b'type="text"' in form.data
    assert b'aria-describedby="vendor-help"' in form.data
    assert b"Acme, Contoso, Fabrikam, Northwind" in form.data
    assert b"<select" not in form.data

    invalid = client.post(
        "/payables",
        data={
            "vendor": "Unknown Company",
            "invoice_number": "INV-5510",
            "amount": "25.00",
            "due_date": "2026-12-01",
        },
    )
    assert invalid.status_code == 400
    assert b"Vendor must be one of the listed vendors." in invalid.data
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM payables WHERE invoice_number = ?", ("INV-5510",)
        ).fetchone() is None


def test_multi_page_navigation_requires_login_and_renders_reference_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "navigation.db"))
    app = create_app()
    client = app.test_client()

    for path in ("/payables", "/payables/new", "/vendors", "/activity"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login")

    client.post("/login", data={"username": "demo", "password": "demo"})
    payables = client.get("/payables")
    vendors = client.get("/vendors")
    activity = client.get("/activity")

    assert payables.status_code == 200 and b"All payables" in payables.data
    assert b"Add payable" in payables.data
    assert vendors.status_code == 200 and b"Approved vendors" in vendors.data
    assert b"Northwind" in vendors.data
    assert activity.status_code == 200 and b"Recent activity" in activity.data
