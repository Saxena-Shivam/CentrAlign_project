from __future__ import annotations

import sqlite3
from pathlib import Path


DB_DIR = Path(__file__).resolve().parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(db_path) if db_path else DB_DIR / "demo.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database(db_path: str | Path | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor TEXT NOT NULL,
                invoice_number TEXT NOT NULL UNIQUE,
                amount REAL NOT NULL,
                due_date TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        seed_vendors = ["Acme", "Northwind", "Contoso", "Fabrikam"]
        for name in seed_vendors:
            conn.execute("INSERT OR IGNORE INTO vendors (name) VALUES (?)", (name,))

        default_payables = [
            ("Acme", "INV-1042", 1250.00, "2026-10-15"),
            ("Northwind", "INV-2041", 980.50, "2026-11-04"),
        ]
        for vendor, invoice_number, amount, due_date in default_payables:
            conn.execute(
                "INSERT OR IGNORE INTO payables (vendor, invoice_number, amount, due_date) VALUES (?, ?, ?, ?)",
                (vendor, invoice_number, amount, due_date),
            )

        conn.execute(
            "INSERT OR IGNORE INTO activity_log (message) VALUES (?)",
            ("System seeded vendors and initial payables.",),
        )
        conn.commit()
    finally:
        conn.close()
