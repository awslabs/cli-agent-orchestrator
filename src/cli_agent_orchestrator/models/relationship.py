"""Side-effect-free relationship vocabulary shared by parsers and services."""

VALID_TYPES = frozenset({"relates_to", "contradiction", "supersedes"})
VALID_STATUSES = frozenset({"active", "proposal", "rejected", "superseded", "deleted"})
VALID_ORIGINS = frozenset(
    {
        "compiler",
        "wiki_lint",
        "human",
        "legacy_related_keys",
        "external_import",
        "vault",
    }
)
