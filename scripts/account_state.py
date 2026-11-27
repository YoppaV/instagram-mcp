r"""Show or clear the block state the MCP server keeps for an account.

When Instagram stops an account (challenge, checkpoint, rate limit...), the
server records it under ``INSTAGRAM_STATE_DIR`` and every later call fails
fast, without network. Timed cooldowns lift by themselves; the rest needs a
person: resolve the notice in your own browser, then either log in again
(``python -m scripts.auth_login``) or clear the flag here. There is no MCP
tool for this on purpose, so the model cannot loop on it.

    venv/bin/python -m scripts.account_state                 # show every account
    venv/bin/python -m scripts.account_state <handle>        # show one account
    venv/bin/python -m scripts.account_state --clear <handle>

``--clear`` also lifts the 30-minute pause that a block on ``<handle>`` put
on the other accounts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from instagram_sdk.accounts import AccountRegistry, AccountStateStore  # noqa: E402
from instagram_sdk.settings import Settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("handle", nargs="?", help="account handle (default: every account)")
    parser.add_argument("--clear", metavar="HANDLE", help="reset this account to ok")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    store = AccountStateStore(settings.state_dir)
    if args.clear:
        changed = store.clear(args.clear.lstrip("@"))
        print(f"{args.clear}: cleared" if changed else f"{args.clear}: was already ok")
        return 0

    registry = AccountRegistry(settings)
    accounts = [registry.resolve(args.handle)] if args.handle else registry.list()
    out = [{"account": a.handle, **store.summary(a)} for a in accounts]
    print(json.dumps(out if not args.handle else out[0], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
