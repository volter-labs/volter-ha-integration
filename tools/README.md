# tools/ — warsztat wizualny karty

## Po co

Karta Lovelace to jedyna część tej integracji, której poprawności nie da się
orzec z testów jednostkowych: przepełnienia, rozciągnięty tekst i łamanie układu
widać dopiero po wyrenderowaniu w prawdziwej przeglądarce, przy konkretnej
szerokości i z konkretnymi liczbami.

Ten warsztat renderuje kartę w Chrome przez CDP — **z originu Home Assistanta**,
więc ładowany jest dokładnie ten moduł, który dostaje użytkownik — i zapisuje
zrzut. Bez zależności: node 22 ma wbudowany `WebSocket`.

## Jak uruchomić

```bash
# 1. Chrome z portem debugowania
"C:/Program Files/Google/Chrome/Application/chrome.exe" \
  --headless=new --disable-gpu --remote-debugging-port=9333 \
  --user-data-dir=/c/tmp/chrome-profil --no-first-run about:blank &

# 2. Dane z żywej instalacji (atrybuty planu + wartości encji) do C:/tmp/dane-karty.json
#    — wyciągane z bazy recordera, patrz komenda w nagłówku zrzut-karty.mjs

# 3. Render i zrzut do C:/tmp/karta.png
node tools/zrzut-karty.mjs
```

`zrzut-render.js` buduje kilka kart obok siebie o różnych szerokościach
(440 / 320 / 260 px) i z różnymi wartościami — w tym celowo skrajnymi, jak
`-10,7 kW`. Trzy szerokości, bo każda łamie co innego: 440 px to typowa kolumna
sekcji na pulpicie, 320 px wąska kolumna, 260 px telefon.

## Uwaga o CORS

Strona z `file://` NIE zaimportuje modułu z HA — blokuje to CORS. Dlatego skrypt
najpierw nawiguje na origin HA, a dopiero potem wstrzykuje render. Niezalogowany
HA przekierowuje na `/auth/authorize`, więc render trzeba wstrzyknąć PO tym
przekierowaniu — inaczej mierzy się ekran logowania, nie kartę.

## sonda-karty.js

Odczytuje z wyrenderowanej karty to, czego nie widac na zrzucie: tresc wszystkich
podpowiedzi (`<title>` kolumn) i pozycje etykiet osi SoC w pikselach. Sluzy do
sprawdzenia, czy os siada na krzywej i czy dymek niesie to, co ma niesc —
zamiast najezdzania kursorem na 34 kolumny recznie.

Wstrzykiwana tak samo jak render, do tego samego dokumentu, PO nim.
