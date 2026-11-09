(() => {
  const trends = JSON.parse(document.getElementById('intelligence-trends').textContent);
  const batches = JSON.parse(document.getElementById('intelligence-batches').textContent);
  const dataFor = key => ({
    volume: trends.map(r => ({label: r.date, value: r.volume, color: '#1769aa'})),
    predictions: trends.map(r => ({label: r.date, value: r.defective, secondary: r.normal, color: '#b42318'})),
    failed: trends.map(r => ({label: r.date, value: r.failed, color: '#b42318'})),
    backlog: trends.map(r => ({label: r.date, value: r.review_backlog, color: '#9a6517'})),
    correction: trends.filter(r => r.correction_rate !== null).map(r => ({label: r.date, value: r.correction_rate, color: '#7b4bb7'})),
    'batch-rate': batches.map(r => ({label: r.label, value: r.value, color: '#b42318'})),
  })[key] || [];
  document.querySelectorAll('.intelligence-chart').forEach(canvas => {
    const data = dataFor(canvas.dataset.series); const ctx = canvas.getContext('2d');
    const render = () => { const width = canvas.clientWidth || 320, height = 150; canvas.width = width * devicePixelRatio; canvas.height = height * devicePixelRatio; ctx.scale(devicePixelRatio, devicePixelRatio); ctx.clearRect(0, 0, width, height); if (!data.length) { ctx.fillStyle = '#52616b'; ctx.font = '13px sans-serif'; ctx.fillText('No stored data for this range', 12, 74); return; } const max = Math.max(...data.map(d => d.value + (d.secondary || 0)), 1); const gap = 6, bar = Math.max(8, (width - 28 - gap * (data.length - 1)) / data.length); data.forEach((d, i) => { const x = 14 + i * (bar + gap), h = (height - 30) * d.value / max; ctx.fillStyle = d.color; ctx.fillRect(x, height - 18 - h, bar, h); if (d.secondary) { const second = (height - 30) * d.secondary / max; ctx.fillStyle = '#177245'; ctx.fillRect(x, height - 18 - h - second, bar, second); } }); ctx.fillStyle = '#52616b'; ctx.font = '10px sans-serif'; ctx.fillText(data[0].label, 12, height - 3); ctx.fillText(data[data.length - 1].label, Math.max(12, width - 80), height - 3); };
    canvas.title = data.map(d => `${d.label}: ${d.value}${d.secondary !== undefined ? ` Defective, ${d.secondary} Normal` : ''}`).join('\n'); render(); addEventListener('resize', render);
  });
})();
