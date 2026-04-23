import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { open } from "@tauri-apps/plugin-dialog";

import "./styles.css";
import { AppShell, type ImportSelectionMode } from "@evidence-brain/app";

const pickImportPaths = async (mode: ImportSelectionMode): Promise<string[]> => {
  const selection = await open({
    multiple: mode === "files",
    directory: mode === "folder",
  });
  if (selection == null) {
    return [];
  }
  if (Array.isArray(selection)) {
    return selection.filter((item): item is string => typeof item === "string");
  }
  return typeof selection === "string" ? [selection] : [];
};

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppShell pickImportPaths={pickImportPaths} />
  </StrictMode>,
);
