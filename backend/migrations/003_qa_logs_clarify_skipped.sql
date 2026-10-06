-- 「本轮因澄清轮次到顶而跳过澄清」的标记（负责人 2026-10-06）
--
-- 为什么需要它：澄清上限（单链 2 轮 / 会话 6 次）生效后，到顶那一轮**不再反问**、
-- 改为按最可能的理解作答 —— 它的 `route` 是 `knowledge`，与管理员主动问的正常轮次
-- **完全分不出来**。于是「哪些问题被反复反问、最后被强行作答」这件事在管理端是隐形的，
-- 而这正是限流机制唯一需要被盯住的那一面。
--
-- 幂等：`ADD COLUMN IF NOT EXISTS`，`init-db` 重跑安全。
ALTER TABLE qa_logs ADD COLUMN IF NOT EXISTS clarify_skipped BOOLEAN NOT NULL DEFAULT FALSE;
