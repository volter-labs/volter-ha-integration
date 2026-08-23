(async () => {
  await import('/volter_static/volter-plan-card.js?v=2.7.1&t=' + Date.now());
  const dane = window.__dane;
  document.body.style.cssText = 'margin:0;padding:16px;background:#0B1120;'
    + 'font-family:Outfit,sans-serif';
  document.body.innerHTML = '';

  const zbuduj = (wartosci, podpis, szer) => {
    const stany = { 'sensor.volter_energy_plan': {
      state: dane.atrybuty.tryb_biezacy || 'charge', attributes: dane.atrybuty } };
    for (const [id, v] of Object.entries(wartosci)) stany[id] = { state: String(v) };
    const el = document.createElement('volter-plan-card');
    el.setConfig({ entity: 'sensor.volter_energy_plan' });
    el.hass = { states: stany };
    const opak = document.createElement('div');
    opak.style.cssText = 'width:' + (szer || 440) + 'px;margin-bottom:18px';
    const tyt = document.createElement('div');
    tyt.textContent = podpis;
    tyt.style.cssText = 'color:#6B7280;font-size:11px;margin-bottom:6px';
    opak.appendChild(tyt); opak.appendChild(el);
    document.body.appendChild(opak);
  };

  document.body.style.display = 'flex';
  document.body.style.gap = '18px';
  document.body.style.alignItems = 'flex-start';
  zbuduj(dane.wartosci, 'karta 440 px', 440);
  // Wartosci ze zrzutu, na ktorym liczby wychodzily poza kafelek.
  const e = dane.atrybuty.encje;
  zbuduj({ [e.soc]: 84, [e.pv]: 1850, [e.dom]: 2400,
           [e.bateria]: -2730, [e.siec]: -3240 }, 'karta 320 px, wartosci ze zrzutu', 320);
  zbuduj({ [e.soc]: 84, [e.pv]: 12850, [e.dom]: 2400,
           [e.bateria]: -10730, [e.siec]: -3240 }, 'karta 260 px, moce dwucyfrowe kW', 260);
  window.__gotowe = true;
  return 'ok';
})()
