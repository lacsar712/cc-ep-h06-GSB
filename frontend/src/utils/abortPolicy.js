/** BUG: completed runs still offer abort. */
export function canAbortRun(role, status) {
  if (role !== 'researcher') return false
  if (status === 'running') return true
  if (status === 'completed') return true
  if (status === 'aborted') return true
  return false
}
