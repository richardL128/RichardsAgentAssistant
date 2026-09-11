"""Agent-facing wrapper for constrained interview research."""

from __future__ import annotations

from dataclasses import dataclass

from app.connectors.job_research import (
    CompanyResearchClient,
    CompanyResearchFailure,
    CompanyResearchFailureCode,
    CompanyResearchIntent,
    CompanyResearchRequest,
    CompanyResearchResult,
)


@dataclass(frozen=True, slots=True)
class JobInterviewResearchInput:
    posting_url: str
    company: str
    role: str | None
    interview_source_id: str
    application_source_id: str
    intents: tuple[str, ...] = ("company_overview",)


async def research_job_interview(
    payload: JobInterviewResearchInput,
    *,
    researcher: CompanyResearchClient,
) -> CompanyResearchResult:
    """Run validated, read-only company research for a selected interview."""

    intents = _parse_intents(payload.intents)
    if isinstance(intents, CompanyResearchFailure):
        return CompanyResearchResult(
            posting_url=payload.posting_url,
            canonical_posting_url=None,
            company=payload.company,
            role=payload.role,
            sources=(),
            failures=(intents,),
            search_queries=(),
            retrieved_at=researcher.clock(),
            research_fingerprint="",
        )
    return await researcher.research(
        CompanyResearchRequest(
            posting_url=payload.posting_url,
            company=payload.company,
            role=payload.role,
            intents=intents,
            interview_source_id=payload.interview_source_id,
            application_source_id=payload.application_source_id,
        )
    )


def _parse_intents(
    values: tuple[str, ...],
) -> tuple[CompanyResearchIntent, ...] | CompanyResearchFailure:
    intents: list[CompanyResearchIntent] = []
    for value in values:
        try:
            intents.append(CompanyResearchIntent(value))
        except ValueError:
            return CompanyResearchFailure(
                code=CompanyResearchFailureCode.REQUEST_INVALID,
                diagnostic="company research intent is not allowlisted",
            )
    return tuple(dict.fromkeys(intents))


__all__ = [
    "JobInterviewResearchInput",
    "research_job_interview",
]
