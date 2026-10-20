"""Shared reader for the operator's provider-to-model tier table."""

import logging
import re
from typing import Any, Mapping

from cli_agent_orchestrator.constants import MODEL_ID_MAX_LEN, MODEL_ID_RE
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services import settings_service

logger = logging.getLogger(__name__)
TIERS = ("small", "medium", "large")


def valid_model(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MODEL_ID_MAX_LEN
        and re.fullmatch(MODEL_ID_RE, value) is not None
    )


def load_model_tiers(settings: Mapping[str, Any] | None = None) -> dict[str, dict[str, str]]:
    if settings is None:
        try:
            settings = settings_service._load_or_raise()
        except settings_service.SettingsUnreadableError:
            logger.warning("Model tier settings are unreadable")
            return {}
    raw = settings.get("model_tiers", {})
    if not isinstance(raw, dict):
        logger.warning("Invalid model tier table")
        return {}
    result: dict[str, dict[str, str]] = {}
    providers = {p.value for p in ProviderType}
    for provider, entries in raw.items():
        if provider not in providers or not isinstance(entries, dict):
            logger.warning("Ignoring invalid model tier provider entry")
            continue
        result[provider] = {}
        for tier, model in entries.items():
            if tier in TIERS and valid_model(model):
                result[provider][tier] = model
            else:
                logger.warning("Ignoring invalid model tier mapping")
    return result
