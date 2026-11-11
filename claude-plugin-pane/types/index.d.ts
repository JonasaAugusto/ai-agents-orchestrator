export type CodexFeed = { taskId: string; lines: string[]; updatedAt: number; error: string }

declare module 'claude-code' {
  interface PluginState {
    'aao-codex-pane': { feed: Shaped<CodexFeed> }
  }
}
