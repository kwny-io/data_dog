/**
 * 空态文案（纯函数，便于单测钉住"说得清"）。
 *
 * 为什么单独抽出来：空列表有两种完全不同的含义——
 *  1) 真的没有数据（还没投递 / 筛选条件过窄）；
 *  2) **租户不对**：数据投到了别的租户，多租户隔离正常工作。
 * 两者在界面上长得一模一样。本仓库真实发生过第 2 种：`make ingest` 投 `tenant-real`、
 * 控制台读 `tenant-demo`，照 README 跑完演示后列表永远为空，被误判成"接口坏了"。
 * 所以空态必须把**当前租户**和**"这不是故障"**都说出来，且这句话要被测试钉住。
 */

/** 候选列表空态：点名当前租户 + 区分"没数据"与"租户不对"。 */
export function emptyCandidatesHint(tenant: string): string {
  return `候选由在线数据面从原始日志产出，只统计当前租户 ${tenant} 的数据。先往租户 ${tenant} 提交一批设备日志（如 make ingest）；若数据投到了别的租户，这里会一直为空——这是多租户隔离在正常工作，不是接口故障。`;
}

/**
 * 告警列表空态。
 * @param tenant 当前租户（来自服务端 DD_WEB_TENANT，浏览器改不了）
 * @param stateLabel 已选的告警状态中文名；为空表示没有筛选
 */
export function emptyAlertsHint(tenant: string, stateLabel?: string | null): string {
  if (stateLabel) {
    return `当前筛选的是「${stateLabel}」状态；去掉筛选看全部。`;
  }
  return (
    `告警由 P3 上报闭环在候选经策略门判定后产生，只统计当前租户 ${tenant}；` +
    `若数据投到了别的租户，这里会一直为空（隐私隔离在正常工作，不是接口故障）。`
  );
}
