"""知识库 RAG 系统 — Chat 对话主页。"""
from __future__ import annotations

import streamlit as st

from config import API_BASE_URL, USER_ID
from api_client import (
    check_health,
    send_chat_stream,
    list_conversations,
    get_messages,
    delete_conversation,
    toggle_pin,
)

PAGE_SIZE = 20

st.set_page_config(page_title="知识库 RAG 系统", page_icon="📚", layout="wide")

if "main_session_id" not in st.session_state:
    st.session_state.main_session_id = None
if "main_messages" not in st.session_state:
    st.session_state.main_messages = []
if "backend_online" not in st.session_state:
    st.session_state.backend_online = False
if "mode" not in st.session_state:
    st.session_state.mode = "agent"
if "main_confirm_delete_cid" not in st.session_state:
    st.session_state.main_confirm_delete_cid = None
if "main_convs" not in st.session_state:
    st.session_state.main_convs = []
if "main_convs_offset" not in st.session_state:
    st.session_state.main_convs_offset = 0
if "main_convs_has_more" not in st.session_state:
    st.session_state.main_convs_has_more = True
if "main_convs_loaded" not in st.session_state:
    st.session_state.main_convs_loaded = False


def _load_messages(sid: str) -> list[dict] | None:
    try:
        msg_resp = get_messages(sid)
    except Exception:
        return None
    data = msg_resp.get("data") or {}
    msgs = data.get("messages", [])
    return [
        {"role": "user" if m.get("role") == "human" else "assistant", "content": m.get("content", "")}
        for m in msgs
    ]


def _refresh_convs(reset: bool = False):
    if reset:
        st.session_state.main_convs_offset = 0
        st.session_state.main_convs = []
        st.session_state.main_convs_has_more = True
    try:
        resp = list_conversations(USER_ID, offset=st.session_state.main_convs_offset, limit=PAGE_SIZE)
        convs = resp.get("data", {}).get("conversations", [])
        if reset:
            st.session_state.main_convs = convs
        else:
            existing_ids = {c.get("session_id") or c.get("id") for c in st.session_state.main_convs}
            new_convs = [c for c in convs if (c.get("session_id") or c.get("id")) not in existing_ids]
            st.session_state.main_convs.extend(new_convs)
        st.session_state.main_convs_offset += len(convs)
        st.session_state.main_convs_has_more = len(convs) == PAGE_SIZE
        st.session_state.main_convs_loaded = True
    except Exception as e:
        st.error(f"获取会话列表失败: {e}")
        st.session_state.main_convs_loaded = False


def _do_delete_conversation(cid: str):
    delete_conversation(cid)
    if st.session_state.main_session_id == cid:
        st.session_state.main_session_id = None
        st.session_state.main_messages = []
    st.session_state.main_confirm_delete_cid = None
    _refresh_convs(reset=True)


@st.dialog("确认删除会话")
def confirm_delete_dialog(cid: str):
    convs = st.session_state.main_convs
    titles = [c for c in convs if (c.get("session_id") or c.get("id")) == cid]
    title = titles[0].get("session_title", cid[:8]) if titles else cid[:8]
    st.write(f"确定要删除会话「{title}」吗？")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("确认删除", type="primary", use_container_width=True):
            try:
                _do_delete_conversation(cid)
                st.rerun()
            except Exception as e:
                st.error(f"删除失败: {e}")
    with c2:
        if st.button("取消", use_container_width=True):
            st.session_state.main_confirm_delete_cid = None
            st.rerun()


if st.session_state.main_confirm_delete_cid:
    confirm_delete_dialog(st.session_state.main_confirm_delete_cid)

with st.sidebar:
    st.title("📚 知识库 RAG")

    backend_online = check_health()
    st.session_state.backend_online = backend_online
    if backend_online:
        st.success(f"✅ 后端已连接 ({API_BASE_URL})")
    else:
        st.error(f"❌ 后端不可用 ({API_BASE_URL})")
        st.info("请先启动后端: `uv run uvicorn main:app --reload`")

    st.divider()

    mode_options = {"Agent 工具链": "agent", "直接 RAG 检索": "rag"}
    mode_label = st.selectbox(
        "🔍 检索模式",
        options=list(mode_options.keys()),
        index=list(mode_options.values()).index(st.session_state.mode),
        key="mode_select",
    )
    st.session_state.mode = mode_options[mode_label]

    st.divider()

    if st.button("➕ 新建对话", use_container_width=True):
        st.session_state.main_session_id = None
        st.session_state.main_messages = []
        st.rerun()

    st.divider()

    sid = st.session_state.main_session_id
    if sid:
        sid_title = sid[:8] + "..."
        for c in st.session_state.main_convs:
            if (c.get("session_id") or c.get("id")) == sid:
                t = c.get("session_title") or c.get("title", "")
                if t:
                    sid_title = t
                break
        st.caption(f"当前会话: {sid_title}")
    else:
        st.caption("尚未选择会话")

    st.subheader("📋 历史会话")

    # 首次加载强制刷新 + 手动刷新按钮
    col_title, col_refresh = st.columns([4, 1])
    with col_refresh:
        if st.button("🔄", key="refresh_convs_btn", help="刷新列表"):
            _refresh_convs(reset=True)
            st.rerun()

    if not st.session_state.main_convs_loaded:
        _refresh_convs(reset=True)

    convs = st.session_state.main_convs

    if not convs:
        st.caption("暂无会话记录")
    else:
        for i, conv in enumerate(convs):
            cid = conv.get("session_id") or conv.get("id", "")
            title = conv.get("session_title") or conv.get("title", "") or f"会话 {cid[:8]}"
            is_top = conv.get("is_top", 0)
            is_active = cid == sid

            c_pin, c_title, c_del = st.columns([0.5, 3.5, 1])
            with c_pin:
                pin_icon = "📌" if is_top else "📍"
                if st.button(pin_icon, key=f"pin_{cid}", help="取消置顶" if is_top else "置顶"):
                    try:
                        toggle_pin(cid, not is_top)
                        _refresh_convs(reset=True)
                        st.rerun()
                    except Exception as e:
                        st.error(f"操作失败: {e}")
            with c_title:
                if st.button(title, key=f"sel_{cid}", use_container_width=True,
                             type="primary" if is_active else "secondary"):
                    msgs = _load_messages(cid)
                    if msgs is None:
                        st.error("加载消息失败")
                    else:
                        st.session_state.main_session_id = cid
                        st.session_state.main_messages = msgs
                        st.rerun()
            with c_del:
                if st.button("🗑️", key=f"del_{cid}", help=f"删除 {title}"):
                    st.session_state.main_confirm_delete_cid = cid
                    st.rerun()

        if st.session_state.main_convs_has_more:
            if st.button("📥 加载更多...", use_container_width=True):
                _refresh_convs(reset=False)
                st.rerun()

st.title("💬 知识库问答")

for msg in st.session_state.main_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input(
    "请输入问题，例如：知识库中有哪些文档？",
    disabled=not st.session_state.backend_online,
):
    st.session_state.main_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("🔍 检索中...")
        full_response = ""
        references = []

        try:
            for event in send_chat_stream(prompt, st.session_state.main_session_id, USER_ID, st.session_state.mode):
                ev_type = event.get("event", "")

                if ev_type == "session_created":
                    st.session_state.main_session_id = event.get("session_id")

                elif ev_type == "token":
                    full_response += event.get("data", "")
                    placeholder.markdown(full_response)

                elif ev_type == "done":
                    data = event.get("data", "")
                    final = str(data) if data else full_response
                    if final:
                        full_response = final

                elif ev_type == "references":
                    references = event.get("data", [])

                elif ev_type == "error":
                    full_response = event.get("data", "发生未知错误")
                    placeholder.markdown(f"❌ {full_response}")

            display = full_response
            if references:
                ref_text = "\n\n---\n**📚 参考来源：**\n" + "\n".join(f"- {s}" for s in references)
                display += ref_text
                full_response += ref_text
            placeholder.markdown(display)

        except Exception as e:
            placeholder.error(f"请求失败: {e}")
            full_response = f"**错误**: {e}"

        if full_response:
            st.session_state.main_messages.append({"role": "assistant", "content": full_response})
            _refresh_convs(reset=True)
            st.rerun()

if not st.session_state.main_messages and st.session_state.backend_online:
    if st.session_state.main_session_id:
        st.info("该会话暂无消息，在下方输入框开始对话")
    else:
        st.info("👈 直接输入问题即自动创建新会话，或从侧边栏选择已有会话")
