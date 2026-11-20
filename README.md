# Payment Email Extractor

Turns client payment emails into a table of payments (`extract_payments.py`), then checks each payment against the client's verified account on file before release (`check_payments.py` with the Great Expectations contract in `payment_contract.json`).

## Which payments are held, and why

A payment is **ready** only if it passes every rule. Otherwise it is **held**. `hold_category` gives the most serious rule it failed, and `reason` lists every failed rule, most serious first, with the actual values:

- **Bank-detail change** (most serious). The email or its forwarded thread says an old account has been replaced by a new one. This is how payment-redirection fraud works, so these rows always need a call-back to the client's registered contact.
- **Client ID not in the accounts file.** We have no verified account for this client, so there's nothing to check the payment against.
- **Account differs from the account on file.** This is the main fraud risk. A fake "we've changed banks" email works by getting money sent to a new account, and some fakes are only one digit off the real one. Any difference, however small, holds the payment until someone confirms it with the client's registered contact.
- **Currency differs from the client's account currency.** Paying in a currency the account isn't held in can mean a mistake, a conversion nobody intended, or the wrong account.

Holding is the safe default. A held payment costs a phone call, while releasing a bad one can lose the money for good.

## Results

`payments_checked.csv` has one row per email (300 rows):

| Status | Rows |
|---|---|
| Ready | 0 |
| Held | 300 |

| Hold category (most serious failed check) | Rows |
|---|---|
| Bank-detail change (old → new account in email) | 17 |
| Unknown client | 0 |
| Account differs from account on file | 283 |
| Currency differs (as main category) | 0 |

Currency also differs from the account currency on 230 rows, but those rows fail the account check too, so it shows in `reason` rather than as the main category. Every row is held because the emails pay suppliers, payroll and other third-party accounts, and none of the 30 on-file client accounts appears in any email. Analysts should call back the 17 bank-detail changes first.

## How accounts are compared

- **As text, never as numbers.** Turning `004417829310` into a number drops the leading zeros and changes the account, so both sides are read and compared as text.
- **Spaces removed.** IBANs are often written in groups of four (`GB47 NWBK 6034 …`). Spaces are stripped from both sides first, so formatting alone never causes a mismatch.
- **Nothing else changes.** Letters, digits and other characters are compared exactly, and there's no fuzzy matching. An account that is "nearly" the same is treated as different.

## One row per email

The payment table has exactly one row per email (300 rows). If an email asks for more than one payment, the row holds the first one, `payment_count` shows how many there were, and the others are written out in `additional_payments`. Two emails do this (EM-00021 and EM-00291, which also pay Dataform Labs). Those extra payments aren't checked against the accounts file, so they need to be reviewed by hand.

## What the account column holds

`account_with_sort_code` is the account the email asks us to pay, usually a supplier, payroll or other third party. It is not the client's own settlement account. None of the clients' on-file accounts appears in any of the 300 emails, so the checker currently holds every row.

## Known limitations

- Account numbers written with hyphens (e.g. `3301-556-0098`) are compared with the hyphens kept, so they won't match an on-file account stored without them.
- The accounts workbook stores some account numbers as numbers, so leading zeros may already be missing in the file itself. Those rows hold rather than release, which is the safe way to fail.

## Loading the Snowflake queue

`load_queue.py` loads the 300 checked rows into `PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE`, following `Payment Instruction Queue Table Definition (as provisioned by Data Platform).sql`.

- **Credentials:** `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER` and `SNOWFLAKE_PASSWORD` are read from `.env`, which is not in the repo. Use the account identifier (`ORGNAME-ACCOUNTNAME`), not the bare locator.
- **Access:** connects as `PAYMENTS_OPS_LOADER` on `PAYMENTS_OPS_WH` and only runs `SELECT` and `INSERT`.
- **Checks before writing anything:** every value is checked against the table definition (types, NOT NULL, allowed values, date range, ID formats, 300 unique email IDs). Any email already in the queue also stops the run. If anything fails, nothing is inserted.
- **Run stamp:** one `LOAD_RUN_ID` per run, and the same `LOADED_AT` on every row. All rows go in one transaction.
- **Mapping to the table:**
  - `HOLD_REASON` is `bank_detail_change`, because the table defines that as paying an account different from the one on file. The 17-versus-283 split stays in `payments_checked.csv`.
  - `GE_VALIDATION_PASSED` is true only for ready rows.
  - `IS_FORWARDED_THREAD` comes from the email export.

Run `.venv/bin/python load_queue.py`, or add `--dry-run` to validate without connecting.

**Result:** 300 rows inserted in run `load-20261120T230359Z-143eb215`, all 300 held and 0 ready. `SELECT COUNT(*)` on the queue returns 300. A second run stopped with "300 EMAIL_ID(s) already in the queue; nothing inserted", so the queue was not doubled.

## Claude Code prompts used

### Payment contract (`payment_contract.json`, `check_payments.py`)

> Use my payment table from the last stage and client_accounts_on_file.xlsx. Write a Great Expectations payment contract with these rules: strip spaces from IBANs and compare account numbers as text so leading zeros are kept. Hold any row whose account differs from the account on file, any row whose currency differs from the client's account currency, and any row whose client ID is not in the accounts file. Every other row is ready. Install great_expectations if it is missing. Apply the contract to all 300 rows with Pandas. Hand back the contract file and the checked payment table, with a ready or held status and a reason for each row.

Follow-up changes from review: one row per email (extra payments moved to `additional_payments`), a bank-detail change rule, and a `hold_category` column with specific reasons.

### Snowflake load (`load_queue.py`)

> Write a Python load script that reads my checked payment table of 300 rows from the last stage and inserts them into PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE. Follow payment_instruction_queue_ddl.sql exactly: same column names, types and NOT NULL flags, and no extra tables or schemas.
>
> Rules:
> - Read SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PASSWORD from environment variables, loaded from my .env file. Never write them into code.
> - Connect with role PAYMENTS_OPS_LOADER on warehouse PAYMENTS_OPS_WH. Only INSERT and SELECT, no UPDATE, DELETE or TRUNCATE.
> - Give the whole run one load_run_id and stamp loaded_at on every row.
> - Before inserting, check every value against the allowed values in the DDL column comments and check email_id is unique. If anything fails, stop and report it without inserting.
>
> Hand back: the script. When it runs, it prints the total rows inserted plus the held and ready counts.

Follow-up from running it: the script also refuses to insert any email already in the queue, because the primary key isn't enforced and the loader can't delete duplicates.
