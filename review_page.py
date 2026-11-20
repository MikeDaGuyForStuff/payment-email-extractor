"""Payment review page: paste one client email, see READY/HELD and its live queue row.

Run:  .venv/bin/streamlit run review_page.py
All logic lives in review_email.py, which reuses the extractor, the Great Expectations
contract and the Snowflake loader. Credentials come only from .env.
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import review_email as R

st.set_page_config(page_title="Payment review", page_icon="💷", layout="wide")
st.title("Payment review")
st.caption("Paste one client payment email. It is extracted, checked against the payment contract, "
           "added to the Snowflake payment queue, and the queue row is read back live.")

# Optional: fill the form from a holdout email for testing.
try:
    holdout = pd.read_csv(R.HOLDOUT, dtype=str)
    ids = [""] + holdout.email_id.tolist()
except FileNotFoundError:
    ids = [""]
pick = st.sidebar.selectbox("Fill from holdout email (optional)", ids)
example = R.holdout_email(pick) if pick else {}
if pick:
    st.sidebar.caption(f"Loaded {pick}. Edit anything before checking.")

with st.form("email"):
    c1, c2, c3 = st.columns(3)
    email_id = c1.text_input("Email ID", example.get("email_id", ""), placeholder="EM-00361")
    client_id = c2.text_input("Client ID", example.get("client_id", ""), placeholder="CL-1001")
    received_at = c3.text_input("Received at (UTC)", example.get("received_at",
                                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    c4, c5 = st.columns(2)
    sender = c4.text_input("Sender", example.get("sender_email", ""))
    subject = c5.text_input("Subject", example.get("subject", ""))
    body = st.text_area("Email body (include any forwarded thread)", example.get("email_body", ""), height=220)
    submitted = st.form_submit_button("Check and queue", type="primary")

if submitted:
    missing = [n for n, v in [("Email ID", email_id), ("Client ID", client_id),
                              ("Received at", received_at), ("Email body", body)] if not v.strip()]
    if missing:
        st.error("Please fill in: " + ", ".join(missing))
        st.stop()
    email = {"email_id": email_id.strip(), "client_id": client_id.strip(), "received_at": received_at.strip(),
             "sender_email": sender.strip(), "subject": subject.strip(), "email_body": body}
    if pick and email_id.strip() == pick and body == example.get("email_body"):
        email["is_forwarded_thread"] = example.get("is_forwarded_thread")
    try:
        with st.spinner("Extracting with the model, checking, and writing to Snowflake..."):
            res = R.review(email)
    except SystemExit as e:  # e.g. OpenRouter daily limit
        st.error(f"Extraction stopped: {e}")
        st.stop()

    p = res["payment"]

    # 1. Status and reason
    if res["status"] == "HELD":
        st.markdown("<h1 style='color:#c0392b;margin:0'>HELD</h1>", unsafe_allow_html=True)
        st.markdown(f"**Queue hold reason:** `{res['hold_reason']}`")
        for r in res["reasons"]:
            st.markdown(f"- {r}")
    else:
        st.markdown("<h1 style='color:#1e8449;margin:0'>READY</h1>", unsafe_allow_html=True)
        st.markdown("Every check in the payment contract passed.")
        for r in res["reasons"]:
            st.markdown(f"- {r}")

    # 2. Extracted details
    st.subheader("Payment details")
    details = {
        "Payee": p.get("payee_name"),
        "Amount": p.get("amount"),
        "Currency": p.get("currency"),
        "Payment date (as written)": p.get("value_date"),
        "Payment date (read as)": None if res["payment_date"] == "blank" else res["payment_date"],
        "Account": p.get("account_with_sort_code"),
        "Old account": p.get("old_account"),
        "New account": p.get("new_account"),
        "Reference": p.get("reference"),
        "Sender": p.get("sender_email") or email["sender_email"],
    }
    st.table(pd.DataFrame({"Field": list(details), "Value": ["—" if v is None else str(v) for v in details.values()]}))
    if res["extraction_problems"]:
        st.caption("Extraction notes: " + "; ".join(res["extraction_problems"]))

    # 3. Queue row, read live from Snowflake
    st.subheader("Payment queue row (live from Snowflake)")
    if res["snowflake_error"]:
        st.error(f"No queue row shown: {res['snowflake_error']}")
        for q in res["queue_problems"]:
            st.markdown(f"- {q}")
    elif res["queue_row"]:
        if res["already_in_queue"]:
            st.info(f"{email['email_id']} was already in the queue, so nothing was inserted. "
                    "This is the existing row.")
        else:
            st.success(f"Inserted {email['email_id']} into PAYMENT_INSTRUCTION_QUEUE.")
        row = {k: ("NULL" if v is None else str(v)) for k, v in res["queue_row"].items()}
        st.table(pd.DataFrame({"Column": list(row), "Value": list(row.values())}))
    else:
        st.error("The row was not found in the queue after the insert.")
