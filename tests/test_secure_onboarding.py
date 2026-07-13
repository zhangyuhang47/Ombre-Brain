"""安全部署模式、首次向导路由和独立页面的回归测试。"""

import json
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest
import yaml

from deployment_profile import (
    build_profile_patch,
    effective_configuration_report,
    validate_profile_patch,
)
import web.onboarding as onboarding


class FakeMCP:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}

    def custom_route(self, path: str, methods: list[str]) -> Callable[[Any], Any]:
        def decorator(handler: Any) -> Any:
            for method in methods:
                self.routes[(method, path)] = handler
            return handler
        return decorator


class JsonRequest:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = body or {}
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _payload(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def test_profile_defaults_make_public_safe_and_local_simple() -> None:
    local = build_profile_patch("local")
    public = build_profile_patch("public_secure", {"public_url": "https://ob.example"})

    assert local["transport"] == "streamable-http"
    assert local["mcp_require_auth"] is False
    assert public["mcp_require_auth"] is True
    assert validate_profile_patch(local) == []
    assert validate_profile_patch(public) == []


def test_public_profile_rejects_non_https_and_cannot_disable_oauth() -> None:
    patch = build_profile_patch("public_secure", {"public_url": "http://ob.example"})
    patch["mcp_require_auth"] = False

    issues = validate_profile_patch(patch)

    assert "公网安全模式不能关闭 OAuth" in issues
    assert "公网地址必须使用 HTTPS" in issues


def test_effective_report_exposes_environment_override_without_hiding_saved_value() -> None:
    report = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": False, "buckets_dir": "/data"},
        {"transport": "streamable-http", "mcp_require_auth": True, "deployment": {"profile": "public_secure", "onboarding_completed": True}},
        environment={"OMBRE_MCP_REQUIRE_AUTH": "false"},
        config_path="/data/config.yaml",
        persistence={"persistent": True, "mode": "volume"},
    )

    assert report["saved"]["mcp_require_auth"] is True
    assert report["effective"]["mcp_require_auth"] is False
    assert report["restart_required"] is True
    assert report["overrides"] == [{"env": "OMBRE_MCP_REQUIRE_AUTH", "field": "mcp_require_auth", "value": "false"}]
    assert report["environment_sources"] == report["overrides"]


def test_effective_report_flags_manual_auth_configuration_without_onboarding() -> None:
    """用户没走 /onboarding，但已经在「MCP 连接」面板手动保存过鉴权——
    profile 仍是 unconfigured，但 manual_auth_configured 要能让诊断识别出
    这是一次主动选择，而不是从没配置过。"""
    report = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": True},
        {"mcp_require_auth": True},
    )

    assert report["profile"] == "unconfigured"
    assert report["manual_auth_configured"] is True

    report_mode_only = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": True, "mcp_auth_mode": "token"},
        {"mcp_auth_mode": "token"},
    )

    assert report_mode_only["manual_auth_configured"] is True


def test_effective_report_manual_auth_configured_is_false_for_fresh_install() -> None:
    report = effective_configuration_report(
        {"transport": "stdio", "mcp_require_auth": True},
        {},
    )

    assert report["profile"] == "unconfigured"
    assert report["manual_auth_configured"] is False


def test_effective_report_does_not_warn_for_matching_platform_defaults() -> None:
    report = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": True, "buckets_dir": "/app/buckets"},
        {"transport": "streamable-http", "mcp_require_auth": True},
        environment={
            "OMBRE_TRANSPORT": "streamable-http",
            "OMBRE_CONFIG_PATH": "/app/buckets/config.yaml",
        },
    )

    assert report["overrides"] == []
    assert len(report["environment_sources"]) == 2
    assert report["restart_required"] is False


@pytest.mark.asyncio
async def test_onboarding_apply_preserves_unrelated_config_and_requires_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"merge_threshold": 82, "embedding": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(onboarding, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(onboarding.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(onboarding.sh, "config", {"transport": "streamable-http", "mcp_require_auth": True, "buckets_dir": str(tmp_path)})
    monkeypatch.setattr(onboarding.sh, "data_dir_persistence", lambda path: {"persistent": True, "mode": "local", "note": "ok"})
    mcp = FakeMCP()
    onboarding.register(mcp)

    response = await mcp.routes[("POST", "/api/onboarding/apply")](JsonRequest({"profile": "public_secure", "options": {"public_url": "https://ob.example"}, "confirm": True}))
    data = _payload(response)
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert data["ok"] is True
    assert data["restart_required"] is True
    assert persisted["merge_threshold"] == 82
    assert persisted["embedding"] == {"enabled": True}
    assert persisted["mcp_require_auth"] is True
    assert persisted["deployment"]["profile"] == "public_secure"


def test_onboarding_page_has_file_contract_and_safe_json_parser() -> None:
    text = Path("frontend/onboarding.html").read_text(encoding="utf-8")

    assert "onboarding.html — Ombre Brain 首次部署向导" in text
    assert "本机模式" not in text  # 模式文案来自后端单一目录，页面不维护第二份。
    assert "readJsonSafe" in text
    assert "/api/onboarding/preflight" in text
    assert "/api/onboarding/apply" in text

    dashboard = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    assert 'href="/onboarding"' in dashboard
    assert "打开安全部署向导" in dashboard
