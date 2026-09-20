from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_nightly_configuration_is_typed_bounded_and_redacted() -> None:
    settings = Settings(
        _env_file=None,
        discord_academic_authorized_user_ids=[333333333333333333],
        discord_academic_proactive_user_id=333333333333333333,
        academic_end_of_day_catchup_grace_minutes=45,
    )

    diagnostics = settings.safe_diagnostics()

    assert diagnostics["discord_academic_proactive_user_configured"] is True
    assert diagnostics["discord_academic_proactive_user_authorized"] is True
    assert diagnostics["academic_end_of_day_catchup_grace_minutes"] == 45
    assert "333333333333333333" not in str(diagnostics)


def test_blank_proactive_owner_is_unconfigured_and_fails_closed() -> None:
    settings = Settings(
        _env_file=None,
        discord_academic_authorized_user_ids=[333333333333333333],
        discord_academic_proactive_user_id="",
    )

    diagnostics = settings.safe_diagnostics()

    assert settings.discord_academic_proactive_user_id is None
    assert diagnostics["discord_academic_proactive_user_configured"] is False
    assert diagnostics["discord_academic_proactive_user_authorized"] is False


@pytest.mark.parametrize("grace", [0, 181])
def test_nightly_catchup_grace_is_bounded(grace: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            academic_end_of_day_catchup_grace_minutes=grace,
        )
