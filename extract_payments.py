"""Extract payment instructions from client emails with an OpenRouter model.

Reads OPENROUTER_API_KEY and OPENROUTER_MODEL from .env (never from code).
Emails are sent in batches (free tier allows ~50 requests/day), each tagged with
its email_id. Every value the model returns is then checked against that email's
own text; anything that cannot be found verbatim is blanked, so nothing is guessed.

Usage:
    .venv/bin/python extract_payments.py [input.csv] [output.csv]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIStatusError
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_INPUT = "Client Payment Emails, Shared Operations Inbox Export (Sep–Nov 2026).csv"
DEFAULT_OUTPUT = "payments_sep_nov_2026.csv"
CACHE_FILE = Path(".cache/raw_responses.jsonl")  # lets a stopped run resume
BATCH_SIZE = 10
MIN_SECONDS_BETWEEN_CALLS = 4  # stay under 20 requests/minute


class Payment(BaseModel):
    """One payment instruction. Blank (None) when the email does not state it."""

    email_id: str
    payee_name: Optional[str] = Field(None, description="Who receives the money")
    amount: Optional[float] = Field(None, description="Plain number, no currency or commas")
    currency: Optional[str] = Field(None, description="ISO code, e.g. GBP")
    value_date: Optional[str] = Field(None, description="Payment date exactly as written")
    account_with_sort_code: Optional[str] = Field(
        None, description="Account/IBAN to pay into, including sort code if given"
    )
    old_account: Optional[str] = Field(None, description="Account the email says is old/closed")
    new_account: Optional[str] = Field(None, description="Account the email says replaces it")
    reference: Optional[str] = Field(None, description="Payment reference / invoice numbers")
    sender_email: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "n/a", "na", "unknown"}:
            return None
        return v

    @field_validator("amount", mode="before")
    @classmethod
    def plain_number(cls, v):
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return v


class Extraction(BaseModel):
    payments: list[Payment]


SYSTEM_PROMPT = """You extract payment instructions from client emails for a bank operations team.

Return JSON: {"payments": [ {...}, ... ]} with ONE object per payment instruction.
An email with no payment instruction produces no objects. An email asking for
several payments produces several objects. Every object must carry the email_id
of the email it came from.

Fields: email_id, payee_name, amount, currency, value_date, account_with_sort_code,
old_account, new_account, reference, sender_email.

Rules:
- Copy values exactly as written in that email. Use null for anything not stated.
  NEVER guess, infer, convert, or borrow a value from a different email.
- amount: plain number only (e.g. 184250.00), no currency symbol or thousands separators.
- currency: 3-letter code as written (GBP, EUR, USD...).
- value_date: the date the payment should be made, as written.
- account_with_sort_code: the account or IBAN the money should be sent to; include the
  sort code if one is given.
- Emails may contain a forwarded thread (after "-----Original Message-----"). Read it:
  if it says an old account is closed/no longer used, put that in old_account and the
  replacement in new_account. Leave both null if no change is mentioned.
- reference: the payment reference or invoice number(s) as written.
- sender_email: the sender_email given for that email.
Output JSON only."""


# ---------- anti-guessing check: every value must appear in the source email ----------

def _norm(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s).upper()


def _numbers_in(text: str) -> set[float]:
    out = set()
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def verify(p: Payment, source: str) -> tuple[Payment, list[str]]:
    """Blank any field whose value is not present in the source email."""
    blanked = []
    src_norm = _norm(source)
    data = p.model_dump()
    for field, val in data.items():
        if field == "email_id" or val is None:
            continue
        if field == "amount":
            ok = round(float(val), 2) in _numbers_in(source)
        else:
            ok = bool(_norm(str(val))) and _norm(str(val)) in src_norm
        if not ok:
            data[field] = None
            blanked.append(f"{field}={val!r}")
    return Payment(**data), blanked


# ---------- model call ----------

def email_block(row) -> str:
    return (
        f"<email email_id=\"{row.email_id}\">\n"
        f"sender_email: {row.sender_email}\n"
        f"subject: {row.subject}\n"
        f"body: {row.email_body}\n"
        f"</email>"
    )


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def call_model(client: OpenAI, model: str, batch: pd.DataFrame) -> str:
    user = "Extract the payments from these emails:\n\n" + "\n\n".join(
        email_block(r) for r in batch.itertuples()
    )
    for attempt in range(6):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
            )
            content = resp.choices[0].message.content
            if content:
                return content
            raise ValueError("empty response")
        except RateLimitError as e:
            wait = 30 * (attempt + 1)
            msg = str(e)
            if "per-day" in msg or "free-models-per-day" in msg:
                raise SystemExit(f"Daily free-model limit reached. Re-run later to resume.\n{msg}")
            print(f"  rate limited ({msg[:160]}), waiting {wait}s")
            time.sleep(wait)
        except (APIStatusError, ValueError) as e:
            wait = 10 * (attempt + 1)
            print(f"  error ({e}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("gave up after retries")


def load_cache() -> dict[str, str]:
    cache = {}
    if CACHE_FILE.exists():
        for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            cache[rec["batch_key"]] = rec["content"]
    return cache


def main() -> None:
    in_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    out_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUTPUT

    load_dotenv(Path(__file__).with_name(".env"))
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL")
    if not api_key or not model:
        raise SystemExit("Set OPENROUTER_API_KEY and OPENROUTER_MODEL in .env")

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    emails = pd.read_csv(in_path, dtype=str).fillna("")
    sources = {r.email_id: email_block(r) for r in emails.itertuples()}
    print(f"{len(emails)} emails, model {model}, batches of {BATCH_SIZE}")

    CACHE_FILE.parent.mkdir(exist_ok=True)
    cache = load_cache()
    rows, problems = [], []
    last_call = 0.0

    for start in range(0, len(emails), BATCH_SIZE):
        batch = emails.iloc[start : start + BATCH_SIZE]
        ids = list(batch.email_id)
        key = f"{ids[0]}..{ids[-1]}"

        if key in cache:
            content = cache[key]
        else:
            time.sleep(max(0, MIN_SECONDS_BETWEEN_CALLS - (time.time() - last_call)))
            print(f"batch {key}")
            try:
                content = call_model(client, model, batch)
            except (RuntimeError, SystemExit) as e:
                # save what we have; re-running resumes from the cache
                print(f"  stopping early: {e}")
                for eid in emails.email_id.iloc[start:]:
                    problems.append({"email_id": eid, "issue": "not processed yet (API limit); re-run to resume"})
                break
            last_call = time.time()

        try:
            extraction = Extraction.model_validate(parse_json(content))
        except (ValueError, ValidationError) as e:
            problems.append({"email_id": key, "issue": f"unparseable batch response: {e}"})
            continue  # not cached, so a re-run retries it

        if key not in cache:
            with CACHE_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"batch_key": key, "content": content}) + "\n")

        seen = set()
        by_number = {int(re.sub(r"\D", "", i)): i for i in ids}
        for p in extraction.payments:
            if p.email_id not in ids:
                # model sometimes mangles padding, e.g. EM-000010 for EM-00010
                digits = re.sub(r"\D", "", p.email_id)
                fixed = by_number.get(int(digits)) if digits else None
                if fixed:
                    p.email_id = fixed
            if p.email_id not in ids:
                problems.append({"email_id": p.email_id, "issue": "row tagged with email not in batch; dropped"})
                continue
            p, blanked = verify(p, sources[p.email_id])
            if blanked:
                problems.append({"email_id": p.email_id, "issue": "blanked unverifiable: " + "; ".join(blanked)})
            rows.append(p.model_dump())
            seen.add(p.email_id)
        for eid in ids:
            if eid not in seen:
                problems.append({"email_id": eid, "issue": "no payment extracted"})

    df = pd.DataFrame(rows, columns=list(Payment.model_fields))
    df.to_csv(out_path, index=False)

    # Shared fields (date, reference) in multi-payment emails may have been
    # copied to a payment they don't apply to; the text check can't catch that.
    for eid, n in df.email_id.value_counts().items():
        if n > 1:
            problems.append({"email_id": eid, "issue": f"{n} payments in one email: check date/reference apply to each"})
    # Same account for different payees is a common payment-fraud signal.
    for acct, payees in df.groupby("account_with_sort_code").payee_name.unique().items():
        if len(payees) > 1:
            for eid in df[df.account_with_sort_code == acct].email_id.unique():
                problems.append({"email_id": eid, "issue": f"account {acct} used by several payees: {', '.join(payees)}"})
    pd.DataFrame(problems, columns=["email_id", "issue"]).to_csv(
        Path(out_path).with_name(Path(out_path).stem + "_review.csv"), index=False
    )
    print(f"\n{len(df)} payment rows from {df.email_id.nunique()} emails -> {out_path}")
    print(f"{len(problems)} items flagged for review")
    print(df.notna().sum().to_string())


if __name__ == "__main__":
    main()
