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
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_max_images: int = 5

    @property
    def confluence_configured(self) -> bool:
        return all(
            [self.confluence_base_url, self.confluence_email, self.confluence_api_token, self.confluence_space_key]
        )

    @property
    def jira_configured(self) -> bool:
        return all([self.jira_base_url, self.jira_email, self.jira_api_token])


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("CURATOR_ENV_FILE", ".env")))
    advisor = os.environ.get("CURATOR_ADVISOR", "").strip().lower()
    if not advisor:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        advisor = "anthropic" if has_key else "mock"
    confluence_base_url = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/").removesuffix("/wiki")
    return Settings(
        confluence_base_url=confluence_base_url,
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
        # Jira Cloud suele estar en el mismo sitio y aceptar la misma cuenta que Confluence.
        jira_base_url=os.environ.get("JIRA_BASE_URL", confluence_base_url).rstrip("/"),
        jira_email=os.environ.get("JIRA_EMAIL", os.environ.get("CONFLUENCE_EMAIL", "")),
        jira_api_token=os.environ.get("JIRA_API_TOKEN", os.environ.get("CONFLUENCE_API_TOKEN", "")),
        jira_max_images=int(os.environ.get("JIRA_MAX_IMAGES", "5")),
    )
