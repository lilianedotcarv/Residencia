"""Ferramentas LangChain somente de leitura para consultar o GLPI.

As ferramentas são criadas para cada requisição do backend. Dessa forma, o
cliente já contém o token do usuário autenticado e credenciais não aparecem no
schema que é enviado ao modelo. O agente informa apenas o ID necessário para a
consulta e recebe dados do GLPI ou uma mensagem amigável de falha.
"""

import json
import logging
from typing import Any, Callable, Protocol

import requests
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.errors import GLPIConfigError, erro_glpi_por_status

logger = logging.getLogger("glpi_chat_bot")

TICKET_SOLVED_STATUS = 5
TICKET_CLOSED_STATUS = 6
MSG_RESPOSTA_INESPERADA = "O GLPI retornou uma resposta inesperada. Tente novamente."


class ContextoGLPI(Protocol):
    """Contexto de uma requisição autenticada, fornecido pelo backend."""

    client: Any


class BuscarMeusChamadosInput(BaseModel):
    """Argumentos permitidos para a consulta de chamados ativos."""

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(
        ge=1,
        description=(
            "ID numérico do usuário autenticado no GLPI. Esse valor deve vir do "
            "contexto confiável do backend, nunca da mensagem do usuário."
        ),
    )


class ConsultarStatusChamadoInput(BaseModel):
    """Argumentos permitidos para a consulta de status de um chamado."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: int = Field(ge=1, description="Número (ID) do chamado no GLPI.")


def _mensagem_erro_rede(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return "O GLPI demorou demais para responder. Tente novamente em instantes."
    if isinstance(exc, requests.ConnectionError):
        return "Não foi possível conectar ao GLPI. Ele pode estar fora do ar."
    return "Não foi possível acessar o GLPI. Tente novamente em instantes."


def _mensagem_validacao(exc: ValidationError) -> str:
    detalhes = "; ".join(
        f"{'.'.join(str(parte) for parte in erro['loc'])}: {erro['msg']}"
        for erro in exc.errors()
    )
    return f"Argumentos inválidos para a consulta. {detalhes}"


def _validar_resultado(result: Any) -> Any:
    """Extrai dados de uma resposta do cliente e traduz erros HTTP do GLPI."""
    if not isinstance(result, dict):
        raise ToolException(MSG_RESPOSTA_INESPERADA)

    status_code = result.get("status_code")
    if not isinstance(status_code, int):
        raise ToolException(MSG_RESPOSTA_INESPERADA)
    if not 200 <= status_code < 300:
        raise ToolException(erro_glpi_por_status(status_code, result.get("data")).mensagem)
    if "data" not in result:
        raise ToolException(MSG_RESPOSTA_INESPERADA)
    return result["data"]


def _executar_consulta(consulta: Callable[[], Any]) -> Any:
    """Executa leitura do GLPI sem propagar falhas da integração para o agente."""
    try:
        return _validar_resultado(consulta())
    except ToolException:
        raise
    except requests.RequestException as exc:
        logger.error("Falha de comunicação com o GLPI na ferramenta de leitura.", exc_info=exc)
        raise ToolException(_mensagem_erro_rede(exc)) from exc
    except ValueError as exc:
        logger.error("Falha ao consultar o GLPI: %s", exc)
        mensagem = str(exc).lower()
        if "credencial" in mensagem or "glpi_" in mensagem:
            raise ToolException(GLPIConfigError().mensagem) from exc
        raise ToolException(MSG_RESPOSTA_INESPERADA) from exc
    except (KeyError, TypeError) as exc:
        logger.error("Resposta inválida do GLPI na ferramenta de leitura.", exc_info=exc)
        raise ToolException(MSG_RESPOSTA_INESPERADA) from exc


def _status_id(chamado: dict[str, Any]) -> int | None:
    status = chamado.get("status")
    if isinstance(status, dict):
        status = status.get("id")
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _resumo_status(chamado: dict[str, Any]) -> dict[str, Any]:
    status = chamado.get("status")
    if isinstance(status, dict):
        return {"id": _status_id(chamado), "nome": status.get("name")}
    return {"id": _status_id(chamado), "nome": None}


def _resumo_chamado(chamado: dict[str, Any]) -> dict[str, Any]:
    """Mantém somente os campos úteis para uma resposta do agente."""
    return {
        "id": chamado.get("id"),
        "titulo": chamado.get("name") or chamado.get("title"),
        "status": _resumo_status(chamado),
    }


def _chamado_esta_ativo(chamado: dict[str, Any]) -> bool:
    """Considera encerrados os chamados resolvidos ou fechados no GLPI."""
    return _status_id(chamado) not in {TICKET_SOLVED_STATUS, TICKET_CLOSED_STATUS}


def _buscar_meus_chamados_ativos(context: ContextoGLPI, user_id: int) -> str:
    data = _executar_consulta(
        lambda: context.client.tickets_by_user(user_id=user_id, limit=100)
    )
    if not isinstance(data, list) or not all(isinstance(chamado, dict) for chamado in data):
        raise ToolException(MSG_RESPOSTA_INESPERADA)

    chamados_ativos = [
        _resumo_chamado(chamado) for chamado in data if _chamado_esta_ativo(chamado)
    ]
    if not chamados_ativos:
        return f"Nenhum chamado ativo foi encontrado para o usuário #{user_id}."

    return json.dumps(
        {"user_id": user_id, "total": len(chamados_ativos), "chamados": chamados_ativos},
        ensure_ascii=False,
    )


def _consultar_status_chamado(context: ContextoGLPI, ticket_id: int) -> str:
    data = _executar_consulta(lambda: context.client.ticket_detail(ticket_id))
    if not isinstance(data, dict):
        raise ToolException(MSG_RESPOSTA_INESPERADA)

    resumo = _resumo_chamado(data)
    resumo["id"] = resumo["id"] or ticket_id
    return json.dumps(resumo, ensure_ascii=False)


def criar_ferramentas_leitura(context: ContextoGLPI) -> list[BaseTool]:
    """Cria as ferramentas de consulta presas ao cliente da requisição atual.

    O backend deve chamar esta fábrica após autenticar a requisição. O token e a
    URL do GLPI permanecem no ``context`` e não podem ser fornecidos nem alterados
    pelo modelo de linguagem.
    """

    @tool(args_schema=BuscarMeusChamadosInput)
    def buscar_meus_chamados_ativos(user_id: int) -> str:
        """Lista os chamados ainda ativos que foram abertos por um usuário no GLPI.

        Use quando a pergunta for sobre os chamados abertos, pendentes ou em
        atendimento do próprio usuário. Não use para consultar um chamado
        específico: nesse caso, use ``consultar_status_chamado``. Não use esta
        ferramenta para pesquisar artigos ou respostas da base de conhecimento.

        Args:
            user_id: ID numérico do usuário autenticado, recebido do contexto
                confiável do backend.
        """
        return _buscar_meus_chamados_ativos(context, user_id)

    @tool(args_schema=ConsultarStatusChamadoInput)
    def consultar_status_chamado(ticket_id: int) -> str:
        """Consulta o status atual de um chamado específico no GLPI.

        Use somente depois de ter o número do chamado, quando o usuário pedir seu
        status, andamento ou situação. Não invente um status e não use esta
        ferramenta para listar vários chamados: para isso, use
        ``buscar_meus_chamados_ativos``.

        Args:
            ticket_id: Número (ID) do chamado informado pelo usuário.
        """
        return _consultar_status_chamado(context, ticket_id)

    ferramentas = [buscar_meus_chamados_ativos, consultar_status_chamado]
    for ferramenta in ferramentas:
        ferramenta.handle_tool_error = True
        ferramenta.handle_validation_error = _mensagem_validacao
    return ferramentas
