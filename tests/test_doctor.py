"""doctor.check_env(): the ANTHROPIC_API_KEY contamination check."""

from __future__ import annotations

import pytest

from anycli.doctor import check_env


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return monkeypatch


def test_api_key_present_yields_warning(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")

    findings = check_env()

    warnings = [f for f in findings if f.level == "warning"]
    assert len(warnings) == 1
    assert "ANTHROPIC_API_KEY" in warnings[0].message


def test_api_key_absent_yields_no_warning(clean_env: pytest.MonkeyPatch) -> None:
    findings = check_env()

    assert findings, "check_env should always report at least one finding"
    assert all(f.level != "warning" for f in findings)


def test_empty_api_key_is_not_a_warning(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ANTHROPIC_API_KEY", "")

    findings = check_env()

    assert all(f.level != "warning" for f in findings)
