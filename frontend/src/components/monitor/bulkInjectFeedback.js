/** Convert settled injection outcomes into target-specific UI feedback. */
export function summarizeBulkInjectResults(results, targets, translate) {
  const settled = Array.isArray(results) ? results : []
  const ok = settled.length ? settled.filter(result => result?.status === 'fulfilled').length : targets.length
  const failures = settled.flatMap((result, index) => result?.status === 'rejected'
    ? [`${targets[index]?.name || targets[index]?.agent_name || 'Agent'}: ${result.reason?.status === 409 ? translate('ambiguousActionBlocked') : (result.reason?.message || String(result.reason))}`]
    : [])
  return { ok, failures }
}
