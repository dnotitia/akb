"""Allow `python -m app <subcommand>` as a shorthand for `python -m app.cli <subcommand>`."""
import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
