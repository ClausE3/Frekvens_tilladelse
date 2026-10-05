# MW_LINKS — MW permit candidates

Microwave links from ATOLL, with the TP3 permit link status where one exists. Intended for
matching MW permit candidates against ATOLL.

How to authenticate, page and handle errors is described in the generic guide,
[README.md](README.md). This page lists only what is specific to this endpoint.

## Endpoint

| | |
|---|---|
| Method | `GET` |
| Path | `mw_links/MW_PERMIT_CANDIDATES/` (case-sensitive, trailing slash required) |
| PROD URL | `https://dbhotel-ords.om.tre.se:8174/ords/omsinv/mw_links/MW_PERMIT_CANDIDATES/` |
| DEV URL | `https://dbhotel-ords.nextrel.tre.se:8174/ords/omsinv/mw_links/MW_PERMIT_CANDIDATES/` |
| Auth | OAuth2 client credentials, client granted `mw_links_role` |
| Page size | 25 rows by default. Use `?limit=` (max tested: `1000`) and `?offset=`. See [Pagination](#pagination). |
| Order | `name` ascending (stable across pages) |
| Filters | `?country=DK` (country code, e.g. `DK`). |

> **DEV vs PROD:** ATOLL is only connected to PROD. DEV data is not a reliable copy and may be
> empty or out of date. Use PROD for real data.

## Fields

JSON keys are lower case. A key may be missing when its value is `null`.

| Key | Type | Description |
|---|---|---|
| `name` | string | ATOLL link name, e.g. `BN0300A-BN0325B_01` |
| `link_status` | string \| null | TP3 link status display text, e.g. `Live`. `null` when the link has no matching TP3 permit link. |
| `name_1` | string \| null | ATOLL custom field `CF_NAME1`. Often empty. |
| `name_2` | string \| null | ATOLL custom field `CF_NAME2`. Often empty. |
| `site_a` | string | Site at end A, e.g. `BN0300A` |
| `site_b` | string | Site at end B |
| `freq_a` | number | Frequency at end A, MHz (may be in exponent form: `2.2078E+004` = 22078) |
| `freq_b` | number | Frequency at end B, MHz |
| `subband_a_to_b` | string | Band and channel width A→B, e.g. `23, 112MHz SE` |
| `subband_b_to_a` | string | Band and channel width B→A |
| `polarization_a` | string | Polarisation code at end A, as stored in ATOLL (values seen so far: `V`, `D`) |
| `country` | string | Country code of the link, e.g. `DK`. Filter with `?country=DK`. |

Not available from this endpoint: `Length (m)` and a separate MW-permit status do not exist in
ATOLL. `link_status` is the closest permit-side status.

> **Observed (PROD, 2026-10-05):** `name_1` is populated for only 5 of 4,032 `DK` links and
> `name_2` for none, so permit IDs cannot yet be matched from the API. `link_status` is `Live` or
> `null` for `DK`; there are no retired statuses (`Dismantled`, `Out of service`, ...).

## Country filter

```
GET {BASE}/mw_links/MW_PERMIT_CANDIDATES/?country=DK
GET {BASE}/mw_links/MW_PERMIT_CANDIDATES/?country=DK&offset=0&limit=1000
```

`country=DK` returned 4,032 links on PROD (5 pages of up to 1,000). It replaces the temporary
`name` prefix filter (`C`/`J`) that was used before the country flag existed.

## Pagination

Responses carry paging metadata next to `items`:

| Key | Description |
|---|---|
| `hasMore` | `true` while more rows exist. Stop when `false`. |
| `limit`, `offset`, `count` | Page size, start row and rows in this page. |
| `links[]` | Objects with `rel` and `href`: `self`, `describedby`, `first`, `next`, `prev`. |

Traverse by following `links[rel="next"].href` while `hasMore` is `true`.

> **Gotcha:** the `href` values are `http://...:8174/...`, but the server only answers HTTPS, so
> requesting an `href` as-is fails (`The response ended prematurely`). Reuse only its query string
> (`country`, `offset`, `limit`) against the `https://` base URL, or switch the scheme to `https`.
> Doing the former also guarantees the bearer token is only ever sent to the known host.

The older `?page=N` style (25 rows per page) still works but is not part of the documented API.

## Example

```bash
BASE="https://dbhotel-ords.om.tre.se:8174/ords/omsinv"
TOKEN=$(curl -s -X POST "$BASE/oauth/token" -u "$CLIENT_ID:$CLIENT_SECRET" \
          -d grant_type=client_credentials | jq -r .access_token)

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/mw_links/MW_PERMIT_CANDIDATES/" | jq '.items[0]'
```

```json
{
  "name": "BN0300A-BN0325B_01",
  "link_status": "Live",
  "site_a": "BN0300A",
  "site_b": "BN0325B",
  "freq_a": 22078,
  "freq_b": 23086,
  "subband_a_to_b": "23, 112MHz SE",
  "subband_b_to_a": "23, 112MHz SE",
  "polarization_a": "D"
}
```

To fetch every row, use one of the loops in [README.md §6](README.md#6-full-fetch-examples).

## Data source and freshness

ATOLL `MWLINKS` → OMSINV staging (`STG_ATOLL_MWLINKS`) → view `MART_V_MW_PERMIT_CANDIDATES`,
with `link_status` joined in from TP3 through SMIP. The data is refreshed when the ATOLL ETL
batch runs, so it is as fresh as the last successful load.
