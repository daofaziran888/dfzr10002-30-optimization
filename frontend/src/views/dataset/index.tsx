import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataset'
const columns = [
  "数据集编号",
  "数据集名称",
  "来源表名",
  "字段列表",
  "数据集版本",
  "稳定版本",
  "最近刷新",
  "是否启用"
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置"
]
const stats = [
  { label: '分析数据集总数', key: 'total' },
  { label: '待校验数据集', key: 'pending' },
  { label: '已启用数据集', key: 'enabled' }
] as const

type Row = Record<string, string | number | null | string[]>

export default function DatasetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [fieldDraft, setFieldDraft] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const statsValue = useMemo(() => ({
    total,
    pending: rows.filter((row) => row.status === '待校验').length,
    enabled: rows.filter((row) => row.status === '已启用').length
  }), [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('数据集列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    const values: Record<string, unknown> = { action }
    if (fieldDraft.trim()) {
      values["字段列表"] = fieldDraft.split(/[,，\s]+/).filter(Boolean)
    }
    try {
      const response = await request(`${ENDPOINT}/${row["数据集编号"]}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      if (!response.ok) {
        throw new Error('分析数据集动作未生效，请稍后重试')
      }
      const result = await response.json()
      setNotice(result.message ?? '')
      if (!result.ok) setErrorMessage(result.message ?? '动作未生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集操作失败')
    }
  }

  return (
    <section className="page" data-module="dataset">
      <header className="page-head">
        <div>
          <h2>分析数据集管理</h2>
          <p className="page-desc">维护分析可用的数据集定义、字段版本与刷新策略；字段变更会同步检查依赖模型的兼容状态。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('数据集登记入口尚未接入审批流')}>
            登记数据集
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析数据集清单
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
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>字段列表（执行动作时发布为新版本）</span>
          <input
            value={fieldDraft}
            placeholder="如 user_id, amount, channel, event_time"
            onChange={(event) => setFieldDraft(event.target.value)}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
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
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {Array.isArray(row[column]) ? (row[column] as string[]).join('、') : (row[column] ?? '—')}
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析数据集数据，可先登记数据集</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析数据集记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
        {!errorMessage && notice && <span className="notice-text">{notice}</span>}
      </footer>
    </section>
  )
}
