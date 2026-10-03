/** Manual activation uses one reviewed, signed confirmation per runtime. */
export const ALL_CURRENT_TARGETS = 'all:current'
export const targetKey = target => target.run_id || target.agent

export function blockingCapabilities(plugin) {
  const allowed = plugin.policy_configured
    ? ['mcp_requires_policy_review', ...((plugin.skills || []).length ? [] : ['executable_content'])]
    : []
  return (plugin.blockers || []).filter(blocker => !allowed.includes(blocker))
}

export function activationTargets(selection, available) {
  const candidates = selection === ALL_CURRENT_TARGETS
    ? available : available.filter(target => targetKey(target) === selection)
  return [...new Map(candidates.map(target => [targetKey(target), { ...target }])).values()]
}

export async function activateReviewedTargets(reviews, activate, onResult = () => {}) {
  const results = []
  for (const review of reviews) {
    let outcome
    try {
      const record = await activate(review)
      const binding = (record.bindings || []).find(item => review.run_id
        ? item.run_id === review.run_id
        : item.agent === review.target)
      outcome = { target: review.target, run_id: review.run_id, label: review.target_label || review.target,
        status: binding?.status || (record.status === 'ready' ? 'needs_reactivation' : record.status) }
    } catch (error) {
      outcome = { target: review.target, run_id: review.run_id, label: review.target_label || review.target,
        status: 'activation_failed', error: error.message }
    }
    results.push(outcome)
    onResult([...results])
  }
  return results
}
