"""Configuração explícita, validada na inicialização."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CODIGO_TURMA = re.compile(r"[A-Z0-9]{1,10}\Z")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    allowed_classes: frozenset[str]
    cors_origins: tuple[str, ...]
    environment: str

    @classmethod
    def from_env(cls) -> "Settings":
        path = Path(os.getenv("DATABASE_PATH", "/data/boletim.db"))
        if not path.is_absolute() or path.name != "boletim.db":
            raise ValueError("DATABASE_PATH deve ser absoluto e terminar em boletim.db")
        classes = frozenset(x.strip().upper() for x in os.getenv(
            "ALLOWED_CLASSES", "8A,8B,8C,9A,9B,9C"
        ).split(","))
        if not classes or any(not CODIGO_TURMA.fullmatch(x) for x in classes):
            raise ValueError("ALLOWED_CLASSES inválido")
        origins = tuple(x.strip().rstrip("/") for x in os.getenv(
            "CORS_ORIGINS", "https://profbt.github.io"
        ).split(","))
        for origin in origins:
            parsed = urlsplit(origin)
            if (not origin or origin == "*" or parsed.path or parsed.query or parsed.fragment
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.scheme not in {"http", "https"}
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})):
                raise ValueError("CORS_ORIGINS requer origens explícitas HTTPS (ou localhost HTTP)")
        environment = os.getenv("ENVIRONMENT", "production").lower()
        if environment not in {"production", "development", "test"}:
            raise ValueError("ENVIRONMENT inválido")
        return cls(path, classes, origins, environment)
