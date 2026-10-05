# Frekvenstilladelser

Lokal web-app til at gemme frekvenstilladelses-PDF'er, vise en filtrerbar tabel og kontrollere tilladelser mod ATOLL API.

## Start

```powershell
python -m pip install -r requirements.txt
$env:ATOLL_CLIENT_ID = "..."
$env:ATOLL_CLIENT_SECRET = "..."
python app.py
```

Åbn derefter `http://127.0.0.1:8000`. API-legitimationsoplysninger må ikke gemmes i kode eller versionsstyrede filer.

Upload permit-PDF'er under **Gem eller opdatér tilladelser**. Den seneste PDF gemmes pr. tilladelsesnummer (eksempelvis `H032508`); en ny PDF med samme nummer erstatter kun den tidligere PDF og data, når dens `Dato` øverst i dokumentet er **nyere**. Samme eller ældre datoer overskriver aldrig den gemte version. Tilladelser fra alle gemte PDF'er bruges i hver kontrol.

Klik på **Kontrollér ATOLL API (DK)** for at hente og kontrollere alle danske links (`?country=DK`). ATOLL-data gemmes ikke.

Hver `Strækning` bliver til to rækker: én for Site A og én for Site B. PDF'ens dokumentdato bruges som `Frek. gyldig fra`. `Frekvensnummer` og `Frek. tilbagekaldt` efterlades tomme, da de ikke fremgår af den aktuelle PDF-type.

Landsdækkende/flade tilladelser understøttes også. De gemmes uden position eller SiteID og matches mod alle ATOLL-navne med det relevante tilladelsesnummer (for eksempel matcher `H102156` `H102156-1-1`).

Ved sammenligning matches ATOLL-felterne `1st Name` og `2nd Name` mod tilladelsesnummer og positionsnummer i formatet `H032813-340-3`. API'ets `Live` og `Planned` behandles som aktive links. Appen viser:

- ATOLL-links med status `Live` eller `Planned`, der ikke har en matchende tilladelse.
- ATOLL-links med status `Live` eller `Planned`, hvor frekvensparret ikke stemmer med den matchende tilladelse. P2P-tilladelser sammenlignes med deres præcise sende/modtage-frekvenser; flade tilladelser med båndgrænser kontrolleres inden for de tilladte bånd.

API'et indeholder ikke udfasede linkstatusser. Derfor kan rapporten **Tilladelser på udfasede links** ikke afgøre, om et link er udfaset, når kontrollen kører mod API'et.
