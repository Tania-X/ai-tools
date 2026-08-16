"""OTel 可观测性演示: 审查 → 生成可视化瀑布图页面(trace_report.html)。

用法: OTEL_ENABLED=1 DEEPSEEK_API_KEY=<key> python l3-eval/demo_otel_trace.py
产物: l3-eval/trace_report.html — 浏览器打开即可查看完整 trace 瀑布图
     (根 span → 批次 → LLM 往返/工具调用, 含耗时与属性)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pr-review"))

from gateway.client import GatewayConfig, LLMClient, ProviderConfig
from gateway.otel import reset_tracing, setup_tracing
from pr_review.config import QualityConfig, ReviewConfig
from pr_review.github import PRInfo
from pr_review.review import ReviewRunner

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
PR = PRInfo(number=777, title="otel demo", body="", head_sha="x", head_ref="demo", base_ref="main")

PATCH = (
    "diff --git a/l3-eval/case-refactor-context/src/user.go b/l3-eval/case-refactor-context/src/user.go\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/l3-eval/case-refactor-context/src/user.go\n"
    "@@ -0,0 +1,6 @@\n"
    "+// Package main 提供用户示例。\n"
    "+package main\n"
    "+\n"
    "+// getUserName 根据 id 返回用户名。\n"
    "+func getUserName(id string) string {\n"
    "+\treturn \"alice\"\n"
    "+}\n"
)


def span_to_dict(s) -> dict:
    return {
        "name": s.name,
        "span_id": s.context.span_id,
        "parent_id": s.parent.span_id if s.parent else None,
        "start_ms": s.start_time / 1e6,
        "end_ms": s.end_time / 1e6,
        "duration_ms": round((s.end_time - s.start_time) / 1e6, 1),
        "attributes": {k: str(v) for k, v in (s.attributes or {}).items()},
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>审查链路 Trace 瀑布图</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; background: #fafaf8; margin: 0; padding: 24px; color: #2c2c2a; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .sub {{ color: #6b6a66; font-size: 12px; margin-bottom: 20px; }}
  .row {{ display: flex; align-items: center; margin: 2px 0; }}
  .label {{ width: 260px; font-size: 12px; padding-right: 12px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .track {{ position: relative; flex: 1; height: 22px; background: #f1efe8; border-radius: 4px; }}
  .bar {{ position: absolute; top: 3px; height: 16px; border-radius: 3px; opacity: 0.9; }}
  .dur {{ width: 90px; font-size: 11px; color: #6b6a66; padding-left: 8px; }}
  .legend {{ margin-top: 18px; font-size: 11px; color: #6b6a66; }}
  .legend span {{ display: inline-block; margin-right: 14px; }}
  .swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }}
  .attrs {{ margin: 14px 0 0 272px; font-size: 11px; color: #555; background: #fff; border: 1px solid #e3e1da; border-radius: 6px; padding: 10px 14px; max-width: 720px; }}
  .attrs div {{ padding: 1px 0; }}
</style>
</head>
<body>
<h1>审查链路 Trace</h1>
<div class="sub">pr_review.run → review.batch → llm.chat / repo_tools.execute · 生成时间 {time}</div>
<div id="chart"></div>
<div class="legend">
  <span><span class="swatch" style="background:#185fa5"></span>llm.chat</span>
  <span><span class="swatch" style="background:#0f6e56"></span>repo_tools.execute</span>
  <span><span class="swatch" style="background:#854f0b"></span>review.batch</span>
  <span><span class="swatch" style="background:#6b6a66"></span>pr_review.run</span>
</div>
<div class="attrs" id="attrs"></div>
<script>
const SPANS = {data};
const COLORS = {{"llm.chat": "#185fa5", "repo_tools.execute": "#0f6e56", "review.batch": "#854f0b", "pr_review.run": "#6b6a66"}};
// 树形整理
const byId = {{}};
SPANS.forEach(s => byId[s.span_id] = s);
const root = SPANS.find(s => !s.parent_id);
const t0 = root ? root.start_ms : Math.min(...SPANS.map(s => s.start_ms));
const tEnd = Math.max(...SPANS.map(s => s.end_ms));
const total = tEnd - t0;
function childrenOf(id) {{ return SPANS.filter(s => s.parent_id === id); }}
// 按树序展开(深度优先)
const ordered = [];
(function walk(s, depth) {{ ordered.push({{...s, depth}}); childrenOf(s.span_id).forEach(c => walk(c, depth + 1)); }})(root || {{span_id: null}}, 0);
const chart = document.getElementById("chart");
ordered.forEach(s => {{
  const row = document.createElement("div"); row.className = "row";
  const label = document.createElement("div"); label.className = "label";
  label.textContent = "  ".repeat(s.depth) + s.name;
  const track = document.createElement("div"); track.className = "track";
  const bar = document.createElement("div"); bar.className = "bar";
  bar.style.left = ((s.start_ms - t0) / total * 100) + "%";
  bar.style.width = Math.max((s.duration_ms / total * 100), 0.6) + "%";
  bar.style.background = COLORS[s.name] || "#8a8a86";
  bar.title = s.name + " " + s.duration_ms + "ms\\n" + JSON.stringify(s.attributes);
  track.appendChild(bar);
  const dur = document.createElement("div"); dur.className = "dur"; dur.textContent = s.duration_ms + "ms";
  row.appendChild(label); row.appendChild(track); row.appendChild(dur);
  chart.appendChild(row);
}});
// 属性面板(汇总关键信息)
const attrsBox = document.getElementById("attrs");
const summary = {{}};
ordered.forEach(s => Object.entries(s.attributes).forEach(([k, v]) => {{
  summary[s.name + "." + k] = v;
}}));
const lines = [
  "pr.number=" + (summary["pr_review.run.pr.number"] || "?"),
  "pr.files=" + (summary["pr_review.run.pr.files"] || "?"),
  "LLM 往返=" + ordered.filter(s => s.name === "llm.chat").length,
  "工具调用=" + ordered.filter(s => s.name === "repo_tools.execute").length,
  "总耗时=" + Math.round(total) + "ms",
];
lines.forEach(l => {{ const d = document.createElement("div"); d.textContent = l; attrsBox.appendChild(d); }});
</script>
</body>
</html>"""


def main() -> None:
    # 上报模式: 设了 OTEL_EXPORTER_OTLP_ENDPOINT → 走 OTLP(远程 Jaeger/云平台);
    # 否则内存 exporter → 生成本地可视化 HTML
    otlp_mode = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if otlp_mode:
        from gateway.otel import setup_tracing as _setup

        _setup()
        spans: list[dict] = []
    else:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        reset_tracing()
        exporter = InMemorySpanExporter()
        setup_tracing(span_exporter=exporter)

    cfg = GatewayConfig(
        providers={
            "deepseek": ProviderConfig(
                name="deepseek",
                base_url="https://api.deepseek.com",
                api_keys=[KEY],
                model="deepseek-v4-flash",
                max_tokens=4096,
                temperature=0.3,
                timeout=120,
            )
        }
    )
    llm = LLMClient(cfg)
    review_cfg = ReviewConfig(quality_gate=QualityConfig(enabled=False))

    class FakeGitHub:
        def get_pr_info(self):
            return PR

        def get_pr_files(self):
            return [{"filename": "l3-eval/case-refactor-context/src/user.go", "status": "added", "patch": PATCH}]

    runner = ReviewRunner(
        github=FakeGitHub(),
        llm=llm,
        config=review_cfg,
        repo_root=str(ROOT),
    )
    result = runner.run()

    if otlp_mode:
        print(f"审查完成: issues={len(result.issues)} token={result.total_tokens} | 已上报 OTLP: {os.environ['OTEL_EXPORTER_OTLP_ENDPOINT']}")
        return

    spans = [span_to_dict(s) for s in exporter.get_finished_spans()]
    html = HTML_TEMPLATE.format(
        time=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        data=json.dumps(spans, ensure_ascii=False),
    )
    out = Path(__file__).parent / "trace_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"审查完成: issues={len(result.issues)} token={result.total_tokens}")
    print(f"trace 可视化已生成: {out}")
    print(f"span 数: {len(spans)} | LLM 往返: {sum(1 for s in spans if s['name']=='llm.chat')} | "
          f"工具调用: {sum(1 for s in spans if s['name']=='repo_tools.execute')} | 总耗时: {sum(s['duration_ms'] for s in spans):.0f}ms")


if __name__ == "__main__":
    main()
