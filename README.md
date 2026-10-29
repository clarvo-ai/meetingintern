# MeetingIntern

## Meet audio archive MVP

`audio_archive.py` is a separate path for the newer Google Meet folder layout.
The checker scans meeting folders under configured personal `Google Meet`
folders every 15 minutes. It waits until a Meet Doc and an MP4 recording are
both unchanged for five minutes. If a recording has not appeared three hours after
the Doc's last update, it archives the Meet transcript alone. A visible but
unfinished recording is allowed to settle before processing. Shortcuts are
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
The source folders must be shared with the job's service account; access to a
shortcut does not grant access to the shortcut's target. Keep the existing
organizer and Chat summary pipeline configured separately.

## Meeting briefs and Chat updates

When an archive job publishes a full English transcript, it marks that meeting
ready for a separate analysis job. The 15-minute checker dispatches analysis
jobs independently, so a summary or Chat failure never reruns audio extraction.
The analysis job reads the full archived transcript and uses Gemini
`gemini-3.5-flash-lite` in focused passes for questions and replies, discussed
next steps, and a detailed narrative. Every pass receives the complete spoken
transcript; audio chunking remains an earlier, separate step. The report pairs
external questions with Clarvo's actual answer or an open status. Agreed,
proposed, and unclear next steps remain labeled as meeting notes to verify.
An individual owner is only used when the original Meet transcript provides a
named speaker and the discussion supports the assignment. The job creates a
companion `Meeting Brief` Doc in the same shared folder; the full English
transcript Doc stays untouched. Firestore stores the type, tags, brief ID, and
delivery status for later reporting and safe retries.

The Chat header combines confirmed Meet joiners with matched Calendar invitees
by unambiguous name or email matches. Anonymous first-name matches to an
invitee email are marked as likely and unverified; invitation response status
never establishes attendance. The header keeps the meeting type when supported,
topic tags, Finland-local meeting time, relevant company context, and a matched
Calendar event link. The full transcript is the first URL for the Chat preview,
followed by the event and companion brief. The job posts one update to the
configured Google Chat space. Google Chat incoming webhooks
do not support idempotent request IDs, so a send with an uncertain result is
held for manual reconciliation rather than retried into a possible duplicate.
Customer demo search links and the representative's own feedback are outside
this increment.

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
   GEMINI_MODEL=gemini-3.5-flash-lite

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
