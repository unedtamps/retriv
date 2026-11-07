"""The provider presets: base URLs an admin should not have to know.

Each preset is an OpenAI-compatible endpoint whose address, default model
and key provenance the server owns. The admin picks a preset and supplies
an API key; only the ``custom`` entry asks for a base URL, and it exists
so a preset lagging behind a real gateway never blocks anyone.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinProvider:
    label: str
    base_url: str
    default_model: str
    key_hint: str
    session_header: str | None = None


BUILTIN_PROVIDERS: dict[str, BuiltinProvider] = {
    "opencode-go": BuiltinProvider(
        label="OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        default_model="deepseek-v4.1-flash",
        key_hint="Create a subscription key at opencode.ai/auth.",
        session_header="x-opencode-session",
    ),
    "deepseek": BuiltinProvider(
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        key_hint="Create an API key at platform.deepseek.com.",
    ),
}

CUSTOM_PROVIDER = "custom"


def session_header_for(provider: str) -> str | None:
    """The per-conversation header this provider wants, if it names one.

    OpenCode Go rejects requests that do not carry one stable session id
    per conversation -- the id is how it routes and keeps its prompt cache
    warm. Presets without such a rule return None.
    """
    preset = BUILTIN_PROVIDERS.get(provider)
    return preset.session_header if preset is not None else None


def resolve_base_url(provider: str, base_url: str | None) -> str:
    """The endpoint a configuration means, given the provider choice.

    A preset owns its URL outright: one that was typed alongside it is
    ignored rather than half-honored. ``custom`` is the only provider that
    reads the typed value, and it has nothing to fall back to.
    """
    preset = BUILTIN_PROVIDERS.get(provider)
    if preset is not None:
        return preset.base_url
    if provider != CUSTOM_PROVIDER:
        raise ValueError(f"Unknown provider: {provider}")
    if base_url is None or not base_url.strip():
        raise ValueError("A custom provider needs a base URL.")
    normalized = base_url.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("Base URL must start with http:// or https://")
    return normalized
