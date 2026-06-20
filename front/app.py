"""知识库 RAG 系统 — Chat 对话主页。"""
from __future__ import annotations

import streamlit as st

from config import API_BASE_URL, USER_ID
from api_client import (
    check_health,
    send_chat_stream,
    create_conversation,
    list_conversations,
    get_messages,
    delete_conversation,
)

st.set_page_config(page_title="知识库 RAG 系统", page_icon="📚", layout="wide")

# ============================================================
# Session State 初始化
# ============================================================
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "messages" not in st.session_state:
    st.session_state.messages = []
if "backend_online" not in st.session_state:
    st.session_state.backend_online = False


# ============================================================
# 侧边栏
# ============================================================
with st.sidebar:
    st.title("📚 知识库 RAG")

    # 后端状态
    backend_online = check_health()
    st.session_state.backend_online = backend_online
    if backend_online:
        st.success(f"✅ 后端已连接 ({API_BASE_URL})")
    else:
        st.error(f"❌ 后端不可用 ({API_BASE_URL})")
        st.info("请先启动后端: `uv run uvicorn main:app --reload`")

    st.divider()

    # 新建对话
    if st.button("➕ 新建对话", use_container_width=True):
        try:
            resp = create_conversation(USER_ID)
            st.session_state.session_id = resp["data"]["session_id"]
            st.session_state.messages = []
            st.rerun()
        except Exception as e:
            st.error(f"创建会话失败: {e}")

    st.divider()

    # 当前会话信息
    sid = st.session_state.session_id
    if sid:
        st.caption(f"当前会话: `{sid[:8]}...`")
    else:
        st.caption("尚未选择会话")

    # 会话列表
    st.subheader("📋 历史会话")
    try:
        resp = list_conversations(USER_ID)
        convs = resp.get("data", {}).get("conversations", [])
        for conv in convs:
            cid = conv["id"]
            title = conv.get("title", "") or f"会话 {cid[:8]}"
            c1, c2 = st.columns([4, 1])
            with c1:
                label = f"{'📌' if cid == sid else '💬'} {title}"
                if st.button(label, key=f"sel_{cid}", use_container_width=True):
                    st.session_state.session_id = cid
                    try:
                        msg_resp = get_messages(cid)
                        msgs = msg_resp.get("data", {}).get("messages", [])
                        st.session_state.messages = [
                            {"role": m["role"], "content": m["content"]} for m in msgs
                        ]
                        st.rerun()
                    except Exception as e:
                        st.error(f"加载消息失败: {e}")
            with c2:
                if st.button("🗑️", key=f"del_{cid}", help=f"删除 {title}"):
                    try:
                        delete_conversation(cid)
                        if st.session_state.session_id == cid:
                            st.session_state.session_id = None
                            st.session_state.messages = []
                        st.rerun()
                    except Exception as e:
                        st.error(f"删除失败: {e}")
    except Exception:
        st.caption("无法加载会话列表")

# ============================================================
# 主区域 — Chat
# ============================================================
st.title("💬 知识库问答")

# 显示消息历史
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 输入框
if prompt := st.chat_input("请输入问题，例如：知识库中有哪些文档？", disabled=not backend_online):
    # 添加用户消息
    st.session_state.messages.append({"role": "human", "content": prompt})
    with st.chat_message("human"):
        st.markdown(prompt)

    # 流式获取 AI 回复
    with st.chat_message("ai"):
        placeholder = st.empty()
        full_response = ""
        tool_logs = []

        try:
            for event in send_chat_stream(prompt, st.session_state.session_id, USER_ID):
                ev_type = event.get("event", "")

                if ev_type == "session_created":
                    st.session_state.session_id = event.get("session_id")

                elif ev_type == "token":
                    full_response += event.get("data", "")
                    # 渲染时显示工具日志摘要
                    display = full_response
                    if tool_logs:
                        log_text = "\n\n---\n**🔧 工具调用记录：**\n" + "\n".join(
                            f"- {t}" for t in tool_logs
                        )
                        display += log_text
                    placeholder.markdown(display)

                elif ev_type == "done":
                    # 合并最终答案（done 事件的 data 包含完整答案）
                    data = event.get("data", "")
                    if isinstance(data, dict):
                        final = data.get("response", "") or data.get("output", "") or str(data)
                    else:
                        final = str(data) if data else full_response
                    if final:
                        full_response = final

                elif ev_type == "tool_start":
                    tname = event.get("tool", "unknown")
                    tdata = event.get("data", "")
                    tool_logs.append(f"🔍 **{tname}**: {tdata}")

                elif ev_type == "tool_end":
                    tname = event.get("tool", "unknown")
                    tdata = event.get("data", "")
                    tool_logs.append(f"✅ **{tname}** 完成")

                elif ev_type == "error":
                    full_response = event.get("data", "发生未知错误")
                    st.error(full_response)

            # 最终渲染
            display = full_response
            if tool_logs:
                log_text = "\n\n---\n**🔧 工具调用记录：**\n" + "\n".join(
                    f"- {t}" for t in tool_logs
                )
                display += log_text
            placeholder.markdown(display)

        except Exception as e:
            placeholder.error(f"请求失败: {e}")
            full_response = f"**错误**: {e}"

        # 保存 AI 回复
        if full_response:
            st.session_state.messages.append({"role": "ai", "content": full_response})

# 无会话时的引导
if not st.session_state.messages and backend_online:
    st.info("👈 从侧边栏创建新会话开始对话，或选择已有会话")
