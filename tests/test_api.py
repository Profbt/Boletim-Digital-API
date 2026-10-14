import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import criar_app, gerar_pin

ABRIR = "/api/v1/boletins/abrir"
SALVAR = "/api/v1/boletins/salvar"
EXEMPLO = {"disciplinas": [{"disciplina": "Matemática", "tri1": 7.5, "tri2": 8.0,
                            "tri3": None, "faltas": [2, 1, 0]}]}


def app_para(path: Path, classes=frozenset({"8A", "8B", "8C", "9A", "9B", "9C"})):
    return criar_app(Settings(path / "boletim.db", classes, ("https://profbt.github.io",), "test"))


@pytest.fixture
def client(tmp_path):
    with TestClient(app_para(tmp_path)) as api:
        yield api


def acesso(turma="8C", numero=17, pin=None):
    return {"turma": turma, "numero_chamada": numero,
            "pin": gerar_pin(numero) if pin is None and isinstance(numero, int) else pin}


@pytest.mark.parametrize("numero,pin", [(1, "202601"), (7, "202607"), (17, "202617"), (50, "202650")])
def test_pin_e_criacao(client, numero, pin):
    resposta = client.post(ABRIR, json=acesso(numero=numero, pin=pin))
    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True, "turma": "8C", "numero_chamada": numero,
                              "novo": True, "dados": None}
    assert "pin" not in resposta.text.lower()


@pytest.mark.parametrize("mudanca", [
    {"pin": "202616"}, {"pin": "123456"}, {"turma": "8Z"},
    {"numero_chamada": 0}, {"numero_chamada": 51},
    {"numero_chamada": 17.5}, {"numero_chamada": "17"},
    {"pin": 202617}, {"turma": 8},
])
def test_acesso_invalido_uniforme_sem_registro(client, tmp_path, mudanca):
    entrada = acesso()
    entrada.update(mudanca)
    resposta = client.post(ABRIR, json=entrada)
    assert resposta.status_code == 401
    assert resposta.json() == {"ok": False, "erro": "Dados de acesso inválidos."}
    with sqlite3.connect(tmp_path / "boletim.db") as conn:
        assert conn.execute("SELECT count(*) FROM boletins").fetchone()[0] == 0


def test_turma_normalizada_e_configuravel(tmp_path):
    with TestClient(app_para(tmp_path, frozenset({"8C", "9Z"}))) as client:
        resp = client.post(ABRIR, json=acesso(turma=" 9z "))
        assert resp.status_code == 200
        assert resp.json()["turma"] == "9Z"
        assert client.post(ABRIR, json=acesso(turma="9A")).status_code == 401


def test_persistencia_e_isolamento(client):
    assert client.get("/health").json() == {"ok": True, "service": "boletim-digital-api"}
    assert client.post(ABRIR, json=acesso()).json()["novo"] is True
    assert client.put(SALVAR, json={**acesso(), "dados": EXEMPLO}).json() == {
        "ok": True, "mensagem": "Dados salvos com sucesso."}
    resposta = client.post(ABRIR, json=acesso()).json()
    assert resposta["novo"] is False
    assert resposta["dados"] == EXEMPLO
    assert client.post(ABRIR, json=acesso(numero=18)).json()["dados"] is None
    assert client.post(ABRIR, json=acesso(turma="9A")).json()["dados"] is None
    assert client.post(ABRIR, json=acesso(numero=18)).json()["novo"] is False


def test_reinicio_com_mesmo_volume(tmp_path):
    with TestClient(app_para(tmp_path)) as client:
        assert client.put(SALVAR, json={**acesso(), "dados": EXEMPLO}).status_code == 200
    assert (tmp_path / "boletim.db").exists()
    with TestClient(app_para(tmp_path)) as outro_processo:
        resposta = outro_processo.post(ABRIR, json=acesso()).json()
        assert resposta["dados"] == EXEMPLO
        assert resposta["novo"] is False
    with sqlite3.connect(tmp_path / "boletim.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        colunas = {x[1] for x in conn.execute("PRAGMA table_info(boletins)")}
        assert colunas == {"id", "turma", "numero_chamada", "dados_json", "created_at", "updated_at"}
        assert conn.execute("SELECT count(*) FROM boletins").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO boletins (turma, numero_chamada, created_at, updated_at) "
                         "VALUES ('8C', 17, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO boletins (turma, numero_chamada, created_at, updated_at) "
                         "VALUES ('8C', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")


@pytest.mark.parametrize("dados", [
    {"nome": "Aluno"}, {"disciplinas": [{"email": "x@y.com"}]},
    {"disciplinas": list(range(501))}, {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}},
    {"x": "a" * 2049},
])
def test_dados_invalidos_nao_substituem(client, dados):
    assert client.put(SALVAR, json={**acesso(), "dados": EXEMPLO}).status_code == 200
    resp = client.put(SALVAR, json={**acesso(), "dados": dados})
    assert resp.status_code == 422
    assert client.post(ABRIR, json=acesso()).json()["dados"] == EXEMPLO


def test_tamanho_e_json_malformado(client):
    resp = client.put(SALVAR, json={**acesso(), "dados": {"x": "a" * 70000}})
    assert resp.status_code == 413
    resp = client.post(ABRIR, content=b'{"pin":"202617",bad', headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert "202617" not in resp.text


def test_salvar_exige_pin_e_atualiza_timestamp(client, tmp_path):
    negado = client.put(SALVAR, json={**acesso(pin="999999"), "dados": EXEMPLO})
    assert negado.status_code == 401
    assert negado.json() == {"ok": False, "erro": "Dados de acesso inválidos."}
    with sqlite3.connect(tmp_path / "boletim.db") as conn:
        assert conn.execute("SELECT count(*) FROM boletins").fetchone()[0] == 0
    assert client.put(SALVAR, json={**acesso(), "dados": EXEMPLO}).status_code == 200
    with sqlite3.connect(tmp_path / "boletim.db") as conn:
        criado, anterior = conn.execute(
            "SELECT created_at, updated_at FROM boletins WHERE turma='8C' AND numero_chamada=17"
        ).fetchone()
    assert client.put(SALVAR, json={**acesso(), "dados": {"disciplinas": []}}).status_code == 200
    with sqlite3.connect(tmp_path / "boletim.db") as conn:
        criado_depois, atualizado = conn.execute(
            "SELECT created_at, updated_at FROM boletins WHERE turma='8C' AND numero_chamada=17"
        ).fetchone()
    assert criado_depois == criado
    assert atualizado >= anterior
    assert client.post(ABRIR, json=acesso()).json()["dados"] == {"disciplinas": []}


def test_rate_limit_por_ip(client):
    for numero in range(10):
        assert client.post(ABRIR, json=acesso(pin="999999")).status_code == 401
    resp = client.post(ABRIR, json=acesso())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "60"
    assert client.get("/health").status_code == 200


def test_cors_controlado(client):
    headers = {"Origin": "https://profbt.github.io", "Access-Control-Request-Method": "POST"}
    resp = client.options(ABRIR, headers=headers)
    assert resp.headers["access-control-allow-origin"] == "https://profbt.github.io"
    assert resp.headers.get("access-control-allow-credentials") is None
    resp = client.options(ABRIR, headers={**headers, "Origin": "https://malicioso.example"})
    assert resp.headers.get("access-control-allow-origin") is None
    resp = client.post(ABRIR, json=acesso(pin="000000"), headers={"Origin": "https://profbt.github.io"})
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == "https://profbt.github.io"


def test_configuracao_invalida(monkeypatch):
    monkeypatch.setenv("ALLOWED_CLASSES", "8C,???")
    with pytest.raises(ValueError):
        Settings.from_env()
