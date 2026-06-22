"""Agent 编排服务 — LangChain Tool Calling Agent + 消息历史。"""
from typing import AsyncIterator
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)


class AgentService:
    """LangChain Agent 编排服务：工具链注册 + 推理循环 + 流式输出。"""

    def _get_llm(self):
        from app.core.background_init import init_manager
        llm = init_manager.chat_model
        if llm is None:
            from app.utils.factory import create_chat_model
            llm = create_chat_model()
        return llm

    def _get_tools(self, user_id: str, chat_history: list = None):
        from langchain_core.tools import tool
        from app.rag.rag_service import RAGService

        rag_service = RAGService()

        @tool
        def knowledge_search(query: str) -> str:
            """从用户知识库中检索相关文档（HyDE 改写 + 混合检索 + 重排序 + 摘要）。
            当需要查找用户上传的文档内容时使用此工具。
            """
            result = rag_service.search_sync(query=query, user_id=user_id, chat_history=chat_history)
            if not result or not result.get("documents"):
                return "知识库中未找到相关内容。"
            answer = result.get("answer", "")
            if not answer:
                docs = result.get("documents", [])
                max_chars = get_config("knowledge_search_max_chars", 300)
                lines = [f"[{i+1}] {doc.page_content[:max_chars]}" for i, doc in enumerate(docs)]
                answer = "\n\n".join(lines)
            return answer

        from app.rag.web_search_service import WebSearchService
        web_svc = WebSearchService()

        @tool
        def web_search(query: str) -> str:
            """联网搜索补充外部实时信息。仅在知识库无相关内容时使用。"""
            return web_svc.search(query)

        @tool
        def summarize_document(content: str) -> str:
            """对长文档内容进行摘要。"""
            if len(content) < get_config("summarize_min_chars", 500):
                return content
            try:
                llm = self._get_llm()
                loader = PromptLoader()
                prompt = loader.load("summary", content=content)
                response = llm.invoke(prompt)
                return response.content if hasattr(response, "content") else str(response)
            except Exception as e:
                logger.error(f"摘要生成失败: {e}")
                max_chars = get_config("knowledge_search_max_chars", 300)
                return content[:max_chars] + "..."

        return [knowledge_search, web_search, summarize_document]

    def _create_executor(self, user_id: str, chat_history: list = None):
        """创建 AgentExecutor（不含 RunnableWithMessageHistory，手动管理历史）。"""
        from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

        llm = self._get_llm()
        tools = self._get_tools(user_id, chat_history)

        loader = PromptLoader()
        system_prompt = loader.load("agent") or loader.load("system")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent = create_tool_calling_agent(llm, tools, prompt)
        return AgentExecutor(
            agent=agent, tools=tools,
            verbose=False, handle_parsing_errors=True,
            max_iterations=get_config("agent_max_iterations", 5),
        )

    async def stream_chat(self, query: str, session_id: str,
                          user_id: str = "default_user") -> AsyncIterator[dict]:
        """流式执行 Agent 对话，通过 SSE 推送事件。"""
        from app.memory.memory_service import ConversationMemoryService
        memory_svc = ConversationMemoryService()

        # 手动加载历史消息
        chat_history = memory_svc.load_context(session_id)

        agent = self._create_executor(user_id, chat_history)

        accumulated = ""
        done_sent = False
        tool_call_counts: dict[str, int] = {}
        tool_limits: dict = get_config("tool_call_limits", {})

        try:
            async for event in agent.astream_events(
                {
                    "input": query,
                    "chat_history": chat_history or [],
                },
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_tool_start":
                    tname = event.get("name", "")
                    tool_call_counts[tname] = tool_call_counts.get(tname, 0) + 1
                    limit = tool_limits.get(tname, 3)
                    if tool_call_counts[tname] > limit:
                        logger.warning(f"【Agent】工具 {tname} 重复调用 {tool_call_counts[tname]} 次，超过阈值 {limit}，终止本轮")
                        yield {
                            "event": "error",
                            "data": f"工具 {tname} 重复调用超过 {limit} 次，已终止",
                        }
                        return
                    tinput = str(event.get("data", {}).get("input", ""))
                    logger.info(f"【Agent】调用工具: {tname}, 输入: {tinput[:200]}")
                    yield {
                        "event": "tool_start",
                        "tool": tname,
                        "data": tinput,
                    }
                elif kind == "on_tool_end":
                    tname = event.get("name", "")
                    toutput = str(event.get("data", {}).get("output", ""))
                    logger.info(f"【Agent】工具 {tname} 返回 {len(toutput)} 字符")
                    yield {
                        "event": "tool_end",
                        "tool": tname,
                        "data": toutput[:500],
                    }
                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk", None)
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        accumulated += chunk.content
                        yield {
                            "event": "token",
                            "data": chunk.content,
                        }
                elif kind == "on_agent_finish":
                    output = event.get("data", {}).get("output", {})
                    if hasattr(output, "return_values"):
                        answer = output.return_values.get("output", "")
                    elif isinstance(output, dict):
                        answer = output.get("output", "")
                    else:
                        answer = str(output)
                    logger.info(f"[Agent] done via on_agent_finish, answer length={len(answer)}")
                    accumulated = answer
                    done_sent = True
                    yield {
                        "event": "done",
                        "data": answer,
                    }

                elif kind == "on_chain_end" and event.get("name", "") == "AgentExecutor":
                    if not done_sent:
                        output = event.get("data", {}).get("output", {})
                        if isinstance(output, dict):
                            answer = output.get("output", "")
                        elif hasattr(output, "return_values"):
                            answer = output.return_values.get("output", "")
                        else:
                            answer = str(output)
                        logger.info(f"[Agent] done via on_chain_end, answer length={len(answer)}")
                        accumulated = answer
                        done_sent = True
                        yield {
                            "event": "done",
                            "data": answer,
                        }

            # 兜底（仅在没有 on_agent_finish / on_chain_end 时触发）
            if not done_sent and accumulated:
                yield {
                    "event": "done",
                    "data": accumulated,
                }

        except Exception as e:
            logger.error(f"Agent 执行失败: {e}")
            yield {
                "event": "error",
                "data": f"处理请求时出错: {str(e)}",
            }
        finally:
            logger.debug(f"【Agent】准备持久化: session={session_id}, query_len={len(query)}, answer_len={len(accumulated)}")
            if memory_svc.append_messages(session_id, query, accumulated):
                logger.info(f"【Agent】消息持久化成功: session={session_id}")
            else:
                logger.error(f"【Agent】消息保存失败: session={session_id}")
