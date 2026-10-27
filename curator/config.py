import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Lee un archivo .env sencillo (CLAVE=valor) sin pisar variables ya definidas."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    confluence_base_url: str = ""
    confluence_email: str = ""
    confluence_api_token: str = ""
    confluence_space_key: str = ""
    advisor: str = "mock"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "high"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:12b"
    ollama_context: int = 32768
    database_path: str = "data/curator.db"
    candidates: int = 10
    app_user: str = ""
    app_password: str = ""

    @property
    def confluence_configured(self) -> bool:
        return all(
            [self.confluence_base_url, self.confluence_email, self.confluence_api_token, self.confluence_space_key]
        )


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("CURATOR_ENV_FILE", ".env")))
    advisor = os.environ.get("CURATOR_ADVISOR", "").strip().lower()
    if not advisor:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        advisor = "anthropic" if has_key else "mock"
    return Settings(
        confluence_base_url=os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/").removesuffix("/wiki"),
        confluence_email=os.environ.get("CONFLUENCE_EMAIL", ""),
        confluence_api_token=os.environ.get("CONFLUENCE_API_TOKEN", ""),
        confluence_space_key=os.environ.get("CONFLUENCE_SPACE_KEY", ""),
        advisor=advisor,
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", "high"),
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "gemma3:12b"),
        ollama_context=int(os.environ.get("OLLAMA_CONTEXT", "32768")),
        database_path=os.environ.get("CURATOR_DATABASE", "data/curator.db"),
        candidates=int(os.environ.get("CURATOR_CANDIDATES", "10")),
        app_user=os.environ.get("CURATOR_USER", ""),
        app_password=os.environ.get("CURATOR_PASSWORD", ""),
    )
