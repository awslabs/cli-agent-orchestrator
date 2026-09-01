"""Which plugin contributed a given skill — a read-only lookup over install records.

Projection deliberately names each link with the **unprefixed** skill name:
``utils/skills.py::_load_skill_folder`` raises when a folder name differs from
its frontmatter ``name``, and the Agent Skills specification requires the same,
so no namespacing prefix is possible without rewriting plugin bytes — which
§4.1's "CAO never mutates a PLUGIN_ROOT" posture forbids.

The cost of that is provenance not being visible in the folder name, and this
module is the mitigation: ``cao plugin list``, the web panel, and the
``cao skills list`` annotation all recover it from the install records. It is
also the prompt-injection mitigation of record — plugin skill content flows into
every agent's system prompt, so an operator must always be able to see which
plugin contributed a given skill.

``SkillMetadata`` is deliberately **not** extended: it models ``SKILL.md``
frontmatter, and projection is not frontmatter.
"""

from __future__ import annotations

from typing import Dict, Optional

from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore


def projection_map(store: Optional[InstalledPluginStore] = None) -> Dict[str, str]:
    """Return ``{skill_name: owning_plugin_name}`` for every projected skill.

    Built from the records' ``projected_skill_names``, which the projection
    rebuild keeps in step with what is actually on disk.
    """
    store = store or InstalledPluginStore()
    owners: Dict[str, str] = {}
    for record in store.list_installed():  # already sorted by plugin name
        for skill_name in record.projected_skill_names:
            owners.setdefault(skill_name, record.name)
    return owners


def owning_plugin(
    skill_name: str,
    store: Optional[InstalledPluginStore] = None,
) -> Optional[str]:
    """Return the plugin that projected ``skill_name``, or ``None``.

    ``None`` means the skill is not plugin-provided — it is a built-in, a
    ``cao skills add`` install, or an ``extra_dirs`` skill. It also covers the
    case where a plugin *claims* the name but lost a collision, which is
    correct: the operator is asking who provides the skill they can actually
    resolve.
    """
    return projection_map(store).get(skill_name)
