<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const api = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
const tabs = [
  { id: 'overview', label: '总览', icon: '◫' },
  { id: 'health', label: '健康评估', icon: '⌁' },
  { id: 'anomaly', label: '异常检测', icon: '⌖' },
  { id: 'records', label: '数据管理', icon: '▤' },
  { id: 'models', label: '模型与外部验证', icon: '⌘' },
]
const active = ref('overview')
const summary = ref({}), batteries = ref([]), selected = ref('B0005')
const health = ref({ records: [] }), anomalies = ref([]), anomalyCheckedCount = ref(0)
const provenance = ref(null), metrics = ref([]), externalTest = ref({ ready: false, batteries: [] })
const records = ref([]), recordBattery = ref(''), recordPage = ref(1), recordTotal = ref(0), recordsBusy = ref(false)
const importDialog = ref(false), importPreview = ref(null), importSourceName = ref('')
const importMapping = ref({}), importCapacityUnit = ref('Ah'), selectedImportFile = ref(null), qualityReport = ref(null)
const filePicker = ref(null)
const importFields = [
  { key: 'battery_id', label: '电池编号', required: true },
  { key: 'cycle_index', label: '循环次数', required: true },
  { key: 'capacity_Ah', label: '放电容量', required: true },
  { key: 'voltage_mean', label: '平均电压', required: false },
  { key: 'voltage_min', label: '最低电压', required: false },
  { key: 'current_mean', label: '平均电流', required: false },
  { key: 'temperature_mean', label: '平均温度', required: false },
  { key: 'time_max', label: '放电时长', required: false },
  { key: 'quality_flag', label: '数据质量标记', required: false },
  { key: 'quality_reason', label: '质量说明', required: false },
]
const importRequiredReady = computed(() => importFields.filter(x => x.required).every(x => importMapping.value[x.key]))
const currentSource = computed(() => provenance.value?.database_sources?.[0] || null)
const latestHealth = computed(() => health.value.records?.at(-1) || null)
const healthAssessment = computed(() => {
  const value = Number(latestHealth.value?.soh_pct)
  if (!Number.isFinite(value)) return { label: '暂无评估结果', tone: 'neutral', note: '请选择有足够有效容量记录的电池。' }
  if (value > 120) return { label: '数据需要复核', tone: 'warn', note: '健康度高于参考容量较多，请先检查参考值和测量记录。' }
  if (value < 80) return { label: '低于 80% 参考线', tone: 'warn', note: '建议查看容量变化和对应的原始测试记录。' }
  return { label: '高于 80% 参考线', tone: 'good', note: '表示容量保持比例较高，不代表安全结论。' }
})
const importDataUrl = computed(() => `${api}/api/data/export${recordBattery.value ? `?battery_id=${encodeURIComponent(recordBattery.value)}` : ''}`)
const chartRefs = { overview: ref(null), health: ref(null) }
const charts = {}
const busy = ref(false), message = ref('')

async function request(path, options = {}) {
  const response = await fetch(`${api}${path}`, options)
  if (!response.ok) {
    let detail = `请求失败 ${response.status}`
    try { detail = (await response.json()).detail || detail } catch {}
    throw new Error(detail)
  }
  return response
}
async function get(path) { return (await request(path)).json() }
async function post(path, body) {
  const form = body instanceof FormData
  return (await request(path, { method: 'POST', headers: form ? {} : { 'Content-Type': 'application/json' }, body: form ? body : JSON.stringify(body || {}) })).json()
}
function tell(text) {
  message.value = text
  window.setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
}
function formatValue(value, digits = 2) {
  const number = Number(value)
  return value == null || value === '' || !Number.isFinite(number) ? '—' : number.toFixed(digits)
}
function parseCsv(text) {
  const [header, ...lines] = text.trim().split(/\r?\n/)
  if (!header) return []
  const keys = header.split(',')
  return lines.map(line => Object.fromEntries(line.split(',').map((value, index) => [keys[index], Number.isNaN(Number(value)) ? value : Number(value)])))
}
async function loadBase() {
  try {
    const [overview, list, source] = await Promise.all([
      get('/api/health/summary'), get('/api/data/batteries'), get('/api/data/provenance'),
    ])
    summary.value = overview
    batteries.value = list
    provenance.value = source
    if (list.length && !list.some(item => item.battery_id === selected.value)) selected.value = list[0].battery_id
  } catch (error) { tell(`连接后端失败：${error.message}`) }
}
async function loadHealth() {
  if (!selected.value) { health.value = { records: [] }; return }
  try { health.value = await get(`/api/health/${encodeURIComponent(selected.value)}`) }
  catch (error) { health.value = { records: [] }; tell(error.message) }
  await nextTick()
  drawHealth()
}
async function loadAnomaly() {
  try {
    const query = recordBattery.value && active.value === 'anomaly' ? `?battery_id=${encodeURIComponent(recordBattery.value)}` : ''
    const result = await post(`/api/anomaly/detect${query}`)
    anomalies.value = result.items || []
    anomalyCheckedCount.value = Number(result.checked_count || 0)
  } catch (error) { anomalies.value = []; anomalyCheckedCount.value = 0; tell(error.message) }
}
async function loadRecords() {
  recordsBusy.value = true
  try {
    const query = new URLSearchParams({ page: String(recordPage.value), page_size: '20' })
    if (recordBattery.value) query.set('battery_id', recordBattery.value)
    const result = await get(`/api/data/records?${query}`)
    records.value = result.items || []
    recordTotal.value = Number(result.total || 0)
  } catch (error) { tell(error.message) }
  finally { recordsBusy.value = false }
}
function changeRecordFilter() { recordPage.value = 1; loadRecords() }
function changeRecordPage(delta) { recordPage.value += delta; loadRecords() }
async function loadModels() {
  try {
    const response = await request('/api/reports/export?report=metrics')
    metrics.value = parseCsv(await response.text())
  } catch { metrics.value = [] }
  await loadExternalTest()
}
async function loadExternalTest() {
  try { externalTest.value = await get('/api/models/external-test') }
  catch (error) { tell(error.message) }
}
async function runTraining() {
  busy.value = true
  try { const result = await post('/api/models/train'); metrics.value = result.metrics || []; tell('NASA 内部模型评价已更新。') }
  catch (error) { tell(error.message) }
  finally { busy.value = false }
}
async function runExternalEvaluation() {
  busy.value = true
  try { externalTest.value = await post('/api/models/evaluate-external'); tell('CALCE 独立外部评价已完成，数据未并入训练。') }
  catch (error) { tell(error.message) }
  finally { busy.value = false }
}
async function importFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  const form = new FormData(); form.append('file', file)
  busy.value = true
  try {
    const result = await post('/api/data/import/preview', form)
    importPreview.value = result
    importMapping.value = { ...result.mapping }
    importSourceName.value = file.name.replace(/\.[^.]+$/, '')
    importCapacityUnit.value = result.capacity_unit || 'Ah'
    selectedImportFile.value = file
    importDialog.value = true
  } catch (error) { tell(error.message) }
  finally { busy.value = false; event.target.value = '' }
}
function closeImportDialog() { importDialog.value = false; importPreview.value = null; selectedImportFile.value = null }
async function confirmImport() {
  const file = selectedImportFile.value
  if (!file || !importPreview.value) return
  const accepted = window.confirm(`导入“${importSourceName.value || file.name}”会替换当前通用分析数据，并重新生成健康度和异常结果。NASA 建模数据及 CALCE 外部测试数据保持独立。确认继续吗？`)
  if (!accepted) return
  const form = new FormData()
  form.append('file', file)
  form.append('mapping', JSON.stringify(importMapping.value))
  form.append('source_name', importSourceName.value || '用户导入数据')
  form.append('capacity_unit', importCapacityUnit.value)
  busy.value = true
  try {
    const result = await post('/api/data/import', form)
    qualityReport.value = result.quality_report
    closeImportDialog()
    tell(`导入完成：${result.imported_records.toLocaleString()} 条记录，${result.batteries} 块电池。`)
    await loadBase()
    await loadHealth()
    if (active.value === 'overview') drawOverview()
    if (active.value === 'records') await loadRecords()
    if (active.value === 'anomaly') await loadAnomaly()
  } catch (error) { tell(error.message) }
  finally { busy.value = false }
}
async function saveAnomalyReview(row) {
  try {
    await request(`/api/anomaly/${row.source_row}/review`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: row.review_status || '待复核', note: row.review_note || '' }),
    })
    tell('复核记录已保存。')
  } catch (error) { tell(error.message) }
}
function setChart(name, option) {
  const node = chartRefs[name]?.value
  if (!node || !node.clientWidth || !node.clientHeight) return
  charts[name] ||= init(node)
  charts[name].setOption(option, true)
}
function drawOverview() {
  const points = summary.value.trend || []
  setChart('overview', {
    color: ['#5579e8'], grid: { left: 56, right: 20, top: 22, bottom: 40 },
    tooltip: { trigger: 'axis' }, xAxis: { type: 'category', name: '循环次数', data: points.map(x => x.cycle_index) },
    yAxis: { type: 'value', name: '平均 SOH（%）', min: 0, max: 120 },
    series: [{ name: '平均 SOH', type: 'line', smooth: true, symbol: 'none', areaStyle: { opacity: 0.1 }, data: points.map(x => x.soh_pct) }],
  })
}
function drawHealth() {
  const points = health.value.records || []
  setChart('health', {
    color: ['#4f7cf3', '#e6a15f'], grid: { left: 56, right: 22, top: 24, bottom: 46 },
    tooltip: { trigger: 'axis' }, legend: { data: ['电池 SOH', '80% 观察线'], bottom: 0 },
    xAxis: { type: 'category', name: '循环次数', data: points.map(x => x.cycle_index) },
    yAxis: { type: 'value', name: 'SOH（%）', min: 0, max: 130 },
    series: [
      { name: '电池 SOH', type: 'line', smooth: true, symbol: 'none', areaStyle: { opacity: 0.08 }, data: points.map(x => x.soh_pct) },
      { name: '80% 观察线', type: 'line', symbol: 'none', lineStyle: { type: 'dashed' }, data: points.map(() => 80) },
    ],
  })
}
function resizeCharts() { Object.values(charts).forEach(chart => chart.resize()) }
watch(selected, loadHealth)
watch(active, async view => {
  await nextTick()
  if (view === 'overview') drawOverview()
  if (view === 'health') drawHealth()
  if (view === 'anomaly') await loadAnomaly()
  if (view === 'records') await loadRecords()
  if (view === 'models') await loadModels()
})
watch(recordBattery, async value => { if (active.value === 'anomaly') await loadAnomaly(value) })
onMounted(async () => {
  await loadBase()
  await loadHealth()
  await nextTick()
  drawOverview()
  window.addEventListener('resize', resizeCharts)
})
onBeforeUnmount(() => { window.removeEventListener('resize', resizeCharts); Object.values(charts).forEach(chart => chart.dispose()) })
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">B</div><div><b>电池健康分析</b><small>SOH & ANOMALY</small></div></div>
      <div class="nav-label">系统功能</div>
      <button v-for="item in tabs.slice(0, 4)" :key="item.id" class="nav-item" :class="{ selected: active === item.id }" @click="active = item.id"><span class="nav-icon">{{item.icon}}</span>{{item.label}}</button>
      <button class="nav-item secondary-nav experiment-entry" :class="{ selected: active === 'models' }" @click="active = 'models'"><span class="nav-icon">⌘</span>实验功能</button>
      <div class="sidebar-foot"><span class="pulse"></span>分析服务<small>基于多源电池循环数据</small></div>
    </aside>

    <main class="main">
      <header class="topbar"><div class="crumb">{{tabs.find(item => item.id === active)?.label}}</div><label class="upload-btn">＋ 导入循环数据<input ref="filePicker" type="file" accept=".csv,.xlsx" @change="importFile"></label></header>
      <div class="content">
        <template v-if="active === 'overview'">
          <div class="page-heading"><div><div class="eyebrow">BATTERY HEALTH & ANOMALY</div><h1>锂离子电池健康评估与异常检测</h1><p>基于多源循环数据，查看电池容量健康变化并定位需要复核的记录。</p></div><button class="button primary" @click="active = 'health'">查看健康评估</button></div>
          <section class="stats-grid">
            <article class="stat-card"><div class="stat-top"><span>分析电芯</span><i>◉</i></div><strong>{{summary.battery_count ?? '—'}}</strong><small>当前展示的数据集</small><div class="stat-line blue"></div></article>
            <article class="stat-card"><div class="stat-top"><span>有效 SOH 记录</span><i>▤</i></div><strong>{{summary.valid_record_count?.toLocaleString?.() ?? '—'}}</strong><small>通过容量基本检查</small><div class="stat-line green"></div></article>
            <article class="stat-card"><div class="stat-top"><span>平均 SOH</span><i>⌁</i></div><strong>{{summary.mean_soh_pct == null ? '—' : `${summary.mean_soh_pct.toFixed(1)}%`}}</strong><small>按电芯校准后汇总</small><div class="stat-line violet"></div></article>
            <article class="stat-card"><div class="stat-top"><span>异常待复核</span><i>⌖</i></div><strong>{{summary.review_record_count ?? '—'}}</strong><small>统计筛查提示，不是故障结论</small><div class="stat-line orange"></div></article>
          </section>
          <section class="panel workflow-panel"><div class="panel-head"><div><h2>系统分析流程</h2><p>围绕毕业设计的两个核心目标组织操作。</p></div><span class="tag">当前数据：{{currentSource?.source_name || 'NASA 演示数据'}}</span></div><div class="workflow"><button @click="filePicker?.click()"><b>01</b><strong>导入与检查</strong><small>统一字段、单位与循环记录</small></button><button @click="active = 'health'"><b>02</b><strong>健康状态评估</strong><small>计算容量型 SOH 并查看趋势</small></button><button @click="active = 'anomaly'"><b>03</b><strong>异常检测与复核</strong><small>查看偏离记录并填写处理结果</small></button></div></section>
          <section class="panel"><div class="panel-head"><div><h2>整体 SOH 趋势</h2><p>按循环次数汇总当前数据集中的健康度变化。</p></div><button class="text-action" @click="active = 'health'">进入健康评估 →</button></div><div :ref="chartRefs.overview" class="chart"></div></section>
          <section class="panel explain"><span class="explain-icon">i</span><p>SOH 是当前放电容量相对该电芯参考容量的比例。系统的异常提示用于筛查需要核对的数据，不替代专业电池检测或质量判定。导入的数据仅进入当前分析，不会自动加入模型训练。</p></section>
        </template>

        <template v-else-if="active === 'health'">
          <div class="page-heading"><div><div class="eyebrow">CORE / 01</div><h1>健康状态评估</h1><p>查看单块电芯的容量型 SOH、循环变化趋势和容量记录。</p></div><select v-model="selected" class="select"><option v-for="item in batteries" :key="item.battery_id" :value="item.battery_id">{{item.battery_id}}</option></select></div>
          <section class="panel assessment-card"><div><small>当前电芯</small><h2>{{selected || '暂无数据'}}</h2><p>SOH = 当前放电容量 ÷ 参考容量 × 100%。参考容量仅为本研究的计算基准。</p></div><div class="assessment-value"><strong>{{latestHealth ? `${Number(latestHealth.soh_pct).toFixed(1)}%` : '—'}}</strong><span class="status-pill" :class="healthAssessment.tone">{{healthAssessment.label}}</span><small>{{healthAssessment.note}}</small></div></section>
          <section class="detail-strip"><div><small>有效记录</small><b>{{health.records?.length ?? 0}} 条</b></div><div><small>参考容量</small><b>{{health.reference_capacity_Ah ? `${health.reference_capacity_Ah.toFixed(3)} Ah` : '—'}}</b></div><div><small>参考值计算</small><b>前三条合格容量的中位数</b></div><div><small>80% 参考线</small><b>趋势观察线，不是故障阈值</b></div></section>
          <section class="panel"><div class="panel-head"><div><h2>SOH 随循环变化</h2><p>低于参考线时可结合容量记录和异常检测结果进一步核对。</p></div><span class="tag">{{selected}}</span></div><div :ref="chartRefs.health" class="chart tall"></div></section>
          <section class="panel table-panel"><div class="panel-head"><div><h2>健康度计算明细</h2><p>参考值按当前电芯单独计算。</p></div></div><div class="table-wrap"><table><thead><tr><th>循环次数</th><th>放电容量 (Ah)</th><th>SOH (%)</th><th>数据说明</th></tr></thead><tbody><tr v-for="row in health.records" :key="row.cycle_index"><td>{{row.cycle_index}}</td><td>{{formatValue(row.capacity_Ah, 4)}}</td><td>{{formatValue(row.soh_pct, 2)}}%</td><td>{{row.data_quality_flag || '—'}}</td></tr><tr v-if="!health.records?.length"><td colspan="4" class="empty-state">当前电芯没有足够的有效容量记录。可切换电芯或导入循环数据。</td></tr></tbody></table></div></section>
        </template>

        <template v-else-if="active === 'anomaly'">
          <div class="page-heading"><div><div class="eyebrow">CORE / 02</div><h1>异常检测</h1><p>筛选与同批记录统计分布差异较大的循环记录，供人工检查。</p></div><div class="record-actions"><select v-model="recordBattery" class="select"><option value="">全部电芯</option><option v-for="item in batteries" :key="item.battery_id" :value="item.battery_id">{{item.battery_id}}</option></select><button class="button secondary" @click="loadAnomaly">重新检测</button></div></div>
          <section class="stats-grid record-stats"><article class="stat-card"><div class="stat-top"><span>待复核记录</span><i>⌖</i></div><strong>{{anomalies.length.toLocaleString()}}</strong><small>{{recordBattery || '全部电芯'}}</small><div class="stat-line orange"></div></article><article class="stat-card"><div class="stat-top"><span>已筛查记录</span><i>▤</i></div><strong>{{anomalyCheckedCount.toLocaleString()}}</strong><small>使用可用循环特征进行统计筛查</small><div class="stat-line blue"></div></article></section>
          <section class="panel explain"><span class="explain-icon">i</span><p>系统使用正则化马氏距离筛查容量、电压、电流、温度和时长等可用特征。被标记只表示数据偏离参考分布，可能来自真实差异、测量误差或数据整理问题，需对照原始记录复核，不直接判断电芯故障。</p></section>
          <section class="panel table-panel"><div class="panel-head"><div><h2>异常记录与人工复核</h2><p>复核状态和备注会保存在当前数据库。</p></div><a class="download-link" :href="`${api}/api/reports/export?report=anomaly${recordBattery ? `&battery_id=${encodeURIComponent(recordBattery)}` : ''}`" download>下载异常清单</a></div><div class="table-wrap"><table><thead><tr><th>电芯</th><th>循环</th><th>偏离度 / 界限</th><th>容量 (Ah)</th><th>电压 (V)</th><th>电流 (A)</th><th>温度 (°C)</th><th>复核提示</th><th>状态</th><th>备注</th></tr></thead><tbody><tr v-for="row in anomalies" :key="row.source_row"><td><b>{{row.battery_id}}</b></td><td>{{row.cycle_index}}</td><td>{{formatValue(row.mahalanobis_distance, 2)}} / {{formatValue(row.anomaly_threshold, 2)}}</td><td>{{formatValue(row.capacity_Ah, 4)}}</td><td>{{formatValue(row.voltage_mean, 3)}}</td><td>{{formatValue(row.current_mean, 3)}}</td><td>{{formatValue(row.temperature_mean, 2)}}</td><td :title="row.review_reason || row.quality_reason">{{row.review_reason || row.quality_reason || '对照原始数据复核'}}</td><td><select v-model="row.review_status" class="select review-select" @change="saveAnomalyReview(row)"><option>待复核</option><option>已确认</option><option>已排除</option></select></td><td><input v-model="row.review_note" class="note-input" maxlength="1000" placeholder="复核备注" @change="saveAnomalyReview(row)"></td></tr><tr v-if="!anomalies.length"><td colspan="10" class="empty-state">当前筛选下没有触发异常提示的记录。异常数量为零不代表所有数据都绝对正常。</td></tr></tbody></table></div></section>
        </template>

        <template v-else-if="active === 'records'">
          <div class="page-heading"><div><div class="eyebrow">DATA / IMPORT & CLEANING</div><h1>数据管理</h1><p>导入多源循环数据、查看处理后的记录，并导出当前分析数据。</p></div><div class="record-actions"><select v-model="recordBattery" class="select" @change="changeRecordFilter"><option value="">全部电芯</option><option v-for="item in batteries" :key="item.battery_id" :value="item.battery_id">{{item.battery_id}}</option></select><a class="button secondary link-button" :href="importDataUrl" download>导出记录 CSV</a></div></div>
          <section class="stats-grid record-stats"><article class="stat-card"><div class="stat-top"><span>当前数据来源</span><i>▤</i></div><strong class="source-value">{{currentSource?.source_name || 'NASA 演示数据'}}</strong><small>通用导入只用于当前健康评估和异常筛查</small><div class="stat-line blue"></div></article><article class="stat-card"><div class="stat-top"><span>当前筛选记录</span><i>⌕</i></div><strong>{{recordTotal.toLocaleString()}}</strong><small>{{recordBattery || '全部电芯'}}</small><div class="stat-line green"></div></article></section>
          <section class="panel explain"><span class="explain-icon">i</span><p>支持 CSV/XLSX。必需字段为电芯编号、循环次数、放电容量；导入前可预览并映射字段，系统检查单位、缺失容量和重复循环。每次导入会替换当前通用分析批次，NASA 模型开发数据与 CALCE 独立外测数据保持分开。</p></section>
          <section class="panel table-panel"><div class="panel-head"><div><h2>循环数据明细</h2><p>缺少的可选测量字段显示为“—”。</p></div><span v-if="recordsBusy" class="tag">加载中…</span></div><div class="table-wrap records-table"><table><thead><tr><th>电芯</th><th>循环</th><th>容量 (Ah)</th><th>平均电压 (V)</th><th>平均电流 (A)</th><th>平均温度 (°C)</th><th>放电时长</th><th>质量标记</th></tr></thead><tbody><tr v-for="row in records" :key="row.source_row"><td><b>{{row.battery_id}}</b></td><td>{{row.cycle_index}}</td><td>{{formatValue(row.capacity_Ah, 4)}}</td><td>{{formatValue(row.voltage_mean, 3)}}</td><td>{{formatValue(row.current_mean, 3)}}</td><td>{{formatValue(row.temperature_mean, 2)}}</td><td>{{formatValue(row.time_max, 1)}}</td><td>{{row.quality_flag || '未标记'}}</td></tr><tr v-if="!recordsBusy && !records.length"><td colspan="8" class="empty-state">暂无循环记录。</td></tr></tbody></table></div><div class="pagination"><span>显示 {{recordTotal ? (recordPage - 1) * 20 + 1 : 0}}–{{Math.min(recordPage * 20, recordTotal)}} 条，共 {{recordTotal.toLocaleString()}} 条</span><div><button class="button secondary" :disabled="recordsBusy || recordPage <= 1" @click="changeRecordPage(-1)">上一页</button><button class="button secondary" :disabled="recordsBusy || recordPage >= Math.ceil(recordTotal / 20)" @click="changeRecordPage(1)">下一页</button></div></div></section>
        </template>

        <template v-else>
          <div class="page-heading"><div><div class="eyebrow">EXPERIMENT / MODEL EVALUATION</div><h1>模型与外部验证</h1><p>作为研究方法的实验依据；日常使用主要关注健康评估和异常检测。</p></div><button class="button primary" :disabled="busy" @click="runTraining">{{busy ? '运行中…' : '重新运行 NASA 内部评价'}}</button></div>
          <section v-if="metrics.length" class="metric-grid"><article v-for="item in metrics" :key="item.model" class="panel metric-card"><small>{{item.model === 'persistence_baseline' ? '持久性基线' : item.model === 'linear_regression' ? '线性回归' : item.model === 'decision_tree' ? '决策树回归' : item.model}}</small><strong>{{formatValue(item.MAE_percentage_points, 3)}} <em>pp MAE</em></strong><div class="metric-meta">RMSE {{formatValue(item.RMSE_percentage_points, 3)}} pp <span>R² {{formatValue(item.R2, 3)}}</span></div></article></section>
          <section v-else class="panel empty-state">暂无模型评价结果。确认 NASA 建模数据和依赖文件已就绪后，可运行内部评价。</section>
          <section class="panel explain"><span class="explain-icon">i</span><p>内部评价按电芯编号划分训练、验证和测试，评价目标为下一条有效放电观测的 SOH。MAE、RMSE 越低通常表示预测误差越小。用户导入数据不会自动进入 NASA 训练。</p></section>
          <section class="panel external-panel"><div class="panel-head"><div><h2>CALCE CX2 独立外部验证</h2><p>用另一来源的电芯数据观察模型跨数据源表现，不回流训练。</p></div><span class="tag">独立测试</span></div><div class="external-cells"><span v-for="item in externalTest.batteries || []" :key="item.battery_id" class="pill">{{item.battery_id}} · {{item.xlsx_files}} 个 Excel</span><span v-if="!externalTest.batteries?.length" class="muted">未发现外部测试电池文件</span></div><div class="external-actions"><button class="button primary" :disabled="busy || !externalTest.batteries?.length" @click="runExternalEvaluation">{{busy ? '评价中…' : '运行 CALCE 外部验证'}}</button><span v-if="externalTest.ready">{{Number(externalTest.scored_rows || 0).toLocaleString()}} 条评分记录 · {{externalTest.battery_count}} 块电芯</span></div><div v-if="externalTest.metrics?.length" class="table-wrap"><table><thead><tr><th>模型</th><th>MAE（百分点）</th><th>RMSE（百分点）</th><th>R²</th><th>记录数</th></tr></thead><tbody><tr v-for="item in externalTest.metrics.filter(x => x.evaluation_scope === 'overall')" :key="item.model"><td>{{item.model === 'persistence_baseline' ? '持久性基线' : item.model === 'linear_regression' ? '线性回归' : '决策树回归'}}</td><td>{{formatValue(item.MAE_percentage_points, 3)}}</td><td>{{formatValue(item.RMSE_percentage_points, 3)}}</td><td>{{formatValue(item.R2, 3)}}</td><td>{{Number(item.test_rows).toLocaleString()}}</td></tr></tbody></table></div><div v-if="externalTest.ready" class="external-downloads"><a v-for="file in [{key:'metrics',label:'评价指标'},{key:'predictions',label:'逐条预测'},{key:'records',label:'标准化记录'},{key:'cleaned',label:'清洗明细'}]" :key="file.key" class="download-link" :href="`${api}/api/models/external-test/export/${file.key}`" download>{{file.label}}</a></div></section>
        </template>
      </div>
    </main>

    <div v-if="importDialog && importPreview" class="modal-backdrop" @click.self="closeImportDialog"><section class="import-modal" role="dialog" aria-modal="true" aria-labelledby="import-title"><div class="modal-heading"><div><div class="eyebrow">DATA IMPORT</div><h2 id="import-title">检查并对应数据列</h2><p>{{importPreview.filename}} · {{importPreview.row_count.toLocaleString()}} 行</p></div><button class="close-button" @click="closeImportDialog">×</button></div><div class="explain"><span class="explain-icon">i</span><p>必需字段为电芯编号、循环次数和放电容量。电压、电流、温度、时长为可选字段；缺少时仍可计算容量型 SOH，但异常筛查可用信息会减少。</p></div><label class="source-name-field">数据集名称<input v-model="importSourceName" maxlength="200" placeholder="例如：HUST 循环数据"></label><div class="mapping-grid"><label v-for="field in importFields" :key="field.key" class="mapping-field"><span>{{field.label}}<b v-if="field.required">必填</b></span><select v-model="importMapping[field.key]" class="select"><option value="">{{field.required ? '请选择对应列' : '不导入此项'}}</option><option v-for="column in importPreview.columns" :key="column" :value="column">{{column}}</option></select></label></div><label class="source-name-field capacity-unit-field">放电容量单位<select v-model="importCapacityUnit" class="select"><option value="Ah">Ah（安时）</option><option value="mAh">mAh（导入时换算为 Ah）</option></select></label><div v-if="importPreview.missing_required_fields.length" class="import-warning">未识别出全部必填字段，请手动选择。</div><div v-else class="quality-preview"><b>预检查</b><span>无效编号/周期 {{importPreview.quality_preview.invalid_battery_cycle_rows}}</span><span>重复循环 {{importPreview.quality_preview.duplicate_battery_cycle_rows}}</span><span>缺少容量 {{importPreview.quality_preview.missing_capacity_rows}}</span></div><div class="import-preview-table"><table><thead><tr><th v-for="column in importPreview.columns" :key="column">{{column}}</th></tr></thead><tbody><tr v-for="(row,index) in importPreview.preview" :key="index"><td v-for="column in importPreview.columns" :key="column">{{row[column] ?? '—'}}</td></tr></tbody></table></div><div class="modal-actions"><a class="download-link" :href="`${api}/api/data/import/template`" download>下载通用模板</a><div><button class="button secondary" @click="closeImportDialog">取消</button><button class="button primary" :disabled="busy || !importRequiredReady" @click="confirmImport">{{busy ? '分析中…' : '导入并分析'}}</button></div></div><p class="import-footnote">导入仅影响当前通用分析批次；NASA 建模集和 CALCE 外部测试集保持独立。</p></section></div>
    <div v-if="qualityReport" class="modal-backdrop" @click.self="qualityReport = null"><section class="import-modal quality-report"><div class="modal-heading"><div><div class="eyebrow">DATA QUALITY</div><h2>导入检查结果</h2><p>数据已进入当前分析批次</p></div><button class="close-button" @click="qualityReport = null">×</button></div><div class="stats-grid record-stats"><article class="stat-card"><div class="stat-top"><span>导入记录</span></div><strong>{{qualityReport.total_records.toLocaleString()}}</strong></article><article class="stat-card"><div class="stat-top"><span>可用容量记录</span></div><strong>{{qualityReport.valid_capacity_records.toLocaleString()}}</strong></article><article class="stat-card"><div class="stat-top"><span>暂不参与 SOH</span></div><strong>{{qualityReport.excluded_capacity_records.toLocaleString()}}</strong></article><article class="stat-card"><div class="stat-top"><span>缺失容量</span></div><strong>{{qualityReport.missing_capacity_records.toLocaleString()}}</strong></article><article class="stat-card"><div class="stat-top"><span>非正容量</span></div><strong>{{qualityReport.non_positive_capacity_records.toLocaleString()}}</strong></article></div><div class="explain"><span class="explain-icon">i</span><p>{{qualityReport.batteries}} 块电芯；生成 {{qualityReport.soh_records}} 条 SOH 结果和 {{qualityReport.anomaly_records}} 条异常筛查结果。重复电芯循环会阻止导入。可到“健康评估”查看趋势，到“异常检测”记录复核状态。</p></div><div class="modal-actions"><span></span><button class="button primary" @click="qualityReport = null">完成</button></div></section></div>
    <div v-if="message" class="toast">{{message}}</div>
  </div>
</template>

<style>
:root {
  font-family: Inter, "Microsoft YaHei", "Noto Sans SC", sans-serif;
  color: #202944;
  background: #f4f6fb;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  --ink: #202944;
  --muted: #7e89a1;
  --line: #e8edf5;
  --blue: #526ee5;
  --blue-dark: #405bd0;
  --surface: #fff;
  --shadow: 0 12px 32px rgba(38, 54, 92, .055);
}
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; background: radial-gradient(ellipse at 72% 0%, #e9efff 0, transparent 30%), #f4f6fb; }
button, input, select { font: inherit; }
button, a, select { -webkit-tap-highlight-color: transparent; }
button:focus-visible, a:focus-visible, select:focus-visible, input:focus-visible { outline: 3px solid #9cafff; outline-offset: 2px; }
.shell { min-height: 100vh; }
.sidebar { width: 246px; position: fixed; inset: 0 auto 0 0; z-index: 5; display: flex; flex-direction: column; padding: 25px 17px 18px; background: rgba(255,255,255,.94); border-right: 1px solid rgba(225,231,242,.9); box-shadow: 8px 0 28px rgba(39,53,87,.025); backdrop-filter: blur(14px); }
.brand { display: flex; align-items: center; gap: 12px; padding: 2px 9px 38px; }
.brand-mark { width: 40px; height: 40px; display: grid; place-items: center; border-radius: 13px; color: #fff; font-size: 20px; font-weight: 750; background: linear-gradient(145deg,#6d86f2,#405bd0); box-shadow: 0 8px 18px rgba(76,105,222,.26); }
.brand b { color: var(--ink); font-size: 14px; letter-spacing: -.2px; }
.brand small { display: block; margin-top: 4px; color: #9aa5ba; font-size: 8px; font-weight: 650; letter-spacing: 1.25px; }
.nav-label { padding: 0 12px 10px; color: #a0a9bb; font-size: 9px; font-weight: 750; letter-spacing: 1.3px; text-transform: uppercase; }
.nav-item { position: relative; display: flex; align-items: center; gap: 11px; width: 100%; height: 43px; margin: 3px 0; padding: 0 12px; border: 0; border-radius: 11px; color: #707b92; background: transparent; text-align: left; font-size: 12px; font-weight: 550; cursor: pointer; transition: color .18s ease, background .18s ease, transform .18s ease; }
.nav-item:hover { color: #415dcc; background: #f4f6ff; transform: translateX(2px); }
.nav-item.selected { color: #405bd0; background: linear-gradient(100deg,#edf1ff,#f5f7ff); font-weight: 700; }
.nav-item.selected::before { content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px; border-radius: 4px; background: var(--blue); }
.nav-icon { width: 20px; color: #8e9bb6; text-align: center; font-size: 16px; }
.nav-item.selected .nav-icon { color: var(--blue); }
.secondary-nav { margin-top: 4px; }
.sidebar-foot { position: relative; margin-top: auto; padding: 17px 7px 3px 24px; border-top: 1px solid #edf0f5; color: #71809a; font-size: 11px; font-weight: 600; }
.sidebar-foot small { display: block; margin-top: 6px; color: #a0a9b9; font-size: 10px; font-weight: 450; }
.pulse { position: absolute; left: 7px; top: 22px; width: 7px; height: 7px; border-radius: 50%; background: #4fbd94; box-shadow: 0 0 0 4px #e9f7f0; }
.main { min-height: 100vh; margin-left: 246px; }
.topbar { position: sticky; top: 0; z-index: 4; height: 64px; display: flex; align-items: center; justify-content: space-between; padding: 0 38px; border-bottom: 1px solid rgba(227,232,242,.85); background: rgba(255,255,255,.85); backdrop-filter: blur(16px); }
.crumb { color: #66738d; font-size: 12px; font-weight: 650; letter-spacing: .1px; }
.upload-btn, .button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 38px; padding: 0 14px; border: 1px solid #e2e7f1; border-radius: 10px; color: #4f5d79; background: #fff; box-shadow: 0 2px 6px rgba(37,52,89,.025); font-size: 11px; font-weight: 700; cursor: pointer; transition: transform .17s ease, box-shadow .17s ease, background .17s ease; }
.upload-btn:hover, .button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 7px 17px rgba(46,64,111,.09); }
.upload-btn { color: #fff; border-color: transparent; background: linear-gradient(135deg,#5f7cef,#4b67da); box-shadow: 0 7px 16px rgba(76,105,222,.19); }
.upload-btn:hover { background: linear-gradient(135deg,#526fe6,#405bd0); }
.upload-btn input { display: none; }
.content { width: min(100%, 1420px); margin: 0 auto; padding: 36px 40px 60px; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; margin-bottom: 25px; }
.eyebrow { margin-bottom: 9px; color: #657bd2; font-size: 9px; font-weight: 800; letter-spacing: 1.55px; }
.page-heading h1 { margin: 0; color: var(--ink); font-size: clamp(22px,2.1vw,29px); font-weight: 750; line-height: 1.25; letter-spacing: -.65px; }
.page-heading p { margin: 9px 0 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.button.primary { color: #fff; border-color: transparent; background: linear-gradient(135deg,#607df0,#4c68dd); box-shadow: 0 7px 16px rgba(76,105,222,.18); }
.button.primary:hover:not(:disabled) { background: linear-gradient(135deg,#526fe6,#405bd0); }
.button.secondary { color: #586680; border-color: #e2e8f1; background: rgba(255,255,255,.94); }
.button:disabled { opacity: .54; cursor: wait; }
.select { min-height: 38px; padding: 0 34px 0 12px; border: 1px solid #e1e7f0; border-radius: 10px; color: #485672; background-color: #fff; font-size: 11px; font-weight: 600; }
.stats-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 14px; margin-bottom: 17px; }
.stat-card, .panel { border: 1px solid rgba(229,234,243,.95); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow); }
.stat-card { position: relative; min-width: 0; overflow: hidden; padding: 17px 18px 16px; transition: transform .18s ease, box-shadow .18s ease; }
.stat-card:hover { transform: translateY(-2px); box-shadow: 0 15px 32px rgba(38,54,92,.09); }
.stat-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: #7e89a1; font-size: 10px; font-weight: 650; }
.stat-top i { color: #8b9be1; font-size: 17px; font-style: normal; }
.stat-card strong { display: block; margin-top: 8px; color: #202b47; font-size: clamp(22px,2vw,27px); font-weight: 750; line-height: 1.35; letter-spacing: -.7px; }
.stat-card strong.source-value { overflow: hidden; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.stat-card small { display: block; margin-top: 3px; color: #98a2b4; font-size: 9px; line-height: 1.55; }
.stat-line { position: absolute; right: 0; bottom: 0; left: 0; height: 3px; opacity: .72; }
.blue { background: #6784ed; } .green { background: #55bd98; } .violet { background: #9276dc; } .orange { background: #efa45f; }
.panel { margin-bottom: 16px; padding: 21px 23px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.panel h2 { margin: 0; color: #26314d; font-size: 13px; font-weight: 750; letter-spacing: -.15px; }
.panel-head p { margin: 6px 0 0; color: #96a0b1; font-size: 10px; line-height: 1.6; }
.tag, .pill { display: inline-flex; align-items: center; border-radius: 99px; padding: 6px 10px; color: #6477bd; background: #f1f4ff; font-size: 9px; font-weight: 650; white-space: nowrap; }
.chart { width: 100%; height: 245px; margin-top: 13px; }
.chart.tall { height: 350px; }
.workflow { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 12px; margin-top: 19px; }
.workflow button { position: relative; display: grid; grid-template-columns: 36px 1fr; align-items: center; column-gap: 10px; min-height: 77px; padding: 13px 14px; border: 1px solid #e9edf5; border-radius: 13px; text-align: left; background: linear-gradient(145deg,#fff,#fbfcff); cursor: pointer; transition: border-color .18s ease, transform .18s ease, box-shadow .18s ease; }
.workflow button:hover { transform: translateY(-2px); border-color: #ced8ff; box-shadow: 0 10px 22px rgba(67,88,159,.08); }
.workflow button>b { grid-row: span 2; display: grid; place-items: center; width: 34px; height: 34px; border-radius: 11px; color: #526bd3; background: #eef2ff; font-size: 10px; font-weight: 800; }
.workflow button strong { color: #43506d; font-size: 11px; font-weight: 750; }
.workflow button small { margin-top: 4px; color: #97a1b4; font-size: 9px; line-height: 1.45; }
.text-action { border: 0; padding: 7px 0; color: #526bd3; background: transparent; font-size: 10px; font-weight: 700; cursor: pointer; }
.explain { display: flex; align-items: flex-start; gap: 11px; border: 1px solid #e9eef8; border-radius: 12px; background: linear-gradient(110deg,#f7f9ff,#fafbfe); }
.explain-icon { display: grid; flex: 0 0 20px; place-items: center; width: 20px; height: 20px; border-radius: 50%; color: #6078d6; background: #e9efff; font-size: 10px; font-weight: 750; }
.explain p { margin: 0; color: #71809a; font-size: 10px; line-height: 1.85; }
.assessment-card { display: flex; align-items: center; justify-content: space-between; gap: 30px; padding: 24px 26px; }
.assessment-card h2 { margin: 8px 0; color: #27324e; font-size: 20px; font-weight: 750; }
.assessment-card p { max-width: 650px; color: #7d89a1; font-size: 11px; line-height: 1.8; }
.assessment-card>div:first-child small, .assessment-value small { color: #929db0; font-size: 10px; }
.assessment-value { display: grid; justify-items: start; gap: 8px; min-width: 205px; }
.assessment-value strong { color: #25314d; font-size: 38px; font-weight: 780; letter-spacing: -1px; }
.status-pill { display: inline-flex; border-radius: 99px; padding: 6px 10px; font-size: 10px; font-weight: 700; }
.status-pill.good { color: #328566; background: #e9f7f0; }
.status-pill.warn { color: #ad702d; background: #fff4e5; }
.status-pill.neutral { color: #6e7b91; background: #f0f2f6; }
.detail-strip { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); margin-bottom: 16px; padding: 16px 20px; border: 1px solid var(--line); border-radius: 14px; background: #fff; box-shadow: var(--shadow); }
.detail-strip div+div { padding-left: 20px; border-left: 1px solid #edf0f5; }
.detail-strip small { display: block; margin-bottom: 7px; color: #9aa4b6; font-size: 9px; }
.detail-strip b { color: #4a5670; font-size: 11px; font-weight: 700; }
.table-panel { padding-bottom: 9px; }
.table-wrap { max-height: 510px; overflow: auto; margin-top: 15px; border: 1px solid #edf0f5; border-radius: 11px; }
table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 10px; white-space: nowrap; }
th { position: sticky; top: 0; z-index: 1; color: #75819a; background: #f7f9fc; text-align: left; font-size: 9px; font-weight: 750; }
td, th { padding: 11px 12px; border-bottom: 1px solid #eef1f6; }
td { color: #596780; }
td b { color: #37445f; font-weight: 700; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #f8faff; }
.record-actions { display: flex; align-items: center; gap: 9px; }
.record-stats { grid-template-columns: repeat(2,minmax(0,280px)); }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 2px 2px; color: #8792a7; font-size: 10px; }
.pagination>div { display: flex; gap: 8px; }
.empty-state { padding: 35px; color: #95a0b2; text-align: center; white-space: normal; line-height: 1.8; }
.download-link { color: #526bd3; font-size: 10px; font-weight: 650; text-decoration: none; }
.download-link:hover { color: #354fbd; text-decoration: underline; }
.link-button { text-decoration: none; }
.review-select { min-width: 96px; padding: 7px 25px 7px 9px; }
.note-input { min-width: 135px; padding: 8px 9px; border: 1px solid #e1e7f0; border-radius: 8px; color: #4d5b75; background: #fff; font-size: 10px; }
.metric-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 14px; margin-bottom: 16px; }
.metric-card { margin: 0; padding: 21px; }
.metric-card small { color: #8793a9; font-size: 10px; font-weight: 650; }
.metric-card strong { display: block; margin-top: 15px; color: #25314d; font-size: 25px; font-weight: 760; }
.metric-card em { color: #96a1b2; font-size: 9px; font-style: normal; font-weight: 600; }
.metric-meta { margin-top: 9px; color: #8894aa; font-size: 10px; }
.metric-meta span { margin-left: 12px; }
.external-panel { margin-top: 16px; }
.external-cells, .external-actions, .external-downloads { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-top: 14px; }
.external-actions { justify-content: space-between; color: #8591a6; font-size: 10px; }
.external-downloads { padding-top: 12px; border-top: 1px solid #edf0f5; }
.external-downloads a { margin-right: 7px; }
.muted { color: #99a4b5; font-size: 10px; }
.modal-backdrop { position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; padding: 20px; background: rgba(24,34,59,.46); backdrop-filter: blur(5px); }
.import-modal { width: min(900px,100%); max-height: 92vh; overflow: auto; padding: 25px; border: 1px solid rgba(255,255,255,.7); border-radius: 18px; background: #fff; box-shadow: 0 28px 80px rgba(22,35,72,.22); }
.modal-heading { display: flex; align-items: flex-start; justify-content: space-between; }
.modal-heading h2 { margin: 0; color: #27324e; font-size: 20px; font-weight: 750; }
.modal-heading p { margin: 7px 0 0; color: #8995aa; font-size: 10px; }
.close-button { width: 32px; height: 32px; border: 0; border-radius: 50%; color: #64708a; background: #f1f3f8; font-size: 21px; cursor: pointer; }
.source-name-field { display: grid; gap: 7px; margin: 15px 0; color: #55627c; font-size: 10px; font-weight: 700; }
.source-name-field input, .mapping-field select { height: 38px; padding: 0 11px; border: 1px solid #e1e7f0; border-radius: 9px; color: #4d5b75; background: #fff; }
.mapping-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 11px 15px; }
.mapping-field { display: grid; gap: 6px; color: #55627c; font-size: 10px; }
.mapping-field span { display: flex; align-items: center; gap: 6px; font-weight: 650; }
.mapping-field b { padding: 3px 6px; border-radius: 99px; color: #a86f30; background: #fff4e5; font-size: 8px; font-weight: 650; }
.capacity-unit-field { max-width: 300px; }
.quality-preview { display: flex; flex-wrap: wrap; gap: 13px; padding: 11px 13px; border-radius: 9px; color: #63718a; background: #f5f7fb; font-size: 9px; }
.quality-preview b { font-weight: 750; }
.import-warning { padding: 11px 13px; border-radius: 9px; color: #a86f2d; background: #fff5e9; font-size: 10px; }
.import-preview-table { max-height: 180px; overflow: auto; margin-top: 13px; border: 1px solid #edf0f5; border-radius: 10px; }
.import-preview-table table { width: max-content; min-width: 100%; white-space: nowrap; }
.modal-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; }
.modal-actions>div { display: flex; gap: 8px; }
.import-footnote { margin: 12px 0 0; color: #919caf; font-size: 9px; line-height: 1.7; }
.quality-report { max-width: 760px; }
.quality-report .stats-grid { grid-template-columns: repeat(3,minmax(0,1fr)); margin-top: 18px; }
.quality-report .stat-card strong { font-size: 20px; }
.toast { position: fixed; right: 22px; bottom: 22px; z-index: 15; max-width: 500px; padding: 13px 16px; border: 1px solid rgba(255,255,255,.12); border-radius: 12px; color: #fff; background: #27334f; box-shadow: 0 14px 34px rgba(23,34,60,.22); font-size: 11px; }
@media (max-width: 1100px) {
  .sidebar { width: 210px; }
  .main { margin-left: 210px; }
  .content { padding: 30px 25px 48px; }
  .topbar { padding: 0 25px; }
  .stats-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .metric-grid { grid-template-columns: 1fr; }
}
@media (max-width: 820px) {
  .assessment-card { align-items: flex-start; flex-direction: column; gap: 8px; }
  .assessment-value { min-width: 0; }
  .workflow { grid-template-columns: 1fr; }
  .detail-strip { grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px; }
  .detail-strip div+div { padding-left: 0; border-left: 0; }
}
@media (max-width: 680px) {
  body { background: #f4f6fb; }
  .sidebar { position: sticky; top: 0; width: 100%; height: auto; display: block; overflow-x: auto; padding: 10px 12px 8px; border-right: 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
  .brand { padding: 0 5px 9px; }
  .brand-mark { width: 32px; height: 32px; border-radius: 10px; font-size: 16px; }
  .brand b { font-size: 12px; }
  .brand small { font-size: 7px; }
  .nav-label, .sidebar-foot { display: none; }
  .sidebar .nav-item { display: inline-flex; width: auto; height: 32px; margin: 0 2px; padding: 0 9px; font-size: 9px; }
  .nav-icon { display: none; }
  .main { margin: 0; }
  .topbar { position: sticky; top: 84px; height: 48px; padding: 0 13px; }
  .content { padding: 22px 13px 40px; }
  .page-heading { align-items: flex-start; flex-direction: column; gap: 12px; margin-bottom: 17px; }
  .page-heading h1 { font-size: 21px; }
  .page-heading p { font-size: 10px; }
  .stats-grid { gap: 9px; }
  .stat-card { padding: 12px; border-radius: 13px; }
  .stat-card strong { font-size: 20px; }
  .panel { padding: 15px; border-radius: 14px; }
  .chart { height: 210px; }
  .chart.tall { height: 260px; }
  .record-actions { width: 100%; flex-wrap: wrap; }
  .record-stats { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .pagination { align-items: flex-start; flex-direction: column; }
  .mapping-grid { grid-template-columns: 1fr; }
  .modal-backdrop { padding: 9px; }
  .import-modal { padding: 16px; border-radius: 15px; }
  .modal-actions { align-items: flex-start; flex-direction: column; }
  .modal-actions>div { width: 100%; justify-content: flex-end; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; }
}
</style>
