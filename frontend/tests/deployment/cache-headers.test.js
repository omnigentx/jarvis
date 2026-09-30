import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const nginxConfig = fileURLToPath(new URL('../../nginx.conf', import.meta.url))

test('Nginx revalidates SPA entry documents while preserving hashed asset caching', { skip: !process.env.RUN_DOCKER_TESTS }, async () => {
  const root = mkdtempSync(join(tmpdir(), 'jarvis-cache-test-'))
  const name = `jarvis-cache-test-${process.pid}`
  const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8' }).trim()
  try {
    mkdirSync(join(root, 'assets'))
    writeFileSync(join(root, 'index.html'), '<html>release-entry</html>')
    writeFileSync(join(root, 'assets', 'entry-123.js'), 'export default 1')
    docker('run', '--rm', '-d', '--name', name, '--add-host', 'jarvis-backend:127.0.0.1',
      '-p', '127.0.0.1::80', '-v', `${resolve(root)}:/usr/share/nginx/html:ro`,
      '-v', `${nginxConfig}:/etc/nginx/conf.d/default.conf:ro`, 'nginx:1.27-alpine')
    docker('exec', name, 'nginx', '-t')
    const address = docker('port', name, '80/tcp').split('\n')[0]
    const base = `http://${address}`
    for (const path of ['/', '/index.html', '/meetings', '/settings?tab=general']) {
      const response = await fetch(base + path)
      assert.equal(response.status, 200, path)
      assert.equal(response.headers.get('cache-control'), 'no-store', path)
      assert.equal(response.headers.get('x-content-type-options'), 'nosniff', path)
      assert.match(await response.text(), /release-entry/, path)
    }
    const asset = await fetch(base + '/assets/entry-123.js')
    assert.equal(asset.status, 200)
    assert.match(asset.headers.get('cache-control'), /immutable/)
    const missing = await fetch(base + '/assets/entry-missing.js')
    assert.equal(missing.status, 404)
    assert.doesNotMatch(missing.headers.get('cache-control') || '', /immutable/)
  } finally {
    try { docker('stop', name) } finally { rmSync(root, { recursive: true, force: true }) }
  }
})
