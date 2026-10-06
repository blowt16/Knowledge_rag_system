-- 用户可用性（§4.3 用户管理的「改角色 / 停用」）
--
-- 为什么需要它：`users` 表原先只有 id/username/password_hash/role/token_version/created_at，
-- **没有任何「停用」的落点** —— 而 §4.3 的用户管理页明确要求「改角色 / 停用」。
-- 停用必须是持久状态（不是「踢下线」）：停机重启、用户重新登录都该继续拦。
--
-- 幂等：`ADD COLUMN IF NOT EXISTS`，`init-db` 重跑安全。
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
