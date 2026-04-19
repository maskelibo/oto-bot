"""LLM wrapper — Claude Code CLI üzerinden çağrı.

Neden CLI:
    - API key setup yok (kullanıcının Claude Code subscription'ı yeterli)
    - Token maliyeti subscription dahilinde
    - Drop-in, sistem Python'dan subprocess ile çağrılır

Fallback:
    claude CLI yoksa veya hata verirse → None döner. Çağıran fonksiyon
    rule-based fallback'e düşmeli.
"""

from oto_bot.llm.claude_cli import ClaudeCLI, query

__all__ = ["ClaudeCLI", "query"]
