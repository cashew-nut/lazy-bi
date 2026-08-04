"""The Agent abstraction (specs/017-agent-skills-mcp-server/): a named,
described bundle of Skill names, declared in `agents/*.yaml` the same way a
model is declared in `models/*.yaml` — an Agent carries no privilege of its
own; every skill call is still gated by that skill's own `min_role` against
the caller's real role (see app/skills.py), regardless of which agent's
declaration listed it.

Loaded once at startup by `Registry.init()` (app/registry.py), after the
skill registry itself has been populated (app/skills_analytics.py's import
side effect) — an agent referencing an unregistered skill name is a
load-time error, not a silently-skipped entry, mirroring how app/semantic.py
validates model YAML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import skills as skills_mod


class AgentError(Exception):
    """A malformed or invalid agents/*.yaml declaration."""


@dataclass
class Agent:
    name: str
    label: str
    description: str
    skills: list[str] = field(default_factory=list)


def _parse_agent(raw: dict, origin: Path) -> Agent:
    if not isinstance(raw, dict):
        raise AgentError(f"{origin.name}: yaml must be a mapping with name / skills")
    try:
        name = raw["name"]
    except KeyError as exc:
        raise AgentError(f"{origin.name}: agent missing required key {exc}") from exc
    agent = Agent(
        name=name,
        label=raw.get("label", name),
        description=raw.get("description", ""),
        skills=list(raw.get("skills", [])),
    )
    for skill_name in agent.skills:
        if skills_mod.get_skill(skill_name) is None:
            raise AgentError(
                f"{origin.name}: agent '{agent.name}' references unknown skill '{skill_name}'"
            )
    return agent


def load_agents(directory: Path) -> dict[str, Agent]:
    """Parse every *.yml/*.yaml file in `directory` into an Agent, indexed
    by name. An empty/missing directory yields no agents (same tolerance as
    app/semantic.py's model loader) rather than an error."""
    agents: dict[str, Agent] = {}
    if not directory.is_dir():
        return agents
    for path in sorted(directory.glob("*.y*ml")):
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        if raw is None:  # empty file — skip quietly, same tolerance as pipelines' layers.yaml
            continue
        agent = _parse_agent(raw, path)
        if agent.name in agents:
            raise AgentError(f"{path.name}: duplicate agent name '{agent.name}'")
        agents[agent.name] = agent
    return agents
