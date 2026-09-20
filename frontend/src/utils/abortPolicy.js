/**
 * 终态约束：只有进行中的 Run 才能中止。
 * completed / aborted 均为终态，不再提供中止入口。
 */
export function canAbortRun(role, status) {
  return role === 'researcher' && status === 'running'
}
