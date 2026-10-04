import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const nginxConfig = fileURLToPath(new URL('../../nginx.conf', import.meta.url))

test('OAuth callback credentials never enter proxy logs, including unavailable upstream', { skip: !process.env.RUN_DOCKER_TESTS }, async () => {
  const name = `jarvis-oauth-log-test-${process.pid}`
  const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8' }).trim()
  try {
    docker('run', '--rm', '-d', '--name', name, '--add-host', 'jarvis-backend:127.0.0.1',
      '-p', '127.0.0.1::80', '-v', `${nginxConfig}:/etc/nginx/conf.d/default.conf:ro`, 'nginx:1.27-alpine')
    docker('exec', name, 'nginx', '-t')
    const base = `http://${docker('port', name, '80/tcp').split('\n')[0]}`
    for (const path of ['/api/plugins/oauth/callback', '/api/plugins/oauth/callback/']) {
      const response = await fetch(`${base}${path}?code=test-only-sensitive-code&state=test-only-sensitive-state`, { redirect: 'manual' })
      assert.equal(response.status, 502)
    }
    await fetch(base + '/ordinary-observable-request')
    const captured = spawnSync('docker', ['logs', name], { encoding: 'utf8' })
    assert.equal(captured.status, 0)
    const logs = captured.stdout + captured.stderr
    assert.doesNotMatch(logs, /test-only-sensitive-code|test-only-sensitive-state/)
    assert.match(logs, /GET \/api\/plugins\/oauth\/callback.*502/)
    assert.match(logs, /ordinary-observable-request/)
  } finally {
    docker('stop', name)
  }
})
