(async () => {
  const el = document.querySelector('volter-plan-card');
  if (!el) return 'brak karty';
  const tytuly = Array.from(el.shadowRoot.querySelectorAll('g.hit title'))
    .map((t) => t.textContent);
  const os = Array.from(el.shadowRoot.querySelectorAll('.os-soc span'))
    .map((s) => s.textContent + ' @ ' + s.style.top);
  return JSON.stringify({
    os,
    pierwsze: tytuly.slice(0, 2),
    biezaca: tytuly.filter((t) => t.includes('od pomiaru')),
    przyklad_prognozy: tytuly.filter((t) => t.includes('→')).slice(1, 4),
    minione: tytuly.filter((t) => t.includes('miniona')).slice(0, 2),
  }, null, 1);
})()
