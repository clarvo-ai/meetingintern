# Google Meet Recording Organizer

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

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/meetings-organizer.git
cd meetings-organizer
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
   - Name it "meet-organizer"
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
gcloud scheduler jobs run meet-organizer-job --location=[YOUR_CHOSEN_SERVER_LOCATION]
```

### Cloud Build Configuration

The `cloudbuild.yaml` file uses Cloud Build substitutions to make deployment more flexible. You can customize the deployment by setting these variables:

```bash
# Set your desired region
gcloud config set compute/region europe-west1

# Create a new build with custom substitutions
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=europe-west1,_REPOSITORY=meet-organizer,_SERVICE_NAME=meet-organizer
```

The available substitutions are:
- `_REGION`: The Google Cloud region (default: europe-west1)
- `_REPOSITORY`: The Artifact Registry repository name (default: meet-organizer)
- `_SERVICE_NAME`: The Cloud Run service name (default: meet-organizer)
- `_SERVICE_ACCOUNT`: Automatically generated as `${_SERVICE_NAME}@${PROJECT_ID}.iam.gserviceaccount.com`
- `_SERVICE_URL`: Automatically generated as `https://${_SERVICE_NAME}-${PROJECT_ID}.${_REGION}.run.app/`

To use a different region or service name, you can override these values during deployment:

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_SERVICE_NAME=my-meet-organizer
```

## �� Project Structure

```
meetings-organizer/
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
gcloud scheduler jobs update http meet-organizer-job \
  --schedule "YOUR_CRON_SCHEDULE"
```

## 🔍 Monitoring

- View function logs:
```bash
gcloud logging read "resource.type=cloud_function AND resource.labels.function_name=meet-organizer"
```

- Check scheduler job status:
```bash
gcloud scheduler jobs get-execution-history meet-organizer-job
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

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