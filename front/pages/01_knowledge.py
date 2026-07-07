"""知识库管理页 — 文档上传、列表、删除。"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from config import USER_ID, ALLOWED_SINGLE, ALLOWED_ZIP, MAX_SINGLE_SIZE, MAX_ZIP_SIZE
from api_client import (
    upload_document_stream,
    stream_single_progress,
    list_documents,
    delete_document_by_md5,
    clear_knowledge,
    upload_zip,
    stream_zip_progress,
)

st.set_page_config(page_title="知识库管理", page_icon="📦", layout="wide")


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
if "show_clear_confirm" not in st.session_state:
    st.session_state.show_clear_confirm = False
if "toast_message" not in st.session_state:
    st.session_state.toast_message = None
if "delete_confirm" not in st.session_state:
    st.session_state.delete_confirm = None
if "degraded_upload_info" not in st.session_state:
    st.session_state.degraded_upload_info = None
if "duplicate_upload_name" not in st.session_state:
    st.session_state.duplicate_upload_name = None

# 在顶层渲染 toast（不依赖 button 回调时机）
if st.session_state.toast_message:
    st.toast(st.session_state.toast_message, icon="⚠️")
    st.session_state.toast_message = None

st.title("📦 知识库管理")

# ============================================================
# 单文件上传
# ============================================================
st.subheader("📄 上传文档")
uploaded_file = st.file_uploader(
    "选择文件（txt/pdf/md/pptx/docx，最大 100MB）",
    type=ALLOWED_SINGLE,
    key="single_uploader",
)
if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    file_size_mb = len(file_bytes) / (1024 * 1024)
    st.caption(f"文件大小: {file_size_mb:.1f} MB")
    if len(file_bytes) > MAX_SINGLE_SIZE:
        st.error(f"文件大小 {file_size_mb:.1f} MB 超过限制（最大 100MB），请压缩后重新上传")
        st.stop()
    if st.button("🚀 上传到知识库", key="btn_upload_single"):
        try:
            task_id = upload_document_stream(file_bytes, uploaded_file.name, USER_ID)
            bar = st.progress(0, "准备处理…")
            stage_placeholder = st.empty()

            for event in stream_single_progress(task_id):
                ev_type = event.get("event", "")

                if ev_type == "stage":
                    stage_placeholder.info(event.get("data", "处理中…"))
                    stage_map = {
                        "hashing": 0.02, "checking": 0.05, "extracting": 0.08,
                        "classifying": 0.12, "loading": 0.15, "cleaning": 0.45,
                        "splitting": 0.55, "embedding": 0.70,
                    }
                    pct = stage_map.get(event.get("stage", ""), 0.3)
                    bar.progress(pct, event.get("data", ""))

                elif ev_type == "error":
                    st.error(event.get("data", "处理失败"))
                    bar.empty()
                    stage_placeholder.empty()
                    break

                elif ev_type == "done":
                    ddata = event.get("data", {})
                    status = ddata.get("status", "")
                    if status == "duplicate":
                        bar.empty()
                        stage_placeholder.empty()
                        st.session_state.duplicate_upload_name = uploaded_file.name
                        st.rerun()
                    elif status in ("done", "degraded"):
                        bar.progress(1.0, "完成！")
                        if status == "degraded":
                            st.session_state.degraded_upload_info = {
                                "filename": uploaded_file.name,
                                "md5": ddata.get("md5", ""),
                                "chunks": ddata.get("chunks", 0),
                                "degradation": ddata.get("degradation", {}),
                            }
                        else:
                            stage_placeholder.success(f"上传成功: {uploaded_file.name} ({ddata.get('chunks', 0)} chunks)")
                        refresh_docs()
                        st.rerun()
                    else:
                        bar.empty()
                        stage_placeholder.error(f"处理失败: {ddata.get('reason', ddata.get('detail', '未知错误'))}")

        except Exception as e:
            st.error(f"上传失败: {e}")

st.divider()

# ============================================================
# 压缩包上传
# ============================================================
st.subheader("📦 批量上传（压缩包）")
zip_file = st.file_uploader(
    "选择压缩包（zip/tar.gz，最大 50MB）",
    type=ALLOWED_ZIP,
    accept_multiple_files=False,
    key="zip_uploader",
)
if zip_file is not None:
    zip_bytes = zip_file.getvalue()
    file_size_mb = len(zip_bytes) / (1024 * 1024)
    st.caption(f"文件大小: {file_size_mb:.1f} MB")
    if len(zip_bytes) > MAX_ZIP_SIZE:
        st.error(f"压缩包大小 {file_size_mb:.1f} MB 超过限制（最大 50MB），请分包后重新上传")
        st.stop()
    if st.button("🚀 上传压缩包", key="btn_upload_zip"):
        with st.spinner("正在提交压缩包..."):
            try:
                result = upload_zip(zip_file.getvalue(), zip_file.name, USER_ID)
                task_id = result.get("data", {}).get("task_id", "")
                if not task_id:
                    st.error("创建任务失败")
                else:
                    st.success("压缩包已接收，正在后台处理...")
                    bar = st.progress(0, "等待处理...")
                    status_placeholder = st.empty()
                    file_log_placeholder = st.empty()
                    docs_done = []
                    total = 0
                    success_count = 0
                    skipped_count = 0
                    failed_count = 0

                    for event in stream_zip_progress(task_id):
                        ev_type = event.get("event", "")

                        if ev_type == "status":
                            data = event.get("data", "")
                            # 初始进度（可能在 event 顶层或 data 内）
                            prog = event.get("progress", {})
                            if not prog and isinstance(data, dict):
                                prog = data.get("progress", {})
                            if prog:
                                total = prog.get("total", 0)
                            if data == "extracting":
                                status_placeholder.info("正在解压...")
                            elif data == "processing":
                                status_placeholder.info("正在处理文件...")
                            elif data == "failed":
                                st.error(event.get("error", "处理失败"))

                        elif ev_type == "file_done":
                            finfo = event.get("data", {})
                            fname = finfo.get("filename", "?")
                            fstatus = finfo.get("status", "failed")
                            if fstatus in ("done", "ok"):
                                success_count += 1
                                docs_done.append(f"✅ {fname}")
                            elif fstatus == "degraded":
                                success_count += 1
                                docs_done.append(f"⚠️ {fname}")
                            elif fstatus == "duplicate":
                                skipped_count += 1
                                docs_done.append(f"⏭️ {fname}")
                            else:
                                failed_count += 1
                                docs_done.append(f"❌ {fname}")
                            completed = success_count + skipped_count + failed_count
                            if total > 0:
                                bar.progress(completed / total, f"处理中: {completed}/{total}")
                            status_placeholder.info(
                                f"✅ {success_count} | ⏭️ {skipped_count} | ❌ {failed_count}"
                            )
                            # 显示最近处理记录
                            file_log_placeholder.markdown("\n".join(docs_done[-10:]))

                        elif ev_type == "done":
                            ddata = event.get("data", {})
                            if isinstance(ddata, dict) and "progress" in ddata:
                                final_prog = ddata["progress"]
                                success_count = final_prog.get("success", success_count)
                                skipped_count = final_prog.get("skipped", skipped_count)
                                failed_count = final_prog.get("failed", failed_count)
                                bar.progress(1.0, "处理完成")
                                if success_count == 0 and skipped_count > 0:
                                    st.warning(f"压缩包内所有文件均已存在，跳过 {skipped_count} 个文件，无新增")
                                else:
                                    if skipped_count > 0:
                                        st.toast(f"文件已存在，跳过 {skipped_count} 个重复文件", icon="⚠️")
                                    st.success(f"压缩包处理完成！成功 {success_count}，跳过 {skipped_count}，失败 {failed_count}")
                                error_details = ddata.get("error_details", [])
                                if error_details:
                                    with st.expander("查看错误详情"):
                                        for err in error_details:
                                            st.warning(
                                                f"**{err.get('file_path', '?')}**: [{err.get('error_type', '?')}] {err.get('reason', '?')}"
                                            )

                        elif ev_type == "error":
                            st.error(event.get("data", "未知错误"))

                    refresh_docs()
                    st.rerun()
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
        st.rerun()
with col3:
    if st.button("🗑️ 清空全部", type="secondary", use_container_width=True):
        st.session_state.show_clear_confirm = True
        st.rerun()

if not st.session_state.docs_loaded:
    refresh_docs()

docs = st.session_state.docs
if not docs:
    st.info("暂无文档，上传一个试试吧")
else:
    for i, d in enumerate(docs):
        c1, c2, c3 = st.columns([6, 3, 2])
        with c1:
            st.text(d["original_filename"])
        with c2:
            st.text(d["upload_time"])
        with c3:
            if st.button("🗑️", key=f"del_{i}"):
                st.session_state.delete_confirm = i
                st.rerun()


@st.dialog("确认清空知识库")
def confirm_clear():
    st.write("确定要清空知识库中的所有文档吗？此操作不可撤销！")
    if st.button("确认清空", type="primary"):
        try:
            clear_knowledge(USER_ID)
            st.success("知识库已清空")
            refresh_docs()
            st.session_state.show_clear_confirm = False
            st.rerun()
        except Exception as e:
            st.error(f"清空失败: {e}")

if st.session_state.show_clear_confirm:
    confirm_clear()


@st.dialog("确认删除文档")
def confirm_delete():
    i = st.session_state.delete_confirm
    if i is None or i >= len(docs):
        st.session_state.delete_confirm = None
        return
    doc = docs[i]
    st.write(f"确定要删除文档 **{doc['original_filename']}** 吗？")
    if st.button("确认删除", type="primary"):
        try:
            delete_document_by_md5(doc["md5"], USER_ID)
            st.success(f"已删除: {doc['original_filename']}")
            refresh_docs()
            st.session_state.delete_confirm = None
            st.rerun()
        except Exception as e:
            st.error(f"删除失败: {e}")


if st.session_state.delete_confirm is not None:
    confirm_delete()


@st.dialog("⚠️ 文件已存在")
def duplicate_upload_dialog():
    fn = st.session_state.duplicate_upload_name or "未知文件"
    st.warning(f"**{fn}** 已经上传过，不能重复上传。")
    if st.button("我知道了", use_container_width=True):
        st.session_state.duplicate_upload_name = None
        st.rerun()


@st.dialog("⚠️ 文档解析不完整")
def degraded_upload_dialog():
    info = st.session_state.degraded_upload_info
    if not info:
        st.session_state.degraded_upload_info = None
        return
    fn = info.get("filename", "未知文件")
    chunks = info.get("chunks", 0)
    deg = info.get("degradation", {})
    st.write(f"**{fn}** 上传成功，但存在以下问题：")
    st.warning(
        f"文本内容已正常入库（共 {chunks} 个片段），"
        f"但部分图片描述处理失败，详情: {deg}。\n\n"
        f"建议删除后重新上传以修复缺失的图片描述。"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("删除并重新上传", type="primary", use_container_width=True):
            try:
                delete_document_by_md5(info["md5"], USER_ID)
                st.success("已删除，请重新上传文件。")
                refresh_docs()
                st.session_state.degraded_upload_info = None
                st.rerun()
            except Exception as e:
                st.error(f"删除失败: {e}")
    with c2:
        if st.button("暂不处理", use_container_width=True):
            st.session_state.degraded_upload_info = None
            st.rerun()


if st.session_state.duplicate_upload_name is not None:
    duplicate_upload_dialog()

if st.session_state.degraded_upload_info is not None:
    degraded_upload_dialog()
