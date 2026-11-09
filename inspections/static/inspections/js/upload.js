/* Engineer upload page: single + batch upload with progress, duplicate-submit protection and live status. */
(function () {
  const root = document.getElementById('upload-app');
  if (!root) return;
  const {el, uuid, formatBytes, resultCard, batchPanel, poll} = Inspect;

  const MAX_BYTES = 5 * 1024 * 1024, MAX_FILES = 10, MIN_DIM = 64, MAX_DIM = 4096;
  const form = document.getElementById('upload-form');
  const input = document.getElementById('file-input');
  const dropzone = document.getElementById('dropzone');
  const fileList = document.getElementById('file-list');
  const previewWrap = document.getElementById('preview-wrap');
  const preview = document.getElementById('preview');
  const submit = document.getElementById('submit-btn');
  const errorBox = document.getElementById('form-error');
  const resultArea = document.getElementById('result-area');
  const progressWrap = document.getElementById('progress-wrap');
  const progressBar = document.getElementById('progress-bar');
  const progressPct = document.getElementById('progress-pct');
  const progressLabel = document.getElementById('progress-label');
  const title = document.getElementById('dz-title');

  let mode = 'single';
  let files = [];
  let token = uuid();   // identifies this submission; a repeated send can never create a second batch
  let busy = false;
  let stopPolling = null;
  let previewUrl = null;

  function setError(message) {
    errorBox.textContent = message || '';
    errorBox.classList.toggle('d-none', !message);
  }

  function setMode(next) {
    mode = next;
    document.querySelectorAll('#mode-tabs button').forEach(btn => {
      const active = btn.dataset.mode === mode;
      btn.className = `btn btn-sm ${active ? 'btn-primary' : 'btn-outline-primary'}`;
    });
    input.multiple = mode === 'batch';
    title.textContent = mode === 'batch' ? 'Choose up to 10 images or drop them here' : 'Choose an image or drop it here';
    reset();
  }

  function reset() {
    files = [];
    input.value = '';
    fileList.replaceChildren();
    previewWrap.classList.add('d-none');
    if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
    setError('');
    refresh();
  }

  // Advisory checks only: the server is authoritative, and a mixed batch with bad files must still be submittable.
  function advisory(file) {
    return new Promise(resolve => {
      const notes = [];
      if (file.size === 0) notes.push('empty file');
      if (file.size > MAX_BYTES) notes.push(`larger than 5 MiB (${formatBytes(file.size)})`);
      if (!/\.(jpe?g|png)$/i.test(file.name) && !/^image\/(jpeg|png)$/.test(file.type)) notes.push('not a JPG or PNG');
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        if (img.naturalWidth < MIN_DIM || img.naturalHeight < MIN_DIM) notes.push(`${img.naturalWidth}×${img.naturalHeight}px is below ${MIN_DIM}px`);
        if (img.naturalWidth > MAX_DIM || img.naturalHeight > MAX_DIM) notes.push(`${img.naturalWidth}×${img.naturalHeight}px is above ${MAX_DIM}px`);
        URL.revokeObjectURL(url);
        resolve({notes, url: null, dims: `${img.naturalWidth}×${img.naturalHeight}`});
      };
      img.onerror = () => { URL.revokeObjectURL(url); notes.push('cannot be decoded as an image'); resolve({notes, url: null, dims: null}); };
      img.src = url;
    });
  }

  async function addFiles(list) {
    if (busy) return;
    setError('');
    files = Array.from(list);
    if (mode === 'single' && files.length > 1) { files = files.slice(0, 1); }
    fileList.replaceChildren();
    if (mode === 'batch' && files.length > MAX_FILES) {
      setError(`A batch can contain at most ${MAX_FILES} images; you selected ${files.length}. Remove some and try again.`);
    }
    const rows = await Promise.all(files.map(async file => ({file, ...(await advisory(file))})));
    rows.forEach(({file, notes, dims}) => {
      const previewable = /\.(jpe?g|png)$/i.test(file.name) || /^image\/(jpeg|png)$/.test(file.type);
      let thumbnail = null;
      if (mode === 'batch' && previewable) {
        const url = URL.createObjectURL(file);
        thumbnail = el('img', {class: 'thumb', src: url, alt: `Preview of ${file.name}`});
        thumbnail.addEventListener('load', () => URL.revokeObjectURL(url), {once: true});
        thumbnail.addEventListener('error', () => { URL.revokeObjectURL(url); thumbnail.remove(); }, {once: true});
      }
      const row = el('div', {class: `file-row ${notes.length ? 'is-invalid' : ''}`},
        thumbnail,
        el('div', {class: 'file-meta'},
          el('div', {class: 'file-name', text: file.name}),
          el('div', {class: 'file-note', text: notes.length
            ? `Likely to be rejected: ${notes.join(', ')}`
            : `${formatBytes(file.size)}${dims ? ' · ' + dims + ' px' : ''}`})));
      fileList.appendChild(row);
    });
    if (mode === 'single' && files[0] && /^image\//.test(files[0].type)) {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(files[0]);
      preview.src = previewUrl;
      previewWrap.classList.remove('d-none');
    } else {
      previewWrap.classList.add('d-none');
    }
    refresh();
  }

  function refresh() {
    const tooMany = mode === 'batch' && files.length > MAX_FILES;
    submit.disabled = busy || files.length === 0 || tooMany;
  }

  function setBusy(state) {
    busy = state;
    submit.replaceChildren();
    if (state) {
      submit.appendChild(el('span', {class: 'spinner-inline', 'aria-hidden': 'true'}));
      submit.appendChild(document.createTextNode('Uploading…'));
    } else {
      submit.textContent = mode === 'batch' ? 'Run batch inspection' : 'Run inspection';
    }
    input.disabled = state;
    form.querySelectorAll('select, button[data-mode]').forEach(node => { node.disabled = state; });
    document.querySelectorAll('#mode-tabs button').forEach(btn => { btn.disabled = state; });
    progressWrap.classList.toggle('d-none', !state);
    refresh();
  }

  function send(url, body) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.setRequestHeader('X-CSRFToken', getCookie('csrftoken'));
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.responseType = 'json';
      xhr.upload.onprogress = event => {
        if (!event.lengthComputable) return;
        const percent = Math.round(100 * event.loaded / event.total);
        progressBar.style.width = `${percent}%`;
        progressPct.textContent = `${percent}%`;
        if (percent >= 100) progressLabel.textContent = 'Processing upload…';
      };
      xhr.onload = () => resolve({status: xhr.status, data: xhr.response});
      xhr.onerror = () => reject(new Error('network'));
      xhr.ontimeout = () => reject(new Error('timeout'));
      xhr.send(body);
    });
  }

  function describeError(status, data) {
    if (status === 403) return 'Your account is not permitted to submit inspections.';
    if (status === 413) return 'The upload is too large. Each file may be at most 5 MiB.';
    if (data && typeof data === 'object') return flattenErrors(data);
    return `The upload failed (HTTP ${status}). Please try again.`;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || files.length === 0) return;      // duplicate-submit guard
    setError('');
    const body = new FormData();
    body.append('product', document.getElementById('product').value);
    body.append('client_token', token);
    if (mode === 'single') body.append('image', files[0]);
    else files.forEach(file => body.append('files', file));
    progressBar.style.width = '0%'; progressPct.textContent = '0%'; progressLabel.textContent = 'Uploading…';
    setBusy(true);
    let outcome;
    try {
      outcome = await send(mode === 'single' ? root.dataset.singleUrl : root.dataset.batchUrl, body);
    } catch (err) {
      setBusy(false);
      setError('Network error. The upload may not have been received; submit again to retry safely.');
      return;
    }
    setBusy(false);
    const {status, data} = outcome;
    const accepted = status === 200 || status === 202 || status === 422;
    if (!accepted || !data) {
      setError(describeError(status, data));
      return;
    }
    token = uuid();                  // a new submission gets a new identity
    const finishedMode = mode;
    reset();
    if (finishedMode === 'single') showSingle(data.inspection);
    else showBatch(data.batch);
  });

  function showSingle(inspection) {
    if (stopPolling) stopPolling();
    resultArea.replaceChildren(resultCard(inspection));
    if (Inspect.isFinished(inspection.status)) return;
    stopPolling = poll(`${root.dataset.inspectionApi}${inspection.id}/`, d => Inspect.isFinished(d.status), (data, timedOut) => {
      if (timedOut) { resultArea.appendChild(el('p', {class: 'text-secondary mt-2', text: 'Still processing. You can leave this page; the result is saved in your history.'})); return; }
      resultArea.replaceChildren(resultCard(data));
    });
  }

  function showBatch(batch) {
    if (stopPolling) stopPolling();
    resultArea.replaceChildren(batchPanel(batch));
    if (batch.is_finished) return;
    stopPolling = poll(`${root.dataset.batchApi}${batch.id}/`, d => d.is_finished, (data, timedOut) => {
      if (timedOut) { resultArea.appendChild(el('p', {class: 'text-secondary mt-2', text: 'Still processing. You can leave this page; progress is saved.'})); return; }
      resultArea.replaceChildren(batchPanel(data));
    });
  }

  document.querySelectorAll('#mode-tabs button').forEach(btn => btn.addEventListener('click', () => { if (!busy) setMode(btn.dataset.mode); }));
  input.addEventListener('change', () => addFiles(input.files));
  ['dragenter', 'dragover'].forEach(name => dropzone.addEventListener(name, e => { e.preventDefault(); dropzone.classList.add('is-dragover'); }));
  ['dragleave', 'drop'].forEach(name => dropzone.addEventListener(name, e => { e.preventDefault(); dropzone.classList.remove('is-dragover'); }));
  dropzone.addEventListener('drop', e => { if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });
  setMode('single');
})();
