"""Langfuse tracing setup."""

import logging

from langfuse import Langfuse, get_client

from .env import env, is_langfuse_configured

logger = logging.getLogger("dad-it-support")


def init_tracing() -> None:
    if not is_langfuse_configured():
        logger.warning(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set. "
            "Tracing is disabled; the app still answers."
        )
        # Without keys the SDK repeats its own auth warning on every traced call.
        logging.getLogger("langfuse").setLevel(logging.ERROR)
        return

    Langfuse(
        public_key=env.langfuse_public_key,
        secret_key=env.langfuse_secret_key,
        base_url=env.langfuse_base_url,
    )


def shutdown_tracing() -> None:
    if not is_langfuse_configured():
        return
    # Send whatever is still queued, so the last trace is not lost on Ctrl+C.
    client = get_client()
    client.flush()
    client.shutdown()
