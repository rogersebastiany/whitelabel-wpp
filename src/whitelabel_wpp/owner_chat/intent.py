"""Intent classification for owner chat queries.

BDD coverage:
- Owner Chat: 6 scenarios (summary, key_members, topics, engagement, member_profile, unknown)
"""

from __future__ import annotations

INTENTS: list[tuple[str, list[str]]] = [
    ("topics", ["topics", "recurring", "what keeps", "keep coming", "common themes", "temas", "assuntos", "recorrente"]),
    ("summary", ["summary", "summarize", "what happened", "recap", "discuss", "resumo", "resumir"]),
    ("key_members", ["who are", "contributors", "key members", "top contributors", "quem", "contribui", "destaque"]),
    ("engagement", ["engagement", "how active", "metrics", "stats", "ativo", "métricas", "estatísticas"]),
    ("member_profile", ["+55", "phone", "member", "about", "telefone", "membro", "sobre"]),
]


def classify_intent(message: str) -> str:
    """Keyword-based intent classification with LLM fallback.

    Returns one of: summary, key_members, topics, engagement, member_profile, unknown
    """
    lower = message.lower()

    for intent, keywords in INTENTS:
        if any(kw in lower for kw in keywords):
            return intent

    return "unknown"
