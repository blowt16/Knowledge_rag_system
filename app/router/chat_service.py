"""对话业务逻辑层 — 统一对话入口，支持 Agent / RAG 双模式。"""
import json
from typing import AsyncIterator
from app.memory.memory_service import ConversationMemoryService
from app.agent.agent_service import AgentService
from app.rag.rag_service import RAGService
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class ChatService:
    """统一对话服务：Agent + RAG 双模式 + 会话管理。"""

    def __init__(self):
        self._memory = ConversationMemoryService()
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
            session_id = self._memory.create_conversation(user_id, query[:30])
            logger.info(f"【对话】新建会话: session={session_id[:8]}..., mode={mode}, query_len={len(query)}")
            yield f"data: {json.dumps({'event': 'session_created', 'session_id': session_id})}\n\n"
        else:
            logger.info(f"【对话】继续会话: session={session_id[:8]}..., mode={mode}, query_len={len(query)}")

        if mode == "rag":
            # 直接 RAG 检索路径：不经过 Agent，效率更高
            async for sse in self._handle_rag_stream(query, user_id, session_id):
                yield sse
        else:
            # Agent 工具链路径（含 mode="agent" 和 mode="auto"）
            async for sse in self._handle_agent_stream(query, session_id, user_id):
                yield sse

    async def _handle_rag_stream(self, query: str, user_id: str,
                                 session_id: str) -> AsyncIterator[str]:
        """RAG 直通模式：检索 → LLM 生成 → 流式输出。"""
        from app.core.background_init import init_manager
        from langchain_core.output_parsers import StrOutputParser

        # 加载历史上下文
        history = self._memory.load_context(session_id)
        answer = ""

        try:
            # RAG 检索
            try:
                logger.info(f"【RAG直通】开始检索: query_len={len(query)}, history_turns={len(history)//2}")
                result = await self._rag_svc.search(query, user_id, history)
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
                elif answer:
                    # 二次流式生成更自然的回答
                    try:
                        llm = init_manager.chat_model
                        if llm:
                            context_text = "\n\n".join(
                                f"[{i+1}] {d.page_content[:600]}" for i, d in enumerate(documents[:3])
                            )
                            from langchain_core.prompts import ChatPromptTemplate
                            prompt = ChatPromptTemplate.from_messages([
                                ("system", "你是一个知识库助手。基于检索结果简洁回答用户问题。"),
                                ("human", "问题：{query}\n\n参考资料：\n{context}"),
                            ])
                            chain = prompt | llm | StrOutputParser()
                            regenerated = ""
                            async for chunk in chain.astream({"query": query, "context": context_text}):
                                if chunk:
                                    regenerated += chunk
                                    yield f"data: {json.dumps({'event': 'token', 'data': chunk}, ensure_ascii=False)}\n\n"
                            if regenerated:
                                answer = regenerated
                                logger.info(f"【RAG直通】LLM 二次生成完成: answer_len={len(regenerated)}")
                        else:
                            yield f"data: {json.dumps({'event': 'token', 'data': answer}, ensure_ascii=False)}\n\n"
                    except Exception as e:
                        logger.error(f"【RAG直通】流式生成失败: {e}")
                        yield f"data: {json.dumps({'event': 'token', 'data': answer}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'event': 'token', 'data': answer}, ensure_ascii=False)}\n\n"
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

    async def _handle_agent_stream(self, query: str, session_id: str,
                                   user_id: str) -> AsyncIterator[str]:
        """Agent 工具链模式。持久化由 AgentService.stream_chat 的 finally 块保证。"""
        logger.info(f"【Agent】开始处理: session={session_id[:8]}..., query_len={len(query)}")
        try:
            async for event in self._agent_svc.stream_chat(
                query=query, session_id=session_id, user_id=user_id
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"对话处理失败: {e}")
            yield f"data: {json.dumps({'event': 'error', 'data': str(e)})}\n\n"
