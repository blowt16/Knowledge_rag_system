"""会话管理页 — 查看、删除、置顶历史会话。"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from config import USER_ID
from api_client import (
    list_conversations,
    get_messages,
    delete_conversation,
    clear_conversations,
    toggle_pin,
)

PAGE_SIZE = 20

st.set_page_config(page_title="会话管理", page_icon="💬", layout="wide")


def refresh_convs(reset: bool = False):
    if reset:
        st.session_state.convs_offset = 0
        st.session_state.convs = []
        st.session_state.convs_has_more = True
        st.session_state.msgs_cache = {}
    try:
        resp = list_conversations(USER_ID, offset=st.session_state.convs_offset, limit=PAGE_SIZE)
        convs = resp.get("data", {}).get("conversations", [])
        if reset:
            st.session_state.convs = convs
        else:
            existing_ids = {c.get("session_id") or c.get("id") for c in st.session_state.convs}
            new_convs = [c for c in convs if (c.get("session_id") or c.get("id")) not in existing_ids]
            st.session_state.convs.extend(new_convs)
        st.session_state.convs_offset += len(convs)
        st.session_state.convs_has_more = len(convs) == PAGE_SIZE
        st.session_state.convs_loaded = True
    except Exception as e:
        st.session_state.convs = []
        st.session_state.convs_loaded = False
        st.error(f"获取会话列表失败: {e}")


if "convs" not in st.session_state:
    st.session_state.convs = []
if "convs_loaded" not in st.session_state:
    st.session_state.convs_loaded = False
if "convs_offset" not in st.session_state:
    st.session_state.convs_offset = 0
if "convs_has_more" not in st.session_state:
    st.session_state.convs_has_more = True
if "confirm_delete_conv_id" not in st.session_state:
    st.session_state.confirm_delete_conv_id = None
if "confirm_clear_all" not in st.session_state:
    st.session_state.confirm_clear_all = False
if "msgs_cache" not in st.session_state:
    st.session_state.msgs_cache = {}  # {cid: [messages]}
if "msgs_loading" not in st.session_state:
    st.session_state.msgs_loading = set()  # set of cids being loaded


# ============================================================
# 删除单条确认弹窗
# ============================================================
@st.dialog("确认删除会话")
def confirm_delete_dialog(cid: str, title: str):
    st.write(f"确定要删除会话「{title}」吗？")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("确认删除", type="primary", use_container_width=True):
            try:
                delete_conversation(cid)
                st.session_state.confirm_delete_conv_id = None
                refresh_convs(reset=True)
                st.rerun()
            except Exception as e:
                st.error(f"删除失败: {e}")
    with c2:
        if st.button("取消", use_container_width=True):
            st.session_state.confirm_delete_conv_id = None
            st.rerun()


if st.session_state.confirm_delete_conv_id:
    cid = st.session_state.confirm_delete_conv_id
    titles = [c for c in st.session_state.convs if (c.get("session_id") or c.get("id")) == cid]
    confirm_delete_dialog(cid, titles[0].get("session_title", cid[:8]) if titles else cid[:8])


# ============================================================
# 清空全部确认弹窗
# ============================================================
@st.dialog("确认清空所有会话")
def confirm_clear_dialog():
    st.write("确定要清空所有会话记录吗？此操作不可撤销！")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("确认清空", type="primary", use_container_width=True):
            try:
                clear_conversations(USER_ID)
                st.session_state.confirm_clear_all = False
                refresh_convs(reset=True)
                st.rerun()
            except Exception as e:
                st.error(f"清空失败: {e}")
    with c2:
        if st.button("取消", use_container_width=True):
            st.session_state.confirm_clear_all = False
            st.rerun()


if st.session_state.confirm_clear_all:
    confirm_clear_dialog()

# ============================================================
# 主页面
# ============================================================
st.title("💬 会话管理")

col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    st.subheader("📋 会话列表")
with col2:
    if st.button("🔄 刷新", use_container_width=True):
        refresh_convs(reset=True)
        st.rerun()
with col3:
    if st.button("🗑️ 清空全部", type="secondary", use_container_width=True):
        st.session_state.confirm_clear_all = True
        st.rerun()

if not st.session_state.convs_loaded:
    refresh_convs(reset=True)

convs = st.session_state.convs

if not convs:
    st.info("暂无会话记录")
else:
    for i, conv in enumerate(convs):
        cid = conv.get("session_id") or conv.get("id", "")
        title = conv.get("session_title") or conv.get("title", "") or "未命名"
        is_top = conv.get("is_top", 0)

        with st.expander(
            f"{'📌' if is_top else '💬'} {title}  |  {conv.get('last_chat_time', '')}",
            expanded=False,
        ):
            c1, c2, c3 = st.columns([5, 1, 1])
            with c1:
                st.caption(f"会话 ID: `{cid}`")
            with c2:
                pin_label = "📌 取消置顶" if is_top else "📍 置顶"
                if st.button(pin_label, key=f"pin_{cid}"):
                    try:
                        toggle_pin(cid, not is_top)
                        st.session_state.msgs_cache.pop(cid, None)
                        refresh_convs(reset=True)
                        st.rerun()
                    except Exception as e:
                        st.error(f"操作失败: {e}")
            with c3:
                if st.button("🗑️ 删除", key=f"delconv_{cid}"):
                    st.session_state.confirm_delete_conv_id = cid
                    st.rerun()

            # 按需加载消息
            if cid in st.session_state.msgs_cache:
                msgs = st.session_state.msgs_cache[cid]
                if msgs:
                    st.markdown("---")
                    for msg in msgs:
                        role_label = "👤 用户" if msg.get("role") == "human" else "🤖 AI"
                        st.caption(role_label)
                        st.markdown(msg.get("content", ""))
                        st.markdown("---")
                else:
                    st.caption("暂无消息")
            else:
                if st.button("📩 查看消息", key=f"loadmsg_{cid}"):
                    try:
                        msg_resp = get_messages(cid)
                        msgs = msg_resp.get("data", {}).get("messages", [])
                        st.session_state.msgs_cache[cid] = msgs
                        st.rerun()
                    except Exception as e:
                        st.error(f"加载消息失败: {e}")

    # 加载更多
    if st.session_state.convs_has_more:
        if st.button("📥 加载更多...", use_container_width=True):
            refresh_convs(reset=False)
            st.rerun()
