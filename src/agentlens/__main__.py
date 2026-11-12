"""Entry point for ``python -m agentlens``.

The ``hook`` subcommand is routed straight to the fast path so that a hook
invocation never imports argparse, sqlite3 or the rendering code.
"""

import sys


def _main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["hook"]:
        from agentlens.hook import run

        sys.exit(run(argv[1:]))
    from agentlens.cli import main

    sys.exit(main(argv))


if __name__ == "__main__":
    _main()
