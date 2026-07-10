"""Focused tests for Phase 2 security risk detectors."""

from __future__ import annotations

import pytest

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.risk_rules.features import DiffFeatures
from codeguard_agent.pipeline.risk_rules.security import (
    detect_authentication_session,
    detect_authorization,
    detect_config_security,
    detect_data_exposure,
    detect_file_path_io,
    detect_input_validation,
    detect_injection,
    detect_ssrf_outbound,
    detect_web_security_config,
)


def features(*added: str, deleted: tuple[str, ...] = (), path: str = "src/App.java") -> DiffFeatures:
    return DiffFeatures(
        path=path,
        added_lines=tuple((index + 10, line) for index, line in enumerate(added)),
        deleted_lines=deleted,
        context_lines=(),
        has_added=bool(added),
        has_deleted=bool(deleted),
        has_changed=bool(added) and bool(deleted),
    )


@pytest.mark.parametrize(
    ("detector", "tag", "added", "deleted"),
    [
        (detect_authorization, RiskTag.AUTHORIZATION, "@PreAuthorize(\"hasRole('ADMIN')\")", "@PreAuthorize(\"hasRole('ADMIN')\")"),
        (detect_authentication_session, RiskTag.AUTHENTICATION_SESSION, "validate BearerToken authentication", "validate BearerToken authentication"),
        (detect_web_security_config, RiskTag.WEB_SECURITY_CONFIG, "http.csrf().disable()", "http.csrf().disable()"),
        (detect_input_validation, RiskTag.INPUT_VALIDATION, "@Valid CreateRequest request", "@Valid CreateRequest request"),
        (detect_injection, RiskTag.INJECTION, "query = \"select * from users where id=\" + id", "query = \"select * from users where id=\" + id"),
        (detect_file_path_io, RiskTag.FILE_PATH_IO, "Files.readString(Paths.get(name))", "Files.readString(Paths.get(name))"),
        (detect_ssrf_outbound, RiskTag.SSRF_OUTBOUND, "client.get(URI.create(url))", "client.get(URI.create(url))"),
        (detect_config_security, RiskTag.CONFIG_SECURITY, "@Value(\"${apiKey}\") String apiKey", "@Value(\"${apiKey}\") String apiKey"),
        (detect_data_exposure, RiskTag.DATA_EXPOSURE, "log.info(\"token={}\", token)", "log.info(\"token={}\", token)"),
    ],
)
def test_security_detector_emits_canonical_signal(detector, tag, added, deleted):
    signal = detector(features(added, deleted=(deleted,)))[0]

    assert signal.tag is tag
    assert signal.score in {1, 2, 3}
    assert signal.source.startswith("text:changed:")
    assert signal.line == 10
    assert "需审查" in signal.reason


def test_deleting_authorization_guard_is_high_score_and_deleted_line_is_unknown():
    signal = detect_authorization(features(deleted=("@PreAuthorize(\"hasRole('ADMIN')\")",)))[0]

    assert signal.score == 3
    assert signal.source == "text:deleted:authorization"
    assert signal.line is None


def test_deleting_validation_is_high_score():
    signal = detect_input_validation(features(deleted=("@NotBlank String name",)))[0]

    assert signal.score == 3
    assert signal.source == "text:deleted:input_validation"


def test_path_only_does_not_emit_a_concrete_security_signal():
    assert detect_authorization(features(path="src/security/AuthController.java")) == []

