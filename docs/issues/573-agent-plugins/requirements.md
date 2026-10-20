# Requirements Document

> **Feature:** `cao-agent-plugins`
> **Issue of record:** [awslabs/cli-agent-orchestrator#573](https://github.com/awslabs/cli-agent-orchestrator/issues/573) — "[Feat] Agent Plugins 1.0 support — package the CAO ecosystem as portable plugins + a plugin install/marketplace surface" (labels `enhancement`, `feature`).
> **Derived from:** `design.md`. Every requirement below traces to a specific design.md section, correctness property (P1–P11), or #573 acceptance criterion (AC1–AC7); none paraphrases the issue directly.
> **Open maintainer decisions:** Maintainer input on **M1–M4** (see Requirement 21) may change decisions this document deliberately leaves open.

## Scope of this document

**#573 is the canonical statement of intent.** This file is retained for one
reason: the test suite cites its numbered criteria (`**Validates: Requirement
13.2, 18.9**` and similar), so it is the traceability target that makes those
citations checkable. The narrative rationale lives in `design.md` and the
user-facing motivation in the issue; neither is restated here.

Two increments are referenced throughout, matching `design.md`'s Increment
Boundary:

- **Increment 1** — skills-only; conformant and shippable on its own per Agent
  Plugins §11.2, but does **not** close #573 by itself.
- **Increment 2** — adds `mcp.json` support: PLUGIN_ROOT/PLUGIN_DATA expansion,
  per-provider MCP mapping, and `mcp.json` in CAO's own operator package.

Where a #573 acceptance criterion spans both increments it is split and tagged by
increment rather than flattened, because reporting such an AC as met at the end of
Increment 1 would be incorrect.

## Requirements

### Requirement 1: Operator Package Content and Conformance

**User Story:** As an operator driving CAO from a foreign Agent-Plugins-compatible client, I want to install a single conformant package that lets me launch and manage CAO sessions, so that I do not need CAO-specific integration code in my client.

#### Acceptance Criteria

1. THE Operator_Package SHALL declare a `plugin.json` whose `$schema` value identifies the Agent Plugins 1.0.0 canonical schema and whose `name` field is a value distinct from the Event_Plugin_System's identifiers.
2. THE Operator_Package SHALL include the `cao-session-management` skill and the `cao-agent-routing` skill.
3. WHERE `cao-supervisor-protocols` and `cao-worker-protocols` are designated by maintainers as portable-package content (an item `design.md` records as maintainer-tunable, defaulting to inclusion), THE Operator_Package SHALL include both skills.
4. THE Operator_Package SHALL NOT include `cao-provider`, the event-plugin authoring skill (named `cao-plugin` or `cao-event-plugin` pending **M4**), or any skill under the vendored `skills/vendor/ext-apps/` tree.
5. THE Operator_Package `plugin.json` `description` field SHALL state the `uv`-on-`PATH` prerequisite, the local CAO API server prerequisite, and the localhost-only communication posture.
6. [Increment 1] WHEN the Operator_Package is validated by the Validator, THE Validator SHALL report `loadable=true` with zero `FATAL` findings and SHALL discover `cao-session-management` among its skills.
7. [Increment 2] THE Operator_Package SHALL include an `mcp.json` as specified in Requirement 19.

**Traceability:** design.md §"Two packages, not one", §"Skill allowlists", §"Package prerequisites"; validates AC2 (skill half), AC3, AC7 (prerequisite statement); exercised by Property P6, P10.

---

### Requirement 2: Contributor Package Content and Conformance

**User Story:** As a contributor extending CAO itself, I want to install a package of authoring skills scoped to development tasks, so that my authoring agent has the right context without CAO's operator-facing capabilities crowding its prompt.

#### Acceptance Criteria

1. THE Contributor_Package SHALL declare a `plugin.json` whose `$schema` value identifies the Agent Plugins 1.0.0 canonical schema.
2. THE Contributor_Package SHALL be a package distinct from the Operator_Package, satisfying AC3's requirement for a separate contributor plugin.
3. THE Contributor_Package SHALL include the `cao-provider` skill and the event-plugin authoring skill, packaged under whichever name **M4** settles (`cao-plugin` or `cao-event-plugin`).
4. THE Contributor_Package SHALL NOT include `cao-session-management`, `cao-agent-routing`, or any other operator-facing skill.
5. THE Contributor_Package SHALL NOT include an `mcp.json` in either Increment 1 or Increment 2.
6. IF pull request #448 ("feat(skills): add cao-contributing skill") is open and not yet merged, THEN THE Contributor_Package SHALL NOT include a `cao-contributing` skill, and this omission SHALL NOT be treated as a validation failure.
7. WHEN pull request #448 merges, THE Contributor_Package's skill allowlist SHALL be extended to include `cao-contributing` without requiring a change to package structure, build tooling, or CI configuration.
8. THE name of the Contributor_Package SHALL remain `cao-contributor` unless and until **M4** determines otherwise; this document does not settle that name.

**Traceability:** design.md §"Two packages, not one", §"Skill allowlists", §"AC3" coverage row; validates AC3; references M4 without resolving it.

---

### Requirement 3: Multi-Package Build, Versioning, and Drift Guard

**User Story:** As a maintainer, I want both CAO packages built and version-synced from one source of truth with an automated drift check, so that the packages cannot silently diverge from the skills and version they claim to ship.

#### Acceptance Criteria

1. THE build process SHALL generate both the Operator_Package and the Contributor_Package from a single per-package configuration that specifies each package's name, manifest fields, and skill allowlist independently.
2. THE `version` field in each package's `plugin.json` SHALL be synchronized from CAO's own package metadata such that the two values cannot diverge without a build failure.
3. WHEN the build process runs in a drift-check mode, THE build process SHALL exit with a non-zero status if either package's generated tree diverges from its declared skill allowlist or source skills.
4. WHEN the build process runs in a drift-check mode, THE build process SHALL exit with a non-zero status if either package fails Validator loading with a `FATAL` finding.
5. THE drift-check mode SHALL evaluate both packages independently, such that a failure in one package's check does not suppress reporting of a failure in the other.

**Traceability:** design.md §"Package layout", §"Skill allowlists"; validates AC1 (plugin.json half), AC3; supports W9 acceptance signal.

---

### Requirement 4: Schema Pinning and Offline Validation

**User Story:** As a security-conscious operator, I want plugin validation to run entirely against schemas already committed to the CAO repository, so that no network compromise of a schema host can change what CAO considers valid.

#### Acceptance Criteria

1. THE Validator SHALL validate `plugin.json` and (Increment 2) `mcp.json` against schema files vendored inside CAO's own repository, never fetched over a network connection at validation time.
2. THE vendored schema files SHALL be accompanied by a recorded source reference and a cryptographic hash of each file's bytes.
3. WHEN a drift-check is run against the vendored schemas, THE drift-check SHALL exit with a non-zero status if any vendored schema file's bytes no longer match its recorded hash.
4. WHILE all outbound socket operations are blocked, THE Validator SHALL still successfully complete validation of a valid plugin, demonstrating that no schema retrieval is required.
5. IF a plugin's `plugin.json` declares a `$schema` value CAO does not recognize as a locally pinned schema version, THEN THE Validator SHALL reject the plugin and report the unrecognized schema version as a `FATAL` finding.

**Traceability:** design.md §"Schema pinning", Property **P11**; validates AC1 (plugin.json half, and gates the mcp.json half described in Requirement 19).

---

### Requirement 5: Plugin Validation Totality

**User Story:** As any caller of the Validator (CLI, API, web panel, installer, or CI), I want a structured, complete answer for any candidate directory, so that partial success can be reported instead of an unhandled exception.

#### Acceptance Criteria

1. WHEN the Validator is given any directory tree — including one containing arbitrary bytes in place of `plugin.json`, symlink loops, unreadable file modes, zero-byte files, or non-UTF-8 content — THE Validator SHALL return a validation report and SHALL NOT raise an exception.
2. THE Validator SHALL terminate for every input within a bounded time proportional to the size of the candidate directory.
3. THE validation report's `loadable` field SHALL equal `true` if and only if the report contains zero findings of `FATAL` severity.
4. THE validation report's `loadable` field SHALL be a derived value computed from the findings, and SHALL NOT be set independently of them.

**Traceability:** design.md §"Validator", §"Data Models" (`PluginValidationReport.loadable`); validates Property **P1**; validates AC4 (fatal violation before any component loads).

---

### Requirement 6: Manifest Fatality Classification

**User Story:** As a plugin author, I want to know which manifest problems are tolerated and which reject my plugin outright, so that I can distinguish a warning from a blocking error.

#### Acceptance Criteria

1. IF a manifest contains an unrecognized top-level field, THEN THE Validator SHALL report a `WARNING`-severity finding, ignore the field, and continue loading the plugin.
2. IF a manifest's `extensions` member is present but is not a JSON object, THEN THE Validator SHALL report a `WARNING`-severity finding, ignore the member, and continue loading the plugin.
3. IF a manifest's `extensions` member names a namespace CAO does not implement, THEN THE Validator SHALL ignore that namespace's contents entirely without validating them, and SHALL NOT report a finding for it.
4. IF a manifest violates any requirement other than an unrecognized top-level field or a non-object `extensions` member (including a missing required field, invalid JSON, or a name violating the length or character constraints), THEN THE Validator SHALL set `loadable=false` and SHALL discover zero components for that plugin.

**Traceability:** design.md §"Validator", §"Failure Isolation and Reporting" table; validates Property **P2**; validates AC4 (fatal manifest violation rejects the plugin before any component loads).

---

### Requirement 7: Path Containment Enforcement

**User Story:** As an operator installing a third-party plugin, I want every path a plugin references to be confined to that plugin's own root, so that a malicious or buggy plugin cannot read or write outside its boundary.

#### Acceptance Criteria

1. THE Installer SHALL resolve every discovered skill directory, every resolved MCP `command` path (Increment 2), and every resolved `cwd` (Increment 2) via realpath canonicalization rather than lexical path inspection before treating it as valid.
2. IF a path's realpath-canonicalized location lies outside the realpath-canonicalized plugin root, THEN THE Installer SHALL apply the narrowest applicable failure boundary: reject the whole plugin if `plugin.json` itself is outside the root; invalidate only that component type if a fixed component location is outside the root; skip only that skill if a discovered `SKILL.md` is outside the root; invalidate only that MCP server entry if its `command` or `cwd` is outside the root.
3. THE Installer SHALL permit a symlink whose target resolves within the plugin root and SHALL reject a symlink whose target resolves outside the plugin root, regardless of the lexical path used to reach it.
4. THE Installer SHALL apply containment resolution to paths introduced via symlinks, not only to paths written literally in configuration values.

**Traceability:** design.md §"Containment" (§4.1 ladder); validates Property **P3**; validates AC7 (containment enforced on install).

---

### Requirement 8: Plugin Source Resolution

**User Story:** As an operator, I want to install a plugin from either a local directory or a GitHub repository, so that I can use whichever source is convenient, including CAO's own in-repo packages.

#### Acceptance Criteria

1. WHEN a plugin source is a local directory path, THE Resolver SHALL copy that directory's contents into a staging location rather than referencing the original location in place.
2. WHEN a plugin source is a git repository reference, THE Resolver SHALL clone the repository into a staging location and SHALL record the resolved commit identifier in the resulting install record.
3. WHERE a git plugin source specifies a subdirectory within the repository, THE Resolver SHALL address that subdirectory as the candidate plugin root rather than the repository root.
4. IF a plugin source is unreachable (an invalid local path or a failed git operation), THEN THE Resolver SHALL report the failure with the underlying cause and SHALL leave the installed set unchanged.
5. THE Resolver SHALL NOT perform plugin name resolution against any index, dependency solving, or signature verification as part of resolving a source.

**Traceability:** design.md §"Resolver"; validates AC5 (install from a GitHub URL); supports Requirement 3's use of `--subdir` for CAO's own packages.

---

### Requirement 9: Atomic Installation and Isolation on Failure

**User Story:** As an operator, I want a failed plugin install to leave my existing installation untouched, so that one bad install attempt cannot corrupt or destabilize plugins I already have working.

#### Acceptance Criteria

1. WHEN the Installer processes an install request, THE Installer SHALL resolve the source and validate the staged copy before publishing anything to the Store.
2. IF the staged copy is not loadable, THEN THE Installer SHALL return the validation report and SHALL NOT publish the plugin, SHALL NOT modify the Store, and SHALL NOT modify the skill projection.
3. WHEN a plugin is loadable, THE Installer SHALL publish it to the Store using a stage-then-rename operation such that a process interruption during publish leaves the Store byte-identical to its pre-install state.
4. FOR ANY invalid plugin and any pre-existing installed set, an install attempt SHALL leave the installed set and the skill projection unchanged from their state immediately before the attempt.
5. IF a plugin name already exists in the installed set and the `force` option is not supplied, THEN THE Installer SHALL refuse the install and SHALL suggest the `force` option.

**Traceability:** design.md §"Installer" (ordering, stage-then-rename); validates Property **P4**; validates AC4 (fatal violation rejects the plugin, nothing published).

---

### Requirement 10: Install and Removal Idempotence

**User Story:** As an operator, I want repeated installs and a subsequent removal to behave predictably, so that re-running a command or cleaning up a plugin does not leave the system in an ambiguous state.

#### Acceptance Criteria

1. WHEN a plugin is installed and then reinstalled with the `force` option, THE Store SHALL reach the same state as a single install of that plugin.
2. WHEN a plugin is installed and then removed, THE Store SHALL be restored to its pre-install state, except that the plugin's Plugin_Data directory SHALL persist.
3. WHEN a plugin is removed with the purge-data option supplied, THE Store SHALL also delete that plugin's Plugin_Data directory.
4. WHEN a plugin is removed without the purge-data option supplied, THE Store SHALL retain that plugin's Plugin_Data directory.

**Traceability:** design.md §"Store and paths" (`unpublish(purge_data=False)` default); validates Property **P5**.

---

### Requirement 11: Increment 1 Skills-Only Conformance Boundary

**User Story:** As a maintainer shipping Increment 1 independently of Increment 2, I want CAO's client-side behavior to remain fully conformant and safe with `mcp.json` support entirely absent, so that Increment 1 can ship on its own schedule without half-built MCP handling.

#### Acceptance Criteria

1. FOR ANY plugin with a valid manifest, at least one valid skill, and no `mcp.json` present, THE Validator SHALL report `loadable=true`, SHALL discover and project every valid skill, SHALL report `mcp_present=false`, and SHALL report zero `FATAL`-severity findings.
2. IF a plugin includes an `mcp.json` file, THEN, WHILE Increment 1 is the delivered scope, THE Validator SHALL record `mcp_present=true`, report an "MCP not supported in this CAO version" finding, and SHALL continue to deliver that plugin's skills unaffected.
3. WHILE Increment 1 is the delivered scope, CAO's Increment-1 codebase SHALL contain no code path that expands `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`, launches a subprocess on behalf of a plugin, or validates against the `mcp.schema.json` file.
4. THE automated test suite exercised in Increment 1 SHALL contain zero tests that launch a plugin subprocess.
5. WHILE Increment 1 is the delivered scope, THE Operator_Package SHALL ship with no `mcp.json` file.
6. THIS requirement's satisfaction SHALL NOT, by itself, be reported as closing AC1 or AC2 of #573; those acceptance criteria additionally require Increment 2 behavior described in Requirements 18 and 19.

**Traceability:** design.md §"Increment Boundary"; validates Property **P6**; makes explicit the increment-splitting called out in the coverage table for AC1 and AC2.

---

### Requirement 12: Sibling Skill Independence

**User Story:** As a plugin author, I want one broken skill inside my plugin to be reported and skipped without disabling my plugin's other skills, so that a single mistake does not take down the whole package.

#### Acceptance Criteria

1. FOR a plugin with N skill directories among which k are invalid, THE Validator SHALL discover exactly N−k valid skills and SHALL report exactly k `SKIPPED` findings, one per invalid skill.
2. THE set of skills discovered as valid SHALL be independent of the order in which the plugin's skill directories are enumerated on disk.
3. IF the `skills/` location exists but is not a directory, THEN THE Validator SHALL invalidate only the skills component type and SHALL NOT invalidate MCP component discovery (Increment 2) for the same plugin.
4. IF the `skills/` location does not exist at all, THEN THE Validator SHALL NOT treat this as an error and SHALL continue validating other component types.

**Traceability:** design.md §"Validator", §"Failure Isolation and Reporting" table; validates Property **P7**; validates AC4 (invalid sibling skill skipped with a report; missing `skills/` tolerated).

---

### Requirement 13: Cross-Provider Skill Projection and Delivery

**User Story:** As an operator using any CAO-supported provider, I want a plugin's skills to be discoverable by whichever provider I launch, so that installing a plugin once benefits every provider without per-provider configuration.

#### Acceptance Criteria

1. WHEN a plugin skill is discovered as valid and does not collide with an existing skill name, THE Projection_Engine SHALL make that skill discoverable through the same skill-discovery mechanism CAO's existing built-in and user-added skills already use, without requiring provider-specific code changes.
2. FOR EVERY CAO-supported provider (Claude Code, Codex, Kimi, Antigravity, Copilot, Kiro CLI, OpenCode), the set of skill names reachable by an agent launched under that provider SHALL include every built-in skill and every successfully projected valid plugin skill, and SHALL NOT include any skill that is neither built-in, nor supplied by a configured extra directory, nor a successfully projected valid plugin skill. The extra-directory term is provider-dependent and is specified by Criteria 6 and 7.
3. WHEN a plugin is installed, updated, or removed, THE Projection_Engine SHALL rebuild the skill projection as a pure function of the currently installed set, rather than incrementally patching prior projection state.
4. IF symlink creation is unsupported in the current environment, THEN THE Projection_Engine SHALL fall back to copying skill content instead of linking it, and SHALL report this fallback.
5. THE terminal-launch path SHALL NOT perform any additional filesystem scan beyond the scan it already performs for built-in and user-added skills, so that skill projection introduces no new launch-time cost.
6. WHERE a provider receives its skill catalog through CAO's skill-discovery scan (Claude Code, Codex, Kimi, Antigravity, Copilot), the reachable set SHALL additionally include user-configured extra-directory skills.
7. WHERE a provider discovers skills by traversing the skill-store path directly (Kiro CLI via its `skill://` resource glob, OpenCode via its skill-directory symlink), user-configured extra-directory skills SHALL NOT be expected in the reachable set, because such a provider receives a path rooted at the skill store and extra directories lie outside it. THIS limitation SHALL be recorded as a known limitation in the skills documentation, and SHALL NOT be treated as a defect introduced by this feature.

**Traceability:** design.md §"Skill Delivery (the critical seam)", §"Performance Considerations"; validates Property **P10**; validates AC2 (skill-discovery half) and AC4 (skill delivered to a provider).

> **Amendment note (W6 finding, maintainer-approved).** Criterion 2 originally required the reachable set to *equal* `built-in ∪ extra-directory ∪ projected` for **every** provider. Cross-provider verification (W6) demonstrated that this is not satisfiable by the current codebase and never was: `services/install_service.py` hands Kiro CLI a `skill://` glob rooted at `SKILLS_DIR` and OpenCode a single `OPENCODE_CONFIG_DIR/skills` symlink to the store, so skills registered through `skills.extra_dirs` — which live outside the store — are invisible to both. It was reproduced **with zero plugins installed**, confirming it predates agent plugins and is not a projection regression.
>
> Criterion 2 has therefore been narrowed to the guarantee this feature is actually responsible for — built-in and projected plugin skills reach every provider, and nothing spurious appears — and the extra-directory term is split into the new provider-dependent Criteria 6 and 7. **Criteria 3–5 keep their original numbering and meaning**, so existing `13.3` / `13.4` / `13.5` references in `design.md` and the implementation remain correct; inserting the new criteria mid-list would have silently invalidated those cross-references.
>
> Decision recorded: amend the requirement and document the limitation, rather than expand this feature's scope to modify provider paths the design deliberately froze. Asserted by `test_every_provider_reaches_the_full_union` in `test/agent_plugins/test_delivery_equivalence.py`, which excludes the extra-directory skill for exactly the two providers Criterion 7 names.

---

### Requirement 14: Projection Collision Resolution

**User Story:** As an operator with multiple plugins and pre-existing skills, I want skill-name collisions resolved by a fixed, predictable rule, so that the outcome does not depend on install order or directory scan order.

#### Acceptance Criteria

1. IF a plugin-provided skill name matches the name of a pre-existing built-in or user-added skill, THEN THE Projection_Engine SHALL skip projecting the plugin's skill, SHALL leave the pre-existing skill resolvable exactly as before, and SHALL report a `SKIPPED` finding naming the collision.
2. IF two or more installed plugins provide a skill with the same name and no pre-existing built-in or user-added skill has that name, THEN THE Projection_Engine SHALL select the plugin whose manifest `name` is lexicographically smallest as the projected skill's source, and SHALL report a `SKIPPED` finding for every other claimant naming the winning plugin.
3. WHEN the installed set is unchanged, repeated rebuilds of the skill projection SHALL select the same winning plugin for every colliding skill name.
4. FOR ANY permutation of the order in which a fixed set of colliding plugins is installed, the resulting winning plugin for a given collision SHALL be identical across all permutations.
5. THE collision-resolution rule SHALL NOT depend on plugin install timestamp, directory scan order, or any other value not derived from the plugin's persisted manifest `name`.

**Traceability:** design.md §"Skill Delivery" (collision rules), §"Failure Isolation and Reporting" table; validates Property **P8**; validates AC4 (invalid or colliding sibling skill isolation).

---

### Requirement 15: Removal Safety During Live Sessions

**User Story:** As an operator removing a plugin while a CAO session is running, I want to be warned if the removal could affect that session, so that I can make an informed choice rather than silently breaking a running agent.

#### Acceptance Criteria

1. WHEN a plugin removal is requested and at least one live session's profile references a skill projected by that plugin, THE CLI SHALL report which live sessions and which skill names are affected and SHALL require confirmation before proceeding.
2. WHERE the confirmation-bypass option is supplied, THE CLI SHALL proceed with removal without requiring interactive confirmation.
3. THE removal confirmation check described in Criterion 1 SHALL warn rather than refuse the removal outright, even when a live session is affected.
4. WHEN the skill projection is rebuilt or swept for dangling links concurrently with a new terminal being created, THE sweep operation SHALL NOT raise an exception into terminal creation.
5. IF a dangling projected skill link cannot be removed due to a filesystem permission error, THEN THE sweep operation SHALL log the failure at warning level and SHALL continue processing the remaining links without halting.

**Traceability:** design.md §"Removal while a session is live"; validates the W5/W7 acceptance signals recorded in design.md's failure-isolation table.

---

### Requirement 16: CLI Management Surface for Agent Plugins

**User Story:** As an operator, I want command-line capabilities to add, list, remove, and validate agent plugins, so that I can manage them without leaving the terminal.

#### Acceptance Criteria

1. THE CLI SHALL provide a capability to install an agent plugin from a source, a capability to list installed agent plugins, a capability to remove an installed agent plugin, and a capability to validate a candidate plugin directory without installing it.
2. WHERE the `--json` option is supplied to the list or validate capability, THE CLI SHALL emit a machine-readable report suitable for automated consumption.
3. WHERE the `--json` option is not supplied, THE CLI SHALL emit a human-readable report.
4. THE verb under which these capabilities are exposed SHALL be the verb maintainers approve under decision **M1** (design.md records `cao plugin` as the recommended option and `cao agent-plugin` as an alternative); this document does not itself select the verb.
5. IF decision **M1** has not been resolved by maintainers, THEN CAO SHALL NOT ship this CLI surface to end users, consistent with AC6's requirement that the naming decision be recorded and applied before any public surface ships.
6. WHEN a plugin removal would affect a live session as described in Requirement 15, THE CLI SHALL apply that same warning-and-confirmation behavior.

**Traceability:** design.md §"CLI", §"CLI verb collision (M1 — blocking)"; validates AC5 (CLI list/remove) and AC6 (naming decision gates public surfaces); references M1 without resolving it.

---

### Requirement 17: HTTP API and Web Panel for Agent Plugins

**User Story:** As an operator using CAO's web UI, I want to view, install, and remove agent plugins visually, so that I do not need the CLI for routine plugin management.

#### Acceptance Criteria

1. THE API SHALL provide an endpoint to list installed plugins together with each plugin's findings and projected skill names, an endpoint to install a plugin from a source, an endpoint to validate a plugin without installing it, and an endpoint to uninstall a named plugin.
2. THE Web_Panel SHALL render the installed set, each plugin's findings (including non-fatal ones), and the skills each plugin contributes.
3. THE Web_Panel SHALL provide an affordance to install a plugin from a GitHub URL.
4. WHEN a new tab is added to the web UI for the Web_Panel, THE web UI SHALL append that tab after all existing tabs so that existing tab positions and their keyboard shortcuts are unaffected.
5. WHEN a plugin removal via the API would affect a live session as described in Requirement 15, THE API SHALL report the affected sessions and skill names in its response.

**Traceability:** design.md §"HTTP API and web panel"; validates AC5 (CLI and Web UI list/remove, install from a GitHub URL).

---

### Requirement 18: MCP Configuration Mapping (Increment 2)

**User Story:** As an operator on a client that supports MCP servers, I want a plugin's `mcp.json` translated correctly into CAO's internal MCP configuration, so that the declared server behaves exactly as the plugin author specified.

#### Acceptance Criteria

1. [Increment 2] WHEN the MCP_Mapper processes an `mcp.json` entry, THE MCP_Mapper SHALL expand only the placeholders `${PLUGIN_ROOT}` and `${PLUGIN_DATA}`, only within `args` elements, `env` values, and `cwd` — never within `env` keys, `command`, `url`, or header names or values.
2. [Increment 2] THE MCP_Mapper's placeholder expansion SHALL be single-pass: text introduced by a replacement SHALL NOT be rescanned for further placeholder expansion.
3. [Increment 2] IF a value contains a `${...}` token that is not `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`, THEN THE MCP_Mapper SHALL leave that token literal and unexpanded.
4. [Increment 2] THE MCP_Mapper SHALL mark every mapped MCP entry as pre-expanded so that CAO's separate profile-level placeholder interpolation does not re-process it.
5. [Increment 2] IF a plugin's `mcp.json` `env` block declares a key named `PLUGIN_ROOT` or `PLUGIN_DATA`, THEN THE MCP_Mapper SHALL invalidate that server entry and report a finding, rather than allowing the plugin-declared value to override CAO-supplied values.
6. [Increment 2] IF an MCP server entry's resolved `command` or `cwd` fails path containment against the plugin root (or, for `cwd`, against `${PLUGIN_DATA}` when so rooted), THEN THE MCP_Mapper SHALL invalidate only that server entry, leaving sibling entries and the plugin's skills unaffected.
7. [Increment 2] IF an MCP server entry declares a transport type unsupported by the target provider, THEN THE MCP_Mapper SHALL skip that entry with a report rather than substituting a different transport.
8. [Increment 2] IF a value in an MCP server entry's `env` or `headers` block appears credential-shaped, THEN THE MCP_Mapper SHALL report a `WARNING`-severity finding and SHALL NOT block installation or reject the entry on that basis alone.

9. [Increment 2] WHEN an agent profile is installed for a provider, THE MCP_Mapper's output for every installed plugin SHALL be merged into that profile's MCP server configuration, so that a declared server is actually reachable by the provider rather than only reported.
10. [Increment 2] WHERE a mapped server's name is already declared by the agent profile, THE system SHALL keep the profile's declaration and report the plugin's entry as skipped, and SHALL NOT rename, prefix, or merge the plugin's entry.
11. [Increment 2] WHERE two installed plugins declare the same MCP server name, THE system SHALL deliver the entry from the lexicographically smallest plugin name and report the other as skipped.
12. [Increment 2] WHEN a plugin is installed or removed, THE system SHALL re-materialize the MCP configuration of already-installed agents, so that a plugin's servers reach existing agents and do not outlive the plugin.

**Traceability:** design.md §"MCP mapping — Increment 2 only" and §"10a. MCP delivery — the consumer this section presupposed"; validates Property **P9**; validates AC7 (credential-shape warning, Increment 2 portion) and the second half of AC1/AC2 as described in Requirement 11's increment-splitting note.

> **Amendment note.** Criteria 1–8 specify the *mapper* and were implemented and tested, but nothing consumed the mapper's output: it reached the validation report and stopped, leaving `docs/agent-plugins.md` claiming servers were delivered when they were not, and leaving the pre-expanded marker of Criterion 4 unreachable in production. Criteria 9–12 were added to state the delivery obligation the mapper's design already presupposed — Criterion 4 is only meaningful if something puts a marked entry where CAO's interpolation pass will encounter it. Criteria 1–8 keep their numbering and meaning. Covered by `test/agent_plugins/test_mcp_delivery.py`, which drives `install_agent` and reads the emitted provider config rather than re-implementing the merge.

---

### Requirement 19: Packaged MCP Server Selection and Version Pinning (Increment 2)

**User Story:** As an operator installing the Operator_Package, I want the packaged MCP server to be the one that actually works outside a CAO-managed session, at a version that matches what the plugin declares, so that tool calls do not fail on first use or drift from the declared plugin version.

#### Acceptance Criteria

1. [Increment 2] THE Operator_Package's `mcp.json` SHALL declare a server under the key `cao-ops` whose invocation launches the `cao-ops-mcp-server` console script, and SHALL NOT declare a server that launches the in-session `cao-mcp-server` entry point.
2. [Increment 2] THE `command` field of the `cao-ops` server entry SHALL be the single token `uvx`, with all remaining invocation detail supplied via `args`.
3. [Increment 2] THE `args` field of the `cao-ops` server entry SHALL pin an exact, published version of the `cli-agent-orchestrator` package rather than resolving to the latest available release at invocation time.
4. [Increment 2] THE pinned version in the `cao-ops` server entry SHALL be produced by the same build step that synchronizes the `plugin.json` `version` field, such that the two values cannot diverge without a build failure.
5. [Increment 2] WHEN the build process pins a version for the `cao-ops` server entry, THE build process SHALL verify that the pinned version is already published before writing it, and SHALL fail if it is not.
6. [Increment 2] THE `cao-ops` server entry SHALL declare no `env` and no `headers` fields.
7. [Increment 2] THE `$schema` value declared in `mcp.json` SHALL equal the `$schema` value declared in the same package's `plugin.json`.

**Traceability:** design.md §"Why the *ops* server, not `cao-mcp-server`", §"Version pinning"; validates AC2 (tools-callable half); corrects the earlier `cao-mcp-server` packaging choice at the requirements level per instruction.

---

### Requirement 20: CAO API Server Availability Behavior

**User Story:** As an operator whose CAO API server is not running, I want any Ops_MCP_Server tool call to fail with a clear, structured error rather than hanging or silently returning nothing, so that I know exactly what to do next.

#### Acceptance Criteria

1. IF a tool call to the Ops_MCP_Server occurs while the CAO API server is unreachable, THEN THE Ops_MCP_Server SHALL return a structured tool-level error string naming the failed operation and the underlying cause.
2. THE Ops_MCP_Server SHALL NOT hang indefinitely on a connection failure to the CAO API server.
3. THE Ops_MCP_Server SHALL NOT return a silent empty result in place of an error when the CAO API server is unreachable.
4. THE Ops_MCP_Server SHALL NOT attempt to self-start a CAO API server under any circumstance; starting the CAO API server remains an operator-initiated action.

**Traceability:** design.md §"Resolved: behavior when `cao-server` is not running"; records the resolved open question as settled behavior per the given instruction, without re-opening it as a question.

---

### Requirement 21: Naming and Namespace Decisions (M1–M4)

**User Story:** As a maintainer reviewing this feature, I want the naming and namespace decisions this design leaves open to remain visibly open in the requirements, so that requirements.md does not become the place those decisions get settled by default.

#### Acceptance Criteria

1. THE requirements in this document that reference a CLI verb for agent-plugin management SHALL describe the underlying capability (add/list/remove/validate an agent plugin) and SHALL treat the exact verb text as unresolved pending decision **M1**, as described in Requirement 16.
2. WHILE Increment 1 is the delivered scope, CAO SHALL implement no reverse-domain extension namespace under decision **M2**, and SHALL treat every namespace found in a manifest's `extensions` member as unimplemented per Requirement 6, Criterion 3 — regardless of whether **M2** has been decided.
3. IF decision **M2** is later resolved, THEN CAO SHALL NOT read or document any `extensions` namespace data until that resolution is recorded.
4. THE documentation covering event plugins and agent plugins SHALL consistently distinguish the two systems by name (never using an unqualified "plugin" outside a document whose title already scopes it), with the exact retitling and banner treatment of the existing event-plugin documentation page subject to decision **M3**.
5. THE decision to rename the `cao-plugin` authoring skill to `cao-event-plugin`, and the retirement step required so an upgrade does not leave both the old and new skill directories installed simultaneously, SHALL remain gated on decision **M4**; no requirement in this document SHALL assume that rename has occurred.
6. THE name of the Contributor_Package (`cao-contributor`) SHALL be treated as provisional, contingent on decision **M4**, per Requirement 2, Criterion 8.
7. WHERE `cao-supervisor-protocols`, `cao-worker-protocols`, `cao-memory`, `cao-learning`, or `cao-workflow` are discussed as candidates for the Operator_Package, their inclusion or exclusion SHALL remain maintainer-tunable, and this document SHALL NOT assert a final answer beyond the current default recorded in Requirement 1, Criteria 3–4.

**Traceability:** design.md §"Naming and Namespacing", §"Open Decisions Requiring Maintainer Sign-Off"; validates AC6 (decision recorded and applied before any public surface ships) while deliberately not resolving M1–M4 itself, per the given instruction.

---

### Requirement 22: Security and Trust Posture

**User Story:** As an operator, I want CAO to be explicit that installing an agent plugin runs untrusted content, and to enforce the containment and credential postures the specification allows without inventing new trust guarantees it cannot back, so that I understand exactly what protection I do and do not have.

#### Acceptance Criteria

1. THE CLI, API, and Web_Panel SHALL each present a clear statement, at or before the point of install, that installing an agent plugin is equivalent to running untrusted code and content from that plugin's source.
2. CAO SHALL NOT implement a trust model, plugin signing, or provenance verification for agent plugins in this feature; where the specification defers these to a future revision, CAO SHALL inherit that deferral rather than implement its own mechanism.
3. THE containment enforcement described in Requirement 7 SHALL apply uniformly to every plugin, regardless of its declared source or any future trust signal.
4. THE Validator and the Installer SHALL perform no network request to fetch a validation schema at any point during validation, consistent with Requirement 4.
5. IF a plugin's `mcp.json` `env` or `headers` values appear credential-shaped, THEN CAO SHALL warn as described in Requirement 18, Criterion 8, and SHALL NOT silently accept the value as a supported credential-injection mechanism, and SHALL NOT block installation solely on that basis.
6. THE on-disk locations used to store installed plugins and their persistent data SHALL be created with permissions restricting access to the owning user, consistent with CAO's existing home-directory permission convention.
7. THE default network posture for CAO's own API server and any packaged MCP server SHALL remain localhost-only; a user who reconfigures the server host or port to a non-loopback address SHALL be considered to have knowingly left that posture, not CAO to have silently changed it.

**Traceability:** design.md §"Security Considerations", §"Non-Goals" table; validates AC7 (no credentials in package data, localhost-only default posture) in full, combining both the Increment 1 and Increment 2 portions.

---

### Requirement 23: CI Conformance Gates and Canonical Fixture Validation

**User Story:** As a maintainer merging changes to this feature, I want CI to fail automatically whenever a schema drifts, a package drifts, or CAO's own packages fail conformance, so that regressions are caught before merge rather than discovered by an operator.

#### Acceptance Criteria

1. THE continuous integration workflow SHALL run, on every pull request, a step that fails if the vendored Agent Plugins schemas no longer match their recorded hashes, as described in Requirement 4, Criterion 3.
2. THE continuous integration workflow SHALL run, on every pull request, a step that fails if either the Operator_Package or the Contributor_Package drifts from its declared skill allowlist or fails Validator loading with a `FATAL` finding, as described in Requirement 3.
3. THE continuous integration workflow SHALL include a fixture-based conformance corpus containing at least one directory case per row of the failure-isolation behaviors described in Requirements 6, 9, 12, and 14, each asserting the exact expected finding and the specification clause it cites.
4. THE conformance corpus SHALL include the upstream canonical example Agent Plugin as a known-good positive fixture.
5. WHEN the canonical example Agent Plugin is installed via the CLI capability described in Requirement 16, THE Installer SHALL deliver its skill to at least one provider, and any intentionally invalid sibling skill within that fixture SHALL be skipped with a report rather than rejecting the whole fixture.

**Traceability:** design.md §"Testing Strategy" (CI section, conformance corpus); validates AC1 (CI-on-every-PR requirement) and AC4 (canonical example plugin behavior) in full.

## Traceability Matrix

The table below records, for inspection, which requirement(s) validate each correctness property and each #573 acceptance criterion. No property or acceptance criterion from `design.md` is omitted.

| Correctness Property | Validated by |
|---|---|
| P1 — Validation totality | Requirement 5 |
| P2 — Fatality classification | Requirement 6 |
| P3 — Containment | Requirement 7 |
| P4 — Isolation | Requirement 9 |
| P5 — Idempotence | Requirement 10 |
| P6 — Skills-only conformance | Requirement 11 |
| P7 — Sibling independence | Requirement 12 |
| P8 — Projection non-shadowing / deterministic winner | Requirement 14 |
| P9 — Expansion soundness (Increment 2) | Requirement 18 |
| P10 — Cross-provider delivery equivalence | Requirement 13 |
| P11 — Schema pin integrity and offline validation | Requirement 4 |

| #573 Acceptance Criterion | Validated by | Increment status |
|---|---|---|
| AC1 — Schemas validate in CI on every PR | Requirements 3, 4, 23 | Spans both — `plugin.json` half closes in Increment 1 (Req. 4, 23); `mcp.json` half requires Increment 2 (Req. 18, 19) |
| AC2 — Installs and works in ≥2 clients: skill discovered + tools callable | Requirements 1, 13 (skill half); 18, 19 (tools half) | Spans both — Increment 1 alone does NOT close this AC (see Requirement 11, Criterion 6) |
| AC3 — Contributor plugin validates and installs the same way | Requirements 2, 3 | Increment 1 |
| AC4 — `cao plugin add` installs canonical example; failure isolation behaviors | Requirements 5, 6, 9, 12, 23 | Increment 1 |
| AC5 — List/remove via CLI and Web UI; install from GitHub URL | Requirements 8, 16, 17 | Increment 1 |
| AC6 — Naming decision recorded and applied before any public surface ships | Requirements 16, 21 | Increment 1; gates the CLI/API/web surfaces |
| AC7 — Localhost-only, no credentials in package data, containment enforced | Requirements 7, 20, 22 | Increment 1 for containment/localhost/no-credentials-in-manifest; Increment 2 for the `env`/`headers` credential-shape warning (Requirement 18) |

| Maintainer Decision | Referenced by (not resolved by) |
|---|---|
| M1 — CLI verb | Requirement 16 |
| M2 — Extension namespace | Requirement 21 |
| M3 — Docs vocabulary | Requirement 21 |
| M4 — Skill rename + retirement, contributor package name | Requirements 2, 21 |
