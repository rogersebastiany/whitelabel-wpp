"""WhatsApp client via local whatsmeow bridge — drop-in for TelnyxClient in local/POC mode."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class WhatsmeowClient:
    def __init__(self, bridge_url: str = "http://localhost:8080"):
        self._http = httpx.AsyncClient(
            base_url=bridge_url,
            timeout=30.0,
        )

    async def send_text(self, from_number: str, to: str, text: str) -> dict:
        """Send a text message via the whatsmeow bridge.

        from_number is ignored (bridge uses the linked device's number).
        Kept for API compatibility with TelnyxClient.
        """
        resp = await self._http.post("/send", json={"to": to, "text": text})
        if resp.status_code >= 400:
            logger.error("Bridge send error %d: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()

    async def send_template(self, from_number: str, to: str, template_name: str, **kwargs) -> dict:
        logger.warning("Templates not supported in local mode, sending as text")
        return await self.send_text(from_number, to, f"[template:{template_name}]")

    async def send_reaction(self, from_number: str, to: str, message_id: str, emoji: str) -> dict:
        logger.warning("Reactions not supported in local mode")
        return {"status": "skipped"}

    async def close(self):
        await self._http.aclose()
