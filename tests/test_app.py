from __future__ import annotations

import os

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

    page = client.get("/payables")
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

    page = client.get("/payables")
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

    page = client.get("/payables").data
    assert page.index(b"Save payable") < page.index(b'name="vendor"')
