"""对话业务逻辑层 — 统一对话入口，支持 Agent / RAG 双模式。"""
import json
import time
from typing import AsyncIterator
from app.memory.memory_service import ConversationMemoryService
from app.agent.agent_service import AgentService
from app.rag.rag_service import RAGService
from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

# 标题截断长度（与 memory_service 一致）
_TITLE_TRUNCATE = int(get_config("session_title_max_length", 20))


class ChatService:
    """统一对话服务：Agent + RAG 双模式 + 会话管理。"""

    def __init__(self):
        self._memory = ConversationMemoryService.get_shared()
        self._agent_svc = AgentService()
        self._rag_svc = RAGService()

    async def handle_chat(self, query: str, session_id: str | None,
                          user_id: str, mode: str = "agent") -> AsyncIterator[str]:
        """处理对话请求，SSE 流式输出。

        Args:
            mode: "agent" (Agent工具链) | "rag" (直接RAG检索) | "auto" (暂按agent)
        """
        # 1. 会话管理
        if not session_id:
            session_id = self._memory.create_conversation(user_id, query[:_TITLE_TRUNCATE])
            logger.info(f"【对话】新建会话: session={session_id[:8]}..., mode={mode}, query_len={len(query)}")
            yield f"data: {json.dumps({'event': 'session_created', 'session_id': session_id})}\n\n"
        else:
            logger.info(f"【对话】继续会话: session={session_id[:8]}..., mode={mode}, query_len={len(query)}")

        if mode == "rag":
            logger.info(f"【对话】路由 → RAG直通: session={session_id[:8]}...")
            async for sse in self._handle_rag_stream(query, user_id, session_id):
                yield sse
        else:
            logger.info(f"【对话】路由 → Agent工具链: session={session_id[:8]}...")
            async for sse in self._handle_agent_stream(query, session_id, user_id):
                yield sse

    async def _handle_rag_stream(self, query: str, user_id: str,
                                 session_id: str) -> AsyncIterator[str]:
        """RAG 直通模式：检索 → LLM 生成 → 流式输出。"""
        t_start = time.time()

        # 加载历史上下文
        history = self._memory.load_context(session_id)
        answer = ""

        try:
            # RAG 检索 (流式: 边生成摘要边推送 token)
            try:
                logger.info(f"【RAG直通】开始检索: query_len={len(query)}, history_turns={len(history)//2}")
                import asyncio as _asyncio
                token_queue: _asyncio.Queue = _asyncio.Queue()

                async def _push_token(chunk: str):
                    await token_queue.put(chunk)

                _skip = get_config("rag_skip_summary", False)
                search_task = _asyncio.create_task(
                    self._rag_svc.search(query, user_id, history, on_chunk=_push_token,
                                         skip_summary=_skip))

                # 检索 + 重排序期间 token_queue 为空, search_task 完成后 tokens 才开始到达
                while not search_task.done() or not token_queue.empty():
                    try:
                        poll_timeout = float(get_config("token_queue_poll_timeout", 0.1))
                        chunk = await _asyncio.wait_for(token_queue.get(), timeout=poll_timeout)
                        yield f"data: {json.dumps({'event': 'token', 'data': chunk}, ensure_ascii=False)}\n\n"
                    except _asyncio.TimeoutError:
                        pass

                result = await search_task
            except Exception as e:
                logger.error(f"【RAG直通】检索失败: {e}")
                answer = f"检索失败: {str(e)}"
                yield f"data: {json.dumps({'event': 'error', 'data': answer}, ensure_ascii=False)}\n\n"
            else:
                documents = result.get("documents", [])
                answer = result.get("answer", "")
                logger.info(f"【RAG直通】检索完成: docs={len(documents)}, answer_len={len(answer)}")

                if not documents:
                    logger.info("【RAG直通】知识库中未找到相关内容")
                    answer = "知识库中未找到相关内容。"
                    yield f"data: {json.dumps({'event': 'token', 'data': answer}, ensure_ascii=False)}\n\n"
                else:
                    if not answer:
                        yield f"data: {json.dumps({'event': 'token', 'data': answer}, ensure_ascii=False)}\n\n"
                    # 参考资料来源（章节溯源，图片已通过 LLM 回答展示）
                    sources = []
                    seen = set()
                    img_refs = []
                    img_seen = set()
                    from app.utils.path_tool import get_server_url
                    _base_url = get_server_url()
                    for d in documents:
                        src = d.metadata.get("original_filename", "未知")
                        page = d.metadata.get("page", "")
                        chapter = d.metadata.get("current_chapter", "")
                        label = src
                        if page:
                            label += f" (第{page}页)"
                        if chapter:
                            label += f" [{chapter}]"
                        if label not in seen:
                            seen.add(label)
                            sources.append({
                                "label": label,
                                "source": src,
                                "page": str(page) if page else "",
                                "chapter": chapter or "",
                            })
                        for img_path in d.metadata.get("image_paths", []):
                            relative = img_path.replace("\\", "/")
                            from app.config.loader import get_config as _cfg
                            prefix = _cfg("image_extract_dir", "extracted_images") + "/"
                            if relative.startswith(prefix):
                                relative = relative[len(prefix):]
                            if relative not in img_seen:
                                img_seen.add(relative)
                                img_refs.append(f"{src} → {_base_url}/images/{relative}")
                    if sources:
                        logger.info(f"【RAG直通】文本参考来源:\n  - " + "\n  - ".join(s["label"] for s in sources))
                    if img_refs:
                        logger.info(f"【RAG直通】图片参考来源:\n  - " + "\n  - ".join(img_refs))
                    yield f"data: {json.dumps({'event': 'references', 'data': sources}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"【RAG直通】未预期异常: {e}")
            answer = answer or f"处理失败: {str(e)}"
            yield f"data: {json.dumps({'event': 'error', 'data': answer}, ensure_ascii=False)}\n\n"
        finally:
            logger.debug(f"【RAG直通】准备持久化: query_len={len(query)}, answer_len={len(answer)}")
            if self._memory.append_messages(session_id, query, answer or "未找到相关内容"):
                logger.info(f"【RAG直通】消息持久化成功: session={session_id}")
            else:
                logger.error(f"【RAG直通】消息保存失败: session={session_id}")

        yield f"data: {json.dumps({'event': 'done', 'data': ''})}\n\n"
        logger.info(f"【RAG直通】本轮耗时: {time.time() - t_start:.1f}s")

    async def _handle_agent_stream(self, query: str, session_id: str,
                                   user_id: str) -> AsyncIterator[str]:
        """Agent 工具链模式。持久化由 AgentService.stream_chat 的 finally 块保证。"""
        t_start = time.time()
        logger.info(f"【Agent】开始处理: session={session_id[:8]}..., query_len={len(query)}")
        try:
            async for event in self._agent_svc.stream_chat(
                query=query, session_id=session_id, user_id=user_id
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"对话处理失败: {e}")
            yield f"data: {json.dumps({'event': 'error', 'data': str(e)})}\n\n"
        finally:
            logger.info(f"【Agent】本轮耗时: {time.time() - t_start:.1f}s")
