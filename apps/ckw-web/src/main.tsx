import React from "react";
import { initializeObservability } from "./observability";
import { ErrorBoundary } from "./ErrorBoundary";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { AccessGate } from "./AccessGate";
import "./styles.css";

initializeObservability();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary><AccessGate><App /></AccessGate></ErrorBoundary>
  </React.StrictMode>
);
