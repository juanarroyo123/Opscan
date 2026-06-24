# OpScan

Radar de opciones (volumen inusual + ratios) y calendario de catalizadores. Web estatica
en GitHub Pages que se actualiza sola con GitHub Actions. Solo fines educativos; no es
asesoramiento financiero.

## Subir a GitHub (version plana, sin carpetas)

1. Borra todos los archivos que tengas ahora en el repo.
2. "Add file" -> "Upload files" y arrastra TODOS estos sueltos a la vez:
   index.html, latest.json, catalysts.json, scan.py, catalyst_calendar.py,
   watchlist.json, catalysts.csv, requirements.txt, README.md
   -> "Commit changes".
3. Settings -> Pages -> Source "Deploy from a branch" -> Branch main, carpeta / (root) -> Save.
   En 1-2 min tendras tu URL publica y veras la app con los datos de ejemplo.

## Datos reales que se actualizan solos (opcional)

1. Clave gratis en finnhub.io. En el repo: Settings -> Secrets and variables -> Actions ->
   New repository secret -> nombre FINNHUB_TOKEN, valor: tu clave.
2. Settings -> Actions -> General -> Workflow permissions -> "Read and write" -> Save.
3. Crea el archivo del Action: "Add file" -> "Create new file" -> en el nombre escribe
   exactamente:  .github/workflows/scan.yml  (las barras crean las carpetas solas).
   Pega dentro el contenido del archivo scan.yml que te paso -> "Commit changes".
4. Pestana Actions -> "OpScan diario" -> Run workflow. En un par de minutos la web
   mostrara datos reales y luego se actualizara cada dia laborable.

Editar tu lista de valores: watchlist.json. Fechas PDUFA manuales: catalysts.csv.
