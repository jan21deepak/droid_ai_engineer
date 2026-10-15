"""Replace this process with a program that sees only the named environment variables.

Invoked as ``python -I agent_launcher.py NAME [NAME ...] -- PROGRAM [ARGS ...]``.
The parent passes variable *names* only, never values, so credentials do not
appear in any command line. Everything not named (GitHub tokens, operator
secrets, webhook secrets, ...) is dropped before ``PROGRAM`` starts, and
``execve`` means no wrapper process keeps the original environment alive.

Standard library only: it runs in isolated mode outside the app package.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if "--" not in argv:
        sys.stderr.write("usage: agent_launcher.py NAME... -- PROGRAM [ARGS...]\n")
        return 2
    separator = argv.index("--")
    keep = argv[:separator]
    program = argv[separator + 1 :]
    if not program:
        sys.stderr.write("agent_launcher.py: missing program\n")
        return 2
    env = {name: os.environ[name] for name in keep if name in os.environ}
    os.execve(program[0], program, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
