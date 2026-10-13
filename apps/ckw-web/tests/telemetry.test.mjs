import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sanitizeEvent, endpointTemplate } from '../../shared/telemetry.js';

test('reconstructs browser events without private data, preserving status and code location', () => {
  const event = sanitizeEvent({
    message: 'private.csv secret@example.com', request: { headers: { authorization: 'private' }, data: 'private' },
    user: { email: 'private' }, breadcrumbs: [{ message: 'private' }], contexts: { private: true }, extra: { token: 'private' },
    tags: { operation: 'http', endpoint: '/v1/ckw/projects/private/results/private?token=private', method: 'POST', status: 401, project: 'private' },
    exception: { values: [{ type: 'Error', value: 'private', stacktrace: { frames: [
      { filename: 'https://ckw.communi.ch/assets/index-hash.js?token=private', lineno: 10, vars: { private: true }, context_line: 'private' },
    ] } }] },
  }, 'ckw-web');
  assert.ok(!JSON.stringify(event).includes('private'));
  assert.equal(event.tags.endpoint, '/v1/ckw/projects/:id/results/:id');
  assert.equal(event.tags.status, '401');
  assert.equal(event.exception.values[0].stacktrace.frames[0].lineno, 10);
  assert.equal(endpointTemplate('https://example.invalid/api/vzev/invoices?token=x#/private'), '/api/vzev/invoices');
});
