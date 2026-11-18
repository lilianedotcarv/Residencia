"""Testes das ferramentas LangChain somente de leitura do GLPI."""

import json
import unittest

import requests

from src.glpi_read_tools import criar_ferramentas_leitura
from src.main import GLPIAuthContext


def chamado(ticket_id: int, *, status=(2, "Em atendimento"), titulo="Sem acesso à VPN"):
    return {
        "id": ticket_id,
        "name": titulo,
        "status": {"id": status[0], "name": status[1]} if status is not None else None,
    }


class ClientStub:
    def __init__(
        self,
        *,
        tickets=None,
        tickets_status=200,
        detail=None,
        detail_status=200,
        tickets_error=None,
        detail_error=None,
    ):
        self.tickets = tickets if tickets is not None else [chamado(1), chamado(2, status=(5, "Resolvido"))]
        self.tickets_status = tickets_status
        self.detail = detail if detail is not None else chamado(9, status=(4, "Pendente"))
        self.detail_status = detail_status
        self.tickets_error = tickets_error
        self.detail_error = detail_error
        self.calls = []

    def tickets_by_user(self, *, user_id, limit):
        self.calls.append(("tickets_by_user", user_id, limit))
        if self.tickets_error is not None:
            raise self.tickets_error
        data = self.tickets if self.tickets_status < 400 else {"detail": "segredo-do-glpi"}
        return {"status_code": self.tickets_status, "data": data}

    def ticket_detail(self, ticket_id):
        self.calls.append(("ticket_detail", ticket_id))
        if self.detail_error is not None:
            raise self.detail_error
        data = self.detail if self.detail_status < 400 else {"detail": "segredo-do-glpi"}
        return {"status_code": self.detail_status, "data": data}


def ferramentas(client):
    context = GLPIAuthContext(client=client, user=None, jwt_claims=None)
    return {ferramenta.name: ferramenta for ferramenta in criar_ferramentas_leitura(context)}


class FerramentasLeituraTests(unittest.TestCase):
    def test_exposes_expected_tools_and_only_business_arguments(self):
        tools = ferramentas(ClientStub())

        self.assertEqual(
            set(tools), {"buscar_meus_chamados_ativos", "consultar_status_chamado"}
        )
        self.assertEqual(set(tools["buscar_meus_chamados_ativos"].args), {"user_id"})
        self.assertEqual(set(tools["consultar_status_chamado"].args), {"ticket_id"})

    def test_tool_descriptions_guide_when_each_one_is_used(self):
        tools = ferramentas(ClientStub())

        self.assertIn("chamados", tools["buscar_meus_chamados_ativos"].description)
        self.assertIn("status", tools["consultar_status_chamado"].description)
        self.assertIn("Não invente", tools["consultar_status_chamado"].description)

    def test_active_tickets_excludes_solved_and_closed_tickets(self):
        client = ClientStub(
            tickets=[
                chamado(1, status=(1, "Novo")),
                chamado(2, status=(5, "Resolvido")),
                chamado(3, status=(6, "Fechado")),
                chamado(4, status=(4, "Pendente")),
            ]
        )

        result = ferramentas(client)["buscar_meus_chamados_ativos"].invoke({"user_id": 7})

        self.assertEqual(client.calls, [("tickets_by_user", 7, 100)])
        resposta = json.loads(result)
        self.assertEqual(resposta["user_id"], 7)
        self.assertEqual(resposta["total"], 2)
        self.assertEqual([item["id"] for item in resposta["chamados"]], [1, 4])

    def test_active_tickets_returns_clear_message_when_none_exist(self):
        client = ClientStub(tickets=[chamado(2, status=(5, "Resolvido"))])

        result = ferramentas(client)["buscar_meus_chamados_ativos"].invoke({"user_id": 7})

        self.assertEqual(result, "Nenhum chamado ativo foi encontrado para o usuário #7.")

    def test_status_tool_returns_ticket_summary(self):
        client = ClientStub(detail=chamado(14, status=(2, "Em atendimento"), titulo="E-mail indisponível"))

        result = ferramentas(client)["consultar_status_chamado"].invoke({"ticket_id": 14})

        self.assertEqual(client.calls, [("ticket_detail", 14)])
        self.assertEqual(
            json.loads(result),
            {
                "id": 14,
                "titulo": "E-mail indisponível",
                "status": {"id": 2, "nome": "Em atendimento"},
            },
        )

    def test_invalid_inputs_do_not_reach_glpi(self):
        for name, args in (
            ("buscar_meus_chamados_ativos", {"user_id": 0}),
            ("consultar_status_chamado", {"ticket_id": -1}),
            ("consultar_status_chamado", {"ticket_id": 3, "user_id": 7}),
        ):
            with self.subTest(name=name, args=args):
                client = ClientStub()
                result = ferramentas(client)[name].invoke(args)

                self.assertTrue(result.startswith("Argumentos inválidos"))
                self.assertEqual(client.calls, [])

    def test_network_failures_become_friendly_messages(self):
        unavailable = ferramentas(
            ClientStub(tickets_error=requests.ConnectionError())
        )["buscar_meus_chamados_ativos"].invoke({"user_id": 7})
        slow = ferramentas(
            ClientStub(detail_error=requests.Timeout())
        )["consultar_status_chamado"].invoke({"ticket_id": 9})

        self.assertIn("Não foi possível conectar ao GLPI", unavailable)
        self.assertIn("demorou demais", slow)

    def test_http_error_is_friendly_and_hides_upstream_detail(self):
        result = ferramentas(ClientStub(detail_status=500))["consultar_status_chamado"].invoke(
            {"ticket_id": 9}
        )

        self.assertIn("O GLPI está com problemas", result)
        self.assertNotIn("segredo-do-glpi", result)

    def test_malformed_response_is_a_friendly_error(self):
        result = ferramentas(ClientStub(tickets={"not": "a list"}))["buscar_meus_chamados_ativos"].invoke(
            {"user_id": 7}
        )

        self.assertEqual(result, "O GLPI retornou uma resposta inesperada. Tente novamente.")

    def test_tool_error_has_error_status_for_the_agent(self):
        tool_call = {
            "name": "consultar_status_chamado",
            "args": {"ticket_id": 9},
            "id": "call-1",
            "type": "tool_call",
        }

        message = ferramentas(ClientStub(detail_status=404))["consultar_status_chamado"].invoke(
            tool_call
        )

        self.assertEqual(message.status, "error")


if __name__ == "__main__":
    unittest.main()
