import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.chat_agent import _create_chat_agent, generate_chat_response
from src.errors import ChatbotError


class ChatAgentTests(unittest.TestCase):
    def test_agent_receives_history_and_glpi_results(self):
        agent = Mock()
        agent.invoke.return_value = {
            "messages": [
                SimpleNamespace(type="ai", content="Para configurar, siga o artigo.")
            ]
        }

        with (
            patch.dict(os.environ, {"LANGCHAIN_CHAT_MODEL": "provider:model"}),
            patch("src.chat_agent._create_chat_agent", return_value=agent),
        ):
            answer = generate_chat_response(
                "Como configuro a VPN?",
                {"artigos": [{"titulo": "Configurar VPN"}]},
                [{"role": "user", "content": "Preciso de ajuda"}],
            )

        self.assertEqual(answer, "Para configurar, siga o artigo.")
        messages = agent.invoke.call_args.args[0]["messages"]
        self.assertEqual(messages[0], {"role": "user", "content": "Preciso de ajuda"})
        self.assertIn("Como configuro a VPN?", messages[1]["content"])
        self.assertIn("Configurar VPN", messages[1]["content"])

    def test_agent_requires_model_configuration(self):
        with (
            patch.dict(os.environ, {"LANGCHAIN_CHAT_MODEL": ""}),
            self.assertRaises(ChatbotError) as error,
        ):
            generate_chat_response("VPN", {}, [])

        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.codigo, "AGENTE_IA_NAO_CONFIGURADO")

    def test_agent_rejects_invalid_provider_configuration_explicitly(self):
        with self.assertRaises(ChatbotError) as error:
            _create_chat_agent("unsupported_provider:test")

        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.codigo, "AGENTE_IA_INDISPONIVEL")


if __name__ == "__main__":
    unittest.main()
