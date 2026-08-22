"""Codeguard 本地审查 Web UI。

这是一个轻量的本地控制台：项目文件由 Docker 挂载到
``/workspace/projects``，审查仍然复用 ``codeguard_agent review`` CLI，
因此 UI 不复制或改变核心审查管线。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import zipfile


PROJECT_ROOT = Path(os.environ.get("CODEGUARD_PROJECTS_DIR", "/workspace/projects"))
TRACE_ROOT = Path(os.environ.get("CODEGUARD_TRACE_DIR", "/app/trace"))
HOST = os.environ.get("CODEGUARD_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("CODEGUARD_UI_PORT", "8501"))
JOBS: dict[str, dict[str, object]] = {}
JOBS_LOCK = threading.Lock()


def _safe_project(name: str) -> Path:
    candidate = (PROJECT_ROOT / name).resolve()
    root = PROJECT_ROOT.resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise ValueError("项目必须是 projects 目录下的一级子目录")
    return candidate


def _projects() -> list[dict[str, object]]:
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    result = []
    for path in sorted(PROJECT_ROOT.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        result.append({
            "name": path.name,
            "has_git": (path / ".git").exists(),
            "path": str(path),
        })
    return result


def _latest_report(repo: Path) -> str | None:
    reports = repo / "reports"
    files = sorted(reports.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


def _latest_trace() -> str | None:
    if not TRACE_ROOT.exists():
        return None
    files = sorted(TRACE_ROOT.rglob("*.html"), key=lambda item: item.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


def _parse_review_output(stdout: str) -> tuple[dict[str, object] | None, str]:
    """从 CLI 输出中提取 JSON，同时保留无变更/异常提示。"""
    text = stdout.strip()
    if not text:
        return None, "没有检测到代码变更"
    try:
        decoder = json.JSONDecoder()
        result, end = decoder.raw_decode(text)
        if isinstance(result, dict):
            return result, text[end:].strip()
    except json.JSONDecodeError:
        pass
    return None, text


def _run_review(job_id: str, project: str, base: str, report: bool, trace: bool) -> None:
    try:
        repo = _safe_project(project)
        command = [
            os.environ.get("CODEGUARD_PYTHON", "python"),
            "-m",
            "codeguard_agent",
            "review",
            "--repo",
            str(repo),
            "--base",
            base,
            "--format",
            "json",
        ]
        if report:
            command.append("--report")
        command.extend(["--trace", "--no-trace"] if not trace else ["--trace"])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        stdout = completed.stdout.strip()
        try:
            result, _trailing_output = _parse_review_output(stdout)
            if result is None:
                raise ValueError(_trailing_output)
            summary = result.get("summary", "审查完成")
            issues = result.get("issues", [])
            issue_count = len(issues) if isinstance(issues, list) else 0
        except (ValueError, TypeError):
            result = None
            summary = stdout or "没有检测到代码变更"
            issue_count = 0
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "completed",
                "summary": summary,
                "issue_count": issue_count,
                "result": result,
                "log": completed.stderr[-4000:],
                "exit_code": completed.returncode,
                "report_path": _latest_report(repo) if report else None,
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
        elif parsed.path == "/api/projects":
            self._json({"projects": _projects()})
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
        key = "report_path" if kind == "report" else "trace_path"
        path_value = job.get(key)
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
        parsed = urlparse(self.path)
        if parsed.path == "/api/review":
            self._start_review()
        elif parsed.path == "/api/import":
            self._import_zip(parse_qs(parsed.query).get("name", ["project.zip"])[0])
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
            thread = threading.Thread(
                target=_run_review,
                args=(job_id, project, base, bool(payload.get("report", True)), bool(payload.get("trace", True))),
                daemon=True,
            )
            thread.start()
            self._json({"job_id": job_id}, HTTPStatus.ACCEPTED)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _import_zip(self, filename: str) -> None:
        if not filename.lower().endswith(".zip"):
            self._json({"error": "只支持 ZIP 项目包"}, HTTPStatus.BAD_REQUEST)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100 * 1024 * 1024:
            self._json({"error": "项目包不能超过 100 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        safe_name = Path(filename).stem.replace(" ", "-") or "project"
        target = PROJECT_ROOT / safe_name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        archive = self.rfile.read(length)
        temp = PROJECT_ROOT / f".{safe_name}.zip"
        try:
            temp.write_bytes(archive)
            with zipfile.ZipFile(temp) as package:
                root = target.resolve()
                for member in package.infolist():
                    destination = (target / member.filename).resolve()
                    if root not in destination.parents and destination != root:
                        raise ValueError("ZIP 包包含非法路径")
                package.extractall(target)
            self._json({"project": safe_name})
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            self._json({"error": f"导入失败: {exc}"}, HTTPStatus.BAD_REQUEST)
        finally:
            temp.unlink(missing_ok=True)

    def log_message(self, *_args: object) -> None:
        return


def main() -> None:
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), UIHandler)
    print(f"Codeguard UI listening on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


UI_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codeguard · Local Review</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e8edf7;background:#0b1020}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 85% 0,#19345c 0,transparent 35%),#0b1020}.shell{max-width:1120px;margin:auto;padding:42px 24px}.brand{display:flex;align-items:center;gap:12px;margin-bottom:55px}.mark{display:grid;place-items:center;width:40px;height:40px;border-radius:13px;background:linear-gradient(135deg,#7c5cff,#24c8b6);font-weight:800;font-size:20px}.brand strong{font-size:18px}.brand span{color:#8592ae;font-size:13px;display:block;margin-top:3px}.hero{max-width:730px;margin-bottom:38px}.eyebrow{color:#66e0d2;text-transform:uppercase;letter-spacing:.15em;font-size:11px;font-weight:700}.hero h1{font-size:clamp(34px,6vw,62px);line-height:1.05;margin:15px 0 16px;letter-spacing:-.05em}.hero p{color:#9ba9c5;font-size:16px;line-height:1.7;margin:0}.grid{display:grid;grid-template-columns:1.25fr .75fr;gap:18px}.card{background:rgba(20,28,49,.82);border:1px solid #273653;border-radius:20px;padding:25px;box-shadow:0 18px 60px #0002;backdrop-filter:blur(12px)}.card h2{font-size:15px;margin:0 0 20px}.label{display:block;color:#91a0bc;font-size:12px;margin:0 0 8px}.row{display:flex;gap:10px}input,select{width:100%;border:1px solid #354564;background:#10182b;color:#eef3ff;border-radius:10px;padding:12px 13px;font:inherit;outline:none}input:focus,select:focus{border-color:#6c79ff}.upload{border:1px dashed #43547a;padding:11px;border-radius:10px;color:#9eabc3;font-size:12px;margin-top:12px}.actions{display:flex;gap:10px;margin-top:22px}button{border:0;border-radius:10px;padding:12px 18px;font:inherit;font-weight:700;cursor:pointer;color:#fff;background:linear-gradient(135deg,#7164ff,#4f8bff)}button.secondary{background:#202c47;color:#bdc8dd}button:disabled{cursor:not-allowed;opacity:.5}.checks{display:grid;gap:13px}.check{display:flex;gap:10px;align-items:center;color:#d4dced;font-size:13px}.check input{width:16px;height:16px;accent-color:#6d67ff}.pipeline{display:grid;gap:13px}.stage{display:flex;align-items:center;gap:12px;color:#b9c5da;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#43d4bd;box-shadow:0 0 15px #43d4bd}.result{margin-top:18px;display:none}.result.show{display:block}.status{color:#65e1d2;font-size:13px;margin-bottom:13px}.stats{display:flex;gap:10px;flex-wrap:wrap}.stat{background:#111a2f;border-radius:12px;padding:13px 16px;min-width:100px}.stat b{display:block;font-size:22px;color:#fff}.stat span{font-size:11px;color:#8492ae}.message{margin-top:15px;white-space:pre-wrap;color:#aab7cc;font-size:12px;line-height:1.6;max-height:180px;overflow:auto}.links{margin-top:15px;display:flex;gap:12px;flex-wrap:wrap}.links a{color:#8e9dff;font-size:12px}.hint{color:#8492ae;font-size:12px;line-height:1.7;margin-top:16px}.error{color:#ff8f9a}.footer{color:#687792;font-size:11px;margin-top:28px}@media(max-width:780px){.grid{grid-template-columns:1fr}.shell{padding:25px 16px}.brand{margin-bottom:38px}}
</style></head><body><main class="shell"><header class="brand"><div class="mark">C</div><div><strong>Codeguard</strong><span>Evidence-driven AI code review</span></div></header>
<section class="hero"><div class="eyebrow">Local review console</div><h1>让每一次代码变更，都有证据可循。</h1><p>选择一个 Git 项目，Codeguard 会运行风险路由、多 Agent 审查、证据校验与 Judge 裁决，并生成可追踪的审查结果。</p></section>
<section class="grid"><div class="card"><h2>开始一次审查</h2><label class="label" for="project">项目</label><select id="project"><option value="">正在加载项目…</option></select><div class="upload"><input id="zip" type="file" accept=".zip"> 上传 ZIP 项目（建议保留 Git 历史）</div><label class="label" for="base" style="margin-top:18px">Diff 基线</label><input id="base" value="HEAD" placeholder="HEAD / main / commit SHA"><div class="checks" style="margin-top:19px"><label class="check"><input id="report" type="checkbox" checked> 生成 Markdown 报告</label><label class="check"><input id="trace" type="checkbox" checked> 生成 Agent Trace</label></div><div class="actions"><button id="run">开始审查</button><button id="refresh" class="secondary">刷新项目</button></div><div class="hint">摘要、代码图谱、证据链和 Judge 按标准管线自动运行，不在界面中拆成独立开关。</div></div>
<div class="card"><h2>标准审查管线</h2><div class="pipeline"><div class="stage"><i class="dot"></i>Diff / 风险任务路由</div><div class="stage"><i class="dot"></i>Threat · Behavior · Maintainability</div><div class="stage"><i class="dot"></i>候选协调与上下文注入</div><div class="stage"><i class="dot"></i>Evidence Ledger 验证</div><div class="stage"><i class="dot"></i>Council Judge 裁决</div><div class="stage"><i class="dot"></i>因果语义合并</div></div><div id="result" class="result"><div id="status" class="status"></div><div class="stats"><div class="stat"><b id="count">—</b><span>最终问题</span></div><div class="stat"><b id="code">—</b><span>退出码</span></div></div><div id="message" class="message"></div><div id="links" class="links"></div></div></div></section><div class="footer">Codeguard · Python Agent + Java Gateway · local only</div></main>
<script>
const $=id=>document.getElementById(id); let timer;
async function loadProjects(){const r=await fetch('/api/projects');const d=await r.json();$('project').innerHTML=d.projects.length?d.projects.map(p=>`<option value="${p.name}">${p.name}${p.has_git?'':' · 非 Git 项目'}</option>`).join(''):'<option value="">请先导入或挂载项目</option>'}
async function importZip(){const f=$('zip').files[0];if(!f)return;const r=await fetch('/api/import?name='+encodeURIComponent(f.name),{method:'POST',body:f});const d=await r.json();if(!r.ok)return alert(d.error);await loadProjects();$('project').value=d.project}
$('zip').addEventListener('change',importZip);$('refresh').onclick=loadProjects;
$('run').onclick=async()=>{if(!$('project').value)return alert('请先选择项目');$('run').disabled=true;$('result').classList.add('show');$('status').textContent='正在启动审查…';$('message').textContent='';$('links').innerHTML='';const r=await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:$('project').value,base:$('base').value,report:$('report').checked,trace:$('trace').checked})});const d=await r.json();if(!r.ok){$('status').innerHTML='<span class="error">'+d.error+'</span>';$('run').disabled=false;return}timer=setInterval(()=>poll(d.job_id),1000)};
async function poll(id){const r=await fetch('/api/jobs/'+id);const d=await r.json();$('status').textContent=d.status==='running'?'审查进行中…':d.status==='failed'?'审查失败':'审查完成';if(d.status==='running')return;clearInterval(timer);$('run').disabled=false;if(d.status==='failed'){$('message').innerHTML='<span class="error">'+d.error+'</span>';return}$('count').textContent=d.issue_count??0;$('code').textContent=d.exit_code??0;$('message').textContent=d.summary+(d.log?'\n\n'+d.log:'');const links=[];if(d.report_path)links.push('<a target="_blank" href="/api/jobs/'+id+'/report">打开 Markdown 报告</a>');if(d.trace_path)links.push('<a target="_blank" href="/api/jobs/'+id+'/trace">打开 Agent Trace</a>');$('links').innerHTML=links.join('')}
loadProjects();
</script></body></html>'''


if __name__ == "__main__":
    main()
