import * as Sentry from '@sentry/browser';
import { sanitizeEvent } from '../../../shared/telemetry.js';

declare global {
    interface Window {
        // eslint-disable-next-line no-unused-vars
        __COMMUNI_REPORT_ERROR__?: (error: unknown, operation: string) => void;
    }
}

const config = (window as unknown as { __COMMUNI_CONFIG__?: Record<string, string> }).__COMMUNI_CONFIG__;
if (config?.glitchtipDsn) {
    Sentry.init({
        dsn: config.glitchtipDsn, tunnel: "/telemetry/envelope", environment: config.glitchtipEnvironment, release: config.release,
        sendDefaultPii: false, sendClientReports: false, maxBreadcrumbs: 0,
        tracesSampleRate: 0, sampleRate: 1,
        integrations: defaults => defaults.filter(item => !['Breadcrumbs', 'BrowserSession', 'HttpContext'].includes(item.name)),
        beforeSend: event => sanitizeEvent(event, 'site'),
    });
}
window.__COMMUNI_REPORT_ERROR__ = (error, operation) => {
    Sentry.captureException(error instanceof Error ? error : new Error('Handled application failure'), { tags: { operation } });
};
