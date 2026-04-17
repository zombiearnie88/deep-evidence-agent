"""CLI wrapper around the evidence compiler library API."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from evidence_compiler.api import (
    add_path,
    compile_workspace,
    find_workspace_root,
    get_status,
    init_workspace,
    list_documents,
)
from evidence_compiler.converter import SUPPORTED_EXTENSIONS
from evidence_compiler.watcher import watch_directory


def _resolve_workspace(candidate: Path | None) -> Path:
    """Resolve workspace path from explicit argument or nearest marker lookup."""
    if candidate is not None:
        return candidate.resolve()
    discovered = find_workspace_root(Path.cwd())
    if discovered is None:
        return Path.cwd().resolve()
    return discovered


def main() -> None:
    """Run the evidence-compiler command-line interface."""
    parser = ArgumentParser(prog="evidence-compiler")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser(
        "init", help="Initialize workspace layout and compiler state"
    )
    init_parser.add_argument("workspace", type=Path, nargs="?", default=Path("."))
    init_parser.add_argument("--model", type=str, default=None)

    add_parser = sub.add_parser("add", help="Ingest a document file or directory")
    add_parser.add_argument("path", type=Path)
    add_parser.add_argument("--workspace", type=Path, default=None)

    list_parser = sub.add_parser("list", help="List indexed documents")
    list_parser.add_argument("--workspace", type=Path, default=None)

    status_parser = sub.add_parser("status", help="Show workspace status")
    status_parser.add_argument("--workspace", type=Path, default=None)

    sub.add_parser("rebuild", help="Queue a compilation job").add_argument(
        "--workspace", type=Path, default=None
    )

    watch_parser = sub.add_parser(
        "watch", help="Watch raw directory and auto-ingest files"
    )
    watch_parser.add_argument("--workspace", type=Path, default=None)
    watch_parser.add_argument("--debounce", type=float, default=2.0)

    args = parser.parse_args()
    command = args.command or "rebuild"

    if command == "init":
        result = init_workspace(args.workspace, model=args.model)
        mode = "initialized" if result.created else "already initialized"
        print(f"{mode}: {result.workspace}")
        return

    workspace = _resolve_workspace(getattr(args, "workspace", None))

    try:
        if command == "add":
            result = add_path(workspace, args.path)
            print(
                f"ingested workspace={workspace} discovered={result.discovered_files} "
                f"added={len(result.added_documents)} skipped={len(result.skipped_files)} "
                f"unsupported={len(result.unsupported_files)}"
            )
            if result.job_id:
                print(f"job_id={result.job_id}")
            return

        if command == "list":
            documents = list_documents(workspace)
            if not documents:
                print("No documents indexed yet.")
                return
            print(f"Documents ({len(documents)}):")
            for document in documents:
                source = str(document.source_path) if document.source_path else "-"
                print(
                    f"- {document.name} type={document.file_type} status={document.status} "
                    f"pageindex={document.requires_pageindex} source={source}"
                )
            return

        if command == "status":
            status = get_status(workspace)
            print(
                f"workspace={status.workspace}\n"
                f"indexed_documents={status.indexed_documents}\n"
                f"raw_files={status.raw_files}\n"
                f"source_pages={status.source_pages}\n"
                f"long_documents_pending_pageindex={status.long_documents_pending_pageindex}\n"
                f"jobs queued={status.queued_jobs} completed={status.completed_jobs} failed={status.failed_jobs}"
            )
            return

        if command == "watch":
            raw_dir = workspace / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            print(
                f"Watching {raw_dir} for new documents. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

            def _on_paths(paths: list[Path]) -> None:
                for path in paths:
                    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    try:
                        result = add_path(workspace, path)
                    except ValueError:
                        continue
                    if result.added_documents:
                        print(f"ingested {path.name}")

            watch_directory(raw_dir, _on_paths, debounce=args.debounce)
            return

        result = compile_workspace(workspace)
        print(
            f"compiled workspace={result.workspace} processed={result.processed_files} "
            f"pages={result.created_pages} job_id={result.job_id}"
        )
    except ValueError as error:
        print(error)


if __name__ == "__main__":
    main()
