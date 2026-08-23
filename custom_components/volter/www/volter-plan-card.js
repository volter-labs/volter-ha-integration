/**
 * Volter Plan Card — kokpit magazynu energii na dashboardzie HA.
 *
 * Karta jest dostarczana RAZEM z integracją i rejestrowana automatycznie, więc
 * użytkownik nie wgrywa niczego ręcznie ani nie dodaje zasobu w Lovelace.
 *
 * Styl: Volter Aura (styles/theme.ts w aplikacji) — te same tokeny, co w Volterze
 * i Volcaście, żeby wszystko, co widzi klient, wyglądało jak jedna rzecz.
 *
 * SKĄD DANE:
 *   - wartości BIEŻĄCE czytamy WPROST z encji falownika (`hass.states`) — lokalnie,
 *     bez chmury. Kokpit żyje nawet przy zerwanym łączu.
 *   - PLAN przychodzi z chmury przez `get-schedule` i siedzi w atrybutach sensora.
 *   - prognoza SoC liczona jest TUTAJ, z bieżącego odczytu i mocy per slot.
 *     Zakotwiczenie w realnym SoC (a nie w projekcji sprzed doby) sprawia, że
 *     krzywa sama się koryguje przy każdym odświeżeniu.
 *
 * Kolumny i krzywa dzielą jeden SVG — inaczej oś godzinowa rozjeżdżałaby się
 * między warstwami przy dowolnej szerokości karty.
 */

const AURA = {
  canvas: '#0B1120',
  surface: '#111827',
  surface2: '#1F2937',
  border: 'rgba(255,255,255,0.06)',
  borderMid: 'rgba(255,255,255,0.10)',
  primary: '#34D399',
  primaryBright: '#6EE7B7',
  violet: '#A78BFA',
  amber: '#FBBF24',
  sky: '#60A5FA',
  orange: '#FB923C',
  danger: '#F87171',
  textPrimary: '#F9FAFB',
  textSecondary: '#9CA3AF',
  textMuted: '#6B7280',
};

const AKCJE = {
  charge: { kolor: AURA.primary, etykieta: 'Ładowanie', znak: 1 },
  discharge: { kolor: AURA.orange, etykieta: 'Rozładowanie', znak: -1 },
  self_consume: { kolor: AURA.sky, etykieta: 'Autokonsumpcja', znak: 0 },
  idle: { kolor: AURA.textMuted, etykieta: 'Postój', znak: 0 },
};

const KOL_W = 22;   // szerokość kolumny godzinowej w jednostkach viewBox
const WYS_SLUP = 96;
const WYS_SOC = 58;
const MARGINES = 26;

const esc = (s) => String(s).replace(/[&<>"]/g, (z) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[z]));

const godzina = (iso) => new Date(iso).getHours();

const liczba = (v, jedn, dokl) => (v == null || Number.isNaN(v))
  ? '—'
  : (dokl ? Number(v).toFixed(dokl) : Math.round(v)) + (jedn ? ' ' + jedn : '');

class VolterPlanCard extends HTMLElement {
  static getStubConfig() {
    return { entity: 'sensor.volter_energy_plan' };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error('Podaj pole entity — sensor planu Voltera.');
    }
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
  }

  getCardSize() { return 8; }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  /** Wartość liczbowa encji falownika albo null. Zawsze lokalnie, nigdy z chmury. */
  _val(id) {
    if (!id || !this._hass || !this._hass.states) return null;
    const st = this._hass.states[id];
    if (!st || st.state === 'unknown' || st.state === 'unavailable') return null;
    const v = Number(st.state);
    return Number.isNaN(v) ? null : v;
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
    const enc = a.encje || {};
    const soc = this._val(enc.soc);

    this.shadowRoot.innerHTML = this._szkielet(
      this._naglowek(st, a)
      + this._teraz(enc, soc)
      + (sloty.length
        ? this._wykres(sloty, soc, Number(a.pojemnosc_kwh) || 10)
        : '<div class="pusto">Brak planu z chmury</div>')
      + (sloty.length ? this._legenda(sloty) : '')
      + this._stopka(a));
  }

  _naglowek(st, a) {
    const akcja = String(st.state || '').replace(' (fallback)', '');
    const cfg = AKCJE[akcja] || AKCJE.idle;
    const sterowanie = a.sterowanie_wlaczone === true;
    return ''
      + '<div class="head">'
      + '<div class="teraz-tryb">'
      + '<span class="kropka" style="--k:' + cfg.kolor + '"></span>'
      + '<span class="tytul">' + cfg.etykieta + (a.fallback ? ' · fallback' : '') + '</span>'
      + '</div>'
      + '<span class="pigulka ' + (sterowanie ? 'on' : 'off') + '">'
      + (sterowanie ? 'Sterowanie włączone' : 'Sterowanie wyłączone')
      + '</span>'
      + '</div>';
  }

  /** Pas wartości bieżących — wszystko z encji falownika, na żywo. */
  _teraz(enc, soc) {
    const pv = this._val(enc.pv);
    const dom = this._val(enc.dom);
    const bat = this._val(enc.bateria);
    const siec = this._val(enc.siec);
    const kafel = (etykieta, wartosc, kolor, dopisek) => ''
      + '<div class="kafel">'
      + '<span class="et">' + etykieta + '</span>'
      + '<b style="color:' + kolor + '">' + wartosc + '</b>'
      + (dopisek ? '<span class="dop">' + dopisek + '</span>' : '')
      + '</div>';
    return '<div class="teraz">'
      + kafel('SoC', liczba(soc, '%'), AURA.primaryBright,
        soc != null ? this._pasekSoc(soc) : '')
      + kafel('PV', liczba(pv, 'W'), AURA.amber)
      + kafel('Dom', liczba(dom, 'W'), AURA.violet)
      + kafel('Bateria', liczba(bat, 'W'),
        bat != null && bat < 0 ? AURA.primary : AURA.orange)
      + kafel('Sieć', liczba(siec, 'W'),
        siec != null && siec > 0 ? AURA.danger : AURA.sky,
        siec == null ? '' : (siec > 0 ? 'import' : 'eksport'))
      + '</div>';
  }

  _pasekSoc(soc) {
    return '<span class="soc-bar"><i style="width:'
      + Math.max(0, Math.min(100, soc)) + '%"></i></span>';
  }

  /**
   * Kolumny godzinowe + krzywa prognozy SoC w jednym SVG.
   *
   * Prognoza: start = REALNY SoC z falownika, potem per slot
   * delta = moc_bateryjna * 1 h / pojemność. `power_w` jest po stronie baterii,
   * więc to jest dokładnie ta wielkość, która zmienia SoC.
   */
  _wykres(sloty, socTeraz, pojemnosc) {
    const n = sloty.length;
    const W = n * KOL_W;
    const H = WYS_SLUP + WYS_SOC + MARGINES;
    const maks = Math.max.apply(null,
      sloty.map((s) => Math.abs(s.moc_w || 0)).concat([1]));

    // — kolumny godzinowe (moc) —
    let kolumny = '';
    let etykiety = '';
    let siatka = '';
    sloty.forEach((s, i) => {
      const cfg = AKCJE[s.akcja] || AKCJE.idle;
      const h = Math.max(3, Math.round((Math.abs(s.moc_w || 0) / maks) * (WYS_SLUP - 8)));
      const x = i * KOL_W;
      const y = WYS_SOC + (WYS_SLUP - h);
      const tytul = [
        String(godzina(s.od)).padStart(2, '0') + ':00',
        cfg.etykieta,
        s.moc_w != null ? Math.round(s.moc_w) + ' W' : 'moc niezadana',
        s.cena != null ? Number(s.cena).toFixed(2) + ' zł/kWh' : null,
        s.zrodlo_ladowania ? 'źródło: ' + s.zrodlo_ladowania : null,
        s.cel_rozladowania ? 'cel: ' + s.cel_rozladowania : null,
      ].filter(Boolean).join(' · ');

      if (s.teraz) {
        siatka += '<rect x="' + x + '" y="0" width="' + KOL_W + '" height="'
          + (WYS_SOC + WYS_SLUP) + '" fill="rgba(255,255,255,.06)" rx="3"/>';
      }
      kolumny += '<g><title>' + esc(tytul) + '</title>'
        + '<rect x="' + (x + 2) + '" y="' + y + '" width="' + (KOL_W - 4)
        + '" height="' + h + '" rx="3" fill="' + cfg.kolor + '"'
        + (s.moc_w == null ? ' opacity=".35"' : '') + '/></g>';

      const g = godzina(s.od);
      if (g % 3 === 0) {
        etykiety += '<text x="' + (x + KOL_W / 2) + '" y="' + (H - 8)
          + '" class="oc">' + String(g).padStart(2, '0') + '</text>';
        siatka += '<line x1="' + x + '" y1="0" x2="' + x + '" y2="'
          + (WYS_SOC + WYS_SLUP) + '" stroke="rgba(255,255,255,.07)" stroke-width="1"/>';
      }
    });

    // — krzywa prognozy SoC —
    let krzywa = '';
    let etykietaSoc = '';
    if (socTeraz != null && pojemnosc > 0) {
      let s = socTeraz;
      const punkty = [];
      sloty.forEach((slot, i) => {
        const cfg = AKCJE[slot.akcja] || AKCJE.idle;
        const kwh = ((slot.moc_w || 0) / 1000) * cfg.znak;
        s = Math.max(0, Math.min(100, s + (kwh / pojemnosc) * 100));
        const x = i * KOL_W + KOL_W / 2;
        const y = WYS_SOC - (s / 100) * (WYS_SOC - 6) - 3;
        punkty.push(x.toFixed(1) + ',' + y.toFixed(1));
      });
      krzywa = '<polyline points="' + punkty.join(' ') + '" fill="none" stroke="'
        + AURA.primaryBright + '" stroke-width="2" stroke-linejoin="round"'
        + ' stroke-linecap="round" opacity=".9"/>';
      etykietaSoc = '<text x="2" y="10" class="os-soc">SoC — prognoza</text>';
    }

    return '<div class="wykres">'
      + '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" '
      + 'style="width:100%;height:' + (H * 1.35) + 'px">'
      + siatka + krzywa + etykietaSoc + kolumny + etykiety
      + '</svg></div>';
  }

  _legenda(sloty) {
    const obecne = sloty.map((s) => s.akcja).filter((v, i, t) => t.indexOf(v) === i);
    return '<div class="legenda">' + obecne.map((k) => {
      const cfg = AKCJE[k] || AKCJE.idle;
      return '<span class="poz"><i style="--k:' + cfg.kolor + '"></i>' + cfg.etykieta + '</span>';
    }).join('')
      + '<span class="poz"><i class="linia"></i>Prognoza SoC</span></div>';
  }

  _stopka(a) {
    const doKiedy = a.wazny_do
      ? new Date(a.wazny_do).toLocaleString('pl-PL',
        { weekday: 'short', hour: '2-digit', minute: '2-digit' })
      : '—';
    const cena = a.cena != null ? Number(a.cena).toFixed(2) + ' zł/kWh' : '—';
    return '<div class="stopka">'
      + '<span>Cena tej godziny <b>' + cena + '</b></span>'
      + '<span>Plan do <b>' + doKiedy + '</b></span>'
      + '</div>';
  }

  _szkielet(tresc) {
    return ''
      + '<style>'
      + ':host{display:block}'
      + '.karta{background:linear-gradient(160deg,' + AURA.surface + ' 0%,' + AURA.canvas + ' 100%);'
      + 'border:1px solid ' + AURA.border + ';border-radius:24px;padding:18px 20px;'
      + 'color:' + AURA.textPrimary + ';'
      + 'font-family:Outfit,"DM Sans",Roboto,system-ui,-apple-system,sans-serif;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35)}'
      + '.head{display:flex;justify-content:space-between;align-items:center;gap:12px}'
      + '.teraz-tryb{display:flex;align-items:center;gap:10px;min-width:0}'
      + '.kropka{width:11px;height:11px;border-radius:9999px;background:var(--k);'
      + 'box-shadow:0 0 0 4px color-mix(in srgb,var(--k) 18%,transparent);flex:0 0 auto}'
      + '.tytul{font-size:19px;font-weight:700;letter-spacing:-.2px}'
      + '.pigulka{padding:5px 11px;border-radius:9999px;font-size:11px;white-space:nowrap;'
      + 'border:1px solid ' + AURA.borderMid + '}'
      + '.pigulka.on{color:' + AURA.primaryBright + ';background:rgba(52,211,153,.10);'
      + 'border-color:rgba(52,211,153,.35)}'
      + '.pigulka.off{color:' + AURA.textSecondary + ';background:rgba(255,255,255,.03)}'
      + '.teraz{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:16px 0 4px}'
      + '.kafel{background:rgba(255,255,255,.03);border:1px solid ' + AURA.border + ';'
      + 'border-radius:14px;padding:9px 10px;min-width:0}'
      + '.kafel .et{display:block;font-size:10px;letter-spacing:.7px;text-transform:uppercase;'
      + 'color:' + AURA.textMuted + '}'
      + '.kafel b{display:block;font-size:17px;font-weight:600;margin-top:2px;'
      + 'font-variant-numeric:tabular-nums;white-space:nowrap}'
      + '.kafel .dop{font-size:10px;color:' + AURA.textMuted + '}'
      + '.soc-bar{display:block;height:3px;border-radius:9999px;margin-top:6px;'
      + 'background:rgba(255,255,255,.08);overflow:hidden}'
      + '.soc-bar i{display:block;height:100%;background:' + AURA.primary + '}'
      + '.wykres{margin:10px -2px 0}'
      + 'svg text.oc{fill:' + AURA.textMuted + ';font-size:9px;text-anchor:middle;'
      + 'font-family:inherit}'
      + 'svg text.os-soc{fill:' + AURA.textMuted + ';font-size:8px;font-family:inherit}'
      + '.legenda{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px}'
      + '.poz{display:flex;align-items:center;gap:6px;font-size:12px;'
      + 'color:' + AURA.textSecondary + '}'
      + '.poz i{width:9px;height:9px;border-radius:3px;background:var(--k)}'
      + '.poz i.linia{width:14px;height:2px;border-radius:2px;background:'
      + AURA.primaryBright + '}'
      + '.stopka{margin-top:14px;padding-top:12px;border-top:1px solid ' + AURA.border + ';'
      + 'display:flex;justify-content:space-between;font-size:12px;'
      + 'color:' + AURA.textSecondary + '}'
      + '.stopka b{color:' + AURA.textPrimary + ';font-weight:600;'
      + 'font-variant-numeric:tabular-nums}'
      + '.pusto{padding:26px 0;text-align:center;color:' + AURA.textMuted + ';font-size:14px}'
      + '.karta code{font-family:ui-monospace,monospace;font-size:12px}'
      + '@media(max-width:520px){.teraz{grid-template-columns:repeat(3,1fr)}}'
      + '</style>'
      + '<div class="karta">' + tresc + '</div>';
  }
}

customElements.define('volter-plan-card', VolterPlanCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'volter-plan-card',
  name: 'Volter — plan pracy',
  description: 'Kokpit magazynu: wartości bieżące z falownika, plan z chmury i prognoza SoC.',
  preview: true,
});
