"""文档处理核心 — 加载 → 清洗 → 切分 → 入库 → MD5 记录。"""
import os
import re
import hashlib
from pathlib import Path
from datetime import datetime
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.rag.md5_manager.md5_store import MD5Store
from app.rag.text_spliter import AsyncTextSplitter
from app.rag.vector_store import VectorStoreService

logger = get_logger(__name__)


def _get_magic_signatures() -> dict[bytes, tuple[str, str]]:
    raw = get_config("magic_signatures", {})
    return {k.encode("utf-8"): (v[0], v[1]) for k, v in raw.items()}


def _remove_toc_lines(text: str) -> str:
    """移除目录行（含省略号/制表符/空白连接 + 尾部页码）。"""
    lines = text.split('\n')
    result = []
    in_toc_section = False

    def _is_toc_line(s: str) -> bool:
        # 1. 经典省略号 + 尾部页码
        if re.search(r'[.…]{3,}\s*\d{1,4}\s*$', s):
            return True
        # 2. 制表符 + 尾部页码
        if re.search(r'\t{1,3}\d{1,4}\s*$', s):
            return True
        # 3. 行尾括号页码
        if re.search(r'[（(]\d{1,4}[）)]\s*$', s):
            return True
        # 4. 多空白 + 尾部页码（MinerU OCR 常见格式）
        if re.search(r'\s{2,}\d{1,4}\s*$', s) and len(s) > 5:
            return True
        # 5. Markdown 链接 + 尾部页码
        if re.search(r'\]\([^)]+\)\s*\d{1,4}\s*$', s):
            return True
        return False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        # 检测"目录"标题行，标记进入目录区域
        if re.match(r'^目\s*录\s*$', stripped):
            in_toc_section = True
            continue

        if in_toc_section:
            if _is_toc_line(stripped):
                continue  # 跳过目录行
            # 遇到非目录行 → 退出目录区域
            in_toc_section = False

        if _is_toc_line(stripped):
            continue  # 不在目录区域但匹配目录特征，仍然跳过

        result.append(line)

    return '\n'.join(result)


def _clean_text(documents: list) -> list:
    """文本清洗流水线（4 组操作：控制字符→空白规范→页眉页脚/模型标记→目录清除）。"""
    if not os.getenv("TEXT_CLEAN_ENABLED", "true").lower() == "true":
        return documents

    cleaned = []
    for doc in documents:
        text = doc.page_content

        # ① 控制字符清理
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        # ② 空白规范化
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        text = '\n'.join(line.strip() for line in text.split('\n'))
        # ③ 页眉页脚清除
        text = re.sub(r'第\s*\d+\s*页\s*/\s*共\s*\d+\s*页', '', text)
        text = re.sub(r'^\d{1,4}$', '', text, flags=re.MULTILINE)
        text = re.sub(r'---\s*Page\s*\d+\s*---', '', text)
        # ④ 目录清除（保留章节标题）
        text = _remove_toc_lines(text)

        if not text.strip():
            continue

        doc.page_content = text.strip()
        cleaned.append(doc)

    return cleaned


def diagnose_failure(file_bytes: bytes, filename: str, loader_errors: list[str] = None) -> dict:
    """诊断兜底：所有 Loader 均失败后，检测魔数给出明确失败原因。"""
    file_size = len(file_bytes)

    if file_size == 0:
        return {
            "status": "failed", "reason": "empty_file",
            "detail": "文件为空", "suggestion": "文件内容为空，请检查后重新上传",
            "filename": filename,
        }

    magic_bytes = file_bytes[:8]
    magic_hex = magic_bytes.hex()
    signatures = _get_magic_signatures()

    for sig_bytes, (reason, detail) in signatures.items():
        if magic_bytes.startswith(sig_bytes):
            suggestion = "文件可能已损坏，请重新导出/保存后上传" if reason == "corrupted" \
                else "PDF 解析失败，可尝试使用其他阅读器另存为新 PDF 后重新上传" if reason == "parse_error" \
                else "不支持该格式，支持的格式：pdf/txt/md/docx/pptx"
            _log_diagnosis(filename, file_size, magic_hex, loader_errors or [], reason, detail)
            return {
                "status": "failed", "reason": reason,
                "detail": detail, "suggestion": suggestion,
                "filename": filename,
            }

    _log_diagnosis(filename, file_size, magic_hex, loader_errors or [], "unknown_format", "无法识别文件类型")
    return {
        "status": "failed", "reason": "unknown_format",
        "detail": "无法识别文件类型",
        "suggestion": "无法识别文件类型，请确认文件格式正确后重新上传",
        "filename": filename,
    }


def _log_diagnosis(filename: str, file_size: int, magic_hex: str,
                   loader_errors: list[str], reason: str, detail: str):
    logger.error(
        f"【诊断兜底】文件: {filename} | 大小: {file_size}B"
        f"{' | 魔数: ' + magic_hex if magic_hex else ''}"
    )
    for err in loader_errors:
        logger.error(f"  Loader 失败: {err}")
    logger.error(f"  诊断: {reason} → {detail}")


def _extract_loader_error(loader_errors: list[str]) -> str:
    """从 Loader 错误列表中提取真实错误，避免魔数误判为"损坏"。"""
    if not loader_errors:
        return ""
    patterns = [
        (r"视觉服务.*(?:超时|描述失败)", True),
        (r"未安装|ImportError|ModuleNotFoundError|No module named", True),
        (r"不支持的文件格式|格式不支持", True),
        (r"解析结果为空|提取结果为空|加载内容为空", True),
        (r"无法打开|已加密|密码", True),
        (r"pdfplumber|PyMuPDF|fitz", True),
    ]
    for err in loader_errors:
        for pattern, _ in patterns:
            if re.search(pattern, err):
                return err
    return ""


class DocumentProcessor:
    """文档处理核心 — 加载 → 清洗 → 切分 → 向量化 → MD5。"""

    def __init__(self):
        self._vector_store = VectorStoreService()
        self._md5_store = MD5Store()
        self._splitter = AsyncTextSplitter()

    async def _prepare_chunks(
        self, documents: list, user_id: str, md5_hex: str,
        original_filename: str, extension: str,
    ) -> list:
        """清洗 → 切分 → 元数据注入，返回处理好的 chunk 列表。

        从 process_to_chunks 中抽取，供 MinerU 分批流水线复用。
        """
        documents = _clean_text(documents)
        if not documents:
            return []

        documents = await self._splitter.async_split_documents(documents)
        if not documents:
            return []

        digits = int(get_config("chunk_id_digits", 4))
        for i, doc in enumerate(documents):
            doc.metadata["chunk_index"] = i
            doc.metadata["kb_id"] = user_id
            doc.metadata["chunk_id"] = f"{user_id}_{md5_hex}_{i:0{digits}d}"
            doc.metadata["user_id"] = user_id
            doc.metadata["md5"] = md5_hex
            doc.metadata["original_filename"] = original_filename
            doc.metadata["created_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            doc.metadata["file_type"] = extension
            doc.metadata["current_chapter"] = doc.metadata.get("current_chapter", "")
            doc.metadata["chapter_level"] = doc.metadata.get("chapter_level", 0)

        logger.info(
            f"【文档处理】{original_filename}: "
            f"{len(documents)} chunks (清洗+切分+元数据)"
        )
        return documents

    @staticmethod
    def _cleanup_images(user_id: str, md5_hex: str):
        """清理 PDF 解析失败后遗留的提取图片目录。"""
        import shutil
        from app.utils.path_tool import get_image_dir
        img_dir = get_image_dir(f"{user_id}/{md5_hex}")
        if img_dir.exists():
            shutil.rmtree(img_dir, ignore_errors=True)
            logger.info(f"【图片清理】已清理孤儿图片目录: {img_dir}")

    async def process(self, file_path: str | Path, user_id: str,
                      original_filename: str = "") -> dict:
        """完整处理管线（兼容旧调用方）：切分 → 嵌入 → 写入 → MD5。

        降级模式下仍入库文本内容，但不标记 MD5 完成，允许用户重试修复 VL 图片描述。
        """
        result = await self.process_to_chunks(file_path, user_id, original_filename)
        status = result.get("status")
        if status not in ("ok", "degraded"):
            return result

        is_degraded = status == "degraded"
        chunks = result["chunks"]
        md5_hex = result["md5"]
        fp = result["file_path"]

        try:
            self._vector_store.add_documents(chunks)
            logger.info(f"【向量数据库】文件 {original_filename} 入库完成: {len(chunks)} 个 chunk"
                        + (" (降级模式，部分图片描述缺失)" if is_degraded else ""))
        except Exception as e:
            logger.error(f"【向量数据库】文件入库失败: {e}")
            return {"status": "failed", "reason": str(e), "filename": original_filename, "md5": md5_hex}

        # 降级模式同样保存 MD5，防止用户重试导致文本 chunks 重复入库。
        # 用户如需重试修复图片描述，需先在前端删除该文件（删除 chunks + MD5 记录）。
        if is_degraded:
            try:
                self._md5_store.save_md5_hex(user_id, md5_hex, original_filename, fp)
            except Exception as e:
                logger.error(f"【MD5】保存失败，回滚向量数据: {e}")
                rolled_back = False
                try:
                    self._vector_store.delete_by_md5(user_id, md5_hex)
                    rolled_back = True
                except Exception as re:
                    logger.error(f"【MD5】回滚 ChromaDB 也失败: {re}")
                if rolled_back:
                    self._cleanup_images(user_id, md5_hex)
                else:
                    logger.error(
                        f"【严重】ChromaDB 回滚失败，图片保留不删: user={user_id}, md5={md5_hex}。"
                        f"ChromaDB 有 chunks 引用这些图片，若删除图片将导致 404。请手动检查。"
                    )
                return {"status": "failed", "reason": f"MD5 保存失败: {e}", "filename": original_filename, "md5": md5_hex}
            logger.info(
                f"【降级】文件 {original_filename} 已入库: {len(chunks)} chunks, "
                f"图片描述部分失败: {result.get('degradation', {})}. "
                f"如需重试，请先删除该文件后重新上传。"
            )
            return {
                "status": "degraded", "md5": md5_hex, "filename": original_filename,
                "chunks": len(chunks), "degradation": result.get("degradation", {}),
            }

        try:
            self._md5_store.save_md5_hex(user_id, md5_hex, original_filename, fp)
        except Exception as e:
            logger.error(f"【MD5】保存失败，回滚向量数据: {e}")
            rolled_back = False
            try:
                self._vector_store.delete_by_md5(user_id, md5_hex)
                rolled_back = True
            except Exception as re:
                logger.error(f"【MD5】回滚 ChromaDB 也失败: {re}")
            if rolled_back:
                self._cleanup_images(user_id, md5_hex)
            else:
                logger.error(
                    f"【严重】ChromaDB 回滚失败，图片保留不删: user={user_id}, md5={md5_hex}。"
                    f"ChromaDB 有 chunks 引用这些图片，若删除图片将导致 404。请手动检查。"
                )
            return {"status": "failed", "reason": f"MD5 保存失败: {e}", "filename": original_filename, "md5": md5_hex}

        return {"status": "done", "md5": md5_hex, "filename": original_filename, "chunks": len(chunks)}

    async def process_to_chunks(self, file_path: str | Path, user_id: str,
                                original_filename: str = "",
                                progress_callback=None,
                                on_batch=None) -> dict:
        """只做切分 + 元数据注入，不嵌入不写库。返回 chunks 供批量缓冲池使用。

        on_batch: 可选，MinerU 扫描件分批回调 (docs, start_page, end_page)
        """
        file_path = Path(file_path)
        if not original_filename:
            original_filename = file_path.name

        async def _push(stage: str, text: str):
            if progress_callback:
                await progress_callback(stage, text)

        # 1. MD5
        await _push("hashing", "计算文件指纹…")
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            md5_hex = hashlib.md5(file_bytes).hexdigest()
        except Exception as e:
            logger.error(f"【MD5计算】读取文件失败: {e}")
            return {"status": "failed", "reason": str(e), "filename": original_filename}

        # 2. 去重
        if self._md5_store.check_md5_exists(user_id, md5_hex):
            logger.debug(f"【向量数据库】文件 {original_filename} 的 md5 值 {md5_hex} 已存在，跳过")
            return {"status": "duplicate", "md5": md5_hex, "filename": original_filename}

        # 3. 加载
        await _push("loading", "文档加载中…")
        extension = file_path.suffix.lower().lstrip(".")
        documents = []
        degradation: dict = {}
        loader_errors = []

        try:
            if extension == "pdf":
                from app.utils.pdf_loader import load_pdf
                documents, degradation = await load_pdf(
                    str(file_path), user_id=user_id, md5_hex=md5_hex,
                    original_filename=original_filename,
                    progress_callback=progress_callback,
                    on_batch=on_batch,
                )
            else:
                from app.utils.file_handler import load_file
                documents = load_file(file_path, extension, user_id=user_id, md5_hex=md5_hex)
                if not documents:
                    loader_errors.append(f"{extension.upper()} Loader 返回空列表")
        except Exception as e:
            loader_errors.append(f"{extension.upper()} Loader 异常: {str(e)}")
            logger.error(f"【文档加载】{extension.upper()} 加载失败: {e}")

        if not documents:
            logger.error(f"【向量数据库】文件 {original_filename} 加载内容为空")
            # 加载失败时清理已提取的图片（pdf_multimodal_loader 在解析前已提取）
            self._cleanup_images(user_id, md5_hex)
            # 先检查是否为依赖缺失/解析错误（非文件损坏），避免 %PDF 魔数误判
            actual_error = _extract_loader_error(loader_errors)
            if actual_error:
                is_vl = bool(re.search(r"视觉服务", actual_error))
                suggestion = (
                    "视觉模型调用失败（超时或网络问题），可稍后重试或检查网络连接"
                    if is_vl else
                    "请检查依赖安装或文件格式后重新上传"
                )
                return {
                    "status": "failed",
                    "diagnosis": {
                        "status": "failed",
                        "reason": "vl_error" if is_vl else "parse_error",
                        "detail": actual_error,
                        "suggestion": suggestion,
                        "filename": original_filename,
                    },
                    "filename": original_filename,
                }
            diagnosis = diagnose_failure(file_bytes, original_filename, loader_errors)
            return {"status": "failed", "diagnosis": diagnosis, "filename": original_filename}

        # 3.5 降级检测：文本提取成功但 VL 图片描述部分失败
        if degradation:
            logger.warning(
                f"【降级】文件 {original_filename} 文本提取成功但有部分内容降级: "
                + ", ".join(f"{k}={v}" for k, v in degradation.items())
            )
            # 有降级时继续处理文本内容，但暂缓清理图片（保留给后续重试复用）
            # 注意：不入库 MD5，允许用户重试

        # 4-6. 清洗 → 切分 → 元数据
        if on_batch and not documents:
            # 流水线模式：文档已通过 on_batch 逐批处理，直接返回成功
            result = {
                "status": "ok",
                "chunks": [],
                "md5": md5_hex,
                "filename": original_filename,
                "file_path": str(file_path),
            }
            return result

        await _push("cleaning", "文本清洗中…")
        documents = await self._prepare_chunks(
            documents, user_id, md5_hex, original_filename, extension,
        )
        if not documents:
            return {"status": "failed", "reason": "empty_content",
                    "filename": original_filename, "md5": md5_hex}

        result = {
            "status": "degraded" if degradation else "ok",
            "chunks": documents,
            "md5": md5_hex,
            "filename": original_filename,
            "file_path": str(file_path),
        }
        if degradation:
            result["degradation"] = degradation
        return result
