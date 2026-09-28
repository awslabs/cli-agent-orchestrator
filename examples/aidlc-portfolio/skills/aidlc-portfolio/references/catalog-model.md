# Portfolio Catalog Model

## Evidence Hierarchy

Use the strongest available source:

1. Versioned contracts, IaC, deployment manifests, and executable configuration.
2. Source code and package manifests.
3. Ownership files and maintained architecture documentation.
4. Human-confirmed business or organization knowledge.
5. Repository names and informal inference, which may only produce `proposed`
   facts.

Every relationship includes:

- `status`: `discovered`, `proposed`, or `verified`;
- `confidence`: `low`, `medium`, or `high`;
- `evidence`: source path or human decision record; and
- `lastVerified`: date when the fact was checked.

## Catalog Layers

- Organization: domains, governance, standards, compliance, ownership.
- Business: outcomes, actors, capabilities, KPIs, criticality.
- Project: repository purpose, lifecycle, owners, environments.
- Component: deployables, libraries, data stores, interfaces, infrastructure.
- Dependency: typed relationship between projects or components.
- Intent: bounded outcome assigned to one or more projects and worktrees.

## Dependency Types

- `runtime`: synchronous or asynchronous runtime interaction.
- `contract`: API, event, or schema compatibility.
- `data`: production, consumption, transfer, or shared persistence.
- `build`: package, artifact, generated client, or compilation dependency.
- `deployment`: infrastructure or environment ordering.
- `operational`: observability, incident, support, or runbook dependency.
- `business`: components jointly deliver a business capability.
- `release`: rollout, migration, or compatibility ordering.

Do not treat every dependency as blocking. Record the lifecycle checkpoints it
blocks: `dispatch`, `design`, `build`, `integration`, or `release`.

