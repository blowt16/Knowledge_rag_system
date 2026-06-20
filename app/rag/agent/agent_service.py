"""Agent 编排服务 — LangChain Tool Calling Agent + 消息历史。"""
from typing import AsyncIterator
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)


class AgentService:
    """LangChain Agent 编排服务：工具链注册 + 推理循环 + 流式输出。"""

    def __init__(self):
        self._tools_cache: dict[str, list] = {}

    def _get_llm(self):
        from app.core.background_init import init_manager
        llm = init_manager.chat_model
        if llm is None:
            from app.utils.factory import create_chat_model
            llm = create_chat_model()
        return llm

    def _get_tools(self, user_id: str):
        """注册 Agent 工具链（按 user_id 缓存）。"""
        if user_id in self._tools_cache:
            return self._tools_cache[user_id]

        from langchain_core.tools import tool
        from app.rag.rag_service import RAGService

        rag_service = RAGService()

        @tool
        async def knowledge_search(query: str) -> str:
            """从用户知识库中检索相关文档（HyDE 改写 + 混合检索 + 重排序 + 摘要）。
            当需要查找用户上传的文档内容时使用此工具。
            """
            result = await rag_service.search(query=query, user_id=user_id)
            if not result or not result.get("documents"):
                return "知识库中未找到相关内容。"
            answer = result.get("answer", "")
            if not answer:
                docs = result.get("documents", [])
                lines = [f"[{i+1}] {doc.page_content[:300]}" for i, doc in enumerate(docs)]
                answer = "\n\n".join(lines)
            return answer

        from app.rag.web_search_service import WebSearchService
        web_svc = WebSearchService()

        @tool
        def web_search(query: str) -> str:
            """联网搜索补充外部实时信息。仅在知识库无相关内容时使用。"""
            return web_svc.search(query)

        @tool
        async def summarize_document(content: str) -> str:
            """对长文档内容进行摘要。"""
            if len(content) < 200:
                return content
            try:
                llm = self._get_llm()
                loader = PromptLoader()
                prompt = loader.load("summary", content=content)
                response = await llm.ainvoke(prompt)
                return response.content if hasattr(response, "content") else str(response)
            except Exception as e:
                logger.error(f"摘要生成失败: {e}")
                return content[:300] + "..."

        self._tools_cache[user_id] = [knowledge_search, web_search, summarize_document]
        return self._tools_cache[user_id]

    def create_agent_with_history(self, session_id: str, user_id: str = "default_user"):
        """创建带消息历史的 Agent Executor。"""
        from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_core.runnables.history import RunnableWithMessageHistory
        from app.memory.memory_service import ConversationMemoryService

        llm = self._get_llm()
        tools = self._get_tools(user_id)
        memory_svc = ConversationMemoryService()

        loader = PromptLoader()
        system_prompt = loader.load("agent") or loader.load("system")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent = create_tool_calling_agent(llm, tools, prompt)
        agent_executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=True,
            handle_parsing_errors=True,
            max_iterations=5,
        )

        agent_with_history = RunnableWithMessageHistory(
            agent_executor,
            lambda sid: memory_svc.get_message_history(sid),
            input_messages_key="input",
            history_messages_key="chat_history",
        )

        return agent_with_history

    async def stream_chat(self, query: str, session_id: str,
                          user_id: str = "default_user") -> AsyncIterator[dict]:
        """流式执行 Agent 对话，通过 SSE 推送事件。"""
        agent = self.create_agent_with_history(session_id, user_id)

        try:
            async for event in agent.astream_events(
                {"input": query},
                config={"configurable": {"session_id": session_id}},
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_tool_start":
                    yield {
                        "event": "tool_start",
                        "tool": event.get("name", ""),
                        "data": str(event.get("data", {}).get("input", "")),
                    }
                elif kind == "on_tool_end":
                    yield {
                        "event": "tool_end",
                        "tool": event.get("name", ""),
                        "data": str(event.get("data", {}).get("output", ""))[:500],
                    }
                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk", None)
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield {
                            "event": "token",
                            "data": chunk.content,
                        }
                elif kind == "on_agent_finish":
                    output = event.get("data", {}).get("output", "")
                    yield {
                        "event": "done",
                        "data": str(output),
                    }

        except Exception as e:
            logger.error(f"Agent 执行失败: {e}")
            yield {
                "event": "error",
                "data": f"处理请求时出错: {str(e)}",
            }
