from __future__ import annotations

import pytest

from agentic_core.auth_tokens import create_signed_token, main, verify_signed_token


def test_signed_token_round_trip() -> None:
    token = create_signed_token(
        secret="secret",
        subject="user_1",
        tenant="tenant_a",
        scopes={"eval.viewer", "eval.reviewer"},
        ttl_seconds=60,
        now=100,
    )

    claims = verify_signed_token(token, "secret", now=120)

    assert claims.subject == "user_1"
    assert claims.tenant == "tenant_a"
    assert claims.scopes == {"eval.viewer", "eval.reviewer"}
    assert claims.issued_at == 100
    assert claims.expires_at == 160


def test_signed_token_rejects_tampering_and_wrong_secret() -> None:
    token = create_signed_token(
        secret="secret",
        subject="user_1",
        scopes={"eval.viewer"},
        ttl_seconds=60,
        now=100,
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(ValueError, match="signature"):
        verify_signed_token(tampered, "secret", now=120)

    with pytest.raises(ValueError, match="signature"):
        verify_signed_token(token, "wrong", now=120)


def test_signed_token_rejects_expired_token() -> None:
    token = create_signed_token(
        secret="secret",
        subject="user_1",
        scopes={"eval.viewer"},
        ttl_seconds=60,
        now=100,
    )

    with pytest.raises(ValueError, match="expired"):
        verify_signed_token(token, "secret", now=160)


def test_auth_tokens_cli_create(capsys) -> None:
    code = main(
        [
            "create",
            "--secret",
            "secret",
            "--subject",
            "user_1",
            "--tenant",
            "tenant_a",
            "--scopes",
            "eval.viewer,eval.reviewer",
            "--ttl",
            "60",
        ]
    )
    token = capsys.readouterr().out.strip()
    claims = verify_signed_token(token, "secret")

    assert code == 0
    assert claims.subject == "user_1"
    assert claims.tenant == "tenant_a"
    assert claims.scopes == {"eval.viewer", "eval.reviewer"}
