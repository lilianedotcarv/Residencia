import json
import os
import unittest
from unittest.mock import patch

from src.main import _new_glpi_client


class MockResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        self.content = json.dumps(body).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self.body


class GLPIServiceAuthTests(unittest.TestCase):
    def test_glpi_queries_get_jwt_using_environment_service_credentials(self):
        environment = patch.dict(
            os.environ,
            {
                "GLPI_BASE_URL": "https://glpi.example",
                "GLPI_CLIENT_ID": "service-client",
                "GLPI_CLIENT_SECRET": "service-secret",
                "GLPI_USERNAME": "service-user",
                "GLPI_PASSWORD": "service-password",
            },
        )
        with environment:
            client = _new_glpi_client()
            with (
                patch(
                    "src.glpi_client.requests.post",
                    return_value=MockResponse(
                        200,
                        {"access_token": "service-glpi-jwt", "expires_in": 3600},
                    ),
                ) as token_request,
                patch(
                    "src.glpi_client.requests.request",
                    return_value=MockResponse(200, []),
                ) as glpi_request,
            ):
                response = client.get_all_users(batch_size=1)

        self.assertEqual(response, [])
        token_payload = token_request.call_args.kwargs["json"]
        self.assertEqual(token_payload["username"], "service-user")
        self.assertEqual(token_payload["password"], "service-password")
        self.assertEqual(glpi_request.call_args.kwargs["method"], "GET")
        self.assertEqual(
            glpi_request.call_args.kwargs["url"],
            "https://glpi.example/api.php/v2/Administration/User",
        )
        self.assertEqual(
            glpi_request.call_args.kwargs["headers"]["Authorization"],
            "Bearer service-glpi-jwt",
        )


if __name__ == "__main__":
    unittest.main()
