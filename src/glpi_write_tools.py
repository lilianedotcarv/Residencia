"""Ferramentas LangChain de escrita no GLPI: comentar e aprovar a solução de um chamado.

O LLM informa apenas os argumentos de negócio (``ticket_id`` e ``mensagem``).
O cliente GLPI e a identidade do usuário vêm do contexto injetado pelo backend
(``GLPIAuthContext``), ficam fechados na fábrica ``criar_ferramentas_escrita`` e
não aparecem no schema das ferramentas, então o modelo não consegue trocá-los.

Antes de qualquer escrita, o chamado é carregado do GLPI com o token do próprio
usuário e só segue se ele for o solicitante (``role == "requester"``) do chamado,
o mesmo critério usado por ``GLPIClient.tickets_by_user``.
"""

import logging
from typing import Any, Callable, Protocol

import requests
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.errors import GLPIConfigError, erro_glpi_por_status
from src.glpi_client import TICKET_CLOSED_STATUS

logger = logging.getLogger("glpi_chat_bot")

TICKET_SOLVED_STATUS = 5

MSG_SEM_IDENTIDADE = (
    "Não foi possível identificar o usuário autenticado. Nenhuma ação foi executada."
)
MSG_RESPOSTA_INESPERADA = "O GLPI retornou uma resposta inesperada. Nenhuma ação foi executada."


class ContextoGLPI(Protocol):
    """Contexto injetado pelo backend (``GLPIAuthContext`` em ``src/main.py``)."""

    client: Any
    user: Any
    jwt_claims: dict[str, Any] | None


class ComentarioChamadoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: int = Field(ge=1, description="Número (ID) do chamado no GLPI.")
    mensagem: str = Field(
        min_length=1,
        max_length=10_000,
        description="Texto do comentário a registrar no chamado.",
    )

    @field_validator("mensagem")
    @classmethod
    def validate_mensagem(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("O comentário não pode ser vazio.")
        return value


class SolucaoChamadoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: int = Field(ge=1, description="Número (ID) do chamado no GLPI.")


def _mensagem_chamado_nao_encontrado(ticket_id: int) -> str:
    # Mesma mensagem para "não existe" e "é de outro usuário": não revela chamados alheios.
    return (
        f"Não encontrei o chamado #{ticket_id} entre os chamados do usuário autenticado. "
        "Nenhuma ação foi executada."
    )


def _mensagem_erro_rede(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return "O GLPI demorou demais para responder. Tente novamente em instantes."
    if isinstance(exc, requests.ConnectionError):
        return "Não foi possível conectar ao GLPI. Ele pode estar fora do ar."
    return "Não foi possível concluir a chamada ao GLPI."


def _como_id(valor: Any) -> str | None:
    texto = str(valor).strip() if valor is not None else ""
    return texto if texto.isdigit() else None


def _id_do_usuario(context: ContextoGLPI) -> str | None:
    """Resolve o ID numérico do usuário autenticado, sem aceitar nada vindo do LLM.

    Ordem: usuário já resolvido pelo backend, claim ``sub`` do JWT e, por último,
    ``/Administration/User/Me`` (cobre tokens opacos). Sem identidade, retorna None.
    """
    if isinstance(context.user, dict):
        user_id = _como_id(context.user.get("id"))
        if user_id:
            return user_id

    if isinstance(context.jwt_claims, dict):
        user_id = _como_id(context.jwt_claims.get("sub"))
        if user_id:
            return user_id

    result = context.client.me()
    status_code = result.get("status_code")
    data = result.get("data")
    if isinstance(status_code, int) and 200 <= status_code < 300 and isinstance(data, dict):
        return _como_id(data.get("id"))
    logger.warning("Não foi possível obter o usuário autenticado (status %s).", status_code)
    return None


def _eh_solicitante(chamado: dict[str, Any], user_id: str) -> bool:
    team = chamado.get("team")
    if not isinstance(team, list):
        return False
    return any(
        isinstance(membro, dict)
        and membro.get("role") == "requester"
        and membro.get("type", "User") == "User"
        and _como_id(membro.get("id")) == user_id
        for membro in team
    )


def _carregar_chamado_do_usuario(context: ContextoGLPI, ticket_id: int) -> dict[str, Any]:
    """Carrega o chamado e garante que ele pertence ao usuário autenticado."""
    user_id = _id_do_usuario(context)
    if user_id is None:
        raise ToolException(MSG_SEM_IDENTIDADE)

    result = context.client.ticket_detail(ticket_id)
    status_code = result.get("status_code")
    if not isinstance(status_code, int):
        raise ToolException(MSG_RESPOSTA_INESPERADA)
    if status_code in (403, 404):
        raise ToolException(_mensagem_chamado_nao_encontrado(ticket_id))
    if status_code >= 400:
        raise ToolException(erro_glpi_por_status(status_code, result.get("data")).mensagem)

    chamado = result.get("data")
    if not isinstance(chamado, dict):
        raise ToolException(MSG_RESPOSTA_INESPERADA)
    if not _eh_solicitante(chamado, user_id):
        logger.warning(
            "Usuário %s tentou agir no chamado %s, que não é dele.", user_id, ticket_id,
        )
        raise ToolException(_mensagem_chamado_nao_encontrado(ticket_id))
    return chamado


def _status_do_chamado(chamado: dict[str, Any]) -> tuple[int | None, str]:
    status = chamado.get("status")
    nome = ""
    if isinstance(status, dict):
        nome = str(status.get("name") or "")
        status = status.get("id")
    try:
        return int(status), nome
    except (TypeError, ValueError):
        return None, nome


def _checar_escrita(result: dict[str, Any]) -> None:
    status_code = result.get("status_code")
    if not isinstance(status_code, int):
        raise ToolException(MSG_RESPOSTA_INESPERADA)
    if status_code >= 400:
        raise ToolException(erro_glpi_por_status(status_code, result.get("data")).mensagem)


def _executar_acao(
    context: ContextoGLPI,
    ticket_id: int,
    acao: Callable[[dict[str, Any]], str],
) -> str:
    """Valida a posse do chamado e só então executa a escrita, traduzindo os erros."""
    try:
        chamado = _carregar_chamado_do_usuario(context, ticket_id)
        return acao(chamado)
    except ToolException:
        raise
    except requests.RequestException as exc:
        logger.error("Falha de comunicação com o GLPI na ferramenta de escrita.", exc_info=exc)
        raise ToolException(_mensagem_erro_rede(exc)) from exc
    except ValueError as exc:
        logger.error("Configuração do GLPI incompleta ou inválida: %s", exc)
        raise ToolException(GLPIConfigError().mensagem) from exc


def _adicionar_comentario(context: ContextoGLPI, ticket_id: int, mensagem: str) -> str:
    def acao(_: dict[str, Any]) -> str:
        _checar_escrita(context.client.add_followup(ticket_id, mensagem.strip()))
        logger.info("Comentário adicionado ao chamado %s pela ferramenta do agente.", ticket_id)
        return f"Comentário adicionado ao chamado #{ticket_id}."

    return _executar_acao(context, ticket_id, acao)


def _aprovar_solucao(context: ContextoGLPI, ticket_id: int) -> str:
    def acao(chamado: dict[str, Any]) -> str:
        status_id, nome = _status_do_chamado(chamado)
        if status_id is None:
            raise ToolException(
                f"Não foi possível confirmar o status do chamado #{ticket_id}. "
                "Nenhuma ação foi executada."
            )
        if status_id == TICKET_CLOSED_STATUS:
            raise ToolException(
                f"O chamado #{ticket_id} já está fechado, não há solução pendente de aprovação."
            )
        if status_id != TICKET_SOLVED_STATUS:
            atual = f" (status atual: {nome})" if nome else ""
            raise ToolException(
                f"O chamado #{ticket_id} não tem uma solução aguardando aprovação{atual}. "
                "Nenhuma ação foi executada."
            )
        _checar_escrita(context.client.approve_ticket(ticket_id))
        logger.info("Solução do chamado %s aprovada pela ferramenta do agente.", ticket_id)
        return f"Solução do chamado #{ticket_id} aprovada. O chamado foi fechado."

    return _executar_acao(context, ticket_id, acao)


def _mensagem_validacao(exc: ValidationError) -> str:
    detalhes = "; ".join(
        f"{'.'.join(str(parte) for parte in erro['loc'])}: {erro['msg']}"
        for erro in exc.errors()
    )
    return f"Argumentos inválidos. Nenhuma ação foi executada. {detalhes}"


def criar_ferramentas_escrita(context: ContextoGLPI) -> list[StructuredTool]:
    """Cria as ferramentas de escrita presas ao contexto da requisição atual.

    Deve ser chamada uma vez por requisição, com o ``GLPIAuthContext`` já resolvido
    pelo backend: o token e o usuário nunca passam pelo LLM.
    """

    def adicionar_comentario_chamado(ticket_id: int, mensagem: str) -> str:
        return _adicionar_comentario(context, ticket_id, mensagem)

    def aprovar_solucao_chamado(ticket_id: int) -> str:
        return _aprovar_solucao(context, ticket_id)

    return [
        StructuredTool.from_function(
            func=adicionar_comentario_chamado,
            name="adicionar_comentario_chamado",
            description=(
                "Adiciona um comentário (acompanhamento) a um chamado do usuário "
                "autenticado no GLPI. Só funciona para chamados que o usuário abriu. "
                "Use apenas quando o usuário pedir para registrar um comentário."
            ),
            args_schema=ComentarioChamadoInput,
            handle_tool_error=True,
            handle_validation_error=_mensagem_validacao,
        ),
        StructuredTool.from_function(
            func=aprovar_solucao_chamado,
            name="aprovar_solucao_chamado",
            description=(
                "Aprova a solução proposta para um chamado do usuário autenticado, o que "
                "fecha o chamado. Ação definitiva: use SOMENTE quando o usuário pedir "
                "explicitamente para aprovar ou aceitar a solução daquele chamado."
            ),
            args_schema=SolucaoChamadoInput,
            handle_tool_error=True,
            handle_validation_error=_mensagem_validacao,
        ),
    ]
