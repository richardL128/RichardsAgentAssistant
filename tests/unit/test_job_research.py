from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.agents.job_interviews.research import (
    JobInterviewResearchInput,
    research_job_interview,
)
from app.connectors.job_research import (
    CompanyResearchClient,
    CompanyResearchFailureCode,
    CompanyResearchIntent,
    CompanyResearchSearchQuery,
    CompanyResearchSearchResult,
    CompanyResearchSearchResultItem,
    CompanyResearchSourceClass,
    UnconfiguredCompanyResearchSearchProvider,
    canonicalize_public_https_url,
    extract_visible_text,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class FakeResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]] | None = None) -> None:
        self.mapping = mapping or {}

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        return self.mapping.get(hostname, ("93.184.216.34",))


class FakeSearchProvider:
    configured = True

    def __init__(self, items: tuple[CompanyResearchSearchResultItem, ...]) -> None:
        self.items = items
        self.queries: list[CompanyResearchSearchQuery] = []

    async def search(self, query: CompanyResearchSearchQuery) -> CompanyResearchSearchResult:
        self.queries.append(query)
        return CompanyResearchSearchResult(items=self.items)


def _html_response(body: str, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=body.encode(), headers={"content-type": "text/html"})


@pytest.mark.asyncio
async def test_research_fetches_posting_visible_text_with_provenance() -> None:
    html = """
    <html>
      <head><title>Senior Platform Engineer</title><style>.x{}</style></head>
      <body>
        <nav hidden>Navigation should disappear</nav>
        <h1>Senior Platform Engineer</h1>
        <script>stealToken()</script>
        <form>private form</form>
        <p>Build reliable APIs for hiring teams.</p>
        <p style="display: none">invisible tracking copy</p>
      </body>
    </html>
    """

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _html_response(html))
    ) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            search_provider=UnconfiguredCompanyResearchSearchProvider(),
            resolver=FakeResolver(),
            clock=lambda: NOW,
        )
        result = await researcher.research(
            request=researcher_request(
                intents=(),
                posting_url="https://Jobs.Example/post?utm_source=x",
            )
        )

    assert result.ok is True
    assert result.canonical_posting_url == "https://jobs.example/post"
    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.source_class == CompanyResearchSourceClass.POSTING
    assert source.title == "Senior Platform Engineer"
    assert "Build reliable APIs" in source.excerpt
    assert "stealToken" not in source.excerpt
    assert "private form" not in source.excerpt
    assert "invisible tracking" not in source.excerpt
    assert source.content_fingerprint
    assert result.research_fingerprint


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.example/post",
        "https://user:secret@jobs.example/post",
        "https://127.0.0.1/post",
        "https://[::1]/post",
        "https://localhost/post",
        "https://metadata.google.internal/post",
    ],
)
def test_canonical_url_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(ValueError, match=r"company research URL|HTTPS"):
        canonicalize_public_https_url(url)


def test_extract_visible_text_removes_hidden_and_active_content() -> None:
    title, text = extract_visible_text(
        """
        <title>Role</title>
        <main aria-hidden="false">Visible role details</main>
        <script>ignore()</script>
        <style>body { color: red }</style>
        <div aria-hidden="true">hidden</div>
        <template>template text</template>
        """,
        content_type="text/html",
    )

    assert title == "Role"
    assert text == "Visible role details"


@pytest.mark.asyncio
async def test_research_revalidates_redirect_targets_against_dns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "jobs.example":
            return httpx.Response(302, headers={"location": "https://internal.example/post"})
        return _html_response("<p>should not fetch private DNS target</p>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            resolver=FakeResolver(
                {
                    "jobs.example": ("93.184.216.34",),
                    "internal.example": ("10.0.0.4",),
                }
            ),
            clock=lambda: NOW,
        )
        result = await researcher.research(researcher_request(intents=()))

    assert result.sources == ()
    assert result.failures[0].code == CompanyResearchFailureCode.URL_UNSAFE


@pytest.mark.asyncio
async def test_research_rejects_non_textual_and_oversized_content() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"x" * 10,
                headers={"content-type": "application/zip"},
            )
        )
    ) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            resolver=FakeResolver(),
            clock=lambda: NOW,
        )
        non_text = await researcher.research(researcher_request(intents=()))

    assert non_text.failures[0].code == CompanyResearchFailureCode.CONTENT_TYPE_REJECTED

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _html_response("<p>" + ("x" * 50) + "</p>"))
    ) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            resolver=FakeResolver(),
            max_response_bytes=20,
            clock=lambda: NOW,
        )
        oversized = await researcher.research(researcher_request(intents=()))

    assert oversized.failures[0].code == CompanyResearchFailureCode.PAYLOAD_TOO_LARGE


@pytest.mark.asyncio
async def test_search_provider_receives_host_built_query_and_fetches_results() -> None:
    provider = FakeSearchProvider(
        (
            CompanyResearchSearchResultItem(
                title="Example engineering",
                url="https://engineering.example/blog",
                source_class=CompanyResearchSourceClass.OFFICIAL,
            ),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "jobs.example":
            return _html_response("<title>Posting</title><p>Python platform role.</p>")
        if request.url.host == "engineering.example":
            return _html_response(
                "<title>Engineering</title><p>Official engineering practices.</p>"
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            search_provider=provider,
            resolver=FakeResolver(),
            clock=lambda: NOW,
        )
        result = await researcher.research(
            researcher_request(
                intents=(CompanyResearchIntent.ENGINEERING_PRACTICES,),
                company="Acme",
                role="Backend Engineer",
            )
        )

    assert provider.queries == [
        CompanyResearchSearchQuery(
            query="Acme Backend Engineer engineering practices",
            company="Acme",
            role="Backend Engineer",
            intent=CompanyResearchIntent.ENGINEERING_PRACTICES,
            max_results=3,
        )
    ]
    assert [source.source_class for source in result.sources] == [
        CompanyResearchSourceClass.POSTING,
        CompanyResearchSourceClass.OFFICIAL,
    ]
    assert "Official engineering practices" in result.sources[1].excerpt


@pytest.mark.asyncio
async def test_unconfigured_provider_is_explicit_but_preserves_posting_snapshot() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _html_response("<p>Posting body.</p>"))
    ) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            search_provider=UnconfiguredCompanyResearchSearchProvider(),
            resolver=FakeResolver(),
            clock=lambda: NOW,
        )
        result = await researcher.research(
            researcher_request(intents=(CompanyResearchIntent.COMPANY_OVERVIEW,))
        )

    assert result.ok is True
    assert len(result.failures) == 1
    assert result.failures[0].code == CompanyResearchFailureCode.PROVIDER_UNCONFIGURED
    assert result.search_queries == ()


@pytest.mark.asyncio
async def test_agent_wrapper_rejects_non_allowlisted_intent() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _html_response("<p>unused</p>"))
    ) as http_client:
        researcher = CompanyResearchClient(
            client=http_client,
            resolver=FakeResolver(),
            clock=lambda: NOW,
        )
        result = await research_job_interview(
            JobInterviewResearchInput(
                posting_url="https://jobs.example/post",
                company="Acme",
                role="Engineer",
                interview_source_id="interview-1",
                application_source_id="row-1",
                intents=("arbitrary_raw_query",),
            ),
            researcher=researcher,
        )

    assert result.ok is False
    assert result.failures[0].code == CompanyResearchFailureCode.REQUEST_INVALID


def researcher_request(
    *,
    intents: tuple[CompanyResearchIntent, ...],
    posting_url: str = "https://jobs.example/post",
    company: str = "Acme",
    role: str | None = "Backend Engineer",
):
    from app.connectors.job_research import CompanyResearchRequest

    return CompanyResearchRequest(
        posting_url=posting_url,
        company=company,
        role=role,
        intents=intents,
        interview_source_id="interview-1",
        application_source_id="row-1",
    )
