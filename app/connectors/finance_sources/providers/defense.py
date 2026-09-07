"""Defense.gov public RSS provider parser."""

from __future__ import annotations

from datetime import datetime

from app.agents.finance.contracts import SourceClassification, SourceDocument, SourceQuery

from .feeds import FeedParseOptions, parse_feed_documents
from .registries import DEFENSE_GOV_RSS, DEFENSE_SOURCE_VERSION

DEFENSE_RELEASES_ENDPOINT_ID = "defense-gov-releases-rss"
DEFENSE_RELEASES_URL = (
    "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=50"
)


def parse_defense_gov_rss(
    payload: bytes | str,
    query: SourceQuery,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return parse_feed_documents(
        payload,
        query,
        retrieved_at,
        FeedParseOptions(
            source_id=DEFENSE_GOV_RSS,
            source_version=DEFENSE_SOURCE_VERSION,
            classification=SourceClassification.PRIMARY,
            endpoint_url=DEFENSE_RELEASES_URL,
            endpoint_id=DEFENSE_RELEASES_ENDPOINT_ID,
            themes=("defense",),
            excerpt_allowed=True,
            excerpt_max_chars=300,
            require_canonical_host="www.war.gov",
        ),
    )
