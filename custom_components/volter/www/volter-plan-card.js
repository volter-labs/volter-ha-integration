/**
 * Volter Plan Card — plan pracy magazynu energii na dashboardzie HA.
 *
 * Karta jest dostarczana RAZEM z integracją i rejestrowana automatycznie, więc
 * użytkownik nie wgrywa niczego ręcznie ani nie dodaje zasobu w Lovelace.
 *
 * Styl: Volter Aura (styles/theme.ts w aplikacji) — ten sam, co w Volterze
 * i Volcaście, żeby wszystko, co widzi klient, wyglądało jak jedna rzecz.
 *
 * Zero zależności i zero build stepu: to zwykły web component, więc aktualizacja
 * karty to podmiana jednego pliku razem z integracją.
 */

const AURA = {
  canvas: '#0B1120',
  surface: '#111827',
  border: 'rgba(255,255,255,0.06)',
  borderMid: 'rgba(255,255,255,0.10)',
  primary: '#34D399',
  primaryBright: '#6EE7B7',
  sky: '#60A5FA',
  orange: '#FB923C',
  textPrimary: '#F9FAFB',
  textSecondary: '#9CA3AF',
  textMuted: '#6B7280',
};

/** Kolor i etykieta per kierunek — zgodne z semantyką kontraktu urządzenia. */
const AKCJE = {
  charge: { kolor: AURA.primary, etykieta: 'Ładowanie' },
  discharge: { kolor: AURA.orange, etykieta: 'Rozładowanie' },
  self_consume: { kolor: AURA.sky, etykieta: 'Autokonsumpcja' },
  idle: { kolor: AURA.textMuted, etykieta: 'Postój' },
};

const czasHM = (iso) =>
  new Date(iso).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });

const esc = (s) => String(s).replace(/[&<>"]/g, (z) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[z]));

class VolterPlanCard extends HTMLElement {
  static getStubConfig() {
    return { entity: 'sensor.volter_planned_mode' };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error('Podaj pole entity — sensor planu Voltera.');
    }
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
  }

  getCardSize() {
    return 5;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    const st = this._hass && this._hass.states
      ? this._hass.states[this._config.entity]
      : null;
    if (!st) {
      this.shadowRoot.innerHTML = this._szkielet(
        '<div class="pusto">Brak encji <code>' + esc(this._config.entity) + '</code></div>');
      return;
    }
    const a = st.attributes || {};
    const sloty = Array.isArray(a.sloty) ? a.sloty : [];
    const sterowanie = a.sterowanie_wlaczone === true;

    this.shadowRoot.innerHTML = this._szkielet(
      this._naglowek(st, a, sterowanie)
      + (sloty.length ? this._pasek(sloty) : '<div class="pusto">Brak planu z chmury</div>')
      + (sloty.length ? this._legenda(sloty) : '')
      + this._stopka(a));
  }

  _naglowek(st, a, sterowanie) {
    const akcja = String(st.state || '').replace(' (fallback)', '');
    const cfg = AKCJE[akcja] || AKCJE.idle;
    const moc = a.moc_w != null ? Math.round(a.moc_w) + ' W' : '—';
    const cena = a.cena != null ? Number(a.cena).toFixed(2) + ' zł/kWh' : '—';
    const okno = a.slot_od ? czasHM(a.slot_od) + '–' + czasHM(a.slot_do) : 'brak slotu';
    return ''
      + '<div class="head">'
      + '<div class="teraz">'
      + '<div class="kropka" style="--k:' + cfg.kolor + '"></div>'
      + '<div>'
      + '<div class="tytul">' + cfg.etykieta + (a.fallback ? ' · fallback' : '') + '</div>'
      + '<div class="podtytul">' + okno + '</div>'
      + '</div></div>'
      + '<div class="metryki">'
      + '<div class="metryka"><span>Moc</span><b>' + moc + '</b></div>'
      + '<div class="metryka"><span>Cena</span><b>' + cena + '</b></div>'
      + '</div></div>'
      + '<div class="pigulka ' + (sterowanie ? 'on' : 'off') + '">'
      + (sterowanie
        ? 'Sterowanie włączone'
        : 'Sterowanie wyłączone — Volter nie zapisuje do falownika')
      + '</div>';
  }

  _pasek(sloty) {
    const moce = sloty.map((s) => Math.abs(s.moc_w || 0)).concat([1]);
    const maks = Math.max.apply(null, moce);
    const kolumny = sloty.map((s) => {
      const cfg = AKCJE[s.akcja] || AKCJE.idle;
      const h = Math.max(6, Math.round((Math.abs(s.moc_w || 0) / maks) * 100));
      const tytul = [
        czasHM(s.od) + '–' + czasHM(s.do),
        cfg.etykieta,
        s.moc_w != null ? Math.round(s.moc_w) + ' W' : 'moc niezadana',
        s.cena != null ? Number(s.cena).toFixed(2) + ' zł/kWh' : null,
        s.zrodlo_ladowania ? 'źródło: ' + s.zrodlo_ladowania : null,
        s.cel_rozladowania ? 'cel: ' + s.cel_rozladowania : null,
      ].filter(Boolean).join(' · ');
      return '<div class="kol ' + (s.teraz ? 'teraz' : '') + '" title="' + esc(tytul) + '">'
        + '<div class="slup" style="--k:' + cfg.kolor + ';--h:' + h + '%"></div>'
        + '</div>';
    }).join('');
    return '<div class="pasek">' + kolumny + '</div>'
      + '<div class="os"><span>' + czasHM(sloty[0].od) + '</span>'
      + '<span>' + czasHM(sloty[sloty.length - 1].do) + '</span></div>';
  }

  _legenda(sloty) {
    const obecne = sloty.map((s) => s.akcja).filter((v, i, t) => t.indexOf(v) === i);
    return '<div class="legenda">' + obecne.map((k) => {
      const cfg = AKCJE[k] || AKCJE.idle;
      return '<span class="poz"><i style="--k:' + cfg.kolor + '"></i>' + cfg.etykieta + '</span>';
    }).join('') + '</div>';
  }

  _stopka(a) {
    const doKiedy = a.wazny_do
      ? new Date(a.wazny_do).toLocaleString('pl-PL',
        { weekday: 'short', hour: '2-digit', minute: '2-digit' })
      : '—';
    return '<div class="stopka"><span>Plan ważny do</span><b>' + doKiedy + '</b></div>';
  }

  _szkielet(tresc) {
    return ''
      + '<style>'
      + ':host{display:block}'
      + '.karta{background:linear-gradient(160deg,' + AURA.surface + ' 0%,' + AURA.canvas + ' 100%);'
      + 'border:1px solid ' + AURA.border + ';border-radius:24px;padding:20px;'
      + 'color:' + AURA.textPrimary + ';'
      + 'font-family:Outfit,"DM Sans",Roboto,system-ui,-apple-system,sans-serif;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35)}'
      + '.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}'
      + '.teraz{display:flex;align-items:center;gap:12px;min-width:0}'
      + '.kropka{width:12px;height:12px;border-radius:9999px;background:var(--k);flex:0 0 auto;'
      + 'box-shadow:0 0 0 4px color-mix(in srgb,var(--k) 18%,transparent)}'
      + '.tytul{font-size:20px;font-weight:700;letter-spacing:-.2px}'
      + '.podtytul{font-size:13px;color:' + AURA.textSecondary + ';margin-top:2px;'
      + 'font-variant-numeric:tabular-nums}'
      + '.metryki{display:flex;gap:20px;flex:0 0 auto}'
      + '.metryka{text-align:right}'
      + '.metryka span{display:block;font-size:11px;letter-spacing:.6px;text-transform:uppercase;'
      + 'color:' + AURA.textMuted + '}'
      + '.metryka b{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}'
      + '.pigulka{margin-top:14px;padding:7px 12px;border-radius:9999px;font-size:12px;'
      + 'border:1px solid ' + AURA.borderMid + ';display:inline-block}'
      + '.pigulka.on{color:' + AURA.primaryBright + ';background:rgba(52,211,153,.10);'
      + 'border-color:rgba(52,211,153,.35)}'
      + '.pigulka.off{color:' + AURA.textSecondary + ';background:rgba(255,255,255,.03)}'
      + '.pasek{display:flex;align-items:flex-end;gap:3px;height:104px;margin:18px 0 6px}'
      + '.kol{flex:1 1 0;height:100%;display:flex;align-items:flex-end;border-radius:6px}'
      + '.kol.teraz{background:rgba(255,255,255,.05);outline:1px solid rgba(255,255,255,.12)}'
      + '.slup{width:100%;height:var(--h);border-radius:5px;'
      + 'background:linear-gradient(180deg,var(--k) 0%,'
      + 'color-mix(in srgb,var(--k) 55%,transparent) 100%)}'
      + '.os{display:flex;justify-content:space-between;font-size:11px;'
      + 'color:' + AURA.textMuted + ';font-variant-numeric:tabular-nums}'
      + '.legenda{display:flex;flex-wrap:wrap;gap:14px;margin-top:14px}'
      + '.poz{display:flex;align-items:center;gap:6px;font-size:12px;'
      + 'color:' + AURA.textSecondary + '}'
      + '.poz i{width:9px;height:9px;border-radius:3px;background:var(--k)}'
      + '.stopka{margin-top:16px;padding-top:14px;border-top:1px solid ' + AURA.border + ';'
      + 'display:flex;justify-content:space-between;font-size:13px;'
      + 'color:' + AURA.textSecondary + '}'
      + '.stopka b{color:' + AURA.textPrimary + ';font-weight:600;'
      + 'font-variant-numeric:tabular-nums}'
      + '.pusto{padding:26px 0;text-align:center;color:' + AURA.textMuted + ';font-size:14px}'
      + '.karta code{font-family:ui-monospace,monospace;font-size:12px}'
      + '</style>'
      + '<div class="karta">' + tresc + '</div>';
  }
}

customElements.define('volter-plan-card', VolterPlanCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'volter-plan-card',
  name: 'Volter — plan pracy',
  description: 'Plan magazynu energii z chmury Volter: tryb, moc i ceny w horyzoncie planowania.',
  preview: true,
});
