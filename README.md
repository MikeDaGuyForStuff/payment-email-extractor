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

## Payment review page

`review_page.py` reviews one pasted email at a time:

```
.venv/bin/streamlit run review_page.py
```

It opens at http://localhost:8501. Paste the email ID, client ID, received time, sender, subject and body, or pick a holdout email in the sidebar, then click **Check and queue**. The page shows:

1. **The payment details:** payee, amount, currency, the payment date as written and as read, the account, any old and new account, the reference and the sender.
2. **READY or HELD** in large text, with every reason in plain words and the queue hold-reason code.
3. **The queue row,** read live from `PAYMENT_INSTRUCTION_QUEUE` with a `SELECT` by email ID after the insert. If the email is already queued, nothing is inserted and the existing row is shown. If Snowflake can't be reached, the page says so instead of showing a row.

The logic is in `review_email.py`, which reuses the existing code instead of copying it: `extract_payments.extract_single` (same prompt, model, Pydantic model and text check), the same Great Expectations suite in `payment_contract.json`, and the loader's own validate, insert and read-back steps. Each rule in the suite is tagged `batch`, `page` or both.

The page holds a payment when the email itself gives a reason. Several rules can fail at once; the most serious one in this order becomes the queue's hold reason:

| Rule | Queue HOLD_REASON |
|---|---|
| Bank-detail change: the email asks to start paying a different account (old and new shown) | `bank_detail_change` |
| Payee, currency or account missing, or a currency outside USD/EUR/GBP/CAD/CHF/JPY | `missing_required_field` |
| Amount missing, not positive, or with more than 2 decimals | `invalid_amount` |
| Payment date missing or unreadable, before the email was received, or outside 2026-09-01 to 2026-12-18 | `invalid_payment_date` |
| Account partial ("ending 0371"), masked ("****"), under 8 characters or over 34 | `invalid_account_number` |
| Model output unusable | `unusable_model_output` |

Unlike the batch run, the page doesn't hold a payment just because its account differs from the accounts workbook. That matches the holdout answers. A date written without a year takes the year the email was received.

**Test results (holdout emails):**

| Email | Result | Queue row |
|---|---|---|
| EM-00311 | HELD, bank-detail change 5503918826 → 9921043376 | held / bank_detail_change |
| EM-00304 | READY to 7725480193 (the quoted account is described as closed history) | ready / none |
| EM-00353 | READY to 5503918826 | ready / none |
| EM-00305 | HELD, bank-detail change to DE12500105170648489890, payee missing | held / bank_detail_change |

The queue now has 304 rows: the 300 from the batch load (run IDs starting `load-`) and these 4 (run IDs starting `page-`). Running the page again on EM-00311 showed the existing row and inserted nothing.

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

### Payment review page (`review_page.py`, `review_email.py`)

> Build a small payment review page for a single pasted client email, reusing what is already in this repo. Do not make new copies of the extractor, the contract or the loader; import and call the existing code.
>
> Parts to build on:
> - extract_payments.py: send the pasted email (sender, subject, body, including any forwarded thread) to the same OpenRouter model, with the same prompt, the same Pydantic Payment model and the same check that blanks any value not found in the email text.
> - payment_contract.json, applied with check_payments.py: run the same Great Expectations suite on the extracted row and add the new hold rules below as expectations in that same suite.
> - load_queue.py: map the row to the PAYMENT_INSTRUCTION_QUEUE columns and validate it exactly as the loader does, then insert it as PAYMENTS_OPS_LOADER (INSERT and SELECT only). If the EMAIL_ID is already in the queue, insert nothing and show the existing row.
> - Credentials: OPENROUTER_API_KEY, OPENROUTER_MODEL, SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PASSWORD come only from .env. Never put them in code or in the page.
>
> Hold rules. The page must mark the payment HELD, with a reason, if any of these is true:
> 1. Bank-detail change: the newest message, or a forwarded/quoted thread, says an old account is replaced or closed, or the newest message asks to pay a different account than the quoted message. Show both the old and the new account. Paying the newest instruction's account is never automatic; it waits for a call-back.
> 2. Missing amount: no amount, or an amount that isn't a positive number with two decimals.
> 3. Past or invalid date: the payment date is missing, can't be read, is before the email's received date, or is outside 2026-09-01 to 2026-12-18.
> 4. Partial account: the account is masked, truncated or incomplete (for example "ending 4471", "****8893" or a sort code with no account number), or longer than 34 characters.
> 5. Any other missing required field (payee or currency), a currency not in USD, EUR, GBP, CAD, CHF or JPY, or model output that can't be parsed.
>
> Map each rule to the queue's HOLD_REASON codes: bank_detail_change, missing_required_field, invalid_amount, invalid_payment_date, invalid_account_number or unusable_model_output. If several rules fail, use the most serious one in that order and list all of them in the page's reason text. Everything else is READY with HOLD_REASON none. Never guess a missing value; leave it blank and hold.
>
> What the page shows:
> 1. The extracted payment details: payee, amount, currency, payment date, account, old and new account if any, reference and sender.
> 2. READY or HELD in large text, with the reason in plain words and the actual values involved.
> 3. The queue row for this email, read live from PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE in Snowflake with a SELECT by EMAIL_ID after the insert, never from a CSV or from the values in memory. If Snowflake can't be reached, say so on the page instead of showing a row.
>
> Test it with these holdout emails and show me the result for each: EM-00311 (should be HELD, bank-detail change to 9921043376), EM-00304 (should be READY to 7725480193, because the quoted older account is closed), EM-00353 and EM-00305. Then tell me how to run the page.

Follow-ups from building it:
- **Extractor prompt:** old and new accounts are now only filled when the email asks to *start* paying a different account, not when a former, closed account is mentioned as history (EM-00304, EM-00320 and EM-00341 in the holdout). Partial accounts are copied as written, never completed.
- **Account check:** "sort code 20-32-06 account 60218834" now passes the text check.
- **Date bounds:** the date limits are converted to real dates when the suite loads, because Great Expectations ignores text date limits.
