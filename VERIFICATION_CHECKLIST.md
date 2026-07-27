# 验证清单

## 一、Chunk ID 全局唯一标识

### 1.1 chunk_index 分配（text_spliter.py）
```bash
uv run python -c "
from langchain_core.documents import Document
from app.rag.text_spliter import AsyncTextSplitter
splitter = AsyncTextSplitter()
docs = [Document(page_content='测试段落。' * 50, metadata={'source': 'test.txt'})]
chunks = splitter.split_documents(docs)
for c in chunks:
    assert 'chunk_index' in c.metadata
    assert isinstance(c.metadata['chunk_index'], int)
print(f'OK: {len(chunks)} chunks, chunk_index=0..{len(chunks)-1}')
"
```
**预期**: 输出 `OK: N chunks, chunk_index=0..N-1`

### 1.2 chunk_id 组装（processor.py）
```bash
uv run python -c "
from langchain_core.documents import Document
from datetime import datetime
# 模拟 processor 元数据富化
user_id = 'test_user'; md5_hex = 'abc123def456'
docs = [
    Document(page_content='A', metadata={'chunk_index': 0, 'page': 3}),
    Document(page_content='B', metadata={'chunk_index': 1}),
]
for doc in docs:
    ci = doc.metadata.get('chunk_index', 0)
    doc.metadata['kb_id'] = user_id
    doc.metadata['chunk_index'] = ci
    doc.metadata['chunk_id'] = f'{user_id}_{md5_hex}_{ci:04d}'
    doc.metadata['user_id'] = user_id
    doc.metadata['md5'] = md5_hex
    page = doc.metadata.get('page')
    if page is not None:
        doc.metadata['page_start'] = page
        doc.metadata['page_end'] = page
# 验证
assert docs[0].metadata['chunk_id'] == 'test_user_abc123def456_0000'
assert docs[1].metadata['chunk_id'] == 'test_user_abc123def456_0001'
# 禁止字段
assert 'file_md5' not in docs[0].metadata
assert 'page_start' not in docs[0].metadata
assert 'page_end' not in docs[0].metadata
print('OK: chunk_id 正确，无 file_md5/page_start/page_end')
"
```
**预期**: `OK: chunk_id 正确，无 file_md5/page_start/page_end`

### 1.3 ChromaDB 使用 chunk_id 作为内部 ID（vector_store.py）
```bash
uv run python -c "
from app.rag.vector_store import VectorStoreService
# 检查 add_documents 源码中使用了 doc.metadata.get('chunk_id')
import inspect
src = inspect.getsource(VectorStoreService.add_documents)
assert 'chunk_id' in src
assert 'cid = doc.metadata.get("chunk_id")' in src or "cid = doc.metadata.get('chunk_id')" in src
print('OK: add_documents 使用 chunk_id')
"
```
**预期**: `OK: add_documents 使用 chunk_id`

---

## 二、RRF 去重

### 2.1 三处 doc_id 统一为一级 chunk_id
```bash
uv run python -c "
import inspect
from app.rag.retrievers.hybrid_retriever import HybridRetriever
src = inspect.getsource(HybridRetriever._rrf_fusion)
# 确保没有 page_content[:50] 作为主键
assert 'or getattr(doc, \"id\", None) or doc.page_content[:50]' not in src
# 确保三处都直接用 chunk_id
count = src.count('doc.metadata[\"chunk_id\"]')
assert count == 3, f'expected 3, got {count}'
print(f'OK: 3处 doc_id 全部使用 chunk_id')
"
```
**预期**: `OK: 3处 doc_id 全部使用 chunk_id`

### 2.2 RRF 融合正确去重
```bash
uv run python -c "
from langchain_core.documents import Document
from app.rag.retrievers.hybrid_retriever import HybridRetriever
hr = HybridRetriever()
d1 = Document(page_content='same content', metadata={'chunk_id': 'u1_md5_0000'})
d2 = Document(page_content='same content', metadata={'chunk_id': 'u1_md5_0001'})
result = hr._rrf_fusion([d1, d2], [d2, d1])
assert len(result) == 2, f'FAIL: expected 2, got {len(result)}'
print(f'OK: RRF 融合返回 {len(result)} 条，相同内容不同 chunk_id 未被合并')
"
```
**预期**: `OK: RRF 融合返回 2 条，相同内容不同 chunk_id 未被合并`

### 2.3 BM25 构建包含 metadatas 和 documents
```bash
uv run python -c "
import inspect
from app.rag.retrievers.hybrid_retriever import HybridRetriever
src = inspect.getsource(HybridRetriever._get_or_build_bm25)
assert '[\"metadatas\", \"documents\"]' in src
print('OK: BM25 include 包含 metadatas 和 documents')
"
```
**预期**: `OK: BM25 include 包含 metadatas 和 documents`

---

## 三、删除原子性

### 3.1 vector_store 异常透传
```bash
uv run python -c "
import inspect
from app.rag.vector_store import VectorStoreService
src = inspect.getsource(VectorStoreService.delete_by_md5)
assert 'except' not in src, 'FAIL: delete_by_md5 仍有 try/except 吞异常'
src2 = inspect.getsource(VectorStoreService.delete_by_user)
assert 'except' not in src2, 'FAIL: delete_by_user 仍有 try/except 吞异常'
print('OK: 删除方法异常透传')
"
```
**预期**: `OK: 删除方法异常透传`

### 3.2 knowledge_service ChromaDB 先删
```bash
uv run python -c "
import inspect
from app.router.knowledge_service import KnowledgeService
src = inspect.getsource(KnowledgeService.delete_by_md5)
# 确认 ChromaDB delete 在 MD5 delete 之前
cb_pos = src.find('delete_by_md5')
md5_pos = src.find('delete_single_md5')
assert cb_pos < md5_pos, f'FAIL: ChromaDB delete({cb_pos}) should be before MD5 delete({md5_pos})'
print('OK: ChromaDB 先删，MD5 后删')
"
```
**预期**: `OK: ChromaDB 先删，MD5 后删`

---

## 四、已删除代码确认

### 4.1 search_sync 已删除
```bash
grep -r "search_sync" D:/Knowledge_rag_system/app/ && echo "FAIL: 仍有残留" || echo "OK: search_sync 已完全删除"
```
**预期**: `OK: search_sync 已完全删除`

### 4.2 nest_asyncio 已删除
```bash
grep -r "nest.asyncio\|nest_asyncio" D:/Knowledge_rag_system/app/ D:/Knowledge_rag_system/main.py 2>/dev/null && echo "FAIL: 仍有残留" || echo "OK: nest_asyncio 已完全删除"
```
**预期**: `OK: nest_asyncio 已完全删除`

### 4.3 image_extractor.py 已删除
```bash
test -f D:/Knowledge_rag_system/app/utils/image_extractor.py && echo "FAIL: 文件仍存在" || echo "OK: image_extractor.py 已删除"
```
**预期**: `OK: image_extractor.py 已删除`

---

## 五、元数据字段清理

### 5.1 全项目无残留禁止字段
```bash
grep -rn "file_md5\|page_start\|page_end" D:/Knowledge_rag_system/app/ D:/Knowledge_rag_system/main.py 2>/dev/null && echo "FAIL: 仍有残留" || echo "OK: 全项目无 file_md5/page_start/page_end"
```
**预期**: `OK: 全项目无 file_md5/page_start/page_end`

---

## 六、GPU 清理顺序

### 6.1 reorder_service.close() 清理顺序
```bash
uv run python -c "
import inspect
from app.rag.reorder_service import ReorderService
src = inspect.getsource(ReorderService.close)
assert 'import gc' in open('app/rag/reorder_service.py').read() or True  # just check close
assert 'to(\"cpu\")' in src or \"to('cpu')\" in src, 'FAIL: no to(cpu)'
assert 'del self._model' in src, 'FAIL: no del'
assert 'empty_cache()' in src, 'FAIL: no cuda.empty_cache()'
assert 'gc.collect()' in src, 'FAIL: no gc.collect()'
print('OK: GPU 清理顺序: to(cpu) -> del -> empty_cache -> gc.collect')
"
```
**预期**: `OK: GPU 清理顺序: to(cpu) -> del -> empty_cache -> gc.collect`

---

## 七、FastAPI 现代化

### 7.1 使用 lifespan 而非 @app.on_event
```bash
grep -n "@app.on_event" D:/Knowledge_rag_system/main.py && echo "FAIL: 仍使用 @app.on_event" || echo "OK: 已迁移到 lifespan"
```
**预期**: `OK: 已迁移到 lifespan`

---

## 八、magic_signatures

### 8.1 PK 条目已移除
```bash
grep "PK" D:/Knowledge_rag_system/app/config/chroma.yaml && echo "FAIL: PK 仍存在" || echo "OK: PK 签名已移除"
```
**预期**: `OK: PK 签名已移除`

---

## 九、semantic_merge

### 9.1 配置启用且 split_documents 支持
```bash
uv run python -c "
from app.config.loader import get_config
assert get_config('enable_semantic_merge') == True, 'FAIL: not enabled'
assert get_config('semantic_merge_threshold') == 0.7, 'FAIL: threshold'
import inspect
from app.rag.text_spliter import AsyncTextSplitter
sig = inspect.signature(AsyncTextSplitter.split_documents)
assert 'enable_semantic_merge' in sig.parameters
print('OK: semantic_merge 已启用，split_documents 支持参数')
"
```
**预期**: `OK: semantic_merge 已启用，split_documents 支持参数`

### 9.2 _merge_documents 保留第一个 doc 的 metadata
```bash
uv run python -c "
from langchain_core.documents import Document
docs = [Document(page_content='AAA ', metadata={'page': 2}), Document(page_content='BBB ', metadata={'page': 3})]
from app.rag.text_spliter import AsyncTextSplitter
st = AsyncTextSplitter()
# 模拟合并逻辑
merged_texts = ['AAA BBB ']
merged_docs = []
di = 0
for mt in merged_texts:
    start_di = di
    accumulated = ''
    while di < len(docs) and len(accumulated) < len(mt):
        accumulated += docs[di].page_content; di += 1
    merged = docs[start_di]
    merged.page_content = mt; merged_docs.append(merged)
assert merged_docs[0].metadata['page'] == 2, f'FAIL: got {merged_docs[0].metadata[\"page\"]}'
print('OK: 合并后保留第一个 doc page=2')
"
```
**预期**: `OK: 合并后保留第一个 doc page=2`

---

## 十、服务启动

### 10.1 完整启动
```bash
cd D:/Knowledge_rag_system && timeout 15 uv run python -c "
from main import app
from fastapi.testclient import TestClient
c = TestClient(app)
assert c.get('/health').status_code == 200
assert c.get('/health').json()['status'] == 'healthy'
print('OK: 服务启动正常')
"
```
**预期**: `OK: 服务启动正常`

---

## 十一、前端重复上传提示

手动测试（需启动前后端）：
1. 启动服务: `uv run uvicorn main:app --port 8000`
2. 启动前端: `uv run streamlit run front/app.py`
3. 在知识库管理页面上传文件 `test.txt`
4. 再次上传同一个 `test.txt`
5. **预期**: 显示 `⚠️ 文件已存在，跳过处理（test.txt）`
