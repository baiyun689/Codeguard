"""Dashboard 生成：把 TraceReport 渲染为纯静态 HTML 文件。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from codeguard_agent.observability.models import TraceReport

logger = logging.getLogger("codeguard.observability")

_TEMPLATE_DIR = Path(__file__).resolve().parent


def _load_template() -> str:
    """加载 HTML 模板文件。"""
    template_path = _TEMPLATE_DIR / "dashboard_template.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Dashboard 模板不存在: {template_path}")
    return template_path.read_text(encoding="utf-8")


def render_dashboard(report: TraceReport) -> str:
    """把 TraceReport 渲染为完整的 HTML 字符串。模板中的 __TRACE_DATA__ 被替换为 JSON 数据。"""
    template = _load_template()
    data_json = _json_for_html_script(report)
    if "__TRACE_DATA__" not in template:
        logger.warning("Dashboard 模板缺少 __TRACE_DATA__ 占位符")
    return template.replace("__TRACE_DATA__", data_json)


def _json_for_html_script(report: TraceReport) -> str:
    """编码可安全嵌入 ``script[type=application/json]`` 的 JSON。"""
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return (
        payload
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_dashboard_file(report: TraceReport, output_dir: str, run_id: str) -> Path:
    """把 TraceReport 渲染为 HTML 文件，放到 output_dir 下。返回写入的文件路径。"""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_dashboard(report)
    file_path = out_dir / f"trace-{run_id[:8]}.html"
    file_path.write_text(html, encoding="utf-8")
    logger.info("追踪 Dashboard 已写入: %s", file_path)
    return file_path
