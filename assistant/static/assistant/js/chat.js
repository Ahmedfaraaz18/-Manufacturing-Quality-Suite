const form = document.getElementById('ask-form');
const log = document.getElementById('chat-log');
const input = document.getElementById('question');
const button = document.getElementById('ask-btn');

const STATUS = {
  answered: {cls: 'status-answered', badge: 'text-bg-success', title: 'Answered from the manuals'},
  insufficient_evidence: {cls: 'status-insufficient', badge: 'text-bg-warning', title: 'Insufficient evidence'},
  error: {cls: 'status-error', badge: 'text-bg-danger', title: 'Service error'},
};

function userBubble(text) {
  const div = document.createElement('div');
  div.className = 'd-flex justify-content-end mb-3';
  div.innerHTML = `<div class="msg-user px-3 py-2" style="max-width:80%">${escapeHtml(text)}</div>`;
  return div;
}

function sourceCard(s, index) {
  const link = s.source_url
    ? `<a class="btn btn-sm btn-outline-primary" href="${s.source_url}" target="_blank" rel="noopener">Open source page ${s.page_number}</a>`
    : '<span class="text-secondary small">Source file no longer available</span>';
  return `<div class="card source-card mb-2"><div class="card-body">
    <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-2">
      <div><strong>[${index}] ${escapeHtml(s.document_id)}</strong> — ${escapeHtml(s.document_name)}<br>
        <span class="text-secondary small">Revision ${escapeHtml(s.revision)} · PDF page ${s.page_number}</span></div>${link}
    </div>
    <div class="passage">${escapeHtml(s.passage)}</div></div></div>`;
}

function answerCard(data) {
  const st = STATUS[data.status] || STATUS.error;
  const sources = (data.sources || []).map((s, i) => sourceCard(s, i + 1)).join('');
  const div = document.createElement('div');
  div.className = 'mb-4 chat-answer';
  div.innerHTML = `<div class="card ${st.cls}"><div class="card-body">
    <div class="d-flex justify-content-between align-items-center mb-2">
      <span class="badge ${st.badge}">${st.title}</span>
      <span class="text-secondary small">${escapeHtml(data.machine)} · ${new Date(data.created_at).toLocaleString()}</span></div>
    <div class="answer-text">${escapeHtml(data.answer)}</div>
    ${sources ? `<hr><div class="fw-semibold mb-2">Supporting evidence</div>${sources}` : ''}
  </div></div>`;
  return div;
}

form.addEventListener('submit', async e => {
  e.preventDefault();
  if (button.disabled) return;
  const question = input.value.trim();
  if (!question) return;
  log.appendChild(userBubble(question));
  input.value = ''; button.disabled = true; input.disabled = true; button.textContent = 'Searching…';
  const pending = document.createElement('div');
  pending.className = 'text-secondary mb-3'; pending.textContent = 'Searching the manuals…';
  log.appendChild(pending);
  try {
    const res = await apiFetch(form.dataset.url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({machine: form.dataset.machine, question}),
    });
    const data = await res.json().catch(() => null);
    pending.remove();
    if (res.ok) {
      log.appendChild(answerCard(data));
    } else {
      const div = document.createElement('div');
      div.className = 'alert alert-danger'; div.style.whiteSpace = 'pre-line'; div.textContent = flattenErrors(data);
      log.appendChild(div);
    }
  } catch (err) {
    pending.remove();
    const div = document.createElement('div');
    div.className = 'alert alert-danger'; div.textContent = 'Network error. Please try again.';
    log.appendChild(div);
  } finally {
    button.disabled = false; input.disabled = false; button.textContent = 'Ask'; input.focus();
    log.lastElementChild && log.lastElementChild.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
});
