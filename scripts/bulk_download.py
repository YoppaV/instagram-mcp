r"""Bulk-download a saved collection, all saved posts or a profile, from a terminal.

Same job as the MCP tool ``start_bulk_download`` (``instagram_sdk.bulk``),
run in the foreground: it goes through the same runtime (navigation lock
shared with the MCP servers, per-account budget, pauses) and the same
manifest, so a job started here can be followed or cancelled from the MCP
tools and the other way round. Ctrl-C leaves the job ``interrupted``; run the
same command again to resume.

    venv/bin/python -m scripts.bulk_download start --source collection --target 1789...
    venv/bin/python -m scripts.bulk_download start --source saved --videos
    venv/bin/python -m scripts.bulk_download start --source user_posts --target someone --max-pages 1
    venv/bin/python -m scripts.bulk_download status [JOB_ID] [--account HANDLE]
    venv/bin/python -m scripts.bulk_download cancel JOB_ID

Progress goes to stderr; the final status (JSON) to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from instagram_sdk import bulk  # noqa: E402
from instagram_sdk.accounts import AccountRegistry  # noqa: E402
from instagram_sdk.runtime import Runtime  # noqa: E402
from instagram_sdk.settings import Settings  # noqa: E402


async def _start(args: argparse.Namespace, settings: Settings) -> dict:
    runtime = Runtime(settings, registry=AccountRegistry(settings))
    try:
        acct = runtime.resolve(args.account)
        spec = bulk.make_spec(args.source, args.target, download_videos=args.videos,
                              mode=args.mode, known_stop=args.known_stop,
                              max_pages=args.max_pages, max_posts=args.max_posts)
        runtime.state.check(acct)
        job = bulk.BulkJob(runtime, acct.handle, spec, downloads_dir=settings.downloads_dir)
        print(f"job {job.job_id}: started", file=sys.stderr)
        return await job.run()
    finally:
        await runtime.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start", help="run (or resume) a bulk download in the foreground")
    st.add_argument("--source", required=True, choices=bulk.SOURCES)
    st.add_argument("--target", help="collection id (collection) or username (user_posts)")
    st.add_argument("--videos", action="store_true", help="also download videos")
    st.add_argument("--mode", default="auto", choices=bulk.MODES)
    st.add_argument("--known-stop", type=int, default=None)
    st.add_argument("--max-pages", type=int, default=None)
    st.add_argument("--max-posts", type=int, default=None)
    st.add_argument("--account", default=None, help="account handle (default account if omitted)")
    ss = sub.add_parser("status", help="show jobs (no network)")
    ss.add_argument("job_id", nargs="?")
    ss.add_argument("--account", default=None)
    sc = sub.add_parser("cancel", help="ask a running job to stop (no network)")
    sc.add_argument("job_id")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()

    if args.cmd == "start":
        try:
            out = asyncio.run(_start(args, settings))
        except KeyboardInterrupt:
            print("interrupted; run the same command again to resume", file=sys.stderr)
            return 130
        except (bulk.BulkJobBusyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(out, indent=1, default=str))
        return 0 if out.get("status") in ("done", "partial") else 1

    if args.cmd == "status":
        if args.job_id:
            found = bulk.job_status(settings.downloads_dir, args.job_id)
            out = {"jobs": [found] if found else []}
        else:
            registry = AccountRegistry(settings)
            handles = ([registry.resolve(args.account).handle] if args.account
                       else [a.handle for a in registry.list()])
            out = {"jobs": bulk.list_jobs(settings.downloads_dir, handles)}
        print(json.dumps(out, indent=1, default=str))
        return 0

    found = bulk.request_cancel(settings.downloads_dir, args.job_id)
    if found is None:
        print(f"error: no bulk job {args.job_id!r}", file=sys.stderr)
        return 2
    print(json.dumps(found, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
