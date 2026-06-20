"""空检索器 — 占位符，始终返回空结果。"""
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever


class EmptyRetriever(BaseRetriever):
    """当 user_id 无效或未登录时使用，始终返回空列表。"""

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        return []

    async def _aget_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        return []
