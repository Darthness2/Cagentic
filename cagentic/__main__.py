import sys

from .cli import main

if __name__ == "__main__":
    # Propagate the exit code. Calling main() bare made `python -m cagentic`
    # always exit 0, so a script could not tell a failed one-shot prompt
    # ("could not reach Ollama") from a successful one.
    sys.exit(main())
