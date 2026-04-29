import { Button, UIProvider } from "@evidence-brain/ui";
import { type CSSProperties, type FormEvent, useEffect, useMemo, useRef, useState } from "react";

type WorkspaceItem = {
  workspace_id: string;
  name: string;
  root_path: string;
  initialized: boolean;
  status?: {
    indexed_documents: number;
    compiled_documents: number;
    raw_files: number;
    source_pages: number;
    evidence_pages: number;
    conflict_pages: number;
    queued_jobs: number;
    completed_jobs: number;
    failed_jobs: number;
    credentials_ready: boolean;
  } | null;
  credentials?: CredentialStatus | null;
};

type DocumentItem = {
  doc_id: string;
  name: string;
  file_type: string;
  status: string;
  source_path: string | null;
  requires_pageindex: boolean;
};

type HealthResponse = {
  status: string;
};

type WorkspacesResponse = {
  items: WorkspaceItem[];
};

type DocumentsResponse = {
  workspace: string;
  items: DocumentItem[];
};

type CompileResponse = {
  job_id: string | null;
  processed_files: number;
  created_pages: number;
};

type WatchRequest = {
  auto_compile: boolean;
  debounce_seconds: number;
};

type WatchStatus = {
  workspace: string;
  enabled: boolean;
  paths: string[];
  auto_compile: boolean;
  debounce_seconds: number;
  pending_paths: number;
  active_compile_job_id: string | null;
  last_ingest_job_id: string | null;
  last_compile_job_id: string | null;
  last_error: string | null;
  updated_at: string | null;
};

type WatchBacklogItem = {
  path: string;
  name: string;
  size_bytes: number;
  modified_at: string;
};

type WatchBacklogResponse = {
  workspace: string;
  root: string;
  items: WatchBacklogItem[];
  total: number;
};

type TokenUsageSummary = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
  available: boolean;
};

type StageCounter = {
  completed: number;
  total: number;
  unit: string;
  item_label: string | null;
};

type CompilePlanItem = {
  slug: string;
  title: string;
  brief: string;
};

type CompilePlanBucket = {
  create_count: number;
  update_count: number;
  related_count: number;
  create: CompilePlanItem[];
  update: CompilePlanItem[];
  related: string[];
};

type CompilePlanDocument = {
  document_name: string;
  topics: CompilePlanBucket;
  regulations: CompilePlanBucket;
  procedures: CompilePlanBucket;
  conflicts: CompilePlanBucket;
  evidence_count: number;
};

type CompilePlanSummary = {
  topics: CompilePlanBucket;
  regulations: CompilePlanBucket;
  procedures: CompilePlanBucket;
  conflicts: CompilePlanBucket;
  evidence_count: number;
  documents: CompilePlanDocument[];
};

type CompileProgressDetails = {
  counters: Record<string, StageCounter>;
  plan: CompilePlanSummary | null;
  usage_total: TokenUsageSummary;
  usage_by_stage: Record<string, TokenUsageSummary>;
};

type JobRecord = {
  job_id: string;
  kind: string;
  status: string;
  created_at: string;
  updated_at: string;
  payload: Record<string, unknown>;
  stage: string | null;
  progress: number | null;
  message: string | null;
  error: string | null;
  compile: CompileProgressDetails | null;
};

type ProviderItem = {
  provider_id: string;
  label: string;
  description: string;
  model_examples: string[];
};

type ProvidersResponse = {
  items: ProviderItem[];
};

type CredentialStatus = {
  provider: string | null;
  model: string | null;
  has_api_key: boolean;
  validated: boolean;
  validated_at: string | null;
};

export type ImportSelectionMode = "files" | "folder";

export type AppShellProps = {
  pickImportPaths?: (mode: ImportSelectionMode) => Promise<string[]>;
};

type IngestResponse = {
  discovered_files: number;
  added_documents: DocumentItem[];
  skipped_files: string[];
  unsupported_files: string[];
};

const surfaceStyle: CSSProperties = {
  border: "1px solid #d7dce3",
  borderRadius: 14,
  padding: 16,
  background: "rgba(255, 255, 255, 0.92)",
  boxShadow: "0 8px 24px rgba(8, 30, 52, 0.08)",
};

const compilePollIntervalMs = 900;
const watchPollIntervalMs = 1200;

function formatUsage(usage: TokenUsageSummary | null | undefined): string {
  if (!usage || !usage.available) {
    return "N/A";
  }
  return `${usage.total_tokens.toLocaleString()} tokens (${usage.calls} calls)`;
}

function formatProgress(progress: number | null | undefined): string {
  const safe = Math.max(0, Math.min(1, progress ?? 0));
  return `${Math.round(safe * 100)}%`;
}

function statusBadgeStyle(status: string | null | undefined): CSSProperties {
  if (status === "completed") {
    return { background: "#d8f3df", color: "#0f6b2e" };
  }
  if (status === "failed") {
    return { background: "#fee6e5", color: "#a6332b" };
  }
  if (status === "running") {
    return { background: "#e6f0ff", color: "#1f57c3" };
  }
  return { background: "#eef2f6", color: "#4f6478" };
}

function bucketTotal(bucket: CompilePlanBucket): number {
  return bucket.create_count + bucket.update_count + bucket.related_count;
}

function formatPlanSummary(plan: CompilePlanSummary): string {
  return [
    `${bucketTotal(plan.topics)} topics`,
    `${bucketTotal(plan.regulations)} regulations`,
    `${bucketTotal(plan.procedures)} procedures`,
    `${bucketTotal(plan.conflicts)} conflicts`,
    `${plan.evidence_count} evidence`,
  ].join(", ");
}

function formatCounter(counter: StageCounter | null | undefined): string {
  if (!counter) {
    return "-";
  }
  return `${counter.completed}/${counter.total} ${counter.item_label ?? counter.unit}`;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return value;
  }
  return new Date(timestamp).toLocaleString();
}

function hasMeaningfulWatchChange(previous: WatchStatus | null, next: WatchStatus): boolean {
  if (!previous) {
    return (
      next.enabled ||
      Boolean(next.last_ingest_job_id) ||
      Boolean(next.last_compile_job_id) ||
      Boolean(next.last_error)
    );
  }
  return (
    previous.enabled !== next.enabled ||
    previous.last_ingest_job_id !== next.last_ingest_job_id ||
    previous.last_compile_job_id !== next.last_compile_job_id ||
    previous.active_compile_job_id !== next.active_compile_job_id ||
    previous.last_error !== next.last_error
  );
}

function planBucketPreview(bucket: CompilePlanBucket): string {
  const parts: string[] = [];
  if (bucket.create_count > 0) {
    parts.push(`create=${bucket.create_count}`);
  }
  if (bucket.update_count > 0) {
    parts.push(`update=${bucket.update_count}`);
  }
  if (bucket.related_count > 0) {
    parts.push(`related=${bucket.related_count}`);
  }
  return parts.join(", ") || "none";
}

function previewItems(items: CompilePlanItem[]): string {
  return items.map((item) => item.title || item.slug).join(", ") || "-";
}

function payloadString(
  payload: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const value = payload?.[key];
  return typeof value === "string" && value.trim() ? value : null;
}

export function AppShell({ pickImportPaths }: AppShellProps = {}) {
  const apiBase = useMemo(() => {
    const runtimeConfig = globalThis as { __EVIDENCE_BRAIN_SERVICE_URL__?: string };
    return runtimeConfig.__EVIDENCE_BRAIN_SERVICE_URL__ ?? "http://127.0.0.1:8787";
  }, []);

  const [health, setHealth] = useState<string>("checking...");
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  const [workspaceName, setWorkspaceName] = useState<string>("pilot-site");
  const [selectedWorkspace, setSelectedWorkspace] = useState<string>("");
  const [sourcePath, setSourcePath] = useState<string>("");
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [provider, setProvider] = useState<string>("openai");
  const [model, setModel] = useState<string>("gpt-5.4-mini");
  const [apiKey, setApiKey] = useState<string>("");
  const [credentialStatus, setCredentialStatus] = useState<CredentialStatus | null>(null);
  const [actionInfo, setActionInfo] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string>("");
  const [activeCompileJobId, setActiveCompileJobId] = useState<string>("");
  const [activeCompileWorkspaceRef, setActiveCompileWorkspaceRef] = useState<string>("");
  const [activeCompileJob, setActiveCompileJob] = useState<JobRecord | null>(null);
  const [compilePollError, setCompilePollError] = useState<string>("");
  const [watchStatus, setWatchStatus] = useState<WatchStatus | null>(null);
  const [watchPollError, setWatchPollError] = useState<string>("");
  const [watchBacklogItems, setWatchBacklogItems] = useState<WatchBacklogItem[]>([]);
  const [watchBacklogError, setWatchBacklogError] = useState<string>("");
  const [selectedBacklogPaths, setSelectedBacklogPaths] = useState<string[]>([]);
  const [watchAutoCompile, setWatchAutoCompile] = useState<boolean>(true);
  const [watchDebounceSeconds, setWatchDebounceSeconds] = useState<string>("2.0");
  const watchStatusRef = useRef<WatchStatus | null>(null);
  const lastWatchCompileJobIdRef = useRef<string>("");

  const loadOverview = async () => {
    setBusy(true);
    setError("");
    try {
      const [healthRes, wsRes] = await Promise.all([
        fetch(`${apiBase}/health`),
        fetch(`${apiBase}/workspaces`),
      ]);

      if (!healthRes.ok) {
        throw new Error(`health request failed: ${healthRes.status}`);
      }
      if (!wsRes.ok) {
        throw new Error(`workspaces request failed: ${wsRes.status}`);
      }

      const healthJson = (await healthRes.json()) as HealthResponse;
      const wsJson = (await wsRes.json()) as WorkspacesResponse;
      setHealth(healthJson.status);
      setWorkspaces(wsJson.items);
      if (wsJson.items.length > 0 && !selectedWorkspace) {
        setSelectedWorkspace(wsJson.items[0].workspace_id);
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setHealth("unreachable");
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const loadProviders = async () => {
    try {
      const response = await fetch(`${apiBase}/providers`);
      if (!response.ok) {
        throw new Error(`providers request failed: ${response.status}`);
      }
      const payload = (await response.json()) as ProvidersResponse;
      setProviders(payload.items);
      if (payload.items.length > 0) {
        const active = payload.items.find((item) => item.provider_id === provider) ?? payload.items[0];
        setProvider(active.provider_id);
        if (active.model_examples.length > 0) {
          setModel(active.model_examples[0]);
        }
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    }
  };

  useEffect(() => {
    void loadOverview();
    void loadProviders();
  }, [apiBase]);

  const loadCredentialStatus = async (workspaceRef: string) => {
    if (!workspaceRef) {
      setCredentialStatus(null);
      return;
    }
    try {
      const response = await fetch(`${apiBase}/workspaces/${encodeURIComponent(workspaceRef)}/credentials/status`);
      if (!response.ok) {
        throw new Error(`credentials status failed: ${response.status}`);
      }
      const payload = (await response.json()) as CredentialStatus;
      setCredentialStatus(payload);
      if (payload.provider) {
        setProvider(payload.provider);
      }
      if (payload.model) {
        setModel(payload.model);
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    }
  };

  const loadDocuments = async (workspaceRef: string) => {
    if (!workspaceRef) {
      setDocuments([]);
      return;
    }

    try {
      const response = await fetch(
        `${apiBase}/documents?workspace=${encodeURIComponent(workspaceRef)}`,
      );
      if (!response.ok) {
        throw new Error(`documents request failed: ${response.status}`);
      }
      const payload = (await response.json()) as DocumentsResponse;
      setDocuments(payload.items);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    }
  };

  const loadJob = async (jobId: string, workspaceRef: string): Promise<JobRecord> => {
    const response = await fetch(
      `${apiBase}/jobs/${encodeURIComponent(jobId)}?workspace=${encodeURIComponent(workspaceRef)}`,
    );
    if (!response.ok) {
      throw new Error(`job request failed: ${response.status}`);
    }
    const payload = (await response.json()) as JobRecord;
    setActiveCompileWorkspaceRef(workspaceRef);
    setActiveCompileJobId(jobId);
    setActiveCompileJob(payload);
    setCompilePollError("");
    return payload;
  };

  const loadWatchStatus = async (
    workspaceRef: string,
    syncFormState: boolean = false,
  ): Promise<WatchStatus | null> => {
    if (!workspaceRef) {
      watchStatusRef.current = null;
      setWatchStatus(null);
      setWatchPollError("");
      if (syncFormState) {
        setWatchAutoCompile(true);
        setWatchDebounceSeconds("2.0");
      }
      return null;
    }

    const response = await fetch(
      `${apiBase}/workspaces/${encodeURIComponent(workspaceRef)}/watch`,
    );
    if (!response.ok) {
      throw new Error(`watch status failed: ${response.status}`);
    }

    const payload = (await response.json()) as WatchStatus;
    const previous = watchStatusRef.current;
    watchStatusRef.current = payload;
    setWatchStatus(payload);
    setWatchPollError("");

    if (syncFormState) {
      setWatchAutoCompile(payload.auto_compile);
      setWatchDebounceSeconds(String(payload.debounce_seconds));
    }

    if (payload.last_compile_job_id !== lastWatchCompileJobIdRef.current) {
      lastWatchCompileJobIdRef.current = payload.last_compile_job_id ?? "";
      if (payload.last_compile_job_id) {
        await loadJob(payload.last_compile_job_id, workspaceRef);
      }
    }

    if (hasMeaningfulWatchChange(previous, payload)) {
      await loadOverview();
      if (selectedWorkspace === workspaceRef) {
        await loadDocuments(workspaceRef);
      }
    }

    return payload;
  };

  const loadWatchBacklog = async (
    workspaceRef: string,
    preserveSelection: boolean = false,
  ): Promise<WatchBacklogItem[]> => {
    if (!workspaceRef) {
      setWatchBacklogItems([]);
      setSelectedBacklogPaths([]);
      setWatchBacklogError("");
      return [];
    }

    const response = await fetch(
      `${apiBase}/workspaces/${encodeURIComponent(workspaceRef)}/watch/backlog`,
    );
    if (!response.ok) {
      throw new Error(`watch backlog failed: ${response.status}`);
    }

    const payload = (await response.json()) as WatchBacklogResponse;
    setWatchBacklogItems(payload.items);
    setWatchBacklogError("");
    if (preserveSelection) {
      setSelectedBacklogPaths((current) =>
        current.filter((path) => payload.items.some((item) => item.path === path)),
      );
    } else {
      setSelectedBacklogPaths(payload.items.map((item) => item.path));
    }
    return payload.items;
  };

  useEffect(() => {
    if (!selectedWorkspace) {
      watchStatusRef.current = null;
      lastWatchCompileJobIdRef.current = "";
      setWatchStatus(null);
      setWatchPollError("");
      setWatchBacklogItems([]);
      setWatchBacklogError("");
      setSelectedBacklogPaths([]);
      setWatchAutoCompile(true);
      setWatchDebounceSeconds("2.0");
      return;
    }
    watchStatusRef.current = null;
    lastWatchCompileJobIdRef.current = "";
    void loadDocuments(selectedWorkspace);
    void loadCredentialStatus(selectedWorkspace);
    void loadWatchStatus(selectedWorkspace, true).catch((cause) => {
      const message = cause instanceof Error ? cause.message : String(cause);
      setWatchPollError(message);
    });
    void loadWatchBacklog(selectedWorkspace).catch((cause) => {
      const message = cause instanceof Error ? cause.message : String(cause);
      setWatchBacklogError(message);
    });
  }, [apiBase, selectedWorkspace]);

  useEffect(() => {
    if (!selectedWorkspace) {
      return;
    }

    let cancelled = false;
    let timeoutId = 0;

    const poll = async () => {
      try {
        await loadWatchStatus(selectedWorkspace);
      } catch (cause) {
        if (cancelled) {
          return;
        }
        const message = cause instanceof Error ? cause.message : String(cause);
        setWatchPollError(message);
      }

      if (!cancelled) {
        timeoutId = window.setTimeout(() => {
          void poll();
        }, watchPollIntervalMs);
      }
    };

    timeoutId = window.setTimeout(() => {
      void poll();
    }, watchPollIntervalMs);

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [apiBase, selectedWorkspace]);

  useEffect(() => {
    if (!activeCompileJobId || !activeCompileWorkspaceRef) {
      return;
    }
    if (activeCompileJob?.status === "completed" || activeCompileJob?.status === "failed") {
      return;
    }

    let cancelled = false;
    let timeoutId = 0;

    const poll = async () => {
      try {
        const job = await loadJob(activeCompileJobId, activeCompileWorkspaceRef);
        if (cancelled) {
          return;
        }
        if (job.status === "completed" || job.status === "failed") {
          await loadOverview();
          if (selectedWorkspace === activeCompileWorkspaceRef) {
            await loadDocuments(activeCompileWorkspaceRef);
          }
          return;
        }
      } catch (cause) {
        if (cancelled) {
          return;
        }
        const message = cause instanceof Error ? cause.message : String(cause);
        setCompilePollError(message);
      }
      if (!cancelled) {
        timeoutId = window.setTimeout(() => {
          void poll();
        }, compilePollIntervalMs);
      }
    };

    timeoutId = window.setTimeout(() => {
      void poll();
    }, compilePollIntervalMs);

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [
    activeCompileJob?.status,
    activeCompileJobId,
    activeCompileWorkspaceRef,
    apiBase,
    selectedWorkspace,
  ]);

  const onSaveCredentials = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedWorkspace || !provider || !model || !apiKey.trim()) {
      return;
    }

    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `${apiBase}/workspaces/${encodeURIComponent(selectedWorkspace)}/credentials`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            provider,
            model,
            api_key: apiKey.trim(),
          }),
        },
      );
      if (!response.ok) {
        throw new Error(`save credentials failed: ${response.status}`);
      }
      setApiKey("");
      setActionInfo(`credentials stored for ${provider}/${model}`);
      await loadCredentialStatus(selectedWorkspace);
      await loadOverview();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onValidateCredentials = async () => {
    if (!selectedWorkspace) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `${apiBase}/workspaces/${encodeURIComponent(selectedWorkspace)}/credentials/validate`,
        {
          method: "POST",
        },
      );
      if (!response.ok) {
        throw new Error(`validate credentials failed: ${response.status}`);
      }
      setActionInfo("credentials validated");
      await loadCredentialStatus(selectedWorkspace);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onCreateWorkspace = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!workspaceName.trim()) {
      return;
    }

    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${apiBase}/workspaces`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ name: workspaceName.trim() }),
      });
      if (!response.ok) {
        throw new Error(`create workspace failed: ${response.status}`);
      }
      await loadOverview();
      setActionInfo(`workspace created: ${workspaceName.trim()}`);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const ingestSourcePath = async (path: string): Promise<IngestResponse> => {
    const response = await fetch(`${apiBase}/documents/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        workspace: selectedWorkspace,
        path,
      }),
    });
    if (!response.ok) {
      throw new Error(`ingest failed: ${response.status}`);
    }
    return (await response.json()) as IngestResponse;
  };

  const onIngestSource = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedWorkspace || !sourcePath.trim()) {
      return;
    }

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      const payload = await ingestSourcePath(sourcePath.trim());
      setActionInfo(
        `ingest done: discovered=${payload.discovered_files}, added=${payload.added_documents.length}, skipped=${payload.skipped_files.length}, unsupported=${payload.unsupported_files.length}`,
      );
      await loadOverview();
      await loadDocuments(selectedWorkspace);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onImportSelection = async (mode: ImportSelectionMode) => {
    if (!selectedWorkspace || !pickImportPaths) {
      return;
    }

    let picked: string[];
    try {
      picked = await pickImportPaths(mode);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      return;
    }
    const uniquePaths = Array.from(new Set(picked.map((path) => path.trim()).filter(Boolean)));
    if (uniquePaths.length === 0) {
      return;
    }

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      let discovered = 0;
      let added = 0;
      let skipped = 0;
      let unsupported = 0;

      for (const path of uniquePaths) {
        const payload = await ingestSourcePath(path);
        discovered += payload.discovered_files;
        added += payload.added_documents.length;
        skipped += payload.skipped_files.length;
        unsupported += payload.unsupported_files.length;
      }

      setSourcePath(uniquePaths[0]);
      setActionInfo(
        `ingest done: sources=${uniquePaths.length}, discovered=${discovered}, added=${added}, skipped=${skipped}, unsupported=${unsupported}`,
      );
      await loadOverview();
      await loadDocuments(selectedWorkspace);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onSaveWatch = async () => {
    if (!selectedWorkspace) {
      return;
    }

    const debounce = Number.parseFloat(watchDebounceSeconds);
    if (!Number.isFinite(debounce) || debounce <= 0) {
      setError("watch debounce must be a positive number");
      return;
    }

    const request: WatchRequest = {
      auto_compile: watchAutoCompile,
      debounce_seconds: debounce,
    };

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      const response = await fetch(
        `${apiBase}/workspaces/${encodeURIComponent(selectedWorkspace)}/watch`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(request),
        },
      );
      if (!response.ok) {
        if (response.status === 400) {
          const payload = (await response.json()) as { detail?: { message?: string } };
          throw new Error(payload.detail?.message ?? `save watch failed: ${response.status}`);
        }
        throw new Error(`save watch failed: ${response.status}`);
      }
      const payload = (await response.json()) as WatchStatus;
      watchStatusRef.current = payload;
      lastWatchCompileJobIdRef.current = payload.last_compile_job_id ?? "";
      setWatchStatus(payload);
      setWatchAutoCompile(payload.auto_compile);
      setWatchDebounceSeconds(String(payload.debounce_seconds));
      setWatchPollError("");
      setActionInfo(`watch enabled: ${payload.paths[0] ?? "workspace/raw"}`);
      await loadWatchBacklog(selectedWorkspace, true);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onStopWatch = async () => {
    if (!selectedWorkspace) {
      return;
    }

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      const response = await fetch(
        `${apiBase}/workspaces/${encodeURIComponent(selectedWorkspace)}/watch`,
        {
          method: "DELETE",
        },
      );
      if (!response.ok) {
        throw new Error(`stop watch failed: ${response.status}`);
      }
      const payload = (await response.json()) as WatchStatus;
      watchStatusRef.current = payload;
      setWatchStatus(payload);
      setWatchPollError("");
      setActionInfo("watch stopped");
      await loadWatchBacklog(selectedWorkspace, true);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onToggleBacklogPath = (path: string) => {
    setSelectedBacklogPaths((current) =>
      current.includes(path)
        ? current.filter((item) => item !== path)
        : [...current, path],
    );
  };

  const onIngestBacklogSelected = async () => {
    if (!selectedWorkspace || selectedBacklogPaths.length === 0) {
      return;
    }
    if (!window.confirm(`Ingest ${selectedBacklogPaths.length} pending raw file(s)?`)) {
      return;
    }

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      const response = await fetch(
        `${apiBase}/workspaces/${encodeURIComponent(selectedWorkspace)}/watch/backlog/ingest`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ paths: selectedBacklogPaths }),
        },
      );
      if (!response.ok) {
        if (response.status === 400) {
          const payload = (await response.json()) as { detail?: { message?: string } };
          throw new Error(payload.detail?.message ?? `backlog ingest failed: ${response.status}`);
        }
        throw new Error(`backlog ingest failed: ${response.status}`);
      }

      const payload = (await response.json()) as IngestResponse;
      setActionInfo(
        `backlog ingest done: discovered=${payload.discovered_files}, added=${payload.added_documents.length}, skipped=${payload.skipped_files.length}, unsupported=${payload.unsupported_files.length}`,
      );
      await loadDocuments(selectedWorkspace);
      await loadOverview();
      await loadWatchStatus(selectedWorkspace);
      await loadWatchBacklog(selectedWorkspace);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const onQueueCompile = async () => {
    if (!selectedWorkspace) {
      return;
    }

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      const response = await fetch(`${apiBase}/jobs/compile`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ workspace: selectedWorkspace }),
      });
      if (!response.ok) {
        if (response.status === 409) {
          const payload = (await response.json()) as {
            detail?: { code?: string; message?: string; job_id?: string };
          };
          if (payload.detail?.code === "compile_already_running" && payload.detail.job_id) {
            setActionInfo(`compile already running: job=${payload.detail.job_id}`);
            await loadJob(payload.detail.job_id, selectedWorkspace);
            return;
          }
          if (payload.detail?.code === "missing_llm_credentials") {
            throw new Error("missing workspace credentials; save and validate key first");
          }
          throw new Error(payload.detail?.message ?? `queue compile failed: ${response.status}`);
        }
        throw new Error(`queue compile failed: ${response.status}`);
      }
      const payload = (await response.json()) as CompileResponse;
      if (payload.job_id) {
        await loadJob(payload.job_id, selectedWorkspace);
      }
      setActionInfo(
        `compile queued: job=${payload.job_id ?? "n/a"}, docs=${payload.processed_files}`,
      );
      await loadOverview();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const visibleCompileJob =
    activeCompileWorkspaceRef === selectedWorkspace ? activeCompileJob : null;
  const currentStageCounter =
    visibleCompileJob?.stage && visibleCompileJob.compile?.counters
      ? visibleCompileJob.compile.counters[visibleCompileJob.stage] ?? null
      : null;
  const compileIsActiveForSelectedWorkspace =
    activeCompileWorkspaceRef === selectedWorkspace &&
    Boolean(activeCompileJobId) &&
    visibleCompileJob?.status !== "completed" &&
    visibleCompileJob?.status !== "failed";
  const compileProvider = payloadString(visibleCompileJob?.payload, "provider");
  const compileModel = payloadString(visibleCompileJob?.payload, "model");
  const selectedWorkspaceItem =
    workspaces.find((workspace) => workspace.workspace_id === selectedWorkspace) ?? null;
  const watchRootPath =
    watchStatus?.paths[0] ??
    (selectedWorkspaceItem ? `${selectedWorkspaceItem.root_path}/raw` : "");

  return (
    <UIProvider>
      <main
        style={{
          minHeight: "100vh",
          padding: 24,
          fontFamily: '"IBM Plex Sans", "Segoe UI", sans-serif',
          color: "#18324b",
          background:
            "radial-gradient(circle at 0% 0%, #eaf6ff 0%, #f6fafc 38%, #f2f6f9 100%)",
        }}
      >
        <section
          style={{
            maxWidth: 980,
            margin: "0 auto",
            display: "grid",
            gap: 16,
          }}
        >
          <header style={surfaceStyle}>
            <h1 style={{ marginTop: 0, marginBottom: 8 }}>Evidence Brain Desktop</h1>
            <p style={{ margin: 0 }}>
              Milestone A shell: workspace bootstrap, service health, and list/create
              workspace flow.
            </p>
          </header>

          <section style={{ ...surfaceStyle, display: "grid", gap: 12 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <strong>Service:</strong>
              <code>{apiBase}</code>
              <span
                style={{
                  marginLeft: "auto",
                  padding: "4px 8px",
                  borderRadius: 99,
                  background: health === "ok" ? "#d8f3df" : "#fee6e5",
                  color: health === "ok" ? "#0f6b2e" : "#a6332b",
                  fontWeight: 700,
                }}
              >
                {health}
              </span>
            </div>

            <form onSubmit={onCreateWorkspace} style={{ display: "flex", gap: 8 }}>
              <input
                value={workspaceName}
                onChange={(event) => setWorkspaceName(event.target.value)}
                placeholder="workspace name"
                style={{
                  flex: 1,
                  border: "1px solid #ccd5df",
                  borderRadius: 10,
                  padding: "8px 10px",
                  fontSize: 14,
                }}
              />
              <Button type="submit" disabled={busy}>
                Create Workspace
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={busy}
                onClick={() => {
                  void loadOverview();
                }}
              >
                Refresh
              </Button>
            </form>

            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <label htmlFor="workspace-select" style={{ fontWeight: 600 }}>
                Active Workspace
              </label>
              <select
                id="workspace-select"
                value={selectedWorkspace}
                onChange={(event) => setSelectedWorkspace(event.target.value)}
                style={{
                  border: "1px solid #ccd5df",
                  borderRadius: 10,
                  padding: "8px 10px",
                  minWidth: 280,
                }}
              >
                <option value="">Select workspace...</option>
                {workspaces.map((workspace) => (
                  <option key={workspace.workspace_id} value={workspace.workspace_id}>
                    {workspace.name}
                  </option>
                ))}
              </select>
            </div>

            <form onSubmit={onIngestSource} style={{ display: "flex", gap: 8 }}>
              <input
                value={sourcePath}
                onChange={(event) => setSourcePath(event.target.value)}
                placeholder="absolute document path to ingest"
                style={{
                  flex: 1,
                  border: "1px solid #ccd5df",
                  borderRadius: 10,
                  padding: "8px 10px",
                  fontSize: 14,
                }}
              />
              <Button
                type="submit"
                disabled={busy || !selectedWorkspace}
              >
                Ingest Source
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={busy || !selectedWorkspace || compileIsActiveForSelectedWorkspace}
                onClick={() => {
                  void onQueueCompile();
                }}
              >
                {compileIsActiveForSelectedWorkspace ? "Compile Running" : "Queue Compile"}
              </Button>
            </form>

            {pickImportPaths ? (
              <div style={{ display: "flex", gap: 8 }}>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy || !selectedWorkspace}
                  onClick={() => {
                    void onImportSelection("files");
                  }}
                >
                  Import Files
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy || !selectedWorkspace}
                  onClick={() => {
                    void onImportSelection("folder");
                  }}
                >
                  Import Folder
                </Button>
              </div>
            ) : null}

            <section
              style={{
                border: "1px solid #d7dce3",
                borderRadius: 12,
                padding: 12,
                display: "grid",
                gap: 10,
                background: "rgba(244, 249, 255, 0.72)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "center",
                  justifyContent: "space-between",
                  flexWrap: "wrap",
                }}
              >
                <div style={{ display: "grid", gap: 4 }}>
                  <strong>Watch Raw Folder</strong>
                  <small>
                    Watch mode is fixed to <code>workspace/raw</code>. Copy or move files into this folder to auto-ingest them.
                  </small>
                </div>
                <span
                  style={{
                    ...statusBadgeStyle(watchStatus?.enabled ? "running" : "idle"),
                    padding: "4px 8px",
                    borderRadius: 99,
                    fontWeight: 700,
                  }}
                >
                  {watchStatus?.enabled ? "watching" : "stopped"}
                </span>
              </div>

              <div
                style={{
                  border: "1px solid #d7dce3",
                  borderRadius: 10,
                  padding: "10px 12px",
                  background: "rgba(255, 255, 255, 0.85)",
                }}
              >
                <small>watched path</small>
                <div>
                  <code>{watchRootPath || "-"}</code>
                </div>
              </div>

              <section
                style={{
                  border: "1px solid #d7dce3",
                  borderRadius: 10,
                  padding: 12,
                  display: "grid",
                  gap: 10,
                  background: "rgba(255, 255, 255, 0.85)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    gap: 10,
                    alignItems: "center",
                    justifyContent: "space-between",
                    flexWrap: "wrap",
                  }}
                >
                  <div style={{ display: "grid", gap: 4 }}>
                    <strong>Pending Raw Files</strong>
                    <small>
                      Files already present in <code>workspace/raw</code> need your confirmation before ingest.
                    </small>
                  </div>
                  <span
                    style={{
                      padding: "4px 8px",
                      borderRadius: 99,
                      background: watchBacklogItems.length > 0 ? "#fff2d6" : "#eef2f6",
                      color: watchBacklogItems.length > 0 ? "#8a5a13" : "#4f6478",
                      fontWeight: 700,
                    }}
                  >
                    {watchBacklogItems.length} pending
                  </span>
                </div>

                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={busy || !selectedWorkspace}
                    onClick={() => {
                      void loadWatchBacklog(selectedWorkspace, true).catch((cause) => {
                        const message = cause instanceof Error ? cause.message : String(cause);
                        setWatchBacklogError(message);
                      });
                    }}
                  >
                    Refresh Pending Files
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={busy || watchBacklogItems.length === 0}
                    onClick={() => {
                      setSelectedBacklogPaths(watchBacklogItems.map((item) => item.path));
                    }}
                  >
                    Select All
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={busy || selectedBacklogPaths.length === 0}
                    onClick={() => {
                      setSelectedBacklogPaths([]);
                    }}
                  >
                    Clear Selection
                  </Button>
                  <Button
                    type="button"
                    disabled={busy || selectedBacklogPaths.length === 0}
                    onClick={() => {
                      void onIngestBacklogSelected();
                    }}
                  >
                    Ingest Selected
                  </Button>
                </div>

                {watchBacklogItems.length === 0 ? (
                  <small>No pending raw files awaiting confirmation.</small>
                ) : (
                  <div style={{ display: "grid", gap: 8 }}>
                    {watchBacklogItems.map((item) => {
                      const checked = selectedBacklogPaths.includes(item.path);
                      return (
                        <label
                          key={item.path}
                          style={{
                            display: "grid",
                            gap: 4,
                            border: "1px solid #d7dce3",
                            borderRadius: 10,
                            padding: "10px 12px",
                          }}
                        >
                          <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => {
                                onToggleBacklogPath(item.path);
                              }}
                              disabled={busy}
                            />
                            <strong>{item.name}</strong>
                            <small>
                              {item.size_bytes.toLocaleString()} bytes, updated {formatTimestamp(item.modified_at)}
                            </small>
                          </span>
                          <code>{item.path}</code>
                        </label>
                      );
                    })}
                  </div>
                )}

                {watchBacklogError ? (
                  <small style={{ color: "#a66d1f" }}>
                    backlog refresh failed: <code>{watchBacklogError}</code>
                  </small>
                ) : null}
              </section>

              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Button
                  type="button"
                  disabled={busy || !selectedWorkspace}
                  onClick={() => {
                    void onSaveWatch();
                  }}
                >
                  {watchStatus?.enabled ? "Update Watch" : "Start Watching"}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy || !selectedWorkspace || !watchStatus?.enabled}
                  onClick={() => {
                    void onStopWatch();
                  }}
                >
                  Stop Watching
                </Button>
              </div>

              <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={watchAutoCompile}
                    onChange={(event) => setWatchAutoCompile(event.target.checked)}
                    disabled={busy || !selectedWorkspace}
                  />
                  <span>Auto-compile</span>
                </label>

                <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span>Debounce (s)</span>
                  <input
                    value={watchDebounceSeconds}
                    onChange={(event) => setWatchDebounceSeconds(event.target.value)}
                    inputMode="decimal"
                    style={{
                      width: 90,
                      border: "1px solid #ccd5df",
                      borderRadius: 10,
                      padding: "8px 10px",
                    }}
                  />
                </label>
              </div>

              <div style={{ display: "grid", gap: 4 }}>
                <small>
                  root: <code>{watchRootPath || "-"}</code>
                </small>
                <small>
                  pending paths: <code>{watchStatus?.pending_paths ?? 0}</code>, auto-compile: <code>{watchStatus?.auto_compile ? "on" : "off"}</code>
                </small>
                <small>
                  last ingest job: <code>{watchStatus?.last_ingest_job_id ?? "-"}</code>, last compile job: <code>{watchStatus?.last_compile_job_id ?? "-"}</code>
                </small>
                <small>
                  active compile: <code>{watchStatus?.active_compile_job_id ?? "-"}</code>, updated: <code>{formatTimestamp(watchStatus?.updated_at)}</code>
                </small>
                {watchStatus?.last_error ? (
                  <small style={{ color: "#a6332b" }}>
                    watch error: <code>{watchStatus.last_error}</code>
                  </small>
                ) : null}
                {watchPollError ? (
                  <small style={{ color: "#a66d1f" }}>
                    watch poll retrying after error: <code>{watchPollError}</code>
                  </small>
                ) : null}
              </div>
            </section>

            <form onSubmit={onSaveCredentials} style={{ display: "grid", gap: 8 }}>
              <div style={{ display: "grid", gap: 8, gridTemplateColumns: "180px 1fr 1fr" }}>
                <select
                  value={provider}
                  onChange={(event) => {
                    const value = event.target.value;
                    setProvider(value);
                    const found = providers.find((item) => item.provider_id === value);
                    if (found && found.model_examples.length > 0) {
                      setModel(found.model_examples[0]);
                    }
                  }}
                  style={{ border: "1px solid #ccd5df", borderRadius: 10, padding: "8px 10px" }}
                >
                  {providers.map((item) => (
                    <option key={item.provider_id} value={item.provider_id}>
                      {item.label}
                    </option>
                  ))}
                </select>
                <input
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  placeholder="model name"
                  style={{ border: "1px solid #ccd5df", borderRadius: 10, padding: "8px 10px" }}
                />
                <input
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder="api key"
                  type="password"
                  style={{ border: "1px solid #ccd5df", borderRadius: 10, padding: "8px 10px" }}
                />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button
                  type="submit"
                  disabled={busy || !selectedWorkspace || !provider || !model || !apiKey.trim()}
                >
                  Save Credentials
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy || !selectedWorkspace || !credentialStatus?.has_api_key}
                  onClick={() => {
                    void onValidateCredentials();
                  }}
                >
                  Validate
                </Button>
                <div style={{ alignSelf: "center", display: "grid", gap: 2 }}>
                  <span>
                    credentials: {credentialStatus?.has_api_key ? "saved" : "missing"}, validated: {credentialStatus?.validated ? "yes" : "no"}
                  </span>
                  <span>
                    configured provider: <code>{credentialStatus?.provider ?? "-"}</code>, model: <code>{credentialStatus?.model ?? "-"}</code>
                  </span>
                </div>
              </div>
            </form>

            {actionInfo ? (
              <p style={{ margin: 0, color: "#0f6b2e" }}>
                Action: <code>{actionInfo}</code>
              </p>
            ) : null}

            {error ? (
              <p style={{ margin: 0, color: "#a6332b" }}>
                Error: <code>{error}</code>
              </p>
            ) : null}
          </section>

          {activeCompileWorkspaceRef === selectedWorkspace && (visibleCompileJob || compilePollError) ? (
            <section style={{ ...surfaceStyle, display: "grid", gap: 12 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <h2 style={{ margin: 0 }}>Compile Progress</h2>
                {visibleCompileJob ? (
                  <span
                    style={{
                      ...statusBadgeStyle(visibleCompileJob.status),
                      padding: "4px 8px",
                      borderRadius: 99,
                      fontWeight: 700,
                    }}
                  >
                    {visibleCompileJob.status}
                  </span>
                ) : null}
                {visibleCompileJob ? <code>{visibleCompileJob.job_id}</code> : null}
              </div>

              {visibleCompileJob ? (
                <>
                  <div style={{ display: "grid", gap: 6 }}>
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        gap: 12,
                        alignItems: "center",
                        flexWrap: "wrap",
                      }}
                    >
                      <strong>{formatProgress(visibleCompileJob.progress)}</strong>
                      <span>
                        stage: <code>{visibleCompileJob.stage ?? "-"}</code>
                      </span>
                    </div>
                    <div
                      style={{
                        height: 10,
                        borderRadius: 999,
                        overflow: "hidden",
                        background: "#e8eef5",
                      }}
                    >
                      <div
                        style={{
                          width: formatProgress(visibleCompileJob.progress),
                          height: "100%",
                          background: "linear-gradient(90deg, #4d93ff 0%, #1f57c3 100%)",
                        }}
                      />
                    </div>
                  </div>

                  <div style={{ display: "grid", gap: 4 }}>
                    <span>
                      message: <code>{visibleCompileJob.message ?? "-"}</code>
                    </span>
                    <span>
                      runtime: <code>{compileProvider ?? "-"}</code> / <code>{compileModel ?? "-"}</code>
                    </span>
                    <span>
                      current counter: <code>{formatCounter(currentStageCounter)}</code>
                    </span>
                  </div>

                  {visibleCompileJob.compile?.plan ? (
                    <div style={{ display: "grid", gap: 8 }}>
                      <strong>Planned Outputs</strong>
                      <span>{formatPlanSummary(visibleCompileJob.compile.plan)}</span>
                      <div style={{ display: "grid", gap: 8 }}>
                        {visibleCompileJob.compile.plan.documents.map((document) => (
                          <details
                            key={document.document_name}
                            style={{ border: "1px solid #d7dce3", borderRadius: 10, padding: 10 }}
                          >
                            <summary style={{ cursor: "pointer", fontWeight: 600 }}>
                              {document.document_name}
                            </summary>
                            <div style={{ display: "grid", gap: 6, marginTop: 10 }}>
                              <small>topics: {planBucketPreview(document.topics)}</small>
                              {document.topics.create.length > 0 ? (
                                <small>topic create preview: {previewItems(document.topics.create)}</small>
                              ) : null}
                              {document.topics.update.length > 0 ? (
                                <small>topic update preview: {previewItems(document.topics.update)}</small>
                              ) : null}
                              {document.topics.related.length > 0 ? (
                                <small>topic related preview: {document.topics.related.join(", ")}</small>
                              ) : null}

                              <small>regulations: {planBucketPreview(document.regulations)}</small>
                              {document.regulations.create.length > 0 ? (
                                <small>
                                  regulation create preview: {previewItems(document.regulations.create)}
                                </small>
                              ) : null}
                              {document.regulations.update.length > 0 ? (
                                <small>
                                  regulation update preview: {previewItems(document.regulations.update)}
                                </small>
                              ) : null}
                              {document.regulations.related.length > 0 ? (
                                <small>
                                  regulation related preview: {document.regulations.related.join(", ")}
                                </small>
                              ) : null}

                              <small>procedures: {planBucketPreview(document.procedures)}</small>
                              {document.procedures.create.length > 0 ? (
                                <small>
                                  procedure create preview: {previewItems(document.procedures.create)}
                                </small>
                              ) : null}
                              {document.procedures.update.length > 0 ? (
                                <small>
                                  procedure update preview: {previewItems(document.procedures.update)}
                                </small>
                              ) : null}
                              {document.procedures.related.length > 0 ? (
                                <small>
                                  procedure related preview: {document.procedures.related.join(", ")}
                                </small>
                              ) : null}

                              <small>conflicts: {planBucketPreview(document.conflicts)}</small>
                              {document.conflicts.create.length > 0 ? (
                                <small>
                                  conflict create preview: {previewItems(document.conflicts.create)}
                                </small>
                              ) : null}
                              {document.conflicts.update.length > 0 ? (
                                <small>
                                  conflict update preview: {previewItems(document.conflicts.update)}
                                </small>
                              ) : null}
                              {document.conflicts.related.length > 0 ? (
                                <small>
                                  conflict related preview: {document.conflicts.related.join(", ")}
                                </small>
                              ) : null}

                              <small>evidence: {document.evidence_count}</small>
                            </div>
                          </details>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <div style={{ display: "grid", gap: 6 }}>
                    <strong>Token Usage</strong>
                    <span>
                      total: <code>{formatUsage(visibleCompileJob.compile?.usage_total)}</code>
                    </span>
                    {Object.entries(visibleCompileJob.compile?.usage_by_stage ?? {}).length > 0 ? (
                      <div style={{ display: "grid", gap: 4 }}>
                        {Object.entries(visibleCompileJob.compile?.usage_by_stage ?? {}).map(
                          ([stage, usage]) => (
                            <small key={stage}>
                              {stage}: {formatUsage(usage)}
                            </small>
                          ),
                        )}
                      </div>
                    ) : (
                      <small>No stage usage reported yet.</small>
                    )}
                  </div>

                  {visibleCompileJob.status === "failed" && visibleCompileJob.error ? (
                    <p style={{ margin: 0, color: "#a6332b" }}>
                      Failure: <code>{visibleCompileJob.error}</code>
                    </p>
                  ) : null}
                </>
              ) : null}

              {compilePollError ? (
                <p style={{ margin: 0, color: "#a66d1f" }}>
                  Polling retrying after error: <code>{compilePollError}</code>
                </p>
              ) : null}
            </section>
          ) : null}

          <section style={surfaceStyle}>
            <h2 style={{ marginTop: 0 }}>Documents ({documents.length})</h2>
            <div style={{ display: "grid", gap: 10, marginBottom: 16 }}>
              {documents.length === 0 ? (
                <p style={{ margin: 0 }}>
                  No document indexed for selected workspace.
                </p>
              ) : null}
              {documents.map((document) => (
                <article
                  key={document.doc_id}
                  style={{
                    border: "1px solid #d7dce3",
                    borderRadius: 12,
                    padding: 12,
                    display: "grid",
                    gap: 6,
                  }}
                >
                  <strong>{document.name}</strong>
                  <small>
                    type={document.file_type}, status={document.status}, pageindex=
                    {document.requires_pageindex ? "yes" : "no"}
                  </small>
                  <code>{document.source_path ?? "-"}</code>
                </article>
              ))}
            </div>

            <h2 style={{ marginTop: 0 }}>Workspaces ({workspaces.length})</h2>
            <div style={{ display: "grid", gap: 10 }}>
              {workspaces.length === 0 ? (
                <p style={{ margin: 0 }}>No workspace yet. Create one above.</p>
              ) : null}
              {workspaces.map((workspace) => (
                <article
                  key={workspace.workspace_id}
                  style={{
                    border: "1px solid #d7dce3",
                    borderRadius: 12,
                    padding: 12,
                    display: "grid",
                    gap: 6,
                  }}
                >
                  <strong>{workspace.name}</strong>
                  <code>{workspace.root_path}</code>
                  <span>
                    initialized: {workspace.initialized ? "yes" : "no"}
                  </span>
                  {workspace.status ? (
                    <small>
                      docs={workspace.status.indexed_documents}, raw=
                      {workspace.status.raw_files}, sources=
                      {workspace.status.source_pages}, compiled={workspace.status.compiled_documents}, evidence=
                      {workspace.status.evidence_pages}, conflicts={workspace.status.conflict_pages}, credentials=
                      {workspace.status.credentials_ready ? "ready" : "missing"}, jobs(queued/completed/failed)=
                      {workspace.status.queued_jobs}/{workspace.status.completed_jobs}/
                      {workspace.status.failed_jobs}
                    </small>
                  ) : null}
                </article>
              ))}
            </div>
          </section>
        </section>
      </main>
    </UIProvider>
  );
}
