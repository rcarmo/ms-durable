"""Run one asyncio server process; native Copilot runtime is a supervised child."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "agent.app:app",
        host=os.getenv("MS_DURABLE_HOST", "127.0.0.1"),
        port=int(os.getenv("MS_DURABLE_PORT", "8015")),
        ssl_keyfile=os.getenv("MS_DURABLE_TLS_KEY"),
        ssl_certfile=os.getenv("MS_DURABLE_TLS_CERT"),
        workers=1,
        # Live event streams never close on their own; do not hold a restart open for them.
        timeout_graceful_shutdown=5,
    )
