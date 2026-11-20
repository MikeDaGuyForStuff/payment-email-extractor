# Payment Email Extractor

Turns client payment emails into a table of payments (`extract_payments.py`), then checks each payment against the client's verified account on file before release (`check_payments.py` with the Great Expectations contract in `payment_contract.json`).

## Which payments are held, and why

A payment is **ready** only if it passes every rule. Otherwise it is **held**, and every rule it failed is listed in the `reason` column:

- **Client ID not in the accounts file.** We have no verified account for this client, so there's nothing to check the payment against.
- **Account differs from the account on file.** This is the main fraud risk. A fake "we've changed banks" email works by getting money sent to a new account, and some fakes are only one digit off the real one. Any difference, however small, holds the payment until someone confirms it with the client's registered contact.
- **Currency differs from the client's account currency.** Paying in a currency the account isn't held in can mean a mistake, a conversion nobody intended, or the wrong account.

Holding is the safe default. A held payment costs a phone call, while releasing a bad one can lose the money for good.

## How accounts are compared

- **As text, never as numbers.** Turning `004417829310` into a number drops the leading zeros and changes the account, so both sides are read and compared as text.
- **Spaces removed.** IBANs are often written in groups of four (`GB47 NWBK 6034 …`). Spaces are stripped from both sides first, so formatting alone never causes a mismatch.
- **Nothing else changes.** Letters, digits and other characters are compared exactly, and there's no fuzzy matching. An account that is "nearly" the same is treated as different.

## Known limitations

- Account numbers written with hyphens (e.g. `3301-556-0098`) are compared with the hyphens kept, so they won't match an on-file account stored without them.
- The accounts workbook stores some account numbers as numbers, so leading zeros may already be missing in the file itself. Those rows hold rather than release, which is the safe way to fail.
