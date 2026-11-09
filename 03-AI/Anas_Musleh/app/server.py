"""Private HTTP wrapper around the assistant. Only the Node backend may call it.

Run: uvicorn app.server:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config
from .backend_client import get_client
from .errors import BackendError
from .llm import Assistant
from .tools import ToolExecutor

logger = logging.getLogger("gdg.chatbot")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _unavailable(code: str) -> HTTPException:
    return HTTPException(status_code=503, detail={"code": code})


@app.get("/health")
def health():
    return {"status": "ok"}


# A plain def: FastAPI runs it in a worker thread, so slow Gemini calls do not block other requests.
@app.post("/chat")
def chat(body: ChatRequest, authorization: str = Header(default=""), x_internal_key: str = Header(default="")):
    if not config.CHATBOT_INTERNAL_KEY or config.BACKEND_MODE != "real" or not config.GEMINI_API_KEY:
        raise _unavailable("CHATBOT_NOT_CONFIGURED")      # never answer real users from mock data
    if not hmac.compare_digest(x_internal_key.encode(), config.CHATBOT_INTERNAL_KEY.encode()):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN"})
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"})

    token = authorization[7:].strip()
    try:
        assistant = Assistant(ToolExecutor(get_client(), token, config.today()))
        return {"answer": assistant.ask(body.question).answer}
    except BackendError as error:
        if error.status == 401:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"})
        logger.warning("Backend error during chat: %s", error.code)
        raise _unavailable("BACKEND_UNAVAILABLE")
    except Exception as error:                            # Gemini down, quota, network
        logger.warning("Chat failed: %s", type(error).__name__)
        raise _unavailable("AI_UNAVAILABLE")
