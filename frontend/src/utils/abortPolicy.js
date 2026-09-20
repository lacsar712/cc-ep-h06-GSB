/** 终态（completed/aborted）不可中止：仅进行中的 Run 提供中止入口。 */
export function canAbortRun(role, status) {
  return role === 'researcher' && status === 'running'
}
