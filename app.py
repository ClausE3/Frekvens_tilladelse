from __future__ import annotations

import cgi
import base64
import csv
import itertools
import json
import os
import re
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from pypdf import PdfReader


HOST = os.environ.get("APP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APP_PORT", "8000"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SITE_ID_PATTERN = r"[A-Z]{1,2}\d{4}[A-Z]?"
PERMIT_STORAGE_DIR = Path(__file__).parent / "data" / "permits"
PARSER_VERSION = 8
ATOLL_API_BASE_URL = "https://dbhotel-ords.om.tre.se:8174/ords/omsinv"
ATOLL_API_COUNTRY = "DK"
ATOLL_API_PAGE_SIZE = 1000
MAX_ATOLL_API_PAGES = 1_000
ACTIVE_LINK_STATUSES = {"In service", "Live", "Planned"}
MISSING_PERMIT_LINK_STATUSES = {"In service", "Planned"}
RETIRED_LINK_STATUSES = {
    "Dismantled",
    "Out of service",
    "CPR Approved",
    "CPR waiting approval",
    "--",
}
DANISH_MONTHS = {
    "januar": "01",
    "februar": "02",
    "marts": "03",
    "april": "04",
    "maj": "05",
    "juni": "06",
    "juli": "07",
    "august": "08",
    "september": "09",
    "oktober": "10",
    "november": "11",
    "december": "12",
}


def clean_frequency(value: str) -> str:
    return value.split(",", 1)[0]


def center_frequency(low: str, high: str) -> str:
    center = (float(low.replace(",", ".")) + float(high.replace(",", "."))) / 2
    return f"{center:.5f}".rstrip("0").rstrip(".").replace(".", ",")


def normalize_document_date(value: str) -> str:
    numeric_date = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", value)
    if numeric_date:
        day, month, year = numeric_date.groups()
        return f"{int(day):02d}-{int(month):02d}-{year} 00:00"

    textual_date = re.search(r"(\d{1,2})\s+([a-zæ]+)\s+(\d{4})", value.lower())
    if textual_date and textual_date.group(2) in DANISH_MONTHS:
        day, month, year = textual_date.groups()
        return f"{int(day):02d}-{DANISH_MONTHS[month]}-{year} 00:00"
    return ""


def extract_header(text: str) -> dict[str, str]:
    date_match = re.search(r"Dato:\s*([^\n]+)", text)
    permit_match = re.search(r"Tilladelsesnummer:\s*([A-Z]\d+)", text)
    customer_match = re.search(r"Brugernummer:\s*(?:DAFF)?(\d+)", text)
    name_match = re.search(r"(?m)^(.+?)\s+Dato:", text)

    valid_from = normalize_document_date(date_match.group(1)) if date_match else ""

    return {
        "Navn": name_match.group(1).strip() if name_match else "",
        "Tilladelsesnummer": permit_match.group(1) if permit_match else "",
        "Frek. gyldig fra": valid_from,
        "Kundenummer": customer_match.group(1) if customer_match else "",
    }


def parse_permit(pdf_bytes: bytes) -> list[dict[str, str]]:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:
        raise ValueError("PDF'en kunne ikke læses. Kontrollér at den ikke er beskadiget eller krypteret.") from error

    header = extract_header(text)
    nationwide_bands = re.findall(
        r"(\d{4,6},\d+)\s*[–-]\s*(\d{4,6},\d+)\s*MHz",
        text,
    )
    nationwide_bandwidth = re.search(r"B[å�]ndbredde\s+(\d+(?:,\d+)?)\s*MHz", text)
    is_nationwide = bool(re.search(r"Landsd[æ�]kkende|Hele landet", text))
    if is_nationwide and len(nationwide_bands) >= 2 and nationwide_bandwidth:
        first_band, second_band = nationwide_bands[:2]
        return [
            {
                **header,
                "Positionnummer": "",
                "Frekvensnummer": "",
                "Beregnet sende centerfrekvens": center_frequency(*first_band),
                "Beregnet modtage centerfrekvens": center_frequency(*second_band),
                "Båndbredde vilkår": clean_frequency(nationwide_bandwidth.group(1)),
                "Frekvensstatus": "Udstedt",
                "Frek. tilbagekaldt": "",
                "_polarisation": "",
                "SiteID": "",
                "_flatPermit": "true",
                "_permittedFrequencyRanges": [
                    [float(value.replace(",", ".")) for value in first_band],
                    [float(value.replace(",", ".")) for value in second_band],
                ],
            }
        ]

    flat_sections = re.split(r"Str[æ�]kning\.?:\s*(\d+)", text)
    flat_rows: list[dict[str, str]] = []
    if is_nationwide:
        for index in range(1, len(flat_sections), 2):
            section = flat_sections[index + 1]
            frequencies = re.search(
                r"Sendefrekvens A:\s*(?:Sendefrekvens B:\s*)?"
                r"(\d{4,6},\d+)\s*MHz\s*(?:Sendefrekvens B:\s*)?"
                r"(\d{4,6},\d+)\s*MHz.*?"
                r"B[å�]ndbredde:?\s*(\d+(?:,\d+)?)\s*MHz",
                section,
                re.DOTALL,
            )
            if not frequencies:
                continue
            send_a, send_b, bandwidth = frequencies.groups()
            flat_rows.append(
                {
                    **header,
                    "Positionnummer": "",
                    "Frekvensnummer": "",
                    "Beregnet sende centerfrekvens": clean_frequency(send_a),
                    "Beregnet modtage centerfrekvens": clean_frequency(send_b),
                    "Båndbredde vilkår": clean_frequency(bandwidth),
                    "Frekvensstatus": "Udstedt",
                    "Frek. tilbagekaldt": "",
                    "SiteID": "",
                    "_flatPermit": "true",
                    "_polarisation": "",
                }
            )
    if flat_rows:
        return flat_rows

    sections = re.split(r"Str[æ�]kning\s+(\d+)", text)
    rows: list[dict[str, str]] = []

    for index in range(1, len(sections), 2):
        position_number = sections[index]
        section = sections[index + 1]
        polarisation = re.search(r"Polarisation:\s*([VHD])", section)
        sites = re.search(
            rf"Site A\s+Site B.*?({SITE_ID_PATTERN})\s+({SITE_ID_PATTERN})",
            section,
            re.DOTALL,
        )
        frequency_section = section.split("Sendefrekvens A", 1)
        frequencies = re.findall(
            r"(\d{4,6},\d+)\s+(\d+(?:,\d+)?)\s+(\d{4,6},\d+)",
            frequency_section[1] if len(frequency_section) == 2 else "",
            re.DOTALL,
        )
        if not sites or not frequencies:
            continue

        site_a, site_b = sites.groups()
        for send_a, bandwidth, send_b in frequencies:
            common = {
                **header,
                "Positionnummer": position_number,
                # The source permit does not state a frequency number.
                "Frekvensnummer": "",
                "Båndbredde vilkår": clean_frequency(bandwidth),
                "Frekvensstatus": "Udstedt",
                "Frek. tilbagekaldt": "",
                "_polarisation": polarisation.group(1) if polarisation else "",
            }
            rows.extend(
                [
                    {
                        **common,
                        "Beregnet sende centerfrekvens": clean_frequency(send_a),
                        "Beregnet modtage centerfrekvens": clean_frequency(send_b),
                        "SiteID": site_a,
                    },
                    {
                        **common,
                        "Beregnet sende centerfrekvens": clean_frequency(send_b),
                        "Beregnet modtage centerfrekvens": clean_frequency(send_a),
                        "SiteID": site_b,
                    },
                ]
            )

    if not rows:
        raise ValueError("Ingen frekvensstrækninger blev fundet. PDF-layoutet understøttes muligvis ikke endnu.")
    return rows


def parse_atoll_export(export_bytes: bytes) -> list[dict[str, str]]:
    try:
        text = export_bytes.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(text.splitlines(), delimiter=";"))
    except csv.Error as error:
        raise ValueError("ATOLL-eksporten kunne ikke læses.") from error

    required_columns = {"Name", "Link Status", "1st Name", "2nd Name"}
    if not rows or not required_columns.issubset(rows[0]):
        raise ValueError("ATOLL-filen mangler de krævede kolonner: Name, Link Status, 1st Name og 2nd Name.")
    return rows


def atoll_api_credentials() -> tuple[str, str]:
    client_id = os.environ.get("ATOLL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ATOLL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            "ATOLL API mangler konfiguration. Sæt ATOLL_CLIENT_ID og ATOLL_CLIENT_SECRET før appen startes."
        )
    return client_id, client_secret


def atoll_api_json(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ValueError(f"ATOLL API returnerede HTTP {error.code}.") from error
    except URLError as error:
        raise ValueError("ATOLL API kunne ikke kontaktes.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("ATOLL API returnerede et ugyldigt svar.") from error
    if not isinstance(payload, dict):
        raise ValueError("ATOLL API returnerede et uventet svar.")
    return payload


def atoll_api_token(client_id: str, client_secret: str) -> str:
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = Request(
        f"{ATOLL_API_BASE_URL}/oauth/token",
        data=urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    token = atoll_api_json(request).get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("ATOLL API returnerede ikke et adgangstoken.")
    return token


def atoll_api_row(item: dict[str, Any]) -> dict[str, str]:
    required_fields = ("name", "site_a", "site_b", "freq_a", "freq_b")
    missing_fields = [field for field in required_fields if item.get(field) is None]
    if missing_fields:
        raise ValueError(f"ATOLL API-svaret mangler: {', '.join(missing_fields)}.")
    return {
        "Name": str(item["name"]),
        "Link Status": str(item.get("atoll_link_status") or ""),
        "1st Name": str(item.get("name_1") or ""),
        "2nd Name": str(item.get("name_2") or ""),
        "Site A": str(item["site_a"]),
        "Site B": str(item["site_b"]),
        "Frequency A (MHz)": str(item["freq_a"]),
        "Frequency B (MHz)": str(item["freq_b"]),
        "Sub-band A>>B": str(item.get("subband_a_to_b") or ""),
        "Sub-band B>>A": str(item.get("subband_b_to_a") or ""),
        "Polarisation A": str(item.get("polarization_a") or ""),
    }


def fetch_atoll_api_rows(country: str = ATOLL_API_COUNTRY) -> list[dict[str, str]]:
    client_id, client_secret = atoll_api_credentials()
    token = atoll_api_token(client_id, client_secret)
    rows: list[dict[str, str]] = []
    parameters = {"country": country, "limit": str(ATOLL_API_PAGE_SIZE), "offset": "0"}
    for _ in range(MAX_ATOLL_API_PAGES):
        request = Request(
            f"{ATOLL_API_BASE_URL}/mw_links/MW_PERMIT_CANDIDATES/?{urlencode(parameters)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = atoll_api_json(request)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("ATOLL API returnerede ingen gyldig linksamling.")
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("ATOLL API returnerede en ugyldig linkrække.")
        rows.extend(atoll_api_row(item) for item in items)
        if not payload.get("hasMore"):
            return rows
        next_href = next(
            (
                link.get("href")
                for link in payload.get("links") or []
                if isinstance(link, dict) and link.get("rel") == "next"
            ),
            None,
        )
        if not isinstance(next_href, str):
            raise ValueError("ATOLL API angav flere rækker men intet next-link.")
        # The API returns http:// links; only the query is reused, against the HTTPS base URL.
        parameters = dict(parse_qsl(urlsplit(next_href).query))
        parameters["country"] = country
    raise ValueError("ATOLL API paginering oversteg grænsen.")


def save_permit(pdf_bytes: bytes, filename: str) -> dict[str, str | int]:
    rows = parse_permit(pdf_bytes)
    permit_number = rows[0]["Tilladelsesnummer"]
    if not permit_number:
        raise ValueError("Tilladelsesnummer blev ikke fundet i PDF'en.")
    document_date = rows[0]["Frek. gyldig fra"]
    try:
        document_date_value = datetime.strptime(document_date, "%d-%m-%Y %H:%M")
    except ValueError as error:
        raise ValueError(f"Dato blev ikke fundet eller kunne ikke læses i tilladelse {permit_number}.") from error

    PERMIT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = PERMIT_STORAGE_DIR / f"{permit_number}.pdf"
    json_path = PERMIT_STORAGE_DIR / f"{permit_number}.json"
    if json_path.is_file():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
            existing_date = existing.get("documentDate") or existing["rows"][0]["Frek. gyldig fra"]
            existing_date_value = datetime.strptime(existing_date, "%d-%m-%Y %H:%M")
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as error:
            raise ValueError(f"Den gemte tilladelse '{permit_number}' har ingen gyldig dokumentdato.") from error
        if document_date_value <= existing_date_value:
            return {
                "permitNumber": permit_number,
                "sourceFilename": existing.get("sourceFilename", ""),
                "rows": len(existing["rows"]),
                "status": "skipped",
                "documentDate": existing_date,
            }

    temporary_pdf_path = pdf_path.with_suffix(".pdf.tmp")
    temporary_json_path = json_path.with_suffix(".json.tmp")
    temporary_pdf_path.write_bytes(pdf_bytes)
    temporary_json_path.write_text(
        json.dumps(
            {
                "permitNumber": permit_number,
                "sourceFilename": filename,
                "documentDate": document_date,
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "parserVersion": PARSER_VERSION,
                "rows": rows,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_pdf_path, pdf_path)
    os.replace(temporary_json_path, json_path)
    return {
        "permitNumber": permit_number,
        "sourceFilename": filename,
        "rows": len(rows),
        "status": "saved",
        "documentDate": document_date,
    }


def stored_permits() -> tuple[list[dict[str, str]], list[dict[str, str | int]]]:
    if not PERMIT_STORAGE_DIR.is_dir():
        return [], []

    all_rows: list[dict[str, str]] = []
    documents: list[dict[str, str | int]] = []
    for path in sorted(PERMIT_STORAGE_DIR.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            rows = document["rows"]
            if not isinstance(rows, list) or not rows:
                raise ValueError("ingen rækker")
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            raise ValueError(f"Den gemte tilladelse '{path.name}' kunne ikke læses.") from error
        if document.get("parserVersion") != PARSER_VERSION:
            pdf_path = path.with_suffix(".pdf")
            if pdf_path.is_file():
                rows = parse_permit(pdf_path.read_bytes())
                document["rows"] = rows
                document["documentDate"] = rows[0]["Frek. gyldig fra"]
                document["parserVersion"] = PARSER_VERSION
                temporary_path = path.with_suffix(".json.tmp")
                temporary_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary_path, path)
        all_rows.extend(rows)
        frequency_values = [
            float(row[field].replace(",", "."))
            for row in rows
            for field in ("Beregnet sende centerfrekvens", "Beregnet modtage centerfrekvens")
            if row.get(field)
        ]
        if min(frequency_values) >= 71000 and max(frequency_values) <= 86000:
            frequency_band = "71-86 GHz"
        else:
            frequency_band = f"{round(sum(frequency_values) / len(frequency_values) / 1000)} GHz"
        documents.append(
            {
                "permitNumber": document["permitNumber"],
                "sourceFilename": document.get("sourceFilename", ""),
                "documentDate": document.get("documentDate", document["rows"][0]["Frek. gyldig fra"]),
                "updatedAt": document.get("updatedAt", ""),
                "rows": len(rows),
                "frequencyBand": frequency_band,
                "type": "flade" if any(row.get("_flatPermit") == "true" for row in rows) else "p2p",
            }
        )
    return all_rows, documents


def permit_keys(permit_rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    keys: dict[str, dict[str, str]] = {}
    for row in permit_rows:
        key = (
            row["Tilladelsesnummer"]
            if row.get("_flatPermit") == "true"
            else f"{row['Tilladelsesnummer']}-{row['Positionnummer']}"
        )
        keys.setdefault(key, row)
    return keys


def permit_key(row: dict[str, Any]) -> str:
    return (
        row["Tilladelsesnummer"]
        if row.get("_flatPermit") == "true"
        else f"{row['Tilladelsesnummer']}-{row['Positionnummer']}"
    )


def parse_atoll_frequency(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(re.sub(r"\s+", "", value).replace(",", "."))
    except ValueError:
        return None


def merge_frequency_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 0.01:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def flat_frequency_coverage(permit_rows: list[dict[str, Any]]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    if not permit_rows or not all(row.get("_flatPermit") == "true" for row in permit_rows):
        return None
    if any(row.get("_permittedFrequencyRanges") for row in permit_rows):
        return None

    send_ranges: list[tuple[float, float]] = []
    receive_ranges: list[tuple[float, float]] = []
    for row in permit_rows:
        send = parse_atoll_frequency(row["Beregnet sende centerfrekvens"])
        receive = parse_atoll_frequency(row["Beregnet modtage centerfrekvens"])
        bandwidth = parse_atoll_frequency(row["Båndbredde vilkår"])
        if send is None or receive is None or bandwidth is None:
            return None
        half_bandwidth = bandwidth / 2
        send_ranges.append((send - half_bandwidth, send + half_bandwidth))
        receive_ranges.append((receive - half_bandwidth, receive + half_bandwidth))
    return merge_frequency_ranges(send_ranges), merge_frequency_ranges(receive_ranges)


def interval_is_covered(center: float, bandwidth: float, ranges: list[tuple[float, float]]) -> bool:
    start = center - bandwidth / 2
    end = center + bandwidth / 2
    return any(start >= lower - 0.01 and end <= upper + 0.01 for lower, upper in ranges)


def p2p_channel_is_covered(
    frequency_a: float,
    frequency_b: float,
    bandwidth: float,
    permit_rows: list[dict[str, Any]],
) -> bool:
    for row in permit_rows:
        if row.get("_flatPermit") == "true":
            continue
        send = parse_atoll_frequency(row["Beregnet sende centerfrekvens"])
        receive = parse_atoll_frequency(row["Beregnet modtage centerfrekvens"])
        permit_bandwidth = parse_atoll_frequency(row["Båndbredde vilkår"])
        if send is None or receive is None or permit_bandwidth is None:
            continue
        send_range = [(send - permit_bandwidth / 2, send + permit_bandwidth / 2)]
        receive_range = [(receive - permit_bandwidth / 2, receive + permit_bandwidth / 2)]
        if (
            interval_is_covered(frequency_a, bandwidth, send_range)
            and interval_is_covered(frequency_b, bandwidth, receive_range)
        ) or (
            interval_is_covered(frequency_b, bandwidth, send_range)
            and interval_is_covered(frequency_a, bandwidth, receive_range)
        ):
            return True
    return False


def bonded_frequency_pairs(permit_rows: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    frequency_pairs: dict[tuple[float, float, float], None] = {}
    for row in permit_rows:
        send = parse_atoll_frequency(row["Beregnet sende centerfrekvens"])
        receive = parse_atoll_frequency(row["Beregnet modtage centerfrekvens"])
        bandwidth = parse_atoll_frequency(row["Båndbredde vilkår"])
        if send is not None and receive is not None and bandwidth is not None:
            frequency_pairs[(send, receive, bandwidth)] = None

    bonded: list[tuple[float, float, float]] = []
    for first, second in itertools.combinations(frequency_pairs, 2):
        first_send, first_receive, bandwidth = first
        second_send, second_receive, second_bandwidth = second
        if (
            abs(bandwidth - second_bandwidth) < 0.01
            and abs(abs(first_send - second_send) - bandwidth) < 0.01
            and abs(abs(first_receive - second_receive) - bandwidth) < 0.01
        ):
            bonded.append(
                (
                    (first_send + second_send) / 2,
                    (first_receive + second_receive) / 2,
                    bandwidth,
                )
            )
    return bonded


def matching_frequency_description(permit_rows: list[dict[str, Any]]) -> str:
    ranges = {
        tuple(tuple(item) for item in row.get("_permittedFrequencyRanges", []))
        for row in permit_rows
        if row.get("_permittedFrequencyRanges")
    }
    if ranges:
        return " eller ".join(
            " / ".join(f"{low:g}-{high:g} MHz" for low, high in frequency_ranges)
            for frequency_ranges in sorted(ranges)
        )
    coverage = flat_frequency_coverage(permit_rows)
    if coverage:
        send_ranges, receive_ranges = coverage
        return " eller ".join(
            f"{send_lower:g}-{send_upper:g} / {receive_lower:g}-{receive_upper:g} MHz (samlet licensbånd)"
            for (send_lower, send_upper), (receive_lower, receive_upper) in zip(send_ranges, receive_ranges)
        )
    descriptions = {
        f"{row['Beregnet sende centerfrekvens']} / {row['Beregnet modtage centerfrekvens']} MHz"
        for row in permit_rows
    }
    descriptions.update(
        f"{send:g} / {receive:g} MHz (2 x {bandwidth:g} MHz)"
        for send, receive, bandwidth in bonded_frequency_pairs(permit_rows)
    )
    return " eller ".join(sorted(descriptions))


def frequencies_match(atoll_row: dict[str, str], permit_rows: list[dict[str, Any]]) -> bool:
    frequency_a = parse_atoll_frequency(atoll_row.get("Frequency A (MHz)"))
    frequency_b = parse_atoll_frequency(atoll_row.get("Frequency B (MHz)"))
    if frequency_a is None or frequency_b is None:
        return False
    requested_bandwidth = atoll_bandwidth(atoll_row)

    for row in permit_rows:
        frequency_ranges = row.get("_permittedFrequencyRanges")
        if frequency_ranges:
            first_band, second_band = frequency_ranges
            if (
                first_band[0] <= frequency_a <= first_band[1]
                and second_band[0] <= frequency_b <= second_band[1]
            ) or (
                first_band[0] <= frequency_b <= first_band[1]
                and second_band[0] <= frequency_a <= second_band[1]
            ):
                return True
            continue

        send_frequency = parse_atoll_frequency(row["Beregnet sende centerfrekvens"])
        receive_frequency = parse_atoll_frequency(row["Beregnet modtage centerfrekvens"])
        if send_frequency is None or receive_frequency is None:
            continue
        if (
            abs(frequency_a - send_frequency) < 0.01
            and abs(frequency_b - receive_frequency) < 0.01
        ) or (
            abs(frequency_a - receive_frequency) < 0.01
            and abs(frequency_b - send_frequency) < 0.01
        ):
            return True
    if requested_bandwidth is not None and p2p_channel_is_covered(
        frequency_a, frequency_b, requested_bandwidth, permit_rows
    ):
        return True
    coverage = flat_frequency_coverage(permit_rows)
    if requested_bandwidth is not None and coverage:
        send_ranges, receive_ranges = coverage
        if (
            interval_is_covered(frequency_a, requested_bandwidth, send_ranges)
            and interval_is_covered(frequency_b, requested_bandwidth, receive_ranges)
        ) or (
            interval_is_covered(frequency_b, requested_bandwidth, send_ranges)
            and interval_is_covered(frequency_a, requested_bandwidth, receive_ranges)
        ):
            return True
    for send_frequency, receive_frequency, permit_bandwidth in bonded_frequency_pairs(permit_rows):
        if requested_bandwidth is None or abs(requested_bandwidth - (2 * permit_bandwidth)) >= 0.01:
            continue
        if (
            abs(frequency_a - send_frequency) < 0.01
            and abs(frequency_b - receive_frequency) < 0.01
        ) or (
            abs(frequency_a - receive_frequency) < 0.01
            and abs(frequency_b - send_frequency) < 0.01
        ):
            return True
    return False


def atoll_bandwidth(atoll_row: dict[str, str]) -> float | None:
    values: list[float] = []
    for field in ("Sub-band A>>B", "Sub-band B>>A"):
        values.extend(
            float(value.replace(",", "."))
            for value in re.findall(r"(\d+(?:,\d+)?)\s*MHz", atoll_row.get(field) or "")
        )
    return values[-1] if values else None


def atoll_polarisation(atoll_row: dict[str, str]) -> str:
    value = (atoll_row.get("Polarisation A") or "").strip().lower()
    return {"vertical": "V", "horizontal": "H", "dual": "D", "v": "V", "h": "H", "d": "D"}.get(value, "")


def frequency_difference(atoll_row: dict[str, str], permit_rows: list[dict[str, Any]]) -> float:
    frequency_a = parse_atoll_frequency(atoll_row.get("Frequency A (MHz)"))
    frequency_b = parse_atoll_frequency(atoll_row.get("Frequency B (MHz)"))
    if frequency_a is None or frequency_b is None:
        return float("inf")

    differences: list[float] = []
    for row in permit_rows:
        frequency_ranges = row.get("_permittedFrequencyRanges")
        if frequency_ranges:
            first_band, second_band = frequency_ranges
            def range_distance(value: float, frequency_range: list[float]) -> float:
                return max(frequency_range[0] - value, 0, value - frequency_range[1])
            differences.append(
                min(
                    range_distance(frequency_a, first_band) + range_distance(frequency_b, second_band),
                    range_distance(frequency_a, second_band) + range_distance(frequency_b, first_band),
                )
            )
            continue
        send_frequency = parse_atoll_frequency(row["Beregnet sende centerfrekvens"])
        receive_frequency = parse_atoll_frequency(row["Beregnet modtage centerfrekvens"])
        if send_frequency is not None and receive_frequency is not None:
            differences.append(
                min(
                    abs(frequency_a - send_frequency) + abs(frequency_b - receive_frequency),
                    abs(frequency_a - receive_frequency) + abs(frequency_b - send_frequency),
                )
            )
    requested_bandwidth = atoll_bandwidth(atoll_row)
    if requested_bandwidth is not None and p2p_channel_is_covered(
        frequency_a, frequency_b, requested_bandwidth, permit_rows
    ):
        differences.append(0)
    coverage = flat_frequency_coverage(permit_rows)
    if requested_bandwidth is not None and coverage:
        send_ranges, receive_ranges = coverage
        if (
            interval_is_covered(frequency_a, requested_bandwidth, send_ranges)
            and interval_is_covered(frequency_b, requested_bandwidth, receive_ranges)
        ) or (
            interval_is_covered(frequency_b, requested_bandwidth, send_ranges)
            and interval_is_covered(frequency_a, requested_bandwidth, receive_ranges)
        ):
            differences.append(0)
    if requested_bandwidth is not None:
        for send_frequency, receive_frequency, permit_bandwidth in bonded_frequency_pairs(permit_rows):
            if abs(requested_bandwidth - (2 * permit_bandwidth)) < 0.01:
                differences.append(
                    min(
                        abs(frequency_a - send_frequency) + abs(frequency_b - receive_frequency),
                        abs(frequency_a - receive_frequency) + abs(frequency_b - send_frequency),
                    )
                )
    return min(differences, default=float("inf"))


def site_prefix(site_id: str | None) -> str:
    return (site_id or "").strip().upper()[:6]


def permit_candidates(atoll_row: dict[str, str], permit_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    for row in permit_rows:
        grouped_rows.setdefault(permit_key(row), []).append(row)

    requested_bandwidth = atoll_bandwidth(atoll_row)
    requested_polarisation = atoll_polarisation(atoll_row)
    atoll_sites = {site_prefix(atoll_row.get("Site A")), site_prefix(atoll_row.get("Site B"))}
    candidates: list[tuple[float, dict[str, str]]] = []
    for key, rows in grouped_rows.items():
        is_flat_permit = rows[0].get("_flatPermit") == "true"
        permit_sites = {site_prefix(row.get("SiteID")) for row in rows if row.get("SiteID")}
        if not is_flat_permit and (not atoll_sites or "" in atoll_sites or permit_sites != atoll_sites):
            continue
        difference = frequency_difference(atoll_row, rows)
        if difference == float("inf"):
            continue
        permit_bandwidths = [
            parse_atoll_frequency(row["Båndbredde vilkår"])
            for row in rows
        ]
        permit_bandwidth = next((bandwidth for bandwidth in permit_bandwidths if bandwidth is not None), None)
        if is_flat_permit and (permit_bandwidth is None or difference > permit_bandwidth):
            continue
        permit_polarisation = rows[0].get("_polarisation", "")
        bandwidth_matches = requested_bandwidth is None or (
            permit_bandwidth is not None and abs(requested_bandwidth - permit_bandwidth) < 0.01
        )
        polarisation_matches = not requested_polarisation or not permit_polarisation or requested_polarisation == permit_polarisation
        candidates.append(
            (
                difference + (0 if bandwidth_matches else 100) + (0 if polarisation_matches else 10),
                {
                    "Tilladelses-ID": key,
                    "Type": "flade" if is_flat_permit else "p2p",
                    "Matchende sites": "Flad tilladelse" if is_flat_permit else " / ".join(sorted(permit_sites)),
                    "Polarisation": permit_polarisation or "Ikke angivet",
                    "Tilladelsesfrekvenser": matching_frequency_description(rows),
                    "Båndbredde": f"{rows[0]['Båndbredde vilkår']} MHz",
                    "Sammenligning": (
                        "Frekvens matcher"
                        if difference < 0.01
                        else f"Nærmeste frekvensafvigelse: {difference:g} MHz"
                    ),
                },
            )
        )
    return [candidate for _, candidate in sorted(candidates, key=lambda candidate: candidate[0])[:5]]


def permit_report_details(key: str, permit_rows: list[dict[str, Any]]) -> dict[str, str]:
    rows = [row for row in permit_rows if permit_key(row) == key]
    bandwidths = sorted(
        {row["Båndbredde vilkår"] for row in rows},
        key=lambda value: parse_atoll_frequency(value) or 0,
    )
    return {
        "Tilladelsesfrekvens": matching_frequency_description(rows),
        "Båndbredde": " / ".join(f"{bandwidth} MHz" for bandwidth in bandwidths),
    }


def names_to_permit_keys(atoll_row: dict[str, str]) -> dict[str, str]:
    matches: dict[str, str] = {}
    for column in ("1st Name", "2nd Name"):
        value = (atoll_row.get(column) or "").strip()
        match = re.search(r"\b([A-Z]\d+-\d+)(?:-\d+)?\b", value)
        if match:
            matches[match.group(1)] = value
            matches[match.group(1).split("-", 1)[0]] = value
        permit_number_match = re.search(r"\b([A-Z]\d+)\b", value)
        if permit_number_match:
            matches[permit_number_match.group(1)] = value
    return matches


def compare_permits(
    permit_rows: list[dict[str, str]],
    atoll_rows: list[dict[str, str]],
    include_unmatched_permits: bool = True,
) -> dict[str, list[dict[str, str]]]:
    permits = permit_keys(permit_rows)
    matched_retired: dict[str, list[dict[str, str]]] = {}
    matched_p2p_permits: set[str] = set()
    missing_active: list[dict[str, str]] = []
    incorrect_frequency: list[dict[str, str]] = []

    for row in atoll_rows:
        status = (row.get("Link Status") or "").strip()
        matches = names_to_permit_keys(row)
        matched = {key: name for key, name in matches.items() if key in permits}
        matched_p2p_permits.update(
            key for key in matched if permits[key].get("_flatPermit") != "true"
        )

        if status in MISSING_PERMIT_LINK_STATUSES and not matched:
            missing_active.append(
                {
                    "ATOLL link": (row.get("Name") or "").strip(),
                    "Link status": status,
                    "1st Name": (row.get("1st Name") or "").strip(),
                    "2nd Name": (row.get("2nd Name") or "").strip(),
                    "Site A": (row.get("Site A") or "").strip(),
                    "Site B": (row.get("Site B") or "").strip(),
                    "candidates": permit_candidates(row, permit_rows),
                }
            )
        if status in ACTIVE_LINK_STATUSES and matched:
            matched_permit_rows = [
                permit_row
                for permit_row in permit_rows
                if permit_key(permit_row) in matched
            ]
            if not frequencies_match(row, matched_permit_rows):
                incorrect_frequency.append(
                    {
                        "ATOLL link": (row.get("Name") or "").strip(),
                        "Link status": status,
                        "Matched permit name": " / ".join(sorted(matched.values())),
                        "ATOLL Frequency A": (row.get("Frequency A (MHz)") or "").strip(),
                        "ATOLL Frequency B": (row.get("Frequency B (MHz)") or "").strip(),
                        "Permit frequency": matching_frequency_description(matched_permit_rows),
                    }
                )
        if status in RETIRED_LINK_STATUSES:
            for key, name in matched.items():
                if permits[key].get("_flatPermit") == "true":
                    continue
                matched_retired.setdefault(key, []).append(
                    {
                        "ATOLL link": (row.get("Name") or "").strip(),
                        "Link status": status,
                        "Matched permit name": name,
                    }
                )

    permits_on_retired_links = [
        {
            "Tilladelsesnummer": permit["Tilladelsesnummer"],
            "Positionnummer": permit["Positionnummer"],
            "Site A / B": " / ".join(
                row["SiteID"]
                for row in permit_rows
                if permit_key(row) == key
            ),
            **permit_report_details(key, permit_rows),
            "ATOLL link": item["ATOLL link"],
            "Link status": item["Link status"],
            "Matched permit name": item["Matched permit name"],
        }
        for key, items in matched_retired.items()
        for permit in [permits[key]]
        for item in items
    ]
    if include_unmatched_permits:
        for key, permit in permits.items():
            if permit.get("_flatPermit") == "true" or key in matched_p2p_permits:
                continue
            permits_on_retired_links.append(
                {
                    "Tilladelsesnummer": permit["Tilladelsesnummer"],
                    "Positionnummer": permit["Positionnummer"],
                    "Site A / B": " / ".join(
                        row["SiteID"] for row in permit_rows if permit_key(row) == key
                    ),
                    **permit_report_details(key, permit_rows),
                    "ATOLL link": "",
                    "Link status": "Ingen ATOLL-match",
                    "Matched permit name": "",
                }
            )
    return {
        "missingActive": missing_active,
        "incorrectFrequency": incorrect_frequency,
        "permitsOnRetiredLinks": permits_on_retired_links,
    }


class PermitHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_POST(self) -> None:
        if self.path not in {"/api/parse", "/api/permits", "/api/compare"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if not 0 < content_length <= MAX_UPLOAD_BYTES:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Vælg en PDF på højst 25 MB."},
            )
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        try:
            if self.path == "/api/permits":
                uploads = form["file"] if "file" in form else []
                uploads = uploads if isinstance(uploads, list) else [uploads]
                if not uploads or not all(getattr(upload, "file", None) for upload in uploads):
                    raise ValueError("Der blev ikke modtaget en PDF-fil.")
                saved = [
                    save_permit(upload.file.read(), upload.filename or "ukendt.pdf")
                    for upload in uploads
                ]
                _, documents = stored_permits()
                self.send_json(HTTPStatus.OK, {"saved": saved, "documents": documents})
                return

            if self.path == "/api/compare":
                atoll_upload = form["atoll"] if "atoll" in form else None
                if atoll_upload is None or not getattr(atoll_upload, "file", None):
                    raise ValueError("Der blev ikke modtaget en ATOLL-eksport.")
                rows, documents = stored_permits()
                if not rows:
                    raise ValueError("Upload mindst én tilladelses-PDF, før ATOLL-eksporten kontrolleres.")
                comparison = compare_permits(rows, parse_atoll_export(atoll_upload.file.read()))
                self.send_json(HTTPStatus.OK, {"rows": rows, "documents": documents, **comparison})
                return

            uploaded = form["file"] if "file" in form else None
            if uploaded is None or not getattr(uploaded, "file", None):
                raise ValueError("Der blev ikke modtaget en PDF-fil.")
            rows = parse_permit(uploaded.file.read())
        except ValueError as error:
            self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return

        self.send_json(HTTPStatus.OK, {"rows": rows})

    def do_GET(self) -> None:
        if self.path == "/api/compare/atoll":
            try:
                rows, documents = stored_permits()
                if not rows:
                    raise ValueError("Upload mindst én tilladelses-PDF, før ATOLL API kontrolleres.")
                atoll_rows = fetch_atoll_api_rows()
                comparison = compare_permits(rows, atoll_rows, include_unmatched_permits=False)
            except ValueError as error:
                self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
                return
            self.send_json(
                HTTPStatus.OK,
                {
                    "rows": rows,
                    "documents": documents,
                    "atollLinks": len(atoll_rows),
                    **comparison,
                },
            )
            return
        if self.path == "/api/permits":
            try:
                _, documents = stored_permits()
            except ValueError as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
                return
            self.send_json(HTTPStatus.OK, {"documents": documents})
            return
        permit_pdf_match = re.fullmatch(r"/api/permits/([A-Z]\d+)/pdf", unquote(self.path))
        if permit_pdf_match:
            permit_number = permit_pdf_match.group(1)
            pdf_path = PERMIT_STORAGE_DIR / f"{permit_number}.pdf"
            if not pdf_path.is_file():
                self.send_json(HTTPStatus.NOT_FOUND, {"error": f"PDF for tilladelse {permit_number} blev ikke fundet."})
                return
            content = pdf_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{permit_number}.pdf"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if self.path == "/download/atoll-config":
            config_path = Path(__file__).parent / "sample_files" / "export_config.cfg"
            if not config_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "ATOLL-konfigurationen blev ikke fundet.")
                return
            content = config_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="export_config.cfg"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        super().do_GET()

    def do_DELETE(self) -> None:
        prefix = "/api/permits/"
        if not self.path.startswith(prefix):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        permit_number = unquote(self.path.removeprefix(prefix))
        if not re.fullmatch(r"[A-Z]\d+", permit_number):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Ugyldigt tilladelsesnummer."})
            return

        pdf_path = PERMIT_STORAGE_DIR / f"{permit_number}.pdf"
        json_path = PERMIT_STORAGE_DIR / f"{permit_number}.json"
        if not pdf_path.is_file() and not json_path.is_file():
            self.send_json(HTTPStatus.NOT_FOUND, {"error": f"Tilladelse {permit_number} blev ikke fundet."})
            return
        try:
            if pdf_path.is_file():
                pdf_path.unlink()
            if json_path.is_file():
                json_path.unlink()
            _, documents = stored_permits()
        except OSError as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Tilladelse {permit_number} kunne ikke slettes: {error}."})
            return
        self.send_json(HTTPStatus.OK, {"documents": documents})

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    print(f"Åbn http://{HOST}:{PORT} i din browser")
    ThreadingHTTPServer((HOST, PORT), PermitHandler).serve_forever()
