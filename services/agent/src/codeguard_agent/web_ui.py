"""Codeguard 本地审查 Web UI。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# Docker 将宿主机项目父目录挂载到固定容器路径；UI 接受宿主机绝对路径，
# 再用 CODEGUARD_HOST_PROJECT_ROOT 映射到容器内的项目路径。
PROJECT_ROOT = Path(os.environ.get("CODEGUARD_PROJECTS_CONTAINER_DIR", "/workspace/projects"))
HOST_PROJECT_ROOT = os.environ.get("CODEGUARD_HOST_PROJECT_ROOT", "").strip()
TRACE_ROOT = Path(os.environ.get("CODEGUARD_TRACE_DIR", "/app/trace"))
HOST = os.environ.get("CODEGUARD_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("CODEGUARD_UI_PORT", "8501"))
JOBS: dict[str, dict[str, object]] = {}
JOBS_LOCK = threading.Lock()


def _safe_project(value: str) -> Path:
    """将用户填写的宿主机路径映射为容器内安全路径。"""
    value = value.strip()
    if not value:
        raise ValueError("请输入本地项目根目录")
    candidate_value = value.replace("\\", "/")
    host_root = HOST_PROJECT_ROOT.replace("\\", "/").rstrip("/")
    if host_root and candidate_value.casefold().startswith(f"{host_root.casefold()}/"):
        candidate_value = candidate_value[len(host_root) + 1:]
    elif host_root and candidate_value.casefold() == host_root.casefold():
        candidate_value = "."
    if candidate_value.startswith("/workspace/projects/"):
        candidate_value = candidate_value.removeprefix("/workspace/projects/")
    candidate = (PROJECT_ROOT / candidate_value).resolve()
    root = PROJECT_ROOT.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("项目路径未挂载到 Docker，请检查 CODEGUARD_PROJECTS_DIR 配置")
    if not candidate.is_dir():
        raise ValueError("项目目录不存在或 Docker 尚未挂载该目录")
    return candidate


def _git_bases(repo: Path) -> list[str]:
    if not (repo / ".git").exists():
        return ["HEAD"]
    completed = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        capture_output=True,
        text=True,
        check=False,
    )
    branches = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return ["HEAD", *[branch for branch in branches if branch != "HEAD"]]


def _latest_report(repo: Path) -> str | None:
    reports = repo / "reports"
    files = sorted(reports.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


def _latest_trace() -> str | None:
    if not TRACE_ROOT.exists():
        return None
    files = sorted(TRACE_ROOT.rglob("*.html"), key=lambda item: item.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


def _host_path(path: str | None) -> str | None:
    """把容器内产物路径转换成用户可以在宿主机打开的路径。"""
    if not path or not HOST_PROJECT_ROOT:
        return path
    try:
        relative = Path(path).resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return path
    return str(Path(HOST_PROJECT_ROOT) / relative)


def _parse_review_output(stdout: str) -> tuple[dict[str, object] | None, str]:
    text = stdout.strip()
    if not text:
        return None, "没有检测到代码变更"
    try:
        result, end = json.JSONDecoder().raw_decode(text)
        if isinstance(result, dict):
            return result, text[end:].strip()
    except json.JSONDecodeError:
        pass
    return None, text


def _run_review(job_id: str, project: str, base: str, report: bool, trace: bool) -> None:
    try:
        repo = _safe_project(project)
        command = [
            os.environ.get("CODEGUARD_PYTHON", "python"), "-m", "codeguard_agent", "review",
            "--repo", str(repo), "--base", base, "--format", "json",
        ]
        if report:
            command.append("--report")
        command.extend(["--trace"] if trace else ["--no-trace"])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        result, trailing = _parse_review_output(completed.stdout)
        if result is None:
            summary, issue_count = trailing, 0
        else:
            summary = str(result.get("summary", "审查完成"))
            issues = result.get("issues", [])
            issue_count = len(issues) if isinstance(issues, list) else 0
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "completed", "summary": summary, "issue_count": issue_count,
                "result": result, "log": completed.stderr[-4000:], "exit_code": completed.returncode,
                "report_path": _latest_report(repo) if report else None,
                "report_host_path": _host_path(_latest_report(repo) if report else None),
                "report_host_dir_path": _host_path(str(repo / "reports")) if report else None,
                "trace_path": _latest_trace() if trace else None,
            })
    except Exception as exc:  # noqa: BLE001 - UI 必须把失败显示给用户
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "failed", "error": str(exc)})


class UIHandler(BaseHTTPRequestHandler):
    server_version = "CodeguardUI/1.0"

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, UI_HTML.encode("utf-8"), "text/html")
        elif parsed.path == "/api/bases":
            try:
                value = parse_qs(parsed.query).get("project", [""])[0]
                self._json({"bases": _git_bases(_safe_project(value))})
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif parsed.path.startswith("/api/jobs/"):
            parts = parsed.path.strip("/").split("/")
            job_id = parts[2] if len(parts) > 2 else ""
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if len(parts) == 4 and parts[3] in {"report", "trace"}:
                self._artifact(job, parts[3])
            else:
                self._json(job or {"error": "任务不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _artifact(self, job: dict[str, object] | None, kind: str) -> None:
        if not job:
            self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
            return
        path_value = job.get("report_path" if kind == "report" else "trace_path")
        if not isinstance(path_value, str) or not path_value:
            self._json({"error": "文件尚未生成"}, HTTPStatus.NOT_FOUND)
            return
        path = Path(path_value).resolve()
        allowed_root = PROJECT_ROOT.resolve() if kind == "report" else TRACE_ROOT.resolve()
        if allowed_root not in path.parents or not path.is_file():
            self._json({"error": "文件路径无效"}, HTTPStatus.NOT_FOUND)
            return
        content_type = "text/markdown" if kind == "report" else "text/html"
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path == "/api/review":
            self._start_review()
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def _start_review(self) -> None:
        try:
            payload = self._read_json()
            project = str(payload.get("project", ""))
            _safe_project(project)
            base = str(payload.get("base", "HEAD")).strip() or "HEAD"
            job_id = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                JOBS[job_id] = {"status": "running", "project": project, "base": base}
            threading.Thread(
                target=_run_review,
                args=(job_id, project, base, bool(payload.get("report", True)), bool(payload.get("trace", True))),
                daemon=True,
            ).start()
            self._json({"job_id": job_id}, HTTPStatus.ACCEPTED)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, *_args: object) -> None:
        return


def main() -> None:
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), UIHandler)
    print(f"Codeguard UI listening on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


UI_HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codeguard</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033;background:#f5f7fb}*{box-sizing:border-box}body{margin:0;min-height:100vh}.shell{max-width:680px;margin:auto;padding:42px 22px}.brand{display:flex;align-items:center;gap:10px;margin-bottom:68px}.mark{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;color:#fff;background:#5264e8;font-weight:800}.brand strong{font-size:17px}.brand span{display:block;color:#8993a7;font-size:12px;margin-top:2px}.card{background:#fff;border:1px solid #e4e8f0;border-radius:16px;padding:28px;box-shadow:0 16px 45px #24345d12}.card h1{font-size:21px;letter-spacing:-.02em;margin:0 0 27px}.field{margin-bottom:20px}.label{display:block;color:#647086;font-size:12px;font-weight:600;margin:0 0 8px}input,select{width:100%;height:44px;border:1px solid #d9dfeb;background:#fff;color:#172033;border-radius:9px;padding:0 12px;font:inherit;outline:none}input:focus,select:focus{border-color:#6573e8;box-shadow:0 0 0 3px #6573e81c}.hint{color:#96a0b2;font-size:11px;margin-top:7px}.checks{display:flex;gap:20px;margin:4px 0 24px}.check{display:flex;align-items:center;gap:8px;color:#536075;font-size:12px}.check input{width:15px;height:15px;accent-color:#5869e8}button{width:100%;height:44px;border:0;border-radius:9px;color:#fff;background:#5264e8;font:inherit;font-weight:700;cursor:pointer}button:hover{background:#4658da}button:disabled{opacity:.55;cursor:not-allowed}.result{display:none;margin-top:22px;padding-top:20px;border-top:1px solid #edf0f5}.result.show{display:block}.status{font-size:13px;font-weight:700;color:#5264e8}.stats{display:flex;gap:10px;margin-top:14px}.stat{flex:1;background:#f6f8fc;border-radius:10px;padding:12px}.stat b{display:block;font-size:20px}.stat span{color:#8893a7;font-size:11px}.message{margin-top:13px;white-space:pre-wrap;color:#69758a;font-size:11px;line-height:1.6;max-height:150px;overflow:auto}.links{display:flex;gap:15px;margin-top:12px}.links a{color:#5264e8;font-size:12px}.error{color:#d84e5e}@media(max-width:520px){.shell{padding:25px 16px}.brand{margin-bottom:45px}.card{padding:22px}.checks{gap:12px}}
</style></head><body><main class="shell"><header class="brand"><div class="mark">C</div><div><strong>Codeguard</strong><span>Local code review</span></div></header><section class="card"><h1>开始一次审查</h1><div class="field"><label class="label" for="project">项目根目录</label><input id="project" placeholder="例如：E:\\workspace\\demo-project"><div class="hint">请输入宿主机上的 Git 项目根目录，并确保该目录已通过 CODEGUARD_PROJECTS_DIR 挂载。</div></div><div class="field"><label class="label" for="base">Diff 基线</label><select id="base"><option value="HEAD">HEAD（最近一次提交）</option></select></div><div class="checks"><label class="check"><input id="report" type="checkbox" checked> Markdown 报告</label><label class="check"><input id="trace" type="checkbox" checked> Agent Trace</label></div><button id="run">开始审查</button><div id="result" class="result"><div id="status" class="status"></div><div class="stats"><div class="stat"><b id="count">—</b><span>最终问题</span></div><div class="stat"><b id="code">—</b><span>退出码</span></div></div><div id="message" class="message"></div><div id="links" class="links"></div></div></section></main><script>
const $=id=>document.getElementById(id);let timer;async function loadBases(){const value=$("project").value.trim();if(!value)return;const r=await fetch("/api/bases?project="+encodeURIComponent(value));const d=await r.json();if(!r.ok)return;const current=$("base").value;$("base").innerHTML=d.bases.map(b=>`<option value="${b}">${b}${b==='HEAD'?'（最近一次提交）':''}</option>`).join("");if(d.bases.includes(current))$("base").value=current)}
$("project").addEventListener("change",loadBases);$("project").addEventListener("blur",loadBases);$("run").onclick=async()=>{if(!$("project").value.trim())return alert("请输入项目根目录");$("run").disabled=true;$("result").classList.add("show");$("status").textContent="正在启动审查…";$("status").className="status";$("message").textContent="";$("links").innerHTML="";const r=await fetch("/api/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:$("project").value,base:$("base").value,report:$("report").checked,trace:$("trace").checked})});const d=await r.json();if(!r.ok){$("status").textContent=d.error;$("status").className="status error";$("run").disabled=false;return}timer=setInterval(()=>poll(d.job_id),1000)};async function poll(id){const r=await fetch("/api/jobs/"+id);const d=await r.json();$("status").textContent=d.status==='running'?"审查进行中…":d.status==='failed'?"审查失败":"审查完成";if(d.status==='running')return;clearInterval(timer);$("run").disabled=false;if(d.status==='failed'){$("message").textContent=d.error;return}$("count").textContent=d.issue_count??0;$("code").textContent=d.exit_code??0;$("message").textContent=d.summary+(d.log?'\n\n'+d.log:'');const links=[];if(d.report_path){const path=d.report_host_path||d.report_path;const directory=d.report_host_dir_path||path;const url=path.match(/^[A-Za-z]:[\\/]/)?'file:///'+directory.replace(/\\/g,'/'):'/api/jobs/'+id+'/report';links.push('<a target="_blank" href="'+url+'">打开报告目录</a><button style="width:auto;height:28px;padding:0 10px;font-size:11px" onclick="navigator.clipboard.writeText(\''+path.replace(/'/g,"\\'")+'\')">复制报告路径</button>')}if(d.trace_path)links.push('<a target="_blank" href="/api/jobs/'+id+'/trace">打开 Agent Trace</a>');$("links").innerHTML=links.join("")}
</script></body></html>'''


if __name__ == "__main__":
    main()
