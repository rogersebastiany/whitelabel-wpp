"""Telnyx WhatsApp API client — send messages, manage templates.

BDD coverage:
- Owner Chat Interface: send_reply for all query responses
- Message Processing: send responses via Telnyx API
- Client Onboarding: WABA management (future)

From docs/telnyx-whatsapp-bsp.md:
  POST https://api.telnyx.com/v2/messages/whatsapp
  Auth: Bearer API key
  whatsapp_message.type: text, template, image, video, document, audio,
                         sticker, location, contacts, interactive, reaction
  Messaging profile auto-resolved from the "from" number.
"""

from __future__ import annotations

import logging

import httpx

from whitelabel_wpp.config import Settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telnyx.com/v2"


class TelnyxClient:
    def __init__(self, settings: Settings):
        self._api_key = settings.telnyx_api_key
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def send_text(self, from_number: str, to: str, text: str) -> dict:
        """Send a free-form text message (within 24h window)."""
        payload = {
            "from": from_number,
            "to": to,
            "whatsapp_message": {
                "type": "text",
                "text": {"body": text, "preview_url": False},
            },
        }
        return await self._post("/messages/whatsapp", payload)

    async def send_template(
        self,
        from_number: str,
        to: str,
        template_name: str,
        language_code: str = "en_US",
        components: list[dict] | None = None,
    ) -> dict:
        """Send a template message (required outside 24h window)."""
        template = {
            "name": template_name,
            "language": {"policy": "deterministic", "code": language_code},
        }
        if components:
            template["components"] = components

        payload = {
            "from": from_number,
            "to": to,
            "whatsapp_message": {"type": "template", "template": template},
        }
        return await self._post("/messages/whatsapp", payload)

    async def send_reaction(
        self, from_number: str, to: str, message_id: str, emoji: str
    ) -> dict:
        """React to a message with an emoji."""
        payload = {
            "from": from_number,
            "to": to,
            "whatsapp_message": {
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji},
            },
        }
        return await self._post("/messages/whatsapp", payload)

    async def _post(self, path: str, payload: dict) -> dict:
        resp = await self._http.post(path, json=payload)
        if resp.status_code >= 400:
            logger.error("Telnyx API error %d: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self._http.aclose()
