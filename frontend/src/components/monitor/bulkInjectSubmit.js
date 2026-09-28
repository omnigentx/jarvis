/** Submit bulk-inject payload and format user-facing outcome without shadowing i18n. */
import { summarizeBulkInjectResults } from './bulkInjectFeedback.js'

export async function submitBulkInject({ text, files, targets, onSubmit, translate }) {
  const trimmedText = text.trim()
  if (!trimmedText && !files.length) return { ignored: true, feedback: '' }
  if (!targets.length) return { ignored: true, feedback: translate('bulkInject.noAgents') }
  const results = await onSubmit({ text: trimmedText, files })
  const { ok, failures } = summarizeBulkInjectResults(results, targets, key => translate(`teamMonitor.${key}`))
  const summary = translate('bulkInject.injectedTo', { ok, total: targets.length })
  return { ignored: false, feedback: failures.length ? `${summary}. ${failures.join('; ')}` : summary, results }
}
