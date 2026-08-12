"""ci-diagnose 单测:日志提取/诊断解析/评论格式/客户端,零真实调用。"""

from unittest.mock import MagicMock

from ci_diagnose.client import GitHubClient
from ci_diagnose.diagnose import Diagnoser, extract_relevant_log, parse_diagnose_json
from ci_diagnose.prompt import build_diagnose_messages, format_diagnosis_comment


# ---------------------------------------------------------------- 日志提取
def test_extract_relevant_log_keeps_error_lines():
    log = "\n".join(
        [
            "line1 ok",
            "line2 build",
            "line3 ERROR: undefined reference to 'foo'",
            "line4 context",
            "line5 tail",
            "line6 end",
        ]
    )
    excerpt = extract_relevant_log(log)
    assert "undefined reference" in excerpt
    assert "context" in excerpt  # 错误行 ±2 的上下文
    assert "line6 end" in excerpt  # 尾部保留


def test_extract_relevant_log_truncates_by_max_chars():
    long_log = "\n".join(f"normal line {i} padding padding padding" for i in range(200))
    excerpt = extract_relevant_log(long_log, max_chars=300)
    assert len(excerpt) <= 300


def test_extract_relevant_log_empty():
    assert extract_relevant_log("") == ""


def test_extract_relevant_log_dedup():
    log = "ERROR: boom\nERROR: boom\nERROR: boom\ntail"
    excerpt = extract_relevant_log(log)
    assert excerpt.count("ERROR: boom") == 1  # 去重保序


# ---------------------------------------------------------------- 诊断解析
def test_parse_diagnose_json_normal():
    diag = parse_diagnose_json(
        '{"summary": "构建失败", "root_cause": "依赖缺失", "location": "main.go:5", '
        '"suggestion": "go mod tidy", "fix": "go mod tidy"}'
    )
    assert diag["summary"] == "构建失败"
    assert diag["location"] == "main.go:5"


def test_parse_diagnose_json_with_fence():
    diag = parse_diagnose_json('```json\n{"summary": "s", "root_cause": "r"}\n```')
    assert diag["summary"] == "s"


def test_parse_diagnose_json_missing_fields():
    diag = parse_diagnose_json('{"summary": "s"}')
    assert diag["root_cause"] == "" and diag["fix"] == ""


# ---------------------------------------------------------------- 诊断 prompt/评论
def test_build_diagnose_messages():
    messages = build_diagnose_messages("CI", 42, "ERROR: x")
    assert "CI" in messages[1]["content"]
    assert "ERROR: x" in messages[1]["content"]
    assert "修复建议" in messages[0]["content"]


def test_format_diagnosis_comment():
    diag = {
        "summary": "测试失败",
        "root_cause": "断言错误",
        "location": "test_a.go:12",
        "suggestion": "修正断言",
        "fix": "assert.Equal(t, want, got)",
    }
    comment = format_diagnosis_comment(
        diag, workflow_name="Go CI", run_id=42, run_url="https://x", model="deepseek-chat"
    )
    assert "CI 失败诊断" in comment
    assert "测试失败" in comment and "断言错误" in comment
    assert "assert.Equal" in comment  # fix 代码块
    assert "查看失败日志" in comment


def test_format_diagnosis_comment_without_fix():
    diag = {"summary": "s", "root_cause": "", "location": "", "suggestion": "s2", "fix": ""}
    comment = format_diagnosis_comment(diag, workflow_name="CI", run_id=1)
    assert "修复示意" not in comment


# ---------------------------------------------------------------- 客户端(mock)
def _client():
    c = GitHubClient.__new__(GitHubClient)
    c.repo = "o/r"
    c._client = MagicMock()
    return c


def test_download_job_logs_zip():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("job.log", "line1\nline2 ERROR\n")
    c = _client()
    c._client.get.return_value = MagicMock(status_code=200, content=buf.getvalue())
    log = c.download_job_logs(123)
    assert "ERROR" in log


def test_download_job_logs_plain_text():
    c = _client()
    c._client.get.return_value = MagicMock(status_code=200, content=b"plain error log")
    assert c.download_job_logs(123) == "plain error log"


def test_find_pr_by_sha():
    c = _client()
    c._get = MagicMock(return_value=[
        {"number": 7, "head": {"sha": "abc"}},
        {"number": 8, "head": {"sha": "def"}},
    ])
    assert c.find_pr_by_sha("def") == 8
    assert c.find_pr_by_sha("zzz") is None


def test_post_issue_comment_payload():
    c = _client()
    c._post = MagicMock()
    c.post_issue_comment(7, "hello")
    c._post.assert_called_once_with("/repos/o/r/issues/7/comments", {"body": "hello"})


# ---------------------------------------------------------------- Diagnoser
def test_diagnoser_parses_llm_output():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(
        content='{"summary": "s", "root_cause": "r", "location": "l", "suggestion": "s2", "fix": ""}'
    )
    diag = Diagnoser(llm=llm, max_log_chars=1000).diagnose("CI", 1, "ERROR: boom\n" * 50)
    assert diag["root_cause"] == "r"
    # 日志已截断
    sent = llm.chat.call_args[0][0][1]["content"]
    assert len(sent) < 3000


def test_diagnoser_falls_back_on_bad_json():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(content="not json")
    diag = Diagnoser(llm=llm).diagnose("CI", 1, "ERROR: x")
    assert "解析失败" in diag["summary"]
