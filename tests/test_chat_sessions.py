import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from src.config.pydantic.chat import ChatRequest
from src.errors import GLPIConfigError
from src.main import GLPIAuthContext, _find_chat_user, chat, get_glpi_auth_context
from src.session_store import session_store


class UserQueryResult:
    def __init__(self, users):
        self.users = users

    def scalars(self):
        return self

    def all(self):
        return self.users


class UserDatabaseSession:
    def __init__(self, users, tickets):
        self.users = users
        self.tickets = tickets

    async def execute(self, statement):
        table_name = statement.selected_columns[0].table.name
        return UserQueryResult(
            self.tickets if table_name == "cache_tickets" else self.users
        )

    async def commit(self):
        pass


@asynccontextmanager
async def user_database_session(users, tickets):
    yield UserDatabaseSession(users, tickets)


def make_request(path, headers=None):
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers or [],
            "server": ("testserver", 80),
            "client": ("testclient", 1),
        }
    )


class ChatSessionTests(unittest.TestCase):
    def setUp(self):
        session_store.clear()
        self.addCleanup(session_store.clear)
        self.users = [
            {
                "id": 7,
                "username": "ana",
                "email": None,
                "extra": {"emails": [{"email": "ana@example.com"}]},
                "is_active": True,
            }
        ]
        self.tickets = []
        self.glpi_client = Mock()
        self.glpi_client.get_all_users.return_value = []
        self.glpi_client_factory = patch(
            "src.main._new_glpi_client",
            return_value=self.glpi_client,
        )
        self.glpi_client_factory.start()
        self.addCleanup(self.glpi_client_factory.stop)

        async def cache_user(domain, source_key, data):
            self.assertEqual(domain, "users")
            for index, user in enumerate(self.users):
                if str(user.get("id")) == source_key:
                    self.users[index] = data
                    return
            self.users.append(data)

        self.cache_upsert = patch(
            "src.main.GenericCRUD.upsert_by_source_key",
            new=AsyncMock(side_effect=cache_user),
        )
        self.cache_upsert.start()
        self.addCleanup(self.cache_upsert.stop)
        self.database = patch(
            "src.main.AsyncSessionLocal",
            side_effect=lambda: user_database_session(self.users, self.tickets),
        )
        self.database.start()
        self.addCleanup(self.database.stop)

    def test_first_chat_message_matches_database_user_and_issues_session_token(self):
        context = GLPIAuthContext(
            client=object(),
            user=None,
            jwt_claims=None,
            initial_chat_session=True,
        )

        response = asyncio.run(
            chat(
                ChatRequest(message="Ana - ANA@EXAMPLE.COM"),
                context,
            )
        )

        self.assertEqual(response.user.id, 7)
        self.assertEqual(response.user.email, "ANA@EXAMPLE.COM")
        self.assertTrue(response.session_token)
        stored_session = session_store.get_session_by_token(response.session_token)
        self.assertEqual(stored_session["session_id"], response.session_id)
        self.assertEqual(stored_session["user"]["id"], 7)
        self.glpi_client.get_all_users.assert_not_called()

    def test_chat_user_cache_miss_fetches_and_persists_glpi_users(self):
        self.users = []
        glpi_user = {
            "id": 9,
            "name": "carla.menezes",
            "realname": "Menezes",
            "firstname": "Carla",
            "email": None,
            "is_active": True,
        }
        self.glpi_client.get_all_users.return_value = [glpi_user]

        user = asyncio.run(_find_chat_user("carla.menezes"))

        self.assertEqual(user["id"], 9)
        self.assertEqual(user["name"], "carla.menezes")
        self.assertEqual(self.users, [glpi_user])
        self.glpi_client.get_all_users.assert_called_once_with()
        self.cache_upsert.new.assert_awaited_once_with("users", "9", glpi_user)

    def test_first_chat_message_accepts_username_without_email(self):
        context = GLPIAuthContext(
            client=object(),
            user=None,
            jwt_claims=None,
            initial_chat_session=True,
        )

        response = asyncio.run(chat(ChatRequest(message="ANA"), context))

        self.assertEqual(response.user.id, 7)
        self.assertIsNone(response.user.email)
        self.assertIsNotNone(session_store.get_session_by_token(response.session_token))

    def test_username_only_rejects_ambiguous_matches(self):
        self.users.append(
            {
                "id": 8,
                "username": "ana",
                "email": "ana2@example.com",
                "is_active": True,
            }
        )
        context = GLPIAuthContext(
            client=object(),
            user=None,
            jwt_claims=None,
            initial_chat_session=True,
        )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(chat(ChatRequest(message="ana"), context))

        self.assertEqual(error.exception.status_code, 409)

    def test_username_in_ticket_team_can_identify_user_missing_from_users_cache(self):
        self.users = []
        self.tickets = [
            {
                "id": 1,
                "extra": {
                    "team": [
                        {
                            "role": "requester",
                            "id": 9,
                            "name": "carla.menezes",
                            "realname": "Menezes",
                            "firstname": "Carla",
                            "type": "User",
                        }
                    ]
                },
            }
        ]
        context = GLPIAuthContext(
            client=object(),
            user=None,
            jwt_claims=None,
            initial_chat_session=True,
        )

        response = asyncio.run(
            chat(ChatRequest(message="carla.menezes"), context)
        )

        self.assertEqual(response.user.id, 9)
        self.assertEqual(response.user.name, "carla.menezes")
        self.assertIsNotNone(session_store.get_session_by_token(response.session_token))

    def test_ticket_cache_is_used_when_glpi_service_configuration_is_missing(self):
        self.users = []
        self.tickets = [
            {
                "id": 1,
                "extra": {
                    "team": [
                        {
                            "id": 9,
                            "name": "carla.menezes",
                            "type": "User",
                        }
                    ]
                },
            }
        ]
        self.glpi_client.get_all_users.side_effect = GLPIConfigError()

        user = asyncio.run(_find_chat_user("carla.menezes"))

        self.assertEqual(user["id"], 9)
        self.assertEqual(user["name"], "carla.menezes")

    def test_glpi_configuration_error_is_preserved_without_ticket_cache_match(self):
        self.users = []
        self.glpi_client.get_all_users.side_effect = GLPIConfigError()

        with self.assertRaises(GLPIConfigError):
            asyncio.run(_find_chat_user("missing.user"))

    def test_email_is_required_when_identity_separator_is_present(self):
        context = GLPIAuthContext(
            client=object(),
            user=None,
            jwt_claims=None,
            initial_chat_session=True,
        )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(chat(ChatRequest(message="ana - "), context))

        self.assertEqual(error.exception.status_code, 400)

    def test_chat_without_bearer_is_marked_for_identification_only_on_chat_route(self):
        context = get_glpi_auth_context(make_request("/api/chat"), None)
        self.assertTrue(context.initial_chat_session)

        with self.assertRaises(HTTPException) as error:
            get_glpi_auth_context(make_request("/api/tickets"), None)
        self.assertEqual(error.exception.status_code, 401)

    def test_development_bypass_does_not_skip_chat_identification(self):
        session_store.create_session(
            "dev-session",
            {"id": 7, "name": "ana"},
            token="dev-session-token",
        )
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="dev-session-token",
        )
        client = object()
        with patch.dict(
            os.environ,
            {"APP_ENV": "development", "DEV_BYPASS_API_VALIDATION": "true"},
        ):
            initial_context = get_glpi_auth_context(make_request("/api/chat"), None)
            with patch("src.main._new_glpi_client", return_value=client):
                session_context = get_glpi_auth_context(
                    make_request("/api/chat"),
                    credentials,
                )

        self.assertTrue(initial_context.initial_chat_session)
        self.assertIs(session_context.client, client)
        self.assertEqual(session_context.user["id"], 7)

    def test_first_chat_message_rejects_email_not_matching_user(self):
        with self.assertRaises(HTTPException) as error:
            asyncio.run(_find_chat_user("ana", "other@example.com"))

        self.assertEqual(error.exception.status_code, 404)

    def test_session_token_authenticates_glpi_routes_without_being_forwarded(self):
        session_store.save_session(
            "session-7",
            {"id": 7, "name": "ana"},
            token="opaque-session-token",
        )
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="opaque-session-token",
        )
        client = object()

        with patch("src.main._new_glpi_client", return_value=client):
            context = get_glpi_auth_context(
                make_request("/api/tickets"),
                credentials,
            )

        self.assertIs(context.client, client)
        self.assertEqual(context.user["id"], 7)
        self.assertEqual(context.jwt_claims, {"sub": "7"})

    def test_session_creation_rejects_existing_session_ids(self):
        self.assertTrue(
            session_store.create_session(
                "existing-session",
                {"id": 7, "name": "ana"},
                token="first-token",
            )
        )
        self.assertFalse(
            session_store.create_session(
                "existing-session",
                {"id": 8, "name": "bia"},
                token="second-token",
            )
        )
        self.assertIsNone(session_store.get_session_by_token("second-token"))
        self.assertEqual(
            session_store.get_session("existing-session")["user"]["id"],
            7,
        )


if __name__ == "__main__":
    unittest.main()
