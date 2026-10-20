# Train chart vacancy monitor

A Python 3.10+ prototype using only the standard library. It checks stations in
route order, stops on the first prepared chart with vacancies for each train,
prints an alert to the console, sends a Telegram message, or sends a Meta WhatsApp template message,
and persists notification receipts in a JSON file.
No LLM or database is required for this deterministic workflow.

## Configure routes

[trains.json](trains.json) contains a researched snapshot of 49 services:
24 RJY-to-SC trains (20 regular and four listed specials), followed by 25
SC-to-RJY trains (20 regular and five listed specials).
Each entry includes the complete published halt sequence from origin to
terminus, not just the section between RJY and SC. This abbreviated example
illustrates the schema:

```json
[
  {
    "trainNo": "18519",
    "trainName": "Visakhapatnam - Lokmanya Tilak Terminus Express",
    "direction": "RJY -> SC",
    "stations": [
      {"stationCode": "VSKP", "stationName": "Visakhapatnam", "relativeDayDelta": 0},
      {"stationCode": "BZA", "stationName": "Vijayawada Jn", "relativeDayDelta": 1},
      {"stationCode": "LTT", "stationName": "Lokmanya Tilak Terminus", "relativeDayDelta": 2}
    ]
  }
]
```

Day zero is the train's departure date. Station codes must be uppercase, unique
within a train, and listed in route order with non-decreasing day offsets.
Train numbers must be five-digit strings. Every train requires a non-empty
`trainName`, and every station requires a non-empty `stationName`. Names are
display labels; API requests and journey-state keys continue to use numbers
and codes. Existing custom configurations must add these name fields.

Every train also requires `direction`, exactly `"RJY -> SC"` or `"SC -> RJY"`.
When a route contains both RJY and SC, loading fails if the direction
contradicts their order. Alerts show it with the boarding station, for example
`Rajahmundry (RJY), towards SC (RJY -> SC)`, so you can tell which way the
train is heading.

### Route sources and coverage

Researched on 2026-10-19 using the
[eRail station-pair listing](https://erail.in/trains-between-stations/rajamundry-RJY/secunderabad-jn-SC)
for discovery and the linked ConfirmTkt full schedules below for station codes,
halt order, origin/terminus, and day numbers. `relativeDayDelta` is the published
schedule's `Day` minus one, adjusted to the arrival date at midnight-spanning
halts where the source assigns the departure day. Only scheduled halts are included; intermediate
pass-through stations are not. Trains retain their real origin and terminus,
which can be beyond RJY and SC.
Station display names come from each ConfirmTkt schedule's `StationName`;
all-uppercase labels are converted to title case. Train display names expand
service abbreviations into readable names (for example, SF to Superfast and
SPL to Special), preserving named services such as Godavari and Falaknuma.
For the already-documented 07070 source-name conflict, the label describes its
VSKP - SC route rather than copying the unrelated source display name.

| Train | Service | Full schedule | Halts |
|---|---|---|---:|
| 07426 | Srikakulam Road - Secunderabad special | [CHE - SC](https://www.confirmtkt.com/train-schedule/07426-CHE-SC-SPL) | 22 |
| 12703 | Falaknuma Express | [HWH - SC](https://www.confirmtkt.com/train-schedule/12703-FALAKNUMA-EXP) | 26 |
| 11020 | Konark Express | [BBSN - CSMT](https://www.confirmtkt.com/train-schedule/11020-KONARK-EXPRESS) | 45 |
| 18519 | Visakhapatnam - LTT Express | [VSKP - LTT](https://www.confirmtkt.com/train-schedule/18519-VSKP-LTT-EXPRES) | 25 |
| 22849 | Shalimar - Secunderabad SF Express | [SHM - SC](https://www.confirmtkt.com/train-schedule/22849-SHM-SC-SUF-EXP) | 20 |
| 17206 | Kakinada Port - Sainagar Shirdi Express | [COA - SNSI](https://www.confirmtkt.com/train-schedule/17206-COA-SNSI-EXP) | 35 |
| 12755 | Kakinada Port - Bhavnagar SF Express | [COA - BVC](https://www.confirmtkt.com/train-schedule/12755-COA-BVC-SF-EXP) | 32 |
| 20833 | Vande Bharat Express | [VSKP - SC](https://www.confirmtkt.com/train-schedule/20833-VANDE-BHARAT-EXP) | 7 |
| 12773 | Shalimar - Secunderabad SF Express | [SHM - SC](https://www.confirmtkt.com/train-schedule/12773-SHM-SC-SUF-EXP) | 16 |
| 12805 | Janmabhoomi Express | [VSKP - LPI](https://www.confirmtkt.com/train-schedule/12805-JANMABHOOMI-EXP) | 25 |
| 17289 | Kakinada Town - Mysuru Express | [CCT - MYS](https://www.confirmtkt.com/train-schedule/17289-CCT-MYS-EXPRESS) | 34 |
| 17221 | Kakinada Port - LTT Express | [COA - LTT](https://www.confirmtkt.com/train-schedule/17221-COA-LTT-EXPRESS) | 24 |
| 07047 | Naharlagun - Secunderabad special | [NHLN - SC](https://www.confirmtkt.com/train-schedule/07047-NHLN-SC-SPECIAL) | 45 |
| 18111 | Tatanagar - Yesvantpur Express | [TATA - YPR](https://www.confirmtkt.com/train-schedule/18111-TATA-YPR-EXP) | 40 |
| 20708 | Vande Bharat Express | [VSKP - SC](https://www.confirmtkt.com/train-schedule/20708-VANDE-BHARAT-EXP) | 8 |
| 07070 | Visakhapatnam - Secunderabad special | [VSKP - SC](https://www.confirmtkt.com/train-schedule/07070-VSKP-SC-SPL) | 14 |
| 17015 | Visakha Express | [BBSN - SC](https://www.confirmtkt.com/train-schedule/17015-VISAKHA-EXPRESS) | 43 |
| 12737 | Goutami SF Express | [COA - LPI](https://www.confirmtkt.com/train-schedule/12737-GOUTAMI-SF-EXP) | 21 |
| 12727 | Godavari Express | [VSKP - HYB](https://www.confirmtkt.com/train-schedule/12727-GODAVARI-EXPRES) | 21 |
| 17044 | Anakapalle - Secunderabad Express | [AKP - SC](https://www.confirmtkt.com/train-schedule/17044-AKP-SC-EXPRESS) | 16 |
| 12775 | Cocanada SF Express | [CCT - LPI](https://www.confirmtkt.com/train-schedule/12775-COCANADA-SF-EXP) | 15 |
| 12783 | Visakhapatnam - Secunderabad AC Express | [VSKP - SC](https://www.confirmtkt.com/train-schedule/12783-VSKP-SC-AC-EXP) | 12 |
| 07040 | Kakinada Town - Secunderabad special | [CCT - SC](https://www.confirmtkt.com/train-schedule/07040-CCT-SC-SPL) | 11 |
| 12739 | Garib Rath | [VSKP - SC](https://www.confirmtkt.com/train-schedule/12739-GARIB-RATH) | 11 |

#### Appended SC-to-RJY routes

Discovered using the reverse
[eRail station-pair listing](https://erail.in/trains-between-stations/secunderabad-jn-SC/rajamundry-RJY)
and checked against each full ConfirmTkt schedule on 2026-10-19. All 25 exact
SC-to-RJY candidates had schedules containing SC before RJY; these add 586 halt
entries. The existing 24 opposite-direction routes are unchanged.

| Train | Service | Full schedule | Halts |
|---|---|---|---:|
| 17222 | LTT - Kakinada Port Express | [LTT - COA](https://www.confirmtkt.com/train-schedule/17222) | 24 |
| 11019 | Konark Express | [CSMT - BBSN](https://www.confirmtkt.com/train-schedule/11019) | 44 |
| 22850 | Secunderabad - Shalimar Weekly Superfast Express | [SC - SHM](https://www.confirmtkt.com/train-schedule/22850) | 21 |
| 12774 | Secunderabad - Shalimar AC Superfast Express | [SC - SHM](https://www.confirmtkt.com/train-schedule/12774) | 16 |
| 20707 | Vande Bharat Express | [SC - VSKP](https://www.confirmtkt.com/train-schedule/20707) | 8 |
| 18112 | Yesvantpur - Tatanagar Express | [YPR - TATA](https://www.confirmtkt.com/train-schedule/18112) | 40 |
| 12756 | Bhavnagar Terminus - Kakinada Port Superfast Express | [BVC - COA](https://www.confirmtkt.com/train-schedule/12756) | 31 |
| 12806 | Janmabhoomi Superfast Express | [LPI - VSKP](https://www.confirmtkt.com/train-schedule/12806) | 25 |
| 17205 | Sainagar Shirdi - Kakinada Port Express | [SNSI - COA](https://www.confirmtkt.com/train-schedule/17205) | 39 |
| 07046 | Secunderabad - Naharlagun Special | [SC - NHLN](https://www.confirmtkt.com/train-schedule/07046) | 45 |
| 17290 | Mysuru - Kakinada Town Express | [MYS - CCT](https://www.confirmtkt.com/train-schedule/17290) | 34 |
| 20834 | Vande Bharat Express | [SC - VSKP](https://www.confirmtkt.com/train-schedule/20834) | 7 |
| 12704 | Falaknuma Superfast Express | [SC - HWH](https://www.confirmtkt.com/train-schedule/12704) | 27 |
| 17016 | Visakha Express | [SC - BBSN](https://www.confirmtkt.com/train-schedule/17016) | 43 |
| 12728 | Godavari Superfast Express | [HYB - VSKP](https://www.confirmtkt.com/train-schedule/12728) | 21 |
| 07039 | Secunderabad - Kakinada Town Special | [SC - CCT](https://www.confirmtkt.com/train-schedule/07039) | 11 |
| 12776 | Cocanada Superfast Express | [LPI - CCT](https://www.confirmtkt.com/train-schedule/12776) | 15 |
| 07069 | Secunderabad - Visakhapatnam Special | [SC - VSKP](https://www.confirmtkt.com/train-schedule/07069) | 14 |
| 07071 | Secunderabad - Visakhapatnam Special | [SC - VSKP](https://www.confirmtkt.com/train-schedule/07071) | 13 |
| 12740 | Garib Rath Express | [SC - VSKP](https://www.confirmtkt.com/train-schedule/12740) | 11 |
| 18520 | LTT - Visakhapatnam Express | [LTT - VSKP](https://www.confirmtkt.com/train-schedule/18520) | 25 |
| 17043 | Secunderabad - Anakapalle Express | [SC - AKP](https://www.confirmtkt.com/train-schedule/17043) | 16 |
| 12738 | Goutami Superfast Express | [LPI - COA](https://www.confirmtkt.com/train-schedule/12738) | 22 |
| 12784 | Secunderabad - Visakhapatnam AC Superfast Express | [SC - VSKP](https://www.confirmtkt.com/train-schedule/12784) | 12 |
| 07425 | Secunderabad - Srikakulam Road Special | [SC - CHE](https://www.confirmtkt.com/train-schedule/07425) | 22 |

Arrival-day corrections: 17205 arrives at Parbhani (PBN) at 23:45 on day
zero and departs at 00:05 on day one; 17290 arrives at Dharmavaram (DMM) at
23:58 on day zero and departs at 00:03 on day one. Both offsets are zero
because `jDate` represents arrival at the queried station.
Nearby-origin results starting at CHZ or KCG, without a scheduled SC halt,
were not included. Specials remain subject to the validity caveats below.

This is a third-party timetable snapshot, not an authoritative or guaranteed
exhaustive list of currently operating trains. Specials can expire or reuse
numbers. Actual operation, cancellations, diversions, weekday schedules, and
special-service validity must be confirmed through NTES/IRCTC before live use.
The current configuration schema does not store service weekdays or validity
dates, and the monitor does not filter non-operating days.

Discovery listed 25 exact RJY-to-SC candidates. Special **07072 is excluded**
because sources disagree: eRail lists VSKP - SC, but
[ConfirmTkt](https://www.confirmtkt.com/train-schedule/07072-VSKP-SC-SPL)
publishes TPTY - DBRG, and
[Goibibo](https://www.goibibo.com/trains/07072-detail/) describes SC - CCT,
the reverse direction. Its route/day offsets remain unverified.
For 07070, ConfirmTkt's display name is inconsistent ("Ask Hyb Spl"), but its
origin, terminus, and scheduled halts describe VSKP - SC, matching discovery;
that published route was retained with this caveat.

Nearby-station-only results were excluded: 15674, 07222, 17295, 18045, 17032,
17066, 17068, 18503, 15646, 07068, 07038, 08045, 17054, 17046, 18527, 12861,
07028, 20809, and 20811. These were returned for CHZ or KCG rather than SC.

## Run

From this directory in PowerShell:

```powershell
python .\monitor.py --help
python .\monitor.py
python .\monitor.py --interval 300
python -m unittest discover -s tests -v
```

The default runs one cycle. `--interval` enables recurring cycles with a delay
after each cycle, not a fixed start-to-start schedule. The example interval is
not a finalized polling policy. `--request-delay` defaults to one second between
request starts, and `--timeout` defaults to 20 seconds. Check the service's
automation policy and rate limits before choosing production settings.

Each scan begins with one warm-up chart request, sent for the first selected
train's first station and its journey date. The API often stalls on the first
request after a quiet period, so the warm-up result is discarded. If the
warm-up fails, a warning is logged and the scan continues. Failed requests are
not retried.

Use `--train-nos` to scan only selected trains, keeping their order from the
route configuration:

```powershell
python .\monitor.py --notification telegram --state .\state-telegram.json --train-nos "18519,12727,07426" --interval 300
```

Omitting this flag scans all configured trains. Numbers must be five digits
(retain leading zeros); duplicates are scanned only once. Unknown numbers or
malformed lists cause an explicit error rather than a partial scan. The filter
applies to every cycle and does not change route data or bypass notification
deduplication. Chat-ID discovery and synthetic test messages do not scan trains.

Without `--start-date`, each cycle picks the train start date from the current
IST time: before 14:00 it scans **today's** trains, and from 14:00 until
midnight it scans **tomorrow's** trains. Charts are usually prepared about 10
hours before departure, so by 2 PM today's charts are mostly done and later
cycles are more likely to find tomorrow's charts.

Use `--start-date YYYY-MM-DD` to scan a specific train start date instead.
Each station's `jDate` is still that date plus its configured
day offset. With `--interval`, every cycle uses the given date and does not
roll over. Invalid dates are rejected before any scan starts.

```powershell
python .\monitor.py --start-date 2026-10-19 --train-nos "18519,12727"
```

### Settings from `.env`

Every scan option can also be set in the project's `.env` file (or as a process
environment variable), so the monitor can run with no flags:

```dotenv
MONITOR_TRAINS=trains.json
MONITOR_TRAIN_NOS=18519,12727
MONITOR_STATE=state-telegram.json
MONITOR_START_DATE=2026-10-19
MONITOR_INTERVAL=300
MONITOR_TIMEOUT=20
MONITOR_REQUEST_DELAY=1
MONITOR_DEBUG=false
MONITOR_NOTIFICATION=telegram
MONITOR_KEEP_AWAKE=display
```

| Setting | Flag | Value |
|---|---|---|
| `MONITOR_TRAINS` | `--trains` | route file path |
| `MONITOR_TRAIN_NOS` | `--train-nos` | comma-separated five-digit numbers |
| `MONITOR_STATE` | `--state` | state file path |
| `MONITOR_START_DATE` | `--start-date` | `YYYY-MM-DD` |
| `MONITOR_INTERVAL` | `--interval` | seconds; empty/unset means one cycle |
| `MONITOR_TIMEOUT` | `--timeout` | seconds |
| `MONITOR_REQUEST_DELAY` | `--request-delay` | seconds |
| `MONITOR_DEBUG` | `--debug` / `--no-debug` | `true/false`, `yes/no`, `on/off`, `1/0` |
| `MONITOR_NOTIFICATION` | `--notification` | `console`, `telegram`, `whatsapp` |
| `MONITOR_KEEP_AWAKE` | `--keep-awake` | `display` (default), `system`, `off` |

Precedence is: command-line flag, then process environment variable, then
`.env`, then the built-in default. Empty or unset values fall back to the
built-in default. Relative paths resolve from the current working directory,
like the flags. Invalid values stop the run before any scan, naming the bad
setting. The action flags `--telegram-chat-ids` and `--test-notification` are
command-line only. A value set in `.env` cannot be cleared from the command
line (for example, remove `MONITOR_INTERVAL` to return to single-cycle runs),
except `--no-debug`.

### Keeping the laptop awake

Many laptops use Modern Standby (`powercfg /a` lists "Standby (S0 Low Power
Idle)"). When the display turns off due to inactivity, Windows enters standby
and pauses desktop programs, so the monitor stops scanning until you unlock,
even when "sleep" is set to never. The event log shows this as Kernel-Power
events 506/507.

While repeating with `--interval`, `--keep-awake` (`MONITOR_KEEP_AWAKE`) asks
Windows to stay awake only for as long as the monitor runs, and releases the
request when it exits:

- `display` (default): keeps the system awake and the screen on, which
  prevents Modern Standby. The screen can still be locked with Win+L.
- `system`: only blocks idle sleep; the screen may turn off, and Modern
  Standby laptops may still pause the monitor.
- `off`: makes no request.

Closing the lid, pressing the power button, or choosing Sleep still suspends
the laptop. Keep it on AC power for long runs. Single-cycle runs make no
request.

Logs go to stderr with timestamps in the host's local timezone, severity, and
logger name. Each cycle logs its train start date explicitly in IST, the train
count, completion result, and the wait before the next cycle. Station `jDate`
still includes the configured relative-day offset.
Every chart request logs an equivalent curl (for Postman import or a POSIX shell),
followed by HTTP status as soon as headers arrive and the complete response body
when read. These are INFO logs, including malformed and HTTP error bodies.
Failure logs show elapsed time, any received HTTP status, and whether the full
body arrived. No status/body can be logged if the server sends none.
`--debug` additionally enables DEBUG-level logging:

```powershell
python .\monitor.py --notification telegram --state .\state-telegram.json --interval 300 --debug
```

Response bodies are quoted on one log line and may contain operational data;
keep logs private. Telegram/WhatsApp response bodies, tokens, and authorization
headers are not logged. The printed curl describes explicit chart headers and
payload, not a byte-for-byte transport capture; automatic HTTP headers may differ
between curl and Python. Do not paste the POSIX-formatted curl into PowerShell
without adapting its quoting.

Alternative configuration and state paths can be passed using `--trains` and
`--state`; their parent directories must already exist. Default paths are
relative to the script, regardless of the shell's working directory.

## Telegram notifications (recommended for getting started)

No extra packages, business account, or template approval are required.
Only private chats are supported, with up to five consenting recipients.

1. In Telegram, open the official [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, choose a display name and an available username ending in
   `bot`, and save the token privately. Never share it in chat or source control.
3. Each recipient must open your bot using its username and send `/start`.
4. Open PowerShell in this project and set the token without echoing it:

```powershell
$secret = Read-Host "Telegram bot token" -AsSecureString
$env:TELEGRAM_BOT_TOKEN = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
python .\monitor.py --telegram-chat-ids
```

The discovery command prints `TELEGRAM_CHAT_IDS=...` using recent private
messages sent to your bot. It does not contact IRCTC or change notification
state. Copy only the IDs of your intended, consenting recipients, not everyone
who happens to message a publicly discoverable bot.
Updates are retained by Telegram for at most 24 hours; if an ID is missing,
have that person send `/start` or a fresh message and run the command again.
This helper reads recent updates without an offset/acknowledgement and never
deletes a webhook. An existing webhook or another polling bot process can
conflict with `getUpdates`; resolve that setup before using discovery.

Set the resulting IDs (not usernames or telephone numbers) in the same terminal:

```powershell
$env:TELEGRAM_CHAT_IDS = "<your-positive-chat-id>,<another-private-chat-id>"
python .\monitor.py --notification telegram --test-notification
```

The test sends clearly labelled synthetic data to every configured chat without
reading routes, calling IRCTC, or modifying state. Verify receipt on your phone.
Then run one cycle or recurring cycles:

```powershell
python .\monitor.py --notification telegram --state .\state-telegram.json
python .\monitor.py --notification telegram --state .\state-telegram.json --interval 300
```

Alternatively, fill the project's local `.env` file with `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_IDS`. It is loaded automatically from beside the script,
regardless of the working directory. Existing process variables take precedence,
including empty variables; remove stale terminal variables to use file values.
Use one `KEY=value` per line, optionally single/double quoted. Blank lines,
full-line comments, and whitespace-prefixed inline comments are supported.
Values are literal: shell commands, variable expansion, multiline values,
escaped quotes, and `export` syntax are not supported. Invalid settings stop
execution without printing their values. A missing file is optional.
The actual `.env` is Git-ignored; never commit or share it.
WhatsApp settings are not needed in Telegram mode. Console remains
the default if `--notification` is omitted. `--timeout` applies to Telegram too.

Messages are one Telegram HTML card per train: train name, number and
direction, the chart preparation time, and a single collapsed (expandable)
section holding every station with vacancies. Tap it to expand the stations and
their class-wise berth counts. Telegram may show the first line or two of a
collapsed section as a preview. Link previews are disabled.
Every name and API value is HTML-escaped, so unusual characters cannot break
the markup.
Telegram success means its API created a message, not that the user read it;
there is no read/delivery tracking or incoming-command server in this cut.

Successful message IDs are atomically stored separately for each bot and chat
using keys `telegram:<bot-id>:<chat-id>`, alongside existing console/WhatsApp
state. Token rotation for the same bot preserves deduplication, while a
different bot does not suppress its alerts. Partial failures retry only failed
recipients on later cycles after a fresh vacancy check. Network timeouts or a
crash after sending but before persistence can still cause duplicates.
Pending sends are not queued beyond the monitored journey's midnight rollover.
There are no immediate retry loops; HTTP 429 errors require waiting before
retrying. HTTP 401 indicates a token problem; 403 commonly means the user
blocked or has not started the bot. Tokens and raw Telegram errors/URLs are
never logged because Telegram embeds the token in its API URL.
Keep state files private (they contain chat IDs); the recommended filename is
Git-ignored. Only run one monitor instance per state file.

Reference: [Telegram Bot API](https://core.telegram.org/bots/api).

## WhatsApp notifications (Meta Cloud API, optional)

The outbound integration uses the standard library; no extra dependencies are
required. Console remains the default. WhatsApp mode fails explicitly if its
configuration is missing; it never silently falls back to console.

### 1. Configure your Meta account

Follow Meta's [Cloud API setup guide](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started):

1. Create a Meta developer app with the WhatsApp use case and connect a business
   portfolio / WhatsApp Business Account.
2. In API Setup, select your sender and record its **Phone number ID** (not its
   telephone number and not the Business Account ID).
3. For a test sender, add and verify your permitted test recipients in Meta's
   dashboard. Do not assume test resources support an unapproved custom template;
   use a registered sender and approved template when required.
4. For ongoing use, register a sender, complete Meta's required account setup and
   payment settings, and provision a suitably scoped system-user access token.
   It needs `whatsapp_business_messaging` and access to the sender's assets.
   Temporary API Setup tokens are suitable only for initial tests and expire.
5. Obtain explicit opt-in from every recipient, and remove anyone who opts out.
   This application sends to all configured recipients, not a WhatsApp group.

Meta controls eligibility, template approval, pricing, and delivery. Five
recipients does not exempt the application from these rules. No Meta account,
sender, token, billing setup, or template approval is provisioned by this code.

### 2. Create the approved template

In WhatsApp Manager create `train_vacancy_alert`, language **English (US)**
(`en_US`), using **positional** body variables. Use the body below with no header
variables, buttons, or additional parameterized components. Submit it under the
category appropriate to your use case; Meta makes the final classification.
Do not send until the template is approved in the selected language.

```text
Train vacancy alert for your subscribed journey.
Train: {{1}} ({{2}})
Boarding station: {{3}}
Train start date: {{4}}
Station arrival date: {{5}}
First chart prepared at: {{6}}
Reported vacancies by class: {{7}}
Check IRCTC for current booking availability. Chart vacancies do not guarantee bookable tickets.
```

The exact positional order is:

| Variable | Value | Approval example |
|---|---|---|
| 1 | Train display name | Godavari Express |
| 2 | Train number | 12727 |
| 3 | Boarding station name and code, with direction | Rajahmundry (RJY), towards SC (RJY -> SC) |
| 4 | Train start date | 2026-10-19 |
| 5 | Arrival date at that station | 2026-10-20 |
| 6 | First chart preparation timestamp | 2026-10-19 19:52:25 |
| 7 | Positive berth counts, sorted by class | 3A: 2, SL: 6 |

Dates use ISO format and are based on IST. The preparation timestamp is passed
through from IRCTC. Station arrival date is not a live arrival time.
The code always uses approved templates, including within a customer-service
window, so it can also send proactive alerts outside that window.
See Meta's [template fundamentals](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview).

### 3. Set environment variables

Set the `WHATSAPP_*` settings in `.env` beside the script, or set process
environment variables in PowerShell in the same terminal used to start the
monitor. Terminal variables take precedence over file values.
Do not paste real tokens into
chat, commit them, put them in shell command arguments, or store them in routes.

```powershell
# Prompts without echoing the token or saving it in command history.
$secret = Read-Host "Meta access token" -AsSecureString
$env:WHATSAPP_ACCESS_TOKEN = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret

# Replace the placeholders using your own Meta dashboard values.
$env:WHATSAPP_PHONE_NUMBER_ID = "<numeric phone number ID>"
$env:WHATSAPP_API_VERSION = "<supported Graph API version, e.g. v23.0>"
$env:WHATSAPP_TEMPLATE_NAME = "train_vacancy_alert"
$env:WHATSAPP_TEMPLATE_LANGUAGE = "en_US"
$env:WHATSAPP_RECIPIENTS = "<country-code-and-number>,<another-number>"
# Set this only after obtaining consent from every recipient.
$env:WHATSAPP_RECIPIENTS_OPTED_IN = "true"
```

Use an explicitly supported Graph API version from your Meta dashboard; the
application intentionally does not guess a version. Recipients must be 1-5
unique international phone numbers containing country code and digits only,
without a plus sign, spaces, or punctuation. For India that normally means `91`
followed by the mobile number. Do not use the placeholder values literally.
`--timeout` also controls the Meta socket timeout (20 seconds by default).

### 4. Send an explicit test, then monitor

The following test sends the configured template to **every configured
recipient**, may incur messaging charges, and labels the data as a test. It
does not call IRCTC, require routes, read/write state, or start recurring cycles.
It still requires your approved seven-parameter template, not Meta's unrelated
`hello_world` template.

```powershell
python .\monitor.py --notification whatsapp --test-notification
```

Verify the test message arrives in WhatsApp. Then run one real polling cycle or
enable recurring checks:

```powershell
python .\monitor.py --notification whatsapp --state .\state-whatsapp.json
python .\monitor.py --notification whatsapp --state .\state-whatsapp.json --interval 300
```

Use the **same state path** on subsequent runs. You can also use the existing
default state path: legacy console records do not suppress WhatsApp messages.
State files contain recipient phone numbers and Meta message IDs; keep them
private. Default state paths and .env files are ignored by Git. If you choose a
different state filename, add it to your ignore rules.

### Acceptance, retries, and delivery limitations

- A successful Meta response containing a message ID means **accepted for
  processing**, not delivered or read. A quality-held message is also recorded
  as accepted; it can still fail later. This cut has no delivery-status webhook,
  callback server, or automatic handling of asynchronous delivery failures.
  Confirm live receipt during setup; use Meta tooling to investigate failures.
- Each successful recipient is atomically saved immediately with the journey
  and Meta message ID. If one recipient fails, the other recipients are still
  attempted. On later cycles/restarts, only recipients without accepted receipts
  are retried, after a fresh chart/vacancy check. A train is skipped only when
  all currently configured recipients have an accepted receipt for that journey.
- A newly added recipient is eligible even if others were already notified;
  removed recipients are not contacted. All recipients currently receive alerts
  for all configured trains. Subscription preferences are not implemented.
- Version-1 state is read without loss and saved in the current version on the next
  successful notification. Legacy records remain console-only. Console and
  WhatsApp deduplication are independent.
- HTTP failures include numeric Meta error codes and actionable hints without
  logging tokens, complete phone numbers, or raw provider error bodies.
  Check code 190 for token/access problems, 131030 for test-recipient access,
  132001 for template name/language/approval, and 132000 for parameter mismatch.
  Other codes may require checking billing, account health, and rate limits.
- There is no immediate automatic retry loop. Failures are logged and retried
  on later polling cycles. One-shot execution exits 1 on failures.
  After a timeout or network error, acceptance may be unknown. A later retry
  can duplicate an already-accepted message; exactly-once delivery is not
  guaranteed. The same risk applies if state persistence fails after a send.
- Pending recipients are not an independent queue: they are checked only while
  that journey is being polled and vacancies still exist. The existing IST
  midnight rollover rule still applies.
- Changing the Meta sender does not reset recipient receipts. Choose a new state
  file only if you intentionally want to notify previously accepted journeys again.

## Behavior

- Each cycle picks the train start date from the current IST time (today
  before 14:00, tomorrow from 14:00), unless `--start-date` is set.
- Each request sends that start date plus the station's day offset as `jDate`.
- Only `chartOneDate` determines chart readiness: null means not prepared.
- Prepared-chart coach vacancies are summed by class; only positive counts
  count as vacancies. Every station is scanned (vacancies no longer end the
  scan), and all stations with vacancies are sent together in **one alert per
  train**. Console output adds a `stations` list; WhatsApp variables 3, 5 and 7
  list every station, its dates, and `STATION - class: count` groups joined by
  ` | `.
- Stations are checked in route order. As soon as one station's chart is not
  prepared, the remaining (later) stations of that train are skipped for that
  cycle and the whole train is checked again next cycle. Vacancies already
  found at earlier stations are still sent in that cycle; because a journey is
  notified only once, stations whose charts become ready later are not added.
- When every station of a train returns a prepared chart without vacancies and
  no request fails, the journey is recorded in `scannedJourneys` and skipped in
  later cycles and restarts, whether or not it was ever notified. A request
  failure, or an unprepared chart at any station, leaves the journey
  unrecorded, so it is scanned again next cycle. Journeys with vacancies are
  tracked by their notification receipts instead.
- State is saved as version 3:
  `{"version": 3, "notifiedJourneys": [...], "acceptedMessages": {...}, "scannedJourneys": [...]}`.
  Version 1/2 files are read without loss and upgraded on the next save. Remove
  a `trainNo:startDate` entry from `scannedJourneys` to force a rescan.
- Successful console output counts as a completed console notification.
  Alerts include `trainName`, `boardingStationName`, `direction`, and
  `towardsStation` alongside `trainNo` and `boardingStation`; Telegram and
  WhatsApp show the direction next to the boarding station.
- Successful notification is followed by an atomic state-file replacement.
  Journeys are keyed by train number and start date, not arrival date. Restarting
  skips notified journeys; the same train on a new start date is checked again.
- Request failures are logged and remaining stations/trains are checked.
  Failed notifications remain eligible for later cycles. One-shot execution
  exits with code 1 if any check/notification fails. Persistent mode logs failures
  and retries on the next cycle. Invalid configuration/state and state-write
  failures stop execution rather than silently discarding deduplication.

## Limitations and safety

- Run only one instance against a given state file. Atomic replacement prevents
  partial writes, but is not a multi-process lock.
- A crash between notification delivery and state persistence can cause a
  duplicate notification. This first cut does not claim exactly-once delivery.
- At midnight IST, the next cycle monitors the new day's departures, not
  yesterday's still-running trains. A cycle already in progress keeps its
  captured start date.
- Chart vacancy counts do not establish that a berth is available for a user's
  full origin-to-destination segment, nor guarantee bookable `CURR_AVBL` tickets.
  Destination-specific booking validation is not part of this cut.
- The chart client explicitly sets only `Accept: application/json`,
  `Content-Type: application/json`, `User-Agent: PostmanRuntime/7.56.1`,
  `Connection: keep-alive`, `Origin: https://chartvacancy.net`,
  and `Referer: https://chartvacancy.net/`
  to match the supplied curl, without cookies or other custom headers.
  The curl executable still supplies transport headers such as Host and Content-Length.
  Chart requests are sent by running `curl` (bundled with Windows 10+ as
  `curl.exe`) with `--http1.1`, not through Python's HTTP libraries. The API's
  edge protection silently stalls requests whose TLS handshake comes from
  OpenSSL 3.5+ (for example Python 3.15), even with identical HTTP bytes, while
  curl and Python builds with OpenSSL 3.0 receive responses. `curl` must be on
  `PATH`. Each request runs a new curl process, so connections are not reused.
  curl honours its usual proxy environment variables and does not follow
  redirects. Non-2xx responses are errors.
  An HTTP 200 response with `"error": "Chart not prepared"` and a null
  `chartOneDate` is treated as "chart not prepared", not as an error.
  These headers do not guarantee access or resolve timeouts. HTTP errors are surfaced. Access
  may require an authorized service arrangement; an unauthenticated endpoint is
  not proof of permission to automate it.
- The tests use mocked curl processes, one local HTTP server and local fixtures;
  they do not contact IRCTC.
- Final polling policy, delivery-status webhook handling, operating-day/validity
  filtering, and authoritative refresh of route data remain outstanding.
  No credentials are bundled.
