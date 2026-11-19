"""FreeTalon content pipeline (slice 1b): draft generation.

Polls the inertiald.com content queue over its token-gated internal
API, claims 'received' entries, drafts the outputs Pat selected
(blog write-up, Instagram caption, YouTube publish package) with the
local Ollama model, and submits the drafts back for Pat's review.
Drafts only - nothing in this package posts or publishes anywhere.
"""

from .generator import ContentGenerator
from .queue_client import ContentQueueClient, ContentQueueError

__all__ = ["ContentGenerator", "ContentQueueClient", "ContentQueueError"]
