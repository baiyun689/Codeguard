"""Focused tests for Phase 2 security risk detectors."""

from __future__ import annotations

import pytest

from codeguard_agent.models.tasks import RiskSignal, RiskTag, ReviewTask
from codeguard_agent.pipeline.risk_rules import catalog
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
from codeguard_agent.pipeline.risk_rules.behavior import (
    detect_api_contract,
    detect_cache_consistency,
    detect_concurrency_consistency,
    detect_error_handling,
    detect_idempotency_retry,
    detect_message_delivery,
    detect_null_state_safety,
    detect_performance,
    detect_resource_lifecycle,
    detect_sql_data_access,
    detect_transaction_atomicity,
)
from codeguard_agent.pipeline.risk_rules.maintainability import (
    detect_complexity_control_flow,
    detect_duplication_design,
    detect_observability_testability,
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


@pytest.mark.parametrize(
    ("detector", "tag", "line", "rule_id", "token", "score"),
    [
        (detect_sql_data_access, RiskTag.SQL_DATA_ACCESS, '@Query("SELECT * FROM orders")', "sql_data_access", "@Query", 2),
        (detect_transaction_atomicity, RiskTag.TRANSACTION_ATOMICITY, "@Transactional", "transaction_atomicity", "@Transactional", 2),
        (detect_concurrency_consistency, RiskTag.CONCURRENCY_CONSISTENCY, "synchronized (lock) { update(); }", "concurrency_consistency", "synchronized", 2),
        (detect_idempotency_retry, RiskTag.IDEMPOTENCY_RETRY, "@Retryable", "idempotency_retry", "@Retryable", 2),
        (detect_cache_consistency, RiskTag.CACHE_CONSISTENCY, "@CacheEvict(cacheNames = \"orders\")", "cache_consistency", "@CacheEvict", 2),
        (detect_message_delivery, RiskTag.MESSAGE_DELIVERY, "@KafkaListener(topics = \"orders\")", "message_delivery", "@KafkaListener", 2),
        (detect_error_handling, RiskTag.ERROR_HANDLING, "catch (IOException exception) { }", "error_handling", "catch", 2),
        (detect_null_state_safety, RiskTag.NULL_STATE_SAFETY, "Objects.requireNonNull(order)", "null_state_safety", "Objects.requireNonNull", 2),
        (detect_resource_lifecycle, RiskTag.RESOURCE_LIFECYCLE, "try (InputStream stream = source.openStream()) {", "resource_lifecycle", "InputStream", 2),
        (detect_api_contract, RiskTag.API_CONTRACT, "@RequestMapping(\"/orders\")", "api_contract", "@RequestMapping", 2),
        (detect_performance, RiskTag.PERFORMANCE, "for (Order order : orders) { repository.findAll(); }", "performance", "findAll", 2),
        (detect_complexity_control_flow, RiskTag.COMPLEXITY_CONTROL_FLOW, "if (ready && valid && permitted) {", "complexity_control_flow", "if", 1),
        (detect_duplication_design, RiskTag.DUPLICATION_DESIGN, "service.save(order);\nservice.save(order);", "duplication_design", "service.save(order)", 1),
        (detect_observability_testability, RiskTag.OBSERVABILITY_TESTABILITY, "auditService.record(order);", "observability_side_effect", "auditService.record", 1),
    ],
)
def test_behavior_and_maintainability_detectors_emit_canonical_added_signal(
    detector, tag, line, rule_id, token, score
):
    signal = detector(features(line))[0]

    assert signal.tag is tag
    assert signal.score == score
    assert signal.source == f"text:added:{rule_id}"
    assert signal.line == 10
    assert signal.reason.endswith(f"命中 {token}，需审查")


@pytest.mark.parametrize(
    ("detector", "tag", "line", "rule_id", "token", "score"),
    [
        (detect_transaction_atomicity, RiskTag.TRANSACTION_ATOMICITY, "@Transactional", "transaction_atomicity", "@Transactional", 3),
        (detect_error_handling, RiskTag.ERROR_HANDLING, "catch (Exception ignored) { }", "error_handling", "catch", 3),
        (detect_resource_lifecycle, RiskTag.RESOURCE_LIFECYCLE, "executor.shutdown();", "resource_lifecycle", "executor.shutdown", 3),
        (detect_observability_testability, RiskTag.OBSERVABILITY_TESTABILITY, "logger.info(\"saved\")", "observability_protection", "logger.info", 3),
    ],
)
def test_deleted_behavior_and_maintainability_protections_are_high_score(
    detector, tag, line, rule_id, token, score
):
    signal = detector(features(deleted=(line,)))[0]

    assert signal.tag is tag
    assert signal.score == score
    assert signal.source == f"text:deleted:{rule_id}"
    assert signal.line is None
    assert signal.reason.endswith(f"命中 {token}，需审查")


def test_same_behavior_token_with_different_text_is_a_changed_signal():
    signal = detect_sql_data_access(
        features('@Query("SELECT * FROM orders")', deleted=('@Query("SELECT * FROM users")',))
    )[0]

    assert signal.source == "text:changed:sql_data_access"
    assert signal.line == 10


def test_duplication_long_statements_with_same_display_prefix_keep_directional_signals():
    added_statement = "service.save(order, customer, address, payment, addedValue)"
    deleted_statement = "service.save(order, customer, address, payment, removedValue)"

    signals = detect_duplication_design(
        features(
            f"{added_statement}; {added_statement};",
            deleted=(f"{deleted_statement}; {deleted_statement};",),
        )
    )

    assert {signal.source for signal in signals} == {
        "text:added:duplication_design",
        "text:deleted:duplication_design",
    }


def test_identical_duplicated_statements_keep_directional_signals():
    statement = "service.save(order)"

    signals = detect_duplication_design(features(f"{statement}; {statement};", deleted=(f"{statement}; {statement};",)))

    assert {signal.source for signal in signals} == {
        "text:added:duplication_design",
        "text:deleted:duplication_design",
    }


@pytest.mark.parametrize(
    "query_line",
    ["repository.findById(orderId);", "Files.readString(path);"],
)
def test_performance_detects_query_or_io_after_iteration_in_same_task(query_line):
    signal = detect_performance(features("for (Order order : orders) {", query_line))[0]

    assert signal.source == "text:added:performance"
    assert signal.line == 11


def test_null_state_safety_detects_ordinary_chained_dereference():
    signal = detect_null_state_safety(features("order.customer().address();"))[0]

    assert signal.source == "text:added:null_state_safety"
    assert signal.reason.endswith("命中 customer().address，需审查")


def test_observability_side_effect_is_not_suppressed_by_unrelated_logger():
    signals = detect_observability_testability(
        features('logger.info("starting");', "orderRepository.save(order);")
    )

    assert {signal.source for signal in signals} == {
        "text:added:observability_protection",
        "text:added:observability_side_effect",
    }


def test_path_only_does_not_emit_a_concrete_behavior_or_maintainability_signal():
    path_features = features(path="src/web/OrderController.java")

    assert detect_api_contract(path_features) == []
    assert detect_complexity_control_flow(path_features) == []


def test_registry_covers_every_concrete_tag_once_and_maps_reviewers():
    expected_reviewers = {
        RiskTag.AUTHORIZATION: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.AUTHENTICATION_SESSION: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.WEB_SECURITY_CONFIG: {"ThreatModelAgent"},
        RiskTag.INPUT_VALIDATION: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.INJECTION: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.SQL_DATA_ACCESS: {"BehaviorAgent"},
        RiskTag.FILE_PATH_IO: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.SSRF_OUTBOUND: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.CONFIG_SECURITY: {"ThreatModelAgent"},
        RiskTag.DATA_EXPOSURE: {"ThreatModelAgent", "BehaviorAgent"},
        RiskTag.TRANSACTION_ATOMICITY: {"BehaviorAgent"},
        RiskTag.CONCURRENCY_CONSISTENCY: {"BehaviorAgent"},
        RiskTag.IDEMPOTENCY_RETRY: {"BehaviorAgent"},
        RiskTag.CACHE_CONSISTENCY: {"BehaviorAgent"},
        RiskTag.MESSAGE_DELIVERY: {"BehaviorAgent"},
        RiskTag.ERROR_HANDLING: {"BehaviorAgent"},
        RiskTag.NULL_STATE_SAFETY: {"BehaviorAgent"},
        RiskTag.RESOURCE_LIFECYCLE: {"BehaviorAgent", "MaintainabilityAgent"},
        RiskTag.API_CONTRACT: {"BehaviorAgent", "MaintainabilityAgent"},
        RiskTag.PERFORMANCE: {"BehaviorAgent", "MaintainabilityAgent"},
        RiskTag.COMPLEXITY_CONTROL_FLOW: {"MaintainabilityAgent"},
        RiskTag.DUPLICATION_DESIGN: {"MaintainabilityAgent"},
        RiskTag.OBSERVABILITY_TESTABILITY: {"MaintainabilityAgent"},
    }

    assert [spec.tag for spec in catalog.RULE_SPECS] == list(expected_reviewers)
    assert {spec.tag for spec in catalog.RULE_SPECS} == set(RiskTag) - {RiskTag.GENERAL_REVIEW}
    assert {spec.tag: set(spec.reviewers) for spec in catalog.RULE_SPECS} == expected_reviewers


def test_classify_task_deduplicates_scores_and_caps_each_tag(monkeypatch):
    first_signal = RiskSignal(
        tag=RiskTag.AUTHORIZATION,
        score=3,
        source="text:added:test",
        reason="r",
        line=7,
    )
    second_signal = RiskSignal(
        tag=RiskTag.AUTHORIZATION,
        score=3,
        source="text:deleted:test",
        reason="r",
    )
    rule = catalog.RiskRuleSpec(
        rule_id="test",
        tag=RiskTag.AUTHORIZATION,
        reviewers=frozenset({"ThreatModelAgent"}),
        detect=lambda _features: [first_signal, first_signal, second_signal],
    )
    monkeypatch.setattr(catalog, "RULE_SPECS", (rule,))

    profile = catalog.classify_task(ReviewTask(id="t", file="A.java", patch="+x"))

    assert profile.tag_scores == {RiskTag.AUTHORIZATION: 5}
    assert profile.signals == [first_signal, second_signal]


def test_classify_task_falls_back_when_only_path_signal_exists(monkeypatch):
    path_signal = RiskSignal(
        tag=RiskTag.API_CONTRACT,
        score=2,
        source="path:controller",
        reason="path match",
    )
    rule = catalog.RiskRuleSpec(
        rule_id="path_only",
        tag=RiskTag.API_CONTRACT,
        reviewers=frozenset({"BehaviorAgent"}),
        detect=lambda _features: [path_signal],
    )
    monkeypatch.setattr(catalog, "RULE_SPECS", (rule,))

    profile = catalog.classify_task(ReviewTask(id="t", file="A.java", patch="+x"))

    assert profile.tag_scores == {RiskTag.GENERAL_REVIEW: 1}
    assert profile.signals == [
        RiskSignal(
            tag=RiskTag.GENERAL_REVIEW,
            score=1,
            source="fallback:unclassified",
            reason="未命中已有风险规则，执行通用审查",
        )
    ]


def test_triage_tasks_records_rule_failure_and_continues(monkeypatch):
    def broken(_features):
        raise RuntimeError("broken detector")

    good_signal = RiskSignal(
        tag=RiskTag.PERFORMANCE,
        score=2,
        source="text:added:performance",
        reason="performance",
        line=1,
    )
    monkeypatch.setattr(
        catalog,
        "RULE_SPECS",
        (
            catalog.RiskRuleSpec("broken", RiskTag.API_CONTRACT, frozenset(), broken),
            catalog.RiskRuleSpec(
                "good", RiskTag.PERFORMANCE, frozenset(), lambda _features: [good_signal]
            ),
        ),
    )

    result = catalog.triage_tasks([ReviewTask(id="t", file="A.java", patch="+x")])

    assert result.profiles["t"].signals == [good_signal]
    assert result.diagnostics == (
        catalog.RuleDiagnostic(task_id="t", rule_id="broken", detail="broken detector"),
    )
