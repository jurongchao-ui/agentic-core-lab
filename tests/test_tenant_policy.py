from __future__ import annotations

import json

import pytest

from evalops.governance.tenant_policy import TenantPolicyStore, main


def test_tenant_policy_allows_enabled_tenant_scope(tmp_path, capsys) -> None:
    path = tmp_path / "tenant-policy.json"
    path.write_text(json.dumps(sample_policy(), ensure_ascii=False), encoding="utf-8")

    store = TenantPolicyStore.from_file(path)
    decision = store.evaluate("tenant_a", "eval.viewer")
    code = main(["show", "--path", str(path)])
    output = json.loads(capsys.readouterr().out)

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.allowed_scopes == {"eval.viewer", "eval.reviewer"}
    assert code == 0
    assert output["type"] == "agentic_tenant_policy"
    assert output["tenants"]["tenant_a"]["enabled"] is True


def test_tenant_policy_denies_unknown_disabled_and_disallowed_scope(tmp_path) -> None:
    path = tmp_path / "tenant-policy.json"
    path.write_text(json.dumps(sample_policy(), ensure_ascii=False), encoding="utf-8")
    store = TenantPolicyStore.from_file(path)

    unknown = store.evaluate("missing", "eval.viewer")
    disabled = store.evaluate("tenant_disabled", "eval.viewer")
    disallowed = store.evaluate("tenant_viewer", "eval.reviewer")

    assert unknown.allowed is False
    assert unknown.reason == "unknown_tenant"
    assert disabled.allowed is False
    assert disabled.reason == "tenant_disabled"
    assert disallowed.allowed is False
    assert disallowed.reason == "scope_not_allowed_for_tenant"


def test_tenant_policy_rejects_invalid_file(tmp_path) -> None:
    path = tmp_path / "tenant-policy.json"
    path.write_text(json.dumps({"tenants": {"tenant_a": {"enabled": "yes"}}}), encoding="utf-8")

    with pytest.raises(ValueError, match="enabled"):
        TenantPolicyStore.from_file(path)


def sample_policy() -> dict:
    return {
        "schemaVersion": 1,
        "tenants": {
            "tenant_a": {
                "enabled": True,
                "allowedScopes": ["eval.viewer", "eval.reviewer"],
            },
            "tenant_viewer": {
                "enabled": True,
                "allowedScopes": ["eval.viewer"],
            },
            "tenant_disabled": {
                "enabled": False,
                "allowedScopes": ["eval.viewer", "eval.reviewer"],
            },
        },
    }
