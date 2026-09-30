"""Expense MCP server — FastMCP + Supabase (Postgres), API-key protected.

Env vars:
    DATABASE_URL   Supabase pooler connection string (required)
    MCP_API_KEY    secret the Streamlit app must send as a Bearer token (required)
    TZ_NAME        your timezone, default Asia/Kolkata (used for "today")
    PORT           set automatically by Render
"""

import hmac
import os
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.responses import PlainTextResponse
from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
MCP_API_KEY = os.environ.get("MCP_API_KEY")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set.")
if not MCP_API_KEY:
    raise RuntimeError("MCP_API_KEY is not set (refusing to start an open server).")


# ---------- Auth: only callers with the secret key may use the tools ----------

class ApiKeyVerifier(TokenVerifier):
    def __init__(self, key: str):
        super().__init__()
        self._key = key

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._key.encode()):
            return AccessToken(token=token, client_id="expense-app", scopes=[])
        return None


mcp = FastMCP(name="expense-tracker", auth=ApiKeyVerifier(MCP_API_KEY))


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return PlainTextResponse("ok")


# ---------- Database ----------

def get_conn():
    # prepare_threshold=None keeps it compatible with Supabase's pooler (pgbouncer)
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id          BIGSERIAL PRIMARY KEY,
                amount      NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                category    TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                date        DATE NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_date ON expenses(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_cat ON expenses(category)")
        # Block Supabase's public REST API from touching this table.
        # (Our server connects as the postgres role, which bypasses RLS.)
        conn.execute("ALTER TABLE expenses ENABLE ROW LEVEL SECURITY")


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _validate_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD format.")


def _num(x) -> float | int:
    x = float(x)
    return int(x) if x.is_integer() else round(x, 2)


def _row(r: dict) -> dict:
    return {**r, "amount": _num(r["amount"])}


_COLS = "id, amount, category, description, date::text AS date"

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
    d = _validate_date(expense_date) if expense_date else _today()
    with get_conn() as conn:
        r = conn.execute(
            f"INSERT INTO expenses (amount, category, description, date) "
            f"VALUES (%s, %s, %s, %s) RETURNING {_COLS}",
            (amount, category.strip().lower(), description.strip(), d),
        ).fetchone()
    return {"status": "added", **_row(r)}


@mcp.tool()
def list_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """List expenses with optional filters."""
    query, params = f"SELECT {_COLS} FROM expenses WHERE TRUE", []
    if start_date:
        query += " AND date >= %s"
        params.append(_validate_date(start_date))
    if end_date:
        query += " AND date <= %s"
        params.append(_validate_date(end_date))
    if category:
        query += " AND category = %s"
        params.append(category.strip().lower())
    query += " ORDER BY date DESC, id DESC LIMIT %s"
    params.append(max(1, min(limit, 50)))
    with get_conn() as conn:
        return [_row(r) for r in conn.execute(query, params).fetchall()]


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
        fields.append("amount = %s")
        params.append(amount)
    if category is not None:
        fields.append("category = %s")
        params.append(category.strip().lower())
    if description is not None:
        fields.append("description = %s")
        params.append(description.strip())
    if expense_date is not None:
        fields.append("date = %s")
        params.append(_validate_date(expense_date))
    if not fields:
        raise ValueError("Nothing to update. Provide at least one field.")
    params.append(expense_id)
    with get_conn() as conn:
        r = conn.execute(
            f"UPDATE expenses SET {', '.join(fields)} WHERE id = %s RETURNING {_COLS}", params
        ).fetchone()
    if r is None:
        raise ValueError(f"No expense found with id {expense_id}.")
    return {"status": "updated", **_row(r)}


@mcp.tool()
def delete_expense(expense_id: int) -> dict:
    """Delete an expense by id."""
    with get_conn() as conn:
        r = conn.execute(
            f"DELETE FROM expenses WHERE id = %s RETURNING {_COLS}", (expense_id,)
        ).fetchone()
    if r is None:
        raise ValueError(f"No expense found with id {expense_id}.")
    return {"status": "deleted", **_row(r)}


def _summary(where: str, params: list) -> dict:
    with get_conn() as conn:
        t = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM expenses {where}", params
        ).fetchone()
        rows = conn.execute(
            f"SELECT category, SUM(amount) AS total, COUNT(*) AS count "
            f"FROM expenses {where} GROUP BY category ORDER BY total DESC",
            params,
        ).fetchall()
    return {
        "total": _num(t["total"]),
        "count": t["n"],
        "by_category": [{"category": r["category"], "total": _num(r["total"]), "count": r["count"]} for r in rows],
    }


@mcp.tool()
def summarize_expenses(start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
    """Return total spending and category totals."""
    where, params = "WHERE TRUE", []
    if start_date:
        where += " AND date >= %s"
        params.append(_validate_date(start_date))
    if end_date:
        where += " AND date <= %s"
        params.append(_validate_date(end_date))
    return {"start_date": start_date, "end_date": end_date, **_summary(where, params)}


@mcp.tool()
def monthly_report(year: int, month: int) -> dict:
    """Return spending totals for one month."""
    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return {"year": year, "month": month, **_summary("WHERE date >= %s AND date < %s", [start, end])}


@mcp.resource("expenses://categories")
def categories() -> list[str]:
    """Return categories used so far."""
    with get_conn() as conn:
        return [r["category"] for r in conn.execute("SELECT DISTINCT category FROM expenses ORDER BY category")]


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))