"""Armazenamento didático de boletins, sem cadastro de estudantes."""
import json
import re
import threading
import time
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hmac import compare_digest
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.config import CODIGO_TURMA, Settings
from app.db import Boletim, criar_engine

MAX_BODY = 64 * 1024
PROTECTED = frozenset({"/api/v1/boletins/abrir", "/api/v1/boletins/salvar"})
INVALIDO = {"ok": False, "erro": "Dados de acesso inválidos."}
DADOS_INVALIDOS = {"ok": False, "erro": "Dados inválidos."}
CAMPOS_PESSOAIS = {"nome", "name", "nomecompleto", "cgm", "email", "telefone", "phone", "celular", "datanascimento", "birthdate", "cpf"}


def gerar_pin(numero_chamada: int) -> str:
    return f"2026{numero_chamada:02d}"


class Acesso(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turma: StrictStr
    numero_chamada: StrictInt = Field(ge=1, le=50)
    pin: StrictStr


class Salvamento(Acesso):
    dados: dict[str, Any]


def chave_normalizada(chave: str) -> str:
    sem_acentos = "".join(c for c in unicodedata.normalize("NFKD", chave) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", sem_acentos.casefold())


def validar_dados(valor: dict[str, Any]) -> str:
    """Limita profundidade, elementos, tamanhos e chaves pessoais; serializa uma só vez."""
    total = 0
    pilha = [(valor, 0)]
    while pilha:
        item, profundidade = pilha.pop()
        total += 1
        if total > 2000 or profundidade > 8:
            raise ValueError("estrutura excessiva")
        if isinstance(item, dict):
            if len(item) > 100:
                raise ValueError("objeto excessivo")
            for chave, conteudo in item.items():
                if (not isinstance(chave, str) or len(chave) > 80
                    or chave_normalizada(chave) in CAMPOS_PESSOAIS):
                    raise ValueError("campo inválido")
                pilha.append((conteudo, profundidade + 1))
        elif isinstance(item, list):
            if len(item) > 500:
                raise ValueError("lista excessiva")
            pilha.extend((conteudo, profundidade + 1) for conteudo in item)
        elif not (item is None or isinstance(item, (str, int, float, bool))):
            raise ValueError("tipo inválido")
        elif isinstance(item, str) and len(item) > 2048:
            raise ValueError("texto excessivo")
    try:
        serializado = json.dumps(valor, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("JSON inválido") from exc
    if len(serializado.encode("utf-8")) > MAX_BODY:
        raise ValueError("JSON excessivo")
    return serializado


class Limitador:
    """Janela deslizante por IP, turma e chamada; processo único."""
    def __init__(self):
        self.baldes: dict[tuple[str, str, int], deque[float]] = {}
        self.lock = threading.Lock()

    def permitir(self, chave: tuple[str, str, int]) -> bool:
        agora = time.monotonic()
        with self.lock:
            if len(self.baldes) >= 4096 and chave not in self.baldes:
                self.baldes = {identidade: valores for identidade, valores in self.baldes.items()
                               if valores and agora - valores[-1] < 60}
                if len(self.baldes) >= 4096:
                    return False
            tentativas = self.baldes.setdefault(chave, deque())
            while tentativas and agora - tentativas[0] >= 60:
                tentativas.popleft()
            if len(tentativas) >= 10:
                return False
            tentativas.append(agora)
            return True


def criar_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    limitador = Limitador()

    @asynccontextmanager
    async def lifespan(api: FastAPI):
        api.state.engine = criar_engine(settings.database_path)
        try:
            yield
        finally:
            api.state.engine.dispose()

    api = FastAPI(title="Boletim Digital API", version="1.0.0", lifespan=lifespan,
                  docs_url=None if settings.environment == "production" else "/docs",
                  redoc_url=None, openapi_url=None if settings.environment == "production" else "/openapi.json")
    @api.middleware("http")
    async def proteger_corpo(request: Request, call_next):
        if request.url.path in PROTECTED:
            tamanho = request.headers.get("content-length")
            if tamanho:
                try:
                    if int(tamanho) > MAX_BODY:
                        return JSONResponse(DADOS_INVALIDOS, status_code=413)
                except ValueError:
                    return JSONResponse(DADOS_INVALIDOS, status_code=400)
            corpo = bytearray()
            async for parte in request.stream():
                corpo.extend(parte)
                if len(corpo) > MAX_BODY:
                    return JSONResponse(DADOS_INVALIDOS, status_code=413)
            request._body = bytes(corpo)
        return await call_next(request)

    # Último middleware registrado = camada mais externa (inclusive erros 401/429).
    api.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_credentials=False, allow_methods=["POST", "PUT"],
                       allow_headers=["Content-Type"])

    @api.exception_handler(RequestValidationError)
    async def erro_validacao(request: Request, _exc: RequestValidationError):
        if request.url.path in PROTECTED:
            return JSONResponse(INVALIDO, status_code=401)
        return JSONResponse({"ok": False, "erro": "Requisição inválida."}, status_code=422)

    @api.exception_handler(HTTPException)
    async def erro_acesso(_request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return JSONResponse(INVALIDO, status_code=401)
        if exc.status_code == 429:
            return JSONResponse({"ok": False, "erro": "Muitas tentativas. Aguarde um minuto."},
                                status_code=429, headers=exc.headers)
        return JSONResponse({"ok": False, "erro": "Requisição inválida."}, status_code=exc.status_code)

    def verificar_limite(acesso: Acesso, request: Request) -> None:
        # Só após o parsing/validação Pydantic; nunca usar PIN na chave.
        ip = request.client.host if request.client else "desconhecido"
        chave = (ip, acesso.turma.strip().upper(), acesso.numero_chamada)
        if not limitador.permitir(chave):
            raise HTTPException(status_code=429, headers={"Retry-After": "60"})

    def credencial_valida(acesso: Acesso) -> str:
        turma = acesso.turma.strip().upper()
        if (not CODIGO_TURMA.fullmatch(turma) or turma not in settings.allowed_classes
            or not re.fullmatch(r"[0-9]{6}", acesso.pin)
            or not compare_digest(acesso.pin, gerar_pin(acesso.numero_chamada))):
            raise ValueError("acesso inválido")
        return turma

    def autenticar(acesso: Acesso) -> str:
        try:
            return credencial_valida(acesso)
        except ValueError:
            # Nunca incluir valor do PIN ou a razão específica no retorno/log.
            raise HTTPException(status_code=401) from None

    @api.get("/health")
    def health():
        return {"ok": True, "service": "boletim-digital-api"}

    @api.post("/api/v1/boletins/abrir")
    def abrir(acesso: Acesso, request: Request):
        verificar_limite(acesso, request)
        turma = autenticar(acesso)
        with Session(request.app.state.engine) as sessao:
            comando = insert(Boletim).values(turma=turma, numero_chamada=acesso.numero_chamada)
            resultado = sessao.execute(comando.on_conflict_do_nothing(index_elements=["turma", "numero_chamada"]))
            novo = resultado.rowcount == 1
            sessao.commit()
            boletim = sessao.scalar(select(Boletim).where(Boletim.turma == turma,
                                                           Boletim.numero_chamada == acesso.numero_chamada))
            return {"ok": True, "turma": turma, "numero_chamada": acesso.numero_chamada,
                    "novo": novo, "dados": json.loads(boletim.dados_json) if boletim.dados_json else None}

    @api.put("/api/v1/boletins/salvar")
    def salvar(acesso: Salvamento, request: Request):
        verificar_limite(acesso, request)
        turma = autenticar(acesso)
        try:
            dados_json = validar_dados(acesso.dados)
        except ValueError:
            return JSONResponse(DADOS_INVALIDOS, status_code=422)
        agora = datetime.now(UTC)
        with Session(request.app.state.engine) as sessao:
            comando = insert(Boletim).values(turma=turma, numero_chamada=acesso.numero_chamada,
                                              dados_json=dados_json, created_at=agora, updated_at=agora)
            sessao.execute(comando.on_conflict_do_update(
                index_elements=["turma", "numero_chamada"],
                set_={"dados_json": dados_json, "updated_at": agora}))
            sessao.commit()
        return {"ok": True, "mensagem": "Dados salvos com sucesso."}

    return api


app = criar_app()
