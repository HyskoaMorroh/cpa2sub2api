'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', state = null, busy = false;
  const selected = new Set();
  const needsAttention = status => status === 'saved_not_applied' || status === 'not_applied';

  function notice(message, kind = '') {
    const box = $('notice');
    box.textContent = message;
    box.className = kind;
    box.hidden = !message;
    box.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  }
  function showLogin() {
    csrf = '';
    state = null;
    selected.clear();
    $('sites').replaceChildren();
    $('master-desired').textContent = '关闭';
    $('master-effective').textContent = '关闭';
    $('summary').textContent = '';
    $('search').value = '';
    $('category').replaceChildren(new Option('全部类别', ''));
    $('workspace').hidden = true;
    $('logout').hidden = true;
    $('login').hidden = false;
  }
  async function api(path, method = 'GET', body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120000);
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (csrf) headers['X-CSRF-Token'] = csrf;
      const response = await fetch(path, { method, headers, credentials: 'same-origin',
        body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 401) showLogin();
        throw new Error(data.error?.message || '操作未完成，请刷新状态后重试。');
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('管理端响应超时，保存状态可能已改变。请刷新确认后重试校准。');
      if (error instanceof TypeError) throw new Error('连接中断，保存状态可能已改变。请刷新确认后重试。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function setBusy(value) {
    busy = value;
    $('workspace').setAttribute('aria-busy', String(value));
    document.querySelectorAll('button, input, select').forEach(el => {
      if (value) { el.dataset.previousDisabled = el.disabled ? 'yes' : 'no'; el.disabled = true; }
      else { el.disabled = el.dataset.previousDisabled === 'yes'; delete el.dataset.previousDisabled; }
    });
    if (!value && state) renderRows();
  }
  async function perform(action) {
    if (busy) return;
    setBusy(true);
    try { await action(); }
    catch (error) { notice(error.message, 'error'); }
    finally { setBusy(false); }
  }
  function visibleSites() {
    const query = $('search').value.trim().toLowerCase(), category = $('category').value;
    return (state?.sites || []).filter(row => (!category || row.categories.includes(category)) &&
      (!query || [row.host, ...row.categories].join(' ').toLowerCase().includes(query)));
  }
  function badge(text, kind) {
    const element = document.createElement('span');
    element.className = `badge ${kind}`;
    element.textContent = text;
    return element;
  }
  function updateMasterControl() {
    if (!state) return;
    const shouldClose = state.master.desired || state.master.effective;
    const hardDisabled = state.master.hardDisabled === true;
    $('master-toggle').textContent = shouldClose ? '关闭总闸' : hardDisabled ? '环境已硬禁用' : '开启总闸';
    $('master-toggle').classList.toggle('danger', shouldClose);
    $('master-toggle').disabled = busy || (hardDisabled && !shouldClose);
  }
  function updateSelection() {
    if (!state) return;
    updateMasterControl();
    const visible = visibleSites().filter(row => !row.locked);
    const chosen = state.sites.filter(row => selected.has(row.host) && !row.locked);
    $('selected-count').textContent = `已选 ${chosen.length} 站`;
    $('enable-selected').disabled = busy || state.master.hardDisabled === true || !chosen.length || chosen.some(row => !row.configured);
    $('disable-selected').disabled = busy || !chosen.length;
    $('select-all').disabled = busy || !visible.length;
    $('select-all').checked = !!visible.length && visible.every(row => selected.has(row.host));
    $('select-all').indeterminate = visible.some(row => selected.has(row.host)) && !$('select-all').checked;
  }
  function renderRows() {
    const focusedHost = document.activeElement?.dataset.host;
    const focusedKind = document.activeElement?.dataset.kind;
    const container = $('sites');
    container.replaceChildren();
    const visible = visibleSites();
    $('empty').hidden = !!visible.length;
    visible.forEach(row => {
      const tr = document.createElement('tr');
      const chooseCell = document.createElement('td');
      const check = document.createElement('input');
      check.type = 'checkbox'; check.checked = selected.has(row.host); check.disabled = row.locked || busy;
      check.dataset.host = row.host; check.dataset.kind = 'selection';
      check.setAttribute('aria-label', `选择 ${row.host}`);
      check.addEventListener('change', () => { check.checked ? selected.add(row.host) : selected.delete(row.host); updateSelection(); });
      chooseCell.append(check);
      const nameCell = document.createElement('td'); nameCell.className = 'site-name';
      const host = document.createElement('span'); host.className = 'host'; host.textContent = row.host;
      const categories = document.createElement('div'); categories.className = 'categories'; categories.textContent = row.categories.join(' / ');
      const reason = document.createElement('span'); reason.className = 'reason'; reason.textContent = row.reason;
      if (!row.configured) reason.textContent += ' 本地配置已移除，只能关闭历史授权。';
      nameCell.append(host, categories, reason);
      const desired = document.createElement('td'); desired.className = 'desired-cell'; desired.dataset.label = '保存授权';
      desired.append(badge(row.desired ? '开启' : '关闭', row.desired ? 'on' : 'off'));
      const effective = document.createElement('td'); effective.className = 'effective-cell'; effective.dataset.label = '本次有效';
      const environmentLocked = state.master.hardDisabled === true;
      effective.append(badge(row.locked ? '永久禁探' : environmentLocked ? '环境禁用' : row.effective ? '开启' : '关闭', row.locked ? 'locked' : environmentLocked ? 'warning' : row.effective ? 'on' : row.desired ? 'warning' : 'off'));
      const action = document.createElement('td');
      const button = document.createElement('button'); button.className = `quiet row-action${row.desired ? ' danger' : ''}`;
      button.dataset.host = row.host; button.dataset.kind = 'action';
      const shouldClose = row.desired || row.effective;
      button.classList.toggle('danger', shouldClose);
      button.textContent = row.locked ? '已锁定' : shouldClose ? '关闭' : environmentLocked ? '环境禁用' : '开启';
      button.disabled = busy || row.locked || (!shouldClose && (environmentLocked || !row.configured));
      button.setAttribute('aria-label', `${shouldClose ? '关闭' : '开启'} ${row.host} 的检测授权`);
      button.addEventListener('click', () => perform(() => save({ hosts: [row.host], enabled: !shouldClose })));
      action.append(button); tr.append(chooseCell, nameCell, desired, effective, action); container.append(tr);
    });
    updateSelection();
    if (focusedHost) [...container.querySelectorAll('[data-host]')].find(element =>
      element.dataset.host === focusedHost && element.dataset.kind === focusedKind && !element.disabled)?.focus();
  }
  function render(next) {
    state = next;
    selected.forEach(host => { if (!state.sites.some(row => row.host === host && !row.locked)) selected.delete(host); });
    $('workspace').hidden = false; $('login').hidden = true; $('logout').hidden = false;
    $('master-desired').textContent = state.master.desired ? '开启' : '关闭';
    $('master-effective').textContent = state.master.effective ? '开启' : '关闭';
    updateMasterControl();
    $('master-explanation').textContent = state.master.hardDisabled === true
      ? state.master.hardDisableReason || '运行环境已硬禁用线上检测。网页不能开启；仍可关闭已保存的授权。'
      : '总闸和每站授权同时允许时才可检测。环境变量仅提供初始默认值；网页保存的开关优先，开启总闸只保存授权。';
    const effectiveCount = state.sites.filter(row => row.effective).length;
    const lockedCount = state.sites.filter(row => row.locked).length;
    $('summary').textContent = `共 ${state.sites.length} 站 · 本次有效 ${effectiveCount} 站 · 永久禁探 ${lockedCount} 站${state.master.hardDisabled === true ? ' · 环境硬禁用' : ''}${state.environmentHostsOverride ? ' · 环境站点清单限制生效范围' : ''}`;
    $('recovery-status').textContent = state.autoRecoveryEnabled ? '自动恢复策略：开启；计划仍受总闸、站级授权和永久禁探约束。' : '自动恢复策略：关闭；校准不会启用检测计划。';
    const previous = $('category').value;
    const options = [new Option('全部类别', '')];
    [...new Set(state.sites.flatMap(row => row.categories))].sort().forEach(name => options.push(new Option(name, name)));
    $('category').replaceChildren(...options); $('category').value = previous;
    renderRows();
  }
  async function save(payload) {
    notice(payload.master === false || payload.enabled === false
      ? '正在保存关闭设置并停用相关计划；管理端限流时需要等待，请勿重复操作。'
      : '正在保存授权；此操作不会执行检测或创建计划。');
    const result = await api('/api/settings', 'PATCH', payload);
    render(result.state);
    notice(result.message, needsAttention(result.applyStatus) ? 'warning' : '');
  }
  $('login-form').addEventListener('submit', event => {
    event.preventDefault();
    perform(async () => {
      const token = $('token').value;
      $('token').value = '';
      const result = await api('/api/session', 'POST', { token });
      csrf = result.csrf;
      render(await api('/api/state'));
      notice('已登录。站点列表来自本地配置；读取和保存开关不会发起上游检测。');
    });
  });
  $('logout').addEventListener('click', () => perform(async () => { await api('/api/session', 'DELETE'); showLogin(); notice('已退出登录。'); }));
  $('refresh').addEventListener('click', () => perform(async () => { const next = await api('/api/state'); render(next); notice(next.lastApplication?.message || '已刷新本地授权状态。', needsAttention(next.lastApplication?.applyStatus) ? 'warning' : ''); }));
  $('master-toggle').addEventListener('click', () => perform(() => save({ master: !(state.master.desired || state.master.effective) })));
  $('search').addEventListener('input', renderRows); $('category').addEventListener('change', renderRows);
  $('select-all').addEventListener('change', event => { visibleSites().filter(row => !row.locked).forEach(row => event.target.checked ? selected.add(row.host) : selected.delete(row.host)); renderRows(); });
  $('enable-selected').addEventListener('click', () => perform(() => save({ hosts: [...selected], enabled: true })));
  $('disable-selected').addEventListener('click', () => perform(() => save({ hosts: [...selected], enabled: false })));
  $('reconcile').addEventListener('click', () => perform(async () => { notice('正在校准管理计划，请等待管理端返回。'); const result = await api('/api/reconciliation', 'POST', {}); render(result.state); notice(result.message, result.ok ? '' : 'warning'); }));
  perform(async () => {
    try { const session = await api('/api/session'); csrf = session.csrf; const next = await api('/api/state'); render(next); if (next.lastApplication) notice(next.lastApplication.message, needsAttention(next.lastApplication.applyStatus) ? 'warning' : ''); }
    catch (error) { if (csrf) throw error; showLogin(); }
  });
})();
