(() => {
  const toggle = document.querySelector('#theme-toggle');
  function themeLabel() {
    const dark = document.documentElement.dataset.theme === 'dark';
    document.querySelector('#theme-label').textContent = dark ? 'Tema claro' : 'Tema oscuro';
    toggle.setAttribute('aria-pressed', String(dark));
    toggle.setAttribute('aria-label', dark ? 'Activar tema claro' : 'Activar tema oscuro');
  }
  toggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('secuoya-dashboard-theme', next); } catch {}
    themeLabel();
  });
  themeLabel();
  const live = document.querySelector('#live-data');
  const form = document.querySelector('#message-form');
  const reply = document.querySelector('#reply');
  let refreshing = false;
  async function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch(live.dataset.url + location.search, {cache: 'no-store'});
      if (response.redirected) { location.href = response.url; return; }
      if (!response.ok) throw new Error();
      // Fragmento renderizado y escapado por Django; no contenido HTML de Gemini.
      const openDetails = [...live.querySelectorAll('details[open][id]')].map(el => el.id);
      live.innerHTML = await response.text();
      for (const id of openDetails) { const el = document.getElementById(id); if (el) el.open = true; }
    } catch { live.querySelector('.refresh').textContent = 'No se pudo actualizar. Reintentaremos la consulta.'; }
    finally { refreshing = false; }
  }
  setInterval(refresh, 10000);
  window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-message]');
    if (!button) return;
    form.elements.text.value = button.dataset.message;
    form.elements.text.focus();
    form.scrollIntoView({block: 'center'});
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    reply.textContent = 'Procesando tu mensaje…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin'});
      if (response.redirected) { location.href = response.url; return; }
      if (!response.ok) throw new Error();
      const result = await response.json();
      reply.textContent = result.reply || 'Mensaje pendiente; revisa la conversación antes de repetirlo.';
      form.elements.nonce.value = result.nonce;
      form.elements.text.value = '';
      await refresh();
    } catch { reply.textContent = 'No se pudo confirmar la respuesta. Puedes reenviar este mismo mensaje sin duplicarlo; conserva el texto.'; }
    finally { button.disabled = false; }
  });
})();
