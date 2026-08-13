"""把 golden-tests 目录加入 sys.path(目录名含连字符, 不能直接 import)。"""

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
