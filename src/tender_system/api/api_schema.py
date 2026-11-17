"""Export OpenAPI without a database, credentials, network, or demo import."""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from tender_system.api.api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the versioned product OpenAPI contract")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    app = create_app(sessionmaker())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(app.openapi(), indent=2) + "\n")


if __name__ == "__main__":
    main()
