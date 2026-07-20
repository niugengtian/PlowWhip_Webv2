import { createHash, randomUUID } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createConnection, createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, spawnSync } from 'node:child_process'

const webDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const rootDir = resolve(webDir, '..')
const startedAt = new Date().toISOString()
const runId = randomUUID()
const head = command('git', ['rev-parse', 'HEAD'], rootDir).stdout.trim()
const evidenceDir = join(
  rootDir,
  'runtime',
  'evidence',
  'browser-acceptance',
  `${startedAt.replaceAll(':', '-').replaceAll('.', '-')}-${head.slice(0, 12)}`,
)
const dataDir = mkdtempSync(join(tmpdir(), 'plow-whip-browser-acceptance-'))
const seedPath = join(evidenceDir, 'seed.json')
const observationsPath = join(evidenceDir, 'observations.json')
const manifestPath = join(evidenceDir, 'browser-gate.json')
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const venvPython = join(rootDir, '.venv', 'bin', 'python')
const pythonExecutable = process.env.PLOW_WHIP_E2E_PYTHON
  || (existsSync(venvPython) ? venvPython : 'python3')
const argv = [process.execPath, 'e2e/run-acceptance.mjs']
let backend
let port
let playwrightExitCode = 1
let runnerError = null
let portReleased = false
let databaseRemoved = false
let stdoutFd
let stderrFd

mkdirSync(evidenceDir, { recursive: true })

try {
  if (!existsSync(join(webDir, 'dist', 'index.html'))) {
    throw new Error('web/dist/index.html is missing; run the frontend build gate first')
  }
  if (!existsSync(chromePath)) {
    throw new Error(`installed Chrome not found: ${chromePath}`)
  }

  port = await freePort()
  if (port === 8742) throw new Error('ephemeral port allocator returned protected port 8742')

  const pythonEnv = {
    ...process.env,
    PYTHONPATH: join(rootDir, 'backend'),
  }
  command(
    pythonExecutable,
    ['web/e2e/seed_candidate.py', '--data-dir', dataDir, '--output', seedPath],
    rootDir,
    pythonEnv,
  )

  stdoutFd = openSync(join(evidenceDir, 'candidate.stdout.log'), 'a')
  stderrFd = openSync(join(evidenceDir, 'candidate.stderr.log'), 'a')
  backend = spawn(
    pythonExecutable,
    ['-m', 'plow_whip_web', '--host', '127.0.0.1', '--port', String(port), '--data-dir', dataDir],
    {
      cwd: rootDir,
      env: pythonEnv,
      stdio: [
        'ignore',
        stdoutFd,
        stderrFd,
      ],
    },
  )
  await waitForHealth(port, backend)

  const playwright = spawn(
    process.execPath,
    ['node_modules/@playwright/test/cli.js', 'test', '--config=e2e/playwright.config.ts'],
    {
      cwd: webDir,
      env: {
        ...process.env,
        E2E_BASE_URL: `http://127.0.0.1:${port}`,
        E2E_CANDIDATE_HEAD: head,
        E2E_CHROME_PATH: chromePath,
        E2E_DATA_DIR: dataDir,
        E2E_EVIDENCE_DIR: evidenceDir,
        E2E_OBSERVATIONS_PATH: observationsPath,
        E2E_PORT: String(port),
        E2E_RUN_ID: runId,
        E2E_SEED_PATH: seedPath,
      },
      stdio: 'inherit',
    },
  )
  playwrightExitCode = await exitCode(playwright)
} catch (error) {
  runnerError = error instanceof Error ? error.stack || error.message : String(error)
} finally {
  if (backend) await stopProcess(backend)
  if (stdoutFd !== undefined) closeSync(stdoutFd)
  if (stderrFd !== undefined) closeSync(stderrFd)
  if (port) portReleased = !(await canConnect(port))
  rmSync(dataDir, { recursive: true, force: true })
  databaseRemoved = !existsSync(dataDir)
}

const observations = readJson(observationsPath)
const evidenceConflict = Boolean(
  playwrightExitCode === 0
  && (
    !observations
    || observations.candidateHead !== head
    || observations.port !== port
    || observations.databasePath !== dataDir
    || observations.consoleErrors?.length
    || observations.pageErrors?.length
    || observations.unexpectedNetworkFailures?.length
    || observations.completedAcceptanceIds?.length !== 9
  )
)
const failedAcceptanceIds = observations?.failedAcceptanceIds || (
  playwrightExitCode === 0 && !evidenceConflict ? [] : ['BROWSER-GATE']
)
const passed = (
  playwrightExitCode === 0
  && !runnerError
  && !evidenceConflict
  && portReleased
  && databaseRemoved
)
const reasonCodes = [
  ...(playwrightExitCode === 0 ? [] : ['browser_suite_failed']),
  ...(runnerError ? ['runner_error'] : []),
  ...(evidenceConflict ? ['evidence_conflict'] : []),
  ...(portReleased ? [] : ['candidate_port_not_released']),
  ...(databaseRemoved ? [] : ['temporary_database_not_removed']),
]
const finishedAt = new Date().toISOString()
const artifacts = artifactRecords(evidenceDir, manifestPath)
const manifest = {
  schema_version: 1,
  verdict: passed ? 'PASS' : 'CHANGES_REQUIRED',
  passed,
  reason_codes: reasonCodes,
  failed_acceptance_ids: failedAcceptanceIds,
  started_at: startedAt,
  finished_at: finishedAt,
  completed_at: passed ? finishedAt : null,
  candidate: {
    head,
    browser_version: observations?.browserVersion || null,
    viewports: observations?.viewports || [],
    database_path: dataDir,
    database_removed: databaseRemoved,
    service_port: port || null,
    port_released: portReleased,
    protected_port: 8742,
  },
  gate: {
    acceptance_id: 'BROWSER-GATE',
    argv,
    cwd: webDir,
    exit_code: passed ? 0 : 1,
    playwright_exit_code: playwrightExitCode,
    host_job_id: process.env.PLOW_WHIP_HOST_JOB_ID || null,
    run_id: process.env.PLOW_WHIP_RUN_ID || runId,
    session_id: process.env.PLOW_WHIP_EXTERNAL_SESSION_ID || null,
    summary: passed
      ? 'Real candidate DOM browser acceptance passed'
      : 'Real candidate DOM browser acceptance requires changes',
  },
  assertions: observations?.assertions || [],
  diagnostics: {
    console_errors: observations?.consoleErrors || [],
    page_errors: observations?.pageErrors || [],
    unexpected_network_failures: observations?.unexpectedNetworkFailures || [],
    expected_cancellations: observations?.expectedCancellations || [],
    runner_error: runnerError,
  },
  evidence: artifacts,
}
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`)
console.log(`EVIDENCE_MANIFEST=${manifestPath}`)
console.log(`VERDICT=${manifest.verdict}`)
process.exitCode = passed ? 0 : 1

function command(executable, args, cwd, env = process.env) {
  const result = spawnSync(executable, args, { cwd, env, encoding: 'utf-8' })
  if (result.status !== 0) {
    const tail = `${result.stdout || ''}\n${result.stderr || ''}`.trim().split('\n').slice(-20).join('\n')
    throw new Error(`${executable} ${args.join(' ')} failed (${result.status})\n${tail}`)
  }
  return result
}

async function freePort() {
  const server = createServer()
  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolvePromise)
  })
  const address = server.address()
  const selected = typeof address === 'object' && address ? address.port : 0
  await new Promise((resolvePromise) => server.close(resolvePromise))
  if (!selected) throw new Error('failed to allocate a temporary port')
  return selected
}

async function waitForHealth(selectedPort, child) {
  const deadline = Date.now() + 20_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`candidate exited early (${child.exitCode})`)
    try {
      const response = await fetch(`http://127.0.0.1:${selectedPort}/health`)
      if (response.ok) return
    } catch {
      // Candidate is still starting.
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
  }
  throw new Error('candidate health did not become ready within 20 seconds')
}

function exitCode(child) {
  return new Promise((resolvePromise) => {
    child.once('exit', (code) => resolvePromise(code ?? 1))
    child.once('error', () => resolvePromise(1))
  })
}

async function stopProcess(child) {
  if (child.exitCode !== null) return
  child.kill('SIGTERM')
  const stopped = await Promise.race([
    exitCode(child).then(() => true),
    new Promise((resolvePromise) => setTimeout(() => resolvePromise(false), 3_000)),
  ])
  if (!stopped && child.exitCode === null) {
    child.kill('SIGKILL')
    await exitCode(child)
  }
}

function canConnect(selectedPort) {
  return new Promise((resolvePromise) => {
    const socket = createConnection({ host: '127.0.0.1', port: selectedPort })
    socket.setTimeout(500)
    socket.once('connect', () => {
      socket.destroy()
      resolvePromise(true)
    })
    const unavailable = () => {
      socket.destroy()
      resolvePromise(false)
    }
    socket.once('error', unavailable)
    socket.once('timeout', unavailable)
  })
}

function readJson(path) {
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, 'utf-8'))
  } catch {
    return null
  }
}

function artifactRecords(directory, excludedPath) {
  const records = []
  const walk = (current) => {
    for (const name of readdirSync(current)) {
      const path = join(current, name)
      if (path === excludedPath) continue
      if (statSync(path).isDirectory()) walk(path)
      else records.push({
        path: relative(rootDir, path),
        sha256: createHash('sha256').update(readFileSync(path)).digest('hex'),
      })
    }
  }
  walk(directory)
  return records.sort((left, right) => left.path.localeCompare(right.path))
}
