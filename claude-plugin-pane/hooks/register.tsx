// EXPERIMENTAL companion of the ai-agents-orchestrator plugin. It uses Claude Code's early-access function-hooks API
// (panes), which may change between releases. Read-only: it only runs `aao watch <id> --no-follow` and draws the
// output; it never changes the task.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { CodexFeed } from '../types'

const PANE = 'aao-codex'
const EMPTY: CodexFeed = { taskId: '', lines: [], updatedAt: 0, error: '' }
const feed = atom({ plugin: 'aao-codex-pane', key: 'feed' } as const, EMPTY, { shape: 'feed-1' })

export const register: Register = on => {
  let timer: { cancel: () => void } | undefined

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'codex-pane',
      description: 'Live side pane with what Codex is doing on a task (default: the running one)',
    })
    return next(e)
  })

  on('command.run', { command: 'codex-pane' }, async ($, e) => {
    const taskId = e.args.trim().replace(/[^0-9]/g, '')
    await update($, feed, () => ({ ...EMPTY, taskId }))
    const refresh = async () => {
      const argv = ['aao', 'watch', ...(taskId ? [taskId] : []), '--no-follow']
      try {
        const { exitCode, stdout, stderr } = await $.process.run(argv, { timeoutMs: 20000 })
        const lines = stdout.split('\n').filter(l => l.length > 0).slice(-400)
        await update($, feed, f => ({ ...f, lines, updatedAt: Date.now(), error: exitCode === 0 ? '' : stderr.slice(-300) }))
      } catch (err) {
        await update($, feed, f => ({ ...f, error: 'could not run `aao` (is it on PATH?)' }))
      }
    }
    timer?.cancel()
    await refresh()
    timer = $.clock.every(2000, () => void refresh())
    const { isPlaced } = await $.ui.open({ id: PANE, title: taskId ? `Codex #${taskId}` : 'Codex' })
    return { text: isPlaced ? 'Codex live pane opened.' : 'Codex pane is queued until the window is wide enough; `aao watch-open` opens it in a terminal instead.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const f = await read($, feed)
    const room = Math.max(1, (e.viewport?.rows ?? 24) - 3)
    return (
      <Box flexDirection="column">
        {f.error !== '' && <Text color="yellow">{f.error}</Text>}
        {f.lines.length === 0 && f.error === '' && <Text dimColor>Waiting for Codex events...</Text>}
        {f.lines.slice(-room).map(line => (
          <Text dimColor={line.startsWith('==')}>{line}</Text>
        ))}
      </Box>
    )
  })
}
