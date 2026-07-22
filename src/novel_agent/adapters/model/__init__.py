"""Model provider adapters."""

from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.model.http_inference import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalInferenceError,
    RetrievalModelRoute,
)
from novel_agent.adapters.model.openai_chat import (
    OpenAIChatEndpointError,
    OpenAICompatibleChatEndpoint,
)
from novel_agent.adapters.model.scripted import ScriptedModelEndpoint

__all__ = [
    "FakeModelEndpoint",
    "HttpEmbeddingProvider",
    "HttpPassageReranker",
    "OpenAIChatEndpointError",
    "OpenAICompatibleChatEndpoint",
    "RetrievalInferenceError",
    "RetrievalModelRoute",
    "ScriptedModelEndpoint",
]
