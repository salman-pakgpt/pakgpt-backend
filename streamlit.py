import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000/chat")

st.set_page_config(page_title="PakGPT Test Console", layout="centered")
st.title("PakGPT Test Console")

if "history" not in st.session_state:
    st.session_state.history = []  # each item: session_id, query, response, context

with st.form("chat_form", clear_on_submit=True):
    col1, col2 = st.columns([1, 3])
    session_id_input = col1.text_input("session_id")
    query_input = col2.text_input("query")
    submitted = st.form_submit_button("Send")

if submitted and query_input.strip():
    payload = {"message": query_input}
    if session_id_input.strip():
        payload["session_id"] = session_id_input.strip()

    try:
        res = requests.post(API_URL, json=payload, timeout=30)
        res.raise_for_status()
        data = res.json()
        st.session_state.history.append(
            {
                "session_id": data["session_id"],
                "query": query_input,
                "response": data["reply"],
                "context": data.get("context", []),
            }
        )
    except Exception as e:
        st.error(f"Request failed: {e}")

# Newest first, so the latest result is visible without scrolling
for i in range(len(st.session_state.history) - 1, -1, -1):
    turn = st.session_state.history[i]

    col1, col2 = st.columns([1, 3])
    col1.text_input("session_id", value=turn["session_id"], disabled=True, key=f"sid_{i}")
    col2.text_input("query", value=turn["query"], disabled=True, key=f"q_{i}")

    st.text_area("response", value=turn["response"], disabled=True, key=f"r_{i}", height=80)

    with st.expander("click to see used context"):
        st.json(turn["context"])

    st.divider()