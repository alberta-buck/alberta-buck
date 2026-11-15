"""Shared pytest configuration for the alberta_buck tests.

legacy/ holds tests of retired worlds, agents and baskets (the legacy-signed Buck, the
pre-canonical household and debtor agents, the mature worlds).  The default run does not
collect it; name a file or the directory to run them explicitly:

    python -m pytest alberta_buck/test/legacy/test_mature_worlds.py
    python -m pytest alberta_buck/test/legacy/
"""

collect_ignore                  = ["legacy"]
