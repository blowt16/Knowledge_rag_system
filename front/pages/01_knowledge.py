"""知识库管理页 — 文档上传、列表、删除。"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

import pandas as pd
import streamlit as st

from config import USER_ID
from api_client import (
    upload_document,
    list_documents,
    delete_document_by_md5,
    delete_document_by_filename,
    clear_knowledge,
    upload_zip,
    get_zip_task_status,
)

st.set_page_config(page_title="知识库管理", page_icon="📦", layout="wide")

ALLOWED_SINGLE = ["txt", "pdf", "md", "pptx", "docx"]
ALLOWED_ZIP = ["zip", "tar", "gz", "rar"]


def refresh_docs():
    try:
        resp = list_documents(USER_ID)
        st.session_state.docs = resp.get("data", {}).get("documents", [])
        st.session_state.docs_loaded = True
    except Exception as e:
        st.session_state.docs = []
        st.session_state.docs_loaded = False
        st.error(f"获取文档列表失败: {e}")


if "docs" not in st.session_state:
    st.session_state.docs = []
if "docs_loaded" not in st.session_state:
    st.session_state.docs_loaded = False

st.title("📦 知识库管理")

# ============================================================
# 单文件上传
# ============================================================
st.subheader("📄 上传文档")
uploaded_file = st.file_uploader(
    "选择文件（txt/pdf/md/pptx/docx，最大 30MB）",
    type=ALLOWED_SINGLE,
    key="single_uploader",
)
if uploaded_file is not None:
    file_size_mb = len(uploaded_file.getvalue()) / (1024 * 1024)
    st.caption(f"文件大小: {file_size_mb:.1f} MB")
    if st.button("🚀 上传到知识库", key="btn_upload_single"):
        with st.spinner("正在处理文档..."):
            try:
                result = upload_document(
                    uploaded_file.getvalue(), uploaded_file.name, USER_ID
                )
                data = result.get("data", {})
                status = data.get("status", "")
                if status == "duplicate":
                    st.warning(f"⚠️ 文件已存在: {data.get('message', '')}")
                else:
                    st.success(f"✅ 上传成功: {uploaded_file.name}")
                    st.json(data)
                refresh_docs()
            except Exception as e:
                st.error(f"上传失败: {e}")

st.divider()

# ============================================================
# 压缩包上传
# ============================================================
st.subheader("📦 批量上传（压缩包）")
zip_file = st.file_uploader(
    "选择压缩包（zip/tar.gz/rar，最大 50MB）",
    type=ALLOWED_ZIP,
    key="zip_uploader",
)
if zip_file is not None:
    file_size_mb = len(zip_file.getvalue()) / (1024 * 1024)
    st.caption(f"文件大小: {file_size_mb:.1f} MB")
    if st.button("🚀 上传压缩包", key="btn_upload_zip"):
        with st.spinner("正在提交压缩包..."):
            try:
                result = upload_zip(zip_file.getvalue(), zip_file.name, USER_ID)
                task_id = result.get("data", {}).get("task_id", "")
                if task_id:
                    st.success("压缩包已接收，正在后台处理...")
                    # 轮询进度
                    bar = st.progress(0, "等待处理...")
                    status_placeholder = st.empty()
                    done = False
                    while not done:
                        time.sleep(2)
                        try:
                            ts = get_zip_task_status(task_id)
                            task_data = ts.get("data", {})
                            status = task_data.get("status", "")
                            progress = task_data.get("progress", {})
                            total = progress.get("total", 0)
                            success = progress.get("success", 0)
                            skipped = progress.get("skipped", 0)
                            failed = progress.get("failed", 0)
                            completed = success + skipped + failed
                            if total > 0:
                                pct = completed / total
                                bar.progress(pct, f"处理中: {completed}/{total}")
                            status_placeholder.info(
                                f"状态: {status} | ✅ {success} | ⏭️ {skipped} | ❌ {failed} | ⏳ {progress.get('pending', 0)}"
                            )
                            if status in ("completed", "failed"):
                                done = True
                                if status == "completed":
                                    bar.progress(1.0, "处理完成")
                                    st.success(f"压缩包处理完成！成功 {success}，跳过 {skipped}，失败 {failed}")
                                else:
                                    st.error("压缩包处理失败")
                                # 显示错误详情
                                error_details = task_data.get("error_details", [])
                                if error_details:
                                    st.subheader("错误详情")
                                    for err in error_details:
                                        st.warning(
                                            f"**{err.get('file_path', '?')}**: [{err.get('error_type', '?')}] {err.get('reason', '?')}"
                                        )
                        except Exception as e:
                            st.error(f"查询进度失败: {e}")
                            done = True
                    refresh_docs()
            except Exception as e:
                st.error(f"上传压缩包失败: {e}")

st.divider()

# ============================================================
# 文档列表
# ============================================================
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    st.subheader("📋 文档列表")
with col2:
    if st.button("🔄 刷新", use_container_width=True):
        refresh_docs()
with col3:
    if st.button("🗑️ 清空全部", type="secondary", use_container_width=True):
        st.warning("确定要清空知识库吗？此操作不可撤销！")

if not st.session_state.docs_loaded:
    refresh_docs()

docs = st.session_state.docs
if not docs:
    st.info("暂无文档，上传一个试试吧")
else:
    df = pd.DataFrame(docs)
    df.columns = ["MD5", "文件名", "上传时间"]
    df["MD5"] = df["MD5"].apply(lambda x: x[:12] + "...")

    for i, row in df.iterrows():
        c1, c2, c3, c4 = st.columns([2, 5, 3, 2])
        with c1:
            st.code(row["MD5"])
        with c2:
            st.text(row["文件名"])
        with c3:
            st.text(row["上传时间"])
        with c4:
            if st.button("🗑️", key=f"del_{i}"):
                try:
                    delete_document_by_md5(
                        docs[i]["md5"], USER_ID
                    )
                    st.success(f"已删除: {docs[i]['original_filename']}")
                    refresh_docs()
                except Exception as e:
                    st.error(f"删除失败: {e}")

# 清空确认
@st.dialog("确认清空知识库")
def confirm_clear():
    st.write("确定要清空知识库中的所有文档吗？此操作不可撤销！")
    if st.button("确认清空", type="primary"):
        try:
            clear_knowledge(USER_ID)
            st.success("知识库已清空")
            refresh_docs()
            st.rerun()
        except Exception as e:
            st.error(f"清空失败: {e}")

if st.session_state.get("show_clear_confirm", False):
    confirm_clear()
