-- 校园 RAG 系统 — 初始表结构（12 张表）
-- 依据：docs/校园RAG系统重构方案.md §3.3.1
--
-- 两条全局口径（§3.3.1）：
--   ① 类型：主键保持 TEXT（不换 UUID）；DATETIME → TIMESTAMPTZ；TEXT(JSON) → JSONB；布尔位保持 INTEGER
--   ② 隔离只有一个维度：角色（visibility + vis_* / visible_roles）。
--      本轮不做多租户 —— 不加 tenant_id、不启用 RLS。
--
-- 幂等：全部 CREATE ... IF NOT EXISTS，可重复执行。

-- ============================================================
-- 1. users
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('student', 'staff', 'admin')),
    -- 令牌版本：自增即让该用户所有已签发令牌失效（§3.7.1）
    token_version   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 2. documents —— 核心表
-- ============================================================
-- status 不含 'superseded'：版本新旧由检索期解析，不在写入时翻转（§3.3.1）
--   indexing 不满足折叠规则的 status='active'，自动被排除 —— 这是解开
--   「写入顺序」与「失败回滚」冲突的关键。
CREATE TABLE IF NOT EXISTS documents (
    id                    TEXT PRIMARY KEY,
    doc_group_id          TEXT NOT NULL,
    title                 TEXT NOT NULL,
    filename              TEXT NOT NULL,
    file_type             TEXT NOT NULL CHECK (file_type IN ('pdf', 'docx', 'pptx', 'md', 'txt')),
    md5                   TEXT NOT NULL,
    version               INTEGER NOT NULL,
    effective_date        DATE NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('indexing', 'active', 'disabled', 'failed')),
    visibility            TEXT NOT NULL CHECK (visibility IN ('public', 'restricted')),
    -- restricted 时生效，如 ["admin"]；进 Chroma 时展开成 vis_<角色> 布尔字段
    visible_roles         JSONB,
    source_path           TEXT,
    normalized_text_path  TEXT,
    uploader_id           TEXT REFERENCES users(id),
    chunk_count           INTEGER NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 并发上传同一 doc_group 时兜底：version 在同一事务内分配，本约束防撞车
    CONSTRAINT uq_documents_group_version UNIQUE (doc_group_id, version)
);

CREATE INDEX IF NOT EXISTS idx_documents_group    ON documents (doc_group_id);
CREATE INDEX IF NOT EXISTS idx_documents_status   ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_md5      ON documents (md5);
-- 版本判定按 title 精确匹配已有文档组（§3.4.3）
CREATE INDEX IF NOT EXISTS idx_documents_title    ON documents (title);

-- ============================================================
-- 3. ingestion_tasks —— 上传任务（支撑上传进度条）
-- ============================================================
-- batch_id / file_name 是 ZIP 批量上传必需的：一次 zip 产生 N 条任务，
--   没有 batch_id 就无法表达「整包处理了 m/N」，也无法表达包内哪个文件失败。
--   单文件上传时 batch_id = id，口径统一。
-- trace_id 必须显式落库：后台任务不能靠 contextvars 隐式继承（§3.2.3.1）
CREATE TABLE IF NOT EXISTS ingestion_tasks (
    id            TEXT PRIMARY KEY,
    doc_group_id  TEXT,
    batch_id      TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    trace_id      TEXT,
    status        TEXT NOT NULL CHECK (
                      status IN ('pending', 'parsing', 'chunking', 'embedding',
                                 'done', 'duplicate', 'failed')),
    progress      INTEGER NOT NULL DEFAULT 0,
    message       TEXT,
    error         TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    document_id   TEXT REFERENCES documents(id),
    uploader_id   TEXT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ingestion_batch  ON ingestion_tasks (batch_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_status ON ingestion_tasks (status);

-- ============================================================
-- 4. conversations
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    title               TEXT,
    -- 增量滚动压缩（§3.8.3）：完整历史 = compressed_summary + messages[compressed_count:]
    compressed_summary  TEXT,
    compressed_count    INTEGER NOT NULL DEFAULT 0,
    is_top              INTEGER NOT NULL DEFAULT 0,
    delete_flag         INTEGER NOT NULL DEFAULT 0,
    last_chat_time      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 会话列表排序固定为 is_top DESC, last_chat_time DESC（§3.7.2）
CREATE INDEX IF NOT EXISTS idx_conversations_list
    ON conversations (user_id, is_top DESC, last_chat_time DESC);

-- ============================================================
-- 5. messages
-- ============================================================
-- content 落库保留原文（含 [n] 标记），剥离只发生在拼 history 时（§3.3.1 / §3.8.4）
CREATE TABLE IF NOT EXISTS messages (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id),
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    citations        JSONB,
    -- 用户消息被路由到哪一类 —— last_route 的来源（§3.5.1）
    route            TEXT CHECK (route IN ('chat', 'clarify', 'knowledge')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, created_at);

-- ============================================================
-- 6. session_locks —— 会话长锁（§3.2.4）
-- ============================================================
-- session_id 直接做主键：锁的语义是「同一会话同时只允许一个生成」，
-- 主键唯一约束在数据库层就保证了这一点，不依赖应用层判断。
-- 注意：本表不设 FK —— 锁是临时协调状态，不该被会话删除牵连。
CREATE TABLE IF NOT EXISTS session_locks (
    session_id    TEXT PRIMARY KEY,
    holder        TEXT NOT NULL,
    acquired_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 到点即可被他人抢走（§3.2.4 的 ON CONFLICT ... WHERE expires_at < now()）
    expires_at    TIMESTAMPTZ NOT NULL,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_session_locks_expires ON session_locks (expires_at);

-- ============================================================
-- 7. qa_logs
-- ============================================================
-- role 改名为 user_role：本表每行是一轮问答，不存在「消息角色」，
--   原名会被实现成 messages.role 的语义（§3.3.1）
CREATE TABLE IF NOT EXISTS qa_logs (
    id                   TEXT PRIMARY KEY,
    session_id           TEXT,
    user_id              TEXT,
    -- OTel trace id（32 位十六进制）：与日志、Jaeger 的关联键（§3.2.3.2）
    trace_id             TEXT,
    user_role            TEXT CHECK (user_role IN ('student', 'staff', 'admin')),
    question             TEXT,
    resolved_query       TEXT,
    route                TEXT CHECK (route IN ('chat', 'clarify', 'knowledge')),
    retrieved_chunk_ids  JSONB,
    reranked_chunk_ids   JSONB,
    answer               TEXT,
    is_refused           INTEGER NOT NULL DEFAULT 0,
    -- 取值见 §5.2 拒答原因取值表：no_candidate / insufficient_evidence
    refusal_reason       TEXT CHECK (refusal_reason IN ('no_candidate', 'insufficient_evidence')),
    verify_report        JSONB,
    -- 规则命中率的数据源
    route_source         TEXT CHECK (route_source IN ('rule', 'llm')),
    degraded             INTEGER NOT NULL DEFAULT 0,
    latency_ms           INTEGER,
    node_timings         JSONB,
    token_usage          JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 按 trace_id 还原单次请求全链路（验收项）—— 必须建索引
CREATE INDEX IF NOT EXISTS idx_qa_logs_trace    ON qa_logs (trace_id);
CREATE INDEX IF NOT EXISTS idx_qa_logs_session  ON qa_logs (session_id);
CREATE INDEX IF NOT EXISTS idx_qa_logs_refused  ON qa_logs (is_refused, created_at DESC);

-- ============================================================
-- 8. degradation_events —— 降级事件
-- ============================================================
-- 单独建表而不是塞进 qa_logs.node_timings：后者是节点耗时字段，
-- 混进去会让仪表盘解析要特判。独立表还能直接 COUNT(*) GROUP BY kind。
-- qa_log_id 是下钻必需的：一个会话有多轮，没有它无法区分是哪一轮。
CREATE TABLE IF NOT EXISTS degradation_events (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    qa_log_id   TEXT REFERENCES qa_logs(id),
    node        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_degradation_kind  ON degradation_events (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_degradation_qalog ON degradation_events (qa_log_id);

-- ============================================================
-- 9. refusal_annotations —— 拒答标注
-- ============================================================
-- 单独建表而不是给 qa_logs 加列：标注是稀疏的运维动作，
-- 生命周期与只增的观测日志不同（可反复修改）。
CREATE TABLE IF NOT EXISTS refusal_annotations (
    id                     TEXT PRIMARY KEY,
    -- 唯一：一条拒答记录只保留最新一次标注
    qa_log_id              TEXT NOT NULL UNIQUE REFERENCES qa_logs(id),
    suggested_document_id  TEXT REFERENCES documents(id),
    note                   TEXT,
    annotated_by           TEXT REFERENCES users(id),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 10. eval_cases —— 测试集
-- ============================================================
CREATE TABLE IF NOT EXISTS eval_cases (
    id                  TEXT PRIMARY KEY,
    question            TEXT NOT NULL,
    ground_truth        TEXT,
    expected_doc_ids    JSONB,
    expected_chunk_ids  JSONB,
    case_type           TEXT NOT NULL CHECK (
                            case_type IN ('factual', 'cross_paragraph', 'doc_number',
                                          'refusal', 'restricted', 'multi_turn')),
    -- 多轮用例的轮次脚本 [{question, ground_truth, expected_doc_ids}]；单轮为 NULL
    turns               JSONB,
    -- 受限题专用：该题要求的可见范围（如 ["admin"]），5.3 的 ACL 对照实验用它挑题
    visible_roles       JSONB,
    -- 题集归属：full（默认）/ refusal_calib（10–15 题拒答校准小集）
    suite               TEXT NOT NULL DEFAULT 'full' CHECK (suite IN ('full', 'refusal_calib')),
    -- A 组指标专用：期望的路由类别
    expected_route      TEXT CHECK (expected_route IN ('chat', 'clarify', 'knowledge')),
    -- A 组指标专用：0/1，该题是否应当触发澄清
    should_clarify      INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_cases_suite ON eval_cases (suite);
CREATE INDEX IF NOT EXISTS idx_eval_cases_type  ON eval_cases (case_type);

-- ============================================================
-- 11. eval_runs —— 每次评测运行
-- ============================================================
-- role / include_restricted 不放 config：它们是「对照实验的分组维度」，
-- 不是消融开关，混进去会让消融表的行含义不纯（§3.3.1）
CREATE TABLE IF NOT EXISTS eval_runs (
    id                  TEXT PRIMARY KEY,
    name                TEXT,
    -- 消融实验的对照依据：
    --   bm25 / rrf / rerank / expand_verbatim / expand_keywords / expand_hyde
    --   + 不对应叠加表行的 rrf_weighted / resolve
    config              JSONB,
    role                TEXT CHECK (role IN ('student', 'staff', 'admin')),
    include_restricted  INTEGER NOT NULL DEFAULT 0,
    -- 注意：只有 4 个取值，评测不做 SSE 进度流（轮询足够，§3.7.3）
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'running', 'done', 'failed')),
    metrics             JSONB,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs (status, started_at DESC);

-- ============================================================
-- 12. eval_case_results —— 单题结果（下钻用）
-- ============================================================
CREATE TABLE IF NOT EXISTS eval_case_results (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES eval_runs(id),
    case_id           TEXT NOT NULL REFERENCES eval_cases(id),
    retrieved_ids     JSONB,
    answer            TEXT,
    -- 本题返回的 chunk 中属于当前角色不可见文档的条数
    -- 5.3 的「越权返回次数应为 0」直接读它
    unauthorized_hits INTEGER NOT NULL DEFAULT 0,
    metrics           JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_case_results_run ON eval_case_results (run_id);
