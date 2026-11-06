"""One image, one entrypoint: ROLE picks which hop this process plays."""
import importlib
import os

import uvicorn

ROLES = {
    "waf": "demo.waf",
    "aigw": "demo.aigw",
    "guardrail": "demo.guardrail",
    "agent": "demo.agent",
    "model": "demo.model",
    "tools": "demo.tools",
    "board": "demo.board.server",
    "devrouter": "demo.devrouter",
    "relay": "demo.relay",
}


def main() -> None:
    role = os.environ.get("ROLE", "board")
    module = importlib.import_module(ROLES[role])
    uvicorn.run(module.app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")),
                log_level=os.environ.get("LOG_LEVEL", "warning"), access_log=False)


if __name__ == "__main__":
    main()
