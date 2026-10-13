import { Component, type ReactNode } from 'react';
import { reportError } from './observability';

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error) { reportError(error, 'react-render'); }
  render() {
    return this.state.failed
      ? <div className="accessScreen"><h1>Ein Fehler ist aufgetreten</h1><p>Bitte den Arbeitsbereich neu laden. Der technische Fehler wurde erfasst.</p></div>
      : this.props.children;
  }
}
