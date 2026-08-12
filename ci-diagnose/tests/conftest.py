"""把 ci-diagnose 目录加入 sys.path(目录名含连字符,不能直接 import)。

说明: 不用 tests/__init__.py,因为那会让 pytest 把 tests 当顶层包,
与 pr-review/tests 冲突(模块名 tests.* 无法唯一解析)。
"""

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[1]  # ci-diagnose 目录
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
