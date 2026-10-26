"""初始化 QuantDesk 数据库：建表 → 自检 → 打印表清单。

用法（项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\init_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from core.config import get_config
        from core.db import get_engine, init_db
    except Exception as e:  # 依赖或导入问题给出中文提示
        print(f"[失败] 无法导入核心模块：{type(e).__name__}: {e}")
        print("       请先确认已在项目根目录、并使用 .venv 解释器，且依赖已安装。")
        return 1

    cfg = get_config()
    print("=" * 56)
    print("QuantDesk 数据库初始化")
    print(f"  app            : {cfg.app.name} v{cfg.app.version}")
    print(f"  数据库 URL     : {cfg.database.url}")
    print(f"  数据目录       : {cfg.app.data_dir}")
    print("=" * 56)

    try:
        init_db()
    except Exception as e:
        print(f"[失败] 建表失败：{type(e).__name__}: {e}")
        return 1

    engine = get_engine()
    try:
        from sqlalchemy import inspect
        tables = sorted(inspect(engine).get_table_names())
    except Exception as e:
        print(f"[警告] 无法读取表清单：{e}")
        tables = []

    print(f"[成功] 数据库已就绪，共 {len(tables)} 张表：")
    for i, t in enumerate(tables, 1):
        print(f"  {i:>2}. {t}")

    # 可选：启动自检（部分实现提供 startup_check）
    try:
        from core import db as dbmod
        check = getattr(dbmod, "startup_check", None)
        if callable(check):
            ok, msg = (check(), None) if not isinstance(check(), tuple) else check()
            print(f"[自检] {msg or ('通过' if ok else '异常')}")
    except TypeError:
        pass
    except Exception as e:
        print(f"[自检] 跳过（{type(e).__name__}: {e}）")

    print("\n下一步：双击 scripts\\start.bat 启动界面，或运行 streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
