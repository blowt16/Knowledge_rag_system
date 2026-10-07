# `backend/eval_sets/` —— 评测集的导出落脚点

**这里不是运行时存储。** 界面上的新建/编辑/删除用例**只写数据库**，
磁盘上什么都不会落；只有**手动跑一次导出**，文件才会被写出来。

一个评测集 = **一个 json 文件**（不是目录）。

```bash
cd backend

# 库 → 文件
uv run python tools/eval_set_io.py export 默认题库
uv run python tools/eval_set_io.py export --all          # 全部导出

# 文件 → 库（按 id upsert，可重复跑，幂等）
uv run python tools/eval_set_io.py import eval_sets/默认题库.json
```

界面上的「导出评测集」按钮走的是**同一个序列化函数**
（`app/eval/sets.py::serialize_set`），区别只在出口：
命令行的文件落在这里，按钮的文件走 HTTP 让浏览器存到本地。

## 为什么不让文件当存储

| 界面要做的事 | 文件做不到 |
|---|---|
| 分页 / 按来源筛选 / 按问题搜索 | 得把整个文件读进内存再解析一遍 |
| 历史报告引用某条用例 | 文件没有外键 |
| 评测正在跑的时候改用例 | 行为无法定义 |
| 后端多进程同时写 | 会打架 |

所以**库里是运行时的事实来源，这里的 json 是给 git 用的存档** ——
可评审、可 diff、能进版本库。

## ⚠️ 导出文件里的字段一个都不能省

90 条老题里 **15 条多轮题靠 `turns`**、受限题靠 `visible_roles`、
A 组指标靠 `expected_route` / `should_clarify`。
漏字段 = 这份存档**静默损坏**，而且 round-trip 测试还测不出来 ——
导入是按字段写入的，缺的列保持库中原值，看起来「没丢」。

字段全集定义在 `app/eval/sets.py::CASE_FIELDS`，改它要同步改
`tests/integration/test_eval_set_io.py` 的往返比对。

## 导入语义

- 按 `id` upsert，**只写文件里出现的列**，缺的列保持库中原值
- `set` 按 `name` 找；**找不到就新建**
- 可重复跑（幂等）
