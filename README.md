# OpScan v2

Radar de **flujo inusual de opciones** (todos los vencimientos) combinado con:

- **Patrones de entrada y salida en futuros:** COT de la CFTC, precio frente a open interest y volumen.
- **Operaciones de congresistas:** Cámara, Senado y altos cargos, cruzadas con sus comités.
- **Catalizadores:** PDUFA y comités de la FDA, lecturas de ensayos, resultados, FOMC, OPEX y M&A.

Todo se junta en una puntuación de confluencia y en un **checklist de entrada**:

| Condición | Puntos |
|---|---|
| Congreso a favor (compras netas en 90 días) | +1 |
| Futuros a favor (régimen del índice y futuros del sector) | +1 |
| Flujo confirmado por subida de OI al día siguiente | +2 |

Es **ENTRADA** cuando el flujo está confirmado y la suma llega a 3 o más.

Funciona solo con **GitHub Actions** (gratis) y se publica en **GitHub Pages**. Sin servidores y sin APIs de pago.

> Solo fines educativos. No es asesoramiento financiero.

## Qué hace en cada ejecución

| Hora (UTC, L-V) | Modo | Qué hace |
|---|---|---|
| 12:35 | `premarket` | Lo hace todo. Compara el OI nuevo, que la OCC publica de madrugada, con el flujo marcado ayer y lo marca como **confirmado** o **no confirmado**. |
| Cada 30 min, de 13:05 a 20:35 | `intraday` | Solo cadenas de opciones de unos 650 valores (Cboe, con 15 min de retraso). |
| 21:30 | `full` | Cierre completo: calendario, congreso, futuros, opciones, Yahoo y alertas. |

## Fuentes de datos (todas gratis)

| Dato | Fuente | Clave |
|---|---|---|
| Cadenas de opciones, todos los vencimientos (volumen, OI, IV, griegas, bid/ask) | JSON público retrasado de Cboe; respaldo con yfinance | no |
| Universo S&P 500 y Nasdaq 100 | datasets/s-and-p-500-companies, Wikipedia y listas locales | no |
| COT de futuros (legacy y TFF) | API Socrata de la CFTC | no |
| Precio y volumen de futuros | Yahoo (yfinance) | no |
| Congreso (Cámara, Senado y ejecutivo) | [kadoa-org/congress-trading-monitor](https://github.com/kadoa-org/congress-trading-monitor) (MIT) | no |
| Comités de cada congresista | [unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators) | no |
| PDUFA, lecturas y AdCom de la FDA | [pdufa.bio](https://www.pdufa.bio) (exige enlace de atribución, ya incluido en la web) | no |
| Fin de ensayos clínicos (fase 2 y 3) | ClinicalTrials.gov, con el promotor convertido a ticker mediante la SEC | no |
| M&A (8-K) | SEC EDGAR | no, pero pon `SEC_USER_AGENT` |
| FOMC | federalreserve.gov, con fechas fijas de respaldo | no |
| Fechas de resultados e IPO | Finnhub | `FINNHUB_TOKEN` (gratis) |
| Macro (IPC, empleo…) | FMP | `FMP_TOKEN` (opcional) |
| Fundamentales, analistas, noticias e insiders (Formulario 4) | Yahoo (yfinance), solo para los 75 mejores | no |

## Instalación en GitHub (unos 10 minutos)

1. **Crea un repositorio público** (por ejemplo `opscan`). Tiene que ser público por dos motivos: GitHub Pages gratis solo funciona en repos públicos, y así las Actions no tienen límite de minutos.
2. **Sube todos los archivos** respetando las carpetas: `opscan/`, `docs/`, `config/`, `tests/` y `.github/workflows/`.
   - Si usas la web de GitHub, entra en *Add file → Upload files* y arrastra la carpeta completa.
   - La carpeta `.github` está oculta en Mac y Windows. Si no aparece al arrastrar, créala a mano con *Add file → Create new file* y el nombre `.github/workflows/opscan.yml`, y pega dentro su contenido. Haz lo mismo con `tests.yml`.
3. **Activa los permisos:** *Settings → Actions → General → Workflow permissions → "Read and write permissions"* y guarda.
4. **Activa Pages:** *Settings → Pages → Source: **GitHub Actions***.
5. **Secrets opcionales**, en *Settings → Secrets and variables → Actions → New repository secret*:
   - `FINNHUB_TOKEN`: clave gratis de finnhub.io, para las fechas de resultados.
   - `SEC_USER_AGENT`: tu nombre y email, por ejemplo `Juan Arroyo tuemail@gmail.com`. La SEC lo exige para usar su API.
   - `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`: para recibir las ENTRADAS en Telegram.
   - `FMP_TOKEN`: para el calendario macro.
6. **Primera prueba:** entra en *Actions → OpScan → Run workflow*, elige el modo `smoke` y pulsa *Run*. Tarda unos 2 minutos.
   - En el resumen de la ejecución verás el estado de cada fuente (OK o AVISO).
   - Si todo está bien, lánzalo otra vez en modo `full`. Tarda entre 5 y 15 minutos.
   - La web queda en `https://TU_USUARIO.github.io/opscan/`.

A partir de ahí se ejecuta solo cada día laborable. La pestaña **Estado** de la web muestra la salud de cada fuente.

### Notas importantes

- **Confirmación por OI:** necesita al menos dos sesiones de datos. **IV rank y volumen relativo:** necesitan unas 20 sesiones de histórico. El primer día esas columnas saldrán vacías y es normal.
- **Datos:** los datos y el estado (histórico de IV y alertas pendientes de confirmar) se guardan en la rama `data`, siempre en un único commit, así que el repo no engorda.
- **Workflows programados:** GitHub los desactiva tras 60 días sin actividad. El workflow se reactiva solo en cada ejecución de pre-apertura.
- **Lado agresor (ASK/BID):** es una **estimación**. Compara el último precio con el bid/ask del momento de la descarga. No son sweeps reales: para eso hace falta una fuente de pago (por ejemplo, la API de Unusual Whales o Polygon).
- **Retraso del Congreso:** la STOCK Act permite declarar hasta 45 días después. Úsalo como sesgo, no como momento de entrada.

## Configuración

Todo está en `config.yml` y se puede editar desde GitHub:

- `universe.watchlist`: tus valores. También se escanean solas las biotech con PDUFA o lectura próxima.
- `options.*`: umbrales de flujo inusual (volumen mínimo, Vol/OI, prima mínima, ballenas…).
- `scoring.*`: horizonte de catalizadores, ventana del congreso y puntos mínimos para ENTRADA.
- `futures.markets` y `futures.sector_links`: qué futuros se siguen y qué sector afecta cada uno.
- `config/catalysts_manual.csv`: fechas propias (`ticker,date,type,note`).

## Ejecutar en tu ordenador

```bash
pip install -r requirements.txt
python -m opscan --mode smoke --build-site _site     # prueba rápida
python -m opscan --mode full --build-site _site      # completo
python -m http.server -d _site 8000                  # abrir http://localhost:8000
python -m opscan --tickers NVDA,RCKT,SPX --mode full # solo unos valores
python -m pytest -q                                  # tests
```

## Estructura

```
opscan/
  options.py    cadenas Cboe/Yahoo, flujo inusual, IV30, max pain, GEX, movimiento esperado
  state.py      histórico diario, confirmación por OI, cachés
  futures.py    COT (legacy + TFF), cuadrantes precio/OI, régimen y contexto por sector
  congress.py   trades, comités, clusters
  catalysts.py  pdufa.bio, Finnhub, ClinicalTrials, SEC, FOMC, OPEX, manual
  enrich.py     Yahoo: fundamentales, analistas, noticias, insiders
  scoring.py    puntuación, dirección, checklist e idea de estructura
  alerts.py     Telegram
  pipeline.py   orquestación y generación de la web
docs/index.html   la web (pestañas Radar, Flujo, Futuros, Congreso, Calendario y Estado)
tests/            30 tests sin red (datos simulados y muestras reales de congreso y comités)
```

## Créditos e inspiración

- Datos del congreso: kadoa-org/congress-trading-monitor (MIT).
- Comités: unitedstates/congress-legislators.
- Catalizadores FDA: pdufa.bio.
- Ideas de diseño: HaochenHu1/options-flow-detector (puntuación de flujo con catalizadores) y klmn800/options_scanner (confirmación por OI al día siguiente).
- Formato COT: NDelventhal/cot_reports.
