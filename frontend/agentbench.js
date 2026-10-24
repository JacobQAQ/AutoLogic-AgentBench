const api = '/api/v1/agentbench';
const cards = document.getElementById('taskCards');
const detail = document.getElementById('taskDetail');
const runButton = document.getElementById('runButton');
const statusLine = document.getElementById('runStatus');
const traceList = document.getElementById('traceList');
const results = document.getElementById('results');
let tasks = [];
let selected = null;
let busy = false;

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function clear(node) { node.replaceChildren(); }
function formatJson(value) { return JSON.stringify(value, null, 2); }
function status(message, error = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle('error', error);
}
function selectTask(task) {
  selected = task;
  for (const button of cards.querySelectorAll('button')) button.classList.toggle('active', button.dataset.id === task.task_id);
  clear(detail);
  detail.append(el('b', '', '任务目标'), el('p', '', task.goal), el('b', '', '评测重点'), el('p', '', task.focus));
}
async function loadTasks() {
  try {
    const response = await fetch(`${api}/tasks`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    tasks = data.tasks;
    document.getElementById('modelName').textContent = data.model_identity;
    document.getElementById('modelId').textContent = data.model_id;
    clear(cards);
    for (const task of tasks) {
      const button = el('button', 'task-card');
      button.type = 'button'; button.dataset.id = task.task_id;
      const copy = el('span'); copy.append(el('small', '', task.task_id), el('strong', '', task.title));
      button.append(copy, el('span', 'arrow', '↗'));
      button.addEventListener('click', () => selectTask(task));
      cards.append(button);
    }
    selectTask(tasks[0]);
    runButton.disabled = !data.has_api_key;
    status(data.has_api_key ? '已连接本地后端。选择任务后即可运行。' : '未配置 DEEPSEEK_API_KEY；请在后端进程环境变量或 backend/.env 中设置。', !data.has_api_key);
  } catch (error) {
    cards.textContent = '无法加载任务。';
    status(`无法连接本地后端：${error.message}`, true);
  }
}
function renderTrace(run) {
  clear(traceList); traceList.classList.remove('empty-state');
  document.getElementById('runId').textContent = run.run_id.slice(0, 8);
  const labels = {model_decision:'模型决策', tool_call:'工具调用', tool_result:'工具结果', final_answer:'最终回答', termination:'运行终止'};
  for (const step of run.steps) {
    const row = el('div', 'trace-step');
    row.append(el('span', 'trace-num', String(step.step_index).padStart(2, '0')));
    const main = el('div', 'trace-main');
    main.append(el('strong', step.status === 'error' ? 'bad' : '', `${labels[step.action_type] || step.action_type}${step.tool_name ? ' · ' + step.tool_name : ''}`));
    main.append(el('span', 'meta', `${step.status.toUpperCase()}  ·  ${step.latency_ms} ms  ·  ${step.timestamp}`));
    if (step.tool_arguments !== null || step.tool_result !== null) {
      const disclosure = el('details');
      disclosure.append(el('summary', '', '查看参数与结果'), el('pre', '', formatJson({arguments:step.tool_arguments,result:step.tool_result})));
      main.append(disclosure);
    }
    row.append(main); traceList.append(row);
  }
}
function renderResults(run) {
  clear(results); results.className = 'result-content';
  const evaluation = run.evaluation;
  const top = el('div', 'result-top');
  top.append(el('span', `verdict ${evaluation.task_success === 'FAIL' ? 'fail' : ''}`, evaluation.task_success));
  const score = el('div', 'score-num'); score.append(document.createTextNode(String(evaluation.diagnostic_score)), el('small', '', ' / 100 诊断分'), el('small', 'score-version', evaluation.evaluation_version ? `规则 v${evaluation.evaluation_version}` : '历史结果 · 旧版规则'));
  top.append(score); results.append(top);
  if (Array.isArray(run.evaluation_history) && run.evaluation_history.length) {
    const previous = run.evaluation_history[run.evaluation_history.length - 1];
    results.append(el('p', 'score-history', `评分规则复核：v${previous.evaluation_version} 为 ${previous.task_success} / ${previous.diagnostic_score}，当前以 v${evaluation.evaluation_version} 为准。`));
  }
  const grid = el('div', 'score-grid');
  for (const [key, label, max] of [['task_completion','任务完成',40],['tool_calls','工具调用',20],['evidence_reliability','证据可靠性',25],['constraint_compliance','约束遵循',15]]) {
    const item = el('div', 'score-item'); item.append(el('span', '', label), el('strong', '', `${evaluation.scores[key]} / ${max}`)); grid.append(item);
  }
  results.append(grid);
  const failures = el('section', 'result-section'); failures.append(el('h3', '', '失败类别与字段检查'));
  if (evaluation.failure_categories.length) {
    for (const category of evaluation.failure_categories) failures.append(el('span', 'tag', category));
    const list = el('ul');
    for (const finding of evaluation.findings) list.append(el('li', '', `${finding.field}${finding.step_index ? ` · 步骤 ${finding.step_index}` : ''}：${finding.message}`));
    failures.append(list);
  } else failures.append(el('p', '', '全部硬性条件通过。'));
  results.append(failures);
  const output = el('section', 'result-section'); output.append(el('h3', '', '最终结构化结果'), el('pre', 'result-json', formatJson(run.final_output ?? run.raw_final_answer)));
  results.append(output);
  const evidence = el('section', 'result-section'); evidence.append(el('h3', '', '关键证据'), el('pre', 'result-json', formatJson(evaluation.key_evidence)));
  results.append(evidence);
}
runButton.addEventListener('click', async () => {
  if (busy || !selected) return;
  busy = true; runButton.disabled = true;
  status(`正在运行 ${selected.task_id}，模型可能需要多轮工具调用…`);
  try {
    const response = await fetch(`${api}/run`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_id:selected.task_id})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    renderTrace(data); renderResults(data);
    history.replaceState(null, '', `?run_id=${encodeURIComponent(data.run_id)}`);
    status(`运行结束 · ${data.termination_reason} · Run ID ${data.run_id}`, data.termination_reason !== 'final_answer');
  } catch (error) { status(`运行失败：${error.message}`, true); }
  finally { busy = false; runButton.disabled = false; }
});
async function init() {
  await loadTasks();
  const runId = new URLSearchParams(location.search).get('run_id');
  if (!runId) return;
  try {
    const response = await fetch(`${api}/runs/${encodeURIComponent(runId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const run = await response.json();
    const task = tasks.find(item => item.task_id === run.task_id);
    if (task) selectTask(task);
    renderTrace(run); renderResults(run);
    status(`已加载运行记录 · ${run.termination_reason} · Run ID ${run.run_id}`, run.termination_reason !== 'final_answer');
  } catch (error) { status(`加载运行记录失败：${error.message}`, true); }
}
init();
