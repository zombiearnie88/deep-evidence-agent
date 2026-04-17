import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AppShell } from "@evidence-brain/app";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppShell />
  </StrictMode>,
);
