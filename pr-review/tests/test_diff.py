"""parse_diff / FileDiff 单测(零网络)。"""

from pr_review.diff import parse_diff

SAMPLE_DIFF = """diff --git a/src/hello.py b/src/hello.py
--- a/src/hello.py
+++ b/src/hello.py
@@ -1,2 +1,5 @@
 def greet(name):
-    return "Hello, " + name
+    return f"Hello, {name}"
+
+def shout(name):
+    return greet(name).upper()
"""

ADD_FILE_DIFF = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/new.py
@@ -0,0 +1,4 @@
+import os
+
+def load():
+    return os.getenv("HOME", "")
"""

DELETE_FILE_DIFF = """diff --git a/old.py b/old.py
deleted file mode 100644
index 1234567..0000000
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def unused():
-    pass
"""

RENAME_DIFF = """diff --git a/src/old.py b/src/new.py
similarity index 90%
rename from src/old.py
rename to src/new.py
--- a/src/old.py
+++ b/src/new.py
@@ -1,1 +1,1 @@
-def old():
+def new():
"""


def test_parse_modified_file():
    files = parse_diff(SAMPLE_DIFF)
    assert len(files) == 1
    fd = files[0]
    assert fd.path == "src/hello.py"
    assert fd.old_path == "src/hello.py"
    assert len(fd.hunks) == 1


def test_added_lines_with_numbers():
    fd = parse_diff(SAMPLE_DIFF)[0]
    added = fd.added
    # diff 行内容保留缩进: '+    return' 的 4 空格是内容一部分
    assert added == [
        (2, '    return f"Hello, {name}"'),
        (3, ""),  # 空行也算新增
        (4, "def shout(name):"),
        (5, "    return greet(name).upper()"),
    ]


def test_removed_lines_with_numbers():
    fd = parse_diff(SAMPLE_DIFF)[0]
    assert fd.removed == [(2, '    return "Hello, " + name')]


def test_new_file_from_dev_null():
    files = parse_diff(ADD_FILE_DIFF)
    assert len(files) == 1
    fd = files[0]
    assert fd.path == "new.py"
    assert fd.old_path == "/dev/null"
    assert fd.added[0] == (1, "import os")
    assert fd.added[-1] == (4, '    return os.getenv("HOME", "")')


def test_deleted_file_to_dev_null():
    fd = parse_diff(DELETE_FILE_DIFF)[0]
    assert fd.path == "old.py"  # +++ /dev/null 不覆盖路径
    assert fd.old_path == "old.py"
    assert fd.removed == [(1, "def unused():"), (2, "    pass")]
    assert fd.added == []


def test_rename_file():
    fd = parse_diff(RENAME_DIFF)[0]
    assert fd.old_path == "src/old.py"
    assert fd.path == "src/new.py"
    assert fd.removed == [(1, "def old():")]
    assert fd.added == [(1, "def new():")]


def test_line_count():
    # line_count = 新增 + 删除(不含上下文)
    assert parse_diff(SAMPLE_DIFF)[0].line_count == 5
    assert parse_diff(DELETE_FILE_DIFF)[0].line_count == 2


def test_display_lines():
    fd = parse_diff(SAMPLE_DIFF)[0]
    lines = fd.to_display_lines()
    assert lines[0] == "@@ -1 +1 @@"  # 首行是 hunk 头
    assert any("2 +     return f" in l for l in lines)
    assert any("2 -     return " in l for l in lines)


# GitHub REST API 的 files[].patch 字段常不带 diff --git 头, 直接从 @@ 开始
GITHUB_API_STYLE_PATCH = """@@ -0,0 +1,3 @@
+def foo():
+    pass
+    return 1
"""


def test_parse_github_api_style_patch():
    """裸 hunk(无 diff --git / --- / +++ 头)也必须解析出内容(回归测试)。"""
    files = parse_diff(GITHUB_API_STYLE_PATCH)
    assert len(files) == 1
    fd = files[0]
    assert len(fd.hunks) == 1
    assert fd.added == [(1, "def foo():"), (2, "    pass"), (3, "    return 1")]


def test_parse_mixed_style_patch():
    """完整 diff 中夹带裸 hunk 片段时互不干扰。"""
    patch = GITHUB_API_STYLE_PATCH + "\n" + SAMPLE_DIFF
    files = parse_diff(patch)
    assert len(files) == 2
    # 裸 hunk 在前, 完整 diff 在后
    assert files[0].added[0] == (1, "def foo():")
    assert files[1].path == "src/hello.py"
