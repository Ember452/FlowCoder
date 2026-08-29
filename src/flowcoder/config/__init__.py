"""配置加载、校验与 schema 包。"""

from flowcoder.config.core import (
    AppConfig,
    ConfigError,
    MCPServerConfig,
    MemoryConfig,
    MemoryProviderConfig,
    ProviderConfig,
    WorktreeConfig,
    build_child_env,
    load_config,
    resolve_env_vars,
    update_user_config_value,
)
from flowcoder.config.model_context import (
    DEFAULT_CONTEXT_WINDOW,
    MODEL_CONTEXT_WINDOWS,
    lookup_model_context_window,
)
from flowcoder.config.removed_capabilities import (
    REMOVED_CONFIG_SECTIONS,
    find_removed_config_sections,
)
from flowcoder.config.validator import (
    CURRENT_CONFIG_SCHEMA_VERSION,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    VALID_SANDBOX_MODES,
    VALID_TEAMMATE_MODES,
    validate_memory,
    validate_mcp_servers,
    validate_permission_mode,
    validate_providers,
    validate_sandbox_mode,
    validate_schema_version,
    validate_worktree,
)

__all__ = [
    "AppConfig",
    "ConfigError",
    "CURRENT_CONFIG_SCHEMA_VERSION",
    "DEFAULT_CONTEXT_WINDOW",
    "MCPServerConfig",
    "MODEL_CONTEXT_WINDOWS",
    "MemoryConfig",
    "MemoryProviderConfig",
    "ProviderConfig",
    "REMOVED_CONFIG_SECTIONS",
    "VALID_PERMISSION_MODES",
    "VALID_PROTOCOLS",
    "VALID_SANDBOX_MODES",
    "VALID_TEAMMATE_MODES",
    "WorktreeConfig",
    "build_child_env",
    "find_removed_config_sections",
    "load_config",
    "lookup_model_context_window",
    "resolve_env_vars",
    "update_user_config_value",
    "validate_memory",
    "validate_mcp_servers",
    "validate_permission_mode",
    "validate_providers",
    "validate_sandbox_mode",
    "validate_schema_version",
    "validate_worktree",
]
