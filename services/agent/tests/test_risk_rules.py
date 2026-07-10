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
    ("detector", "tag", "added", "deleted", "rule_id", "token"),
    [
        (detect_authorization, RiskTag.AUTHORIZATION, "@PreAuthorize(\"hasRole('ADMIN')\")", "@PreAuthorize(\"hasRole('USER')\")", "authorization_guard", "@PreAuthorize"),
        (detect_authentication_session, RiskTag.AUTHENTICATION_SESSION, "validate BearerToken principal", "validate BearerToken oldPrincipal", "authentication_session", "BearerToken"),
        (detect_web_security_config, RiskTag.WEB_SECURITY_CONFIG, "http.csrf().disable(); audit()", "http.csrf().disable(); oldAudit()", "web_security_weakening", "csrf().disable()"),
        (detect_input_validation, RiskTag.INPUT_VALIDATION, "@Valid CreateRequest request", "@Valid UpdateRequest request", "input_validation", "@Valid"),
        (detect_injection, RiskTag.INJECTION, "query = \"select * from users where id=\" + id", "query = \"select * from users where id=\" + userId", "injection_sql", "select"),
        (detect_file_path_io, RiskTag.FILE_PATH_IO, "Files.readString(Paths.get(name))", "Files.readString(Paths.get(path))", "file_path_io", "Files.readString"),
        (detect_ssrf_outbound, RiskTag.SSRF_OUTBOUND, "client.get(URI.create(url))", "client.get(URI.create(endpoint))", "ssrf_outbound", "URI"),
        (detect_config_security, RiskTag.CONFIG_SECURITY, "@Value(\"${apiKey}\") String apiKey", "@Value(\"${token}\") String token", "config_security", "@Value"),
        (detect_data_exposure, RiskTag.DATA_EXPOSURE, "return token;", "return token; // old", "data_exposure", "return token"),
    ],
)
def test_security_detector_emits_canonical_signal(detector, tag, added, deleted, rule_id, token):
    signal = detector(features(added, deleted=(deleted,)))[0]

    assert signal.tag is tag
    assert signal.score in {1, 2, 3}
    assert signal.source == f"text:changed:{rule_id}"
    assert signal.line == 10
    assert signal.reason.endswith(f"命中 {token}，需审查")


def test_deleting_authorization_guard_is_high_score_and_deleted_line_is_unknown():
    signal = detect_authorization(features(deleted=("@PreAuthorize(\"hasRole('ADMIN')\")",)))[0]

    assert signal.score == 3
    assert signal.source == "text:deleted:authorization_guard"
    assert signal.reason.endswith("命中 @PreAuthorize，需审查")
    assert signal.line is None


def test_deleting_validation_is_high_score():
    signal = detect_input_validation(features(deleted=("@NotBlank String name",)))[0]

    assert signal.score == 3
    assert signal.source == "text:deleted:input_validation"
    assert signal.reason.endswith("命中 @NotBlank，需审查")


def test_identical_added_and_deleted_matches_emit_directional_signals_not_changed():
    signals = detect_authorization(
        features(
            '@PreAuthorize("hasRole(\'ADMIN\')")',
            deleted=('@PreAuthorize("hasRole(\'ADMIN\')")',),
        )
    )

    assert {signal.source for signal in signals} == {
        "text:added:authorization_guard",
        "text:deleted:authorization_guard",
    }
    assert all(signal.reason.endswith("命中 @PreAuthorize，需审查") for signal in signals)


def test_unrelated_added_and_deleted_matches_keep_their_own_directions():
    signals = detect_authorization(
        features("@PreAuthorize(\"hasRole('ADMIN')\")", deleted=("hasRole('USER')",))
    )

    assert {signal.source for signal in signals} == {
        "text:added:authorization_guard",
        "text:deleted:authorization_guard",
    }


def test_harmless_web_security_token_is_not_high_score():
    signals = detect_web_security_config(features("http.csrf().enable()"))

    assert [(signal.source, signal.score) for signal in signals] == [("text:added:web_security_config", 1)]


@pytest.mark.parametrize(
    ("line", "token"),
    [
        ("http.csrf().disable()", "csrf().disable()"),
        ('requestMatchers("/public").permitAll()', "permitAll"),
        ("anonymous()", "anonymous"),
    ],
)
def test_web_security_weakening_additions_score_three(line, token):
    signal = detect_web_security_config(features(line))[0]

    assert signal.source == "text:added:web_security_weakening"
    assert signal.score == 3
    assert signal.reason.endswith(f"命中 {token}，需审查")


def test_injection_detects_concatenated_command_text():
    signal = detect_injection(features('command = "sh -c " + userCommand'))[0]

    assert signal.source == "text:added:injection_command"
    assert signal.score == 3
    assert signal.reason.endswith("命中 sh -c，需审查")


def test_path_only_does_not_emit_a_concrete_security_signal():
    assert detect_authorization(features(path="src/security/AuthController.java")) == []
