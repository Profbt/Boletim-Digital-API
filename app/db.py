"""SQLite persistente: uma tabela, chave lógica única, WAL e busy timeout."""
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Boletim(Base):
    __tablename__ = "boletins"
    __table_args__ = (
        UniqueConstraint("turma", "numero_chamada", name="uq_boletim_turma_chamada"),
        CheckConstraint("numero_chamada BETWEEN 1 AND 50", name="ck_chamada_1_50"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turma: Mapped[str] = mapped_column(String(10), nullable=False)
    numero_chamada: Mapped[int] = mapped_column(Integer, nullable=False)
    dados_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


def criar_engine(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})

    @event.listens_for(engine, "connect")
    def configurar_conexao(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # WAL é persistente no arquivo; evitar mudar journal_mode em cada conexão.
    with engine.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
        if mode.lower() != "wal":
            raise RuntimeError("O sistema de arquivos não suporta WAL para este banco")
    Base.metadata.create_all(engine)
    return engine
