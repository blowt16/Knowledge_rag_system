-- ============================================================
-- 005_citation_and_dialogue_mode.sql —— 引用报告 + 评测对话模式
-- ============================================================
-- 两个变更放同一迁移：`messages.verify_report` 与
-- `eval_runs.dialogue_mode`（后者本期立即使用，避免再走一次迁移链）。
--
-- ⚠️ **CHECK 不能内联在 `ADD COLUMN IF NOT EXISTS` 里**：列已存在时整条语句跳过，
--    约束就永远补不上。与 004 同一写法：先加列，再用 DO 块按约束名补 CHECK。
--
-- ⚠️ **必须幂等**：`cli.py` 的 `init-db` 每次都按序重跑 `migrations/*.sql`。

ALTER TABLE messages ADD COLUMN IF NOT EXISTS verify_report JSONB;

ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS dialogue_mode TEXT NOT NULL DEFAULT 'single';

DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_runs_dialogue_mode_check'
                      AND conrelid = 'eval_runs'::regclass) THEN
        ALTER TABLE eval_runs
            ADD CONSTRAINT eval_runs_dialogue_mode_check
            CHECK (dialogue_mode IN ('single', 'multi'));
    END IF;
END $mig$;
