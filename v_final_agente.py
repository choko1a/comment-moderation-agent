# =============================================================================
# MODERATION SYSTEM — Streamlit Interface + LM Studio
# =============================================================================

import os
import uuid
from typing import TypedDict, Optional, Annotated
from dotenv import load_dotenv

import streamlit as st
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import BaseMessage, AIMessage

load_dotenv()

# =============================================================================
# PAGE CONFIGURATION
# =============================================================================

st.set_page_config(
    page_title="评论审核",
    page_icon="🛡️",
    layout="centered"
)

st.title("🛡️ 智能评论审核系统")
st.caption("基于 LangGraph 的智能评论审核工作流")

# =============================================================================
# MODEL
# =============================================================================

@st.cache_resource
def load_model():
    return ChatOpenAI(
        base_url="https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        api_key=os.environ["OPENAI_API_KEY"],
        model="qwen3.8-max",
        temperature=0
    )

llm = load_model()

GUIDELINES = """
COMMUNITY POLICIES - Online Course Platform:
1. SPAM: Sharing external links unrelated to the course is not allowed.
2. OFFENSIVE LANGUAGE: Insults or personal attacks will be removed.
3. IRRELEVANT CONTENT: Comments unrelated to the lesson will be flagged.
4. MISINFORMATION: False information about course content will be removed.
5. RESPECT: Criticism must be constructive. Attacks on students or instructors are prohibited.
"""

STATUS_OPTIONS = [
    "Approve",
    "Remove for Spam",
    "Remove for Offensive Language",
    "Remove for Misinformation",
    "Edit for Inappropriate Language"
]

# =============================================================================
# REDUCE MESSAGES
# =============================================================================

def reduce_messages(
    current: list[BaseMessage],
    updates: list[BaseMessage]
) -> list[BaseMessage]:
    current_map = {m.id: m for m in current if hasattr(m, "id")}
    for msg in updates:
        if hasattr(msg, "id"):
            current_map[msg.id] = msg
    original_ids = [m.id for m in current if hasattr(m, "id")]
    new_ids     = [m.id for m in updates
                     if hasattr(m, "id") and m.id not in set(original_ids)]
    return [current_map[i] for i in original_ids + new_ids]

# =============================================================================
# AGENT STATE
# =============================================================================

class AgentState(TypedDict):
    original_comment:      str
    relevant_policies:     Optional[str]
    agent_analysis:        Optional[str]
    moderation_status:      Optional[str]
    final_justification:      Optional[str]
    problem_detected:       Optional[bool]
    human_decision:           Optional[str]
    moderator_justification:  Optional[str]
    moderator_status:         Optional[str]
    human_intervention:       Optional[bool]
    messages:                Annotated[list[BaseMessage], reduce_messages]

# =============================================================================
# NODES
# =============================================================================

def analyzer_agent(state: AgentState) -> AgentState:
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", """You are a content moderation assistant. Write all explanations in Simplified Chinese. Keep the required machine-readable markers and status values exactly as specified.
Analyze the comment and respond with:
1. Write 分类: 正面, 中性, or 存在问题
2. Brief reason (1-2 sentences)
3. On the last line, write only: PROBLEM: YES or PROBLEM: NO"""),
            ("human", "Comment: {comment}")
        ])
        response = (prompt | llm).invoke({"comment": state["original_comment"]})
        analysis  = response.content
        problem = "PROBLEM: YES" in analysis.upper()
        msg = AIMessage(content=analysis, id=f"analysis-{state['original_comment'][:20]}")
        return {**state, "agent_analysis": analysis,
                "problem_detected": problem, "messages": [msg]}
    except Exception as e:
        msg = AIMessage(content="分析失败。", id="analysis-error")
        return {**state, "agent_analysis": f"处理失败：{e}",
                "problem_detected": False, "messages": [msg]}


def policy_researcher_agent(state: AgentState) -> AgentState:
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", """You are a community policy specialist. Write all explanations in Simplified Chinese. Keep the required machine-readable markers and status values exactly as specified.
Identify WHICH policies were violated based on these guidelines:

{guidelines}

List only the violated policies, concisely and objectively."""),
            ("human", "Analysis: {analysis}\nComment: {comment}")
        ])
        response = (prompt | llm).invoke({
            "guidelines": GUIDELINES,
            "analysis":    state["agent_analysis"],
            "comment": state["original_comment"]
        })
        msg = AIMessage(content=response.content, id="policies-found")
        return {**state, "relevant_policies": response.content, "messages": [msg]}
    except Exception as e:
        fallback = f"查询社区规则失败：{e}"
        msg = AIMessage(content=fallback, id="policies-error")
        return {**state, "relevant_policies": fallback, "messages": [msg]}


def reviewer_agent(state: AgentState) -> AgentState:
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", """You are a senior moderation reviewer. Write all explanations in Simplified Chinese. Keep the required machine-readable markers and status values exactly as specified.
Generate:
1. STATUS: choose exactly one option: "Approve", "Remove for Spam",
   "Remove for Offensive Language", "Remove for Misinformation" or
   "Edit for Inappropriate Language"
2. JUSTIFICATION: a clear explanation in 2-3 sentences, written in Simplified Chinese."""),
            ("human", "Comment: {comment}\nAnalysis: {analysis}\nPolicies: {policies}")
        ])
        response = (prompt | llm).invoke({
            "comment": state["original_comment"],
            "analysis":    state["agent_analysis"],
            "policies":  state.get("relevant_policies", "None")
        })
        content = response.content
        status   = "Remove for Spam"
        for option in STATUS_OPTIONS:
            if option.lower() in content.lower():
                status = option
                break
        msg = AIMessage(content=content, id="final-review")
        return {**state, "moderation_status": status,
                "final_justification": content, "messages": [msg]}
    except Exception as e:
        msg = AIMessage(content="复核失败。", id="review-error")
        return {**state, "moderation_status": "Remove for Spam",
                "final_justification": f"处理失败：{e}", "messages": [msg]}


def approve_directly(state: AgentState) -> AgentState:
    msg = AIMessage(content="已自动批准。", id="auto-approval")
    return {
        **state,
        "moderation_status":  "Approved",
        "final_justification":  "已自动批准，未发现问题。",
        "relevant_policies": "未违反社区规则。",
        "human_intervention":   False,
        "messages":            [msg]
    }


def execute_final_action(state: AgentState) -> AgentState:
    intervened  = bool(state.get("moderator_justification") or state.get("moderator_status"))
    status = state.get("moderator_status")        or state.get("moderation_status")
    justification   = state.get("moderator_justification") or state.get("final_justification")
    if state.get("human_decision") == "canceled":
        status = "Approved"
        justification   = "审核员已取消人工智能建议的操作，并批准此评论。"
        intervened  = True
    msg = AIMessage(content=justification, id="final-review")
    return {
        **state,
        "moderation_status": status,
        "final_justification": justification,
        "human_intervention":  intervened,
        "messages":           [msg]
    }

def router(state: AgentState) -> str:
    return "research_policies" if state.get("problem_detected") else "approve"

# =============================================================================
# GRAPH
# =============================================================================

@st.cache_resource
def create_graph():
    checkpointer = MemorySaver()
    graph        = StateGraph(AgentState)

    graph.add_node("analyzer",          analyzer_agent)
    graph.add_node("research_policies", policy_researcher_agent)
    graph.add_node("reviewer",             reviewer_agent)
    graph.add_node("approve",             approve_directly)
    graph.add_node("execute_final_action", execute_final_action)

    graph.set_entry_point("analyzer")
    graph.add_conditional_edges(
        "analyzer", router,
        {"research_policies": "research_policies", "approve": "approve"}
    )
    graph.add_edge("research_policies", "reviewer")
    graph.add_edge("reviewer",             "execute_final_action")
    graph.add_edge("approve",             END)
    graph.add_edge("execute_final_action", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["execute_final_action"]
    )

app = create_graph()

# =============================================================================
# SESSION STATE
# =============================================================================

if "thread_id"      not in st.session_state: st.session_state.thread_id      = None
if "awaiting_review"     not in st.session_state: st.session_state.awaiting_review     = False
if "agent_state"  not in st.session_state: st.session_state.agent_state  = None
if "history"      not in st.session_state: st.session_state.history      = []
if "processing"    not in st.session_state: st.session_state.processing    = False

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def localize_text(text: str) -> str:
    translations = {
        "Remove for Offensive Language": "删除：冒犯性语言",
        "Edit for Inappropriate Language": "编辑：用语不当",
        "Remove for Misinformation": "删除：虚假信息",
        "Remove for Spam": "删除：垃圾广告",
        "Approved": "已批准",
        "Approve": "批准",
        "PROBLEM: YES": "发现问题：是",
        "PROBLEM: NO": "发现问题：否",
        "Classification:": "分类：",
        "PROBLEMATIC": "存在问题",
        "POSITIVE": "正面",
        "NEUTRAL": "中性",
        "JUSTIFICATION:": "审核理由：",
        "STATUS:": "审核状态：",
        "OFFENSIVE LANGUAGE": "冒犯性语言",
        "IRRELEVANT CONTENT": "无关内容",
        "MISINFORMATION": "虚假信息",
        "RESPECT": "尊重他人",
        "SPAM": "垃圾广告",
        "Automatically approved. No issues detected.": "已自动批准，未发现问题。",
        "No policies violated.": "未违反社区规则。",
        "The moderator canceled the action recommended by AI.": "审核员已取消人工智能建议的操作，并批准此评论。",
        "🤖 AI": "🤖 人工智能",
        "✏️ Moderator": "✏️ 审核员",
    }
    for original, translated in translations.items():
        text = text.replace(original, translated)
    return text


def status_icon(status: str) -> str:
    if "Approve" in status:    return "🟢"
    if "Remove" in status:    return "🔴"
    if "Edit"  in status:    return "🟡"
    return "⚪"

def start_moderation(comment: str):
    thread_id = str(uuid.uuid4())
    config    = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "original_comment":     comment,
        "relevant_policies":    None,
        "agent_analysis":       None,
        "moderation_status":     None,
        "final_justification":     None,
        "problem_detected":      None,
        "human_decision":          None,
        "moderator_justification": None,
        "moderator_status":        None,
        "human_intervention":      None,
        "messages":               [],
    }

    executed_nodes = []
    for event in app.stream(initial_state, config=config):
        for node in event:
            executed_nodes.append(node)

    snapshot      = app.get_state(config)
    state        = snapshot.values
    needs_human = "execute_final_action" in snapshot.next

    st.session_state.thread_id     = thread_id
    st.session_state.awaiting_review    = needs_human
    st.session_state.agent_state = state
    st.session_state.executed_nodes = executed_nodes

    if not needs_human:
        st.session_state.history.append({
            "comment":    comment,
            "status":        state.get("moderation_status"),
            "justification": state.get("final_justification"),
            "source":        "🤖 人工智能",
            "thread_id":     thread_id
        })


def apply_decision(decision: str, new_status: str = None, new_justification: str = None):
    config = {"configurable": {"thread_id": st.session_state.thread_id}}

    new_values = {"human_decision": decision}
    if new_status: new_values["moderator_status"]        = new_status
    if new_justification:   new_values["moderator_justification"] = new_justification

    app.update_state(config, new_values)

    for event in app.stream(None, config=config):
        pass

    final_state = app.get_state(config).values
    source = "✏️ 审核员" if final_state.get("human_intervention") else "🤖 人工智能"

    st.session_state.history.append({
        "comment":    final_state.get("original_comment"),
        "status":        final_state.get("moderation_status"),
        "justification": final_state.get("final_justification"),
        "source":        source,
        "thread_id":     st.session_state.thread_id
    })

    st.session_state.awaiting_review    = False
    st.session_state.agent_state = None
    st.session_state.thread_id     = None

# =============================================================================
# INTERFACE
# =============================================================================

# ── Comment input ─────────────────────────────────────────────────────
st.subheader("📝 新评论")

example_comments = [
    "请选择示例，或在下方输入评论……",
    "这节课太棒了！我终于理解指针了。",
    "访问 www.spam.com，免费领取证书！",
    "这个老师就是个笨蛋，什么都讲不清楚！",
    "这节课的内容已经过时了，里面的信息有错误。",
]

example = st.selectbox("💡 快速示例", example_comments)

comment_input = st.text_area(
    "学员评论：",
    value="" if example == example_comments[0] else example,
    height=100,
    placeholder="请输入评论，或从上方选择一个示例……"
)

col1, col2 = st.columns([1, 4])
with col1:
    analyze = st.button(
        "🔍 开始分析",
        disabled=st.session_state.awaiting_review or st.session_state.processing,
        use_container_width=True
    )

if analyze and comment_input.strip():
    with st.spinner("🤖 正在分析评论……"):
        start_moderation(comment_input.strip())
    st.rerun()

# ── Human review panel ──────────────────────────────────────────────────
if st.session_state.awaiting_review and st.session_state.agent_state:
    state = st.session_state.agent_state

    st.divider()
    st.subheader("🛑 人工审核")

    with st.container(border=True):
        st.markdown(f"**💬 评论：**\n> {state.get('original_comment','')}")

        col_a, col_b = st.columns(2)
        with col_a:
            with st.expander("📊 智能分析", expanded=True):
                st.write(localize_text(state.get("agent_analysis", "")))
        with col_b:
            with st.expander("📋 违反的社区规则", expanded=True):
                st.write(localize_text(state.get("relevant_policies", "")))

        ai_status = state.get("moderation_status", "")
        st.info(f"{status_icon(ai_status)} **审核建议：** {localize_text(ai_status)}")
        st.write("**审核理由：**", localize_text(state.get("final_justification", "")))

    st.subheader("👤 您的决定")

    tab1, tab2, tab3 = st.tabs(["✅ 确认建议", "↩️ 取消建议", "✏️ 修改决定"])

    with tab1:
        st.write("直接确认人工智能的审核建议，不作修改。")
        if st.button("✅ 确认审核建议", use_container_width=True):
            with st.spinner("正在应用审核决定……"):
                apply_decision("confirmed")
            st.rerun()

    with tab2:
        st.write("取消人工智能建议的操作，并批准此评论。")
        if st.button("↩️ 强制批准", use_container_width=True, type="secondary"):
            with st.spinner("正在应用审核决定……"):
                apply_decision("canceled")
            st.rerun()

    with tab3:
        st.write("确认前，可以修改审核状态和审核理由。")
        new_status = st.selectbox(
            "新的审核状态：",
            STATUS_OPTIONS,
            format_func=localize_text,
            index=STATUS_OPTIONS.index(ai_status) if ai_status in STATUS_OPTIONS else 0
        )
        new_justification = st.text_area(
            "新的审核理由：",
            value=localize_text(state.get("final_justification", "")),
            height=120
        )
        if st.button("✏️ 保存并确认", use_container_width=True, type="primary"):
            with st.spinner("正在保存修改……"):
                apply_decision("confirmed", new_status, new_justification)
            st.rerun()

# ── History ─────────────────────────────────────────────────────────────────
if st.session_state.history:
    st.divider()
    st.subheader("📋 审核历史")

    for i, item in enumerate(reversed(st.session_state.history)):
        with st.expander(
            f"{status_icon(item['status'])} {localize_text(item['status'])} — \"{item['comment'][:50]}...\"",
            expanded=(i == 0)
        ):
            st.markdown(f"**评论：** {item['comment']}")
            st.markdown(f"**审核状态：** {localize_text(item['status'])}")
            st.markdown(f"**审核理由：** {localize_text(item['justification'])}")
            st.markdown(f"**决定来源：** {localize_text(item['source'])}")
            st.caption(f"审核编号：{item['thread_id']}")

    if st.button("🗑️ 清空历史"):
        st.session_state.history = []
        st.rerun()
