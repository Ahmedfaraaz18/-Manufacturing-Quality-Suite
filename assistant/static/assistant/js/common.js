function getCookie(name) {
  const m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[1]) : '';
}

function apiFetch(url, options = {}) {
  const headers = Object.assign({'X-CSRFToken': getCookie('csrftoken')}, options.headers || {});
  return fetch(url, Object.assign({credentials: 'same-origin'}, options, {headers}));
}

function flattenErrors(data) {
  if (!data || typeof data !== 'object') return 'Request failed.';
  if (data.detail) return data.detail;
  return Object.entries(data).map(([k, v]) => `${k}: ${[].concat(v).join(' ')}`).join('\n');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
