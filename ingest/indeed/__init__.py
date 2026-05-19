"""Indeed ingestor package."""

from ingest.indeed.playwright_stealth import BanDetectedError, IndeedIngestor

__all__ = ["IndeedIngestor", "BanDetectedError"]
