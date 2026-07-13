"""评测运行身份必须描述实际调用，而非仅复述配置。"""

from types import SimpleNamespace

from evals import runner
from evals.archive import build_archive_record
from evals.metrics import aggregate
from evals.runner import _runtime_identity
from evals.schema import MatchOutcome


def test_mock_without_llm_hides_configured_real_model_name():
    settings = SimpleNamespace(provider="mock", model="deepseek-v4-pro")

    identity = _runtime_identity(settings, llm=None)

    assert identity.provider == "mock"
    assert identity.model == "(mock-no-llm)"
    assert identity.quality_metrics_meaningful is False


def test_real_llm_identity_preserves_executed_model():
    settings = SimpleNamespace(provider="openai", model="gpt-4o-mini")

    identity = _runtime_identity(settings, llm=object())

    assert identity.model == "gpt-4o-mini"
    assert identity.quality_metrics_meaningful is True


def test_archive_uses_runtime_identity_model_label():
    identity = _runtime_identity(
        SimpleNamespace(provider="mock", model="deepseek-v4-pro"),
        llm=None,
    )
    outcome = MatchOutcome(case_id="smoke", is_clean=True)

    record = build_archive_record(
        profile_name="pipeline-notools",
        profile_mode="pipeline",
        profile_tools=[],
        tools_enabled=False,
        provider=identity.provider,
        model=identity.model,
        runs=1,
        metrics=aggregate([[outcome]]),
        by_capability={},
        last_run=[outcome],
        git_sha="abc123",
        timestamp="2026-07-13T14-00-00",
    )

    assert record["provider"] == "mock"
    assert record["model"] == "(mock-no-llm)"


def test_runner_wires_runtime_identity_to_archive_and_report(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        provider="mock",
        model="deepseek-v4-pro",
        tool_server_url="",
        max_retries=3,
        structured_method="function_calling",
    )
    profile = SimpleNamespace(
        name="pipeline-notools",
        mode="pipeline",
        orchestration="adr-032",
        tools=[],
        fp_verify=False,
        model="",
        wants_tools=False,
    )
    outcome = MatchOutcome(case_id="smoke", is_clean=True)
    captured: dict[str, dict] = {}

    monkeypatch.setattr(runner.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(runner, "resolve_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(runner, "build_llm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "load_cases",
        lambda _path: [SimpleNamespace(id="smoke", capability="smoke")],
    )
    monkeypatch.setattr(
        runner,
        "run_once",
        lambda _cases, _review_fn, _judge_llm: [outcome],
    )

    def capture_archive(**kwargs):
        captured["archive"] = kwargs
        return {
            "timestamp": "2026-07-13T14-00-00",
            "git_sha": "abc123",
            "profile": {"name": profile.name},
        }

    def capture_report(*_args, **kwargs):
        captured["report"] = kwargs
        return "# report\n"

    monkeypatch.setattr(runner, "build_archive_record", capture_archive)
    monkeypatch.setattr(runner, "write_archive", lambda _record: tmp_path / "run.json")
    monkeypatch.setattr(runner, "load_archives", lambda: [])
    monkeypatch.setattr(runner, "render_report", capture_report)
    monkeypatch.setattr(runner, "render_history_views", lambda _history: "")

    exit_code = runner.main(
        ["--profile", profile.name, "--report", str(tmp_path / "report.md")]
    )

    assert exit_code == 0
    assert captured["archive"]["provider"] == "mock"
    assert captured["archive"]["model"] == "(mock-no-llm)"
    assert captured["report"]["model_label"] == "(mock-no-llm)"
    assert captured["report"]["quality_metrics_meaningful"] is False
