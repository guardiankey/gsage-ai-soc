# Microsoft Teams and Azure Bot Setup

This guide complements `docs/channels/teams.md` and focuses on the Microsoft-side work required to connect gSage to Microsoft Teams.

## What gSage expects

For each gSage organization, the Teams channel expects one profile with:

- `app_id`: the Microsoft App ID from the Entra app registration
- `app_password`: the client secret value
- `tenant_id`: the Microsoft tenant ID

The public webhook endpoint is:

```text
https://<your-host>/api/v1/channels/teams/<profile_id>/messages
```

The `profile_id` comes from gSage after the Teams channel profile is created.

Current operational behavior:

- the supported path is 1:1 personal chat
- on first contact, gSage may resolve the sender through Microsoft Graph using `aadObjectId`
- the Graph lookup matches the Entra user `mail` or `userPrincipalName` against the existing gSage user email
- once matched, the Entra object ID is stored on the gSage user, and later messages no longer need Graph

This means the email address already stored in gSage should match the user's Entra primary email or UPN.

## Recommended Microsoft design

For most deployments:

- use a single-tenant Entra app
- create one Azure Bot per gSage organization
- publish the Teams app only inside the target tenant

Microsoft is deprecating new multi-tenant bot creation for this resource path, so single-tenant is the safer default for new deployments.

## gSage-side preparation

Before configuring Azure Bot, create the Teams channel profile in gSage so you know the `profile_id`.

Example:

```bash
echo '<client-secret>' | python -m ops_cli channels teams upsert \
  --org-slug acme \
  --description 'Main SOC bot' \
  --app-id 11111111-1111-1111-1111-111111111111 \
  --tenant-id 22222222-2222-2222-2222-222222222222 \
  --app-password-stdin
```

The command returns the new profile ID and the webhook path. That value is needed when you configure the bot endpoint in Azure.

## Microsoft-side setup

### 1. Register the Entra application

In Microsoft Entra admin center:

1. Go to `Microsoft Entra ID -> App registrations -> New registration`.
2. Choose a clear name such as `gSage Teams Bot - <org name>`.
3. Prefer `Accounts in this organizational directory only`.
4. Register the app.
5. Save:
   - `Application (client) ID`
   - `Directory (tenant) ID`

### 2. Create a client secret

Under `Certificates & secrets`:

1. Create a new client secret.
2. Copy the secret value immediately.

That value is the `app_password` stored in gSage.

### 3. Grant Microsoft Graph permission for first-contact resolution

gSage uses Microsoft Graph only to resolve a Teams sender's Entra object ID to an email address on first contact.

Add this permission in the app registration:

- `Microsoft Graph -> Application permissions -> User.Read.All`

Then grant admin consent.

Why this is required:

- gSage calls `GET /v1.0/users/{aadObjectId}?$select=id,mail,userPrincipalName`
- the channel uses client credentials for that lookup
- the code expects an application permission, not a delegated permission

### 4. Create the Azure Bot resource

In Azure portal:

1. Create an `Azure Bot` resource.
2. Use the same app identity created above.
3. Keep the bot single-tenant unless you explicitly need a different model.
4. After deployment, open the bot resource configuration.

### 5. Set the messaging endpoint

Set the bot messaging endpoint to the exact gSage webhook:

```text
https://<your-host>/api/v1/channels/teams/<profile_id>/messages
```

Requirements:

- the endpoint must be public
- it must be reachable over HTTPS
- the `<profile_id>` must be the profile for the correct gSage organization

Optional note:

gSage also exposes a WebSocket endpoint for Bot Framework streaming, but standard deployments should start with the regular HTTPS messaging endpoint above.

### 6. Enable the Microsoft Teams channel in Azure Bot

In the Azure Bot resource:

1. Open `Channels`.
2. Enable `Microsoft Teams`.
3. Save the channel configuration.

If you are in a GCC environment, review the specific Teams for Government guidance before enabling the channel.

### 7. Build or package the Teams app manifest

gSage does not automate Teams manifest publication. Operators install the manifest manually.

At minimum, the Teams app package should:

- define the bot using the same Microsoft App ID
- expose personal scope for 1:1 conversations
- include valid manifest metadata and app icons

Distribution options:

- upload as a custom app for testing
- publish to your organization catalog for internal use
- publish to the Teams Store only if you need a public distribution model

For most gSage deployments, `upload custom app` or `publish to org` is the right path.

## Validation checklist

Before handing the channel to end users, confirm all of the following:

- the Azure Bot resource exists and is healthy
- the Microsoft Teams channel is enabled on the bot
- the messaging endpoint uses the correct `profile_id`
- the app registration has `User.Read.All` application permission with admin consent
- the user already exists in gSage with an email matching Entra `mail` or `userPrincipalName`
- the bot manifest is installed in personal scope

Useful gSage probe:

```text
GET /api/v1/channels/teams/<profile_id>/health
```

## Troubleshooting

### Microsoft returns 401 or invalid bot token errors

Usually one of these is wrong:

- the Azure Bot is using a different App ID than the one stored in gSage
- the client secret rotated in Entra but not in gSage
- the endpoint points to the wrong `profile_id`
- the bot is bound to the wrong tenant model

### Messages reach the bot but gSage says the sender is not mapped

Most common causes:

- the gSage user email does not match Entra `mail` or `userPrincipalName`
- `User.Read.All` application permission is missing or admin consent was not granted
- the bot was configured with the wrong tenant ID

### The bot works for some users but not others on first contact

This is often a directory-data issue rather than a bot issue:

- some users have no `mail` value and rely on `userPrincipalName`
- the gSage directory has a different canonical email than Entra
- the user is chatting from a different tenant than the one registered for the bot

### Teams app installs but chat does not start correctly

Check:

- the manifest uses the correct bot App ID
- the bot is available in personal scope
- the Teams channel is enabled in Azure Bot
- the app was uploaded to the correct tenant

### Group chats and channel chats are unreliable

The current gSage Teams integration is designed and validated for personal 1:1 conversations. Group and channel chat scenarios should be treated as out of scope unless you test them explicitly.

## Official Microsoft references

- Register a bot with Azure: <https://learn.microsoft.com/en-us/azure/bot-service/bot-service-quickstart-registration?view=azure-bot-service-4.0>
- Create a bot for Teams: <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/create-a-bot-for-teams>
- Publish a Teams app: <https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-publish-overview>
- Microsoft Graph permissions reference: <https://learn.microsoft.com/en-us/graph/permissions-reference>
