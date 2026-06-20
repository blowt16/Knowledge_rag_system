"""会话管理页 — 查看、删除历史会话。"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from config import USER_ID
from api_client import (
    list_conversations,
    get_messages,
    delete_conversation,
    clear_conversations,
)

st.set_page_config(page_title="会话管理", page_icon="💬", layout="wide")


def refresh_convs():
    try:
        resp = list_conversations(USER_ID)
        st.session_state.convs = resp.get("data", {}).get("conversations", [])
        st.session_state.convs_loaded = True
    except Exception as e:
        st.session_state.convs = []
        st.session_state.convs_loaded = False
        st.error(f"获取会话列表失败: {e}")


if "convs" not in st.session_state:
    st.session_state.convs = []
if "convs_loaded" not in st.session_state:
    st.session_state.convs_loaded = False

st.title("💬 会话管理")

col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    st.subheader("📋 会话列表")
with col2:
    if st.button("🔄 刷新", use_container_width=True):
        refresh_convs()
with col3:
    if st.button("🗑️ 清空全部", type="secondary", use_container_width=True):
        try:
            clear_conversations(USER_ID)
            st.success("所有会话已清空")
            refresh_convs()
            st.rerun()
        except Exception as e:
            st.error(f"清空失败: {e}")

if not st.session_state.convs_loaded:
    refresh_convs()

convs = st.session_state.convs

if not convs:
    st.info("暂无会话记录")
else:
    for i, conv in enumerate(convs):
        with st.expander(
            f"💬 {conv.get('title', '') or '未命名'}  |  {conv.get('updated_at', '')}",
            expanded=False,
        ):
            c1, c2 = st.columns([6, 1])
            with c1:
                st.caption(f"会话 ID: `{conv['id']}`")
                st.caption(f"创建时间: {conv.get('created_at', '?')}")
            with c2:
                if st.button("🗑️ 删除", key=f"delconv_{i}"):
                    try:
                        delete_conversation(conv["id"])
                        st.success("会话已删除")
                        refresh_convs()
                        st.rerun()
                    except Exception as e:
                        st.error(f"删除失败: {e}")

            # 加载消息
            try:
                msg_resp = get_messages(conv["id"])
                msgs = msg_resp.get("data", {}).get("messages", [])
                if msgs:
                    st.markdown("---")
                    for msg in msgs:
                        role_label = "👤 用户" if msg["role"] == "human" else "🤖 AI"
                        st.caption(f"{role_label} — {msg.get('created_at', '')}")
                        st.markdown(msg["content"])
                        st.markdown("---")
                else:
                    st.caption("暂无消息")
            except Exception as e:
                st.error(f"加载消息失败: {e}")
