"""Review one pasted payment email: extract, check, queue, read back.

Reuses the existing pieces instead of copying them:
- extract_payments.extract_single  (same prompt, model, Pydantic Payment, verify())
- check_payments.load_suite/run_suite with payment_contract.json, scope "page"
- load_queue.validate / insert_rows / fetch_queue_row  (PAYMENTS_OPS_LOADER, INSERT+SELECT)

Used by review_page.py. Can also be run from the command line to test holdout emails:
    .venv/bin/python review_email.py EM-00311 EM-00304            # extract, check, insert, read back
    .venv/bin/python review_email.py --no-insert EM-00311         # skip Snowflake
"""

from __future__ import annotations

import re
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
from dateutil import parser as dateparser

import check_payments as C
import extract_payments as X
import load_queue as L

HOLDOUT = "Holdout client payment emails, 17 Nov to 14 Dec 2026.csv"
FORWARD_MARKERS = re.compile(r"-----Original Message-----|Forwarded message|^\s*>|wrote:", re.I | re.M)
ACCOUNT_WORDS = re.compile(r"(?i)\b(sort\s*code|account|acct|iban|no\.?|number|ending|ends\s+in)\b")


def parse_payment_date(text: str | None, received: date) -> date | None:
    """Read a written date. A date with no year takes the year it was received in."""
    if not text:
        return None
    try:
        return dateparser.parse(text, default=datetime(received.year, 1, 1), dayfirst=True, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def amount_text(v) -> str | None:
    return None if v is None else format(Decimal(str(v)).normalize(), "f")


def check_frame(email: dict, payment: dict, extraction: dict) -> pd.DataFrame:
    """One-row frame with the columns the page rules in payment_contract.json check."""
    received = datetime.fromisoformat(email["received_at"].replace("Z", "+00:00")).date()
    pay_date = parse_payment_date(payment.get("value_date"), received)
    account = payment.get("account_with_sort_code")
    return pd.DataFrame([{
        "row_id": 1,
        **payment,
        "client_id": email["client_id"],
        "amount_text": amount_text(payment.get("amount")),
        "payment_date": pd.Timestamp(pay_date) if pay_date else pd.NaT,
        "received_date": pd.Timestamp(received),
        "account_core": None if account is None else re.sub(r"[^0-9A-Za-z]", "", ACCOUNT_WORDS.sub("", account)),
        "extraction_status": extraction["status"],
        "extraction_problems": "; ".join(extraction["problems"]) or "none",
    }])


def review(email: dict, insert: bool = True) -> dict:
    """Run one email through extraction, the contract and the queue.

    email needs: email_id, client_id, received_at, sender_email, subject, email_body;
    is_forwarded_thread is detected from the body if not given.
    """
    extraction = X.extract_single(email)
    payments = [p.model_dump() for p in extraction["payments"]]
    payment = payments[0] if payments else {f: None for f in X.Payment.model_fields}
    payment["email_id"] = email["email_id"]

    df = check_frame(email, payment, extraction)
    suite = C.load_suite(scope="page")
    fails = sorted(C.run_suite(df, suite, verbose=False)[1], key=lambda f: f[0])
    if extraction["status"] != "ok":  # nothing else can be judged
        fails = [f for f in fails if f[1] == "unusable_output"]

    def shown(v):
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return "blank"
        return v.date().isoformat() if isinstance(v, pd.Timestamp) else v
    values = {k: shown(v) for k, v in df.iloc[0].items()}
    reasons = [m["hold_reason"].format(**values) for _, _, m in fails]
    if len(payments) > 1:
        reasons.append(f"note: the email asks for {len(payments)} payments; only the first is checked")
    held = bool(fails)
    hold_reason = fails[0][2]["queue_code"] if held else "none"

    forwarded = email.get("is_forwarded_thread")
    forwarded = (str(forwarded).lower() == "true") if forwarded not in (None, "") \
        else bool(FORWARD_MARKERS.search(email.get("email_body", "")))

    # Queue row. Values the DDL can't hold (e.g. a date outside its range) go in as NULL;
    # the row is held anyway and the page shows the real value and the reason.
    pay_date = date.fromisoformat(values["payment_date"]) if values["payment_date"] != "blank" else None
    if pay_date and not (L.DATE_MIN <= pay_date <= L.DATE_MAX):
        pay_date = None
    amount = None if payment.get("amount") is None else Decimal(str(payment["amount"])).quantize(Decimal("0.01"))
    if amount is not None and amount <= 0:
        amount = None
    account = payment.get("account_with_sort_code")
    load_run_id = f"page-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    queue_row = {
        "EMAIL_ID": email["email_id"],
        "CLIENT_ID": email["client_id"],
        "PAYEE_NAME": payment.get("payee_name"),
        "PAYMENT_DATE": pay_date,
        "AMOUNT": amount,
        "CURRENCY": payment.get("currency") if payment.get("currency") in L.CURRENCIES else None,
        "ACCOUNT_NUMBER": account if account and len(account) <= 34 else None,
        "BANK_DETAIL_CHANGE": any(f[1] == "bank_detail_change" for f in fails),
        "IS_FORWARDED_THREAD": forwarded,
        "EXTRACTION_STATUS": extraction["status"] if extraction["status"] in L.EXTRACTION_STATUSES else "unusable_output",
        "DISPOSITION": "held" if held else "ready",
        "HOLD_REASON": hold_reason,
        "GE_VALIDATION_PASSED": not held,
        "LOAD_RUN_ID": load_run_id,
        "LOADED_AT": datetime.now(timezone.utc).replace(tzinfo=None),
    }

    result = {
        "email": email, "payment": payment, "payment_count": len(payments),
        "payment_date": values["payment_date"], "status": "HELD" if held else "READY",
        "hold_reason": hold_reason, "reasons": reasons, "rules_failed": [f[1] for f in fails],
        "extraction_problems": extraction["problems"], "queue_row_written": queue_row,
        "queue_problems": L.validate([queue_row], expected_rows=1, max_email_number=None),
        "inserted": False, "already_in_queue": False, "queue_row": None, "snowflake_error": None,
    }
    if not insert:
        return result
    if result["queue_problems"]:
        result["snowflake_error"] = "row failed the queue checks, so nothing was inserted"
        return result

    try:
        conn = L.connect()
    except BaseException as e:  # connect() raises SystemExit when .env is incomplete
        result["snowflake_error"] = f"could not connect to Snowflake: {str(e).splitlines()[0][:200]}"
        return result
    try:
        cur = conn.cursor()
        if L.already_loaded(cur, [email["email_id"]]):
            result["already_in_queue"] = True
        else:
            L.insert_rows(conn, [queue_row])
            result["inserted"] = True
        result["queue_row"] = L.fetch_queue_row(cur, email["email_id"])  # read live, never from memory
    except Exception as e:
        result["snowflake_error"] = f"Snowflake error: {str(e).splitlines()[0][:200]}"
    finally:
        conn.close()
    return result


def holdout_email(email_id: str) -> dict:
    h = pd.read_csv(HOLDOUT, dtype=str).set_index("email_id")
    r = h.loc[email_id]
    return {"email_id": email_id, "client_id": r.client_id, "received_at": r.received_at,
            "sender_email": r.sender_address, "subject": r.subject, "email_body": r.email_body,
            "is_forwarded_thread": r.is_forwarded_thread}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for eid in args:
        res = review(holdout_email(eid), insert="--no-insert" not in sys.argv)
        p = res["payment"]
        print(f"\n=== {eid}: {res['status']}  (HOLD_REASON {res['hold_reason']})")
        print(f"  payee {p['payee_name']} | {p['currency']} {p['amount']} | date {p['value_date']} -> {res['payment_date']}"
              f" | account {p['account_with_sort_code']} | old {p['old_account']} -> new {p['new_account']}")
        for r in res["reasons"]:
            print("  reason:", r)
        if res["extraction_problems"]:
            print("  extraction:", res["extraction_problems"])
        if res["queue_problems"]:
            print("  queue checks:", res["queue_problems"])
        if res["snowflake_error"]:
            print("  snowflake:", res["snowflake_error"])
        elif res["queue_row"]:
            q = res["queue_row"]
            print(f"  queue ({'inserted' if res['inserted'] else 'already there, not inserted'}): "
                  f"{q['DISPOSITION']} / {q['HOLD_REASON']} | {q['PAYEE_NAME']} | {q['AMOUNT']} {q['CURRENCY']}"
                  f" | {q['PAYMENT_DATE']} | {q['ACCOUNT_NUMBER']} | bank change {q['BANK_DETAIL_CHANGE']}"
                  f" | run {q['LOAD_RUN_ID']}")
