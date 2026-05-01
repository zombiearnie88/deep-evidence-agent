"""CLI wrapper around the evidence compiler library API."""

from __future__ import annotations

import getpass
import json
import os
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

import yaml
from pydantic import BaseModel

from evidence_compiler.api import (
    MissingCredentialsError,
    add_path,
    compile_workspace,
    delete_credentials,
    find_workspace_root,
    get_config_snapshot,
    get_credentials_status,
    get_job,
    get_provider_catalog,
    get_status,
    init_workspace,
    list_documents,
    list_jobs,
    preview_compile_plan,
    set_config_value,
    set_workspace_credentials,
    validate_workspace_credentials,
    wait_for_job,
)
from evidence_compiler.converter import SUPPORTED_EXTENSIONS
from evidence_compiler.lint import run_structural_lint
from evidence_compiler.models import (
    CompilePlanPreviewResult,
    CredentialStatus,
    JobRecord,
    WorkspaceStatus,
)
from evidence_compiler.watcher import watch_directory

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2
EXIT_CREDENTIALS = 3
EXIT_TIMEOUT = 4


def _resolve_workspace(candidate: Path | None) -> Path:
    """Resolve workspace path from explicit argument or nearest marker lookup."""
    if candidate is not None:
        return candidate.resolve()
    discovered = find_workspace_root(Path.cwd())
    if discovered is None:
        return Path.cwd().resolve()
    return discovered


def _to_jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _emit_json(value: object) -> None:
    print(json.dumps(_to_jsonable(value), ensure_ascii=False, indent=2))


def _resolve_api_key(args: Namespace) -> str:
    direct = str(getattr(args, "api_key", "") or "").strip()
    from_stdin = bool(getattr(args, "api_key_stdin", False))
    env_name = str(getattr(args, "api_key_env", "") or "").strip()
    sources = int(bool(direct)) + int(from_stdin) + int(bool(env_name))
    if sources > 1:
        raise ValueError("Choose only one API key input path")
    if direct:
        return direct
    if from_stdin:
        api_key = sys.stdin.read().strip()
        if not api_key:
            raise ValueError("API key stdin input was empty")
        return api_key
    if env_name:
        api_key = str(os.environ.get(env_name) or "").strip()
        if not api_key:
            raise ValueError(f"Environment variable is empty: {env_name}")
        return api_key
    api_key = getpass.getpass("API key: ").strip()
    if not api_key:
        raise ValueError("API key cannot be empty")
    return api_key


def _print_status_human(
    workspace_status: WorkspaceStatus, credentials_status: CredentialStatus
) -> None:
    print(
        f"workspace={workspace_status.workspace}\n"
        f"indexed_documents={workspace_status.indexed_documents}\n"
        f"compiled_documents={workspace_status.compiled_documents}\n"
        f"raw_files={workspace_status.raw_files}\n"
        f"source_pages={workspace_status.source_pages}\n"
        f"evidence_pages={workspace_status.evidence_pages}\n"
        f"conflict_pages={workspace_status.conflict_pages}\n"
        f"long_documents_pending_pageindex={workspace_status.long_documents_pending_pageindex}\n"
        f"jobs queued={workspace_status.queued_jobs} completed={workspace_status.completed_jobs} failed={workspace_status.failed_jobs}\n"
        f"credentials provider={credentials_status.provider or '-'} model={credentials_status.model or '-'} ready={credentials_status.has_api_key} validated={credentials_status.validated}"
    )


def _print_job_human(job: JobRecord) -> None:
    progress = f"{job.progress:.0%}" if job.progress is not None else "-"
    print(
        f"job_id={job.job_id}\n"
        f"kind={job.kind}\n"
        f"status={job.status}\n"
        f"stage={job.stage or '-'}\n"
        f"progress={progress}\n"
        f"message={job.message or '-'}\n"
        f"error={job.error or '-'}"
    )


def _print_plan_human(preview: CompilePlanPreviewResult) -> None:
    plan = preview.plan
    print(f"plan workspace={preview.workspace} pending_documents={preview.document_count}")
    print(
        f"topics create={plan.topics.create_count} update={plan.topics.update_count} related={plan.topics.related_count}"
    )
    print(
        f"regulations create={plan.regulations.create_count} update={plan.regulations.update_count} related={plan.regulations.related_count}"
    )
    print(
        f"procedures create={plan.procedures.create_count} update={plan.procedures.update_count} related={plan.procedures.related_count}"
    )
    print(
        f"conflicts create={plan.conflicts.create_count} update={plan.conflicts.update_count} related={plan.conflicts.related_count}"
    )
    print(f"evidence pages={plan.evidence_count}")


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="evidence-compiler")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser("init", help="Initialize workspace layout and compiler state")
    init_parser.add_argument("workspace", type=Path, nargs="?", default=Path("."))
    init_parser.add_argument("--model", type=str, default=None)

    add_parser = sub.add_parser("add", help="Ingest a document file or directory")
    add_parser.add_argument("path", type=Path)
    add_parser.add_argument("--workspace", type=Path, default=None)

    list_parser = sub.add_parser("list", help="List indexed documents")
    list_parser.add_argument("--workspace", type=Path, default=None)

    status_parser = sub.add_parser("status", help="Show workspace status")
    status_parser.add_argument("--workspace", type=Path, default=None)

    credentials_parser = sub.add_parser("credentials", help="Manage workspace credentials")
    credentials_parser.add_argument("--workspace", type=Path, default=None)
    credentials_parser.add_argument("--provider", type=str, default=None)
    credentials_parser.add_argument("--model", type=str, default=None)
    credentials_parser.add_argument("--api-key", type=str, default=None)
    credentials_parser.add_argument("--api-key-stdin", action="store_true")
    credentials_parser.add_argument("--api-key-env", type=str, default=None)
    credentials_sub = credentials_parser.add_subparsers(dest="credentials_command")
    credentials_status_parser = credentials_sub.add_parser("status", help="Show credential status")
    credentials_status_parser.add_argument("--workspace", type=Path, default=None)
    credentials_delete_parser = credentials_sub.add_parser("delete", help="Delete stored credentials")
    credentials_delete_parser.add_argument("--workspace", type=Path, default=None)
    credentials_validate_parser = credentials_sub.add_parser("validate", help="Validate stored credentials")
    credentials_validate_parser.add_argument("--workspace", type=Path, default=None)
    credentials_set_parser = credentials_sub.add_parser("set", help="Store provider/model/api-key")
    credentials_set_parser.add_argument("--workspace", type=Path, default=None)
    credentials_set_parser.add_argument("--provider", type=str, required=True)
    credentials_set_parser.add_argument("--model", type=str, required=True)
    credentials_set_parser.add_argument("--api-key", type=str, default=None)
    credentials_set_parser.add_argument("--api-key-stdin", action="store_true")
    credentials_set_parser.add_argument("--api-key-env", type=str, default=None)

    sub.add_parser("providers", help="List supported provider options")

    validate_parser = sub.add_parser("validate-credentials", help="Validate workspace credentials")
    validate_parser.add_argument("--workspace", type=Path, default=None)

    lint_parser = sub.add_parser("lint", help="Run structural lint report")
    lint_parser.add_argument("--workspace", type=Path, default=None)

    rebuild_parser = sub.add_parser("rebuild", help="Compile pending documents")
    rebuild_parser.add_argument("--workspace", type=Path, default=None)
    rebuild_parser.add_argument("--dry-run", action="store_true")

    plan_parser = sub.add_parser("plan", help="Preview pending compile plan")
    plan_parser.add_argument("--workspace", type=Path, default=None)

    jobs_parser = sub.add_parser("jobs", help="Inspect workspace jobs")
    jobs_parser.add_argument("--workspace", type=Path, default=None)
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command", required=True)
    jobs_list_parser = jobs_sub.add_parser("list", help="List workspace jobs")
    jobs_list_parser.add_argument("--workspace", type=Path, default=None)
    jobs_get_parser = jobs_sub.add_parser("get", help="Get one job")
    jobs_get_parser.add_argument("--workspace", type=Path, default=None)
    jobs_get_parser.add_argument("job_id", type=str)
    jobs_wait_parser = jobs_sub.add_parser("wait", help="Wait for a job to finish")
    jobs_wait_parser.add_argument("--workspace", type=Path, default=None)
    jobs_wait_parser.add_argument("job_id", type=str)
    jobs_wait_parser.add_argument("--timeout", type=float, default=None)
    jobs_wait_parser.add_argument("--interval", type=float, default=0.2)

    config_parser = sub.add_parser("config", help="Read or write workspace config")
    config_parser.add_argument("--workspace", type=Path, default=None)
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_get_parser = config_sub.add_parser("get", help="Show config values")
    config_get_parser.add_argument("--workspace", type=Path, default=None)
    config_get_parser.add_argument("key", nargs="?", default=None)
    config_set_parser = config_sub.add_parser("set", help="Set one config value")
    config_set_parser.add_argument("--workspace", type=Path, default=None)
    config_set_parser.add_argument("key", type=str)
    config_set_parser.add_argument("value", type=str)

    watch_parser = sub.add_parser("watch", help="Watch raw directory and auto-ingest files")
    watch_parser.add_argument("--workspace", type=Path, default=None)
    watch_parser.add_argument("--debounce", type=float, default=2.0)
    return parser


def _handle_init(args: Namespace, as_json: bool) -> int:
    result = init_workspace(args.workspace, model=args.model)
    if as_json:
        _emit_json(result)
    else:
        mode = "initialized" if result.created else "already initialized"
        print(f"{mode}: {result.workspace}")
    return EXIT_OK


def _handle_providers(as_json: bool) -> int:
    options = get_provider_catalog()
    if as_json:
        _emit_json(options)
    else:
        print("Providers:")
        for item in options:
            models = ", ".join(item.model_examples)
            print(f"- {item.provider_id}: {item.label} | examples={models}")
    return EXIT_OK


def _handle_add(workspace: Path, args: Namespace, as_json: bool) -> int:
    result = add_path(workspace, args.path)
    if as_json:
        _emit_json(result)
    else:
        print(
            f"ingested workspace={workspace} discovered={result.discovered_files} "
            f"added={len(result.added_documents)} skipped={len(result.skipped_files)} "
            f"unsupported={len(result.unsupported_files)}"
        )
        if result.job_id:
            print(f"job_id={result.job_id}")
    return EXIT_OK


def _handle_list(workspace: Path, as_json: bool) -> int:
    documents = list_documents(workspace)
    if as_json:
        _emit_json({"workspace": workspace, "items": documents})
        return EXIT_OK
    if not documents:
        print("No documents indexed yet.")
        return EXIT_OK
    print(f"Documents ({len(documents)}):")
    for document in documents:
        source = str(document.source_path) if document.source_path else "-"
        print(
            f"- {document.name} type={document.file_type} status={document.status} "
            f"pageindex={document.requires_pageindex} source={source}"
        )
    return EXIT_OK


def _handle_status(workspace: Path, as_json: bool) -> int:
    status = get_status(workspace)
    credentials = get_credentials_status(workspace)
    if as_json:
        _emit_json({"status": status, "credentials": credentials})
    else:
        _print_status_human(status, credentials)
    return EXIT_OK


def _handle_credentials(workspace: Path, args: Namespace, as_json: bool) -> int:
    credentials_command = args.credentials_command
    if credentials_command == "delete":
        status = delete_credentials(workspace)
        if as_json:
            _emit_json(status)
        else:
            print(f"credentials deleted workspace={workspace} ready={status.has_api_key}")
        return EXIT_OK
    if credentials_command == "status":
        status = get_credentials_status(workspace)
        if as_json:
            _emit_json(status)
        else:
            print(
                f"credentials provider={status.provider or '-'} model={status.model or '-'} "
                f"ready={status.has_api_key} validated={status.validated}"
            )
        return EXIT_OK
    if credentials_command == "validate":
        status = validate_workspace_credentials(workspace)
        if as_json:
            _emit_json(status)
        else:
            print(
                f"credentials validated provider={status.provider} model={status.model} validated_at={status.validated_at}"
            )
        return EXIT_OK

    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    if credentials_command == "set" or provider or model:
        if not provider or not model:
            raise ValueError("Both --provider and --model are required")
        api_key = _resolve_api_key(args)
        status = set_workspace_credentials(
            workspace,
            provider=provider,
            model=model,
            api_key=api_key,
        )
        if as_json:
            _emit_json(status)
        else:
            print(
                f"credentials stored provider={status.provider} model={status.model} ready={status.has_api_key}"
            )
        return EXIT_OK

    status = get_credentials_status(workspace)
    if as_json:
        _emit_json(status)
    else:
        print(
            f"credentials provider={status.provider or '-'} model={status.model or '-'} "
            f"ready={status.has_api_key} validated={status.validated}"
        )
    return EXIT_OK


def _handle_validate_credentials(workspace: Path, as_json: bool) -> int:
    status = validate_workspace_credentials(workspace)
    if as_json:
        _emit_json(status)
    else:
        print(
            f"credentials validated provider={status.provider} model={status.model} validated_at={status.validated_at}"
        )
    return EXIT_OK


def _handle_jobs(workspace: Path, args: Namespace, as_json: bool) -> int:
    if args.jobs_command == "list":
        jobs = list_jobs(workspace)
        if as_json:
            _emit_json(jobs)
        else:
            print(f"Jobs ({len(jobs.items)}):")
            for job in jobs.items:
                print(f"- {job.job_id} kind={job.kind} status={job.status} stage={job.stage or '-'}")
        return EXIT_OK
    if args.jobs_command == "get":
        job = get_job(workspace, args.job_id)
        if as_json:
            _emit_json(job)
        else:
            _print_job_human(job)
        return EXIT_OK
    job = wait_for_job(
        workspace,
        args.job_id,
        timeout_seconds=args.timeout,
        interval_seconds=args.interval,
    )
    if as_json:
        _emit_json(job)
    else:
        _print_job_human(job)
    return EXIT_OK if job.status != "failed" else EXIT_ERROR


def _handle_config(workspace: Path, args: Namespace, as_json: bool) -> int:
    if args.config_command == "get":
        snapshot = get_config_snapshot(workspace)
        if args.key is None:
            if as_json:
                _emit_json(snapshot)
            else:
                for key, value in snapshot.values.items():
                    print(f"{key}={value}")
            return EXIT_OK
        value = snapshot.values.get(args.key)
        payload = {"workspace": workspace, "key": args.key, "value": value}
        if as_json:
            _emit_json(payload)
        else:
            print(value)
        return EXIT_OK

    parsed_value = yaml.safe_load(args.value)
    snapshot = set_config_value(workspace, args.key, parsed_value)
    if as_json:
        _emit_json(snapshot)
    else:
        print(f"config updated {args.key}={snapshot.values.get(args.key)!r}")
    return EXIT_OK


def _handle_lint(workspace: Path, as_json: bool) -> int:
    report = run_structural_lint(workspace)
    report_path = workspace / "wiki" / "reports" / "lint_cli.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    if as_json:
        _emit_json({"workspace": workspace, "report_path": report_path})
    else:
        print(f"lint report written: {report_path}")
    return EXIT_OK


def _handle_plan(workspace: Path, as_json: bool) -> int:
    preview = preview_compile_plan(workspace)
    if as_json:
        _emit_json(preview)
    else:
        _print_plan_human(preview)
    return EXIT_OK


def _handle_watch(workspace: Path, args: Namespace, as_json: bool) -> int:
    raw_dir = workspace / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if as_json:
        _emit_json(
            {
                "event": "watch_started",
                "workspace": workspace,
                "raw_dir": raw_dir,
                "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
            }
        )
    else:
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
                if as_json:
                    _emit_json({"event": "ingested", "path": path, "result": result})
                else:
                    print(f"ingested {path.name}")

    watch_directory(raw_dir, _on_paths, debounce=args.debounce)
    return EXIT_OK


def _handle_rebuild(workspace: Path, as_json: bool) -> int:
    result = compile_workspace(workspace)
    if as_json:
        _emit_json(result)
    else:
        print(
            f"compiled workspace={result.workspace} processed={result.processed_files} "
            f"pages={result.created_pages} job_id={result.job_id}"
        )
    return EXIT_OK


def _run_workspace_command(
    workspace: Path, args: Namespace, command: str, as_json: bool
) -> int:
    if command == "add":
        return _handle_add(workspace, args, as_json)
    if command == "list":
        return _handle_list(workspace, as_json)
    if command == "status":
        return _handle_status(workspace, as_json)
    if command == "credentials":
        return _handle_credentials(workspace, args, as_json)
    if command == "validate-credentials":
        return _handle_validate_credentials(workspace, as_json)
    if command == "jobs":
        return _handle_jobs(workspace, args, as_json)
    if command == "config":
        return _handle_config(workspace, args, as_json)
    if command == "lint":
        return _handle_lint(workspace, as_json)
    if command == "plan" or (command == "rebuild" and args.dry_run):
        return _handle_plan(workspace, as_json)
    if command == "watch":
        return _handle_watch(workspace, args, as_json)
    return _handle_rebuild(workspace, as_json)


def _run_command(args: Namespace) -> int:
    as_json = bool(args.json)
    command = args.command or "rebuild"
    if command == "init":
        return _handle_init(args, as_json)
    if command == "providers":
        return _handle_providers(as_json)
    workspace = _resolve_workspace(getattr(args, "workspace", None))
    return _run_workspace_command(workspace, args, command, as_json)


def main() -> None:
    """Run the evidence-compiler command-line interface."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(_run_command(args))
    except MissingCredentialsError as error:
        if args.json:
            _emit_json({"ok": False, "code": "missing_credentials", "message": str(error)})
        else:
            print(error, file=sys.stderr)
        raise SystemExit(EXIT_CREDENTIALS) from error
    except TimeoutError as error:
        if args.json:
            _emit_json({"ok": False, "code": "timeout", "message": str(error)})
        else:
            print(error, file=sys.stderr)
        raise SystemExit(EXIT_TIMEOUT) from error
    except (FileNotFoundError, ValueError) as error:
        if args.json:
            _emit_json({"ok": False, "code": "invalid_request", "message": str(error)})
        else:
            print(error, file=sys.stderr)
        raise SystemExit(EXIT_INVALID) from error
    except SystemExit:
        raise
    except Exception as error:
        if args.json:
            _emit_json({"ok": False, "code": "unexpected_error", "message": str(error)})
        else:
            print(error, file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from error


if __name__ == "__main__":
    main()
