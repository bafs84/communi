// Rebuild events from an allowlist: uploaded data must never reach error tracking.
const routes = new Set(['api', 'v1', 'ckw', 'projects', 'files', 'results', 'guest', 'session', 'claim', 'vzev', 'invoices', 'me', 'workspace', 'profile', 'uploads', 'questions', 'calculators', 'sunshine-share', 'municipality-signals', 'municipalities', 'health', 'ready', 'live', 'config', 'plants', 'manual', 'cached', 'selection', 'timeseries', 'summary', 'simulate', 'export', 'leg', 'battery', 'analysis', 'tariffs', 'report', 'operations', 'download', 'solar-files', 'consumption-files', 'zev-files', 'adopt-automatic', 'cache', 'battery-placements', 'pv-sites', 'consumers']);

export function endpointTemplate(input) {
  try {
    return new URL(input, 'https://communi.invalid').pathname.split('/').map(part =>
      !part || routes.has(part) ? part : ':id').join('/');
  } catch { return '/unknown'; }
}

export function sanitizeEvent(event, service) {
  const tags = { service };
  const source = event.tags || {};
  if (source.endpoint) tags.endpoint = endpointTemplate(String(source.endpoint));
  if (/^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$/.test(String(source.method))) tags.method = source.method;
  if (/^[45]\d\d$/.test(String(source.status))) tags.status = String(source.status);
  if (['guest', 'registered'].includes(source.access_mode)) tags.access_mode = source.access_mode;
  if (['recovered', 'failed'].includes(source.recovery)) tags.recovery = source.recovery;
  // Context labels are fixed application constants, never exception messages.
  const operations = ['http', 'network', 'billing', 'billing-history', 'calculator', 'access', 'react-render', 'workspace', 'sunshine', 'auth', 'smoke-test'];
  tags.operation = operations.includes(source.operation) ? source.operation : 'unhandled';
  const clean = {
    event_id: event.event_id, timestamp: event.timestamp, platform: 'javascript',
    level: event.level || 'error', release: event.release, environment: event.environment,
    user: { ip_address: '0.0.0.0' },
    tags, message: `Communi ${service}: ${tags.operation}${tags.status ? ` HTTP ${tags.status}` : ''}`,
    fingerprint: [service, tags.operation, tags.endpoint || '', tags.status || ''],
  };
  if (event.exception?.values) {
    clean.fingerprint = ["{{ default }}", service, tags.operation];
    clean.exception = { values: event.exception.values.map(value => ({
      type: ['Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError', 'URIError', 'EvalError', 'UnhandledRejection'].includes(value.type) ? value.type : 'Error',
      value: '[redacted]',
      stacktrace: { frames: (value.stacktrace?.frames || []).filter(frame => {
        try { return /^\/(?:assets|_astro)\/[A-Za-z0-9_.-]+\.m?js$/.test(new URL(frame.filename, 'https://communi.invalid').pathname); }
        catch { return false; }
      }).map(frame => ({
        // Only static code asset paths, never source context, URLs or variables.
        filename: new URL(frame.filename, 'https://communi.invalid').pathname.replace(/^.*\/(assets|_astro)\//, '$1/'),
        lineno: frame.lineno, colno: frame.colno, in_app: frame.in_app,
      })) },
    })) };
  }
  return clean;
}
