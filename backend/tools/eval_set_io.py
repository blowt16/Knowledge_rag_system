"""评测集的导出 / 导入（§3.6）—— 一份 json 就是题库的 git 存档。

    cd backend && uv run python tools/eval_set_io.py export 默认题库
    cd backend && uv run python tools/eval_set_io.py import eval_sets/默认题库.json

**它不是运行时存储，是导出落脚点。** 界面上新建/改用例**只写库，磁盘上不落东西**；
只有手动跑一次这里的 `export`（或点一次界面上的「导出评测集」），文件才会被写出来。
一个评测集 = **一个 json 文件**（不是目录）。

⚠️ 界面上的「导出评测集」按钮与这里**共用同一个序列化函数**
   （`app/eval/sets.py::serialize_set`）—— 两边各写一份迟早会漂，导出的文件就对不上了。
   区别只在出口：这里写进 `backend/eval_sets/`，界面走 HTTP 让浏览器存到本地。

导入语义（写死，别有歧义）：按 `id` upsert，**只写文件里出现的列**；
`set` 按 `name` 找、找不到就新建；可重复跑（幂等）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app import db  # noqa: E402
from app.core.config import BACKEND_DIR  # noqa: E402
from app.eval import sets  # noqa: E402

#: 导出文件的落脚点。（`README.md` 也在里面，不受导出影响。）
DEFAULT_DIR = BACKEND_DIR / "eval_sets"


async def cmd_export(args: argparse.Namespace) -> int:
    if not args.all and not args.name:
        print("❌ 要么给评测集名，要么加 --all", file=sys.stderr)
        return 1
    out_dir = Path(args.dir) if args.dir else DEFAULT_DIR
    await db.init_pool()
    try:
        async with db.tx() as conn:
            names = [r["name"] for r in await conn.fetch(
                "SELECT name FROM eval_sets ORDER BY name")]
            if args.all:
                if not names:
                    print("库里一个评测集都没有", file=sys.stderr)
                    return 1
                targets = names
            else:
                targets = [args.name]

            for name in targets:
                try:
                    payload = await sets.export_payload(conn, name)
                except sets.SetNotFound:
                    print(f"❌ 没有这个评测集：{name}", file=sys.stderr)
                    print(f"   库里现有：{'、'.join(names) or '（空）'}", file=sys.stderr)
                    return 1
                path = sets.write_export_file(payload, out_dir)
                print(f"✅ {name} → {path.relative_to(REPO)}（{len(payload['cases'])} 条用例）")
    finally:
        await db.close_pool()
    return 0


async def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        print(f"❌ 找不到文件：{path}", file=sys.stderr)
        return 1

    await db.init_pool()
    try:
        async with db.tx() as conn:
            stats = await sets.import_file(conn, path)
    finally:
        await db.close_pool()

    print(f"✅ 导入 {path.name}："
          f"{'新建评测集' if stats['set_created'] else '并入已有评测集'}，"
          f"新增 {stats['created']} 题 / 更新 {stats['updated']} 题")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="评测集导出 / 导入（§3.6）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="库 → backend/eval_sets/<评测集名>.json")
    p_exp.add_argument("name", nargs="?", help="评测集名（与 --all 二选一）")
    p_exp.add_argument("--all", action="store_true", help="导出全部评测集")
    p_exp.add_argument("--dir", default=None, help=f"输出目录（默认 {DEFAULT_DIR}）")
    p_exp.set_defaults(func=cmd_export)

    p_imp = sub.add_parser("import", help="文件 → 库（按 id upsert，可重复跑）")
    p_imp.add_argument("file", help="json 文件路径")
    p_imp.set_defaults(func=cmd_import)

    args = ap.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(main())
