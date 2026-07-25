"""Runtime state shared by the API routers: the loaded semantic models and the
persistence store. Kept in one place so the app factory can initialize it and
tests can swap it out.
"""
from __future__ import annotations

from typing import Optional

from . import config, pipelines as pipelines_mod, semantic
from .authstore import AuthStore
from .conversationstore import ConversationStore
from .localmodelstore import LocalModelStore
from .memorystore import MemoryStore
from .pipelinestore import PipelineStore
from .sandboxstore import SandboxStore
from .store import VisualStore


class Registry:
    def __init__(self) -> None:
        self.models: dict[str, semantic.Model] = {}
        self.dimension_bundles: dict[str, semantic.DimensionBundle] = {}
        self.pipelines: dict[str, pipelines_mod.Pipeline] = {}
        self.layers: dict[str, pipelines_mod.Layer] = {}
        self.store: Optional[VisualStore] = None
        self.auth_store: Optional[AuthStore] = None
        self.conversation_store: Optional[ConversationStore] = None
        self.memory_store: Optional[MemoryStore] = None
        self.pipeline_store: Optional[PipelineStore] = None
        self.sandbox_store: Optional[SandboxStore] = None
        self.local_model_store: Optional[LocalModelStore] = None

    def init(self) -> None:
        self.store = VisualStore(config.DB_PATH)
        self.auth_store = AuthStore(
            config.DB_PATH,
            idle_days=config.SESSION_IDLE_DAYS,
            max_days=config.SESSION_MAX_DAYS,
        )
        self.conversation_store = ConversationStore(config.DB_PATH)
        self.memory_store = MemoryStore(config.DB_PATH)
        self.pipeline_store = PipelineStore(config.DB_PATH)
        self.sandbox_store = SandboxStore(config.DB_PATH)
        self.local_model_store = LocalModelStore(config.DB_PATH)
        self.reload_all()

    def reload_all(self) -> None:
        """Reload dimension bundles, then models, then resolve each model's
        imports against the freshly-loaded bundles — bundles must load first
        since models validate their imports against them. Layers then
        pipelines follow the same shape: layers must load first since
        pipelines validate their layer references against them. Pipelines
        load after models since target->model matching (lineage) needs
        models loaded."""
        self.dimension_bundles = semantic.load_dimension_bundles(config.DIMENSIONS_DIR)
        self.models = semantic.load_models(config.MODELS_DIR)
        if self.local_model_store is not None:
            for row in self.local_model_store.list():
                try:
                    local = semantic.parse_model_text(row["yaml"])
                except semantic.ModelError:
                    continue  # a hand-corrupted row shouldn't sink the whole reload
                if local.name in self.models:
                    continue  # a name the built-in catalog (or an earlier local row) already owns wins
                local.locked = False
                local.origin = None
                self.models[local.name] = local
        for model in self.models.values():
            semantic.resolve_imports(model, self.dimension_bundles)
        # facts resolve in a second pass: a multi-fact model conforms on its
        # facts' *imported* dimensions too, so every model's imports must
        # already be merged in before any of them is read as a fact
        for model in self.models.values():
            semantic.resolve_facts(model, self.models)
        self.layers = pipelines_mod.load_layers(config.PIPELINES_DIR)
        self.pipelines = pipelines_mod.load_pipelines(config.PIPELINES_DIR, self.layers)

    def read_model_text(self, model: semantic.Model) -> str:
        if model.locked:
            return model.origin.read_text()
        row = self.local_model_store.get(model.name)
        return row["yaml"] if row else ""

    def write_model_text(self, model: semantic.Model, text: str) -> None:
        """Persist a model's yaml text back to wherever it came from — its
        file if locked (built-in), its LocalModelStore row otherwise. `locked`
        only blocks *structural* changes (create/rename/delete a model — see
        app/api/models.py's _forbid_if_locked); a locked model's measures and
        its pipeline-lineage section (app/pipeline_jobs.py,
        app/api/pipelines.py) are still written in place, same as before
        the local/built-in split existed."""
        if model.locked:
            model.origin.write_text(text)
        else:
            self.local_model_store.update(model.name, text)


registry = Registry()
