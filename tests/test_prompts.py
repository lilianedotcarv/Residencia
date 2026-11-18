import unittest

from src.prompts import (
    NAO_INFORMADO,
    SYSTEM_PROMPT,
    extrair_contexto_usuario,
    montar_prompt,
)


class ExtrairContextoTests(unittest.TestCase):
    def test_extrai_nome_e_email(self):
        ctx = extrair_contexto_usuario({"name": "Ana", "email": "ana@x.com"})
        self.assertEqual(ctx, {"nome": "Ana", "email": "ana@x.com"})

    def test_junta_given_e_family_name(self):
        ctx = extrair_contexto_usuario({"given_name": "Ana", "family_name": "Silva"})
        self.assertEqual(ctx["nome"], "Ana Silva")

    def test_claims_ausentes_usam_nao_informado(self):
        for claims in (None, {}):
            with self.subTest(claims=claims):
                ctx = extrair_contexto_usuario(claims)
                self.assertEqual(ctx["nome"], NAO_INFORMADO)
                self.assertEqual(ctx["email"], NAO_INFORMADO)

    def test_ignora_valores_que_nao_sao_texto(self):
        ctx = extrair_contexto_usuario({"name": 123, "email": ""})
        self.assertEqual(ctx["nome"], NAO_INFORMADO)
        self.assertEqual(ctx["email"], NAO_INFORMADO)


class MontarPromptTests(unittest.TestCase):
    def test_injeta_nome_e_email_no_prompt(self):
        texto = montar_prompt({"name": "Ana", "email": "ana@x.com"}).format(pergunta="oi")
        self.assertIn("Nome: Ana", texto)
        self.assertIn("E-mail: ana@x.com", texto)
        self.assertIn("oi", texto)

    def test_prompt_proibe_inventar_dados(self):
        self.assertIn("Nunca invente dados de chamados", SYSTEM_PROMPT)

    def test_prompt_exige_numero_do_chamado(self):
        self.assertIn("peça o número do chamado", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()