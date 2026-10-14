# Boletim Digital — API central (laboratório)

Backend didático para os projetos de Boletim Digital. **Não é um sistema oficial de notas.** O frontend e os projetos dos estudantes não são alterados por este repositório. URL futura planejada: `https://boletim-api.profbt.com.br` (ainda não implantada).

## Arquitetura

Python 3.12, FastAPI/Pydantic, SQLAlchemy 2.1 e SQLite em um único container. `boletins` contém `id`, `turma`, `numero_chamada`, `dados_json` (TEXT, nulo no boletim novo), `created_at`, `updated_at`; há `UNIQUE(turma, numero_chamada)` e `CHECK(numero_chamada BETWEEN 1 AND 50)`. Não armazena PIN nem cadastro de estudantes. Os nomes das disciplinas e seus dados são JSON fornecido pelo frontend, não tabelas ou listas fixas no servidor. O PUT também cria um registro se não houver um, desde que o acesso seja válido.

SQLite usa WAL, `busy_timeout=5000` ms e uma instância/worker Uvicorn; arquivos `boletim.db`, `boletim.db-wal` e `boletim.db-shm` ficam no diretório `/data` montado em volume persistente. Não coloque o banco na camada descartável da imagem. Não compartilhe o mesmo diretório entre servidores. Operações concorrentes pequenas são adequadas ao cenário didático, mas não há coordenação distribuída nem migrações de esquema automáticas além da criação inicial da tabela.

## Endpoints

`GET /health` → HTTP 200: `{"ok":true,"service":"boletim-digital-api"}`. Verifica que o processo responde (não é teste profundo do banco).

`POST /api/v1/boletins/abrir`:

```json
{"turma":"8C","numero_chamada":17,"pin":"202617"}
```

Novo: `{"ok":true,"turma":"8C","numero_chamada":17,"novo":true,"dados":null}`. Existente: o mesmo envelope com `"novo":false` e `"dados":{"disciplinas":[]}` (ou o JSON salvo). O PIN nunca aparece na resposta.

`PUT /api/v1/boletins/salvar`:

```json
{"turma":"8C","numero_chamada":17,"pin":"202617","dados":{"disciplinas":[{"disciplina":"Matemática","tri1":7.5,"tri2":8.0,"tri3":null,"faltas":[2,1,0]}]}}
```

HTTP 200: `{"ok":true,"mensagem":"Dados salvos com sucesso."}`. Credenciais inválidas, turma não permitida, número fora de 1–50, decimal ou texto → HTTP 401 e `{"ok":false,"erro":"Dados de acesso inválidos."}`. Corpo >64 KiB → 413; JSON de dados inválido/absurdo → 422. Excesso de 10 tentativas em 60 s pelo mesmo IP → 429 com `Retry-After: 60`. `/health` não entra no limite. Não enviar conteúdo pessoal no JSON; chaves pessoais comuns são rejeitadas, mas a aplicação não consegue detectar todo dado sensível em texto livre.

O PIN é `f"2026{numero_chamada:02d}"`: 1→`202601`, 7→`202607`, 17→`202617`, 50→`202650`. Turmas são normalizadas (`strip().upper()`), comparadas à lista configurada e podem ser ampliadas sem mudar o código. O JSON preserva componentes/disciplinas variados; limite de 64 KiB no corpo, 8 níveis, 2000 nós, 500 entradas por lista, 100 campos por objeto, 80 caracteres por chave, 2048 por string. Apenas objeto JSON em `dados`; não executa conteúdo recebido.

## Variáveis de ambiente

Copie os valores de `.env.example` para a configuração do ambiente (não commite `.env`):

| Variável | Valor padrão | Uso |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/boletim.db` | Caminho absoluto terminando em `boletim.db`; use diretório persistente. |
| `ALLOWED_CLASSES` | `8A,8B,8C,9A,9B,9C` | Códigos separados por vírgula; sem lista fixa no código. |
| `CORS_ORIGINS` | `https://profbt.github.io` | Origens explícitas, separadas por vírgula, sem `*`; outras origens de projetos Vercel precisam ser adicionadas uma a uma. HTTP só para localhost. |
| `ENVIRONMENT` | `production` | `production`, `development` ou `test`; documentação interativa desabilitada em produção. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` (Uvicorn) | Opcional: IPs/redes **dos proxies confiáveis** que fornecem `X-Forwarded-For`; nunca `*`. |

Não há cookies nem CORS com credenciais. CORS não é autenticação: clientes fora de navegador também podem chamar a API. Não configure regex geral para `*.vercel.app`: inclua explicitamente as origens dos projetos autorizados.

O limitador é em memória por IP, com janela de 60 s e limite de 10 requisições protegidas (inclusive tentativas válidas) por processo; reiniciar zera os contadores. Atrás do proxy Coolify, sem configurar `FORWARDED_ALLOW_IPS` com o IP/rede real do proxy, todos os estudantes podem compartilhar o mesmo IP aparente e atingir o limite juntos. Descubra a origem da conexão na rede privada, configure somente esse proxy confiável e confirme que ele substitui/controla o cabeçalho `X-Forwarded-For`; não confie num header fornecido diretamente pelo cliente. Use uma só réplica e um só worker nesta versão.

## Execução local

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
DATABASE_PATH="$PWD/data/boletim.db" ENVIRONMENT=development .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Em outro terminal: `curl http://127.0.0.1:8000/health`. A aplicação cria `data/boletim.db` ao iniciar. Não precisa da URL pública para testar. Testes e lint: `.venv/bin/python -m pytest -q` e `.venv/bin/ruff check app tests`.

## Docker e Coolify

A imagem `python:3.12.12-slim-bookworm` tem manifestos linux/amd64 e linux/arm64; roda como UID/GID `10001`, na porta interna `8000`, um worker, com `HEALTHCHECK` interno via biblioteca padrão Python. Para testar onde houver Docker autorizado:

```sh
docker build -t boletim-digital-api:local .
docker run --rm -p 127.0.0.1:8000:8000 -v boletim-data:/data boletim-digital-api:local
```

`compose.yaml` é apenas para desenvolvimento e usa `./data:/data`. Para esse bind mount, prepare a pasta no host com permissão de escrita para UID/GID 10001 **antes** de `docker compose up --build`; Docker não corrige automaticamente as permissões de um diretório existente. Em produção prefira volume persistente gerenciado, garantindo permissão de escrita.

No Coolify, futuramente, sem fazer deploy nesta etapa:

1. Criar uma aplicação **Dockerfile** a partir de `Profbt/Boletim-Digital-API`, branch `main`, Dockerfile na raiz, base directory `/` (não selecionar Compose). Um único container/worker, arquitetura da VPS amd64 ou arm64.
2. Porta exposta interna `8000`, processo escutando em `0.0.0.0`. Associar o domínio `https://boletim-api.profbt.com.br` à porta 8000 e habilitar HTTPS no proxy. Não publicar porta direta no host para a internet.
3. Em **Persistent Storage**, adicionar **Volume Mount** com Destination Path `/data`, com persistência entre redeploys. Garantir que UID/GID 10001 consiga escrever. Configurar `DATABASE_PATH=/data/boletim.db`, `ALLOWED_CLASSES`, `CORS_ORIGINS`, `ENVIRONMENT=production` e, somente após identificar o proxy confiável, `FORWARDED_ALLOW_IPS`.
4. O Dockerfile já define `HEALTHCHECK` que acessa `http://127.0.0.1:8000/health` dentro do container; o Coolify usa essa instrução para aplicação Dockerfile. Conferir build, saúde, rede/proxy, permissões do volume e endpoint público após implantar. DNS e certificados HTTPS são tarefas da implantação, não deste repositório.

## Backup e restauração

Com WAL ativo, não copie apenas `boletim.db` enquanto há gravações. Faça backup consistente com a API online do SQLite (no host com acesso ao volume e Python):

```sh
python3 -c 'import sqlite3,sys; origem=sqlite3.connect(sys.argv[1]); destino=sqlite3.connect(sys.argv[2]); origem.backup(destino); destino.close(); origem.close()' /caminho/persistente/boletim.db /caminho/seguro/boletim-backup.db
python3 -m sqlite3 /caminho/seguro/boletim-backup.db 'PRAGMA integrity_check;'
```

O comando usa `sqlite3.Connection.backup()` da biblioteca padrão (não copie o arquivo ativo com `cp`). A CLI `python3 -m sqlite3` está disponível no Python 3.12 para verificar a integridade; em outros ambientes, use um script Python para executar `PRAGMA integrity_check`. Armazene o backup fora do volume, com controle de acesso e retenção apropriada. Teste a restauração: pare a aplicação, preserve uma cópia de segurança do diretório atual, substitua `boletim.db` pelo backup **sem misturar arquivos `-wal`/`-shm` antigos**, ajuste propriedade UID/GID 10001 e reinicie com o mesmo `/data`. Confirme `/health` e a reabertura de um boletim fictício.

## Segurança e limitações

O PIN é previsível e público (`2026` + chamada): **não é autenticação forte**. Mesmo com limite de tentativas, não impede acesso indevido por quem conhece turma/chamada; qualquer pessoa com PIN válido pode ler e sobrescrever aquele boletim. Não utilizar para notas oficiais, informações escolares sigilosas ou dados pessoais reais sem revisão de segurança, autenticação robusta, autorização, auditoria e requisitos legais adequados. Não armazena nome, CGM, e-mail, telefone ou nascimento como colunas; rejeita chaves pessoais comuns no JSON, mas continua sendo responsabilidade do frontend não enviar conteúdo pessoal em campos livres. Não loga PIN ou JSON completo; Uvicorn inicia sem access log. Não há contas, sessões, histórico de alterações, resolução de edições simultâneas (a última gravação prevalece), sincronização distribuída ou migração automática para PostgreSQL.
