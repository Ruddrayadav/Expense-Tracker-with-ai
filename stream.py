"""Token-efficient Streamlit UI for the expense MCP server (cloud version).

Secrets / env vars (Streamlit Cloud -> App settings -> Secrets):
    GOOGLE_API_KEY   Gemini key
    MCP_SERVER_URL   https://your-service.onrender.com/mcp
    MCP_API_KEY      same value as MCP_API_KEY on the server
    APP_PASSWORD     password to open this web app
    GEMINI_MODEL     optional
    TZ_NAME          optional, default Asia/Kolkata
"""

import asyncio
import hmac
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv
from fastmcp import Client
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

# =========================================================
# CONFIG
# =========================================================

load_dotenv()  # local dev; on Streamlit Cloud, top-level secrets are exposed as env vars

API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL")
MCP_API_KEY = os.getenv("MCP_API_KEY")
APP_PASSWORD = os.getenv("APP_PASSWORD")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Kolkata"))

# Token / context limits
MAX_HISTORY_MESSAGES = 6
MAX_AGENT_STEPS = 4
MAX_OUTPUT_TOKENS = 512        # was 160: thinking tokens count here too, 160 can cut replies off
MAX_TOOL_RESULT_CHARS = 4500
MCP_TIMEOUT = 90               # seconds; covers Render free-tier cold start (~30-50 s)

SYSTEM_PROMPT = """You are a personal expense assistant.

Today: {today}

Rules:
- Use the provided tools for all expense actions.
- When the user says they spent/paid/bought something, use add_expense.
- Preserve the user's description. Do not rewrite or expand it.
- Choose a simple lowercase category.
- Every payment is in ₹ Indian rupees.
- For spending questions, use summarize_expenses, monthly_report, or list_expenses.
- For update/delete, use the expense id. If unknown, call list_expenses first.
- Never invent expense data.
- Keep the final reply to 1-2 short sentences.
"""

# =========================================================
# HELPERS
# =========================================================

def _text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _clean_schema(node):
    """Keep only the schema fields Gemini needs (descriptions dropped to save tokens)."""
    if isinstance(node, list):
        return [_clean_schema(n) for n in node]
    if not isinstance(node, dict):
        return node

    if "anyOf" in node:  # Optional[T] -> T
        options = [o for o in node["anyOf"] if o.get("type") != "null"]
        if options:
            merged = {k: v for k, v in node.items() if k != "anyOf"}
            merged.update(options[0])
            return _clean_schema(merged)

    allowed = {"type", "properties", "items", "enum", "required"}
    out = {}
    for key, value in node.items():
        if key not in allowed:
            continue
        if key == "properties":
            out[key] = {name: _clean_schema(spec) for name, spec in value.items()}
        else:
            out[key] = _clean_schema(value)
    return out


def _to_gemini_tool(tool) -> dict:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    return {"name": tool.name, "description": "", "parameters": _clean_schema(schema)}


def _result_text(result) -> str:
    text = "".join(getattr(c, "text", "") for c in result.content)
    return text[:MAX_TOOL_RESULT_CHARS] if text else "(empty result)"


def _make_llm() -> ChatGoogleGenerativeAI:
    kwargs = {"model": MODEL, "api_key": API_KEY, "max_output_tokens": MAX_OUTPUT_TOKENS}
    if MODEL.startswith("gemini-3"):
        kwargs["thinking_level"] = "low"  # far fewer hidden thinking tokens
    return ChatGoogleGenerativeAI(**kwargs)

# =========================================================
# MCP + AGENT
# =========================================================

async def run_agent(history: list[dict]) -> tuple[str, list[dict]]:
    llm = _make_llm()

    messages = [SystemMessage(content=SYSTEM_PROMPT.format(today=datetime.now(TZ).date().isoformat()))]
    for m in history[-MAX_HISTORY_MESSAGES:]:
        cls = HumanMessage if m["role"] == "user" else AIMessage
        messages.append(cls(content=m["text"]))

    tool_log = []

    async with Client(
        MCP_SERVER_URL,
        auth=MCP_API_KEY,          # sent as "Authorization: Bearer <key>"
        timeout=MCP_TIMEOUT,
        init_timeout=MCP_TIMEOUT,
    ) as mcp:
        tools = [_to_gemini_tool(t) for t in await mcp.list_tools()]
        llm_with_tools = llm.bind_tools(tools)

        for _ in range(MAX_AGENT_STEPS):
            ai = await llm_with_tools.ainvoke(messages)
            messages.append(ai)

            if not ai.tool_calls:
                return (_text(ai.content).strip() or "(no response)"), tool_log

            for call in ai.tool_calls:
                name, args = call["name"], call["args"]
                tool_log.append({"name": name, "args": args if isinstance(args, dict) else {}})
                try:
                    output = _result_text(await mcp.call_tool(name, args))
                except Exception as e:
                    output = f"Error: {e}"
                messages.append(ToolMessage(content=output, tool_call_id=call["id"], name=name))

    return "I couldn't finish that request. Please try again.", tool_log

# =========================================================
# UI
# =========================================================

st.set_page_config(page_title="Expense Tracker", page_icon="💸")
st.title("💸 Expense Tracker")

# ---- config checks (fail closed) ----
missing = [n for n, v in {
    "GOOGLE_API_KEY": API_KEY,
    "MCP_SERVER_URL": MCP_SERVER_URL,
    "MCP_API_KEY": MCP_API_KEY,
    "APP_PASSWORD": APP_PASSWORD,
}.items() if not v]
if missing:
    st.error("Missing settings: " + ", ".join(missing))
    st.stop()

# ---- password gate: the app holds your keys, so don't leave it open ----
if not st.session_state.get("authed"):
    pw = st.text_input("Password", type="password")
    if pw:
        if hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

st.caption(f"{MODEL} + FastMCP")

if "history" not in st.session_state:
    st.session_state.history = []

with st.sidebar:
    st.header("Settings")
    show_tools = st.checkbox("Show tool calls", value=True)
    if st.button("Clear chat"):
        st.session_state.history = []
        st.rerun()
    st.markdown(
        "**Try:**\n\n- Spent 250 on lunch today\n- Paid 1200 for electricity\n"
        "- How much did I spend this month?\n- Show my food expenses\n- Delete the last expense"
    )


def render_tools(tools):
    if show_tools and tools:
        with st.expander(f"🔧 {len(tools)} tool call(s)"):
            for t in tools:
                st.code(f"{t['name']}({json.dumps(t['args'], ensure_ascii=False)})", language="python")


for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["text"])
        render_tools(msg.get("tools"))

if prompt := st.chat_input("Tell me about an expense or ask a question…"):
    st.session_state.history.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking… (first message after idle can take ~40 s)"):
            try:
                reply, tools = asyncio.run(run_agent(st.session_state.history))
            except Exception as e:
                reply, tools = f"⚠️ Something went wrong: `{e}`", []
        st.markdown(reply)
        render_tools(tools)

    st.session_state.history.append({"role": "assistant", "text": reply, "tools": tools})