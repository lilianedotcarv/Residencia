# Roteiro para testar o projeto pelo Postman

Este roteiro cobre as rotas HTTP da aplicação, os principais fluxos de uso e
as validações de entrada. Os testes que consultam ou alteram chamados, usuários
e artigos precisam de um GLPI acessível e de uma conta com as permissões
correspondentes. Faça as operações de escrita somente em uma instância de
teste.

## 1. Preparar o ambiente

1. Configure o acesso ao GLPI no ambiente em que o servidor será iniciado:

   ```powershell
   $env:GLPI_BASE_URL = 'http://<host-do-glpi>'
   $env:APP_ENV = 'production'
   $env:DEV_BYPASS_API_VALIDATION = 'false'
   ```

   O projeto também pode carregar as credenciais de integração do arquivo
   `.env`. Não coloque credenciais reais em variáveis compartilhadas do
   Postman, collections exportadas ou capturas de tela.

2. Na raiz do projeto, inicie a API:

   ```powershell
   .\.venv\Scripts\python.exe -B -m uvicorn src.main:app --host 127.0.0.1 --port 8000
   ```

   Mantenha esse terminal aberto. A documentação interativa fica em
   `http://127.0.0.1:8000/docs`; a API local será `http://127.0.0.1:8000`.

3. Crie um Environment no Postman com estas variáveis:

   | Variável | Valor inicial |
   | --- | --- |
   | `base_url` | `http://127.0.0.1:8000` |
   | `access_token` | vazio |
   | `refresh_token` | vazio |
   | `ticket_id` | preencher depois de criar um chamado de teste |
   | `user_id` | preencher com um ID de usuário válido no GLPI |

4. Para requisições com corpo, use **Body → raw → JSON**. O middleware exige
   `Content-Type: application/json`.

## 2. Ordem recomendada

Execute os testes nesta ordem para reaproveitar os tokens e IDs obtidos:

1. Saúde da API.
2. Emissão e renovação de token.
3. Consultas de identidade, usuários, artigos, categorias e chamados.
4. Criação de chamado, acompanhamento, alteração e encerramento.
5. Busca de artigos pelo chat.
6. Identificação de usuário.
7. Testes negativos e de erro.

Cada grupo abaixo informa método, endereço, corpo quando aplicável e resultado
esperado. Para endpoints GLPI, códigos e conteúdo de sucesso podem variar
conforme a versão, permissões e dados disponíveis no servidor remoto.

## 3. Saúde e autenticação

### 3.1 Verificar que a API está no ar

- **GET** `{{base_url}}/health`
- Esperado: **200**, com `{"status":"ok"}`.

### 3.2 Emitir token GLPI

- **POST** `{{base_url}}/api/auth/token`
- Não exige Bearer token da API local.
- Corpo:

  ```json
  {
    "client_id": "<client-id>",
    "client_secret": "<client-secret>",
    "username": "<usuario-glpi>",
    "password": "<senha-glpi>",
    "scope": "api"
  }
  ```

- Esperado: sucesso do GLPI e um `access_token`. Copie também
  `refresh_token`, se retornado, para as variáveis locais do Environment.
- Falha de autenticação ou credenciais inválidas: erro HTTP amigável (em geral
  **401**). Não registre o corpo com segredos no histórico compartilhado.

### 3.3 Renovar token

- **POST** `{{base_url}}/api/auth/refresh`
- Corpo:

  ```json
  {
    "client_id": "<client-id>",
    "client_secret": "<client-secret>",
    "refresh_token": "<refresh-token>",
    "scope": "api"
  }
  ```

- Esperado: sucesso e novo token; atualize as variáveis se o GLPI rotacionar
  os tokens. Um refresh token inválido ou expirado deve retornar erro.

### 3.4 Configurar autenticação nas requisições protegidas

Nas requisições dos grupos seguintes, selecione **Authorization → Bearer
Token** e use `{{access_token}}`. Como verificação negativa, remova o token ou
envie um valor inválido: as rotas protegidas devem rejeitar a requisição
(normalmente **401**).

## 4. Consultas ao GLPI

### 4.1 Consultar usuário autenticado

- **GET** `{{base_url}}/api/me`
- Com Bearer token válido.
- Esperado: **200** e os dados que o GLPI permitir consultar. Um **403** pode
  significar que o token não tem permissão para essa operação; não implica
  necessariamente que ele não possa consultar artigos ou chamados.

### 4.2 Listar chamados com paginação

- **GET** `{{base_url}}/api/tickets?start=0&limit=10`
- Esperado: **200** ou outro sucesso 2xx do GLPI, com a página de chamados.
- Confira também os defaults sem query string: `start=0`, `limit=100`.

### 4.3 Listar chamados de um usuário

- **GET** `{{base_url}}/api/users/{{user_id}}/tickets?limit=20`
- Esperado: **200**, com `user_id`, `total` e `data`.
- A filtragem considera usuários com papel `requester` nos membros da equipe do
  chamado; um usuário sem chamados pode receber uma lista vazia.

### 4.4 Consultar um chamado e sua linha do tempo

- **GET** `{{base_url}}/api/tickets/{{ticket_id}}`
- **GET** `{{base_url}}/api/tickets/{{ticket_id}}/timeline`
- Esperado: resposta do GLPI para um ID existente e acessível.

### 4.5 Listar artigos, categorias e usuários

- **GET** `{{base_url}}/api/articles?start=0&limit=10`
- **GET** `{{base_url}}/api/categories?start=0&limit=10`
- **GET** `{{base_url}}/api/users?start=0&limit=10`
- Esperado: página de dados do GLPI. Confira a paginação passando `start` maior
  que zero e limites menores.

## 5. Operações de escrita em chamados

Use IDs de teste e registre o resultado de cada operação. Não execute esses
passos em produção sem autorização.

### 5.1 Criar chamado

- **POST** `{{base_url}}/api/tickets`
- Corpo:

  ```json
  {
    "name": "[TESTE POSTMAN] Chamado de validação",
    "content": "Chamado temporário criado para validar a integração."
  }
  ```

- Esperado: resposta de criação do GLPI. Salve o ID retornado em `ticket_id`.

### 5.2 Adicionar acompanhamento

- **POST** `{{base_url}}/api/tickets/{{ticket_id}}/followups`
- Corpo:

  ```json
  {
    "content": "Acompanhamento de teste enviado pelo Postman."
  }
  ```

- Esperado: sucesso do GLPI. Confirme o acompanhamento na linha do tempo do
  chamado.

### 5.3 Atualizar chamado

- **PATCH** `{{base_url}}/api/tickets/{{ticket_id}}`
- Corpo (envie um ou ambos os campos):

  ```json
  {
    "name": "[TESTE POSTMAN] Chamado atualizado",
    "content": "Descrição atualizada para validar a integração."
  }
  ```

- Esperado: resposta de sucesso do GLPI. O corpo não pode estar vazio.

### 5.4 Encerrar chamado

- **POST** `{{base_url}}/api/tickets/{{ticket_id}}/approve`
- Como alternativa, confira o alias **POST**
  `{{base_url}}/api/v1/tickets/{{ticket_id}}/approve`.
- Apesar do nome da rota, a implementação atual altera o status do chamado
  para **6 (fechado)**. Não a use para aprovar uma validação de chamado.
- Esperado: resposta do GLPI. Se aplicável, confirme o novo status consultando
  o chamado.

## 6. Busca de artigos pelo chat

### 6.1 Iniciar a sessão (primeira mensagem)

- **POST** `{{base_url}}/api/chat`
- Não envie `Authorization` nesta primeira chamada.
- Corpo:

  ```json
  {
    "message": "<username>"
  }
  ```

- Também é possível informar `"<username> - <email>"`. A API procura o
  username sem diferenciar maiúsculas/minúsculas; se o e-mail for enviado,
  ambos devem corresponder. Sem e-mail, o username precisa ser único em
  `cache_users`.
- Esperado: **200**, com `session_id`, `user` e `session_token`. O token é
  opaco e identifica a sessão na aplicação. Envie-o nas próximas chamadas do
  chat e nas outras rotas protegidas como `Authorization: Bearer <session_token>`.
- O token local não é encaminhado ao GLPI. O servidor usa `GLPI_USERNAME` e
  `GLPI_PASSWORD` (e as credenciais de cliente OAuth do ambiente) para obter o
  JWT usado nas consultas ao GLPI.
- Essa identificação por nome/e-mail não comprova a identidade da pessoa; o
  token fica em memória e deixa de valer quando o servidor reinicia.

### 6.2 Pergunta com resultados prováveis

- **POST** `{{base_url}}/api/chat`
- Bearer token da sessão obtido no passo anterior.
- Corpo:

  ```json
  {
    "message": "Como configurar a VPN no Windows?",
    "history": []
  }
  ```

- Esperado: **200**, com `message`, `pergunta`, `palavras_chave`, `artigos` e
  `total`. `message` contém uma explicação em português redigida pelo agente
  LangChain a partir dos artigos retornados. A variável `LANGCHAIN_CHAT_MODEL`
  e as credenciais do provedor precisam estar configuradas no backend.
  Cada artigo contém `id`, `titulo`, `conteudo`, `pontuacao`,
  `termos_encontrados` e `fonte_api`. A busca retorna até três artigos; se
  nenhum for relevante, `artigos` pode estar vazio e `total` deve ser zero.

### 6.3 Variar a busca

Repita a requisição com uma pergunta sobre outro tema existente no GLPI. Tente
também variações de acentos, maiúsculas/minúsculas, `e-mail` e `wi-fi`.
Compare palavras-chave e artigos retornados. A pesquisa é lexical; não usa um
modelo de IA nem garante resultados para sinônimos.

`history` é aceito e validado, mas não é usado pelo endpoint para alterar a
busca. A memória SQLite também não é conectada automaticamente a esse fluxo.

## 7. Identificar usuário e criar sessão

### 7.1 Identificar por login ou e-mail

- **POST** `{{base_url}}/api/session/identify`
- Corpo:

  ```json
  {
    "identifier": "<login-ou-email-do-glpi>",
    "session_id": "postman-identificacao-01"
  }
  ```

- O campo `session_id` é opcional; se omitido, a API gera um UUID.
- A rota não usa o Bearer enviado pelo Postman para consultar usuários: o
  servidor precisa ter credenciais GLPI configuradas para a chamada.
- Esperado: **200**, com `success`, `message`, `session_id` e dados do usuário.
  Confirme que não são retornadas credenciais.
- Repita com `session_id` ausente e confira que um identificador é criado.
- Um login/e-mail inexistente resulta em **404**. A rota grava os dados dos
  usuários no cache local (`data/glpi_cache.json`); use ambiente de teste.

## 8. Testes de validação e falhas

Execute cada caso e confira o status HTTP e o corpo de erro. Entradas inválidas
normalmente retornam **422**; erros do GLPI podem ser traduzidos para um
formato amigável com `detail` e `codigo`.

| Caso | Requisição/corpo | Esperado |
| --- | --- | --- |
| Sem autenticação | `GET /api/me` sem Authorization | **401** |
| Token inválido | `GET /api/tickets` com Bearer inválido | **401** ou erro de autenticação do GLPI |
| Paginação inicial negativa | `GET /api/tickets?start=-1` | **422** |
| Limite de listagem fora de 1–200 | `GET /api/users?limit=201` | **422** |
| ID de usuário inválido | `GET /api/users/0/tickets` | **422** |
| Limite de tickets por usuário inválido | `GET /api/users/{{user_id}}/tickets?limit=201` | **422** |
| ID de chamado inválido | `GET /api/tickets/0` | **422** |
| Criar chamado sem título | `POST /api/tickets` com `"name": " "` | **422** |
| Criar chamado sem conteúdo | `POST /api/tickets` com `"content": ""` | **422** |
| Followup vazio | `POST /api/tickets/{{ticket_id}}/followups` com `"content": ""` | **422** |
| Atualização sem campos | `PATCH /api/tickets/{{ticket_id}}` com `{}` | **422** |
| Mensagem vazia | `POST /api/chat` com `"message": " "` | **422** |
| Histórico maior que 50 mensagens | `POST /api/chat` com mais de 50 itens em `history` | **422** |
| Papel de histórico inválido | `POST /api/chat` com `"role": "system"` | **422** |
| JSON malformado | corpo raw como `{` e `Content-Type: application/json` | **422** |
| Corpo não JSON | corpo com texto e `Content-Type: text/plain` | **415** |
| Corpo acima de 1 MB | enviar payload maior que 1 MB | **413** |
| Rota inexistente | `GET /rota-que-nao-existe` | **404** amigável |

Para verificar as demais restrições de tamanho, repita com título acima de 255,
conteúdo/acompanhamento acima de 10.000, mensagem acima de 4.000 caracteres ou
identificador acima de 256. Não é necessário colar esses payloads grandes em
registros compartilhados.

## 9. Simulação sem GLPI

O servidor local `tests.postman_demo:app`, descrito em
[README-simulacao.md](./README-simulacao.md), testa somente a rota `/api/chat`
com artigos fictícios e token de demonstração. Ele não comprova integração
real e não simula autenticação, chamados, renovação de token, categorias,
usuários ou identificação. Para validar essas funcionalidades, use a API real
com uma instância GLPI de teste.

## 10. Escopo que não possui endpoint HTTP

O CRUD genérico do cache (`create`, `get`, `list`, `update`, `delete` e
`upsert`) é uma API interna Python, não uma coleção de endpoints REST. O mesmo
vale para leitura/escrita de memória conversacional e importação do cache na
inicialização. Esses componentes são exercitados pelos testes automatizados
do projeto, não diretamente pelo Postman.

## 11. Checklist final

- [ ] API iniciou e `/health` retornou **200**.
- [ ] Token emitido e renovado sem expor credenciais.
- [ ] Rotas protegidas testadas com token válido, ausente e inválido.
- [ ] Consultas de usuário, chamado, timeline, artigo e categoria executadas.
- [ ] Chamado de teste criado, alterado, acompanhado e encerrado, se autorizado.
- [ ] Busca de artigos conferida com mais de uma pergunta.
- [ ] Identificação de usuário conferida com caso encontrado e não encontrado.
- [ ] Casos negativos retornaram os códigos esperados.
- [ ] Chamados temporários removidos/encerrados conforme o procedimento do
  ambiente de teste.
