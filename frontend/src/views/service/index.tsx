import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/service'
const columns = [
  "服务编号",
  "服务名称",
  "输出数据集",
  "关联模型",
  "生效模型版本",
  "模型版本记录",
  "服务调用状态",
  "查询参数",
  "响应字段",
  "缓存秒数",
  "分页上限",
  "消费应用"
]
const actions = [
  "提交审批",
  "发布服务",
  "停用服务"
]
const statuses = [
  "全部状态",
  "草稿",
  "待审批",
  "已发布",
  "已停用",
  "正常",
  "已降级",
  "版本待兼容"
]
const stats = [
  { label: '数据服务总数', key: 'total' },
  { label: '已发布服务', key: 'published' },
  { label: '调用待兼容', key: 'incompatible' }
] as const

type Row = Record<string, string | number | null | string[]>

export default function ServiceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState('全部状态')
  const [version, setVersion] = useState('')

  const statsValue = useMemo(() => ({
    total,
    published: rows.filter((row) => row.status === '已发布').length,
    incompatible: rows.filter((row) => row["服务调用状态"] === '版本待兼容').length
  }), [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (keyword.trim()) query.set('keyword', keyword.trim())
    if (status !== '全部状态') query.set('status', status)
    if (version.trim()) query.set('version', version.trim())
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('数据服务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务发布列表读取失败')
    }
  }, [keyword, status, version])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row["服务编号"]}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      if (!response.ok) {
        throw new Error('数据服务发布动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务发布操作失败')
    }
  }

  return (
    <section className="page" data-module="service">
      <header className="page-head">
        <div>
          <h2>数据服务发布管理</h2>
          <p className="page-desc">维护数据服务接口的发布参数、缓存配置与消费关系；生效模型版本随模型部署、降级实时同步。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('数据服务登记入口尚未接入审批流')}>
            登记数据服务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据服务发布清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.key} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{statsValue[item.key]}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>服务编号</span>
          <input value={keyword} placeholder="按服务编号检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>状态 / 调用状态</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>生效模型版本</span>
          <input value={version} placeholder="按生效模型版本定位，如 v2" onChange={(event) => setVersion(event.target.value)} />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatus('全部状态'); setVersion('') }}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row["服务调用状态"] === '版本待兼容' ? 'row-warn' : undefined}>
              {columns.map((column) => (
                <td key={column}>
                  {Array.isArray(row[column]) ? (row[column] as string[]).join(' → ') : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据服务数据，可先登记数据服务</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据服务记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
