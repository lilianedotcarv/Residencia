"""Gerenciamento de sessões de usuário do ChatBot com isolamento em memória."""

import hashlib
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional


class SessionStore:
    """Armazena sessões ativas do ChatBot garantindo isolamento estrito entre usuários."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._token_sessions: Dict[str, str] = {}
        self._lock = Lock()

    def save_session(
        self,
        session_id: str,
        user_data: Dict[str, Any],
        *,
        token: str | None = None,
    ) -> Dict[str, Any]:
        """Salva ou atualiza a sessão associando o usuário identificado ao session_id."""
        with self._lock:
            return self._store_session(session_id, user_data, token)

    def create_session(
        self,
        session_id: str,
        user_data: Dict[str, Any],
        *,
        token: str | None = None,
    ) -> bool:
        """Cria uma sessão somente se o identificador ainda não estiver em uso."""
        with self._lock:
            if session_id in self._sessions:
                return False
            self._store_session(session_id, user_data, token)
            return True

    def _store_session(
        self,
        session_id: str,
        user_data: Dict[str, Any],
        token: str | None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        existing = self._sessions.get(session_id)
        token_hash = existing.get("token_hash") if existing else None
        if token is not None:
            if token_hash is not None:
                self._token_sessions.pop(token_hash, None)
            token_hash = self._hash_token(token)
        session = {
            "session_id": session_id,
            "user": dict(user_data),
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            "token_hash": token_hash,
        }
        self._sessions[session_id] = session
        if token_hash is not None:
            self._token_sessions[token_hash] = session_id
        return {
            "session_id": session["session_id"],
            "user": dict(session["user"]),
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
        }

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Recupera os dados da sessão pelo seu ID."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return {
                "session_id": session["session_id"],
                "user": dict(session["user"]),
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
            }

    def get_session_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Recupera uma sessão pelo token opaco, sem armazenar o token em texto puro."""
        with self._lock:
            session_id = self._token_sessions.get(self._hash_token(token))
            if session_id is None:
                return None
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return {
                "session_id": session["session_id"],
                "user": dict(session["user"]),
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
            }

    def delete_session(self, session_id: str) -> bool:
        """Remove uma sessão."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            token_hash = session.get("token_hash")
            if token_hash is not None:
                self._token_sessions.pop(token_hash, None)
            return True

    def clear(self) -> None:
        """Limpa todas as sessões (útil para testes)."""
        with self._lock:
            self._sessions.clear()
            self._token_sessions.clear()


session_store = SessionStore()
