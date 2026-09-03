"""Loading and validating the checked-in Phase 1 evaluation fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    AcademicExtraction,
    CodeFindingTriage,
    FinanceFactInference,
    StructuredSmokeOutput,
)

MODEL_REGISTRY: dict[str, type[BaseModel]] = {
    "AcademicExtraction": AcademicExtraction,
    "CodeFindingTriage": CodeFindingTriage,
    "FinanceFactInference": FinanceFactInference,
    "StructuredSmokeOutput": StructuredSmokeOutput,
}

JsonScalar = str | int | float | bool | None


class ExpectedAssertions(BaseModel):
    """Deterministic semantic checks applied after schema validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    equals: dict[str, JsonScalar] = Field(default_factory=lambda: dict[str, JsonScalar]())
    contains: dict[str, list[str]] = Field(default_factory=lambda: dict[str, list[str]]())


class EvaluationFixture(BaseModel):
    """One immutable benchmark case and its synthetic expected response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+-v[0-9]+$")
    domain: str = Field(pattern=r"^[a-z_]+$")
    prompt: str = Field(min_length=1, max_length=8_000)
    response_model: str
    expected_status: str = Field(pattern=r"^(valid|invalid_output|failed)$")
    expected_output: Any = None
    assertions: ExpectedAssertions = Field(default_factory=ExpectedAssertions)

    @field_validator("response_model")
    @classmethod
    def known_response_model(cls, value: str) -> str:
        if value not in MODEL_REGISTRY:
            raise ValueError(f"unknown evaluation response model: {value}")
        return value

    @model_validator(mode="after")
    def valid_expected_output_matches_schema(self) -> EvaluationFixture:
        if self.expected_status == "valid":
            MODEL_REGISTRY[self.response_model].model_validate(self.expected_output)
        return self

    @property
    def model_type(self) -> type[BaseModel]:
        return MODEL_REGISTRY[self.response_model]


def load_fixtures(directory: Path) -> list[EvaluationFixture]:
    """Load fixtures in lexical order, rejecting duplicate IDs."""

    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"no evaluation fixtures found in {directory}")
    fixtures: list[EvaluationFixture] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(encoding="utf-8") as fixture_file:
            fixture = EvaluationFixture.model_validate(json.load(fixture_file))
        if fixture.fixture_id in seen:
            raise ValueError(f"duplicate evaluation fixture: {fixture.fixture_id}")
        seen.add(fixture.fixture_id)
        fixtures.append(fixture)
    return fixtures
