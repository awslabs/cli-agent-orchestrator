# 实施计划：新增 OMP CLI Provider

需求 d7348a1e《扩展 CLI 支持 - 新增 OMP CLI 对接》— 在 cli-agent-orchestrator (CAO) 中新增 `omp_cli` provider，沿用现有 provider 适配器模式，使 Agent 能通过 `omp` CLI 执行操作。

## 1. 现状与约定（已确认）

- Provider 是 CLI 工具适配器，统一继承 `BaseProvider`（`providers/base.py`），负责：initialize / get_status / extract_last_message_from_script / exit_cli / cleanup。
- `ProviderType` 枚举（`models/provider.py`）是单一真相源：`constants.PROVIDERS = [p.value for p in ProviderType]` 自动派生，`install_service` 的 `valid_providers` 也自动派生 → 新增枚举即多处生效。
- manager.py 用 `if/elif provider_type == ProviderType.X.value` 分支构造 provider。
- install_service.py：`context_file` 对所有 provider 都写入；if/elif 分支只额外写 provider 专属配置（q/kiro/copilot/opencode）。无分支的 provider（claude_code/codex/gemini/kimi/hermes/cursor）走「仅 context file」路径，`agent_file=None`。→ 新增 provider 默认即可安装。
- terminal_service.py 有两个能力集白名单：`RUNTIME_SKILL_PROMPT_PROVIDERS`（启动时注入 skill catalog）、`SOFT_ENFORCEMENT_PROVIDERS`（无原生工具拦截，仅提示级）。
- tool_mapping.py 的 `TOOL_MAPPING` 仅覆盖有原生工具名的 provider；缺省 → `get_disallowed_tools` 返回空（宽松）。
- api/main.py `list_providers_endpoint` 有硬编码的 `provider_binaries` 映射，需手动补。
- launch.py `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` 硬编码集合，需手动补。
- 状态识别：hermes 用 env-overridable 正则（`CAO_HERMES_*_REGEX`）便于校准，是「输出格式未知」场景的最佳范式。opencode/cursor 是 TUI 代表实现。
- 文档约定：`docs/<provider>-cli.md` 每家一份。
- 测试：`test/providers/test_<provider>_unit.py` + `test/providers/test_provider_manager_unit.py`（注册断言）+ `test/e2e/conftest.py` 的 `require_<provider>()`（缺二进制则 skip）。

## 2. 变更文件列表

| 文件 | 类型 | 改动 |
|---|---|---|
| `src/cli_agent_orchestrator/models/provider.py` | 修改 | `ProviderType` 新增 `OMP_CLI = "omp_cli"`。自动生效到 PROVIDERS / valid_providers。 |
| `src/cli_agent_orchestrator/providers/omp_cli.py` | 新建 | `OmpCliProvider(BaseProvider)`，参照 hermes（env-overridable 正则）+ opencode（TUI 生命周期）。命令名 `omp`，`shutil.which` 探测。实现 initialize/_build_launch_command/get_status/extract_last_message_from_script/exit_cli/cleanup/get_idle_pattern_for_log/paste_enter_count。 |
| `src/cli_agent_orchestrator/providers/manager.py` | 修改 | import + 新增 `elif provider_type == ProviderType.OMP_CLI.value:` 分支，构造 `OmpCliProvider(tid, sess, win, agent_profile, allowed_tools, model=model, skill_prompt=skill_prompt)`。 |
| `src/cli_agent_orchestrator/services/install_service.py` | 修改(最小) | 默认走 context-file-only 即可安装；新增显式 `elif provider == ProviderType.OMP_CLI.value:` 占位分支（说明：OMP 暂无原生 agent 配置格式，依赖 context file），便于后续扩展。 |
| `src/cli_agent_orchestrator/services/terminal_service.py` | 修改 | 能力集决策：①`RUNTIME_SKILL_PROMPT_PROVIDERS` 暂不纳入（OMP 经 context file 注入角色描述）；②`SOFT_ENFORCEMENT_PROVIDERS` 暂不纳入（待 develop 阶段确认 OMP 是否有原生工具拦截后再定）。新增注释说明。 |
| `src/cli_agent_orchestrator/utils/tool_mapping.py` | 修改(最小) | 暂不加 `omp_cli` 映射（原生工具名为未知 → 宽松）。develop 阶段若识别到原生工具名再补。 |
| `src/cli_agent_orchestrator/api/main.py` | 修改 | `provider_binaries` 映射补 `"omp_cli": "omp"`。 |
| `src/cli_agent_orchestrator/cli/commands/launch.py` | 修改 | `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` 补 `"omp_cli"`；按需补 per-provider 提示分支（若 OMP 需特殊权限跳过）。 |
| `test/providers/test_omp_cli_unit.py` | 新建 | 单测：get_status 各态、extract、launch 命令、exit_cli、paste_enter_count。参照 test_opencode_cli_unit.py / test_cursor_cli_unit.py，用 fixture + env-overridable 正则便于校准。 |
| `test/providers/test_provider_manager_unit.py` | 修改 | 新增 `test_create_provider_omp_cli_stores_mapping`，断言 manager 能构造并存储 omp_cli。 |
| `test/e2e/conftest.py` | 修改 | 新增 `require_omp()`（`_cli_available("omp")`，缺则 skip）。 |
| `docs/omp-cli.md` | 新建 | per-provider 文档，说明安装/启动/状态识别/已知限制。 |

## 3. 实施步骤（顺序）

1. 枚举 + provider 类骨架（models/provider.py + providers/omp_cli.py）→ 让导入链先通。
2. manager.py 注册分支 → provider 可被 create。
3. api/main.py + launch.py 接入 → 列表/启动链路通。
4. install_service / terminal_service / tool_mapping 的能力集与占位分支。
5. 单测（unit + manager 注册），先以 hermes-style 默认正则跑通，标 TODO 等真实输出校准。
6. e2e conftest `require_omp` + 文档。
7. develop 阶段用真实 `omp` 输出校准正则与提取逻辑（env-overridable 设计使校准无需改代码常量即可在测试中调）。

## 4. 关键设计决策

- **状态识别用 env-overridable 正则**（`CAO_OMP_IDLE_PROMPT_REGEX` 等）：OMP TUI 输出格式未知，遵循 hermes 范式，develop/test 阶段用真实输出校准而不必反复改源码常量。
- **命令名 `omp` + which 探测**：避免手抖；参照 cursor 的 binary 解析思路。
- **install/工具拦截走默认宽松**：context-file-only + 无 tool_mapping → 与 claude_code 早期待遇一致，风险最低；真实能力在 develop 阶段补强。
- **不改动任何现有 provider 行为**：纯新增。

## 5. 风险评估

| 风险 | 等级 | 缓解 |
|---|---|---|
| OMP TUI 真实输出格式未知 → get_status / extract 误判 | 中 | env-overridable 正则 + develop 阶段真实输出校准；单测先以占位 fixture 跑通。 |
| `omp` 命令名与他工具冲突 | 低 | `shutil.which` + 可选 `--version` 探测（参照 cursor）。 |
| 原生工具词汇未知 → 工具限制不生效（仅提示级） | 低 | 默认宽松；SOFT_ENFORCEMENT 待确认；文档明示限制。 |
| OMP 是否需原生 agent 配置未知 | 低 | install 默认 context-file-only；占位分支预留扩展。 |
| 影响现有 provider | 低 | 纯新增，无现有逻辑改动。 |

## 6. 验收 / 测试策略

- 单测：`test/providers/test_omp_cli_unit.py` 覆盖 status 四态 + extract + 命令构造。
- 注册：`test_provider_manager_unit.py` 断言 omp_cli 可被 manager 构造。
- e2e：`require_omp()` 守护，无 omp 二进制环境自动 skip；有则跑一条 send→完成 提取链路。
- plan→develop handoff：develop 阶段必须用真实 `omp` 输出回填正则与 fixture。

复杂度评估：simple（纯新增适配器，沿用成熟模式，无架构改动）。
