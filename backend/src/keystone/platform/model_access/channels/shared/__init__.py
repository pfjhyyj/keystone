"""Protocol instances reused by several channels, each with explicit operation limits."""

from dataclasses import replace

from .chat import ChatCompletions
from .embeddings import Embeddings
from .openai_images import OpenAIImages

CHAT = ChatCompletions(frozenset({"text.generate", "text.structured", "image.understand"}))
RELAY_CHAT = replace(CHAT, pricing_meter="relay_chat.v3")
EMBEDDINGS = Embeddings()
RELAY_IMAGES = OpenAIImages()
