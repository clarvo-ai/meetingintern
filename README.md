# MeetingIntern

## VM migration (staged)

`vm_runner.py` runs discovery every 15 minutes, premeeting settings every 15
minutes, and the durable meeting-ready outbox every five minutes. It supervises
the existing archive and analysis workers and uses the same Firestore records,
source keys, retry delays, Chat uncertainty handling and receiver IDs. A single
local lock and the existing Firestore leases protect worker claims. VM metadata
credentials impersonate the same service account as Cloud Run; audience-bound
ID tokens are minted with that identity. The VM must have sufficient temporary
disk for audio, ffmpeg/ffprobe, access to the original Firestore project and
Secret Manager, and permission to impersonate that service account.

Use `prepare_vm_config.py` to copy the current Cloud Run environment settings
and Secret Manager references into a **new** mode-0600 configuration file; it
does not fetch or print secret values and refuses inconsistent identities or
settings. Its file includes private Drive and calendar settings. Install it as
`/etc/meetingintern/config.json`, owned by root and readable only by the
dedicated `meetingintern` service user. The runner loads referenced secrets at
startup. `requirements-vm.lock` records the dependencies from the deployed
archive image and is verified on the VM's Python version. The VM unit expects
the existing protected meeting Logs credential.

Do not run the VM supervisor concurrently with the Cloud schedulers during
cutover. First install the code, dependencies, configuration, unit and local
tests without starting it. Pause the checker, auto-artifacts and meeting-ready
Cloud Scheduler jobs. Wait for all already-running checker requests and archive,
analysis and delivery job executions to finish; do not reset Firestore leases
or rerun uncertain Chat sends. Verify the premeeting scan has also finished.
Start one VM supervisor, verify a new meeting reaches Drive, analysis, Chat and
both receivers exactly once, and check pending/review and retry state. Leave the
old schedules paused only after verification; preserve cloud jobs for rollback.
For rollback, stop the VM service and wait for its entire process group to exit
before resuming the old schedules. An interrupted worker can remain leased until
its existing lease expires; do not force its claim or repeat an uncertain effect.
This procedure is not a completed cutover until the live journey is verified.

New transcripts and briefs store a digest of their intended complete text.
When an interrupted write leaves a Drive document behind, a worker verifies its
export before treating it as complete. Empty, modified, duplicate, or older
documents without a digest enter Firestore `status=review` or
`analysis_status=review` rather than being overwritten, deleted or blindly
retried. Inspect the source and destination document and its associated Chat
state before deciding on manual recovery. The digest ignores only Google Docs
line-ending and blank-paragraph normalization. An uncertain Chat send remains
separately marked for review; the receiver outbox is independent.

## Meet audio archive MVP

`audio_archive.py` is a separate archive path for new Google Meet artifacts.
Every 15 minutes, the checker first lists completed Meet conference records
visible to the explicitly configured Juha, Robert, and Mark accounts.
The Meet API supplies exact transcript and recording Drive file IDs. The
checker verifies that the source Doc is actually owned by an allowed account,
reads source files by ID through domain-wide delegated Drive access, and writes
the English transcript as the service account to the company shared Drive.
This does not depend on the personal Drive folder layout or change source
sharing. The checker then scans the configured personal `Google Meet` folder
roots as a fallback for artifacts absent from the Meet API. Matching source
file IDs deduplicate the two discovery paths.

Both paths wait until a Meet Doc and an MP4 recording are
both unchanged for five minutes. If a recording has not appeared three hours after
the Doc was created, they archive the settled Meet transcript alone. The Meet
API path also handles a generated recording with no Meet transcript: after
three hours it transcribes the recording audio with Gemini and creates the
English transcript. Meet API artifacts stuck in `STARTED` or `ENDED` wait the
same three hours before the available Doc or recording is used alone. Shortcuts in the folder fallback are
followed only when the service account can read their targets. Recurring
meeting folders are matched by the meeting timestamp in file names. Multiple
Google Meet roots for one owner can be grouped so a Doc in one root pairs with
a same-named recording in another root. Roots for different owners stay separate.

The recording's audio is extracted to 15-minute MP3 chunks. Gemini
`gemini-3.5-flash-lite` checks every audio chunk for non-English speech. For
English, the Google Meet transcript is used when available; for other languages,
Gemini transcribes and translates each audio chunk into English. Speaker names
and emails found in the Meet Docs are supplied only as spelling hints for the
audio transcription. The output is a new Google Doc in the configured
shared-drive folder, with links to available source files. Source files are not
modified or copied. The destination Doc stores a stable source key and source
IDs as Drive properties to prevent duplicate output.

Before publishing, Gemini cleans every transcript in bounded, overlapping text
batches. The archive worker supplies the matching audio chunk when a timestamp
is available, and uses the same `MEETING_TEAM_ROSTER_JSON` and
`MEETING_COMPANY_REFERENCE_JSON` values as the analysis job for spelling hints.
Calendar invitees are hints, not proof of attendance or who spoke. Source
speaker and time boundaries are retained; only unambiguous full-name spelling
matches are normalized. Incomplete responses, changed numbers or materially
shortened segments are retried and then fail the archive job; an audio-backed,
unresolved single-word turn can instead be marked `[unclear]`. Raw text is not
published as a fallback. When audio alignment remains uncertain, an individual
turn can be cleaned and audited again using only the source text. Set the roster environment variable and bind the
company reference secret on the archive job when deploying this change. Optional
`MEETING_TRANSCRIPT_TERMS_JSON` supplies verified product and tool names, for
example `["Codex"]`; these are hints rather than proof of what was said.

Existing archived Docs can be cleaned in place without changing their links.
Run `python transcript_backfill.py --state-id <archive-state-id> --preview-dir <local-dir>`
with the archive worker's credentials, destination, roster and company reference
configuration to generate a local preview. `--all` selects completed archive
records instead. Replace `--preview-dir` with `--apply-from <local-dir>` to apply
the exact reviewed previews. The backfill verifies preview hashes and revision
preconditions, leaves source Meet files alone, and refuses documents with
complex formatting or multiple tabs.

Recordings longer than eight hours are skipped. Video size is not capped:
`ffmpeg` reads the MP4 through a local proxy backed by Google Drive byte-range
requests, so only compressed audio chunks use temporary storage. The
`Dockerfile.checker` and `archive_checker.py` form a short private Cloud Run
service for Cloud Scheduler (`*/15 * * * *`); `Dockerfile.archive` and
`archive_job.py` form a separate Cloud Run Job for each ready meeting. Firestore
claims prevent overlapping checks and retries from launching duplicate work.
This repository does not create or schedule those resources by itself. Enable
the Firestore API and create a Firestore database before deployment. The checker
service account needs Firestore read/write permission and Cloud Run Job execute
with overrides permission. The job service account needs Firestore read/write,
Drive access to the source and destination folders, and the Gemini API key.
Configure the job task timeout for long meetings (for example, 12 hours).

Set `MEETING_SOURCE_FOLDER_IDS` (comma-separated),
or `MEETING_SOURCE_FOLDER_GROUPS_JSON` (a JSON array of root-ID arrays, one
array per owner),
`ARCHIVE_DESTINATION_FOLDER_ID`, `ARCHIVE_JOB_NAME` (full resource name), and
`ARCHIVE_START_TIME` (ISO timestamp with timezone) on the checker. The start
time limits automatic discovery to newly created Docs so deployment does not
unexpectedly backfill historical meetings. Set `ARCHIVE_DESTINATION_FOLDER_ID`
and `GOOGLE_AI_API_KEY` on the job; the checker supplies `ARCHIVE_STATE_ID` for
each execution. `ARCHIVE_DISPATCH_LIMIT` defaults to five meetings per check.
The folder fallback requires source folders shared with the job's service
account; access to a shortcut does not grant access to its target. The primary
Meet API path requires `MEETING_CALENDAR_IDS` and
`MEETING_MEET_SERVICE_ACCOUNT_EMAIL` on both checker and archive job, Meet API
access with `meetings.space.readonly`, and domain-wide delegation for
`drive.readonly`. Set `MEET_API_START_TIME` on the checker to a timestamp with
timezone when first enabling API discovery; this avoids an unintended backfill
of historical test meetings. The existing `ARCHIVE_START_TIME` still limits the
folder fallback. Keep the existing
organizer and Chat summary pipeline configured separately.
The checker dispatches one ready analysis at a time, oldest meeting first;
an active or retrying older brief holds newer Chat posts until it resolves.
The analysis job uses the same delegated source reads for speaker evidence, so
newly discovered files need no folder share for report context. Only artifacts
actually owned by `MEETING_CALENDAR_IDS` or the optional
`MEETING_ADDITIONAL_FILE_OWNERS` accounts are eligible; the latter grants file
access without enabling automatic recording on that person's calendar. An
external organizer's recording visible to an invitee is not archived. Meet conference records expire
after 30 days, so the checker scans the most recent 14 days and the folder
fallback remains useful for older or otherwise absent API records.

## Premeeting Meet artifacts

The checker image also exposes `POST /auto-artifacts` for a separate 15-minute
Cloud Scheduler job. Set `MEETING_CALENDAR_IDS` to the primary calendars that
the service account can read and `MEETING_MEET_SERVICE_ACCOUNT_EMAIL` to the
delegated service account. The route looks at confirmed events starting in the
next 24 hours. It only changes Calendar-created Meet spaces when the event
organizer matches the configured primary calendar. The original creator may be
an older email address after an event or domain migration. Invitations,
past meetings, and events without a Calendar Meet conference ID are ignored.
For each eligible space, it enables automatic recording, transcription, and
Gemini notes if needed, leaving unrelated Meet settings unchanged. A repeated
check is safe because spaces already set to `ON` are not patched.

Workspace domain-wide delegation must authorize the service account's numeric
OAuth client ID for `https://www.googleapis.com/auth/meetings.space.settings`.
The service account needs permission to mint its own delegated token, and the
Cloud Scheduler caller needs Cloud Run invoker permission on the checker.
Schedule `/auto-artifacts` separately from `/`, with minutes staggered from
the archive check if the Cloud Run service has one instance and concurrency one.
An authorization failure in premeeting configuration cannot delay ingestion.
Automatic recording begins
only when a participant with recording privileges joins; Google Workspace
recording eligibility and storage rules still apply.

## Meeting briefs and Chat updates

When an archive job publishes a full English transcript, it marks that meeting
ready for a separate analysis job. The 15-minute checker dispatches analysis
jobs independently, so a summary or Chat failure never reruns audio extraction.
The analysis job reads the full archived transcript and uses Gemini
`gemini-3.5-flash-lite` in focused passes for questions and replies, discussed
next steps, and a detailed narrative. Every pass receives the complete spoken
transcript; audio chunking remains an earlier, separate step. The report pairs
questions in both directions with the actual reply or an open status, and lists
discussed problems separately. Next steps use short action lines and optional
reasons, with proposed or unclear items marked as meeting notes to verify.
The analysis job can use the same optional `MEETING_TRANSCRIPT_TERMS_JSON`
vocabulary as the archive job for grounded product and tool spellings.
A person is attributed only when the summarized speech contains a named speaker
label backed by the original Meet transcript and the discussion supports the
assignment. The job creates a
companion `Meeting Brief` Doc in the same shared folder; the full English
transcript Doc stays untouched. Firestore stores the type, tags, brief ID, and
delivery status for later reporting and safe retries.

The Chat header combines confirmed Meet joiners with matched Calendar invitees
by unambiguous name or email matches. Anonymous first-name matches to an
invitee email are marked as likely and unverified. When a unique first-name
match has an accepted invitation, a matching transcript speaker, and both the
Calendar event and Meet participant record are linked through source artifacts,
the header says "matched by name" instead. This does not change the recorded
identity evidence or treat the person as signed in; invitation response status
never establishes attendance. The header keeps the meeting type when supported,
topic tags, Finland-local meeting time, relevant company context, and a matched
Calendar event link. The full transcript is the first URL for the Chat preview,
followed by the event and companion brief. The job posts one update to the
configured Google Chat space. Google Chat incoming webhooks
do not support idempotent request IDs, so a send with an uncertain result is
held for manual reconciliation rather than retried into a possible duplicate.
Customer demo search links and the representative's own feedback are outside
this increment.

### Customer-context meeting-ready outbox

The integration is disabled by default. When enabled, saving the companion brief
and creating an immutable Firestore outbox event happen in the same transaction.
Delivery runs separately from analysis and Chat. The event contains only:

```json
{"schema_version":1,"event_id":"<source key>","source":"meetingintern","meeting_id":"<source key>","transcript_file_id":"<Drive file ID>","brief_file_id":"<Drive file ID>"}
```

Both keys are the existing 64-character lowercase SHA-256 source key. No
transcript or other meeting details are sent. Set all three variables on the
analysis job and the independently scheduled delivery job:

* `MEETING_READY_WEBHOOK_URL=https://ci.clarvo.ai/webhooks/meeting-ready`
* `MEETING_READY_CUTOVER=<timezone-aware RFC3339 timestamp>`
* `MEETING_READY_SERVICE_ACCOUNT_EMAIL=<analysis job service account email>`
* `MEETING_TODO_WEBHOOK_URL=https://ci.clarvo.ai/todos/api/v1/meeting-events` (optional; enables the to-do receiver)

Run the delivery job as `drive-automation@drive-atuomation-project.iam.gserviceaccount.com`
(the analysis job's existing service account). The worker obtains a Google-signed
ID token for that account with the exact webhook URL as audience. Any missing or
invalid configuration disables delivery. When the to-do URL is set, the worker sends the same immutable event to both receivers with separately audience-bound ID tokens. It acknowledges the outbox only after both return 2xx; partial failures retry both, so both receivers must deduplicate by event and document identity. Run `python meeting_ready_worker.py`
from the analysis image as a separate periodically scheduled Cloud Run Job.

For a safe initial rollout, choose **one future UTC cutover** and keep its exact
value unchanged in both configured jobs. Before that instant:

1. Pause the checker scheduler, then drain all old checker executions. Deploy the
   new checker image; do not remove its discovery marker. New records have an
   immutable first-seen time and `meeting_ready_status=awaiting_brief` even when
   the integration is disabled.
2. Deploy the new analysis image with all three variables above. Configure the
   separate worker job with the same values and service account; schedule it.
   Drain old analysis executions and verify the deployed configurations before
   cutover. Resume the checker scheduler before the chosen cutover.

The worker also reconciles `awaiting_brief` records once a completed archive
has a saved brief. This recovers a post-cutover meeting whose marker was written
by the new checker but whose brief was saved by an old or unconfigured analysis
image, independently of Chat's status. Both jobs must use the same cutover;
changing it later can suppress queued events. These jobs and schedules are not
created by this repository.

Only meetings first discovered at or after cutover, with a verified meeting
start at or after cutover, are eligible. Verification uses the Meet conference
start, a timestamp in the source filename, or an unambiguously matched Calendar
event; artifact upload time alone is not enough. Definitely pre-cutover records
are marked `excluded`, never delivered. Records without first-seen provenance
cannot be assumed new. In particular, meetings discovered by an old checker
before it was drained have no marker and cannot be automatically recovered;
the future cutover and drain above prevent that rollout gap. No historical
backfill is performed. A missing or invalid brief or transcript still needs the
existing archive/analysis pipeline to complete before an event can be created.

Completed candidates with uncertain evidence or invalid IDs are persisted as
`meeting_ready_status=review`, with a non-sensitive `meeting_ready_reason` such
as `missing_verified_start`, `missing_first_seen`, `invalid_source_key`,
`invalid_transcript_file_id`, `invalid_brief_file_id`, or `identical_file_ids`.
Query Firestore's `meeting_audio_archive` collection for `meeting_ready_status`
equal to `review`; the document ID is the meeting source key. After verifying
the source independently, run the worker in the same configured environment:

```bash
python meeting_ready_worker.py --reconcile SOURCE_KEY --verified-start 2026-11-05T12:30:00Z --transcript-file-id DRIVE_ID --brief-file-id DRIVE_ID
```

Provide only corrections that are needed and independently verified. The
verified start requires a timezone; it cannot override known pre-cutover
evidence. The file ID corrections affect only the outbox event, not the Chat
message or the original archive pointers. A successful reconciliation sets the
event pending for the next worker run; otherwise the record stays in review or
is excluded. An absent first-seen time is not automatically repairable: it
requires separate investigation of discovery provenance. A malformed already
queued event is marked `invalid_queued_event` for investigation and cannot be
rewritten by this command, because it may already have been transmitted.

The worker leases events, retries failures with backoff, and marks delivery only
for a 2xx response. It reuses the exact saved payload on every attempt. An HTTP
response or Firestore acknowledgement can be lost after the receiver commits,
so physical requests are at least once: the receiver must atomically deduplicate
by `event_id` to achieve one logical event. The receiver must verify the token's
signature, audience and service account identity. Chat can succeed or fail
independently of this delivery.

The analysis job reads speaker labels and any explicit `Attendees` section from
the original Meet Doc. It never treats names or emails mentioned in Gemini
notes as proof of attendance. The current team roster is supplied as
`MEETING_TEAM_ROSTER_JSON`, a JSON array of `{"name":"Full Name","email":"name@clarvo.ai"}`
objects. For the current team, set it to Juha Lehto, Robert Holmberg, Mark
Szucs, and Miro Keimiöniemi with their `@clarvo.ai` emails. Keep the roster
current when team membership changes. With no roster, it safely omits meeting
types. A team meeting needs substantial speech and either at least two rostered
transcript speakers without outside evidence, or at least two confirmed rostered
Meet joiners with no unknown joiners. An outside Calendar invitee alone cannot
establish attendance or a meeting type. Other external types require high model
confidence and a named outside speaker, explicit Meet Doc attendee, or signed-in
Meet joiner. The Chat update and brief omit uncertain types and the `other` topic
tag; they label each evidence source separately.

`MEETING_COMPANY_REFERENCE_JSON` is an optional JSON array of verified display
names, phonetic aliases, and company domains, for example
`[{"name":"Example Ltd","aliases":["Exampl"],"domains":["example.fi"]}]`.
Names are corrected in generated prose only when an alias occurs in the spoken
transcript or the domain exactly matches a matched Calendar invitee. Keep this
reference current from a trusted company directory; an unknown name is left
uncorrected rather than guessed. Original transcripts and raw evidence stay
unchanged.

`MEETING_CALENDAR_IDS` is an optional comma-separated list of calendars readable
by the analysis job's service account. Calendar events are matched by exact
source attachment ID first, then exact Meet code with time and organizer;
similar titles never establish a match.
Invitees are not proof of attendance. The code requests a Calendar read-only
OAuth scope; enable the Calendar API and give the job's service account event
read access to each calendar before enabling this variable. Calendar
permission failures leave meeting types conservative and do not stop summaries.

Set `MEETING_MEET_SERVICE_ACCOUNT_EMAIL` on the analysis job to enable confirmed
Meet joiners. The Meet API must be enabled in the Cloud project. A Workspace
super admin must authorize the service account's OAuth client ID for only
`https://www.googleapis.com/auth/meetings.space.readonly` through domain-wide
delegation. The service account needs `roles/iam.serviceAccountTokenCreator` on
itself to sign short-lived delegated credentials without a key. The job only
impersonates a source owner on `MEETING_CALENDAR_IDS`, matches a conference by
exact recording Drive ID where available, and otherwise requires a unique Meet
code and time match. Meet conference records expire 30 days after the call.
Unavailable or ambiguous records do not block the rest of the analysis.

Current roster value:

```json
[{"name":"Juha Lehto","email":"juha@clarvo.ai"},{"name":"Robert Holmberg","email":"robert@clarvo.ai"},{"name":"Mark Szucs","email":"mark@clarvo.ai"},{"name":"Miro Keimiöniemi","email":"miro@clarvo.ai"}]
```

Build the third image from `Dockerfile.analysis` with `cloudbuild.archive.yaml`.
Set `ANALYSIS_JOB_NAME` (full Cloud Run Job resource name) on the checker. Set
`GOOGLE_AI_API_KEY`, `MEETING_ARCHIVE_CHAT_WEBHOOK`, and
`MEETING_CHAT_SPACE_ID` and `MEETING_TEAM_ROSTER_JSON` on the analysis job. The
webhook must belong to that
space; the job rejects a webhook for another space. The existing organizer's
General-channel webhook is not used by this flow.

A powerful tool that automatically organizes your Google Meet transcripts into categorized folders using Google Drive API and Gemini AI. Perfect for teams and individuals who want to maintain a clean and organized meeting archive.

## 🌟 Features

- 🔄 Automatic categorization of meeting transcripts
- 📁 Smart folder organization based on meeting content
- 🤖 Powered by Google's Gemini AI for accurate categorization
- 🔒 Secure authentication using Workload Identity Federation
- ⏰ Scheduled processing with Cloud Scheduler
- 📊 Detailed logging and monitoring

## 🚀 Quick Start

### Prerequisites

- Python 3.13
- Google Cloud account
- Google Meet recordings with transcripts enabled
- Share your and tell your team to share their Google Meet recordings folder with the service account email (format: `meetingintern@[YOUR_PROJECT_ID].iam.gserviceaccount.com`) 
- Share all destination folders to the MeetingIntern (format: `meetingintern@[YOUR_PROJECT_ID].iam.gserviceaccount.com`) as Content Manager role.

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/clarvo-ai/meetingintern.git
cd meetingintern
```

2. **Install dependencies**
```bash
pip install -r requirements.txt
```

3. **Set up Google Cloud Project**
   - Create a new project in [Google Cloud Console](https://console.cloud.google.com)
   - Enable required APIs:
     - Cloud Functions API
     - Cloud Scheduler API
     - Cloud Build API
     - Container Registry API
     - Drive API
     - Gemini API
     - IAM API

4. **Create Service Account**
   - Go to [IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
   - Click "Create Service Account"
   - Name it "meetingintern"
   - Grant the following roles:
     - Cloud Functions Invoker
     - Cloud Scheduler Admin
     - Cloud Build Service Account
     - Storage Admin
     - Service Account User
   - Create and download the JSON key file
   - Store it securely and set the path in your environment:
     ```bash
     export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/service-account-key.json"
     ```

5. **Configure Environment**
   Create a `.env` file in the project root:
   ```env
   # Required settings
   GOOGLE_AI_API_KEY=your-gemini-api-key
   USERS_TO_PROCESS=user1@domain.com,user2@domain.com

   # Optional: Choose Gemini model
   GEMINI_MODEL=gemini-2.0-flash

   # Folder mapping for each meeting category (recommended to keep a "Other" folder for meetings that you dont want confused with others)
   # Your FolderId is: the last part of the drive folder URL ----> "https://drive.google.com/drive/folders/1YourTeamMeetingFolderId"
   FOLDER_MAPPING={
       "Team Meeting": "1YourTeamMeetingFolderId",
       "Client Meeting": "1YourClientMeetingFolderId",
       "Investor Meeting": "1YourInvestorMeetingFolderId",
       "Project Update": "1YourProjectUpdateFolderId",
       "Sales Call": "1YourSalesCallFolderId",
       "HR & Recruitment": "1YourHRFolderId",
       "Other": "1YourOtherMeetingFolderId" 
   }

### Chat Integration

The application supports multiple chat platforms for notifications & summaries of important meetings. 
This is used to either forward user meetings to development team chats, etc. 
Be careful what chats you send specific meeting summaries to, as the summaries will be visible to everyone in that chat. 

Choose your preferred platform:

#### Google Chat
1. Go to your Google Chat space
2. Click the space name > Manage webhooks
3. Click "Add webhook"
4. Name it "Meeting Summaries"
5. Copy the webhook URL to your `.env` file:
   ```env
   CHAT_WEBHOOK_URL=your-google-chat-webhook-url
   ```

#### Slack
1. Go to your Slack workspace
2. Create a new app at https://api.slack.com/apps
3. Enable "Incoming Webhooks"
4. Create a new webhook for your channel
5. Copy the webhook URL to your `.env` file:
   ```env
   CHAT_WEBHOOK_URL=your-slack-webhook-url
   ```

#### Discord
1. Go to your Discord server
2. Edit a channel > Integrations > Create Webhook
3. Name it "Meeting Summaries"
4. Copy the webhook URL to your `.env` file:
   ```env
   CHAT_WEBHOOK_URL=your-discord-webhook-url
   ```

#### Other Platforms
The application can be extended to support other chat platforms by:
1. Creating a webhook in your preferred platform
2. Setting the webhook URL in your `.env` file
3. Modifying `chat_api.py` to support your platform's message format

### Deployment

1. **Install Google Cloud SDK**
   - Follow the [official installation guide](https://cloud.google.com/sdk/docs/install)

2. **Initialize and deploy**
```bash
# Initialize gcloud
gcloud init

# Enable required APIs
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  iamcredentials.googleapis.com

# Deploy using Cloud Build
gcloud builds submit --config cloudbuild.yaml

# Manually test the function (recommended before leaving project running)
gcloud scheduler jobs run meetingintern-job --location=[YOUR_CHOSEN_SERVER_LOCATION]
```

### Cloud Build Configuration

The `cloudbuild.yaml` file uses Cloud Build substitutions to make deployment more flexible. You can customize the deployment by setting these variables:

```bash
# Set your desired region
gcloud config set compute/region europe-west1

# Create a new build with custom substitutions
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=europe-west1,_REPOSITORY=meetingintern,_SERVICE_NAME=meetingintern
```

The available substitutions are:
- `_REGION`: The Google Cloud region (default: europe-west1)
- `_REPOSITORY`: The Artifact Registry repository name (default: meetingintern)
- `_SERVICE_NAME`: The Cloud Run service name (default: meetingintern)
- `_SERVICE_ACCOUNT`: Automatically generated as `${_SERVICE_NAME}@${PROJECT_ID}.iam.gserviceaccount.com`
- `_SERVICE_URL`: Automatically generated as `https://${_SERVICE_NAME}-${PROJECT_ID}.${_REGION}.run.app/`

To use a different region or service name, you can override these values during deployment:

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_SERVICE_NAME=my-meetingintern
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository at https://github.com/clarvo-ai/meetingintern
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/meetingintern.git
   cd meetingintern
   ```
3. Add the original repository as upstream:
   ```bash
   git remote add upstream https://github.com/clarvo-ai/meetingintern.git
   ```
4. Create your feature branch:
   ```bash
   git checkout -b feature/AmazingFeature
   ```
5. Make your changes and commit them:
   ```bash
   git commit -m 'Add some AmazingFeature'
   ```
6. Push to your fork:
   ```bash
   git push origin feature/AmazingFeature
   ```
7. Open a Pull Request on GitHub

To keep your fork up to date with the main repository:
```bash
git fetch upstream
git checkout main
git merge upstream/main
```

## 📁 Project Structure

```
meetingintern/
├── .env                    # Environment configuration
├── requirements.txt        # Python dependencies
├── Dockerfile             # Container configuration
├── cloudbuild.yaml        # Cloud Build configuration
├── main.py               # Main application logic
├── drive_api.py          # Google Drive API integration
├── gemini_api.py         # Gemini AI integration
└── chat_api.py           # Chat functionality
```

## 🔧 Configuration

### Meeting Categories

The application comes with default meeting categories, but you can customize them to match your organization's needs:

1. Edit the meeting types in `gemini_api.py`:
   ```python
   # Update these categories in the determine_meeting_type method
   valid_categories = {
       "Daily Team Meeting",
       "Investor Meeting",
       "Client Meeting",
       "HR & Recruitment",
       "User Research Meeting",
       "Product Development Meeting",
       "Other"
   }
   ```

2. Update the folder mapping in your `.env` file to match your categories:
   ```json
   {
       "Daily Team Meeting": "folder-id-1",
       "Investor Meeting": "folder-id-2",
       "Client Meeting": "folder-id-3",
       "HR & Recruitment": "folder-id-4",
       "User Research Meeting": "folder-id-5",
       "Product Development Meeting": "folder-id-6",
       "Other": "folder-id-7"
   }
   ```

3. Customize the meeting type detection prompt in `gemini_api.py` to better match your organization's meeting patterns.

### Custom Properties

The application uses a custom property to track processed files. You can customize this in `drive_api.py`:

```python
# Change this property name if needed
self.PROCESSED_PROPERTY = 'meeting_processed'
```

### Scheduling

The default configuration runs every 2 hours. To modify the schedule:

```bash
gcloud scheduler jobs update http meetingintern-job \
  --schedule "YOUR_CRON_SCHEDULE"
```

## 🔍 Monitoring

- View function logs:
```bash
gcloud logging read "resource.type=cloud_function AND resource.labels.function_name=meetingintern"
```

- Check scheduler job status:
```bash
gcloud scheduler jobs get-execution-history meetingintern-job
```

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Google Cloud Platform
- Google Drive API
- Google Gemini AI
- All contributors and users of this project

## 📞 Support

If you encounter any issues or have questions, please:
1. Reach out to me on [LinkedIn](https://www.linkedin.com/in/holmbrob) and include a subject in the connection request.
2. Join our [Discord community](https://discord.gg/FAaFmrDNdW) for real-time support

## 🔄 Updates

This project will most likely not be updated by me, as it is running on autopilot.

The archive job can send content-free operational events to the Clarvo
Intelligence logs service. Set `CLARVO_LOGS_URL` to the authenticated HTTPS
event endpoint and mount a dedicated producer token as a protected file named
by `CLARVO_LOGS_TOKEN_FILE`. No token or webhook URL belongs in source control.
The job sends a stable deduplicated event after a completed archive or failed
processing claim. Delivery is best-effort and receives at most one second at
job shutdown; an unavailable log service never blocks archive processing.
