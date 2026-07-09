"""Runtime state shared by the API routers: the loaded semantic models and the
persistence store. Kept in one place so the app factory can initialize it and
tests can swap it out.
"""
from __future__ import annotations

from typing import Optional

from . import config, semantic
from .store import VisualStore


class Registry:
    def __init__(self) -> None:
        self.models: dict[str, semantic.Model] = {}
        self.store: Optional[VisualStore] = None

    def init(self) -> None:
        self.reload_models()
        self.store = VisualStore(config.DB_PATH)

    def reload_models(self) -> None:
        self.models = semantic.load_models(config.MODELS_DIR)


registry = Registry()
