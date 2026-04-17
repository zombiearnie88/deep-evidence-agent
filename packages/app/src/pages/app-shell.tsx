import { UIProvider } from "@evidence-brain/ui";
import { type CSSProperties, type FormEvent, useEffect, useMemo, useState } from "react";

type WorkspaceItem = {
  workspace_id: string;
  name: string;
  root_path: string;
  initialized: boolean;
  status?: {
    indexed_documents: number;
    raw_files: number;
    source_pages: number;
    queued_jobs: number;
    completed_jobs: number;
    failed_jobs: number;
  } | null;
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
};

const surfaceStyle: CSSProperties = {
  border: "1px solid #d7dce3",
  borderRadius: 14,
  padding: 16,
  background: "rgba(255, 255, 255, 0.92)",
  boxShadow: "0 8px 24px rgba(8, 30, 52, 0.08)",
};

const buttonStyle: CSSProperties = {
  border: "1px solid #123a5b",
  borderRadius: 10,
  background: "#123a5b",
  color: "#ffffff",
  padding: "8px 12px",
  fontWeight: 600,
  cursor: "pointer",
};

const mutedButtonStyle: CSSProperties = {
  ...buttonStyle,
  borderColor: "#bcc7d3",
  background: "#f2f5f8",
  color: "#213547",
};

export function AppShell() {
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
  const [actionInfo, setActionInfo] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string>("");

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

  useEffect(() => {
    void loadOverview();
  }, [apiBase]);

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

  useEffect(() => {
    if (!selectedWorkspace) {
      return;
    }
    void loadDocuments(selectedWorkspace);
  }, [apiBase, selectedWorkspace]);

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

  const onIngestSource = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedWorkspace || !sourcePath.trim()) {
      return;
    }

    setBusy(true);
    setError("");
    setActionInfo("");
    try {
      const response = await fetch(`${apiBase}/documents/ingest`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          workspace: selectedWorkspace,
          path: sourcePath.trim(),
        }),
      });
      if (!response.ok) {
        throw new Error(`ingest failed: ${response.status}`);
      }

      const payload = (await response.json()) as {
        discovered_files: number;
        added_documents: DocumentItem[];
      };
      setActionInfo(
        `ingest done: discovered=${payload.discovered_files}, added=${payload.added_documents.length}`,
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
        throw new Error(`queue compile failed: ${response.status}`);
      }
      const payload = (await response.json()) as CompileResponse;
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
              <button type="submit" style={buttonStyle} disabled={busy}>
                Create Workspace
              </button>
              <button
                type="button"
                style={mutedButtonStyle}
                disabled={busy}
                onClick={() => {
                  void loadOverview();
                }}
              >
                Refresh
              </button>
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
              <button
                type="submit"
                style={buttonStyle}
                disabled={busy || !selectedWorkspace}
              >
                Ingest Source
              </button>
              <button
                type="button"
                style={mutedButtonStyle}
                disabled={busy || !selectedWorkspace}
                onClick={() => {
                  void onQueueCompile();
                }}
              >
                Queue Compile
              </button>
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
                      {workspace.status.source_pages}, jobs(queued/completed/failed)=
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
