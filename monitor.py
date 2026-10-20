"""Monitor charted train vacancies using ordered station configuration."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


LOGGER = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
API_URL = "https://www.irctc.co.in/online-charts/api/trainComposition"
CHART_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "PostmanRuntime/7.56.1",
    "Origin": "https://chartvacancy.net",
    "Referer": "https://chartvacancy.net/",
    "Connection": "keep-alive",
}
CURL_STATUS_MARKER = "\n__CHART_HTTP_STATUS__:"
CURL_TIMEOUT_EXIT_CODE = 28
# Charts are usually prepared about 10 hours before departure, so by 14:00 IST
# today's trains are charted and later scans target tomorrow's trains.
NEXT_DAY_CUTOFF_HOUR = 14
ROOT = Path(__file__).resolve().parent


class MonitorError(Exception):
    """An actionable configuration, API, or persistence failure."""


def load_env_file(path: Path) -> None:
    """Load simple KEY=value settings without expansion or shell execution."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    except (OSError, UnicodeError):
        raise MonitorError(f"Cannot read environment file {path}.") from None
    values = {}
    for line_number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise MonitorError(f"Invalid environment setting at line {line_number} in {path}.")
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0 or (
                value[closing + 1:].strip()
                and not value[closing + 1:].strip().startswith("#")
            ):
                raise MonitorError(f"Invalid quoted value at line {line_number} in {path}.")
            value = value[1:closing]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value:
            raise MonitorError(f"Invalid environment value at line {line_number} in {path}.")
        values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


class Direction(str, Enum):
    RJY_TO_SC = "RJY -> SC"
    SC_TO_RJY = "SC -> RJY"

    @property
    def origin(self) -> str:
        return self.value.split(" -> ")[0]

    @property
    def destination(self) -> str:
        return self.value.split(" -> ")[1]


@dataclass(frozen=True)
class Station:
    code: str
    relative_day_delta: int
    name: str


@dataclass(frozen=True)
class Train:
    number: str
    stations: tuple[Station, ...]
    name: str
    direction: Direction | None = None


@dataclass(frozen=True)
class Availability:
    chart_one_date: str | None
    vacancies: dict[str, int]


@dataclass(frozen=True)
class StationVacancy:
    station: str
    station_name: str
    arrival_date: date
    chart_one_date: str
    vacancies: dict[str, int]


@dataclass(frozen=True)
class Alert:
    train_number: str
    start_date: date
    station: str
    arrival_date: date
    chart_one_date: str
    vacancies: dict[str, int]
    train_name: str
    station_name: str
    direction: Direction | None = None
    station_vacancies: tuple[StationVacancy, ...] = ()

    @property
    def stops(self) -> tuple[StationVacancy, ...]:
        """Every station with vacancies, in route order (at least the primary one)."""
        return self.station_vacancies or (StationVacancy(
            self.station, self.station_name, self.arrival_date,
            self.chart_one_date, self.vacancies,
        ),)

    @property
    def boarding_description(self) -> str:
        text = ", ".join(f"{stop.station_name} ({stop.station})" for stop in self.stops)
        if self.direction is not None:
            text += f", towards {self.direction.destination} ({self.direction.value})"
        return text


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitorError(f"Cannot read JSON from {path}: {exc}") from exc


def load_trains(path: Path) -> tuple[Train, ...]:
    raw = read_json(path)
    if not isinstance(raw, list) or not raw:
        raise MonitorError(f"{path} must contain a non-empty list of trains.")
    trains = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise MonitorError("Each train must be an object.")
        number = item.get("trainNo")
        if not isinstance(number, str) or not re.fullmatch(r"[0-9]{5}", number):
            raise MonitorError("trainNo must be a five-digit string.")
        if number in seen:
            raise MonitorError(f"Duplicate trainNo: {number}")
        seen.add(number)
        name = item.get("trainName")
        if not isinstance(name, str) or not name.strip():
            raise MonitorError(f"Train {number} requires a non-empty trainName.")
        raw_stations = item.get("stations")
        if not isinstance(raw_stations, list) or not raw_stations:
            raise MonitorError(f"Train {number} requires a non-empty stations list.")
        stations = []
        codes = set()
        previous_delta = 0
        for entry in raw_stations:
            if not isinstance(entry, dict):
                raise MonitorError(f"Train {number}: each station must be an object.")
            code = entry.get("stationCode")
            station_name = entry.get("stationName")
            delta = entry.get("relativeDayDelta")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z]+", code):
                raise MonitorError(f"Train {number}: invalid stationCode {code!r}.")
            if code in codes:
                raise MonitorError(f"Train {number}: duplicate station {code}.")
            if not isinstance(station_name, str) or not station_name.strip():
                raise MonitorError(
                    f"Train {number} station {code} requires a non-empty stationName."
                )
            if type(delta) is not int or delta < previous_delta:
                raise MonitorError(
                    f"Train {number}: day deltas must be non-negative integers "
                    "in non-decreasing route order."
                )
            codes.add(code)
            previous_delta = delta
            stations.append(Station(code, delta, station_name.strip()))
        raw_direction = item.get("direction")
        try:
            direction = Direction(raw_direction)
        except ValueError:
            allowed = ", ".join(repr(member.value) for member in Direction)
            raise MonitorError(
                f"Train {number}: direction must be one of {allowed}."
            ) from None
        if direction.origin in codes and direction.destination in codes:
            route = [station.code for station in stations]
            if route.index(direction.origin) > route.index(direction.destination):
                raise MonitorError(
                    f"Train {number}: direction {direction.value!r} contradicts the "
                    "station order in its route."
                )
        trains.append(Train(number, tuple(stations), name.strip(), direction))
    return tuple(trains)


def parse_availability(payload: object) -> Availability:
    if not isinstance(payload, dict):
        raise MonitorError("API response must be an object.")
    error = payload.get("error")
    if error is not None:
        # Unprepared charts come back as HTTP 200 with this error text and a null chartOneDate.
        if (
            isinstance(error, str)
            and error.strip().lower() == "chart not prepared"
            and payload.get("chartOneDate") is None
        ):
            return Availability(None, {})
        raise MonitorError(f"API reported an error: {error!r}")
    status = payload.get("chartStatusResponseDto")
    if isinstance(status, dict) and status.get("messageType") not in (None, "S"):
        raise MonitorError(f"API reported unsuccessful chart status: {status!r}")
    if "chartOneDate" not in payload:
        raise MonitorError("API response is missing chartOneDate.")
    chart_date = payload["chartOneDate"]
    if chart_date is None:
        return Availability(None, {})
    if not isinstance(chart_date, str) or not chart_date.strip():
        raise MonitorError("API returned an invalid chartOneDate.")
    coaches = payload.get("cdd")
    if not isinstance(coaches, list):
        raise MonitorError("Prepared chart response is missing the cdd list.")
    vacancies: dict[str, int] = {}
    for coach in coaches:
        if not isinstance(coach, dict):
            raise MonitorError("API returned an invalid coach entry.")
        class_code = coach.get("classCode")
        count = coach.get("vacantBerths")
        if not isinstance(class_code, str) or not class_code.strip():
            raise MonitorError("API returned an invalid classCode.")
        if type(count) is not int or count < 0:
            raise MonitorError("API returned an invalid vacantBerths count.")
        if count:
            vacancies[class_code] = vacancies.get(class_code, 0) + count
    return Availability(chart_date, vacancies)


class ChartClient:
    def __init__(self, timeout: float = 20, request_delay: float = 1) -> None:
        self.timeout = timeout
        self.request_delay = request_delay
        self.last_request: float | None = None

    @staticmethod
    def log_response(train: str, arrival_date: date, station: str, body: bytes) -> None:
        LOGGER.info(
            "Chart API response train=%s station=%s jDate=%s body=%s",
            train, station, arrival_date.isoformat(),
            json.dumps(body.decode("utf-8", errors="replace")),
        )

    def fetch(self, train: str, arrival_date: date, station: str) -> Availability:
        if self.last_request is not None:
            remaining = self.request_delay - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
        body = json.dumps(
            {
                "trainNo": train,
                "jDate": arrival_date.isoformat(),
                "boardingStation": station,
            }
        ).encode("utf-8")
        executable = shutil.which("curl.exe") or shutil.which("curl")
        if executable is None:
            raise MonitorError(
                "curl executable not found on PATH; it is required for chart requests."
            )
        curl = ["curl", "--http1.1", "--request", "POST", "--url", API_URL]
        for name, value in CHART_HEADERS.items():
            curl.extend(["--header", f"{name}: {value}"])
        
        # Python's OpenSSL 3.5+ TLS handshake is silently stalled by the API's edge
        # protection, so the request goes through the curl executable instead.
        command = [
            executable, *curl[1:], "--silent", "--show-error",
            "--max-time", f"{self.timeout:g}", "--data-binary", "@-",
            "--write-out", CURL_STATUS_MARKER + "%{http_code}",
        ]
        self.last_request = time.monotonic()
        started = time.perf_counter()
        status: int | None = None
        body_received = False
        try:
            try:
                completed = subprocess.run(
                    command, input=body, capture_output=True, timeout=self.timeout + 5,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("curl did not exit before the timeout") from exc
            output, marker, status_text = completed.stdout.decode(
                "utf-8", errors="replace",
            ).rpartition(CURL_STATUS_MARKER)
            if marker and status_text.strip().isdigit() and int(status_text) > 0:
                status = int(status_text)
                
            if completed.returncode != 0:
                error = completed.stderr.decode("utf-8", errors="replace").strip()
                if completed.returncode == CURL_TIMEOUT_EXIT_CODE:
                    raise TimeoutError(f"The read operation timed out ({error})")
                raise OSError(f"curl exited with code {completed.returncode}: {error}")
            if status is None:
                raise OSError("curl did not report an HTTP status")
            response_body = output.encode("utf-8")
            body_received = True
            if not 200 <= status < 300:
                raise MonitorError(
                    f"Chart API returned HTTP {status}; check service access/rate limits."
                )
            payload = json.loads(output)
        except (TimeoutError, OSError, ValueError) as exc:
            LOGGER.error(
                "Chart API failure train=%s station=%s jDate=%s HTTP=%s "
                "complete_body_received=%s elapsed=%.3fs error=%s",
                train, station, arrival_date.isoformat(),
                status if status is not None else "unavailable (no response headers)",
                body_received, time.perf_counter() - started, exc,
            )
            raise MonitorError(f"Chart API request failed: {exc}") from exc
        return parse_availability(payload)

    def warm_up(self, train: str, arrival_date: date, station: str) -> bool:
        """Send one discarded request; the API tends to stall on the first call after idling."""
        LOGGER.info("Warm-up request: train=%s station=%s jDate=%s", train, station, arrival_date.isoformat())
        try:
            self.fetch(train, arrival_date, station)
        except MonitorError as exc:
            LOGGER.warning("Warm-up request failed (scan continues): %s", exc)
            return False
        LOGGER.info("Warm-up request succeeded.")
        return True


class FileState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.notified: set[str] = set()
        self.accepted_messages: dict[str, dict[str, str]] = {}
        self.scanned: set[str] = set()
        if path.exists():
            raw = read_json(path)
            if not isinstance(raw, dict) or raw.get("version") not in (1, 2, 3):
                raise MonitorError(f"Invalid state format in {path}.")
            keys = raw.get("notifiedJourneys")
            if not isinstance(keys, list) or not all(
                isinstance(key, str) and self.valid_key(key) for key in keys
            ):
                raise MonitorError(f"Invalid notifiedJourneys in {path}.")
            self.notified = set(keys)
            if raw["version"] == 3:
                scanned = raw.get("scannedJourneys")
                if not isinstance(scanned, list) or not all(
                    isinstance(key, str) and self.valid_key(key) for key in scanned
                ):
                    raise MonitorError(f"Invalid scannedJourneys in {path}.")
                self.scanned = set(scanned)
            if raw["version"] >= 2:
                messages = raw.get("acceptedMessages")
                if not isinstance(messages, dict):
                    raise MonitorError(f"Invalid acceptedMessages in {path}.")
                for journey, receipts in messages.items():
                    if (
                        not self.valid_key(journey)
                        or not isinstance(receipts, dict)
                        or not receipts
                        or not all(
                            self.valid_recipient(recipient)
                            and isinstance(message_id, str)
                            and message_id.strip()
                            for recipient, message_id in receipts.items()
                        )
                    ):
                        raise MonitorError(f"Invalid notification receipt in {path}.")
                self.accepted_messages = messages

    @staticmethod
    def valid_recipient(recipient: str) -> bool:
        return bool(re.fullmatch(
            r"(?:[1-9][0-9]{6,14}|telegram:[1-9][0-9]*:[1-9][0-9]*)", recipient
        ))

    @staticmethod
    def valid_key(key: str) -> bool:
        if not re.fullmatch(r"[0-9]{5}:\d{4}-\d{2}-\d{2}", key):
            return False
        try:
            date.fromisoformat(key.split(":")[1])
        except ValueError:
            return False
        return True

    def contains(self, train: str, start_date: date, recipient: str = "console") -> bool:
        key = f"{train}:{start_date.isoformat()}"
        if recipient == "console":
            return key in self.notified
        return recipient in self.accepted_messages.get(key, {})

    def mark_notified(
        self,
        train: str,
        start_date: date,
        recipient: str = "console",
        message_id: str | None = None,
    ) -> None:
        key = f"{train}:{start_date.isoformat()}"
        updated = set(self.notified)
        messages = {journey: dict(receipts) for journey, receipts in self.accepted_messages.items()}
        if recipient == "console":
            updated.add(key)
        else:
            if not self.valid_recipient(recipient) or not message_id:
                raise MonitorError("Cannot save notification receipt without recipient/message ID.")
            messages.setdefault(key, {})[recipient] = message_id
        self.save(updated, messages, self.scanned)

    def is_scanned(self, train: str, start_date: date) -> bool:
        return f"{train}:{start_date.isoformat()}" in self.scanned

    def mark_scanned(self, train: str, start_date: date) -> None:
        """Record a journey whose prepared charts were checked at every station."""
        self.save(self.notified, self.accepted_messages, self.scanned | {
            f"{train}:{start_date.isoformat()}"
        })

    def save(
        self,
        notified: set[str],
        messages: dict[str, dict[str, str]],
        scanned: set[str],
    ) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    {
                        "version": 3,
                        "notifiedJourneys": sorted(notified),
                        "acceptedMessages": messages,
                        "scannedJourneys": sorted(scanned),
                    },
                    handle,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise MonitorError(f"Cannot save notification state to {self.path}: {exc}") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        self.notified = set(notified)
        self.accepted_messages = messages
        self.scanned = set(scanned)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


TRAVEL_CLASS_NAMES = {
    "1A": "First AC",
    "EA": "Executive Anubhuti",
    "EC": "Executive Chair",
    "2A": "AC 2 Tier",
    "FC": "First Class",
    "3A": "AC 3 Tier",
    "3E": "AC 3 Economy",
    "CC": "AC Chair Car",
    "SL": "Sleeper",
    "2S": "Second Sitting",
}


def format_telegram_alert(alert: Alert) -> str:
    """Render one Telegram HTML card per train; every dynamic value is escaped.

    All stations with vacancies sit in a single collapsed (expandable) quote;
    tapping it in Telegram reveals every station and its class-wise berths.
    """
    def esc(value: object) -> str:
        return html.escape(str(value), quote=False)

    try:
        chart_time = datetime.strptime(alert.chart_one_date, "%Y-%m-%d %H:%M:%S")
        chart_text = chart_time.strftime("%a, %d %b · %I:%M %p")
    except ValueError:
        chart_text = alert.chart_one_date
    stops = alert.stops

    subtitle = f"<code>{esc(alert.train_number)}</code>"
    if alert.direction is not None:
        subtitle += (
            f"  •  <b>{esc(alert.direction.origin)}</b> ➜ "
            f"<b>{esc(alert.direction.destination)}</b>"
        )
    lines = [
        f"🚆 <b>{esc(alert.train_name)}</b>",
        subtitle,
        "",
        "🕒 <b>Chart prepared</b>",
        f"<i>{esc(chart_text)}</i>",
        "",
    ]
    sections = []
    for stop in stops:
        count = sum(stop.vacancies.values())
        berths = "\n".join(
            f"   ▸ {esc(TRAVEL_CLASS_NAMES.get(code, code))} <code>{esc(code)}</code>"
            f"  —  <b>{number}</b>"
            for code, number in sorted(stop.vacancies.items())
        )
        sections.append(
            f"📍 <b>{esc(stop.station_name)}</b> <code>{esc(stop.station)}</code>"
            f"  ·  {count} berth{'s' if count != 1 else ''}\n{berths}"
        )
    header = (
        f"🚉 <b>{len(stops)} station{'s' if len(stops) != 1 else ''} with vacancies</b>"
        "  ·  tap to expand"
    )
    lines.append(
        f"<blockquote expandable>{header}\n\n" + "\n\n".join(sections) + "</blockquote>"
    )
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(
        self, token: str, chat_ids: tuple[str, ...], timeout: float = 20,
    ) -> None:
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]+", token):
            raise MonitorError("TELEGRAM_BOT_TOKEN must be a valid BotFather token.")
        if (
            len(chat_ids) > 5 or len(set(chat_ids)) != len(chat_ids)
            or not all(re.fullmatch(r"[1-9][0-9]*", chat_id) for chat_id in chat_ids)
        ):
            raise MonitorError("TELEGRAM_CHAT_IDS must contain 1-5 unique positive private chat IDs.")
        self.token = token
        self.timeout = timeout
        self.chat_ids = chat_ids
        self.recipients = tuple(
            f"telegram:{token.split(':')[0]}:{chat_id}" for chat_id in chat_ids
        )
        self.opener = build_opener(NoRedirect())

    @classmethod
    def from_environment(
        cls, timeout: float = 20, discovery: bool = False,
    ) -> TelegramNotifier:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if discovery:
            return cls(token, (), timeout)
        value = os.environ.get("TELEGRAM_CHAT_IDS", "")
        if not value.strip():
            raise MonitorError("Set TELEGRAM_CHAT_IDS after recipients start the bot.")
        return cls(token, tuple(part.strip() for part in value.split(",")), timeout)

    def call(self, method: str, payload: dict[str, object]) -> object:
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            code = exc.code
            exc.close()
            hints = {
                401: "Check the BotFather token.",
                403: "Recipient must start/unblock the bot.",
                409: "getUpdates conflicts with another poller or an existing webhook.",
                429: "Rate limited; wait before retrying on a later cycle.",
            }
            raise MonitorError(
                f"Telegram HTTP {code}. "
                + hints.get(code, "Check chat IDs, bot access, and Telegram service status.")
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            # Telegram embeds the secret token in the URL; never log the exception.
            raise MonitorError(
                "Telegram request failed or returned invalid JSON; acceptance is unknown. "
                "A later retry may duplicate a message."
            ) from None
        if not isinstance(result, dict) or result.get("ok") is not True or "result" not in result:
            raise MonitorError("Telegram did not confirm success; notification state was not updated.")
        return result["result"]

    def discover_chat_ids(self) -> tuple[str, ...]:
        updates = self.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        if not isinstance(updates, list):
            raise MonitorError("Telegram returned invalid updates.")
        ids = set()
        for update in updates:
            if not isinstance(update, dict):
                raise MonitorError("Telegram returned an invalid update entry.")
            message = update.get("message")
            chat = message.get("chat") if isinstance(message, dict) else None
            if isinstance(chat, dict) and chat.get("type") == "private":
                chat_id = chat.get("id")
                if type(chat_id) is not int or chat_id <= 0:
                    raise MonitorError("Telegram returned an invalid private chat ID.")
                ids.add(str(chat_id))
        return tuple(sorted(ids, key=int))

    def send(self, alert: Alert, recipient: str) -> str:
        if recipient not in self.recipients:
            raise MonitorError("Telegram recipient is not configured.")
        chat_id = recipient.rsplit(":", 1)[1]
        text = format_telegram_alert(alert)
        if len(text) > 4096:
            raise MonitorError("Telegram alert exceeds the 4096-character message limit.")
        result = self.call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })
        if (
            not isinstance(result, dict)
            or type(result.get("message_id")) is not int
            or result["message_id"] <= 0
            or not isinstance(result.get("chat"), dict)
            or type(result["chat"].get("id")) is not int
            or str(result["chat"]["id"]) != chat_id
        ):
            raise MonitorError("Telegram returned an invalid message receipt; state was not updated.")
        LOGGER.info("Telegram accepted alert for chat ending %s.", chat_id[-4:])
        return str(result["message_id"])


class WhatsAppNotifier:
    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_version: str,
        template_name: str,
        language: str,
        recipients: tuple[str, ...],
        timeout: float = 20,
    ) -> None:
        if not token.strip() or any(character.isspace() for character in token):
            raise MonitorError("WHATSAPP_ACCESS_TOKEN must be a non-empty token without whitespace.")
        if not re.fullmatch(r"[0-9]+", phone_number_id):
            raise MonitorError("WHATSAPP_PHONE_NUMBER_ID must be Meta's numeric sender ID.")
        if not re.fullmatch(r"v[1-9][0-9]*\.0", api_version):
            raise MonitorError("WHATSAPP_API_VERSION must have a version such as v23.0.")
        if not re.fullmatch(r"[a-z0-9_]{1,512}", template_name):
            raise MonitorError("WHATSAPP_TEMPLATE_NAME must use lowercase letters, digits, underscores.")
        if not re.fullmatch(r"[a-z]{2,3}(?:_[A-Z]{2})?", language):
            raise MonitorError("WHATSAPP_TEMPLATE_LANGUAGE must match the approved language code.")
        if (
            not 1 <= len(recipients) <= 5
            or len(set(recipients)) != len(recipients)
            or not all(re.fullmatch(r"[1-9][0-9]{6,14}", number) for number in recipients)
        ):
            raise MonitorError(
                "WHATSAPP_RECIPIENTS requires 1-5 unique international numbers "
                "(country code and digits only, without '+')."
            )
        self.token = token
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self.template_name = template_name
        self.language = language
        self.recipients = recipients
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    @classmethod
    def from_environment(cls, timeout: float = 20) -> WhatsAppNotifier:
        required = (
            "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_API_VERSION", "WHATSAPP_TEMPLATE_NAME",
            "WHATSAPP_TEMPLATE_LANGUAGE", "WHATSAPP_RECIPIENTS",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise MonitorError("Missing WhatsApp configuration: " + ", ".join(missing))
        if os.environ.get("WHATSAPP_RECIPIENTS_OPTED_IN") != "true":
            raise MonitorError(
                "Obtain consent from every recipient, then set WHATSAPP_RECIPIENTS_OPTED_IN=true."
            )
        return cls(
            os.environ["WHATSAPP_ACCESS_TOKEN"],
            os.environ["WHATSAPP_PHONE_NUMBER_ID"],
            os.environ["WHATSAPP_API_VERSION"],
            os.environ["WHATSAPP_TEMPLATE_NAME"],
            os.environ["WHATSAPP_TEMPLATE_LANGUAGE"],
            tuple(number.strip() for number in os.environ["WHATSAPP_RECIPIENTS"].split(",")),
            timeout,
        )

    def send(self, alert: Alert, recipient: str) -> str:
        if recipient not in self.recipients:
            raise MonitorError("WhatsApp recipient is not in the configured opt-in list.")
        values = [
            alert.train_name,
            alert.train_number,
            alert.boarding_description,
            alert.start_date.isoformat(),
            ", ".join(dict.fromkeys(stop.arrival_date.isoformat() for stop in alert.stops)),
            alert.chart_one_date,
            " | ".join(
                (f"{stop.station} - " if len(alert.stops) > 1 else "")
                + ", ".join(f"{code}: {count}" for code, count in sorted(stop.vacancies.items()))
                for stop in alert.stops
            ),
        ]
        if any(
            not value.strip() or len(value) > 1024 or any(c in value for c in "\r\n\t")
            for value in values
        ):
            raise MonitorError("WhatsApp template values must be non-empty, single-line text.")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in values],
                }],
            },
        }
        request = Request(
            f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            error_code = None
            try:
                details = json.loads(exc.read().decode("utf-8"))
                if isinstance(details, dict) and isinstance(details.get("error"), dict):
                    candidate = details["error"].get("code")
                    if type(candidate) is int:
                        error_code = candidate
            except (OSError, ValueError):
                LOGGER.warning("Meta returned an unreadable HTTP error body (HTTP %s).", exc.code)
            finally:
                exc.close()
            hints = {
                190: "Check token validity and permissions.",
                131030: "Add the recipient to the allowed test-recipient list.",
                132001: "Check template approval, name, and language.",
                132000: "Check that the approved template has seven positional body parameters.",
            }
            raise MonitorError(
                f"Meta HTTP {exc.code}, error code {error_code}. "
                + hints.get(error_code, "Check Meta sender setup, billing, template, and rate limits.")
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise MonitorError(
                "Meta request failed or returned invalid JSON; acceptance is unknown. "
                "Check connectivity before retrying to avoid duplicate alerts."
            ) from None
        messages = result.get("messages") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or "error" in result
            or result.get("messaging_product") != "whatsapp"
            or not isinstance(messages, list)
            or len(messages) != 1
            or not isinstance(messages[0], dict)
            or not isinstance(messages[0].get("id"), str)
            or not messages[0]["id"].strip()
            or messages[0].get("message_status") not in (None, "accepted", "held_for_quality_assessment")
        ):
            raise MonitorError("Meta did not return a valid accepted message ID; state was not updated.")
        LOGGER.info("Meta accepted alert for recipient ending %s; delivery is not yet confirmed.", recipient[-4:])
        return messages[0]["id"]


def console_notify(alert: Alert, recipient: str = "console") -> None:
    print(
        json.dumps(
            {
                "type": "vacancy_found",
                "trainNo": alert.train_number,
                "trainName": alert.train_name,
                "trainStartDate": alert.start_date.isoformat(),
                "boardingStation": alert.station,
                "boardingStationName": alert.station_name,
                "direction": alert.direction.value if alert.direction else None,
                "towardsStation": alert.direction.destination if alert.direction else None,
                "jDate": alert.arrival_date.isoformat(),
                "chartOneDate": alert.chart_one_date,
                "vacanciesByClass": alert.vacancies,
                "stations": [
                    {
                        "boardingStation": stop.station,
                        "boardingStationName": stop.station_name,
                        "jDate": stop.arrival_date.isoformat(),
                        "chartOneDate": stop.chart_one_date,
                        "vacanciesByClass": stop.vacancies,
                    }
                    for stop in alert.stops
                ],
                "notice": "Chart vacancies are not a guarantee of bookable tickets.",
            }
        ),
        flush=True,
    )


def poll_once(
    trains: tuple[Train, ...],
    start_date: date,
    state: FileState,
    fetch: Callable[[str, date, str], Availability],
    notify: Callable[[Alert, str], str | None],
    recipients: tuple[str, ...] = ("console",),
) -> bool:
    """Return false if a check or notification failed; state failures are fatal."""
    LOGGER.info(
        "Starting scan: train start date=%s (IST), trains=%s. "
        "Each station's jDate includes its configured day offset.",
        start_date.isoformat(), len(trains),
    )
    succeeded = True
    for train in trains:
        if all(state.contains(train.number, start_date, recipient) for recipient in recipients):
            LOGGER.info("Skipping notified journey %s:%s", train.number, start_date)
            continue
        if state.is_scanned(train.number, start_date):
            LOGGER.info(
                "Skipping fully scanned journey %s:%s (charts prepared, no vacancies found).",
                train.number, start_date,
            )
            continue
        fully_checked = True
        found: list[StationVacancy] = []
        for station in train.stations:
            arrival_date = start_date + timedelta(days=station.relative_day_delta)
            try:
                availability = fetch(train.number, arrival_date, station.code)
            except MonitorError as exc:
                LOGGER.error("Train %s station %s: %s", train.number, station.code, exc)
                succeeded = False
                fully_checked = False
                continue
            if availability.chart_one_date is None:
                LOGGER.info(
                    "Chart not prepared: %s (%s) at %s (jDate %s)",
                    train.number, train.name, station.code, arrival_date,
                )
                fully_checked = False
                break
            if not availability.vacancies:
                LOGGER.info("No vacancies: %s at %s", train.number, station.code)
                continue
            LOGGER.info(
                "Vacancies: %s at %s: %s", train.number, station.code, availability.vacancies,
            )
            found.append(StationVacancy(
                station.code, station.name, arrival_date,
                availability.chart_one_date, availability.vacancies,
            ))
        if found:
            first = found[0]
            alert = Alert(
                train.number,
                start_date,
                first.station,
                first.arrival_date,
                first.chart_one_date,
                first.vacancies,
                train.name,
                first.station_name,
                train.direction,
                tuple(found),
            )
            for recipient in recipients:
                if state.contains(train.number, start_date, recipient):
                    continue
                try:
                    message_id = notify(alert, recipient)
                except (MonitorError, OSError) as exc:
                    LOGGER.error(
                        "Notification failed for %s (recipient ending %s): %s",
                        train.number, recipient[-4:], exc,
                    )
                    succeeded = False
                    continue
                state.mark_notified(train.number, start_date, recipient, message_id)
        elif fully_checked and train.stations:
            state.mark_scanned(train.number, start_date)
            LOGGER.info(
                "Marked journey %s:%s as fully scanned; it will be skipped in later cycles.",
                train.number, start_date,
            )
    return succeeded


def default_start_date(now: datetime) -> date:
    """Today's IST date before the cutoff hour, tomorrow's from then until midnight."""
    now = now.astimezone(IST)
    if now.hour < NEXT_DAY_CUTOFF_HOUR:
        return now.date()
    return now.date() + timedelta(days=1)


def positive_number(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("Value must be a finite positive number.")
    return number


def train_numbers(value: str) -> frozenset[str]:
    numbers = [number.strip() for number in value.split(",")]
    if not all(re.fullmatch(r"[0-9]{5}", number) for number in numbers):
        raise argparse.ArgumentTypeError(
            "Provide comma-separated five-digit train numbers, without empty entries."
        )
    return frozenset(numbers)


def iso_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("Provide the date as YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid calendar date: {value}.") from None


NOTIFICATION_CHOICES = ("console", "whatsapp", "telegram")


def boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("Use true/false, yes/no, on/off or 1/0.")


def notification_choice(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in NOTIFICATION_CHOICES:
        raise argparse.ArgumentTypeError("Choose one of: " + ", ".join(NOTIFICATION_CHOICES) + ".")
    return normalized


KEEP_AWAKE_CHOICES = ("display", "system", "off")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake_choice(value: str) -> str:
    choice = value.strip().lower()
    if choice not in KEEP_AWAKE_CHOICES:
        raise argparse.ArgumentTypeError(
            f"expected one of {', '.join(KEEP_AWAKE_CHOICES)}, got {value!r}"
        )
    return choice


def keep_awake(mode: str) -> bool:
    """Hold or release a Windows power request for this thread; no-op elsewhere.

    On Modern Standby laptops the idle display-off timeout puts the machine into
    standby and pauses desktop programs, so 'display' is needed to keep scanning.
    """
    if os.name != "nt":
        if mode != "off":
            LOGGER.warning("--keep-awake is only supported on Windows; ignoring.")
        return False
    flags = ES_CONTINUOUS
    if mode in ("system", "display"):
        flags |= ES_SYSTEM_REQUIRED
    if mode == "display":
        flags |= ES_DISPLAY_REQUIRED
    try:
        import ctypes
        previous = ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except (AttributeError, OSError):
        previous = 0
    if not previous:
        LOGGER.warning("Windows rejected the keep-awake request (mode=%s).", mode)
        return False
    if mode != "off":
        LOGGER.info(
            "Keep-awake enabled (mode=%s): Windows will not idle-sleep%s while the monitor runs.",
            mode, " or turn off the display" if mode == "display" else "",
        )
    return True


# Setting name -> (argparse destination, converter). Command-line flags override these.
ENV_SETTINGS: dict[str, tuple[str, Callable[[str], object]]] = {
    "MONITOR_TRAINS": ("trains", Path),
    "MONITOR_TRAIN_NOS": ("train_nos", train_numbers),
    "MONITOR_STATE": ("state", Path),
    "MONITOR_START_DATE": ("start_date", iso_date),
    "MONITOR_INTERVAL": ("interval", positive_number),
    "MONITOR_TIMEOUT": ("timeout", positive_number),
    "MONITOR_REQUEST_DELAY": ("request_delay", positive_number),
    "MONITOR_DEBUG": ("debug", boolean),
    "MONITOR_NOTIFICATION": ("notification", notification_choice),
    "MONITOR_KEEP_AWAKE": ("keep_awake", keep_awake_choice),
}


def environment_defaults() -> dict[str, object]:
    defaults: dict[str, object] = {}
    for name, (destination, convert) in ENV_SETTINGS.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            defaults[destination] = convert(raw)
        except (argparse.ArgumentTypeError, ValueError) as exc:
            raise MonitorError(f"Invalid {name} setting: {exc}") from None
    return defaults


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Options can also be set in .env or the environment (MONITOR_* settings); "
        "command-line flags take precedence.",
    )
    parser.add_argument("--trains", type=Path, default=ROOT / "trains.json")
    parser.add_argument(
        "--train-nos", type=train_numbers,
        help="Scan only these comma-separated train numbers from the route configuration.",
    )
    parser.add_argument("--state", type=Path, default=ROOT / "state.json")
    parser.add_argument(
        "--start-date", type=iso_date,
        help=(
            "Train start date (YYYY-MM-DD) used for every cycle; omitted means today's IST "
            f"date before {NEXT_DAY_CUTOFF_HOUR}:00 IST and tomorrow's from then until midnight."
        ),
    )
    parser.add_argument(
        "--interval",
        type=positive_number,
        help="Repeat checks every N seconds after each cycle; omitted means one cycle.",
    )
    parser.add_argument("--timeout", type=positive_number, default=20)
    parser.add_argument("--request-delay", type=positive_number, default=1)
    parser.add_argument(
        "--debug", action=argparse.BooleanOptionalAction, default=False,
        help="Enable DEBUG-level logging.",
    )
    parser.add_argument("--notification", choices=NOTIFICATION_CHOICES, default="console")
    parser.add_argument(
        "--keep-awake", type=keep_awake_choice, choices=KEEP_AWAKE_CHOICES, default="display",
        help=(
            "While repeating with --interval, ask Windows to stay awake. 'display' (default) "
            "keeps the screen on, which also stops Modern Standby laptops from pausing the "
            "monitor; 'system' only blocks idle sleep; 'off' changes nothing."
        ),
    )
    parser.add_argument(
        "--telegram-chat-ids", action="store_true",
        help="List private chat IDs from recent bot messages; no IRCTC calls or state changes.",
    )
    parser.add_argument(
        "--test-notification", action="store_true",
        help="Send a clearly labelled synthetic alert without calling IRCTC or modifying state.",
    )
    try:
        load_env_file(ROOT / ".env")
        parser.set_defaults(**environment_defaults())
    except MonitorError as exc:
        parser.error(str(exc))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.telegram_chat_ids:
            telegram = TelegramNotifier.from_environment(args.timeout, discovery=True)
            ids = telegram.discover_chat_ids()
            if not ids:
                raise MonitorError("No private chats found. Open the bot, send /start, and retry.")
            print("TELEGRAM_CHAT_IDS=" + ",".join(ids), flush=True)
            return 0
        notify: Callable[[Alert, str], str | None] = console_notify
        recipients = ("console",)
        if args.notification == "whatsapp":
            notifier = WhatsAppNotifier.from_environment(args.timeout)
            notify = notifier.send
            recipients = notifier.recipients
        elif args.notification == "telegram":
            telegram = TelegramNotifier.from_environment(args.timeout)
            notify = telegram.send
            recipients = telegram.recipients
        if args.test_notification:
            today = datetime.now(IST).date()
            alert = Alert(
                "00000", today, "TEST", today, "TEST - not a real chart",
                {"SL": 1}, "TEST ONLY - no real vacancies", "Test Station",
            )
            succeeded = True
            for recipient in recipients:
                try:
                    notify(alert, recipient)
                except (MonitorError, OSError) as exc:
                    LOGGER.error("Test notification failed (recipient ending %s): %s", recipient[-4:], exc)
                    succeeded = False
            return 0 if succeeded else 1
        trains = load_trains(args.trains)
        if args.train_nos is not None:
            unknown = args.train_nos - {train.number for train in trains}
            if unknown:
                raise MonitorError(
                    "Requested train numbers not found in route configuration: "
                    + ", ".join(sorted(unknown))
                )
            trains = tuple(train for train in trains if train.number in args.train_nos)
            LOGGER.info("Train filter enabled: %s", ", ".join(train.number for train in trains))
        state = FileState(args.state)
        client = ChartClient(args.timeout, args.request_delay)
        if args.interval is not None:
            keep_awake(args.keep_awake)
        while True:
            start_date = args.start_date or default_start_date(datetime.now(IST))
            if trains and trains[0].stations:
                first_station = trains[0].stations[0]
                client.warm_up(
                    trains[0].number,
                    start_date + timedelta(days=first_station.relative_day_delta),
                    first_station.code,
                )
            succeeded = poll_once(trains, start_date, state, client.fetch, notify, recipients)
            LOGGER.info(
                "Scan finished: train start date=%s (IST), result=%s.",
                start_date.isoformat(), "success" if succeeded else "completed with errors",
            )
            if args.interval is None:
                return 0 if succeeded else 1
            LOGGER.info("Next scan starts in %s seconds.", args.interval)
            time.sleep(args.interval)
    except MonitorError as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Monitor stopped.")
        return 0
    finally:
        keep_awake("off")


if __name__ == "__main__":
    raise SystemExit(main())
