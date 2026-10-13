import * as Sentry from '@sentry/browser';
import { sanitizeEvent } from '../../shared/telemetry.js';

export function initializeObservability() {
  const config = window.__COMMUNI_CONFIG__;
  if (!config?.glitchtipDsn) return;
  Sentry.init({
    dsn: config.glitchtipDsn, tunnel: "/telemetry/envelope", environment: config.glitchtipEnvironment, release: config.release,
    sendDefaultPii: false, sendClientReports: false, maxBreadcrumbs: 0,
    tracesSampleRate: 0, sampleRate: 1,
    integrations: defaults => defaults.filter(item => !['Breadcrumbs', 'BrowserSession', 'HttpContext'].includes(item.name)),
    beforeSend: event => sanitizeEvent(event, 'ckw-web'),
  });
}

export function reportError(error: unknown, operation: string) {
  Sentry.captureException(error instanceof Error ? error : new Error('Handled application failure'), { tags: { operation } });
}

export function reportHttpFailure(endpoint: string, method: string, status: number, accessMode: string, recovery?: string) {
  Sentry.captureMessage('HTTP request failed', {
    level: status >= 500 ? 'error' : 'warning',
    tags: { operation: 'http', endpoint, method, status, access_mode: accessMode, recovery },
  });
}
