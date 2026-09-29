"""Token-efficient Streamlit UI for the expense MCP server.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

import asyncio
import json
import os
from datetime import date
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from fastmcp import Client
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_google_genai import ChatGoogleGenerativeAI

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

SERVER_PATH = BASE_DIR / "expense_tracker_server.py"
API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# ---------- Hard token / context limits ----------
MAX_HISTORY_MESSAGES = 6
MAX_AGENT_STEPS = 4
MAX_OUTPUT_TOKENS = 160
MAX_TOOL_RESULT_CHARS = 4500


SYSTEM_PROMPT = """You are a personal expense assistant.

Today: {today}

Rules:
- Use the provided tools for all expense actions.
- When the user says they spent/paid/bought something, use add_expense.
- Preserve the user's description. Do not rewrite or expand it.
- Choose a simple lowercase category.
- Every payment should be in ₹ indian ruppees.
- For spending questions, use summarize_expenses, monthly_report, or list_expenses.
- For update/delete, use the expense id. If unknown, call list_expenses first.
- Never invent expense data.
- Keep the final reply to 1-2 short sentences."""


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
    """Keep only the schema fields needed by Gemini."""
    if isinstance(node, list):
        return [_clean_schema(n) for n in node]

    if not isinstance(node, dict):
        return node

    # Optional[T] -> T
    if "anyOf" in node:
        options = [
            o for o in node["anyOf"]
            if o.get("type") != "null"
        ]
        if options:
            merged = {
                k: v
                for k, v in node.items()
                if k != "anyOf"
            }
            merged.update(options[0])
            return _clean_schema(merged)

    # Descriptions are intentionally removed to reduce prompt tokens.
    allowed = {
        "type",
        "properties",
        "items",
        "enum",
        "required",
    }

    out = {}

    for key, value in node.items():
        if key not in allowed:
            continue

        if key == "properties":
            out[key] = {
                name: _clean_schema(spec)
                for name, spec in value.items()
            }
        else:
            out[key] = _clean_schema(value)

    return out


def _to_gemini_tool(tool) -> dict:
    schema = (
        getattr(tool, "input_schema", None)
        or getattr(tool, "inputSchema", None)
        or {}
    )

    return {
        "name": tool.name,
        "description": "",
        "parameters": _clean_schema(schema),
    }


def _result_text(result) -> str:
    text = "".join(
        getattr(c, "text", "")
        for c in result.content
    )

    if not text:
        return "(empty result)"

    # Hard cap on tool-result tokens sent back to Gemini.
    return text[:MAX_TOOL_RESULT_CHARS]


def _compact_tool_args(args) -> dict:
    """Keep the tool log UI small."""
    return args if isinstance(args, dict) else {}


async def run_agent(history: list[dict]) -> tuple[str, list[dict]]:
    llm = ChatGoogleGenerativeAI(
        model=MODEL,
        api_key=API_KEY,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )

    messages = [
        SystemMessage(
            content=SYSTEM_PROMPT.format(
                today=date.today().isoformat()
            )
        )
    ]

    # Only recent messages are sent to the model.
    recent_history = history[-MAX_HISTORY_MESSAGES:]

    for m in recent_history:
        cls = HumanMessage if m["role"] == "user" else AIMessage
        messages.append(cls(content=m["text"]))

    tool_log = []

    async with Client(SERVER_PATH) as mcp:
        tools = [
            _to_gemini_tool(t)
            for t in await mcp.list_tools()
        ]

        llm_with_tools = llm.bind_tools(tools)

        for _ in range(MAX_AGENT_STEPS):
            ai = await llm_with_tools.ainvoke(messages)
            messages.append(ai)

            if not ai.tool_calls:
                reply = _text(ai.content).strip()
                return reply or "(no response)", tool_log

            for call in ai.tool_calls:
                name = call["name"]
                args = call["args"]

                tool_log.append(
                    {
                        "name": name,
                        "args": _compact_tool_args(args),
                    }
                )

                try:
                    result = await mcp.call_tool(name, args)
                    output = _result_text(result)
                except Exception as e:
                    output = f"Error: {e}"

                messages.append(
                    ToolMessage(
                        content=output,
                        tool_call_id=call["id"],
                        name=name,
                    )
                )

    return (
        "I couldn't finish that request. Please try again."
    ), tool_log


# ---------- UI ----------

st.set_page_config(
    page_title="Expense Tracker",
    page_icon="💸",
)

st.title("💸 Expense Tracker")
st.caption(f"{MODEL} + FastMCP")

if not API_KEY:
    st.error(
        "No API key found. Add GOOGLE_API_KEY=your-key "
        "to a .env file next to app.py."
    )
    st.stop()

if not SERVER_PATH.exists():
    st.error(
        f"Can't find {SERVER_PATH.name}. "
        "Keep it in the same folder as app.py."
    )
    st.stop()

if "history" not in st.session_state:
    st.session_state.history = []

with st.sidebar:
    st.header("Settings")

    show_tools = st.checkbox(
        "Show tool calls",
        value=True,
    )

    if st.button("Clear chat"):
        st.session_state.history = []
        st.rerun()

    st.markdown(
        "**Try:**\n"
        "- Spent 250 on lunch today\n"
        "- Paid 1200 for electricity\n"
        "- How much did I spend this month?\n"
        "- Show my food expenses\n"
        "- Delete the last expense"
    )


def render_tools(tools):
    if show_tools and tools:
        with st.expander(f"🔧 {len(tools)} tool call(s)"):
            for tool in tools:
                st.code(
                    f"{tool['name']}("
                    f"{json.dumps(tool['args'], ensure_ascii=False)}"
                    f")",
                    language="python",
                )


for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["text"])
        render_tools(msg.get("tools"))


if prompt := st.chat_input(
    "Tell me about an expense or ask a question…"
):
    st.session_state.history.append(
        {
            "role": "user",
            "text": prompt,
        }
    )

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                reply, tools = asyncio.run(
                    run_agent(st.session_state.history)
                )
            except Exception as e:
                reply = f"⚠️ Something went wrong: `{e}`"
                tools = []

        st.markdown(reply)
        render_tools(tools)

    st.session_state.history.append(
        {
            "role": "assistant",
            "text": reply,
            "tools": tools,
        }
    )
