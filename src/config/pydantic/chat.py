from pydantic import BaseModel, Field, field_validator

from src.config.pydantic.base import APIInputModel
from src.config.pydantic.session import IdentifiedUserData


MAX_MESSAGE_LENGTH = 4_000


class ArticleResult(BaseModel):
    id: int
    titulo: str
    conteudo: str
    pontuacao: float
    termos_encontrados: list[str]
    fonte_api: str


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatResponse(BaseModel):
    message: str
    session_id: str
    historico: list[HistoryMessage]
    pergunta: str
    palavras_chave: list[str]
    artigos: list[ArticleResult]
    total: int


class ChatSessionResponse(BaseModel):
    success: bool = True
    message: str
    session_id: str
    session_token: str
    user: IdentifiedUserData


class ChatRequest(APIInputModel):
    message: str = Field(json_schema_extra={"minLength": 1, "maxLength": MAX_MESSAGE_LENGTH})
    session_id: str | None = Field(default=None, json_schema_extra={"maxLength": 128})

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if cls.should_bypass_validation():
            return value
        if not value.strip():
            raise ValueError("A mensagem não pode ser vazia.")
        if len(value) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"A mensagem não pode exceder {MAX_MESSAGE_LENGTH} caracteres.")
        return value