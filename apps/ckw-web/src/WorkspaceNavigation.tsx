import type { Workspace } from "./workspaceRouting";

type WorkspaceNavigationProps = {
  current: Workspace;
  onNavigate: (workspace: Workspace) => void;
};

export function WorkspaceNavigation({ current, onNavigate }: WorkspaceNavigationProps) {
  const workspaces: Array<{ id: Workspace; label: string }> = [
    { id: "leg", label: "LEG-Potenzial" },
    { id: "vzev", label: "vZEV-Simulation & Batterie" },
    { id: "billing", label: "Monatliche vZEV-Abrechnung" },
  ];

  return (
    <nav className="workspaceNav" aria-label="Arbeitsbereiche">
      {workspaces.map((workspace) => (
        <button
          key={workspace.id}
          type="button"
          className={current === workspace.id ? "active" : undefined}
          aria-current={current === workspace.id ? "page" : undefined}
          onClick={() => onNavigate(workspace.id)}
        >
          {workspace.label}
        </button>
      ))}
    </nav>
  );
}
