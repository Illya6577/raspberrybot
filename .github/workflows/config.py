import os

# ── Required ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set.\n"
        "Get a token from @BotFather and set it:\n"
        "  export BOT_TOKEN='123456:ABC-DEF...'"
    )

# ── Internal ──────────────────────────────────────────────────────────────────
SUPER_ADMIN_ID: int = 1364956453
