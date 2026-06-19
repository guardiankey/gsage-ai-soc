# gSage AI — Web Client User Guide

This guide covers everything you need to know to use the gSage AI web interface.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Navigation](#navigation)
3. [Chat](#chat)
4. [Knowledge Base](#knowledge-base)
5. [Prompt Library](#prompt-library)
6. [Approvals](#approvals)
7. [Files](#files)
8. [Tasks](#tasks)
9. [Scheduled Jobs](#scheduled-jobs)
10. [Approval Rules](#approval-rules)
11. [DataStores](#datastores)
12. [Profile & Security](#profile--security)
13. [API Keys](#api-keys)
14. [Settings & Preferences](#settings--preferences)

---

## Getting Started

### Creating an Account

1. Open the application URL in your browser.
2. Click **Create account** on the login page.
3. Fill in:
   - **Full name** — your display name
   - **Email** — used for login
   - **Password** — minimum 8 characters
   - **Organization name** — the name of your team or company
4. Click **Create account**. You will be logged in automatically.

> Self-registration may be disabled by your administrator. Contact them if the option is not available.

### Logging In

1. Enter your **email** and **password**.
2. Click **Sign in**.
3. If Two-Factor Authentication (2FA) is enabled on your account, you will be prompted for a 6-digit code from your authenticator app. Enter it to continue.

You can toggle password visibility using the eye icon next to the password field.

### Two-Factor Authentication (2FA) During Login

If your account has 2FA enabled:

- Enter the **6-digit code** from your authenticator app (Google Authenticator, Authy, etc.). The form auto-submits when all 6 digits are entered.
- Alternatively, click **Use a backup code instead** and enter one of your saved backup codes.
- Check **Remember this device for 30 days** to skip 2FA on this browser for 30 days.

---

## Navigation

The top navigation bar provides access to all sections of the application:

| Icon | Section | Description |
|------|---------|-------------|
| 💬 | **Chat** | Conversational AI assistant |
| 📖 | **Knowledge** | Knowledge base management |
| 📚 | **Prompts** | Prompt template library |
| ☑️ | **Approvals** | Tool execution approval requests |
| 📁 | **Files** | Generated files and document templates |
| 📊 | **Tasks** | Background task monitoring |
| 🕐 | **Scheduled Jobs** | Recurring automated tasks |
| 🛡️ | **Approval Rules** | Approval delegation rules |
| 🗄️ | **DataStores** | Structured data storage |

On the right side of the navigation bar you will find:

- **Theme toggle** — switch between light and dark mode
- **Language toggle** — switch between English and Portuguese
- **User menu** — access your profile, API keys, switch organization, or sign out

On mobile devices, the navigation collapses into a hamburger menu.

### Switching Organizations

If you belong to multiple organizations, click your **user avatar** in the top-right corner and select **Switch organization**. A submenu will show all your organizations with your role in each one.

---

## Chat

The Chat page is where you interact with the AI assistant. It is divided into two areas: a **conversation sidebar** on the left and the **chat window** on the right.

### Conversations

- Click **New conversation** to start a fresh conversation. A title is automatically generated from your first message.
- Click any conversation in the sidebar to resume it.
- Hover over a conversation to see options: **Rename** or **Archive**.
- Click **View archived** at the bottom of the sidebar to see archived conversations, where you can **Unarchive** them.

### Sending Messages

- Type your message in the input box at the bottom.
- Press **Enter** to send, or **Shift+Enter** to add a new line.
- While the AI is responding, a **Stop generation** button appears to interrupt the response.

### Approval Waiting State

When the AI needs to execute a sensitive tool, it may pause and wait for approval. You will see an **"Approval required"** banner with a link to the Approvals page. Once approved, the AI continues automatically — no need to resend your message.

---

## Knowledge Base

The Knowledge Base page lets you manage the documents the AI uses to answer your questions. It has three tabs: **Search**, **Documents**, and **Ingest**.

### Search

1. Type a query in the search box.
2. Click **Search** or press Enter.
3. Results show matching documents with a **relevance score** (percentage).

### Documents

This tab lists all documents stored in the knowledge base.

- Click **Add document** to manually add a document:
  - **Title** (required)
  - **Description** (optional)
  - **URL** — the content is fetched automatically from the URL
  - **Content** — paste text directly (leave empty if using a URL)
- To delete a document, click the trash icon and confirm.

### Ingest (File Upload)

Upload files to be processed and added to the knowledge base:

1. **Drag & drop** files onto the upload area, or **click** to browse.
2. Supported formats: PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, HTML, JSON, XML, EML, ZIP, TAR.GZ (max 10 MB per file).
3. Uploaded files appear in a list with their processing status:
   - **Queued** — waiting to be processed
   - **Processing** — being ingested
   - **Completed** — successfully added
   - **Failed** — an error occurred (details shown)

The status updates automatically every few seconds.

---

## Prompt Library

The Prompt Library lets you create, organize, and reuse prompt templates.
Access it from the top navigation bar (📚 **Prompts**) or directly from the
chat input via the library icon.

### Browsing Prompts

- **All tab** — view all prompts visible to you (personal + department + org).
- **Personal / Department / Organization tabs** — filter by scope.
- **Sidebar tree** — browse by category. Click a category to filter.
- **Search bar** — type to search across title, description, and content.

### Creating a Prompt

1. Click **+ New Prompt**.
2. Fill in:
   - **Title** — a short, descriptive name.
   - **Content** — the prompt text (up to 10,000 characters).
   - **Description** — optional short description shown in lists.
   - **Visibility** — who can see this prompt:
     - *Only me* — personal use.
     - *My department* — shared with your department.
     - *Whole organization* — shared with everyone in the org.
   - **Category** — optional folder for organization.
3. Click **Create**.

### Using a Prompt in Chat

1. In the chat input area, click the **Library** icon (📚).
2. Browse or search for a prompt in the modal.
3. Click the prompt — its content fills the chat input.
4. Edit the message as needed, then send.

### Favorites

- Click the **star** icon on any prompt card to add it to your favorites.
- Use the ⭐ **Favorites** shortcut in the category tree or modal sidebar.

### Chat Input Enhancements

- **4-line input** — the message box is taller by default for longer prompts.
- **Enter toggle** — click the ↵ icon to toggle whether Enter sends or adds a newline.
- **Resizable** — drag the bottom-right corner to resize the input area.

---

## Approvals

The Approvals page manages Human-in-the-Loop (HITL) tool execution requests. When the AI wants to execute a sensitive tool, it creates an approval request that must be reviewed.

### Pending Tab

Shows all requests waiting for your review. Each card displays:

- **Tool name** — the tool the AI wants to execute
- **Requester** — who triggered the tool
- **Time** — when the request was made

Click a card to open the detail view.

### Reviewing an Approval

The detail dialog shows:

- **Justification** — why the AI wants to run this tool
- **Tool input** — the exact parameters that will be used
- **Requirements** — any conditions that must be met
- **Delegated to** — if the request was routed to a specific approver
- **Expires at** — deadline, after which the request times out

To decide:

1. Optionally add a **comment**.
2. Click **Approve** (green) to allow execution, or **Reject** (red) to deny it.

After approval, the AI run continues automatically.

### History Tab

Shows all past approvals with their outcomes (Approved, Rejected, Timeout).

---

## Files

The Files page manages tool-generated files and reusable document templates.

### Generated Files

Files created automatically by tools during AI execution (e.g., reports, scan results).

- Use the **search box** to filter by filename.
- Use the **tool filter** to show files from a specific tool.
- Click **Download** to save a file to your computer.
- Some files have **expiration dates** shown as a badge.

### Templates

Reusable document templates that can be used by the AI during conversations.

- Click **Upload Template** to add a new template:
  - Select a file (allowed types: `.md, .docx, .xlsx, .pptx, .pdf, .tex, .zip, .txt, .csv, .json, .yaml, .html, .xml`)
  - Add an optional **description**
  - Choose **visibility**: "Only me" (personal) or "Organization" (shared)
- To delete a template, click the trash icon and confirm.

---

## Tasks

The Tasks page lets you monitor background task executions — long-running operations triggered by the AI (e.g., network scans, data analysis).

### Filtering

Use the status filter buttons at the top:

- **All** — show all tasks
- **Running** — tasks currently in progress
- **Queued** — tasks waiting to start
- **Completed** — successfully finished tasks
- **Failed** — tasks that encountered errors

### Viewing Task Details

Click any task to open its detail dialog:

- **Output** — the task result (shown as formatted code)
- **Error** — error message if the task failed (shown in red)
- **Started** — when the task began execution

The page **auto-refreshes every 5 seconds** when there are running or queued tasks.

---

## Scheduled Jobs

Scheduled Jobs let you create recurring automated tasks that run on a schedule (cron-based).

### Viewing Jobs

- Use the **type filter** to show only `PROMPT_RUN` or `SYSTEM_TASK` jobs.
- Use the **status filter** to show Active, Inactive, or All jobs.
- Each card shows: name, type, cron expression, timezone, last run status, and run count.

### Creating a Job

1. Click **New Job**.
2. Fill in:
   - **Name** (required) — e.g., "Daily security summary"
   - **Description** (optional)
   - **Type** — select the job type
   - **Cron expression** (required) — defines the schedule (e.g., `0 9 * * *` for daily at 9 AM)
   - **Timezone** — the timezone for the cron schedule
   - **Prompt** — the message the AI will process on each execution
   - **Max runs** (optional) — limit total executions
3. Click **Create**.

### Managing Jobs

- **Activate/Deactivate** — toggle the play/pause button on a job card, or use the button in the detail view.
- **Edit** — click the pencil icon to modify a job.
- **Delete** — click the trash icon and confirm. This permanently removes the job.

### Job Status Indicators

- ✅ Success — last run completed successfully
- ⏳ Running — currently executing
- ❌ Failed — last run encountered an error
- ⏭️ Skipped — last run was skipped

---

## Approval Rules

Approval Rules define which tool executions require human approval and who should approve them. This is an **admin feature**.

### How Rules Work

Each rule specifies:

- **Tool pattern** — which tool(s) the rule applies to. Use an exact tool name (e.g., `web_search`) or `*` to match all tools.
- **User pattern** — which user(s) trigger the rule. Use a user UUID or `*` for all users.
- **Approver** — the organization member who must approve the request.
- **Priority** — when multiple rules match, the one with the highest priority wins.

### Creating a Rule

1. Click **New Rule**.
2. Fill in:
   - **Tool pattern** (required)
   - **User pattern** (required, defaults to `*`)
   - **Approver** (required) — select from the dropdown of organization members
   - **Priority** — higher number = higher priority
   - **Description** (optional)
3. Click **Create**.

### Managing Rules

- **Edit** — click the pencil icon on a rule card.
- **Activate/Deactivate** — toggle rules on or off without deleting them.
- **Delete** — click the trash icon and confirm.

Use the **filter buttons** (All, Active, Inactive) to find rules quickly.

---

## DataStores

DataStores let you create and manage structured data collections. Each store contains JSON records that can be created, read, updated, and deleted.

### Store List (Left Panel)

- Click **New Store** to create a data store.
- Each card shows the store name, visibility (Shared/Private), record count, and active status.
- Click a store to view its records in the right panel.
- Hover to reveal **edit** (pencil) and **delete** (trash) buttons.

### Creating a Store

1. Click **New Store**.
2. Fill in:
   - **Name** (required) — e.g., `threat-intel`
   - **Description** (optional)
   - **Visibility** — `Shared` (visible to entire org) or `Private`
   - **Max records** — limit the number of records (0 = unlimited)
   - **JSON Schema** (optional) — a JSON object describing the expected record structure
3. Click **Create**.

### Records Panel (Right Panel)

When you select a store, its records appear in the right panel:

- Click **Add Record** to insert a new record.
- Each record shows its JSON data in a formatted preview.
- Hover over a record to reveal **edit** and **delete** buttons.
- Use pagination at the bottom to navigate through records.

### Adding / Editing a Record

1. Click **Add Record** or the edit icon on an existing record.
2. Enter valid JSON in the text area (e.g., `{"ip": "10.0.0.1", "severity": "high"}`).
3. The form validates your JSON in real-time — an error message appears if the JSON is invalid.
4. Click **Add** or **Save**.

### Deleting

- **Delete a store** — removes the store and **all its records permanently**. Confirmation required.
- **Delete a record** — removes a single record. Confirmation required.

---

## Profile & Security

Access your profile by clicking your **user avatar** → **Profile**.

### Account Information

View your name, email, current organization, and role. If you belong to multiple organizations, all memberships are listed.

### Edit Profile

Click **Edit profile** to change your display name.

### Change Password

1. Click **Change password**.
2. Enter your **current password**.
3. Enter and confirm your **new password**.
4. Click **Change**.

> Accounts using external authentication (SSO) cannot change their password here.

### Two-Factor Authentication (2FA)

The 2FA card shows your current enrollment status.

**To enable 2FA:**

1. Click **Enable 2FA** — you will be redirected to the setup page.
2. Scan the **QR code** with your authenticator app (or enter the secret manually).
3. Enter the **6-digit code** from your app to confirm.
4. **Save your backup codes!** They are shown only once. You can copy them to your clipboard or download them as a text file.

**To disable 2FA:**

1. Click **Disable 2FA**.
2. Confirm with your password or a valid OTP code.

**To regenerate backup codes:**

1. Click **Regenerate backup codes**.
2. Confirm with your password or OTP code.
3. Save the new codes. The old codes are permanently invalidated.

---

## API Keys

Access API Keys by clicking your **user avatar** → **API Keys**.

API keys allow programmatic access to the gSage AI API (e.g., from scripts or the CLI client).

### Personal Keys

Keys tied to your user account.

1. Click **Create key**.
2. Enter a **key name** (e.g., "My CLI key").
3. Click **Create**.
4. **Copy the key immediately** — it will not be shown again.

### Organization Keys (Admin Only)

Keys shared across the organization, visible only to admins.

The creation process is the same as personal keys.

### Deleting a Key

Click the trash icon next to a key and confirm. The key is permanently revoked and cannot be recovered.

---

## Settings & Preferences

### Theme

Click the **moon/sun icon** in the top navigation bar to toggle between **light** and **dark** mode. Your preference is saved automatically.

### Language

Click the **language icon** in the top navigation bar to switch between **English** and **Português (BR)**. The entire interface updates immediately.

---

## Common Interface Patterns

### Pagination

Most list views show 20 items per page. Use the **Previous**/**Next** buttons at the bottom to navigate between pages. The current page and total pages are displayed.

### Confirmations

Destructive actions (delete, disable, etc.) always require a confirmation dialog before proceeding.

### Notifications

Success and error messages appear as **toast notifications** in the top-right corner. They auto-dismiss after a few seconds, or you can close them manually.

### Loading States

When data is being loaded, skeleton placeholders or loading indicators are shown. Buttons display a loading state during form submissions to prevent double-clicks.
