"""Runtime state shared by the API routers: the loaded semantic models and the
persistence store. Kept in one place so the app factory can initialize it and
tests can swap it out.
"""
from __future__ import annotations

from typing import Optional

from . import agents as agents_mod
from . import config, pipelines as pipelines_mod, semantic
from .authstore import AuthStore
from .conversationstore import ConversationStore
from .localbundlestore import LocalBundleStore
from .localmodelstore import LocalModelStore
from .localpipelinestore import LocalPipelineStore
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
        self.agents: dict[str, agents_mod.Agent] = {}
        self.store: Optional[VisualStore] = None
        self.auth_store: Optional[AuthStore] = None
        self.conversation_store: Optional[ConversationStore] = None
        self.memory_store: Optional[MemoryStore] = None
        self.pipeline_store: Optional[PipelineStore] = None
        self.sandbox_store: Optional[SandboxStore] = None
        self.local_model_store: Optional[LocalModelStore] = None
        self.local_bundle_store: Optional[LocalBundleStore] = None
        self.local_pipeline_store: Optional[LocalPipelineStore] = None

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
        self.local_bundle_store = LocalBundleStore(config.DB_PATH)
        self.local_pipeline_store = LocalPipelineStore(config.DB_PATH)
        # One-time side effect: importing app/skills_analytics.py registers
        # its skills (ask_question, list_models) into app/skills.py's
        # registry. Must happen before the first reload_all() below, since
        # load_agents() validates agents/*.yaml's skill references against
        # that registry. A local import (not a module-level one) — skills
        # are code, not reloadable state, so this only ever needs to run
        # once per process, unlike reload_all()'s YAML re-reads.
        from . import skills_analytics  # noqa: F401
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
        if self.local_bundle_store is not None:
            for row in self.local_bundle_store.list():
                try:
                    local = semantic.parse_bundle_text(row["yaml"])
                except semantic.ModelError:
                    continue  # a hand-corrupted row shouldn't sink the whole reload
                if local.name in self.dimension_bundles:
                    continue  # a name the built-in catalog (or an earlier local row) already owns wins
                local.locked = False
                local.origin = None
                self.dimension_bundles[local.name] = local
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
        for name, model in list(self.models.items()):
            try:
                semantic.resolve_imports(model, self.dimension_bundles)
            except semantic.ModelError:
                # a built-in model failing to resolve is a real codebase bug —
                # fail loudly. A *local* model can go stale on its own (e.g. it
                # imports a bundle that a local/built-in change since removed)
                # without anyone touching it, so drop just that one instead of
                # taking the whole app down; it'll keep failing until whoever
                # owns it fixes or deletes it.
                if model.locked:
                    raise
                del self.models[name]
        # facts resolve in a second pass: a multi-fact model conforms on its
        # facts' *imported* dimensions too, so every model's imports must
        # already be merged in before any of them is read as a fact
        for name, model in list(self.models.items()):
            try:
                semantic.resolve_facts(model, self.models)
            except semantic.ModelError:
                if model.locked:
                    raise
                del self.models[name]
        # layers: the DB row (once anyone has PUT /lineage/layers) always
        # wins over the built-in file — a PUT there always replaces the
        # whole ordered list, so there's no per-layer merge to do, unlike
        # pipelines/models/bundles below.
        db_layers_yaml = self.local_pipeline_store.get_layers_yaml() if self.local_pipeline_store else None
        self.layers = (
            pipelines_mod.parse_layers_text(db_layers_yaml) if db_layers_yaml is not None
            else pipelines_mod.load_layers(config.PIPELINES_DIR)
        )
        self.pipelines = pipelines_mod.load_pipelines(config.PIPELINES_DIR, self.layers)
        if self.local_pipeline_store is not None:
            for row in self.local_pipeline_store.list():
                try:
                    local = pipelines_mod.parse_pipeline_text(row["yaml"])
                except pipelines_mod.PipelineError:
                    continue  # a hand-corrupted row shouldn't sink the whole reload
                if local.name in self.pipelines:
                    continue  # a name the built-in catalog (or an earlier local row) already owns wins
                local.locked = False
                local.origin = None
                self.pipelines[local.name] = local
                try:
                    pipelines_mod.validate_pipeline_set(self.pipelines, self.layers)
                except pipelines_mod.PipelineError:
                    # this pipeline alone made the set invalid (stale layer
                    # ref, target collision) — drop just it, same tolerance
                    # as the parse failure above
                    del self.pipelines[local.name]
        # agents/*.yaml — reloaded every pass (unlike the one-time skill
        # registration in init() above) so editing an agent's declared skill
        # list and reloading changes what the MCP server exposes, with no
        # code change (spec 017 User Story 3).
        self.agents = agents_mod.load_agents(config.AGENTS_DIR)

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

    def read_bundle_text(self, bundle: semantic.DimensionBundle) -> str:
        if bundle.locked:
            return bundle.origin.read_text()
        row = self.local_bundle_store.get(bundle.name)
        return row["yaml"] if row else ""

    def read_pipeline_text(self, pipeline: pipelines_mod.Pipeline) -> str:
        if pipeline.locked:
            return pipeline.origin.read_text()
        row = self.local_pipeline_store.get(pipeline.name)
        return row["yaml"] if row else ""


registry = Registry()
