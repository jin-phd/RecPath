from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import Mapping, Sequence


DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
OPENAI_MODEL_OPTIONS = (
    "gpt-5.6",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4-mini",
)
DEFAULT_REGION = "IE"
DEFAULT_LANGUAGE = "en"
DEFAULT_MAX_ROUNDS = 5

# Keys can come from these environment variables or root-level keys of the same name in
# .streamlit/secrets.toml, so they never appear in `ps` output or shell history. Streamlit
# copies secrets.toml values into the environment at startup, so secrets.toml wins over a
# variable exported in the shell.
ENV_KEYS = {
    "youtube_api_key": ("YOUTUBE_API_KEY",),
    "openai_api_key": ("OPENAI_API_KEY",),
    "gcp_credentials_path": ("GCP_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS"),
    "gcp_project_id": ("GCP_PROJECT_ID",),
    "openai_model": ("OPENAI_MODEL",),
}


@dataclass(frozen=True)
class RuntimeConfig:
    youtube_api_key: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    gcp_credentials_path: str | None = field(default=None, repr=False)
    gcp_project_id: str | None = None
    openai_model: str = DEFAULT_OPENAI_MODEL
    region: str = DEFAULT_REGION
    language: str = DEFAULT_LANGUAGE
    max_rounds: int = DEFAULT_MAX_ROUNDS


def parse_runtime_config(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Command-line arguments win, then the given environment (app.py merges secrets.toml in)."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--youtube-api-key", dest="youtube_api_key")
    parser.add_argument("--openai-api-key", dest="openai_api_key")
    parser.add_argument("--gcp-credentials-path", dest="gcp_credentials_path")
    parser.add_argument("--gcp-project-id", dest="gcp_project_id")
    parser.add_argument("--openai-model", dest="openai_model")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)

    args, _ = parser.parse_known_args(argv)

    def pick(name: str) -> str | None:
        value = getattr(args, name)
        if value:
            return value
        for env_name in ENV_KEYS[name]:
            if environ.get(env_name):
                return environ[env_name]
        return None

    return RuntimeConfig(
        youtube_api_key=pick("youtube_api_key"),
        openai_api_key=pick("openai_api_key"),
        gcp_credentials_path=pick("gcp_credentials_path"),
        gcp_project_id=pick("gcp_project_id"),
        openai_model=pick("openai_model") or DEFAULT_OPENAI_MODEL,
        max_rounds=max(args.max_rounds, 1),
    )


def missing_config_fields(config: RuntimeConfig) -> list[str]:
    missing: list[str] = []
    if not config.youtube_api_key:
        missing.append("--youtube-api-key")
    if not config.openai_api_key:
        missing.append("--openai-api-key")
    return missing
