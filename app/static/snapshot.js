const feedback = document.querySelector('#feedback');
const state = { device: null, day: null };
const STATE_LABEL = {
  active: ['采纳', 'ok'], updated: ['更新', 'info'], offline_pending: ['掉线挂起（占额）', 'warn'],
  stream_gap: ['断档挂起（占额）', 'warn'], withdrawn: ['已撤回（释放）', 'bad'],
  manual_withdraw: ['人工撤回', 'bad'], revived: ['撤回后复活', 'ok'], manual_retain: ['人工保留', 'ok']
};
const ACTION_LABEL = { RETAIN: '保留', UPDATE: '更新', SUSPEND: '挂起', WITHDRAW: '撤回', REVIVE: '复活', MANUAL_WITHDRAW: '人工撤回', MANUAL_RETAIN: '人工保留' };

async function call(url, options) {
  feedback.textContent = '处理中…';
  try {
    const res = await fetch(url, options);
    const body = await res.json();
    if (!res.ok) throw Error(body.message || '请求失败');
    feedback.textContent = '';
    return body;
  } catch (e) { feedback.textContent = e.message; throw e; }
}

const HEADER = 'snapshot_seq,device_id,snapshot_time,is_complete,event_id,card,route,event_time,result,reported';
function csv(seq, complete, time, rows) {
  return [HEADER, ...rows.map(r => [seq, 'D1', time, complete, ...r].join(','))].join('\n') + '\n';
}
const DEMO = [
  csv(1, 'true', '2026-11-16T09:00:00+08:00', [
    ['E1', 'C1', '01', '2026-11-16T08:00:00+08:00', 'success', '2.50'],
    ['E2', 'C1', '02', '2026-11-16T08:15:00+08:00', 'success', '1.50'],
    ['E3', 'C1', '03', '2026-11-16T08:40:00+08:00', 'success', '2.50'],
  ]),
  // 重试：同号同内容，必须被识别为重复而非二次占额
  csv(1, 'true', '2026-11-16T09:00:00+08:00', [
    ['E1', 'C1', '01', '2026-11-16T08:00:00+08:00', 'success', '2.50'],
    ['E2', 'C1', '02', '2026-11-16T08:15:00+08:00', 'success', '1.50'],
    ['E3', 'C1', '03', '2026-11-16T08:40:00+08:00', 'success', '2.50'],
  ]),
  // 车机短暂掉线：E3 缺席一次完整快照，应挂起而非撤回
  csv(2, 'true', '2026-11-16T10:00:00+08:00', [
    ['E1', 'C1', '01', '2026-11-16T08:00:00+08:00', 'success', '2.50'],
    ['E2', 'C1', '02', '2026-11-16T08:15:00+08:00', 'success', '1.50'],
  ]),
  // E3 恢复；E2 金额被修正（更新留痕）
  csv(3, 'true', '2026-11-16T11:00:00+08:00', [
    ['E1', 'C1', '01', '2026-11-16T08:00:00+08:00', 'success', '2.50'],
    ['E2', 'C1', '02', '2026-11-16T08:15:00+08:00', 'success', '2.50'],
    ['E3', 'C1', '03', '2026-11-16T08:40:00+08:00', 'success', '2.50'],
  ]),
  // 序号断档（#4 缺失）：E1 缺席只能挂起 STREAM_GAP，永不自动撤回
  csv(5, 'true', '2026-11-16T12:00:00+08:00', [
    ['E2', 'C1', '02', '2026-11-16T08:15:00+08:00', 'success', '2.50'],
    ['E3', 'C1', '03', '2026-11-16T08:40:00+08:00', 'success', '2.50'],
  ]),
];

document.querySelector('#ingest').addEventListener('click', async () => {
  const file = document.querySelector('#snapfile').files[0];
  if (!file) { feedback.textContent = '请选择快照 CSV 文件。'; return; }
  const data = new FormData(); data.append('file', file);
  await call('/api/snapshots/ingest', { method: 'POST', body: data });
  loadStreams();
});
document.querySelector('#loadDemo').addEventListener('click', async () => {
  for (const text of DEMO) {
    await call('/api/snapshots/ingest-text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csv: text })
    });
  }
  feedback.textContent = '演示序列已摄入（含一次同号重试与一次序号断档）。';
  loadStreams();
});

async function loadStreams() {
  const body = await fetch('/api/snapshots/streams').then(r => r.json());
  document.querySelector('#streamCount').textContent = body.streams.length + ' 条流';
  const tbody = document.querySelector('#streams');
  if (!body.streams.length) { tbody.innerHTML = '<tr><td class="empty">暂无快照流。</td></tr>'; return; }
  tbody.innerHTML = body.streams.map(s =>
    `<tr><td>${s.device_id}</td><td>${s.business_day}</td><td>${s.snapshots}</td><td>${s.complete_snapshots}</td>` +
    `<td>${fmt(s.first_received)}</td><td>${fmt(s.last_received)}</td>` +
    `<td><button data-d="${s.device_id}" data-day="${s.business_day}" class="primary open">打开</button></td></tr>`).join('');
  tbody.querySelectorAll('.open').forEach(btn => btn.addEventListener('click', () => openStream(btn.dataset.d, btn.dataset.day)));
  if (state.device) openStream(state.device, state.day);
}

async function openStream(device, day) {
  state.device = device; state.day = day;
  const d = await fetch(`/api/snapshots/${device}/${day}`).then(r => r.json());
  document.querySelector('#detail').hidden = false;
  document.querySelector('#streamTitle').textContent = `车机 ${device} · 业务日 ${day}`;
  document.querySelector('#streamMeta').textContent =
    `${d.snapshots.length} 份快照 · ${d.events.length} 个事件 · ${d.pending.length} 个未裁决`;
  document.querySelector('#events').innerHTML = d.events.map(ev => {
    const [label, cls] = STATE_LABEL[ev.state] || [ev.state, ''];
    const trail = ev.history.map(h =>
      `#${h.seq ?? '—'} ${ACTION_LABEL[h.action] || h.action}：${h.reason}`).join(' ｜ ');
    const manual = (ev.state === 'stream_gap' || ev.state === 'offline_pending')
      ? `<button data-e="${ev.event_id}" data-dec="withdraw" class="decide">判撤回</button>` +
        `<button data-e="${ev.event_id}" data-dec="retain" class="decide">判保留</button>` : '—';
    return `<tr class="st-${cls}"><td>${ev.event_id}</td><td>${ev.current.card}</td>` +
      `<td><span class="pill ${cls}">${label}</span></td><td>${ev.first_seen_seq}</td><td>${ev.last_seen_seq}</td>` +
      `<td>${ev.absent_complete_count}</td><td>${ev.versions.length}</td><td class="reason" title="${esc(trail)}">${esc(trail)}</td><td class="review">${manual}</td></tr>`;
  }).join('');
  document.querySelector('#events').querySelectorAll('.decide').forEach(btn => btn.addEventListener('click', async () => {
    await call(`/api/snapshots/${device}/${day}/events/${btn.dataset.e}/decision`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision: btn.dataset.dec })
    });
    openStream(device, day);
  }));
  renderSnapTable(d);
  document.querySelector('#quarantineCount').textContent = d.quarantine.length;
  document.querySelector('#quarantine').innerHTML = d.quarantine.map(q =>
    `<tr><td>${q.snapshot_seq ?? '—'}</td><td>${JSON.parse(q.payload)._line ?? '—'}</td><td>${q.reason}</td><td title="${esc(q.detail)}">${esc(q.detail)}</td></tr>`
  ).join('') || '<tr><td colspan="4" class="empty">无隔离记录。</td></tr>';
  document.querySelector('#ledgers').innerHTML = d.ledgers.map(l => {
    const m = l.candidate;
    return `<tr><td>${l.card}</td><td>${day}</td><td>${m.events_total}</td><td>${m.transfer_hits}</td>` +
      `<td>${m.expected_total}</td><td>${m.reported_total}</td><td class="${m.difference_total === '0.00' ? '' : 'bad'}">${m.difference_total}</td>` +
      `<td>${m.balanced ? '<span class="pill ok">对平</span>' : '<span class="pill bad">未对平</span>'}</td>` +
      `<td>${l.published ? 'V' + l.published.version + ' ' + l.published.status : '未发布'}</td>` +
      `<td>${l.published ? (l.drifted ? '<span class="pill warn">已漂移，需发新版</span>' : '一致') : '—'}</td>` +
      `<td><button class="primary publish" data-card="${l.card}">发布</button> ` +
      `<a class="btn" href="/ledgers">查账册</a></td></tr>`;
  }).join('');
  document.querySelector('#ledgers').querySelectorAll('.publish').forEach(btn => btn.addEventListener('click', async () => {
    const r = await call(`/api/ledgers/${device}/${day}/${btn.dataset.card}/publish`, { method: 'POST' });
    feedback.textContent = `发布结果：${r.action === 'created' ? '创建 V1' : r.action === 'new_version' ? '追加 V' + r.ledger.version : '内容未变，沿用 V' + r.ledger.version}`;
    openStream(device, day);
  }));
}

function renderSnapTable(d) {
  const seqs = d.snapshots.map(s => s.snapshot_seq);
  const eventIds = d.events.map(e => e.event_id);
  let html = '<thead><tr><th>事件</th>' + d.snapshots.map(s =>
    `<th>#${s.snapshot_seq}${s.is_complete ? ' 完整' : ' 部分'}</th>`).join('') + '</tr></thead><tbody>';
  for (const eid of eventIds) {
    const ev = d.events.find(e => e.event_id === eid);
    html += `<tr><td>${eid}</td>` + d.snapshots.map(s => {
      const rec = s.rows.find(r => r.event_id === eid);
      if (!rec) return '<td class="cell-absent">缺席</td>';
      const prev = d.snapshots.filter(x => x.snapshot_seq < s.snapshot_seq).flatMap(x => x.rows).find(r => r.event_id === eid);
      const changed = prev && JSON.stringify(prev) !== JSON.stringify(rec);
      return `<td class="${changed ? 'cell-update' : 'cell-retain'}" title="申报 ${rec.reported}">${changed ? '更新' : '保留'} ${rec.reported}</td>`;
    }).join('') + '</tr>';
  }
  document.querySelector('#snapTable').innerHTML = html + '</tbody>';
}

document.querySelector('#eventFilter')?.addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#events tr').forEach(tr => tr.hidden = !tr.textContent.toLowerCase().includes(q));
});
function fmt(iso) { return iso ? iso.slice(0, 19).replace('T', ' ') : '—'; }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
loadStreams();
