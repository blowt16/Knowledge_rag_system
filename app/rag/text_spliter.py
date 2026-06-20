"""文本切分器 — RecursiveCharacterTextSplitter + 可选语义合并。"""
import asyncio
import yaml
from app.utils.path_tool import resolve_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def _load_separators() -> list[str]:
    config_path = resolve_path("app/config/chroma.yaml")
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("separators", ["\n\n", "\n", "。", "！", "？", "!", "?", " ", ""])


def _load_chunk_config() -> tuple[int, int]:
    config_path = resolve_path("app/config/chroma.yaml")
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("chunk_size", 500), config.get("chunk_overlap", 50)


class AsyncTextSplitter:
    """异步文本切分器 — 封装 RecursiveCharacterTextSplitter。"""

    def __init__(self):
        chunk_size, chunk_overlap = _load_chunk_config()
        separators = _load_separators()
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._separators = separators

    def _create_splitter(self) -> "RecursiveCharacterTextSplitter":
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        return RecursiveCharacterTextSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            separators=self._separators,
            length_function=len,
        )

    def split_documents(self, documents: list) -> list:
        """切分 Document 列表（同步，保护 metadata 不被跨页合并破坏）。"""
        if not documents:
            return []
        splitter = self._create_splitter()
        result = splitter.split_documents(documents)
        logger.info(f"【文本切分】{len(documents)} 个文档 → {len(result)} 个 chunk")
        return result

    async def async_split_documents(self, documents: list) -> list:
        """异步切分 Document 列表。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.split_documents, documents)

    def split_text(self, text: str, enable_semantic_merge: bool = False) -> list[str]:
        """切分纯文本（可选语义合并）。

        Note: 语义合并在 processor.py 中不会触发（processor 调用 split_documents 保护 metadata）。
        """
        if not text:
            return []
        splitter = self._create_splitter()
        chunks = splitter.split_text(text)

        if enable_semantic_merge and len(chunks) > 1:
            chunks = self._semantic_merge(chunks)

        return chunks

    def _semantic_merge(self, chunks: list[str], threshold: float = 0.7) -> list[str]:
        """语义合并：相邻 chunk 相似度 > threshold 则合并。"""
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            embeddings = model.encode(chunks)

            merged = []
            current_chunk = chunks[0]
            current_emb = embeddings[0]

            for i in range(1, len(chunks)):
                similarity = float(
                    (current_emb @ embeddings[i])
                    / (abs(current_emb) * abs(embeddings[i]) + 1e-9)
                )
                if similarity > threshold:
                    current_chunk += chunks[i]
                    current_emb = (current_emb + embeddings[i]) / 2
                else:
                    merged.append(current_chunk)
                    current_chunk = chunks[i]
                    current_emb = embeddings[i]

            merged.append(current_chunk)
            return merged
        except Exception as e:
            logger.warning(f"【文本切分】语义合并失败: {e}")
            return chunks
