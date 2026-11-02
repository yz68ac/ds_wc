"""Public typed configuration entry points; no global mutable settings."""
from pathlib import Path
from wxbridge.paths import SOURCE_ROOT
from .models import *
from .loader import load_config, load_account, resolve_instance, prepare_runtime
from .codec import (default_document, account_document, instance_document, effective_document,
                    json_value, configuration_hash)
from .migration import migrate_context_data, migrate_instance_data

PROJECT_ROOT = SOURCE_ROOT or Path.cwd()


def explain_config(config):
    from wxbridge.ai.roles import explain_config as explain
    return explain(config)
