import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/model'
const columns = [
  "模型编号",
  "模型名称",
  "模型类型",
  "输入字段",
  "参数配置",
  "训练数据集",
  "模型版本",
  "生效版本",
  "部署状态",
  "兼容状态",
  "服务调用状态"
]
const actions = [
  "部署模型",
  "降级模型",
  "兼容检查",
  "下线模型",
  "归档模型"
]
const statuses = [
  "全部状态",
  "未部署",
  "已部署",
  "已降级",
  "已下线"
]
const versionOrders = [
  { value: 'desc', label: '版本从新到旧' },
  { value: 'asc', label: '版本从旧到新' }
]
const stats = [
  { label: '分析模型总数', key: 'total' },
  { label: '已部署模型', key: 'deployed' },
  { label: '待兼容模型', key: 'incompatible' }
] as const

type Row = Record<string, string | number | null | string[]>
type Diff = Record<string, string | number | null>

export default function ModelView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(10)
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState('全部状态')
  const [version, setVersion] = useState('')
  const [order, setOrder] = useState('desc')
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [selected, setSelected] = useState<Row | null>(null)
  const [selectedReport, setSelectedReport] = useState<Record<string, unknown> | null>(null)
  const [diffs, setDiffs] = useState<Diff[]>([])
  const [diffStatus, setDiffStatus] = useState('全部状态')

  const statsValue = useMemo(() => ({
    total,
    deployed: rows.filter((row) => row["部署状态"] === '已部署').length,
    incompatible: rows.filter((row) => row["兼容状态"] === '待处理').length
  }), [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams({ page: String(page), size: String(pageSize), sort: 'version', order })
    if (keyword.trim()) query.set('keyword', keyword.trim())
    if (status !== '全部状态') query.set('status', status)
    if (version.trim()) query.set('version', version.trim())
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('分析模型列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析模型管理列表读取失败')
    }
  }, [page, pageSize, keyword, status, version, order])

  const reloadDiffs = useCallback(async () => {
    const query = new URLSearchParams()
    if (keyword.trim()) query.set('keyword', keyword.trim())
    if (version.trim()) query.set('version', version.trim())
    if (diffStatus !== '全部状态') query.set('status', diffStatus)
    try {
      const response = await request(`${ENDPOINT}/differences?${query.toString()}`)
      if (!response.ok) {
        throw new Error('兼容差异列表读取失败')
      }
      const payload = await response.json()
      setDiffs(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '兼容差异列表读取失败')
    }
  }, [keyword, version, diffStatus])

  useEffect(() => {
    void reload()
    void reloadDiffs()
  }, [reload, reloadDiffs])

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const [detailResponse, reportResponse] = await Promise.all([
        request(`${ENDPOINT}/${row["模型编号"]}`),
        request(`${ENDPOINT}/${row["模型编号"]}/compatibility`)
      ])
      if (!detailResponse.ok || !reportResponse.ok) {
        throw new Error('模型详情读取失败')
      }
      setSelected(await detailResponse.json())
      setSelectedReport(await reportResponse.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '模型详情读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    const payload: Record<string, unknown> = { action }
    if (action === '降级模型') {
      payload.request_id = `fallback-${row["模型编号"]}-${Date.now()}`
    }
    try {
      const response = await request(`${ENDPOINT}/${row["模型编号"]}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: payload }),
      })
      if (!response.ok) {
        throw new Error('分析模型管理动作未生效，请稍后重试')
      }
      const result = await response.json()
      setNotice(result.message ?? '')
      if (!result.ok) setErrorMessage(result.message ?? '动作未生效')
      if (selected) await openDetail({ ...selected, 模型编号: row["模型编号"] })
      await reload()
      await reloadDiffs()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析模型管理操作失败')
    }
  }

  async function submitFilters() {
    setPage(1)
    await reload()
    await reloadDiffs()
  }

  return (
    <section className="page" data-module="model">
      <header className="page-head">
        <div>
          <h2>分析模型管理</h2>
          <p className="page-desc">维护模型版本、部署兼容检查与降级策略；生效版本会同步到模型详情和数据服务调用清单。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分析模型登记入口尚未接入审批流')}>
            登记分析模型
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析模型管理清单
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

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void submitFilters() }}>
        <label className="filter-item">
          <span>模型编号</span>
          <input value={keyword} placeholder="按模型编号检索，如 MODEL-003" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>部署状态</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>模型版本</span>
          <input value={version} placeholder="按版本定位，如 v3" onChange={(event) => setVersion(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>版本先后</span>
          <select value={order} onChange={(event) => setOrder(event.target.value)}>
            {versionOrders.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => {
          setKeyword(''); setStatus('全部状态'); setVersion(''); setOrder('desc'); setPage(1)
        }}>重置条件</button>
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
            <tr key={String(row.id)} className={row["兼容状态"] === '待处理' ? 'row-warn' : undefined}>
              {columns.map((column) => (
                <td key={column}>
                  {Array.isArray(row[column]) ? (row[column] as string[]).join('、') : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>查看详情</button>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析模型数据，可先登记分析模型</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>
          共 {total} 条分析模型记录，第 {page} 页
          <button className="link" type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
          <button className="link" type="button" disabled={page * pageSize >= total} onClick={() => setPage(page + 1)}>下一页</button>
        </span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
        {!errorMessage && notice && <span className="notice-text">{notice}</span>}
      </footer>

      <section className="detail-panel">
        <header className="panel-head">
          <h3>生效版本与字段差异</h3>
          {selected && <span className="panel-meta">
            {String(selected["模型编号"])} · 生效 {String(selected["生效版本"] ?? '无')} · 稳定版本 {String((selectedReport?.["可兼容稳定版本"] as string) ?? selected["稳定版本"] ?? '无')}
          </span>}
        </header>
        {selected ? (
          <div className="panel-body">
            <p className="panel-line">
              训练数据集 {String(selected["训练数据集"] ?? '—')} · 数据集版本 {String(selectedReport?.["数据集版本"] ?? '—')} ·
              当前字段 {(selectedReport?.["当前数据集字段"] as string[] | undefined)?.join('、') || '—'}
            </p>
            {(selectedReport?.["字段差异"] as Diff[] | undefined)?.length ? (
              <table className="data-table compact">
                <thead>
                  <tr><th>类型</th><th>字段</th><th>说明</th><th>严重级别</th></tr>
                </thead>
                <tbody>
                  {(selectedReport?.["字段差异"] as Diff[]).map((diff, index) => (
                    <tr key={`${diff["类型"]}-${diff["字段"]}-${index}`}>
                      <td>{diff["类型"]}</td>
                      <td>{diff["字段"] || '—'}</td>
                      <td>{diff["说明"]}</td>
                      <td>{diff["严重级别"]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="panel-line">当前字段与生效版本兼容，无需降级。</p>}
          </div>
        ) : <p className="panel-line">从上方列表点击「查看详情」查看模型生效版本与字段差异。</p>}
      </section>

      <section className="detail-panel">
        <header className="panel-head">
          <h3>兼容差异列表</h3>
          <label className="inline-filter">
            <span>状态</span>
            <select value={diffStatus} onChange={(event) => setDiffStatus(event.target.value)}>
              {['全部状态', '待处理', '已兼容', '已降级', '已恢复'].map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
        </header>
        <table className="data-table compact">
          <thead>
            <tr>
              <th>差异编号</th><th>模型编号</th><th>模型版本</th><th>类型</th><th>字段</th><th>状态</th><th>可兼容版本</th><th>说明</th>
            </tr>
          </thead>
          <tbody>
            {diffs.map((diff, index) => (
              <tr key={String(diff["差异编号"] ?? index)}>
                <td>{diff["差异编号"] ?? '—'}</td>
                <td>{diff["模型编号"] ?? '—'}</td>
                <td>{diff["模型版本"] ?? '—'}</td>
                <td>{diff["类型"] ?? '—'}</td>
                <td>{diff["字段"] || '—'}</td>
                <td>{diff["状态"] ?? '—'}</td>
                <td>{diff["可兼容版本"] ?? '—'}</td>
                <td>{diff["说明"] ?? '—'}</td>
              </tr>
            ))}
            {diffs.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>当前筛选条件下没有兼容差异</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </section>
  )
}
