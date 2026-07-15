"""Runtime state shared by the API routers: the loaded semantic models and the
persistence store. Kept in one place so the app factory can initialize it and
tests can swap it out.
"""
from __future__ import annotations

from typing import Optional

from . import config, semantic
from .authstore import AuthStore
from .conversationstore import ConversationStore
from .store import VisualStore


class Registry:
    def __init__(self) -> None:
        self.models: dict[str, semantic.Model] = {}
        self.dimension_bundles: dict[str, semantic.DimensionBundle] = {}
        self.store: Optional[VisualStore] = None
        self.auth_store: Optional[AuthStore] = None
        self.conversation_store: Optional[ConversationStore] = None

    def init(self) -> None:
        self.reload_all()
        self.store = VisualStore(config.DB_PATH)
        self.auth_store = AuthStore(
            config.DB_PATH,
            idle_days=config.SESSION_IDLE_DAYS,
            max_days=config.SESSION_MAX_DAYS,
        )
        self.conversation_store = ConversationStore(config.DB_PATH)

    def reload_all(self) -> None:
        """Reload dimension bundles, then models, then resolve each model's
        imports against the freshly-loaded bundles — bundles must load first
        since models validate their imports against them."""
        self.dimension_bundles = semantic.load_dimension_bundles(config.DIMENSIONS_DIR)
        self.models = semantic.load_models(config.MODELS_DIR)
        for model in self.models.values():
            semantic.resolve_imports(model, self.dimension_bundles)


registry = Registry()
