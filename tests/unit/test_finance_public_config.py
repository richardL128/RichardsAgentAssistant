from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.finance.sources import (
    FINANCE_SOURCE_ALLOWLIST_VERSION,
    FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
)
from app.core.config import Settings


def test_public_finance_baseline_requires_no_paid_vendor_keys() -> None:
    settings = Settings(
        _env_file=None,
        dvids_api_key="",
        eia_api_key="",
        alpha_vantage_api_key="",
        benzinga_api_token="",
        fmp_api_key="",
    )

    assert settings.finance_eia_mode == "bulk"
    assert settings.finance_source_allowlist_version == "finance-sources-2026.09-v2"
    assert FINANCE_SOURCE_ALLOWLIST_VERSION == FINANCE_SOURCE_ALLOWLIST_VERSION_V2
    assert settings.eia_api_key is None
    assert settings.sec_user_agent == "LifeAgent/0.1 contact@example.com"
    assert settings.safe_diagnostics()["finance_source_credentials_configured"] == 0


def test_eia_api_mode_fails_closed_without_key() -> None:
    with pytest.raises(ValidationError, match="FINANCE_EIA_MODE=api requires EIA_API_KEY"):
        Settings(_env_file=None, finance_eia_mode="api", eia_api_key="")


def test_sec_user_agent_requires_application_and_contact() -> None:
    with pytest.raises(ValidationError, match="identify the application and a contact"):
        Settings(_env_file=None, sec_user_agent="python-httpx")
