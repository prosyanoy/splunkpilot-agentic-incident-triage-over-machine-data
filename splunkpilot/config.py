"""Runtime configuration, read from environment variables with local defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/splunkpilot.db"
    hec_token: str = "dev-hec-token"  # demo default, not a secret
    api_url: str = "http://localhost:8000"
    llm: str = "mock"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        d = cls()
        return cls(
            db_path=e("SPLUNKPILOT_DB", d.db_path),
            hec_token=e("SPLUNKPILOT_HEC_TOKEN", d.hec_token),
            api_url=e("SPLUNKPILOT_URL", d.api_url),
            llm=e("SPLUNKPILOT_LLM", d.llm),
            ollama_url=e("OLLAMA_URL", d.ollama_url),
            ollama_model=e("OLLAMA_MODEL", d.ollama_model),
        )
