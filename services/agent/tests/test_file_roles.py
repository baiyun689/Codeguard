"""文件路径角色 → RiskTag 单一注册表(roles.py)的工程正确性测试。

统一此前 path.py 的 _ROLE_TAGS(弱路径信号)与 selector 的
_FILE_ROLE_TOPICS(知识 file-role 评分)两套映射;角色是上下文不是发现,
只做加权/评分。确定性纯数据 + 匹配函数,适合 pytest 死磕。
"""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.risk.rules.roles import (
    FILE_ROLE_SPECS,
    matching_roles,
    path_tags,
)


def test_注册表_每个角色都有匹配方式与标签():
    assert FILE_ROLE_SPECS
    seen_roles: set[str] = set()
    for spec in FILE_ROLE_SPECS:
        assert spec.role not in seen_roles          # 角色唯一
        seen_roles.add(spec.role)
        assert spec.markers or spec.contains        # 至少一种匹配方式
        assert spec.tags                            # 至少一个相关标签


def test_controller_后缀与路径片段都能命中():
    roles = {spec.role for spec in matching_roles("src/controller/UserController.java")}
    assert "controller" in roles
    roles = {spec.role for spec in matching_roles("src/main/OrderController.java")}
    assert "controller" in roles


def test_repository_service_config_命中():
    path = "src/main/java/OrderRepository.java"
    assert {spec.role for spec in matching_roles(path)} == {"repository"}
    assert {spec.role for spec in matching_roles("OrderService.java")} == {"service"}
    assert {spec.role for spec in matching_roles("src/resources/application.yml")} == {"config"}


def test_security_auth_子串启发_保留legacy语义():
    roles = {spec.role for spec in matching_roles("src/SecurityConfig.java")}
    assert "security" in roles
    roles = {spec.role for spec in matching_roles("src/OAuth2Client.java")}
    assert "security" in roles


def test_cache_event_子串启发_保留legacy语义():
    assert {spec.role for spec in matching_roles("src/CacheManager.java")} == {"cache"}
    assert {spec.role for spec in matching_roles("src/EventHandler.java")} == {"event"}


def test_path_tags_并集去重_保持注册表顺序():
    # service 角色标签包含 TRANSACTION_ATOMICITY/CONCURRENCY_CONSISTENCY 等。
    tags = path_tags("src/OrderService.java")
    assert RiskTag.TRANSACTION_ATOMICITY in tags
    assert RiskTag.ERROR_HANDLING in tags
    assert RiskTag.NULL_STATE_SAFETY in tags
    assert len(tags) == len(set(tags))              # 去重


def test_无命中返回空():
    assert matching_roles("src/main/App.java") == ()
    assert path_tags("src/main/App.java") == ()
    assert matching_roles("") == ()
