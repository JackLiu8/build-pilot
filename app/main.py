from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="build-pilot")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    try:
        payload: Any = await request.json()
    except ValueError:
        payload = None

    print("Webhook received" if payload is not None else "Webhook received without JSON")
    return {"ok": True}
