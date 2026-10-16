"""Explicit commands shared by human, conversational agent, and launchd callers."""

import argparse
import json
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import launchd
from .config import Config, default_config_path, load_config, save_config
from .models import RelayError
from .publishing import export
from .store import marker
from .workflow import enqueue, initialize, locked, mark, reconcile, run


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Keep a small research reading queue in iCloud")
    root.add_argument("--config", type=Path, default=default_config_path())
    commands = root.add_subparsers(dest="action", required=True)
    init = commands.add_parser(
        "init", help="Create configuration, local ledger, and reading folders"
    )
    init.add_argument("--reading-dir", type=Path)
    init.add_argument("--data-dir", type=Path)
    init.add_argument("--state-dir", type=Path)
    init.add_argument("--cache-dir", type=Path)
    init.add_argument("--harness", choices=["codex", "omp", "custom"])
    init.add_argument("--model")
    init.add_argument("--notebook-root", type=Path, action="append")
    init.add_argument("--download-root", type=Path, action="append")
    daily = commands.add_parser("run", help="Reconcile feedback and fill vacancies")
    daily.add_argument("--reconcile-only", action="store_true")
    listing = commands.add_parser("list", help="Show ledger IDs and statuses")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--all", action="store_true", help="Include undisplayed candidates")
    commands.add_parser("scan", help="Discover candidates without invoking an LLM")
    add = commands.add_parser("add", help="Add a paper URL or local PDF")
    add.add_argument("source")
    add.add_argument("--title", default="")
    add.add_argument("--queue", action="store_true", help="Explicitly fill a free slot")
    status = commands.add_parser("mark", help="Record an explicit reading decision")
    status.add_argument("id")
    status.add_argument("status", choices=["read", "deferred", "excluded", "queued"])
    status.add_argument("--until", help="Deferral date, YYYY-MM-DD (local midnight)")
    preference = commands.add_parser(
        "prefer", help="Persist a natural-language selection instruction"
    )
    preference.add_argument("text")
    commands.add_parser("preferences", help="Show saved preference IDs")
    forget = commands.add_parser("forget", help="Remove a saved preference by its numeric ID")
    forget.add_argument("id", type=int)
    install = commands.add_parser("install", help="Install and load the daily macOS LaunchAgent")
    install.add_argument("--hour", type=int)
    install.add_argument("--minute", type=int)
    commands.add_parser("uninstall", help="Remove the LaunchAgent; retain papers and ledger")
    commands.add_parser("schedule", help="Inspect the loaded LaunchAgent")
    return root


def deadline(_signal: int, _frame: object) -> None:
    raise RelayError(
        "Workflow deadline exceeded; existing queue retained. Inspect Status.txt and logs."
    )


def _run_with_deadline(seconds: int, operation: Callable[[], None]) -> None:
    if sys.platform != "win32" and hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(seconds)
        try:
            operation()
        finally:
            signal.alarm(0)
        return

    import _thread
    import threading

    expired = threading.Event()

    def interrupt() -> None:
        expired.set()
        _thread.interrupt_main()

    timer = threading.Timer(seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        operation()
    except KeyboardInterrupt:
        if expired.is_set():
            raise RelayError(
                "Workflow deadline exceeded; existing queue retained. Inspect Status.txt and logs."
            ) from None
        raise
    finally:
        timer.cancel()


def execute(args: argparse.Namespace, config: Config) -> None:
    if args.action == "schedule":
        print(launchd.status())
        return
    if args.action == "uninstall":
        launchd.uninstall()
        print("Removed daily LaunchAgent. Papers and ledger retained.")
        return
    if args.action == "install":
        path = launchd.install(
            config,
            args.config,
            config.hour if args.hour is None else args.hour,
            config.minute if args.minute is None else args.minute,
        )
        print(f"Loaded {path}")
        return
    with locked(config) as store:
        initialize(store, config)
        if args.action == "init":
            export(store, config, [])
            print(f"Reading folder: {config.reading_dir}\nLocal state: {config.state_dir}")
        elif args.action == "run":
            diagnostics = run(store, config, reconcile_only=args.reconcile_only)
            queued = sum(row["status"] == "queued" for row in store.rows())
            print(f"{queued}/{config.capacity} queued. {config.reading_dir / 'Reading List.pdf'}")
            for diagnostic in diagnostics:
                print(diagnostic, file=sys.stderr)
        elif args.action == "list":
            rows = [dict(row) for row in store.rows() if args.all or row["status"] != "candidate"]
            if args.json:
                print(
                    json.dumps(
                        [row | {"data": json.loads(row["data"])} for row in rows],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for row in rows:
                    candidate = store.candidate(row["id"])
                    print(
                        f"{row['status']:10} {row['id']}  {candidate.title}  [{marker(row['id'])}]"
                    )
                if not rows:
                    print("No displayed papers. Run `scan` or `run` to discover candidates.")
        elif args.action == "scan":
            from .discovery import discover

            result = discover(
                config.notebook_roots,
                config.download_roots,
                max_files=config.max_files,
                max_candidates=config.max_candidates,
                activity_days=config.activity_days,
            )
            for candidate in result.candidates:
                store.upsert(candidate)
            store.activity(result.activity, config.activity_days)
            store.meta("last_scan", str(time.time()))
            export(store, config, result.diagnostics)
            print(
                f"Discovered {len(result.candidates)} candidates, {len(result.activity)} activity files"
            )
            for diagnostic in result.diagnostics:
                print(diagnostic, file=sys.stderr)
        elif args.action == "add":
            from .discovery import candidate_from_url, discover
            from .models import Selection

            path = Path(args.source).expanduser()
            if path.is_file():
                # Explicit file discovery uses a private directory to avoid scanning its siblings.
                import shutil
                import tempfile

                from .publishing import validate_pdf

                validate_pdf(path, config.max_pdf_bytes)
                with tempfile.TemporaryDirectory(prefix="reading-relay-add-") as temp:
                    linked = Path(temp) / path.name
                    shutil.copyfile(path.resolve(), linked)
                    result = discover([], [Path(temp)], max_files=1)
                    if not result.candidates:
                        raise RelayError("Cannot ingest PDF: " + "; ".join(result.diagnostics))
                    candidate = result.candidates[0]
                    candidate.local_path = str(path.resolve())
                    candidate.sources = [str(path.resolve())]
            else:
                candidate = candidate_from_url(
                    args.source, title=args.title, source="Explicit user addition"
                )
                if candidate is None:
                    raise RelayError("Supply an existing PDF or a supported paper URL")
            if args.title:
                candidate.title = args.title
            store.upsert(candidate)
            diagnostics = reconcile(store, config)
            if args.queue:
                enqueue(store, config, Selection(candidate.id, "Explicit user selection", "active"))
            export(store, config, diagnostics)
            print(candidate.id)
        elif args.action == "mark":
            until = datetime.strptime(args.until, "%Y-%m-%d").timestamp() if args.until else 0
            if args.until and args.status != "deferred":
                raise RelayError("--until applies only to deferred papers")
            if until and until <= time.time():
                raise RelayError("Deferral date must be in the future")
            diagnostics = reconcile(store, config)
            mark(store, config, args.id, args.status, until=until)
            export(store, config, diagnostics)
            print(f"{args.id}: {args.status}")
        elif args.action == "prefer":
            if not args.text.strip() or len(args.text) > 4000:
                raise RelayError("Preference must contain 1..4000 characters")
            identifier = store.prefer(args.text.strip())
            export(store, config, [])
            print(f"Saved preference {identifier}: {args.text.strip()}")
        elif args.action == "preferences":
            for row in store.db.execute("SELECT id,text FROM preferences ORDER BY id"):
                print(f"{row['id']}: {row['text']}")
        elif args.action == "forget":
            store.forget(args.id)
            export(store, config, [])
            print(f"Removed preference {args.id}")


def main() -> int:
    args = parser().parse_args()
    args.config = args.config.expanduser().absolute()
    try:
        if args.action != "init" and not args.config.exists():
            raise RelayError("Run init first to create a persistent configuration")
        if args.action == "init" and not args.config.exists():
            config = Config()
            for key in ("reading_dir", "data_dir", "state_dir", "cache_dir"):
                if getattr(args, key):
                    setattr(config, key, getattr(args, key).expanduser().absolute())
            if args.notebook_root is not None:
                config.notebook_roots = [p.expanduser().absolute() for p in args.notebook_root]
            if args.harness is not None:
                config.harness = args.harness
            if args.model is not None:
                config.model = args.model
            if args.download_root is not None:
                config.download_roots = [p.expanduser().absolute() for p in args.download_root]
            save_config(config, args.config)
        config = load_config(args.config)
        if args.action == "init" and any(
            getattr(args, name)
            for name in (
                "reading_dir",
                "data_dir",
                "state_dir",
                "cache_dir",
                "notebook_root",
                "download_root",
                "harness",
                "model",
            )
        ):
            # Never silently ignore new initialization arguments against an existing configuration.
            for name, value in (
                ("reading_dir", args.reading_dir),
                ("data_dir", args.data_dir),
                ("state_dir", args.state_dir),
                ("cache_dir", args.cache_dir),
            ):
                if value and value.expanduser().absolute() != getattr(config, name):
                    raise RelayError(
                        "Configuration exists; edit it explicitly instead of reinitializing"
                    )
            for name, value in (("harness", args.harness), ("model", args.model)):
                if value is not None and value != getattr(config, name):
                    raise RelayError(
                        "Configuration exists; edit it explicitly instead of reinitializing"
                    )
            for name, values in (
                ("notebook_roots", args.notebook_root),
                ("download_roots", args.download_root),
            ):
                if values is not None and [p.expanduser().absolute() for p in values] != getattr(
                    config, name
                ):
                    raise RelayError("Configuration exists; edit its roots explicitly")
        _run_with_deadline(config.run_timeout, lambda: execute(args, config))
        return 0
    except (RelayError, OSError, ValueError) as exc:
        print(f"reading-relay: {exc}", file=sys.stderr)
        return 1
