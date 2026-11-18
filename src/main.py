import hashlib
import asyncio
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from src.config.pydantic import (
    ChatRequest,
    FollowupRequest,
    IdentifiedUserData,
    PasswordTokenRequest,
    PaginationParams,
    RefreshTokenRequest,
    TicketCreateRequest,
    TicketUpdateRequest,
    UserIdentificationRequest,
    UserIdentificationResponse,
    validation_bypassed,
)
from src.config.pydantic.chat import ChatResponse, ChatSessionResponse
from src.config.pydantic.request import PydanticRequestValidationMiddleware
from src.errors import GLPIConfigError, GLPIError, erro_glpi_por_status, register_error_handlers
from src.chat_agent import generate_chat_response
from src.chat_memory import (
    build_session_id,
    chat_history_engine,
    create_chat_memory,
    load_history,
)
from src.crud import GenericCRUD, import_cache
from src.database import AsyncSessionLocal, CACHE_TABLES, engine, init_db
from src.domain_service import DomainService
from src.glpi_client import GLPIClient
from src.knowledge_service import ArticlesFetchError, processar_pergunta
from src.session_store import session_store

logger = logging.getLogger("glpi_chat_bot")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        await init_db()
        await import_cache()
        yield
    finally:
        chat_history_engine.dispose()
        await engine.dispose()


app = FastAPI(title="GLPI Chat Bot API", version="1.0.0", lifespan=lifespan)
app.add_middleware(PydanticRequestValidationMiddleware)
cors_allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
cors_origin_regex = (
    None
    if cors_allowed_origins
    else r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins,
    allow_origin_regex=cors_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
register_error_handlers(app)
bearer_scheme = HTTPBearer(auto_error=False)


@app.exception_handler(RequestValidationError)
async def pydantic_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request
    details = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


@dataclass
class GLPIAuthContext:
    client: GLPIClient
    user: Any
    jwt_claims: dict[str, Any] | None
    initial_chat_session: bool = False
    session_id: str | None = None


def _new_glpi_client() -> GLPIClient:
    return GLPIClient(
        base_url=os.getenv("GLPI_BASE_URL"),
        username=os.getenv("GLPI_USERNAME"),
        password=os.getenv("GLPI_PASSWORD"),
    )


def get_glpi_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> GLPIAuthContext:
    if request.url.path == "/api/chat" and credentials is None:
        context = GLPIAuthContext(
            client=_new_glpi_client(),
            user=None,
            jwt_claims=None,
            initial_chat_session=True,
        )
        request.state.glpi_auth = context
        return context

    if credentials is not None and credentials.scheme.lower() == "bearer":
        session = session_store.get_session_by_token(credentials.credentials)
        if session is not None:
            user = session["user"]
            context = GLPIAuthContext(
                client=_new_glpi_client(),
                user=user,
                jwt_claims={"sub": str(user["id"])},
                session_id=session["session_id"],
            )
            request.state.glpi_auth = context
            return context

    if validation_bypassed():
        context = GLPIAuthContext(
            client=_new_glpi_client(),
            user=None,
            jwt_claims=None,
        )
        request.state.glpi_auth = context
        return context
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Envie um token de sessão válido no cabeçalho Authorization: Bearer.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=401,
        detail="O token de sessão é inválido ou expirou.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _executar_glpi(funcao: Any, *args: Any, **kwargs: Any) -> Any:
    """Executa uma chamada ao GLPI. Erros de rede são tratados pelos handlers globais."""
    try:
        return funcao(*args, **kwargs)
    except ValueError as exc:
        logger.error("Configuração do GLPI incompleta ou inválida: %s", exc)
        raise GLPIConfigError() from exc


def _proxy_response(result: dict[str, Any]) -> Response:
    status_code = result["status_code"]
    if status_code >= 400:
        raise erro_glpi_por_status(status_code, result.get("data"))
    if status_code == 204:
        return Response(status_code=status_code)
    return JSONResponse(status_code=status_code, content=result.get("data"))


def _proxy_token_response(client: GLPIClient, payload: Any) -> Response:
    status_code = client.last_status_code
    if status_code is None:
        raise HTTPException(status_code=502, detail="O GLPI não retornou um status HTTP para o token.")
    if status_code >= 400:
        raise erro_glpi_por_status(status_code, payload, token=True)
    if status_code == 204:
        return Response(status_code=status_code)
    return JSONResponse(status_code=status_code, content=payload)


def _call_glpi(context: GLPIAuthContext, operation: str, *args: Any, **kwargs: Any) -> Response:
    result = _executar_glpi(getattr(context.client, operation), *args, **kwargs)
    return _proxy_response(result)


def _emails_from_user(user: dict[str, Any]) -> set[str]:
    emails: set[str] = set()
    email = user.get("email")
    if isinstance(email, str) and email.strip():
        emails.add(email.strip().casefold())

    raw_emails = user.get("emails")
    extra = user.get("extra")
    if isinstance(extra, dict):
        raw_emails = [raw_emails, extra.get("emails")]
    for entries in raw_emails if isinstance(raw_emails, list) else [raw_emails]:
        if isinstance(entries, list):
            candidates = entries
        else:
            candidates = [entries]
        for candidate in candidates:
            value = candidate.get("email") if isinstance(candidate, dict) else candidate
            if isinstance(value, str) and value.strip():
                emails.add(value.strip().casefold())
    return emails


def _users_from_ticket_cache(tickets: list[Any], username: str) -> list[dict[str, Any]]:
    normalized_username = username.strip().casefold()
    users_by_id: dict[str, dict[str, Any]] = {}
    for ticket in tickets:
        if not isinstance(ticket, dict):
            continue
        extra = ticket.get("extra")
        team = ticket.get("team") or (
            extra.get("team") if isinstance(extra, dict) else []
        )
        if not isinstance(team, list):
            continue
        for member in team:
            if not isinstance(member, dict):
                continue
            member_name = str(member.get("name") or "").strip()
            member_id = member.get("id")
            if (
                not member_name
                or member_name.casefold() != normalized_username
                or member_id is None
                or member.get("type") not in (None, "User")
            ):
                continue
            users_by_id.setdefault(
                str(member_id),
                {
                    "id": member_id,
                    "username": member_name,
                    "realname": member.get("realname"),
                    "firstname": member.get("firstname"),
                    "email": member.get("email"),
                },
            )
    return list(users_by_id.values())


async def _fetch_and_cache_chat_users() -> list[dict[str, Any]]:
    client = _new_glpi_client()
    users = await asyncio.to_thread(_executar_glpi, client.get_all_users)
    if not isinstance(users, list) or any(
        not isinstance(user, dict) or user.get("id") is None for user in users
    ):
        raise GLPIError(
            "O GLPI retornou uma lista de usuários inválida.",
            codigo="GLPI_RESPOSTA_INVALIDA",
        )

    async with AsyncSessionLocal() as db_session:
        crud = GenericCRUD(db_session)
        for user in users:
            await crud.upsert_by_source_key("users", str(user["id"]), user)
        await db_session.commit()

    return users


def _matching_chat_users(
    users: list[Any],
    username: str,
    email: str | None,
) -> list[dict[str, Any]]:
    normalized_username = username.strip().casefold()
    normalized_email = email.strip().casefold() if email else None
    return [
        user
        for user in users
        if isinstance(user, dict)
        and str(user.get("username") or user.get("name") or "").strip().casefold()
        == normalized_username
        and (
            normalized_email is None
            or normalized_email in _emails_from_user(user)
        )
    ]


async def _find_chat_user(username: str, email: str | None = None) -> dict[str, Any]:
    async with AsyncSessionLocal() as db_session:
        result = await db_session.execute(select(CACHE_TABLES["users"].c.data))
        users = result.scalars().all()

    matching_users = _matching_chat_users(users, username, email)
    glpi_error: GLPIConfigError | requests.RequestException | None = None
    if not matching_users:
        try:
            users = await _fetch_and_cache_chat_users()
            matching_users = _matching_chat_users(users, username, email)
        except (GLPIConfigError, requests.RequestException) as exc:
            glpi_error = exc
            logger.warning(
                "Consulta de usuários ao GLPI falhou; tentando o cache de tickets: %s",
                exc,
            )

    if not matching_users and email is None:
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(CACHE_TABLES["tickets"].c.data))
            tickets = result.scalars().all()
        matching_users = _users_from_ticket_cache(tickets, username)

    if not matching_users:
        if glpi_error is not None:
            raise glpi_error
        raise HTTPException(status_code=404, detail="Usuário não encontrado no banco de dados.")
    if len(matching_users) > 1:
        raise HTTPException(status_code=409, detail="Mais de um usuário corresponde aos dados informados.")
    user = matching_users[0]
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="O usuário está inativo.")

    user_id = user.get("id")
    stored_name = user.get("username") or user.get("name")
    if user_id is None or not stored_name:
        raise HTTPException(status_code=502, detail="O registro do usuário no banco está incompleto.")
    return {
        "id": user_id,
        "name": stored_name,
        "realname": user.get("realname"),
        "firstname": user.get("firstname"),
        "email": user.get("email") or email,
        "is_active": True,
    }


@app.post("/api/auth/token")
def issue_token(payload: PasswordTokenRequest) -> Response:
    client = GLPIClient(
        base_url=os.getenv("GLPI_BASE_URL"),
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        username=payload.username,
        password=payload.password,
        scope=payload.scope,
    )
    result = _executar_glpi(client.get_token)
    return _proxy_token_response(client, result)


@app.post("/api/auth/refresh")
def refresh_token(payload: RefreshTokenRequest) -> Response:
    client = GLPIClient(
        base_url=os.getenv("GLPI_BASE_URL"),
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        scope=payload.scope,
    )
    client.refresh_token = payload.refresh_token
    result = _executar_glpi(client.refresh_token_request)
    return _proxy_token_response(client, result)


def _validate_ticket_id(ticket_id: int) -> None:
    if not validation_bypassed() and ticket_id < 1:
        raise HTTPException(status_code=422, detail="O ID do chamado deve ser maior que zero.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me")
def me(context: GLPIAuthContext = Depends(get_glpi_auth_context)) -> Response:
    if context.user is not None:
        return JSONResponse(content=context.user)
    return _call_glpi(context, "me")


@app.get("/api/tickets")
def list_tickets(
    pagination: PaginationParams = Depends(),
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    return _call_glpi(context, "ticket_list", start=pagination.start, limit=pagination.limit)


@app.get("/api/users/{user_id}/tickets")
def tickets_by_user(
    user_id: int,
    limit: int = 100,
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    if user_id < 1:
        raise HTTPException(
            status_code=422,
            detail="O ID do usuário deve ser maior que zero.",
        )

    if not 1 <= limit <= 200:
        raise HTTPException(
            status_code=422,
            detail="O limite deve estar entre 1 e 200.",
        )

    try:
        result = context.client.tickets_by_user(
            user_id=user_id,
            limit=limit,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível consultar os tickets no GLPI.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="O GLPI retornou uma resposta inesperada.",
        ) from exc

    status_code = result["status_code"]
    content = {
        key: value
        for key, value in result.items()
        if key != "status_code"
    }

    return JSONResponse(status_code=status_code, content=content)

@app.get("/api/tickets/{ticket_id}")
def ticket_detail(ticket_id: int, context: GLPIAuthContext = Depends(get_glpi_auth_context)) -> Response:
    _validate_ticket_id(ticket_id)
    return _call_glpi(context, "ticket_detail", ticket_id)


@app.get("/api/tickets/{ticket_id}/timeline")
def ticket_timeline(ticket_id: int, context: GLPIAuthContext = Depends(get_glpi_auth_context)) -> Response:
    _validate_ticket_id(ticket_id)
    return _call_glpi(context, "ticket_timeline", ticket_id)


@app.post("/api/tickets")
def create_ticket(
    payload: TicketCreateRequest,
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    return _call_glpi(context, "create_ticket", payload.name, payload.content)


@app.post("/api/tickets/{ticket_id}/followups")
def add_followup(
    ticket_id: int,
    payload: FollowupRequest,
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    _validate_ticket_id(ticket_id)
    return _call_glpi(context, "add_followup", ticket_id, payload.content)


@app.patch("/api/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    payload: TicketUpdateRequest,
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    _validate_ticket_id(ticket_id)
    fields = payload.model_dump(exclude_unset=True)
    return _call_glpi(context, "update_ticket", ticket_id, **fields)


@app.post("/api/tickets/{ticket_id}/approve")
@app.post("/api/v1/tickets/{ticket_id}/approve")
def approve_ticket(ticket_id: int, context: GLPIAuthContext = Depends(get_glpi_auth_context)) -> Response:
    _validate_ticket_id(ticket_id)
    try:
        return _call_glpi(context, "approve_ticket", ticket_id)
    except GLPIError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.mensagem, "codigo": exc.codigo},
            headers=exc.headers,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível conectar ao GLPI.",
        ) from exc


@app.get("/api/articles")
def list_articles(
    pagination: PaginationParams = Depends(),
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    return _call_glpi(context, "knowledgebase_articles", start=pagination.start, limit=pagination.limit)


@app.get("/api/categories")
def list_categories(
    pagination: PaginationParams = Depends(),
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    return _call_glpi(context, "itil_categories", start=pagination.start, limit=pagination.limit)


@app.get("/api/users")
def list_users(
    pagination: PaginationParams = Depends(),
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> Response:
    return _call_glpi(context, "users", start=pagination.start, limit=pagination.limit)

def _memory_user_claims(context: GLPIAuthContext) -> dict[str, Any]:
    claims = context.jwt_claims
    if isinstance(claims, dict) and str(claims.get("sub") or "").strip():
        return claims
    token = getattr(context.client, "access_token", None) or "anonymous"
    return {"sub": hashlib.sha256(token.encode()).hexdigest()[:32]} 

@app.post("/api/chat", response_model=ChatResponse | ChatSessionResponse)
async def chat(
    payload: ChatRequest,
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> dict[str, Any] | ChatSessionResponse:
    if context.initial_chat_session:
        username, separator, email = payload.message.rpartition(" - ")
        username = username.strip() if separator else payload.message.strip()
        email = email.strip() if separator else None
        if (
            not username
            or (
                separator
                and (
                    not email
                    or "@" not in email
                    or any(character.isspace() for character in email)
                )
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="A primeira mensagem deve ser um username ou estar no formato 'username - email'.",
            )

        user = await _find_chat_user(username, email)
        session_id = payload.session_id or str(uuid.uuid4())
        session_token = secrets.token_urlsafe(32)
        if not session_store.create_session(session_id, user, token=session_token):
            raise HTTPException(status_code=409, detail="O ID de sessão informado já está em uso.")
        return ChatSessionResponse(
            message="Usuário identificado. Use o token retornado nas próximas mensagens desta sessão.",
            session_id=session_id,
            session_token=session_token,
            user=user,
        )

    conversation_id = payload.session_id or context.session_id or str(uuid.uuid4())
    try:
        memory = create_chat_memory(
            build_session_id(_memory_user_claims(context), conversation_id)
        )
        historico = load_history(memory)
        result = await asyncio.to_thread(
            processar_pergunta,
            payload.message,
            context.client,
        )
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="O GLPI demorou para responder à consulta de artigos.") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Não foi possível consultar os artigos no GLPI.") from exc
    except ArticlesFetchError as exc:
        status_code = exc.status_code if exc.status_code in {401, 403, 429} else 502
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
        raise HTTPException(status_code=status_code, detail=str(exc), headers=headers) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        message = await asyncio.to_thread(
            generate_chat_response,
            payload.message,
            result,
            historico,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="O agente de IA demorou para responder.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Não foi possível consultar o agente de IA.") from exc

    memory.save_context({"input": result["pergunta"]}, {"output": message})
    return {
        **result,
        "message": message,
        "session_id": conversation_id,
        "historico": historico,
    }


@app.post("/api/session/identify", response_model=UserIdentificationResponse)
def identify_user(
    request: UserIdentificationRequest,
    context: GLPIAuthContext = Depends(get_glpi_auth_context),
) -> UserIdentificationResponse:
    response = context.client.users(start=0, limit=100)

    if response.get("status_code") != 200:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível consultar os usuários do GLPI.",
        )

    domain_service = DomainService()
    domain_service.update_from_api_response("users", response)
    result = domain_service.identify_user(request.identifier)

    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["message"])

    session_id = request.session_id or str(uuid.uuid4())
    if not session_store.create_session(session_id, result["user"]):
        raise HTTPException(status_code=409, detail="O ID de sessão informado já está em uso.")
    return UserIdentificationResponse(
        message=result["message"],
        session_id=session_id,
        user=IdentifiedUserData(**result["user"]),
    )


def main() -> None:
    uvicorn.run(
        "src.main:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=os.getenv("APP_ENV", "").lower() == "development",
    )


if __name__ == "__main__":
    main()
