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

/** Wersja karty. Widoczna w stopce, zeby dalo sie jednym spojrzeniem odroznic
 *  „kod jest zly" od „przegladarka trzyma stary plik". Test w `test_karta_frontend.py`
 *  pilnuje, zeby nie rozjechala sie z `manifest.json`. */
const WERSJA = '2.6.1';

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

/** Moc czytelnie: waty do 1 kW, wyżej kilowaty. 4-cyfrowe liczby nie mieszczą
 *  się w kafelku, a „3,2 kW” niesie tę samą informację co „3226 W”. */
const moc = (w) => {
  if (w == null || Number.isNaN(w)) return '—';
  const abs = Math.abs(w);
  if (abs < 1000) return Math.round(w) + ' W';
  return (w / 1000).toFixed(abs < 10000 ? 2 : 1).replace('.', ',') + ' kW';
};

class VolterPlanCard extends HTMLElement {
  static getStubConfig() {
    return { entity: 'sensor.volter_energy_plan' };
  }

  setConfig(config) {
    // NIE rzucamy przy braku `entity`. `preview: true` w customCards każe HA zbudować
    // podgląd karty bez hass i bez konfiguracji — wyjątek w tym miejscu HA pokazuje
    // jako "configuration error" NA DASHBOARDZIE, nie tylko w podglądzie.
    // Brak encji to stan do pokazania użytkownikowi, a nie powód do wysadzenia karty.
    this._config = Object.assign({ entity: 'sensor.volter_energy_plan' }, config || {});
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this._render();
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
    if (!this.shadowRoot || !this._config) return;
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
      + kafel('PV', moc(pv), AURA.amber)
      + kafel('Dom', moc(dom), AURA.violet)
      + kafel('Bateria', moc(bat),
        bat != null && bat < 0 ? AURA.primary : AURA.orange)
      + kafel('Sieć', moc(siec),
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
    const dolSlupka = WYS_SOC + WYS_SLUP;
    const maks = Math.max.apply(null,
      sloty.map((s) => Math.abs(s.moc_w || 0)).concat([1]));

    // Indeks bieżącej godziny. Wszystko przed nim JUŻ SIĘ WYDARZYŁO — plan na te
    // godziny jest historią, a nie zapowiedzią, więc nie może wyglądać tak samo.
    let iTeraz = sloty.findIndex((s) => s.teraz);
    if (iTeraz < 0) iTeraz = 0;

    // Skala pierwiastkowa. Liniowa gubiła wszystko poniżej ~1 kW przy jednej
    // godzinie sprzedaży 4–5 kW: słupki 200 W miały kilka pikseli i nie dało się
    // w nie trafić kursorem. Pierwiastek zachowuje kolejność, a spłaszcza górę.
    const wysokosc = (p) => Math.max(4,
      Math.round(Math.sqrt(Math.abs(p || 0) / maks) * (WYS_SLUP - 8)));

    let kolumny = '';
    let etykiety = '';
    let siatka = '';
    let obszary = '';

    sloty.forEach((s, i) => {
      const cfg = AKCJE[s.akcja] || AKCJE.idle;
      const h = wysokosc(s.moc_w);
      const x = i * KOL_W;
      const y = dolSlupka - h;
      const przeszlosc = i < iTeraz;
      const tytul = [
        String(godzina(s.od)).padStart(2, '0') + ':00–'
          + String(godzina(s.do)).padStart(2, '0') + ':00',
        cfg.etykieta,
        s.moc_w != null ? moc(s.moc_w) : 'moc niezadana',
        s.cena != null ? Number(s.cena).toFixed(2).replace('.', ',') + ' zł/kWh' : null,
        s.soc_docelowy != null ? 'SoC ' + Math.round(s.soc_docelowy) + '%' : null,
        s.zrodlo_ladowania ? 'źródło: ' + s.zrodlo_ladowania : null,
        s.cel_rozladowania ? 'cel: ' + s.cel_rozladowania : null,
        s.eksport === false ? 'eksport zablokowany' : null,
        przeszlosc ? 'godzina miniona' : null,
      ].filter(Boolean).join(' · ');

      if (s.teraz) {
        siatka += '<rect x="' + x + '" y="0" width="' + KOL_W + '" height="'
          + dolSlupka + '" fill="rgba(255,255,255,.07)" rx="3"/>';
      }

      kolumny += '<rect x="' + (x + 2) + '" y="' + y + '" width="' + (KOL_W - 4)
        + '" height="' + h + '" rx="3" fill="' + cfg.kolor + '" opacity="'
        + (s.moc_w == null ? '.28' : (przeszlosc ? '.4' : '1')) + '"/>';

      // CAŁA kolumna jest obszarem najechania, nie sam słupek. Przy niskich mocach
      // słupek ma kilka pikseli i trafienie w niego było loterią.
      obszary += '<g class="hit"><title>' + esc(tytul) + '</title>'
        + '<rect x="' + x + '" y="0" width="' + KOL_W + '" height="' + dolSlupka
        + '" fill="transparent"/></g>';

      const g = godzina(s.od);
      if (g % 3 === 0) {
        etykiety += '<text x="' + (x + KOL_W / 2) + '" y="' + (H - 8)
          + '" class="oc">' + String(g).padStart(2, '0') + '</text>';
        siatka += '<line x1="' + x + '" y1="0" x2="' + x + '" y2="' + dolSlupka
          + '" stroke="rgba(255,255,255,.07)" stroke-width="1"/>';
      }
    });

    // Obszar prognozy: od bieżącej godziny w prawo, w ukośne kreski — jak w aplikacji.
    // Bez tego nie widać, gdzie kończy się fakt, a zaczyna założenie.
    const xProg = iTeraz * KOL_W;
    const prognoza = '<rect x="' + xProg + '" y="0" width="' + (W - xProg)
      + '" height="' + dolSlupka + '" fill="url(#volter-kreski)"/>';

    // Krzywa SoC WYŁĄCZNIE nad prognozą. Historycznego SoC nie mamy — siedzi
    // w recorderze HA, nie w planie — więc linia nad przeszłością byłaby
    // zmyślaniem danych, a nie informacją.
    let krzywa = '';
    let punktStart = '';
    if (socTeraz != null && pojemnosc > 0) {
      let soc = socTeraz;
      const yDlaSoc = (v) => WYS_SOC - (v / 100) * (WYS_SOC - 8) - 4;
      const punkty = [(xProg + 1).toFixed(1) + ',' + yDlaSoc(soc).toFixed(1)];
      for (let i = iTeraz; i < n; i += 1) {
        const cfg = AKCJE[sloty[i].akcja] || AKCJE.idle;
        const kwh = ((sloty[i].moc_w || 0) / 1000) * cfg.znak;
        soc = Math.max(0, Math.min(100, soc + (kwh / pojemnosc) * 100));
        punkty.push((i * KOL_W + KOL_W).toFixed(1) + ',' + yDlaSoc(soc).toFixed(1));
      }
      krzywa = '<polyline points="' + punkty.join(' ') + '" fill="none" stroke="'
        + AURA.primaryBright + '" stroke-width="2" stroke-linejoin="round"'
        + ' stroke-linecap="round"/>';
      punktStart = '<circle cx="' + (xProg + 1) + '" cy="' + yDlaSoc(socTeraz)
        + '" r="2.6" fill="' + AURA.primaryBright + '"/>';
    }

    const defs = '<defs><pattern id="volter-kreski" width="6" height="6" '
      + 'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
      + '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,.06)" '
      + 'stroke-width="2"/></pattern></defs>';

    return '<div class="wykres">'
      + '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" '
      + 'style="width:100%;height:' + Math.round(H * 1.4) + 'px">'
      + defs + prognoza + siatka + krzywa + punktStart + kolumny + etykiety + obszary
      + '</svg></div>';
  }

  _legenda(sloty) {
    const obecne = sloty.map((s) => s.akcja).filter((v, i, t) => t.indexOf(v) === i);
    return '<div class="legenda">' + obecne.map((k) => {
      const cfg = AKCJE[k] || AKCJE.idle;
      return '<span class="poz"><i style="--k:' + cfg.kolor + '"></i>' + cfg.etykieta + '</span>';
    }).join('')
      + '<span class="poz"><i class="linia"></i>Prognoza SoC</span>'
      + '<span class="poz"><i class="kreski"></i>Prognoza (przed: godziny minione)</span></div>';
  }

  _stopka(a) {
    const doKiedy = a.wazny_do
      ? new Date(a.wazny_do).toLocaleString('pl-PL',
        { weekday: 'short', hour: '2-digit', minute: '2-digit' })
      : '—';
    const cena = a.cena != null ? Number(a.cena).toFixed(2) + ' zł/kWh' : '—';
    return '<div class="stopka">'
      + '<span>Cena tej godziny <b>' + cena + '</b></span>'
      + '<span>Plan do <b>' + doKiedy + '</b> <i class="wer">v' + WERSJA + '</i></span>'
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
      + 'svg g.hit rect{fill:transparent}'
      + 'svg g.hit:hover rect{fill:rgba(255,255,255,.09)}'
      + '.poz i.kreski{width:14px;height:9px;border-radius:3px;'
      + 'background:repeating-linear-gradient(45deg,rgba(255,255,255,.22) 0 2px,'
      + 'transparent 2px 5px);border:1px solid ' + AURA.border + '}'
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
      + '.stopka .wer{font-style:normal;color:' + AURA.textMuted + ';font-size:10px;'
      + 'margin-left:6px}'
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
