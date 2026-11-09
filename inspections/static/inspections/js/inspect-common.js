/* Shared helpers for the visual-inspection pages. Depends on assistant/js/common.js (apiFetch, getCookie). */
const Inspect = (() => {
  const STATUS_LABEL = {queued: 'Queued', processing: 'Processing', completed: 'Completed', failed: 'Failed'};

  // Build DOM nodes with textContent only, so file names and reasons can never inject markup.
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else node.setAttribute(key, value === true ? '' : value);
    });
    children.flat().forEach(child => {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  const statusChip = status => el('span', {class: `chip chip-${status}`, text: STATUS_LABEL[status] || status});
  const classChip = cls => cls ? el('span', {class: `chip chip-${cls.toLowerCase()}`, text: cls}) : el('span', {class: 'text-secondary', text: '—'});
  const pct = (value, digits = 2) => (value === null || value === undefined) ? '—' : `${(value * 100).toFixed(digits)}%`;
  const fmtDate = iso => iso ? new Date(iso).toLocaleString() : '—';
  const isFinished = status => status === 'completed' || status === 'failed';

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    return `${(n / 1024 / 1024).toFixed(2)} MiB`;
  }

  function kv(rows) {
    const dl = el('dl', {class: 'kv'});
    rows.forEach(([label, value]) => {
      dl.appendChild(el('dt', {text: label}));
      dl.appendChild(el('dd', {}, value === null || value === undefined ? '—' : value));
    });
    return dl;
  }

  /** Result card for one inspection (used after upload and on polling updates). */
  function resultCard(item) {
    const done = item.status === 'completed';
    const cls = (item.predicted_class || '').toLowerCase();
    const header = el('div', {class: 'd-flex justify-content-between align-items-start gap-3 mb-3'},
      el('div', {},
        el('div', {class: 'page-eyebrow', text: `Inspection #${item.id}`}),
        done
          ? el('div', {class: `result-class is-${cls}`, text: item.predicted_class})
          : el('div', {class: 'result-class', text: item.status === 'failed' ? 'Not inspected' : 'Inspecting…'})),
      statusChip(item.status));
    const body = [];
    if (item.image_url) body.push(el('img', {class: 'preview-img mb-3', src: item.image_url, alt: 'Inspected image'}));
    if (done) {
      body.push(el('div', {class: 'meter mb-1', 'aria-hidden': 'true'}, el('span', {style: `width:${(item.confidence * 100).toFixed(1)}%`})));
      body.push(kv([
        ['Predicted class', item.predicted_class],
        ['Confidence', `${pct(item.confidence)} (probability of the predicted class)`],
        ['Defect probability', `${pct(item.defect_probability)}  ·  raw ${item.defect_probability.toFixed(6)}`],
        ['Model version', item.model_version],
        ['Product', `${item.product} – ${item.product_name}`],
        ['Inspected at', fmtDate(item.completed_at)],
      ]));
      body.push(el('p', {class: 'disclaimer mt-3 mb-0', text: 'Uncalibrated score from a model trained on synthetic images; not a verified probability of product safety.'}));
    } else if (item.status === 'failed') {
      body.push(el('div', {class: 'alert alert-danger mb-0', role: 'alert'},
        el('strong', {text: 'This image was not classified. '}), item.failure_reason || 'Unknown reason.'));
    } else {
      body.push(el('p', {class: 'text-secondary mb-0', text: item.status === 'queued'
        ? 'Waiting for a background worker to pick this image up…'
        : 'A background worker is running the classifier…'}));
    }
    body.push(el('div', {class: 'mt-3'}, el('a', {class: 'btn btn-outline-primary btn-sm', href: `/inspections/${item.id}/`, text: 'Open full record'})));
    return el('div', {class: `card result-card ${done ? `is-${cls}` : ''}`}, el('div', {class: 'card-body'}, header, body));
  }

  /** Batch status panel: progress bar, counts and one row per item. */
  function batchPanel(batch) {
    const done = batch.completed_count + batch.failed_count;
    const counts = el('div', {class: 'd-flex flex-wrap gap-2 mb-3'},
      el('span', {class: 'chip chip-queued', text: `Queued ${batch.queued_count}`}),
      el('span', {class: 'chip chip-processing', text: `Processing ${batch.processing_count}`}),
      el('span', {class: 'chip chip-completed', text: `Completed ${batch.completed_count}`}),
      el('span', {class: 'chip chip-failed', text: `Failed ${batch.failed_count}`}));
    const rows = batch.inspections.map(item => el('tr', {},
      el('td', {class: 'text-nowrap', text: `#${item.id}`}),
      el('td', {}, el('div', {class: 'fw-semibold text-break', text: item.original_filename || '—'}),
        item.status === 'failed' ? el('div', {class: 'small text-danger', text: item.failure_reason}) : null),
      el('td', {}, statusChip(item.status)),
      el('td', {}, classChip(item.predicted_class)),
      el('td', {text: pct(item.confidence)}),
      el('td', {class: 'text-end'}, el('a', {class: 'btn btn-sm btn-outline-secondary', href: `/inspections/${item.id}/`, text: 'Open'}))));
    return el('div', {class: 'card'},
      el('div', {class: 'card-header d-flex justify-content-between align-items-center flex-wrap gap-2'},
        el('div', {}, el('div', {class: 'section-title', text: `Batch #${batch.id}`}),
          el('div', {class: 'section-kicker', text: `${batch.total_files} file(s) · ${batch.product}`})),
        el('span', {class: batch.is_finished ? 'chip chip-completed' : 'chip chip-processing', text: batch.is_finished ? 'Finished' : 'In progress'})),
      el('div', {class: 'card-body'},
        el('div', {class: 'd-flex justify-content-between small text-secondary mb-1'},
          el('span', {text: `${done} of ${batch.total_files} finished`}), el('span', {text: `${batch.progress_percent}%`})),
        el('div', {class: 'meter mb-3', role: 'progressbar', 'aria-valuenow': batch.progress_percent, 'aria-valuemin': 0, 'aria-valuemax': 100},
          el('span', {style: `width:${batch.progress_percent}%`})),
        counts),
      el('div', {class: 'table-responsive'},
        el('table', {class: 'table align-middle mb-0'},
          el('thead', {}, el('tr', {}, ['ID', 'File', 'Status', 'Prediction', 'Confidence', ''].map(h => el('th', {text: h})))),
          el('tbody', {}, rows))));
  }

  /** Poll `url` until `isDone(data)`; slows down in a hidden tab and gives up after `maxMs`. */
  function poll(url, isDone, onUpdate, {maxMs = 180000} = {}) {
    const started = Date.now();
    let stopped = false;
    let timer = null;
    const tick = async () => {
      if (stopped) return;
      try {
        const res = await apiFetch(url, {headers: {Accept: 'application/json'}});
        if (res.ok) {
          const data = await res.json();
          onUpdate(data);
          if (isDone(data)) return;
        } else if (res.status === 403 || res.status === 404) {
          return;
        }
      } catch (err) { /* transient network error: keep polling */ }
      if (Date.now() - started > maxMs) { onUpdate(null, true); return; }
      timer = setTimeout(tick, document.hidden ? 5000 : 1500);
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); };
  }

  return {el, statusChip, classChip, pct, fmtDate, isFinished, uuid, formatBytes, kv, resultCard, batchPanel, poll};
})();
