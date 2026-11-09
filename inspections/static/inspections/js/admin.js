/* Administrator product registration and editing (CSRF-protected API calls; roles are enforced server-side). */
(function () {
  const app = document.getElementById('admin-app');
  if (!app) return;
  const form = document.getElementById('product-form');
  const title = document.getElementById('product-modal-title');
  const errorBox = document.getElementById('product-error');
  const saveBtn = document.getElementById('product-save');
  const idField = document.getElementById('pf-id');
  let editing = null;   // product_id being edited, or null when registering

  function showError(message) {
    errorBox.textContent = message || '';
    errorBox.classList.toggle('d-none', !message);
  }

  document.getElementById('new-product-btn').addEventListener('click', () => {
    editing = null; title.textContent = 'Register product'; idField.readOnly = false; showError('');
  });
  document.querySelectorAll('.edit-product').forEach(btn => btn.addEventListener('click', () => {
    editing = btn.dataset.productId; title.textContent = `Edit ${editing}`; showError('');
    idField.value = editing; idField.readOnly = true;
    form.elements.name.value = btn.dataset.name;
    form.elements.description.value = btn.dataset.description;
    form.elements.is_active.checked = btn.dataset.active === '1';
  }));

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saveBtn.disabled) return;
    showError('');
    saveBtn.disabled = true;
    const body = {
      product_id: form.elements.product_id.value.trim(), name: form.elements.name.value.trim(),
      description: form.elements.description.value.trim(), is_active: form.elements.is_active.checked,
    };
    const url = editing ? `${app.dataset.productsApi}${encodeURIComponent(editing)}/` : app.dataset.productsApi;
    try {
      const res = await apiFetch(url, {method: editing ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      if (res.ok) { location.reload(); return; }
      showError(flattenErrors(await res.json().catch(() => null)));
    } catch (err) {
      showError('Network error. Please try again.');
    }
    saveBtn.disabled = false;
  });
})();
