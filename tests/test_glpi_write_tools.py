"""Ferramentas de escrita do agente: posse do chamado validada antes de qualquer escrita."""

import unittest

import requests

from src.glpi_write_tools import criar_ferramentas_escrita
from src.main import GLPIAuthContext


def chamado(owner=7, *, status=(5, "Resolvido"), role="requester", tipo="User"):
    data = {
        "id": 5,
        "team": [{"id": owner, "role": role, "type": tipo}, {"id": 99, "role": "assigned", "type": "User"}],
    }
    if status is not None:
        data["status"] = {"id": status[0], "name": status[1]}
    return data


class ClientStub:
    def __init__(self, *, ticket=None, detail_status=200, me=None, write_status=201,
                 detail_error=None, write_error=None):
        self.ticket = ticket if ticket is not None else chamado()
        self.detail_status = detail_status
        self.me_result = me if me is not None else {"status_code": 200, "data": {"id": 7}}
        self.write_status = write_status
        self.detail_error = detail_error
        self.write_error = write_error
        self.calls = []

    def me(self):
        self.calls.append(("me",))
        return self.me_result

    def ticket_detail(self, ticket_id):
        self.calls.append(("ticket_detail", ticket_id))
        if self.detail_error is not None:
            raise self.detail_error
        data = self.ticket if self.detail_status < 400 else {"detail": "segredo-do-glpi"}
        return {"status_code": self.detail_status, "data": data}

    def add_followup(self, ticket_id, content):
        self.calls.append(("add_followup", ticket_id, content))
        if self.write_error is not None:
            raise self.write_error
        return {"status_code": self.write_status, "data": {"detail": "segredo-do-glpi"}}

    def approve_ticket(self, ticket_id):
        self.calls.append(("approve_ticket", ticket_id))
        if self.write_error is not None:
            raise self.write_error
        return {"status_code": self.write_status, "data": {"detail": "segredo-do-glpi"}}

    def writes(self):
        return [call for call in self.calls if call[0] in {"add_followup", "approve_ticket"}]


def contexto(client, *, sub="7", user=None):
    claims = {"sub": sub} if sub is not None else None
    return GLPIAuthContext(client=client, user=user, jwt_claims=claims)


def ferramentas(client, **kwargs):
    return {tool.name: tool for tool in criar_ferramentas_escrita(contexto(client, **kwargs))}


class FerramentasBaseTests(unittest.TestCase):
    def test_exposes_the_two_tools_with_only_business_arguments(self):
        tools = ferramentas(ClientStub())

        self.assertEqual(set(tools), {"adicionar_comentario_chamado", "aprovar_solucao_chamado"})
        self.assertEqual(set(tools["adicionar_comentario_chamado"].args), {"ticket_id", "mensagem"})
        self.assertEqual(set(tools["aprovar_solucao_chamado"].args), {"ticket_id"})

    def test_each_toolset_is_bound_to_its_own_context(self):
        client_a, client_b = ClientStub(), ClientStub(ticket=chamado(owner=8))
        tools_a = ferramentas(client_a, sub="7")
        tools_b = ferramentas(client_b, sub="8")

        tools_a["adicionar_comentario_chamado"].invoke({"ticket_id": 5, "mensagem": "a"})
        tools_b["adicionar_comentario_chamado"].invoke({"ticket_id": 5, "mensagem": "b"})

        self.assertEqual(client_a.writes(), [("add_followup", 5, "a")])
        self.assertEqual(client_b.writes(), [("add_followup", 5, "b")])


class AdicionarComentarioTests(unittest.TestCase):
    def comentar(self, client, **kwargs):
        args = {"ticket_id": 5, "mensagem": "Ainda sem acesso à VPN."}
        args.update(kwargs.pop("args", {}))
        return ferramentas(client, **kwargs)["adicionar_comentario_chamado"].invoke(args)

    def test_owner_adds_comment_after_validating_ownership(self):
        client = ClientStub()

        result = self.comentar(client, args={"mensagem": "  Ainda sem acesso.  "})

        self.assertEqual(result, "Comentário adicionado ao chamado #5.")
        self.assertEqual(client.calls, [("ticket_detail", 5), ("add_followup", 5, "Ainda sem acesso.")])

    def test_other_users_ticket_is_never_written(self):
        client = ClientStub(ticket=chamado(owner=8))

        result = self.comentar(client)

        self.assertIn("Não encontrei o chamado #5", result)
        self.assertEqual(client.writes(), [])

    def test_assigned_technician_is_not_the_owner(self):
        client = ClientStub(ticket=chamado(owner=7, role="assigned"))

        self.comentar(client)

        self.assertEqual(client.writes(), [])

    def test_group_with_same_id_is_not_the_owner(self):
        client = ClientStub(ticket=chamado(owner=7, tipo="Group"))

        self.comentar(client)

        self.assertEqual(client.writes(), [])

    def test_missing_and_foreign_tickets_get_the_same_answer(self):
        foreign = self.comentar(ClientStub(ticket=chamado(owner=8)))
        missing = self.comentar(ClientStub(detail_status=404))
        forbidden = self.comentar(ClientStub(detail_status=403))

        self.assertEqual(foreign, missing)
        self.assertEqual(foreign, forbidden)

    def test_user_resolved_by_backend_takes_priority(self):
        client = ClientStub()

        result = self.comentar(client, sub=None, user={"id": 7})

        self.assertEqual(result, "Comentário adicionado ao chamado #5.")
        self.assertNotIn(("me",), client.calls)

    def test_falls_back_to_me_for_opaque_tokens(self):
        client = ClientStub(me={"status_code": 200, "data": {"id": 7}})

        result = self.comentar(client, sub=None)

        self.assertEqual(result, "Comentário adicionado ao chamado #5.")
        self.assertEqual(client.calls[0], ("me",))

    def test_non_numeric_sub_falls_back_to_me(self):
        client = ClientStub(me={"status_code": 200, "data": {"id": 7}})

        result = self.comentar(client, sub="ana.ribeiro")

        self.assertEqual(result, "Comentário adicionado ao chamado #5.")
        self.assertEqual(client.calls[0], ("me",))

    def test_without_identity_nothing_is_read_or_written(self):
        client = ClientStub(me={"status_code": 403, "data": None})

        result = self.comentar(client, sub=None)

        self.assertIn("Não foi possível identificar o usuário", result)
        self.assertEqual(client.calls, [("me",)])

    def test_invalid_arguments_never_reach_glpi(self):
        invalid = [
            {"ticket_id": 0},
            {"ticket_id": -3},
            {"mensagem": ""},
            {"mensagem": "   "},
            {"mensagem": "x" * 10_001},
            {"user_id": 8},
        ]
        for extra in invalid:
            with self.subTest(extra=extra):
                client = ClientStub()
                result = self.comentar(client, args=extra)

                self.assertTrue(result.startswith("Argumentos inválidos"))
                self.assertEqual(client.calls, [])

    def test_glpi_write_error_is_friendly_and_hides_upstream_detail(self):
        client = ClientStub(write_status=500)

        result = self.comentar(client)

        self.assertIn("O GLPI está com problemas", result)
        self.assertNotIn("segredo-do-glpi", result)

    def test_network_errors_are_translated(self):
        unreachable = self.comentar(ClientStub(detail_error=requests.ConnectionError()))
        slow = self.comentar(ClientStub(write_error=requests.Timeout()))

        self.assertIn("Não foi possível conectar ao GLPI", unreachable)
        self.assertIn("demorou demais", slow)

    def test_missing_credentials_become_a_config_message(self):
        client = ClientStub(detail_error=ValueError("Credenciais ausentes: GLPI_PASSWORD"))

        result = self.comentar(client)

        self.assertIn("não está configurada corretamente", result)
        self.assertNotIn("GLPI_PASSWORD", result)


class AprovarSolucaoTests(unittest.TestCase):
    def aprovar(self, client, **kwargs):
        return ferramentas(client, **kwargs)["aprovar_solucao_chamado"].invoke({"ticket_id": 5})

    def test_owner_approves_a_solved_ticket(self):
        client = ClientStub()

        result = self.aprovar(client)

        self.assertEqual(result, "Solução do chamado #5 aprovada. O chamado foi fechado.")
        self.assertEqual(client.calls, [("ticket_detail", 5), ("approve_ticket", 5)])

    def test_status_as_plain_integer_is_accepted(self):
        client = ClientStub(ticket={**chamado(), "status": 5})

        self.aprovar(client)

        self.assertEqual(client.writes(), [("approve_ticket", 5)])

    def test_other_users_ticket_is_never_approved(self):
        client = ClientStub(ticket=chamado(owner=8))

        result = self.aprovar(client)

        self.assertIn("Não encontrei o chamado #5", result)
        self.assertEqual(client.writes(), [])

    def test_closed_ticket_is_not_patched_again(self):
        client = ClientStub(ticket=chamado(status=(6, "Fechado")))

        result = self.aprovar(client)

        self.assertIn("já está fechado", result)
        self.assertEqual(client.writes(), [])

    def test_ticket_without_pending_solution_is_not_closed(self):
        client = ClientStub(ticket=chamado(status=(2, "Em atendimento")))

        result = self.aprovar(client)

        self.assertIn("não tem uma solução aguardando aprovação", result)
        self.assertIn("Em atendimento", result)
        self.assertEqual(client.writes(), [])

    def test_unknown_status_fails_closed(self):
        client = ClientStub(ticket=chamado(status=None))

        result = self.aprovar(client)

        self.assertIn("Não foi possível confirmar o status", result)
        self.assertEqual(client.writes(), [])

    def test_glpi_error_on_approval_is_friendly(self):
        client = ClientStub(write_status=403)

        result = self.aprovar(client)

        self.assertIn("Você não tem permissão", result)
        self.assertNotIn("segredo-do-glpi", result)

    def test_invalid_ticket_id_never_reaches_glpi(self):
        client = ClientStub()

        result = ferramentas(client)["aprovar_solucao_chamado"].invoke({"ticket_id": 0})

        self.assertTrue(result.startswith("Argumentos inválidos"))
        self.assertEqual(client.calls, [])


class ToolMessageStatusTests(unittest.TestCase):
    def call(self, client, name, args):
        tool_call = {"name": name, "args": args, "id": "call-1", "type": "tool_call"}
        return ferramentas(client)[name].invoke(tool_call)

    def test_success_is_reported_to_the_agent_as_success(self):
        message = self.call(ClientStub(), "aprovar_solucao_chamado", {"ticket_id": 5})

        self.assertEqual(message.status, "success")

    def test_refusals_are_reported_to_the_agent_as_errors(self):
        client = ClientStub(ticket=chamado(owner=8))

        comment = self.call(client, "adicionar_comentario_chamado", {"ticket_id": 5, "mensagem": "oi"})
        approval = self.call(client, "aprovar_solucao_chamado", {"ticket_id": 5})

        self.assertEqual(comment.status, "error")
        self.assertEqual(approval.status, "error")
        self.assertEqual(client.writes(), [])


if __name__ == "__main__":
    unittest.main()
