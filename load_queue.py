"""Load the checked payment table into PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE.

Follows "Payment Instruction Queue Table Definition (as provisioned by Data Platform).sql"
(payment_instruction_queue_ddl.sql): same columns, types and NOT NULL flags; INSERT and
SELECT only; no other tables, stages or schemas are created.

Credentials come from SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD in .env.

Every row is validated against the DDL's allowed values before anything is written.
If any check fails, the script reports every problem and inserts nothing.

Usage:
    .venv/bin/python load_queue.py            # validate, then insert
    .venv/bin/python load_queue.py --dry-run  # validate only, no connection
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

CHECKED = "payments_checked.csv"
EMAILS = "Client Payment Emails, Shared Operations Inbox Export (Sep–Nov 2026).csv"
TABLE = "PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE"
ROLE, WAREHOUSE, DATABASE, SCHEMA = "PAYMENTS_OPS_LOADER", "PAYMENTS_OPS_WH", "PAYMENTS_OPS", "PAYMENT_QUEUE"
EXPECTED_ROWS = 300

# Column order and allowed values exactly as in the DDL.
COLUMNS = [
    "EMAIL_ID", "CLIENT_ID", "PAYEE_NAME", "PAYMENT_DATE", "AMOUNT", "CURRENCY",
    "ACCOUNT_NUMBER", "BANK_DETAIL_CHANGE", "IS_FORWARDED_THREAD", "EXTRACTION_STATUS",
    "DISPOSITION", "HOLD_REASON", "GE_VALIDATION_PASSED", "LOAD_RUN_ID", "LOADED_AT",
]
CURRENCIES = {"USD", "EUR", "GBP", "CAD", "CHF", "JPY"}
EXTRACTION_STATUSES = {"ok", "unusable_output"}
DISPOSITIONS = {"held", "ready"}
HOLD_REASONS = {"bank_detail_change", "missing_required_field", "invalid_amount",
                "invalid_payment_date", "invalid_account_number", "unusable_model_output", "none"}
DATE_MIN, DATE_MAX = date(2026, 9, 1), date(2026, 12, 18)

# Our contract's hold_category -> the DDL's HOLD_REASON code. The DDL defines
# bank_detail_change as "payment to an account different from the one on file for
# CLIENT_ID, including a request quoted in a forwarded thread", which covers both our
# bank_detail_change and account_mismatch categories. Categories with no DDL code
# (unknown_client, currency_mismatch as the main reason) are not mapped: such a row
# fails validation and stops the load instead of being given a wrong code.
CATEGORY_TO_HOLD_REASON = {
    "bank_detail_change": "bank_detail_change",
    "account_mismatch": "bank_detail_change",
}


def blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def to_bool(v) -> bool | None:
    s = str(v).strip().lower()
    return True if s == "true" else False if s == "false" else None


def build_rows(load_run_id: str, loaded_at: datetime) -> tuple[list[dict], list[str]]:
    """Map the checked table to DDL rows. Returns (rows, problems)."""
    checked = pd.read_csv(CHECKED, dtype=str, keep_default_na=False).replace("", None)
    emails = pd.read_csv(EMAILS, dtype=str)[["email_id", "is_forwarded_thread"]]
    df = checked.merge(emails, on="email_id", how="left")

    rows, problems = [], []
    for r in df.to_dict("records"):
        eid = r["email_id"]
        bad = lambda msg: problems.append(f"{eid}: {msg}")

        # AMOUNT: positive, 2 decimal places, or NULL
        amount = None
        if not blank(r["amount"]):
            try:
                amount = Decimal(r["amount"]).quantize(Decimal("0.01"))
            except InvalidOperation:
                bad(f"AMOUNT {r['amount']!r} is not a number")

        # PAYMENT_DATE: ISO date or NULL
        pay_date = None
        if not blank(r["value_date"]):
            try:
                pay_date = date.fromisoformat(r["value_date"])
            except ValueError:
                bad(f"PAYMENT_DATE {r['value_date']!r} is not an ISO YYYY-MM-DD date")

        extracted = not blank(r.get("payment_count")) and r["payment_count"] != "0"
        held = r["status"] == "HELD"
        category = r.get("hold_category")
        if not extracted:
            hold_reason = "unusable_model_output"
        elif held:
            hold_reason = CATEGORY_TO_HOLD_REASON.get(category)
            if hold_reason is None:
                bad(f"hold_category {category!r} has no HOLD_REASON code in the DDL")
        else:
            hold_reason = "none"

        rows.append({
            "EMAIL_ID": eid,
            "CLIENT_ID": r["client_id"],
            "PAYEE_NAME": None if blank(r["payee_name"]) else r["payee_name"],
            "PAYMENT_DATE": pay_date,
            "AMOUNT": amount,
            "CURRENCY": None if blank(r["currency"]) else r["currency"],
            "ACCOUNT_NUMBER": None if blank(r["account_with_sort_code"]) else r["account_with_sort_code"],
            # DDL: true when the payment account differs from the one on file
            "BANK_DETAIL_CHANGE": category in CATEGORY_TO_HOLD_REASON,
            "IS_FORWARDED_THREAD": to_bool(r["is_forwarded_thread"]),
            "EXTRACTION_STATUS": "ok" if extracted else "unusable_output",
            "DISPOSITION": "held" if held else "ready",
            "HOLD_REASON": hold_reason,
            # READY means every Great Expectations check passed for this row
            "GE_VALIDATION_PASSED": not held,
            "LOAD_RUN_ID": load_run_id,
            "LOADED_AT": loaded_at,
        })
    return rows, problems


def validate(rows: list[dict]) -> list[str]:
    """Check every value against the DDL column types, NOT NULL flags and comments."""
    problems = []
    if len(rows) != EXPECTED_ROWS:
        problems.append(f"expected {EXPECTED_ROWS} rows, got {len(rows)}")

    ids = [r["EMAIL_ID"] for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"EMAIL_ID not unique: {dupes}")

    for r in rows:
        eid = r["EMAIL_ID"]
        bad = lambda msg: problems.append(f"{eid}: {msg}")
        for col in ("EMAIL_ID", "CLIENT_ID", "BANK_DETAIL_CHANGE", "IS_FORWARDED_THREAD",
                    "EXTRACTION_STATUS", "DISPOSITION", "HOLD_REASON",
                    "GE_VALIDATION_PASSED", "LOAD_RUN_ID", "LOADED_AT"):
            if r[col] is None:
                bad(f"{col} is NULL but NOT NULL in the DDL")

        if not (isinstance(eid, str) and re.fullmatch(r"EM-\d{5}", eid) and 1 <= int(eid[3:]) <= 300):
            bad("EMAIL_ID not in EM-00001..EM-00300")
        cid = r["CLIENT_ID"]
        if not (isinstance(cid, str) and re.fullmatch(r"CL-\d{4}", cid) and 1001 <= int(cid[3:]) <= 1030):
            bad(f"CLIENT_ID {cid!r} not in CL-1001..CL-1030")
        if r["PAYEE_NAME"] is not None and len(r["PAYEE_NAME"]) > 200:
            bad("PAYEE_NAME longer than 200 characters")
        d = r["PAYMENT_DATE"]
        if d is not None and not (DATE_MIN <= d <= DATE_MAX):
            bad(f"PAYMENT_DATE {d} outside {DATE_MIN}..{DATE_MAX}")
        a = r["AMOUNT"]
        if a is not None and (a <= 0 or a.as_tuple().exponent != -2 or len(a.as_tuple().digits) > 18):
            bad(f"AMOUNT {a} is not positive NUMBER(18,2)")
        if r["CURRENCY"] is not None and r["CURRENCY"] not in CURRENCIES:
            bad(f"CURRENCY {r['CURRENCY']!r} not in {sorted(CURRENCIES)}")
        if r["ACCOUNT_NUMBER"] is not None and len(r["ACCOUNT_NUMBER"]) > 34:
            bad(f"ACCOUNT_NUMBER longer than 34 characters ({len(r['ACCOUNT_NUMBER'])})")
        for col in ("BANK_DETAIL_CHANGE", "IS_FORWARDED_THREAD", "GE_VALIDATION_PASSED"):
            if r[col] is not None and not isinstance(r[col], bool):
                bad(f"{col} {r[col]!r} is not true/false")
        if r["EXTRACTION_STATUS"] not in EXTRACTION_STATUSES:
            bad(f"EXTRACTION_STATUS {r['EXTRACTION_STATUS']!r} not allowed")
        if r["DISPOSITION"] not in DISPOSITIONS:
            bad(f"DISPOSITION {r['DISPOSITION']!r} not allowed")
        if r["HOLD_REASON"] not in HOLD_REASONS:
            bad(f"HOLD_REASON {r['HOLD_REASON']!r} not allowed")
        # held rows need a real reason; ready rows must say none
        if r["DISPOSITION"] == "held" and r["HOLD_REASON"] == "none":
            bad("held row with HOLD_REASON 'none'")
        if r["DISPOSITION"] == "ready" and r["HOLD_REASON"] != "none":
            bad(f"ready row with HOLD_REASON {r['HOLD_REASON']!r}")
        if len(r["LOAD_RUN_ID"] or "") > 64:
            bad("LOAD_RUN_ID longer than 64 characters")
    return problems


def connect():
    import snowflake.connector

    load_dotenv(Path(__file__).with_name(".env"))
    creds = {k: os.getenv(k) for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")
    return snowflake.connector.connect(
        account=creds["SNOWFLAKE_ACCOUNT"],
        user=creds["SNOWFLAKE_USER"],
        password=creds["SNOWFLAKE_PASSWORD"],
        role=ROLE,
        warehouse=WAREHOUSE,
        database=DATABASE,
        schema=SCHEMA,
        paramstyle="qmark",
        autocommit=False,
    )


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    load_run_id = f"load-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)  # TIMESTAMP_NTZ, UTC

    rows, problems = build_rows(load_run_id, loaded_at)
    problems += validate(rows)
    if problems:
        print(f"STOPPED: {len(problems)} problem(s); nothing inserted.")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)
    print(f"Validation passed for {len(rows)} rows (run {load_run_id}).")
    if dry_run:
        held = sum(r["DISPOSITION"] == "held" for r in rows)
        print(f"Dry run: would insert {len(rows)} rows ({held} held, {len(rows) - held} ready).")
        return

    conn = connect()
    try:
        cur = conn.cursor()
        # PRIMARY KEY on EMAIL_ID is not enforced, and the loader can't delete,
        # so refuse to load any email that is already in the queue.
        cur.execute(f"SELECT EMAIL_ID FROM {TABLE}")
        already = sorted({r[0] for r in cur.fetchall()} & {r["EMAIL_ID"] for r in rows})
        if already:
            print(f"STOPPED: {len(already)} EMAIL_ID(s) already in the queue; nothing inserted.")
            print("  first few:", already[:10])
            raise SystemExit(1)

        placeholders = ", ".join("?" for _ in COLUMNS)
        cur.executemany(
            f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            [tuple(r[c] for c in COLUMNS) for r in rows],
        )
        conn.commit()

        cur.execute(
            f"SELECT DISPOSITION, COUNT(*) FROM {TABLE} WHERE LOAD_RUN_ID = ? GROUP BY DISPOSITION",
            (load_run_id,),
        )
        counts = dict(cur.fetchall())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    total = sum(counts.values())
    print(f"Inserted {total} rows (run {load_run_id}): "
          f"{counts.get('held', 0)} held, {counts.get('ready', 0)} ready.")


if __name__ == "__main__":
    main()
