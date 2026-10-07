-- ============================================================
-- 004_eval_sets.sql —— 评测集（§3 / §9）
-- ============================================================
-- 目标：把评测从「一张全局题库 + 一个消融表」改造成
--       **评测集 → 用例 → 按集跑测 → 出报告**，同时保留既有消融能力。
--       做法是在现有三张表上**纯增量扩展**，不动老字段、不动老接口语义。
--
-- ⚠️ **每一步都必须幂等。** `cli.py` 的 `init-db` 每次都按序重跑
--    `migrations/*.sql`（002/003 都是按这个标准写的）。不幂等有两个后果：
--      ① 重跑报错，中断整条迁移链；
--      ② 上线后有人把某题的 source 改成 manual，运维重跑一次 init-db
--         就被改回 generated —— **静默改用户数据**。
--
-- ⚠️ 集合归属**只写 `eval_runs.set_id` / `eval_cases.set_id` 两列，绝不进 `config`**。
--    塞进 config 会让字典非空 → `eval.config.switches()` 返回非 None
--    → 这一轮被判成「消融运行」→ 又跑成纯向量基线（§1.1① 那个坑）。

-- ============================================================
-- ① eval_sets —— 评测集
-- ============================================================
CREATE TABLE IF NOT EXISTS eval_sets (
    id          TEXT PRIMARY KEY,
    -- 界面显示成「售后问题测评（5 条用例）」，括号里的数**实时算**（§11.3-4）。
    -- UNIQUE：重名当场 409，不靠应用层查重（并发下查重会漏）。
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_by  TEXT REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- ② eval_cases 加 8 列（老字段一律不动，含 suite）
-- ============================================================
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS set_id             TEXT;
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS in_eval            BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS note               TEXT;
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS source_document_id TEXT;
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS source_chunk_id    TEXT;
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS source_page        INTEGER;
-- ⚠️ `source_snippet` **必须存**（决策 12）：文档重新索引后 chunk 边界会漂，
--    只有把那段原文抄下来，三个月后打开还看得见「当初模型是照着什么出的题」。
ALTER TABLE eval_cases ADD COLUMN IF NOT EXISTS source_snippet     TEXT;

-- ②续：回填来源（决策 19）：老题一律标「文档生成」—— 它们都不是在界面上手工敲的。
--
-- ⚠️⚠️ **建列与回填必须在同一个 DO 块里，靠「列还不存在」判断只跑一次。**
--    这是本文件唯一一处「不能拆开写」的地方。拆成
--        ADD COLUMN IF NOT EXISTS ...  +  单独一条 UPDATE
--    重跑时 ADD COLUMN 静默跳过，但那条 UPDATE **照跑** ——
--    每次 `init-db` 都会把 source 重新刷一遍。用「迁移前那批 id」去限定也没用：
--    `source` 在界面上**不可改**（§5.2），所以能被刷掉的恰恰只有那批老题，
--    而它们全是 generated —— 那条 id 列表挡不住任何真实损失，只挡住新题。
--    把两件事绑在一次「列不存在」判断上，才是真正的幂等。
DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'eval_cases' AND column_name = 'source') THEN
        ALTER TABLE eval_cases ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
        UPDATE eval_cases SET source = 'generated';
    END IF;
END $mig$;

-- 外键单独加（不用 ADD COLUMN 里的内联 REFERENCES）：内联写法没法做存在性判断，
-- 第二次重跑虽然会跳过整条 ADD COLUMN，但**约束名是自动生成的**，出问题时不好引用。
DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_cases_set_id_fkey'
                      AND conrelid = 'eval_cases'::regclass) THEN
        -- 删评测集**连带删它的用例**（决策 14）；历史 run 靠 set_name 快照不受影响
        ALTER TABLE eval_cases
            ADD CONSTRAINT eval_cases_set_id_fkey
            FOREIGN KEY (set_id) REFERENCES eval_sets(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_cases_source_document_id_fkey'
                      AND conrelid = 'eval_cases'::regclass) THEN
        -- 文档删了不该带走用例：「核对标准答案」靠 source_snippet 快照照样能显示
        ALTER TABLE eval_cases
            ADD CONSTRAINT eval_cases_source_document_id_fkey
            FOREIGN KEY (source_document_id) REFERENCES documents(id) ON DELETE SET NULL;
    END IF;
END $mig$;

-- `source` 的取值约束同样是存在性判断（ADD CONSTRAINT 不能重复加）
DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_cases_source_check'
                      AND conrelid = 'eval_cases'::regclass) THEN
        ALTER TABLE eval_cases
            ADD CONSTRAINT eval_cases_source_check
            CHECK (source IN ('manual', 'generated'));
    END IF;
END $mig$;

CREATE INDEX IF NOT EXISTS idx_eval_cases_set    ON eval_cases (set_id);
CREATE INDEX IF NOT EXISTS idx_eval_cases_source ON eval_cases (source);

-- ============================================================
-- ③ eval_runs 加 5 列
-- ============================================================
-- ⚠️ `set_id` **只写这一列，绝不进 `config`**（同模块头）。
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS set_id      TEXT;
-- 评测集名字的**快照** —— 评测集删了，历史里名字还在（决策 14）
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS set_name    TEXT;
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS total_cases INTEGER;
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS done_cases  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS error       TEXT;

DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_runs_set_id_fkey'
                      AND conrelid = 'eval_runs'::regclass) THEN
        ALTER TABLE eval_runs
            ADD CONSTRAINT eval_runs_set_id_fkey
            FOREIGN KEY (set_id) REFERENCES eval_sets(id) ON DELETE SET NULL;
    END IF;
END $mig$;

-- 耗时(ms) **不存列**，用 `finished_at - started_at` 现算 —— 少一个要保持同步的字段。

-- ============================================================
-- ④ eval_case_results 加 3 列 + 改两个外键
-- ============================================================
-- 前两条让**历史报告自给自足**（报告不再需要 join eval_cases）
ALTER TABLE eval_case_results ADD COLUMN IF NOT EXISTS question     TEXT;
ALTER TABLE eval_case_results ADD COLUMN IF NOT EXISTS ground_truth TEXT;
-- 单题失败原因（修 §1.1③：原来 `error` 写库时被丢掉了，库里失败题的原因全是空）
ALTER TABLE eval_case_results ADD COLUMN IF NOT EXISTS error        TEXT;

-- case_id 改为可空 —— 否则下面的 SET NULL 会被 NOT NULL 当场挡住
ALTER TABLE eval_case_results ALTER COLUMN case_id DROP NOT NULL;

-- 用例删了，历史那行还在，只是不再指向某个用例（解开 §1 第 6 条那个死结）
DO $mig$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'eval_case_results_case_id_fkey'
                  AND conrelid = 'eval_case_results'::regclass
                  AND confdeltype IS DISTINCT FROM 'n') THEN
        ALTER TABLE eval_case_results DROP CONSTRAINT eval_case_results_case_id_fkey;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_case_results_case_id_fkey'
                      AND conrelid = 'eval_case_results'::regclass) THEN
        ALTER TABLE eval_case_results
            ADD CONSTRAINT eval_case_results_case_id_fkey
            FOREIGN KEY (case_id) REFERENCES eval_cases(id) ON DELETE SET NULL;
    END IF;
END $mig$;

-- 删一轮 run = 连它的逐题结果一起删。不改的话 `DELETE /runs/{id}` 会被外键挡住
DO $mig$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'eval_case_results_run_id_fkey'
                  AND conrelid = 'eval_case_results'::regclass
                  AND confdeltype IS DISTINCT FROM 'c') THEN
        ALTER TABLE eval_case_results DROP CONSTRAINT eval_case_results_run_id_fkey;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_case_results_run_id_fkey'
                      AND conrelid = 'eval_case_results'::regclass) THEN
        ALTER TABLE eval_case_results
            ADD CONSTRAINT eval_case_results_run_id_fkey
            FOREIGN KEY (run_id) REFERENCES eval_runs(id) ON DELETE CASCADE;
    END IF;
END $mig$;

-- ============================================================
-- ⑤ 两个默认评测集
-- ============================================================
-- id 用**固定可读值**而不是 uuid：`init-db` 会重跑，随机 id 每次都不一样；
-- 而且 `seed-eval-cases`（新装环境的那条路）要能直接引用它们，不必先查一遍。
INSERT INTO eval_sets (id, name, description)
VALUES ('eval-set-default', '默认题库',
        'M5-1 生成的全量题库（suite=full 的 75 条）。评测集功能上线前就存在的老题都在这里。')
ON CONFLICT DO NOTHING;

INSERT INTO eval_sets (id, name, description)
VALUES ('eval-set-refusal-calib', '拒答校准小集',
        '拒答校准题（suite=refusal_calib 的 15 条）。CI 的 eval-calibration 与「跑校准小集」跑的就是它。')
ON CONFLICT DO NOTHING;

-- ============================================================
-- ⑥ 回填归属（**只补 NULL 的**，别覆盖用户后来改过的）
-- ============================================================
-- ⚠️ 「默认题库」是 75 条，不是 90 条：`suite='full'` 的 75 条进「默认题库」，
--    `suite='refusal_calib'` 的 15 条进「拒答校准小集」。
--    而**老的 `suite` 路径照旧跑全部 90 条**（决策 22）—— 两件事互不干扰。
UPDATE eval_cases SET set_id = 'eval-set-default'
 WHERE suite = 'full'          AND set_id IS NULL;

UPDATE eval_cases SET set_id = 'eval-set-refusal-calib'
 WHERE suite = 'refusal_calib' AND set_id IS NULL;

-- ============================================================
-- ⑦ 回填历史结果的问题与标准答案快照（**必做**）
-- ============================================================
-- §3.5 说加了快照列就「历史报告自给自足」，但库里已有的 112 行
-- question/ground_truth 全是 NULL。不回填的话，打开任何一轮历史报告，
-- 「问题」与「标准答案」两列都是空的 —— 而且接口看着完全正常。
UPDATE eval_case_results r
   SET question     = COALESCE(r.question, c.question),
       ground_truth = COALESCE(r.ground_truth, c.ground_truth)
  FROM eval_cases c
 WHERE r.case_id = c.id
   AND r.question IS NULL;
