"""Agent 编排服务 — LangChain Tool Calling Agent + 消息历史。"""
from typing import AsyncIterator, Optional
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)


class AgentService:
    """LangChain Agent 编排服务：工具链注册 + 推理循环 + 流式输出。"""

    @staticmethod
    def _log_agent_refs(text_refs: list, img_refs: list):
        if text_refs:
            text_refs.sort(key=lambda r: (r.get("source", ""), int(r["page"]) if r["page"] else 0))
            labels = [r["label"] for r in text_refs]
            logger.info(f"【Agent】文本参考来源:\n  - " + "\n  - ".join(labels))
        if img_refs:
            logger.info(f"【Agent】图片参考来源:\n  - " + "\n  - ".join(img_refs))

    def _get_llm(self):
        from app.core.background_init import init_manager
        llm = init_manager.chat_model
        if llm is None:
            from app.utils.factory import create_chat_model
            llm = create_chat_model()
        return llm

    def _get_tools(self, user_id: str, chat_history: list = None,
                    refs_list: list = None, img_refs_list: list = None):
        from langchain_core.tools import tool
        from app.rag.rag_service import RAGService

        rag_service = RAGService()

        @tool
        async def knowledge_search(query: str) -> str:
            """从用户私有知识库中实时检索权威文档内容（HyDE 查询改写 + 混合检索 + RRF 重排序）。

            ## 调用时机（强制）
            以下任一条件满足时，必须调用本工具，不可跳过：
            1. 用户问题涉及上传文档中的事实、概念、数据、流程、规范
            2. 用户要求查找、引用、总结、提取、对比或改写文档内容
            3. 用户提及专有名词（项目名、工单号、配置项、内部术语、文件名）
            4. 对话中存在指代不明或上下文缺失（如"上面那个"、"刚才说的"）
            5. 用户意图不明确，无法判定是否属于闲聊或能力问询

            ## 禁止行为
            - 禁止基于对话历史中的旧检索结果直接作答——知识库动态更新，历史内容可能已失效
            - 禁止基于自身训练数据推断私有文档内容——私有文档不在训练集中
            - 禁止在未调用本工具的情况下输出任何引用格式（如"根据文档"、"第X页"）

            ## 参数
            query: str — 检索查询字符串，需结合对话历史完成指代消解
                （如用户说"它的参数是什么"，应消解为"XX产品的参数是什么"）

            ## 返回
            原始文档片段及来源标注（页码、文件名），不做 LLM 摘要，由 Agent 自主分析整合。

            ## 示例
            ✅ 用户："这个合同的有效期是多久？" → 必须调用
            ✅ 用户："帮我总结一下上传的财报" → 必须调用
            ✅ 用户："它和前面那个有什么区别？" → 必须调用（指代消解后）
            ❌ 用户："你好" → 不调用
            ❌ 用户："你能做什么？" → 不调用
            """
            _skip = get_config("agent_skip_summary", True)
            result = await rag_service.search(
                query=query, user_id=user_id, chat_history=chat_history,
                skip_summary=_skip)
            if not result or not result.get("documents"):
                return "知识库中未找到相关内容。"
            docs = result.get("documents", [])
            max_chars = get_config("chunk_size", 500)
            # 收集文档来源供 references 事件使用（含图片溯源）
            if refs_list is not None:
                from app.utils.path_tool import get_server_url
                base_url = get_server_url()
                from app.config.loader import get_config as _cfg
                _img_prefix = _cfg("image_extract_dir", "extracted_images") + "/"
                seen = set()
                for d in docs:
                    src = d.metadata.get("original_filename", "未知")
                    page = d.metadata.get("page", "")
                    chapter = d.metadata.get("current_chapter", "")
                    label = src
                    if page:
                        label += f" (第{page}页)"
                    if chapter:
                        label += f" [{chapter}]"
                    # 收集该文档的图片URL
                    doc_images = []
                    for img_path in d.metadata.get("image_paths", []):
                        relative = img_path.replace("\\", "/")
                        if relative.startswith(_img_prefix):
                            relative = relative[len(_img_prefix):]
                        doc_images.append(f"{base_url}/images/{relative}")
                    if label not in seen:
                        seen.add(label)
                        refs_list.append({
                            "label": label,
                            "source": src,
                            "page": str(page) if page else "",
                            "chapter": chapter or "",
                            "images": doc_images,
                        })
                    else:
                        # 同一来源的多个 chunk → 合并图片
                        for entry in refs_list:
                            if entry.get("label") == label:
                                existing = set(entry.get("images", []))
                                for img in doc_images:
                                    if img not in existing:
                                        entry["images"].append(img)
                                        existing.add(img)
                                break
            # 收集图片引用供日志使用
            if img_refs_list is not None:
                from app.utils.path_tool import get_server_url
                base_url = get_server_url()
                img_seen = set()
                for d in docs:
                    src = d.metadata.get("original_filename", "未知")
                    for img_path in d.metadata.get("image_paths", []):
                        relative = img_path.replace("\\", "/")
                        from app.config.loader import get_config as _cfg
                        prefix = _cfg("image_extract_dir", "extracted_images") + "/"
                        if relative.startswith(prefix):
                            relative = relative[len(prefix):]
                        if relative not in img_seen:
                            img_seen.add(relative)
                            img_refs_list.append(f"{src} → {base_url}/images/{relative}")
            # 格式化原始文档内容（图片紧跟对应文档，确保 LLM 能关联图文）
            lines = []
            for i, doc in enumerate(docs):
                src = doc.metadata.get("original_filename", "未知")
                page = doc.metadata.get("page", "")
                chapter = doc.metadata.get("current_chapter", "")
                header = f"[来源: {src}"
                if page:
                    header += f", 第{page}页"
                if chapter:
                    header += f", {chapter}"
                header += "]"
                line = f"{header}\n{doc.page_content[:max_chars]}"

                # 将该文档关联的图片紧跟其后注入——而非集中堆放在末尾
                image_paths = doc.metadata.get("image_paths", [])
                if image_paths:
                    img_md = RAGService._build_image_markdown([doc])
                    if img_md:
                        line += "\n\n--- 该段资料包含以下图片（必须在回答中引用） ---\n"
                        line += "\n".join(img_md)

                lines.append(line)
            answer = "\n\n---\n\n".join(lines)
            return answer

        from app.rag.web_search_service import WebSearchService
        web_svc = WebSearchService()

        @tool
        def web_search(query: str) -> str:
            """联网搜索补充外部实时信息。仅在知识库无相关内容时使用。"""
            logger.info(f"【Agent-联网搜索】查询: {query[:100]}")
            result = web_svc.search(query)
            logger.info(f"【Agent-联网搜索】返回 {len(result)} 字符")
            return result

        @tool
        def summarize_document(content: str) -> str:
            """对长文档内容进行摘要。"""
            logger.info(f"【Agent-文档摘要】原文 {len(content)} 字符")
            if len(content) < get_config("chunk_size", 500):
                logger.info(f"【Agent-文档摘要】内容不足最小阈值，跳过摘要")
                return content
            try:
                llm = self._get_llm()
                loader = PromptLoader()
                prompt = loader.load("summary", content=content)
                logger.info(f"【Agent-文档摘要】调用 LLM 生成摘要…")
                response = llm.invoke(prompt)
                result = response.content if hasattr(response, "content") else str(response)
                logger.info(f"【Agent-文档摘要】LLM 摘要完成: {len(content)} → {len(result)} 字符")
                return result
            except Exception as e:
                logger.error(f"【Agent-文档摘要】LLM 摘要失败: {e}")
                max_chars = get_config("chunk_size", 500)
                return content[:max_chars] + "..."

        return [knowledge_search, web_search, summarize_document]

    def _create_executor(self, user_id: str, chat_history: list = None,
                          refs_list: list = None, img_refs_list: list = None):
        """创建 AgentExecutor（不含 RunnableWithMessageHistory，手动管理历史）。"""
        from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

        llm = self._get_llm()
        tools = self._get_tools(user_id, chat_history, refs_list, img_refs_list)

        loader = PromptLoader()
        system_prompt = loader.load("agent")

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
                          user_id: str = "default_user",
                          intent_result: Optional["IntentResult"] = None,
                          ) -> AsyncIterator[dict]:
        """流式执行 Agent 对话，通过 SSE 推送事件。

        Args:
            intent_result: IntentResult | None，意图分类结果。
                           非 None 且 action_directive 非空时注入到 agent_input。
        """
        from app.memory.memory_service import ConversationMemoryService
        memory_svc = ConversationMemoryService.get_shared()

        # 手动加载历史消息
        chat_history = memory_svc.load_context(session_id)

        # 构建 agent_input（意图注入 > 多轮注入 > 默认提醒）
        agent_input = query
        if intent_result and intent_result.action_directive:
            # 意图识别成功 → 注入精确的硬约束指令
            agent_input = intent_result.action_directive + "\n\n" + query
            logger.info(
                f"【Agent】注入意图指令: intent={intent_result.intent}, "
                f"confidence={intent_result.confidence:.2f}"
            )
        elif chat_history:
            # 无意图识别结果 + 多轮对话 → 使用原有逻辑（recency bias 兜底）
            agent_input = (
                "[⚠️ 本轮必须调用 knowledge_search 检索知识库，"
                "禁止基于历史对话中的内容直接作答]\n\n" + query
            )
        else:
            # 首轮对话 + 无意图识别 → 默认提醒检索
            agent_input = (
                "请基于用户问题判断是否需要调用 knowledge_search 检索知识库，"
                "需要则先检索再作答。\n\n" + query
            )
            logger.debug("【Agent】首轮无意图，注入默认检索提醒")

        accumulated = ""
        done_sent = False
        tool_call_counts: dict[str, int] = {}
        tool_limits: dict = get_config("tool_call_limits", {})
        agent_references: list[str] = []
        agent_img_refs: list[str] = []

        agent = self._create_executor(user_id, chat_history, agent_references, agent_img_refs)

        try:
            async for event in agent.astream_events(
                {
                    "input": agent_input,
                    "chat_history": chat_history or [],
                },
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_tool_start":
                    tname = event.get("name", "")
                    tool_call_counts[tname] = tool_call_counts.get(tname, 0) + 1
                    limit = tool_limits.get(tname, int(get_config("tool_call_limit_default", 3)))
                    if tool_call_counts[tname] > limit:
                        logger.warning(f"【Agent】工具 {tname} 重复调用 {tool_call_counts[tname]} 次，超过阈值 {limit}，终止本轮")
                        yield {
                            "event": "error",
                            "data": f"工具 {tname} 重复调用超过 {limit} 次，已终止",
                        }
                        return
                    tinput = str(event.get("data", {}).get("input", ""))
                    logger.info(f"【Agent】调用工具: {tname}, 输入: {tinput[:int(get_config('agent_log_truncate_input', 200))]}")
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
                        "data": toutput[:int(get_config("agent_log_truncate_output", 500))],
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
                    if agent_references:
                        self._log_agent_refs(agent_references, agent_img_refs)
                        yield {"event": "references", "data": agent_references}
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
                        if agent_references:
                            self._log_agent_refs(agent_references, agent_img_refs)
                            yield {"event": "references", "data": agent_references}
                        yield {
                            "event": "done",
                            "data": answer,
                        }

            # 兜底（仅在没有 on_agent_finish / on_chain_end 时触发）
            if not done_sent and accumulated:
                if agent_references:
                    self._log_agent_refs(agent_references, agent_img_refs)
                    yield {"event": "references", "data": agent_references}
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
            saved_answer = accumulated or ""
            if agent_references:
                agent_references.sort(key=lambda r: (r.get("source", ""), int(r["page"]) if r["page"] else 0))
                ref_text = "\n\n---\n**📚 参考来源：**\n" + "\n".join(f"- {r['label']}" for r in agent_references)
                saved_answer += ref_text
            logger.debug(f"【Agent】准备持久化: session={session_id}, query_len={len(query)}, answer_len={len(saved_answer)}")
            if memory_svc.append_messages(session_id, query, saved_answer):
                logger.info(f"【Agent】消息持久化成功: session={session_id}")
            else:
                logger.error(f"【Agent】消息保存失败: session={session_id}")
