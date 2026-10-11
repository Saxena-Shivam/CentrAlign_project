from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for

from company_app.database import get_connection, initialize_database


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "centralign-demo-secret")

    app_root = Path(__file__).resolve().parent
    db_path = Path(os.getenv("EVAL_DB_PATH", str(app_root / "data" / "demo.db"))).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_database(db_path)

    app.config["DB_PATH"] = db_path

    def get_db():
        return get_connection(db_path)

    def get_vendors():
        conn = get_db()
        try:
            rows = conn.execute("SELECT name FROM vendors ORDER BY name").fetchall()
            return [row["name"] for row in rows]
        finally:
            conn.close()

    def log_activity(message: str) -> None:
        conn = get_db()
        try:
            conn.execute("INSERT INTO activity_log (message) VALUES (?)", (message,))
            conn.commit()
        finally:
            conn.close()

    def field_names() -> dict[str, str]:
        if os.getenv("CHAOS_MODE", "") == "rename_field":
            return {
                "vendor": "vendor_choice",
                "invoice_number": "invoice_reference",
                "amount": "payable_total",
                "due_date": "payment_deadline",
                "approval_code": "approval_code",
            }
        return {
            "vendor": "vendor",
            "invoice_number": "invoice_number",
            "amount": "amount",
            "due_date": "due_date",
            "approval_code": "approval_code",
        }

    def form_state(
        vendor: str = "",
        invoice_number: str = "",
        amount: str = "",
        due_date: str = "",
        approval_code: str = "",
        first_submit_fails: bool = False,
    ) -> dict:
        chaos_mode = os.getenv("CHAOS_MODE", "")
        return {
            "vendor": vendor,
            "invoice_number": invoice_number,
            "amount": amount,
            "due_date": due_date,
            "approval_code": approval_code,
            "field_names": field_names(),
            "show_extra_required": chaos_mode == "extra_required",
            "move_submit": chaos_mode == "move_submit",
            "first_submit_fails": first_submit_fails,
            "rename_field": chaos_mode == "rename_field",
        }

    @app.get("/")
    def index():
        if "username" not in session:
            return redirect(url_for("login"))
        return redirect(url_for("payables"))

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        expected_user = os.getenv("DEMO_USERNAME", "demo")
        expected_password = os.getenv("DEMO_PASSWORD", "demo")
        if username == expected_user and password == expected_password:
            session["username"] = username
            log_activity(f"User {username} logged in.")
            return redirect(url_for("payables"))
        flash("Invalid username or password.")
        return render_template("login.html")

    @app.get("/payables")
    def payables():
        if "username" not in session:
            return redirect(url_for("login"))

        conn = get_db()
        try:
            payables = conn.execute(
                "SELECT vendor, invoice_number, amount, due_date FROM payables ORDER BY due_date ASC"
            ).fetchall()
            logs = conn.execute("SELECT message, created_at FROM activity_log ORDER BY id DESC LIMIT 12").fetchall()
        finally:
            conn.close()

        form = form_state()
        return render_template("payables.html", payables=payables, logs=logs, vendors=get_vendors(), form_state=form)

    @app.post("/payables")
    def add_payable():
        if "username" not in session:
            return redirect(url_for("login"))

        names = field_names()
        vendor = request.form.get(names["vendor"], "").strip()
        invoice_number = request.form.get(names["invoice_number"], "").strip()
        amount = request.form.get(names["amount"], "").strip()
        due_date = request.form.get(names["due_date"], "").strip()
        approval_code = request.form.get(names["approval_code"], "").strip()
        chaos_mode = os.getenv("CHAOS_MODE", "")
        first_submit_fails = chaos_mode == "first_submit_fails"

        if first_submit_fails:
            if not session.get("first_submit_failed"):
                session["first_submit_failed"] = True
                flash("The first submission fails on purpose to test recovery flow.")
                return render_template(
                    "payables.html",
                    vendors=get_vendors(),
                    payables=_fetch_payables(),
                    logs=_fetch_logs(),
                    form_state=form_state(
                        vendor, invoice_number, amount, due_date, approval_code, first_submit_fails=True
                    ),
                )

        if chaos_mode == "extra_required" and not approval_code:
            flash("Approval code is required in chaos mode.")
            return render_template(
                "payables.html",
                vendors=get_vendors(),
                payables=_fetch_payables(),
                logs=_fetch_logs(),
                form_state=form_state(vendor, invoice_number, amount, due_date, approval_code),
            )

        errors = []
        if not vendor or vendor not in get_vendors():
            errors.append("Vendor must be one of the listed vendors.")
        if not invoice_number:
            errors.append("Invoice number is required.")
        try:
            amount_value = float(amount)
            if amount_value <= 0:
                errors.append("Amount must be a positive number.")
        except ValueError:
            errors.append("Amount must be a valid positive number.")

        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            errors.append("Due date must be a real date in YYYY-MM-DD format.")

        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT 1 FROM payables WHERE invoice_number = ?", (invoice_number,)
            ).fetchone()
            if existing:
                errors.append("Invoice number must be unique across payables.")
        finally:
            conn.close()

        if errors:
            for error in errors:
                flash(error)
            return render_template(
                "payables.html",
                vendors=get_vendors(),
                payables=_fetch_payables(),
                logs=_fetch_logs(),
                form_state=form_state(vendor, invoice_number, amount, due_date, approval_code),
            )

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO payables (vendor, invoice_number, amount, due_date) VALUES (?, ?, ?, ?)",
                (vendor, invoice_number, float(amount), due_date),
            )
            conn.commit()
        finally:
            conn.close()
        log_activity(f"Created payable {invoice_number} for {vendor}.")
        flash("Payable added successfully.")
        return redirect(url_for("payables"))

    def _fetch_payables():
        conn = get_db()
        try:
            return conn.execute("SELECT vendor, invoice_number, amount, due_date FROM payables ORDER BY due_date ASC").fetchall()
        finally:
            conn.close()

    def _fetch_logs():
        conn = get_db()
        try:
            return conn.execute("SELECT message, created_at FROM activity_log ORDER BY id DESC LIMIT 12").fetchall()
        finally:
            conn.close()

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "3000"))
    print(f"Starting CentrAlign mock company app at http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
