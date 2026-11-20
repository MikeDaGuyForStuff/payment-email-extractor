"""Apply payment_contract.json (Great Expectations) to the extracted payment table.

Every row gets status READY or HELD plus the reasons it was held.

Usage:
    .venv/bin/python check_payments.py
"""

from __future__ import annotations

import json
import re

import great_expectations as gx
import pandas as pd

CONTRACT = "payment_contract.json"
PAYMENTS = "payments_sep_nov_2026.csv"
EMAILS = "Client Payment Emails, Shared Operations Inbox Export (Sep–Nov 2026).csv"
ACCOUNTS = "Client Settlement Accounts on File.xlsx"
OUTPUT = "payments_checked.csv"


def as_text_no_spaces(v) -> str | None:
    """Account as text with whitespace removed. Never cast to a number."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = re.sub(r"\s+", "", str(v))
    return s or None


def build_frame() -> tuple[pd.DataFrame, set[str]]:
    # dtype=str everywhere: account numbers stay text, leading zeros survive.
    payments = pd.read_csv(PAYMENTS, dtype=str, keep_default_na=False).replace("", None)
    emails = pd.read_csv(EMAILS, dtype=str)[["email_id", "client_id"]]
    accounts = pd.read_excel(ACCOUNTS, sheet_name="Accounts On File", dtype=str)
    accounts = accounts.rename(columns={
        "Client ID": "client_id",
        "Client Name": "client_name_on_file",
        "Account Number On File": "account_on_file",
        "Account Currency": "account_currency_on_file",
    })[["client_id", "client_name_on_file", "account_on_file", "account_currency_on_file"]]

    df = payments.merge(emails, on="email_id", how="left", validate="many_to_one")
    df = df.merge(accounts, on="client_id", how="left", validate="many_to_one")
    df["account_text"] = df["account_with_sort_code"].map(as_text_no_spaces)
    df["account_on_file_text"] = df["account_on_file"].map(as_text_no_spaces)
    df.insert(0, "row_id", range(1, len(df) + 1))
    return df, set(accounts["client_id"].dropna())


def load_suite(client_ids: set[str] | None = None, scope: str = "batch") -> gx.ExpectationSuite:
    """Load the contract, keeping only the expectations whose meta.scope includes `scope`.

    "batch" = the checked-table run against the accounts file; "page" = the single-email
    review page. Expectations without a scope are batch-only.
    """
    with open(CONTRACT, encoding="utf-8") as f:
        contract = json.load(f)
    kept = []
    for exp in contract["expectations"]:
        if scope not in exp["meta"].get("scope", ["batch"]):
            continue
        if exp["kwargs"].get("value_set") == "__CLIENT_IDS_ON_FILE__":
            exp["kwargs"]["value_set"] = sorted(client_ids or [])
        if exp["meta"].get("date_bounds"):  # JSON holds dates as text; GX needs real dates
            for k in ("min_value", "max_value"):
                if k in exp["kwargs"]:
                    exp["kwargs"][k] = pd.Timestamp(exp["kwargs"][k])
        kept.append(exp)
    contract["expectations"] = kept
    return gx.ExpectationSuite(**contract)


def run_suite(df: pd.DataFrame, suite: gx.ExpectationSuite, verbose: bool = True):
    """Validate df (must have a row_id column). Returns {row_id: [(priority, rule, meta), ...]}."""
    context = gx.get_context(mode="ephemeral")
    batch = (
        context.data_sources.add_pandas("payments")
        .add_dataframe_asset("payment_table")
        .add_batch_definition_whole_dataframe("all_rows")
        .get_batch(batch_parameters={"dataframe": df})
    )
    results = batch.validate(
        suite,
        result_format={"result_format": "COMPLETE", "unexpected_index_column_names": ["row_id"]},
    )
    failed: dict[int, list[tuple[int, str, dict]]] = {rid: [] for rid in df.row_id}
    for r in results.results:
        meta = r.expectation_config.meta
        n_bad = r.result.get("unexpected_count", 0)
        if verbose:
            print(f"{meta['rule']:<20} {'PASS' if r.success else 'FAIL'}  {n_bad} rows held")
        for item in r.result.get("unexpected_index_list") or []:
            entry = (meta["priority"], meta["rule"], meta)
            if entry not in failed[item["row_id"]]:
                failed[item["row_id"]].append(entry)
    return failed


def main() -> None:
    df, client_ids = build_frame()
    suite = load_suite(client_ids, scope="batch")
    failed = run_suite(df, suite)

    rows = df.set_index("row_id")
    status, category, reason = [], [], []
    for rid in df.row_id:
        fails = sorted(failed[rid], key=lambda f: f[0])
        values = {k: ("blank" if pd.isna(v) else v) for k, v in rows.loc[rid].items()}
        texts = [m["hold_reason"].format(**values) for _, _, m in fails]
        if values["payment_count"] not in ("1", 1, "blank"):
            texts.append(f"note: {int(values['payment_count']) - 1} more payment(s) in additional_payments, not checked")
        status.append("HELD" if fails else "READY")
        category.append(fails[0][1] if fails else "")
        reason.append("; ".join(texts) if fails else "matches account and currency on file")
    df["status"], df["hold_category"], df["reason"] = status, category, reason

    out_cols = ["row_id", "status", "hold_category", "reason", "email_id", "client_id", "client_name_on_file",
                "payee_name", "amount", "currency", "account_currency_on_file", "value_date",
                "account_with_sort_code", "account_on_file", "old_account", "new_account",
                "payment_count", "additional_payments",
                "reference", "sender_email"]
    df[out_cols].to_csv(OUTPUT, index=False)
    print(f"\n{len(df)} rows -> {OUTPUT}")
    print(df.status.value_counts().to_string())
    print(df.hold_category.value_counts().to_string())


if __name__ == "__main__":
    main()
