"""LinkedIn ingestor package."""

from ingest.linkedin.playwright_session import (
    BanDetectedError,
    LinkedInIngestor,
    SessionExpiredError,
    interactive_login,
)

__all__ = [
    "LinkedInIngestor",
    "BanDetectedError",
    "SessionExpiredError",
    "interactive_login",
]
