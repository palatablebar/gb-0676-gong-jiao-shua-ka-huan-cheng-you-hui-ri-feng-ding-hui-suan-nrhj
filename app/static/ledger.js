const state = { device: null, day: null, card: null, version: null };
const fb = document.querySelector('#ledgerFeedback');
async function call(url, options) {
  fb.textContent = '处理中…';
  const res = await fetch(url, options);
  const body = await res.json();
  if (!res.ok) { fb.textContent = body.message || '请求失败'; throw new Error(body.message); }
  fb.textContent = '';
  return body;
}
async function loadStreams() {
  const body = await fetch('/api/snapshots/streams').then(r => r.json());
  const tbody = document.querySelector('#streams');
  if (!body.streams.length) { tbody.innerHTML = '<tr><td class="empty">暂无快照流。</td></tr>'; return; }
  tbody.innerHTML = body.streams.map(s =>
    `<tr><td>${s.device_id}</td><td>${s.business_day}</td><td>${s.snapshots}</td><td>${s.complete_snapshots}</td>` +
    `<td><button class="primary pick" data-d="${s.device_id}" data-day="${s.business_day}">查看卡日账</button></td></tr>`).join('');
  tbody.querySelectorAll('.pick').forEach(b => b.addEventListener('click', async () => {
    const d = await call(`/api/snapshots/${b.dataset.d}/${b.dataset.day}`);
    state.device = b.dataset.d; state.day = b.dataset.day;
    renderCards(d);
  }));
}
function renderCards(d) {
  const wrap = document.querySelector('#ledgerView');
  wrap.hidden = false;
  const cards = d.ledgers.map(l =>
    `<button class="cardpick" data-card="${l.card}">${l.card}${l.published ? ' · V' + l.published.version + ' ' + l.published.status : ' · 未发布'}</button>`).join('');
  document.querySelector('#ledgerTitle').textContent = `车机 ${state.device} · 业务日 ${state.day} · 选择卡号`;
  document.querySelector('#ledgerMeta').innerHTML = cards;
  document.querySelector('#ledgerMeta').querySelectorAll('.cardpick').forEach(b =>
    b.addEventListener('click', () => openLedger(b.dataset.card)));
  if (state.card) openLedger(state.card);
}
async function openLedger(card) {
  state.card = card;
  const l = await call(`/api/ledgers/${card}/${state.day}`);
  state.version = l.version;
  document.querySelector('#ledgerTitle').textContent = `卡号 ${card} · 业务日 ${state.day} · V${l.version} ${l.status}`;
  document.querySelector('#ledgerRows').innerHTML = l.records.map(r =>
    `<tr class="${r.status === '差异' ? 'difference' : ''}"><td>${r.event_id}</td><td>${r.route}</td><td>${r.at.slice(11, 16)}</td>` +
    `<td>${r.state}</td><td>${r.fare_version}</td><td>${r.base_fare}</td><td>${r.transfer_anchor}</td>` +
    `<td>${r.transfer_discount}</td><td>${r.cap_discount}</td><td>${r.expected}</td><td>${r.reported}</td>` +
    `<td class="${r.difference !== '0.00' ? 'bad' : ''}">${r.difference}</td><td>${r.day_paid}</td><td>${r.status}</td></tr>`
  ).join('') || '<tr><td colspan="14" class="empty">该卡日无占额事件。</td></tr>';
  document.querySelector('#sumExpected').textContent = l.metrics.expected_total;
  document.querySelector('#sumReported').textContent = l.metrics.reported_total;
  document.querySelector('#conservation').textContent =
    `分币守恒：${l.records.every(r => Number(r.base_fare) === Number(r.transfer_discount) + Number(r.cap_discount) + Number(r.expected)) ? '通过' : '失败'} · ` +
    `对平：${l.metrics.balanced ? '是' : '否'} · 换乘 ${l.metrics.transfer_hits} 次 · 触发封顶：${l.metrics.capped ? '是' : '否'}`;
  document.querySelector('#excludedRows').innerHTML = l.excluded.map(e =>
    `<tr><td>${e.event_id}</td><td>${e.state}</td><td>${e.reason}</td></tr>`
  ).join('') || '<tr><td colspan="3" class="empty">无排除事件。</td></tr>';
  document.querySelector('#versionRows').innerHTML = l.versions.map(v =>
    `<tr><td>V${v.version}</td><td>${v.status}</td><td>${(v.published_at || '—').slice(0, 19).replace('T', ' ')}</td>` +
    `<td>${(v.sealed_at || '—').slice(0, 19).replace('T', ' ')}</td><td>${v.sha256.slice(0, 16)}…</td></tr>`).join('');
  document.querySelector('#sealBtn').dataset.version = l.version;
}
document.querySelector('#publishBtn').addEventListener('click', async () => {
  const r = await call(`/api/ledgers/${state.device}/${state.day}/${state.card}/publish`, { method: 'POST' });
  fb.textContent = r.action === 'unchanged' ? '候选与现版一致，沿用旧版（不产生新版本）' :
    r.action === 'created' ? '已创建 V1（此前无发布版）' : `已追加 V${r.ledger.version}，旧版保留可查`;
  openLedger(state.card);
});
document.querySelector('#sealBtn').addEventListener('click', async () => {
  try {
    await call(`/api/ledgers/${state.card}/${state.day}/seal`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: Number(document.querySelector('#sealBtn').dataset.version) })
    });
    fb.textContent = '已封存。封存版本不可改写。';
    openLedger(state.card);
  } catch (e) { /* 错误已显示 */ }
});
loadStreams();
