import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Optional

from fastmcp import FastMCP

DB_PATH = os.environ.get(
    "EXPENSE_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "expenses.db"),
)

mcp = FastMCP(name="expense-tracker")


# ---------- Database helpers ----------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                amount      REAL    NOT NULL CHECK (amount > 0),
                category    TEXT    NOT NULL,
                description TEXT    DEFAULT '',
                date        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_date ON expenses(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_cat ON expenses(category)")


def _validate_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD format.")


init_db()


# ---------- Tools ----------

@mcp.tool()
def add_expense(
    amount: float,
    category: str,
    description: str = "",
    expense_date: Optional[str] = None,
) -> dict:
    """Add an expense."""

    if amount <= 0:
        raise ValueError("Amount must be greater than 0.")

    d = _validate_date(expense_date) if expense_date else date.today().isoformat()
    clean_category = category.strip().lower()
    clean_description = description.strip()

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO expenses (amount, category, description, date) VALUES (?, ?, ?, ?)",
            (amount, clean_category, clean_description, d),
        )

        return {
            "status": "added",
            "id": cur.lastrowid,
            "amount": amount,
            "category": clean_category,
            "description": clean_description,
            "date": d,
        }


@mcp.tool()
def list_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """List expenses with optional filters."""

    query = (
        "SELECT id, amount, category, description, date "
        "FROM expenses WHERE 1=1"
    )
    params: list = []

    if start_date:
        query += " AND date >= ?"
        params.append(_validate_date(start_date))

    if end_date:
        query += " AND date <= ?"
        params.append(_validate_date(end_date))

    if category:
        query += " AND category = ?"
        params.append(category.strip().lower())

    query += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(max(1, min(limit, 50)))

    with get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


@mcp.tool()
def update_expense(
    expense_id: int,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
    expense_date: Optional[str] = None,
) -> dict:
    """Update an existing expense."""

    fields, params = [], []

    if amount is not None:
        if amount <= 0:
            raise ValueError("Amount must be greater than 0.")
        fields.append("amount = ?")
        params.append(amount)

    if category is not None:
        fields.append("category = ?")
        params.append(category.strip().lower())

    if description is not None:
        fields.append("description = ?")
        params.append(description.strip())

    if expense_date is not None:
        fields.append("date = ?")
        params.append(_validate_date(expense_date))

    if not fields:
        raise ValueError("Nothing to update. Provide at least one field.")

    params.append(expense_id)

    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE expenses SET {', '.join(fields)} WHERE id = ?",
            params,
        )

        if cur.rowcount == 0:
            raise ValueError(f"No expense found with id {expense_id}.")

        row = conn.execute(
            "SELECT id, amount, category, description, date "
            "FROM expenses WHERE id = ?",
            (expense_id,),
        ).fetchone()

        return {"status": "updated", **dict(row)}


@mcp.tool()
def delete_expense(expense_id: int) -> dict:
    """Delete an expense by id."""

    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM expenses WHERE id = ?",
            (expense_id,),
        )

        if cur.rowcount == 0:
            raise ValueError(f"No expense found with id {expense_id}.")

        return {"status": "deleted", "id": expense_id}


@mcp.tool()
def summarize_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Return total spending and category totals."""

    where, params = "WHERE 1=1", []

    if start_date:
        where += " AND date >= ?"
        params.append(_validate_date(start_date))

    if end_date:
        where += " AND date <= ?"
        params.append(_validate_date(end_date))

    with get_conn() as conn:
        total = conn.execute(
            f"""
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n
            FROM expenses {where}
            """,
            params,
        ).fetchone()

        rows = conn.execute(
            f"""
            SELECT category,
                   ROUND(SUM(amount), 2) AS total,
                   COUNT(*) AS count
            FROM expenses {where}
            GROUP BY category
            ORDER BY total DESC
            """,
            params,
        ).fetchall()

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total": round(total["total"], 2),
        "count": total["n"],
        "by_category": [dict(r) for r in rows],
    }


@mcp.tool()
def monthly_report(year: int, month: int) -> dict:
    """Return spending totals for one month."""

    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")

    start = f"{year:04d}-{month:02d}-01"
    end = (
        f"{year + 1:04d}-01-01"
        if month == 12
        else f"{year:04d}-{month + 1:02d}-01"
    )

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT category,
                   ROUND(SUM(amount), 2) AS total,
                   COUNT(*) AS count
            FROM expenses
            WHERE date >= ? AND date < ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (start, end),
        ).fetchall()

    total = round(sum(r["total"] for r in rows), 2)

    return {
        "year": year,
        "month": month,
        "total": total,
        "by_category": [dict(r) for r in rows],
    }


# ---------- Resource ----------

@mcp.resource("expenses://categories")
def categories() -> list[str]:
    """Return categories used so far."""

    with get_conn() as conn:
        return [
            r["category"]
            for r in conn.execute(
                "SELECT DISTINCT category "
                "FROM expenses ORDER BY category"
            )
        ]


if __name__ == "__main__":
    mcp.run()